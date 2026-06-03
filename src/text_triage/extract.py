#!/usr/bin/env python3
"""Unified iMessage extractor — the Mac-side read of ``chat.db`` + Contacts.

Unifies the three ~95%-identical handoff scripts (``1_/7_/30_imessage_export.py``) into one tool.
The proven chat.db/Contacts helpers (``connect_ro``, ``decode_attributed_body``, ``load_contacts``,
timestamp auto-detection, handle/chat building, the ``unresponded`` heuristic) are lifted verbatim.
``conditions.yaml`` (via ``config.py``) is the source of truth for the windows and the conversation
filter; the new surface is:

  * one CLI: ``--window {weekly,monthly}`` (window days from ``weekly_days``/``monthly_days``) XOR
    ``--since <iso>`` (incremental watermark path)
  * the IDs the design keys on: per-conversation ``chat_rowid`` + ``handle`` (the stable keys —
    never key on name), per-message ``message_rowid`` and the raw Apple ``date`` integer
  * a top-level ``watermark`` ``{max_date_raw, max_message_rowid}`` ordered by ``(date, rowid)``
  * ``window_messages`` (replacing the per-script ``{24h,7_day,30_day}_messages`` fields)
  * ``unresponded`` enriched to objects (``{chat_rowid, name, last_at, last_date_raw, days_waiting}``)
  * ``conversation_filter`` applied (include_groups / named_only / min_handle_digits / min_messages)

``--window`` keeps the pre-window context prefix and emits ``unresponded``; ``--since`` (incremental)
drops both, so it never re-emits already-summarized texts or recomputes the stale list every poll.

REQUIRES Full Disk Access for the binary running this (reads ``~/Library/Messages/chat.db`` and the
AddressBook). Under launchd that grant goes to the venv ``python3``, not Terminal — see README/PLAN.
"""
from __future__ import annotations

import argparse
import datetime
import glob
import json
import os
import re
import sqlite3
import sys

from text_triage.config import Config, load_config

# --------------------------------------------------------------------------- config
CHAT_DB = os.path.expanduser("~/Library/Messages/chat.db")
ADDRESSBOOK_DIR = os.path.expanduser("~/Library/Application Support/AddressBook")

# Apple absolute-time epoch (2001-01-01 UTC) offset from unix epoch, in seconds.
MAC_EPOCH_OFFSET = 978307200

# associated_message_type values for tapbacks (2xxx = add, 3xxx = remove).
TAPBACKS = {
    2000: "Loved", 2001: "Liked", 2002: "Disliked",
    2003: "Laughed", 2004: "Emphasized", 2005: "Questioned",
    2006: "Reaction",
}


# ----------------------------------------------------------------- helpers (verbatim)
def connect_ro(path):
    """Open a SQLite DB strictly read-only via URI so we never lock or touch the live file."""
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def cols_present(cur, table):
    """Return the set of column names in a table, or empty set if it doesn't exist."""
    try:
        cur.execute(f"PRAGMA table_info({table})")
        return {r[1] for r in cur.fetchall()}
    except sqlite3.Error:
        return set()


def norm_phone(s):
    """Normalize a phone string to its last 10 digits for matching."""
    digits = re.sub(r"\D", "", s or "")
    return digits[-10:] if len(digits) >= 10 else digits


def decode_attributed_body(blob):
    """Best-effort extraction of message text from the NSArchiver 'streamtyped' blob used when
    message.text is NULL. Imperfect by nature; covers ordinary messages (~99%)."""
    if not blob:
        return None
    try:
        data = bytes(blob)
        for marker in (b"NSString", b"NSMutableString"):
            i = data.find(marker)
            if i == -1:
                continue
            j = data.find(b"\x2b", i)  # '+' precedes the raw character bytes
            if j == -1:
                continue
            p = j + 1
            ln = data[p]
            p += 1
            if ln == 0x81:                                  # 2-byte length
                ln = int.from_bytes(data[p:p + 2], "little"); p += 2
            elif ln == 0x82:                                # 4-byte length
                ln = int.from_bytes(data[p:p + 4], "little"); p += 4
            text = data[p:p + ln].decode("utf-8", errors="replace")
            if text:
                return text
        return None
    except Exception:
        return None


def tapback_label(t):
    if t in TAPBACKS:
        return f"[Reacted: {TAPBACKS[t]}]"
    if 3000 <= t < 4000:
        return f"[Removed reaction: {TAPBACKS.get(t - 1000, 'reaction')}]"
    return "[reaction]"


def load_contacts(addressbook_dir=ADDRESSBOOK_DIR):
    """Build {normalized_phone -> card} and {email -> card} maps from every AddressBook database.
    Parameterized on ``addressbook_dir`` (the only change from the handoff scripts) so tests and
    forkers can point it elsewhere."""
    phone_map, email_map = {}, {}
    paths = glob.glob(os.path.join(addressbook_dir, "**", "*.abcddb"), recursive=True)

    for path in paths:
        try:
            con = connect_ro(path)
            cur = con.cursor()
        except sqlite3.Error:
            continue

        rcols = cols_present(cur, "ZABCDRECORD")
        if "Z_PK" not in rcols:
            con.close(); continue

        want = [c for c in ("Z_PK", "ZFIRSTNAME", "ZLASTNAME", "ZORGANIZATION",
                            "ZJOBTITLE", "ZNOTE", "ZBIRTHDAY", "ZNICKNAME") if c in rcols]
        records = {}
        for row in cur.execute(f"SELECT {','.join(want)} FROM ZABCDRECORD"):
            d = dict(zip(want, row))
            first, last = (d.get("ZFIRSTNAME") or ""), (d.get("ZLASTNAME") or "")
            org = d.get("ZORGANIZATION")
            name = (f"{first} {last}").strip() or org or d.get("ZNICKNAME")
            bday = d.get("ZBIRTHDAY")
            if isinstance(bday, (int, float)):
                try:
                    bday = datetime.date.fromtimestamp(bday + MAC_EPOCH_OFFSET).isoformat()
                except Exception:
                    bday = None
            records[d["Z_PK"]] = {
                "name": name, "organization": org, "job_title": d.get("ZJOBTITLE"),
                "birthday": bday, "note": d.get("ZNOTE"),
                "phones": [], "emails": [], "addresses": [],
            }

        if "ZFULLNUMBER" in cols_present(cur, "ZABCDPHONENUMBER"):
            for owner, num in cur.execute("SELECT ZOWNER, ZFULLNUMBER FROM ZABCDPHONENUMBER"):
                if owner in records and num:
                    records[owner]["phones"].append(num)
        if "ZADDRESS" in cols_present(cur, "ZABCDEMAILADDRESS"):
            for owner, addr in cur.execute("SELECT ZOWNER, ZADDRESS FROM ZABCDEMAILADDRESS"):
                if owner in records and addr:
                    records[owner]["emails"].append(addr)
        acols = cols_present(cur, "ZABCDPOSTALADDRESS")
        if {"ZOWNER", "ZSTREET"} <= acols:
            sel = [c for c in ("ZOWNER", "ZSTREET", "ZCITY", "ZSTATE",
                              "ZZIPCODE", "ZCOUNTRYNAME") if c in acols]
            for r in cur.execute(f"SELECT {','.join(sel)} FROM ZABCDPOSTALADDRESS"):
                a = dict(zip(sel, r))
                owner = a.pop("ZOWNER", None)
                if owner in records:
                    parts = [a.get(k) for k in ("ZSTREET", "ZCITY", "ZSTATE", "ZZIPCODE", "ZCOUNTRYNAME")]
                    formatted = ", ".join(p for p in parts if p)
                    if formatted:
                        records[owner]["addresses"].append(formatted)
        con.close()

        # First match wins across databases (handles iCloud + local duplicates).
        for rec in records.values():
            for ph in rec["phones"]:
                k = norm_phone(ph)
                if k:
                    phone_map.setdefault(k, rec)
            for em in rec["emails"]:
                k = em.lower().strip()
                if k:
                    email_map.setdefault(k, rec)

    return phone_map, email_map


# ------------------------------------------------------------------- new pure helpers
def window_days_for(window, config):
    """Resolve a named window to its day count from config (``weekly``/``monthly``)."""
    if window == "weekly":
        return config.windows.weekly_days
    if window == "monthly":
        return config.windows.monthly_days
    raise ValueError(f"unknown window {window!r}; expected 'weekly' or 'monthly'")


def iso_to_db(iso, units_per_sec):
    """Convert an ISO-8601 datetime to an Apple absolute-time integer (the chat.db scale)."""
    unix = datetime.datetime.fromisoformat(iso).timestamp()
    return int((unix - MAC_EPOCH_OFFSET) * units_per_sec)


def compute_watermark(messages):
    """Watermark over the emitted messages: the max raw ``date``, tie-broken by the max
    ``message_rowid`` at that date — never ``date`` alone (same-second clusters would skip/dup)."""
    if not messages:
        return {"max_date_raw": 0, "max_message_rowid": 0}
    max_date = max(m["date"] for m in messages)
    max_rowid = max(m["message_rowid"] for m in messages if m["date"] == max_date)
    return {"max_date_raw": max_date, "max_message_rowid": max_rowid}


def _passes_handle_digits(handle, min_digits):
    """A 1:1 handle passes if it's an email (no '@'-based digit rule) or has >= min_digits digits.
    Filters shortcodes / sub-10-digit senders (e.g. 2FA '38792')."""
    if not handle or "@" in handle:
        return True
    return len(re.sub(r"\D", "", handle)) >= min_digits


# ------------------------------------------------------------------------------ extract
def extract(*, db_path=CHAT_DB, addressbook_dir=ADDRESSBOOK_DIR, window=None, since=None,
            now=None, config=None):
    """Read ``chat.db`` and return the export dict (the source the skeleton builder transforms).

    Exactly one of ``window`` (``"weekly"``/``"monthly"``) or ``since`` (ISO datetime) must be given.
    ``config`` (a :class:`text_triage.config.Config`) drives the window days, the unresponded
    lookback, the context-prefix length and the conversation filter; defaults are used if omitted.
    ``now`` (unix seconds) is injectable for deterministic windows in tests.
    """
    if (window is None) == (since is None):
        raise ValueError("exactly one of window= or since= is required")
    if config is None:
        config = Config()
    if now is None:
        now = datetime.datetime.now().timestamp()

    cf = config.conversation_filter
    context_messages = config.windows.context_messages

    con = connect_ro(db_path)
    cur = con.cursor()

    # detect timestamp scale (ns vs seconds since 2001)
    max_date = cur.execute("SELECT MAX(date) FROM message").fetchone()[0] or 0
    units_per_sec = 1_000_000_000 if max_date > 1e12 else 1

    def to_dt(db_date):
        unix = db_date / units_per_sec + MAC_EPOCH_OFFSET
        return datetime.datetime.fromtimestamp(unix).strftime("%Y-%m-%d %H:%M:%S")

    lookback_days = config.windows.unresponded_lookback_days
    cutoff_lookback_db = int((now - lookback_days * 86400 - MAC_EPOCH_OFFSET) * units_per_sec)
    if window is not None:
        window_days = window_days_for(window, config)
        cutoff_window_db = int((now - window_days * 86400 - MAC_EPOCH_OFFSET) * units_per_sec)
        include_prefix, compute_unresponded = True, True
        window_label = window
    else:
        cutoff_window_db = iso_to_db(since, units_per_sec)
        include_prefix, compute_unresponded = False, False
        window_label = {"since": since}

    try:
        phone_map, email_map = load_contacts(addressbook_dir)
    except Exception:
        phone_map, email_map = {}, {}

    def lookup(addr):
        if not addr:
            return None
        if "@" in addr:
            return email_map.get(addr.lower().strip())
        return phone_map.get(norm_phone(addr))

    def display(addr):
        rec = lookup(addr)
        return (rec["name"] if rec and rec["name"] else addr)

    handle_addr = {rid: addr for rid, addr in cur.execute("SELECT ROWID, id FROM handle")}

    chats = {}
    for cid, ident, dname in cur.execute("SELECT ROWID, chat_identifier, display_name FROM chat"):
        chats[cid] = {"identifier": ident, "display_name": dname, "handles": []}
    for cid, hid in cur.execute("SELECT chat_id, handle_id FROM chat_handle_join"):
        if cid in chats:
            chats[cid]["handles"].append(hid)

    agg = {}
    for cid, total, from_me, last_date, win in cur.execute(f"""
            SELECT cmj.chat_id,
                   COUNT(*),
                   SUM(CASE WHEN m.is_from_me=1 THEN 1 ELSE 0 END),
                   MAX(m.date),
                   SUM(CASE WHEN m.date >= {cutoff_window_db} THEN 1 ELSE 0 END)
            FROM chat_message_join cmj
            JOIN message m ON m.ROWID = cmj.message_id
            GROUP BY cmj.chat_id"""):
        agg[cid] = {"total": total, "from_me": from_me or 0,
                    "from_them": (total - (from_me or 0)),
                    "last_date": last_date, "window": win or 0}

    def last_msg_is_from_me(cid):
        row = cur.execute("""
            SELECT m.is_from_me FROM chat_message_join cmj
            JOIN message m ON m.ROWID = cmj.message_id
            WHERE cmj.chat_id=? ORDER BY m.date DESC LIMIT 1""", (cid,)).fetchone()
        return bool(row[0]) if row else False

    def fetch_messages(cid, where, params, limit=None, desc=False):
        order = "DESC" if desc else "ASC"
        lim = f"LIMIT {limit}" if limit else ""
        return cur.execute(f"""
            SELECT m.ROWID, m.date, m.is_from_me, m.handle_id, m.text,
                   m.attributedBody, m.cache_has_attachments, m.associated_message_type
            FROM chat_message_join cmj
            JOIN message m ON m.ROWID = cmj.message_id
            WHERE cmj.chat_id=? AND {where}
            ORDER BY m.date {order} {lim}""", (cid, *params)).fetchall()

    def body(text, ablob, has_att, amt):
        if amt:                                   # tapback / associated message
            return tapback_label(amt)
        if text:
            return text
        decoded = decode_attributed_body(ablob)
        if decoded:
            return decoded
        if has_att:
            return "[attachment]"
        return "[unknown]"

    def render(rows):
        out = []
        for rowid, date, is_from_me, hid, text, ablob, has_att, amt in rows:
            sender = "me" if is_from_me else display(handle_addr.get(hid, "unknown"))
            out.append({
                "message_rowid": rowid,
                "date": date,
                "datetime": to_dt(date),
                "sender": sender,
                "text": body(text, ablob, has_att, amt),
            })
        return out

    conversations = []
    unresponded = []
    emitted = []  # every message we emit, for the watermark

    for cid, meta in chats.items():
        a = agg.get(cid)
        if not a or a["total"] == 0:
            continue

        is_group = len(meta["handles"]) > 1
        members = ([display(handle_addr.get(h, "unknown")) for h in meta["handles"]]
                   if is_group else None)

        if is_group:
            handle = None
            named = bool(meta["display_name"])
            name = meta["display_name"] or ("Group: " + ", ".join(members or []))
            contact_details = None
            responded = True
        else:
            addr = handle_addr.get(meta["handles"][0]) if meta["handles"] else meta["identifier"]
            rec = lookup(addr)
            handle = addr or meta["identifier"]
            named = rec is not None
            name = (rec["name"] if rec and rec["name"] else (addr or meta["identifier"]))
            contact_details = rec
            responded = last_msg_is_from_me(cid)

        # ---- unresponded (1:1 only, window mode; same filter as conversations) ----
        if (compute_unresponded and not is_group
                and not (cf.named_only and not named)
                and _passes_handle_digits(handle, cf.min_handle_digits)):
            last_is_them = not responded
            in_band = cutoff_lookback_db <= (a["last_date"] or 0) < cutoff_window_db
            qualifies = (a["from_me"] >= 1 and a["from_them"] >= 1) or a["from_them"] >= 2
            if last_is_them and in_band and qualifies:
                last_raw = a["last_date"] or 0
                last_unix = last_raw / units_per_sec + MAC_EPOCH_OFFSET
                unresponded.append({
                    "chat_rowid": cid,
                    "name": name,
                    "last_at": to_dt(last_raw),
                    "last_date_raw": last_raw,
                    "days_waiting": int((now - last_unix) // 86400),
                    "_sort": last_raw,
                })

        # ---- conversation filter (conditions.yaml) ----
        if a["window"] < cf.min_messages:
            continue
        if is_group and not cf.include_groups:
            continue
        if cf.named_only and not named:
            continue
        if not is_group and not _passes_handle_digits(handle, cf.min_handle_digits):
            continue

        in_window = fetch_messages(cid, f"m.date >= {cutoff_window_db}", ())
        if include_prefix and context_messages:
            prior = fetch_messages(cid, f"m.date < {cutoff_window_db}", (),
                                   limit=context_messages, desc=True)[::-1]
        else:
            prior = []
        rendered = render(prior + in_window)
        emitted.extend(rendered)

        conversations.append({
            "chat_rowid": cid,
            "name": name,
            "handle": handle,
            "is_named": named,
            "is_groupchat": is_group,
            "responded": responded,
            "members": members,
            "contact_details": contact_details,
            "window_messages": a["window"],
            "conversation": rendered,
            "_sort": a["last_date"] or 0,
        })

    con.close()

    conversations.sort(key=lambda c: c.pop("_sort"), reverse=True)
    unresponded.sort(key=lambda u: u.pop("_sort"), reverse=True)

    return {
        "generated_at": datetime.datetime.fromtimestamp(now).strftime("%Y-%m-%d %H:%M:%S"),
        "window": window_label,
        "context_messages": context_messages if include_prefix else 0,
        "watermark": compute_watermark(emitted),
        "conversations": conversations,
        "unresponded": unresponded,
    }


# --------------------------------------------------------------------------------- CLI
def main(argv=None):
    p = argparse.ArgumentParser(
        prog="text-triage extract",
        description="Export iMessage history from chat.db to JSON (the summarizer's source).",
    )
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--window", choices=["weekly", "monthly"],
                   help="fixed window; days come from conditions.yaml weekly_days/monthly_days")
    g.add_argument("--since", help="ISO datetime; incremental extract since this moment")
    p.add_argument("--out", help="write JSON here (default: stdout)")
    p.add_argument("--db", default=CHAT_DB, help="path to chat.db (default: ~/Library/Messages)")
    p.add_argument("--addressbook", default=ADDRESSBOOK_DIR, help="AddressBook dir for contacts")
    p.add_argument("--config", help="path to conditions.yaml (default: auto-discover)")
    args = p.parse_args(argv)

    config = load_config(args.config)
    result = extract(db_path=args.db, addressbook_dir=args.addressbook,
                     window=args.window, since=args.since, config=config)
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"Wrote {args.out}: {len(result['conversations'])} conversations, "
              f"{len(result['unresponded'])} unresponded")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
