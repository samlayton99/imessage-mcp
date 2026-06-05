"""The server-side scheduler — runs the summary worker on cadence, as a SEPARATE process.

The always-on host (VPS or Mac mini) is the timekeeper. When a daily/weekly/monthly time is due, the
scheduler SPAWNS ``text-triage summarize --source raw-store`` as a subprocess, so a long LLM run never
blocks the MCP serving loop (honoring the "server is its own process / pipeline is its own process"
split). ``POST /trigger`` runs the same path immediately (the collector's first-wake ``on_open``).

The date math (``prev_fire``/``due_runs``) is pure and unit-tested; naive local datetimes implement
``timezone: auto`` (the host's clock). ``on_open`` is event-driven (via /trigger), not time-scheduled,
so it is ignored by the time loop.
"""
from __future__ import annotations

import datetime
import json
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Optional, Union

__all__ = ["prev_fire", "due_runs", "build_command", "spawn", "make_trigger", "run_loop"]

_WEEKDAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
_T = r"(\d{1,2}):(\d{2})"
_DAILY_RE = re.compile(rf"^{_T}$")
_WEEKLY_RE = re.compile(rf"^(mon|tue|wed|thu|fri|sat|sun)\s+{_T}$")
_MONTHLY_RE = re.compile(rf"^(\d{{1,2}})\s+{_T}$")


def _days_in_month(year: int, month: int) -> int:
    nxt = datetime.date(year + 1, 1, 1) if month == 12 else datetime.date(year, month + 1, 1)
    return (nxt - datetime.date(year, month, 1)).days


def prev_fire(spec: str, now: datetime.datetime) -> Optional[datetime.datetime]:
    """The most recent moment at/before ``now`` that ``spec`` fires, or ``None`` for ``on_open`` /
    unparseable specs. Specs: ``"HH:MM"`` (daily), ``"mon HH:MM"`` (weekly), ``"D HH:MM"`` (monthly)."""
    s = spec.strip().lower()
    m = _DAILY_RE.match(s)
    if m:
        cand = now.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)
        return cand if cand <= now else cand - datetime.timedelta(days=1)
    m = _WEEKLY_RE.match(s)
    if m:
        cand = now.replace(hour=int(m.group(2)), minute=int(m.group(3)), second=0, microsecond=0)
        cand -= datetime.timedelta(days=(now.weekday() - _WEEKDAYS[m.group(1)]) % 7)
        return cand if cand <= now else cand - datetime.timedelta(days=7)
    m = _MONTHLY_RE.match(s)
    if m:
        dom, hour, minute = int(m.group(1)), int(m.group(2)), int(m.group(3))
        year, month = now.year, now.month
        for _ in range(13):  # walk back month by month until a real day <= now
            if dom <= _days_in_month(year, month):
                cand = datetime.datetime(year, month, dom, hour, minute)
                if cand <= now:
                    return cand
            month -= 1
            if month == 0:
                month, year = 12, year - 1
    return None  # on_open / unparseable


def due_runs(schedule, now: datetime.datetime,
             last_runs: dict[str, datetime.datetime]) -> list[str]:
    """The modes whose most recent scheduled fire is newer than their last run (so they're owed a
    run now). A mode with no prior run is due. ``schedule`` is a ``config.Schedule``."""
    out = []
    for mode in ("daily", "weekly", "monthly"):
        fires = [f for spec in getattr(schedule, mode) if (f := prev_fire(spec, now)) is not None]
        if not fires:
            continue
        most_recent = max(fires)
        last = last_runs.get(mode)
        if last is None or most_recent > last:
            out.append(mode)
    return out


def build_command(mode: str, *, out: Union[str, Path], raw_store: Optional[Union[str, Path]] = None,
                  config: Optional[Union[str, Path]] = None, watch: Optional[Union[str, Path]] = None,
                  python: Optional[str] = None, limit: Optional[int] = None) -> list[str]:
    """The argv to run one cadence as a subprocess: ``text-triage summarize --source raw-store``."""
    cmd = [python or sys.executable, "-m", "text_triage.cli", "summarize",
           "--mode", mode, "--source", "raw-store", "--out", str(out)]
    if raw_store:
        cmd += ["--raw-store", str(raw_store)]
    if config:
        cmd += ["--config", str(config)]
    if watch:
        cmd += ["--watch", str(watch)]
    if limit:
        cmd += ["--limit", str(limit)]
    return cmd


def spawn(mode: str, **kw) -> subprocess.Popen:
    """Spawn the summary worker for one cadence (a separate OS process)."""
    return subprocess.Popen(build_command(mode, **kw))


def make_trigger(*, state_path, raw_path, config_path=None, watch_path=None,
                 limit=None) -> Callable[[str], object]:
    """A callback the server's ``/trigger`` route invokes to run a cadence immediately."""
    def trigger(mode: str):
        return spawn(mode, out=state_path, raw_store=raw_path, config=config_path, watch=watch_path,
                     limit=limit)
    return trigger


def _load_last_runs(path: Optional[Union[str, Path]]) -> dict[str, datetime.datetime]:
    if not path or not Path(path).exists():
        return {}
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return {k: datetime.datetime.fromisoformat(v) for k, v in raw.items()}


def _save_last_runs(path: Optional[Union[str, Path]], last_runs: dict[str, datetime.datetime]) -> None:
    if not path:
        return
    Path(path).write_text(json.dumps({k: v.isoformat() for k, v in last_runs.items()}), encoding="utf-8")


def run_loop(config, *, state_path, raw_path, config_path=None, watch_path=None,
             last_runs_path=None, interval_seconds: int = 60, limit: Optional[int] = None,
             clock: Optional[Callable[[], datetime.datetime]] = None,
             sleep: Optional[Callable[[float], None]] = None,
             _spawn: Optional[Callable[..., object]] = None, _once: bool = False) -> None:
    """Poll the schedule; spawn the summary worker for each due cadence; persist last-run times so a
    restart doesn't re-run. A brand-new server (no ``last_runs_path`` file yet) seeds every cadence to
    now and schedules FORWARD — it never fires retroactively (the one-time bootstrap comes from the
    collector's ``/trigger``); an existing file still catches up after downtime. ``limit`` is passed to
    each spawned summarize as ``--limit``. ``clock``/``sleep``/``_spawn``/``_once`` are injectable."""
    clock = clock or datetime.datetime.now
    sleep = sleep or time.sleep
    runner = _spawn or spawn
    fresh_boot = bool(last_runs_path) and not Path(last_runs_path).exists()
    last_runs = _load_last_runs(last_runs_path)
    if fresh_boot:                          # forward-only: don't fire every cadence on the first ever boot
        last_runs = {m: clock() for m in ("daily", "weekly", "monthly")}
        _save_last_runs(last_runs_path, last_runs)
    while True:
        now = clock()
        for mode in due_runs(config.server.schedule, now, last_runs):
            runner(mode, out=state_path, raw_store=raw_path, config=config_path, watch=watch_path,
                   limit=limit)
            last_runs[mode] = now
        _save_last_runs(last_runs_path, last_runs)
        if _once:
            return
        sleep(interval_seconds)
