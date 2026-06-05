"""The Mac-side collector — poll chat.db, push new raw to the server's ``/ingest``, advance a local
watermark. Runs wherever chat.db lives (a laptop, or the Mac mini itself); it holds NO model keys.

Topology is one knob: ``server.url`` blank pushes to the local server on this machine (loopback —
the all-in-one Mac mini); a URL pushes to a remote VPS. Re-pushing an overlapping window is safe — the
server's raw store dedups on ``(chat_rowid, message_rowid)`` — so the watermark can be conservative.
The HTTP ``post`` is injectable (tests pass a fake); the default is stdlib ``urllib`` (zero new deps).
"""
from __future__ import annotations

import datetime
import json
import os
import sys
import time
import urllib.request
from pathlib import Path
from typing import Callable, Optional, Union

from text_triage.collect.extract import ADDRESSBOOK_DIR, CHAT_DB, extract, recoverable_deletions
from text_triage.config import Config, load_config

__all__ = ["ingest_url", "trigger_url", "push_once", "watch", "main"]

PostFn = Callable[[str, bytes, dict], tuple]


def _base_url(config: Config) -> str:
    """The server base URL: ``server.url`` if set, else the local loopback for ``server.bind``."""
    if config.server.url:
        return config.server.url.rstrip("/")
    host, _, port = config.server.bind.rpartition(":")
    if host in ("", "0.0.0.0"):
        host = "127.0.0.1"
    return f"http://{host}:{port}"


def ingest_url(config: Config) -> str:
    return _base_url(config) + "/ingest"


def trigger_url(config: Config) -> str:
    return _base_url(config) + "/trigger"


def _auth_headers() -> dict:
    headers = {"Content-Type": "application/json"}
    token = os.environ.get("TEXT_TRIAGE_INGEST_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _http_post(url: str, data: bytes, headers: dict) -> tuple:
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.status, r.read().decode()


def _state_path(state_dir: Path) -> Path:
    return Path(state_dir) / "collector.json"


def _read_state(state_dir: Path) -> dict:
    """The collector's local state: the pushed-watermark ``since`` and the ``admitted`` conversation ids
    (those whose full history has been mirrored). Missing/corrupt → empty dict (a fresh bootstrap)."""
    p = _state_path(state_dir)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (ValueError, OSError):
        return {}


def _write_state(state_dir: Path, state: dict) -> None:
    Path(state_dir).mkdir(parents=True, exist_ok=True)
    _state_path(state_dir).write_text(json.dumps(state), encoding="utf-8")


def push_once(config: Optional[Config] = None, *, db_path: str = CHAT_DB,
              addressbook_dir: str = ADDRESSBOOK_DIR, state_dir: Union[str, Path],
              post: Optional[PostFn] = None, now: Optional[datetime.datetime] = None,
              extract_fn=None, deletions_fn=None) -> dict:
    """Mirror new raw to the server. The first sync mirrors the whole ``backfill_years`` window in one
    extract; later runs push only the delta since the watermark. When a conversation first crosses the
    spam floor (it shows up in the delta but isn't yet ``admitted``), its FULL history is backfilled once
    so the server's deep-dive store isn't missing the pre-admission messages. No new messages → no POST
    (the watermark still advances). Re-pushing overlap is safe — the store dedups on the message PK."""
    config = config or load_config()
    state_dir = Path(state_dir)
    post = post or _http_post
    extract_fn = extract_fn or extract
    deletions_fn = deletions_fn or recoverable_deletions
    now = now or datetime.datetime.now()

    state = _read_state(state_dir)
    since = state.get("since")
    admitted = set(state.get("admitted") or [])
    backfill_since = (now - datetime.timedelta(days=365 * config.messages.backfill_years)
                      ).strftime("%Y-%m-%d %H:%M:%S")

    bootstrap = since is None
    if bootstrap:                       # first sync: mirror the whole backfill window at once
        since = backfill_since

    export = extract_fn(db_path=db_path, addressbook_dir=addressbook_dir, since=since, config=config)
    # Conversations newly crossing the spam floor need their FULL history, not just the recent delta.
    new_cids = [c["chat_rowid"] for c in export["conversations"] if c["chat_rowid"] not in admitted]
    if new_cids and not bootstrap:
        back = extract_fn(db_path=db_path, addressbook_dir=addressbook_dir, since=backfill_since,
                          config=config, chat_rowids=new_cids)
        by_id = {c["chat_rowid"]: c for c in export["conversations"]}
        for c in back["conversations"]:
            by_id[c["chat_rowid"]] = c          # replace the delta with the full backfill
        export = dict(export, conversations=list(by_id.values()))

    export["deleted"] = deletions_fn(db_path)       # deleted/unsent signal for the server to flag
    pushed = sum(len(c["conversation"]) for c in export["conversations"])
    url = ingest_url(config)
    if pushed or export["deleted"]:
        post(url, json.dumps(export).encode(), _auth_headers())
    admitted |= set(new_cids)
    state.update(since=export["generated_at"], admitted=sorted(admitted))
    _write_state(state_dir, state)
    return {"pushed": pushed, "url": url, "since": since, "admitted_new": new_cids}


def _open_trigger(config: Config, state_dir: Union[str, Path], post: PostFn) -> str:
    """The on-open trigger: a one-time MONTHLY bootstrap the first time ever (so a fresh setup builds the
    full note stack once the store is seeded), recorded in ``collector.json``; DAILY thereafter. Returns
    the mode fired."""
    state_dir = Path(state_dir)
    state = _read_state(state_dir)
    mode = "daily" if state.get("bootstrapped") else "monthly"
    post(trigger_url(config), json.dumps({"mode": mode}).encode(), _auth_headers())
    if mode == "monthly":
        state["bootstrapped"] = True
        _write_state(state_dir, state)
    return mode


def watch(config: Optional[Config] = None, *, db_path: str = CHAT_DB,
          addressbook_dir: str = ADDRESSBOOK_DIR, state_dir: Union[str, Path, None] = None,
          post: Optional[PostFn] = None, on_open: bool = True) -> None:
    """Poll forever every ``server.live.interval_seconds``, pushing new raw. On the first wake it pokes
    ``/trigger`` — a one-time monthly bootstrap the first time ever, then daily (``on_open``)."""
    config = config or load_config()
    state_dir = Path(state_dir) if state_dir else Path.home() / ".text-triage"
    post = post or _http_post
    interval = config.server.live.interval_seconds
    first = True
    while True:
        try:
            res = push_once(config, db_path=db_path, addressbook_dir=addressbook_dir,
                            state_dir=state_dir, post=post)
            if first and on_open:
                _open_trigger(config, state_dir, post)
            print(f"pushed {res['pushed']} message(s) -> {res['url']}")
        except Exception as e:  # a transient network/db error must not kill the daemon
            print(f"push failed: {e}", file=sys.stderr)
        first = False
        time.sleep(interval)


def main(argv: Optional[list] = None) -> int:
    """``text-triage push`` — push raw once, or ``--watch`` to poll continuously."""
    import argparse

    p = argparse.ArgumentParser(prog="text-triage push",
                                description="Push new raw messages from chat.db to the server.")
    p.add_argument("--watch", action="store_true", help="poll continuously (every live.interval_seconds)")
    p.add_argument("--config", help="path to conditions.yaml (default: auto-discover)")
    p.add_argument("--db", default=CHAT_DB, help="path to chat.db")
    p.add_argument("--addressbook", default=ADDRESSBOOK_DIR, help="AddressBook dir for contacts")
    p.add_argument("--state-dir", dest="state_dir", help="where the push watermark lives "
                                                         "(default: ~/.text-triage)")
    args = p.parse_args(argv)
    config = load_config(args.config)
    state_dir = args.state_dir or str(Path.home() / ".text-triage")
    if args.watch:
        watch(config, db_path=args.db, addressbook_dir=args.addressbook, state_dir=state_dir)
        return 0
    res = push_once(config, db_path=args.db, addressbook_dir=args.addressbook, state_dir=state_dir)
    print(f"pushed {res['pushed']} message(s) -> {res['url']}")
    return 0
