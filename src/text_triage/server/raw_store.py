"""raw_messages.sqlite — the server-owned raw-message store (the always-on host: VPS or Mac mini).

The collector pushes extractor exports here (``ingest``); the scheduler rebuilds summary inputs from
here (``export``) instead of re-reading chat.db — the source-agnostic seam, so ``triage.skeleton`` and
``triage.summarize`` consume the rebuilt dict unchanged. ``history`` is the MCP deep-dive read; ``prune``
enforces ``server.raw_store_days``.

Dedup is by ``(chat_rowid, message_rowid)`` (the message PK), so re-pushing an overlapping window is
idempotent. The window/unresponded reconstruction in ``export`` mirrors ``collect.extract`` exactly —
the same pure helpers (``window_days_for``/``iso_to_db``/``compute_watermark``/``_passes_handle_digits``)
are reused so the two sources can never drift.
"""
from __future__ import annotations

import datetime
import json
import sqlite3
from pathlib import Path
from typing import Optional, Union

from text_triage.collect.extract import (
    MAC_EPOCH_OFFSET,
    _passes_handle_digits,
    compute_watermark,
    iso_to_db,
    window_days_for,
)
from text_triage.config import Config

__all__ = ["ingest", "history", "counts", "export", "deltas", "prune"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    chat_rowid       INTEGER PRIMARY KEY,
    name             TEXT,
    handle           TEXT,
    is_named         INTEGER,
    is_groupchat     INTEGER,
    members          TEXT,   -- JSON array, or NULL for 1:1
    contact_details  TEXT    -- JSON object, or NULL
);
CREATE TABLE IF NOT EXISTS messages (
    chat_rowid     INTEGER NOT NULL,
    message_rowid  INTEGER NOT NULL,
    date           INTEGER NOT NULL,   -- raw Apple absolute-time integer (same scale as chat.db)
    datetime       TEXT,
    sender         TEXT,               -- "me" for from-me, else the display name
    text           TEXT,
    deleted        INTEGER NOT NULL DEFAULT 0,   -- 1 = deleted/unsent on chat.db; hidden unless include_deleted
    PRIMARY KEY (chat_rowid, message_rowid)
);
CREATE INDEX IF NOT EXISTS idx_messages_chat_date ON messages (chat_rowid, date);
"""


def _connect(path: Union[str, Path]) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)  # sqlite won't create a db in a missing dir
    con = sqlite3.connect(str(path))
    con.executescript(_SCHEMA)
    if "deleted" not in {r[1] for r in con.execute("PRAGMA table_info(messages)")}:  # migrate older stores
        con.execute("ALTER TABLE messages ADD COLUMN deleted INTEGER NOT NULL DEFAULT 0")
        con.commit()
    return con


def _units_per_sec(cur: sqlite3.Cursor) -> int:
    """Auto-detect the timestamp scale (ns vs seconds since 2001), same rule as the extractor."""
    max_date = cur.execute("SELECT MAX(date) FROM messages").fetchone()[0] or 0
    return 1_000_000_000 if max_date > 1e12 else 1


# --------------------------------------------------------------------------- ingest
def ingest(export: dict, *, path: Union[str, Path]) -> int:
    """Persist an extractor ``export`` (conversation metadata + messages). Returns the number of
    NEW messages stored; re-pushing already-seen messages is a no-op (deduped on the message PK)."""
    con = _connect(path)
    cur = con.cursor()
    new = 0
    for c in export.get("conversations", []):
        members = c.get("members")
        details = c.get("contact_details")
        cur.execute(
            """INSERT INTO conversations
                 (chat_rowid, name, handle, is_named, is_groupchat, members, contact_details)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(chat_rowid) DO UPDATE SET
                 name=excluded.name, handle=excluded.handle, is_named=excluded.is_named,
                 is_groupchat=excluded.is_groupchat, members=excluded.members,
                 contact_details=excluded.contact_details""",
            (
                c["chat_rowid"], c["name"], c.get("handle"),
                int(bool(c.get("is_named"))), int(bool(c.get("is_groupchat"))),
                json.dumps(members) if members is not None else None,
                json.dumps(details) if details is not None else None,
            ),
        )
        for m in c.get("conversation", []):
            cur.execute(
                """INSERT OR IGNORE INTO messages
                     (chat_rowid, message_rowid, date, datetime, sender, text)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (c["chat_rowid"], m["message_rowid"], m["date"],
                 m.get("datetime"), m.get("sender"), m.get("text")),
            )
            new += cur.rowcount  # 1 inserted, 0 ignored
    # Deletion signal (Recently Deleted / unsends): flip the flag on already-stored messages. Messages
    # never stored (deleted before admission) simply have no row to update — out of reach.
    for d in export.get("deleted", []):
        cur.execute("UPDATE messages SET deleted=1 WHERE chat_rowid=? AND message_rowid=?",
                    (d["chat_rowid"], d["message_rowid"]))
    con.commit()
    con.close()
    return new


# -------------------------------------------------------------------------- history
def history(chat_rowid: int, *, since: Optional[str] = None, after_rowid: Optional[int] = None,
            limit: Optional[int] = None, include_deleted: bool = False,
            path: Union[str, Path]) -> list[dict]:
    """One conversation's stored messages, oldest first. ``since`` (an ISO ``YYYY-MM-DD HH:MM:SS``
    string) keeps only messages at/after that moment; ``after_rowid`` keeps only messages strictly
    newer than that rowid (the derived-texts_today read against a summary cursor); ``limit`` caps the
    count. Deleted/unsent messages are hidden unless ``include_deleted`` is set."""
    con = _connect(path)
    cur = con.cursor()
    q = "SELECT message_rowid, datetime, sender, text FROM messages WHERE chat_rowid=?"
    params: list = [chat_rowid]
    if not include_deleted:
        q += " AND deleted=0"
    if since is not None:
        q += " AND datetime >= ?"
        params.append(since)
    if after_rowid is not None:
        q += " AND message_rowid > ?"
        params.append(after_rowid)
    q += " ORDER BY date ASC, message_rowid ASC"
    if limit:
        q += f" LIMIT {int(limit)}"
    rows = cur.execute(q, params).fetchall()
    con.close()
    return [{"message_rowid": r[0], "datetime": r[1], "sender": r[2], "text": r[3]} for r in rows]


def counts(*, path: Union[str, Path]) -> dict[int, int]:
    """Total stored (non-deleted) message count per conversation — the quickscan read."""
    con = _connect(path)
    rows = con.execute(
        "SELECT chat_rowid, COUNT(*) FROM messages WHERE deleted=0 GROUP BY chat_rowid").fetchall()
    con.close()
    return {r[0]: r[1] for r in rows}


# --------------------------------------------------------------------------- export
def export(*, window: Optional[str] = None, since: Optional[str] = None,
           config: Optional[Config] = None, now: Optional[float] = None,
           path: Union[str, Path]) -> dict:
    """Rebuild an extractor-shaped export dict from the store — the source-agnostic seam the
    scheduler feeds to the summarizer. ``window`` (``"weekly"``/``"monthly"``) keeps the pre-window
    context prefix and computes the stale ``unresponded`` list; ``since`` (ISO) is incremental and
    drops both. Exactly one of ``window``/``since`` is required."""
    if (window is None) == (since is None):
        raise ValueError("exactly one of window= or since= is required")
    if config is None:
        config = Config()
    if now is None:
        now = datetime.datetime.now().timestamp()
    cf = config.messages

    con = _connect(path)
    cur = con.cursor()
    units = _units_per_sec(cur)

    if window is not None:
        window_days = window_days_for(window, config)
        cutoff_window_db = int((now - window_days * 86400 - MAC_EPOCH_OFFSET) * units)
        cutoff_lookback_db = int((now - cf.unresponded_lookback_days * 86400 - MAC_EPOCH_OFFSET) * units)
        include_prefix, compute_unresponded = True, True
        window_label: Union[str, dict] = window
    else:
        cutoff_window_db = iso_to_db(since, units)
        cutoff_lookback_db = 0
        include_prefix, compute_unresponded = False, False
        window_label = {"since": since}

    meta = {}
    for row in cur.execute(
        "SELECT chat_rowid, name, handle, is_named, is_groupchat, members, contact_details FROM conversations"
    ):
        meta[row[0]] = {
            "name": row[1], "handle": row[2], "is_named": bool(row[3]),
            "is_groupchat": bool(row[4]),
            "members": json.loads(row[5]) if row[5] else None,
            "contact_details": json.loads(row[6]) if row[6] else None,
        }

    conversations: list = []
    unresponded: list = []
    emitted: list = []

    chat_ids = [r[0] for r in cur.execute("SELECT DISTINCT chat_rowid FROM messages")]
    for cid in chat_ids:
        m = meta.get(cid)
        if m is None:
            continue  # messages with no conversation metadata (shouldn't happen via ingest)
        rows = cur.execute(
            """SELECT message_rowid, date, datetime, sender, text FROM messages
               WHERE chat_rowid=? AND deleted=0 ORDER BY date ASC, message_rowid ASC""", (cid,)
        ).fetchall()
        if not rows:
            continue

        total = len(rows)
        from_me = sum(1 for r in rows if r[3] == "me")
        from_them = total - from_me
        last_date = rows[-1][1]
        window_count = sum(1 for r in rows if r[1] >= cutoff_window_db)
        is_group = m["is_groupchat"]
        handle = m["handle"]
        is_named = m["is_named"]
        responded = True if is_group else (rows[-1][3] == "me")

        # ---- unresponded (1:1 only, window mode, same filter as conversations) ----
        if (compute_unresponded and not is_group
                and not (cf.named_only and not is_named)
                and _passes_handle_digits(handle, cf.min_handle_digits)):
            last_is_them = not responded
            in_band = cutoff_lookback_db <= last_date < cutoff_window_db
            qualifies = (from_me >= 1 and from_them >= 1) or from_them >= 2
            if last_is_them and in_band and qualifies:
                last_unix = last_date / units + MAC_EPOCH_OFFSET
                unresponded.append({
                    "chat_rowid": cid, "name": m["name"],
                    "last_at": rows[-1][2], "last_date_raw": last_date,
                    "days_waiting": int((now - last_unix) // 86400), "_sort": last_date,
                })

        # ---- conversation filter ----
        if window_count < 1:                # no activity in this window/since -> skip (stale -> unresponded)
            continue
        if is_group and not cf.include_groups:
            continue
        if cf.named_only and not is_named:
            continue
        if not is_group and not _passes_handle_digits(handle, cf.min_handle_digits):
            continue

        in_window = [r for r in rows if r[1] >= cutoff_window_db]
        prior = ([r for r in rows if r[1] < cutoff_window_db][-cf.context_messages:]
                 if include_prefix and cf.context_messages else [])
        rendered = [{"message_rowid": r[0], "date": r[1], "datetime": r[2],
                     "sender": r[3], "text": r[4]} for r in (prior + in_window)]
        emitted.extend(rendered)
        conversations.append({
            "chat_rowid": cid, "name": m["name"], "handle": handle,
            "is_named": is_named, "is_groupchat": is_group, "responded": responded,
            "members": m["members"], "contact_details": m["contact_details"],
            "window_messages": window_count, "text_count": total,
            "conversation": rendered, "_sort": last_date,
        })

    con.close()
    conversations.sort(key=lambda c: c.pop("_sort"), reverse=True)
    unresponded.sort(key=lambda u: u.pop("_sort"), reverse=True)

    return {
        "generated_at": datetime.datetime.fromtimestamp(now).strftime("%Y-%m-%d %H:%M:%S"),
        "window": window_label,
        "context_messages": cf.context_messages if include_prefix else 0,
        "watermark": compute_watermark(emitted),
        "conversations": conversations,
        "unresponded": unresponded,
    }


# ---------------------------------------------------------------------------- deltas
def deltas(cursors: dict, *, now: Optional[float] = None, path: Union[str, Path]) -> dict:
    """Per-conversation messages newer than each conversation's summary cursor (``summarized_through``,
    a ``message_rowid``) — the daily summarizer's source. Same export-dict shape as :func:`export`, but
    keyed off per-conversation cursors instead of a time window: each conversation also carries
    ``new_count`` (messages strictly after its cursor) so the daily gate can decide whether it's worth an
    LLM call. A conversation absent from ``cursors`` uses 0 (its whole history is new); one with no new
    messages is omitted. No window cutoff, no spam filter — reads straight from the full store."""
    if now is None:
        now = datetime.datetime.now().timestamp()
    con = _connect(path)
    cur = con.cursor()

    meta = {}
    for row in cur.execute(
        "SELECT chat_rowid, name, handle, is_named, is_groupchat, members, contact_details FROM conversations"
    ):
        meta[row[0]] = {
            "name": row[1], "handle": row[2], "is_named": bool(row[3]),
            "is_groupchat": bool(row[4]),
            "members": json.loads(row[5]) if row[5] else None,
            "contact_details": json.loads(row[6]) if row[6] else None,
        }

    # Full stored count per conversation (all history, not just the post-cursor delta) -> the
    # new_conversation flag keys on this, so it must reflect total texts, not what's new this run.
    totals = dict(cur.execute(
        "SELECT chat_rowid, COUNT(*) FROM messages WHERE deleted=0 GROUP BY chat_rowid"))

    conversations: list = []
    emitted: list = []
    for cid in [r[0] for r in cur.execute("SELECT DISTINCT chat_rowid FROM messages")]:
        m = meta.get(cid)
        if m is None:
            continue
        rows = cur.execute(
            """SELECT message_rowid, date, datetime, sender, text FROM messages
               WHERE chat_rowid=? AND message_rowid > ? AND deleted=0
               ORDER BY date ASC, message_rowid ASC""",
            (cid, cursors.get(cid, 0)),
        ).fetchall()
        if not rows:
            continue
        is_group = m["is_groupchat"]
        rendered = [{"message_rowid": r[0], "date": r[1], "datetime": r[2],
                     "sender": r[3], "text": r[4]} for r in rows]
        emitted.extend(rendered)
        conversations.append({
            "chat_rowid": cid, "name": m["name"], "handle": m["handle"],
            "is_named": m["is_named"], "is_groupchat": is_group,
            "responded": True if is_group else (rows[-1][3] == "me"),
            "members": m["members"], "contact_details": m["contact_details"],
            "window_messages": len(rendered), "conversation": rendered,
            "new_count": len(rendered), "text_count": totals.get(cid, 0), "_sort": rows[-1][1],
        })

    con.close()
    conversations.sort(key=lambda c: c.pop("_sort"), reverse=True)
    return {
        "generated_at": datetime.datetime.fromtimestamp(now).strftime("%Y-%m-%d %H:%M:%S"),
        "window": {"deltas": True},
        "context_messages": 0,
        "watermark": compute_watermark(emitted),
        "conversations": conversations,
        "unresponded": [],
    }


# ----------------------------------------------------------------------------- prune
def prune(*, raw_store_days: int, now: Optional[float] = None, path: Union[str, Path]) -> int:
    """Delete messages older than ``raw_store_days``. ``0`` (or less) keeps everything. Returns the
    number of rows deleted."""
    if not raw_store_days or raw_store_days <= 0:
        return 0
    if now is None:
        now = datetime.datetime.now().timestamp()
    con = _connect(path)
    cur = con.cursor()
    units = _units_per_sec(cur)
    cutoff = int((now - raw_store_days * 86400 - MAC_EPOCH_OFFSET) * units)
    cur.execute("DELETE FROM messages WHERE date < ?", (cutoff,))
    deleted = cur.rowcount
    con.commit()
    con.close()
    return deleted
