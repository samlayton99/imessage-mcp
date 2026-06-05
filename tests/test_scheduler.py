"""server/scheduler.py — the cadence clock. Pure date math (prev_fire/due_runs) + the spawn argv +
one loop iteration with an injected clock and a fake spawner (no real subprocess, no sleeping)."""
import datetime as dt
import json

from text_triage.config import Config, Schedule
from text_triage.server import scheduler


def D(y, m, d, h=0, mi=0):
    return dt.datetime(y, m, d, h, mi)


# ------------------------------------------------------------------- prev_fire
def test_prev_fire_daily():
    assert scheduler.prev_fire("21:00", D(2026, 6, 4, 22, 0)) == D(2026, 6, 4, 21, 0)
    assert scheduler.prev_fire("21:00", D(2026, 6, 4, 20, 0)) == D(2026, 6, 3, 21, 0)  # before today's time


def test_prev_fire_weekly():
    # Thu 2026-06-04 -> most recent Monday 03:00 is 2026-06-01
    assert scheduler.prev_fire("mon 03:00", D(2026, 6, 4, 10, 0)) == D(2026, 6, 1, 3, 0)
    # on Monday but before 03:00 -> the PREVIOUS Monday
    assert scheduler.prev_fire("mon 03:00", D(2026, 6, 1, 2, 0)) == D(2026, 5, 25, 3, 0)


def test_prev_fire_monthly_walks_back_over_month_boundary():
    assert scheduler.prev_fire("1 03:00", D(2026, 6, 4, 10, 0)) == D(2026, 6, 1, 3, 0)
    assert scheduler.prev_fire("1 03:00", D(2026, 6, 1, 2, 0)) == D(2026, 5, 1, 3, 0)  # before the 1st's time


def test_prev_fire_on_open_and_garbage_are_none():
    assert scheduler.prev_fire("on_open", D(2026, 6, 4)) is None
    assert scheduler.prev_fire("whenever", D(2026, 6, 4)) is None


# -------------------------------------------------------------------- due_runs
def test_due_runs_first_start_runs_everything():
    due = scheduler.due_runs(Schedule(), D(2026, 6, 4, 22, 0), {})
    assert set(due) == {"daily", "weekly", "monthly"}


def test_due_runs_nothing_due_when_last_run_is_recent():
    last = {"daily": D(2026, 6, 4, 21, 30), "weekly": D(2026, 6, 1, 3, 0), "monthly": D(2026, 6, 1, 3, 0)}
    assert scheduler.due_runs(Schedule(), D(2026, 6, 4, 22, 0), last) == []


def test_due_runs_daily_fires_after_its_scheduled_time():
    last = {"daily": D(2026, 6, 4, 20, 0), "weekly": D(2026, 6, 1, 3, 0), "monthly": D(2026, 6, 1, 3, 0)}
    assert scheduler.due_runs(Schedule(), D(2026, 6, 4, 22, 0), last) == ["daily"]


# --------------------------------------------------------------- build_command
def test_build_command_targets_the_raw_store_summarize_path():
    cmd = scheduler.build_command("monthly", out="/x/state.json", raw_store="/x/raw.sqlite",
                                  config="/x/conditions.yaml", watch="/x/watch.md", python="/py")
    assert cmd[:6] == ["/py", "-m", "text_triage.cli", "summarize", "--mode", "monthly"]
    assert cmd[cmd.index("--source") + 1] == "raw-store"
    assert cmd[cmd.index("--raw-store") + 1] == "/x/raw.sqlite"
    assert cmd[cmd.index("--out") + 1] == "/x/state.json"
    assert cmd[cmd.index("--config") + 1] == "/x/conditions.yaml"


def test_build_command_includes_limit_when_set():
    assert scheduler.build_command("daily", out="/x/s.json", limit=20)[-2:] == ["--limit", "20"]
    assert "--limit" not in scheduler.build_command("daily", out="/x/s.json")


# ----------------------------------------------------------- run_loop (one tick)
def _kw(tmp_path, sched_file, spawned, **extra):
    return dict(state_path=tmp_path / "state.json", raw_path=tmp_path / "raw.sqlite",
                last_runs_path=sched_file, _spawn=lambda mode, **k: spawned.append((mode, k.get("limit"))),
                _once=True, **extra)


def test_run_loop_fresh_boot_seeds_forward_and_spawns_nothing(tmp_path):
    """A brand-new server (no scheduler.json) schedules FORWARD: it seeds every cadence to now and fires
    nothing retroactively (the one-time bootstrap arrives via the collector's /trigger instead)."""
    spawned, sched_file = [], tmp_path / "scheduler.json"
    scheduler.run_loop(Config(), clock=lambda: D(2026, 6, 4, 22, 0), **_kw(tmp_path, sched_file, spawned))
    assert spawned == []                                        # nothing fired retroactively
    assert set(json.loads(sched_file.read_text())) == {"daily", "weekly", "monthly"}   # all seeded


def test_run_loop_existing_file_catches_up_after_downtime(tmp_path):
    """An existing scheduler.json (a server that's run before) still catches up a cadence whose time
    passed while it was down."""
    spawned, sched_file = [], tmp_path / "scheduler.json"
    sched_file.write_text(json.dumps({"daily": D(2026, 6, 4, 20, 0).isoformat(),
                                      "weekly": D(2026, 6, 1, 3, 0).isoformat(),
                                      "monthly": D(2026, 6, 1, 3, 0).isoformat()}))
    scheduler.run_loop(Config(), clock=lambda: D(2026, 6, 4, 22, 0), **_kw(tmp_path, sched_file, spawned))
    assert [m for m, _ in spawned] == ["daily"]                # the 21:00 daily slot passed -> caught up


def test_run_loop_no_last_runs_path_runs_all_due(tmp_path):
    """The test-injection path (no file): no forward-seed, behaves as the raw due_runs (all due)."""
    spawned = []
    scheduler.run_loop(Config(), clock=lambda: D(2026, 6, 4, 22, 0),
                       state_path=tmp_path / "s.json", raw_path=tmp_path / "r.sqlite",
                       _spawn=lambda mode, **k: spawned.append(mode), _once=True)
    assert set(spawned) == {"daily", "weekly", "monthly"}


def test_run_loop_threads_limit_to_spawn(tmp_path):
    """The server's bootstrap_limit is passed through to each spawned summarize as --limit."""
    spawned, sched_file = [], tmp_path / "scheduler.json"
    sched_file.write_text(json.dumps({"daily": D(2026, 6, 4, 20, 0).isoformat(),
                                      "weekly": D(2026, 6, 1, 3, 0).isoformat(),
                                      "monthly": D(2026, 6, 1, 3, 0).isoformat()}))
    scheduler.run_loop(Config(), clock=lambda: D(2026, 6, 4, 22, 0),
                       **_kw(tmp_path, sched_file, spawned, limit=20))
    assert spawned == [("daily", 20)]
