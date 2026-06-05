"""The summary agents (PLAN Step 0+amendments): daily, weekly, monthly.

Each turns an :func:`text_triage.collect.extract.extract` export + the previous ``state.json`` into a fresh,
validated :class:`~text_triage.state.schema.State`, with strictly non-overlapping write power (the
agent→field matrix in the handoff PLAN.md):

  * **daily** — reads the whole record + new raw msgs; appends one ``daily`` note, ADDS tags (never
    deletes), clears its ``texts_today``. Touches nothing else.
  * **weekly** — re-reads the last 7 days raw; appends one ``weekly`` note, **clears ``daily[]``**,
    rarely (re)proposes a blank ``identity``, add/deletes tags.
  * **monthly** — re-reads the last 30 days raw; rewrites ``monthly``, condenses one ``history``
    line, **clears ``weekly[]`` and ``daily[]``**, rarely (re)proposes a blank ``identity``,
    add/deletes tags, marks silent-30d conversations ``dormant``.

Code owns the facts (`build_skeleton`); the LLM writes prose + proposes tags; each record is
schema-validated with one retry, and on persistent failure the prior record is kept untouched, so a
bad LLM return never corrupts the batch. Prompts live in ``agents/<mode>.md`` (filled by
``prompts.render``); tags carry lifetimes (``tags.py``) and are shown to the model with hints.
"""
from __future__ import annotations

import asyncio
import datetime
import json
import logging
import re
from pathlib import Path
from typing import Optional, Sequence, Union

from text_triage.config import Config
from text_triage.triage.engine import Engine
from text_triage.triage.prompts import build_system, build_user
from text_triage.state.schema import Conversation, State, validate_state
from text_triage.triage.skeleton import build_skeleton
from text_triage.triage.tags import active_slugs, load_law

__all__ = [
    "summarize_daily", "summarize_weekly", "summarize_monthly",
    "build_daily_prompt", "build_weekly_prompt", "build_monthly_prompt", "build_contexts", "main",
]

log = logging.getLogger(__name__)

# Agent-authored fields carried forward from the previous record (facts come fresh from extract).
# `summarized_through` (the per-conversation summary cursor) carries so a skipped conversation keeps
# its cursor and its new messages accumulate until they cross the floor.
_CARRY = ("identity", "tags", "daily", "weekly", "monthly", "history", "edited", "texts_today",
          "summarized_through")


# --------------------------------------------------------------------------- formatting
def _kind(skel: dict) -> str:
    return "group" if skel.get("is_group") else "1:1"


def _format_law(law: dict) -> str:
    lines = []
    for slug, spec in sorted(law.items()):
        life = "sticky" if spec.lifetime == "sticky" else f"ttl {spec.ttl_days}d"
        lines.append(f"  - {slug} ({life}): {spec.description}")
    return "\n".join(lines) or "  (none)"


def _format_messages(msgs: list) -> str:
    return "\n".join(f"  [{m['datetime']}] {m['sender']}: {m['text']}" for m in msgs) or "  (no messages)"


def _format_dated(notes: list) -> str:
    return "\n".join(f"  - {n['date']}: {n['text']}" for n in (notes or [])) or "  (none)"


def _format_weekly(notes: list) -> str:
    return "\n".join(f"  - {n['week_of']}: {n['text']}" for n in (notes or [])) or "  (none)"


# ------------------------------------------------------------------------- prompt builders
def _cap_raw(raw: list, caps, mode: str) -> list:
    """The per-call context budget: keep only the newest-N raw messages for this cadence (raw is
    oldest-first), from ``engine.max_raw_messages``; 0 = no cap."""
    n = getattr(caps, mode, 0)
    return raw[-n:] if n and len(raw) > n else raw


def build_daily_prompt(skel: dict, raw: list, *, prev: Optional[dict], law: dict, agents_dir=None):
    """(system, user) for the daily agent: shared global frame + daily role; per-conversation data."""
    prev = prev or {}
    system = build_system("daily", law=_format_law(law), agents_dir=agents_dir)
    user = build_user("daily", {
        "name": skel.get("name"), "kind": _kind(skel),
        "identity": prev.get("identity") or "(none yet)",
        "monthly": prev.get("monthly") or "(none yet)",
        "weekly": _format_weekly(prev.get("weekly")),
        "daily": _format_dated(prev.get("daily")),
        "history": _format_dated(prev.get("history")),
        "msg_count": len(raw),
        "messages": _format_messages(raw),
    }, agents_dir=agents_dir)
    return system, user


def build_weekly_prompt(skel: dict, raw: list, *, prev: Optional[dict], law: dict, agents_dir=None):
    """(system, user) for the weekly agent. The user payload omits daily notes (week rebuilt from raw)."""
    prev = prev or {}
    system = build_system("weekly", law=_format_law(law), agents_dir=agents_dir)
    user = build_user("weekly", {
        "name": skel.get("name"), "kind": _kind(skel),
        "identity": prev.get("identity") or "(none yet)",
        "monthly": prev.get("monthly") or "(none yet)",
        "history": _format_dated(prev.get("history")),
        "msg_count": len(raw),
        "messages": _format_messages(raw),
    }, agents_dir=agents_dir)
    return system, user


def build_monthly_prompt(skel: dict, raw: list, *, prev: Optional[dict], law: dict, agents_dir=None):
    """(system, user) for the monthly agent. The user payload omits weekly + daily notes."""
    prev = prev or {}
    system = build_system("monthly", law=_format_law(law), agents_dir=agents_dir)
    user = build_user("monthly", {
        "name": skel.get("name"), "kind": _kind(skel),
        "identity": prev.get("identity") or "(none yet)",
        "monthly": prev.get("monthly") or "(none yet)",
        "history": _format_dated(prev.get("history")),
        "msg_count": len(raw),
        "messages": _format_messages(raw),
    }, agents_dir=agents_dir)
    return system, user


_BUILDERS = {"daily": build_daily_prompt, "weekly": build_weekly_prompt, "monthly": build_monthly_prompt}


# --------------------------------------------------------------------------- parse / merge
def _parse_json(text: str) -> dict:
    """Parse the model's reply into a dict, tolerating ```json fences or surrounding prose."""
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
        t = re.sub(r"\s*```$", "", t).strip()
    try:
        obj = json.loads(t)
    except json.JSONDecodeError:
        i, j = t.find("{"), t.rfind("}")
        if i == -1 or j == -1 or j < i:
            raise
        obj = json.loads(t[i:j + 1])
    if not isinstance(obj, dict):
        raise ValueError("model reply was not a JSON object")
    return obj


def _clean(v, default: str) -> str:
    return (v or "").strip() or default


def _merge_tags(existing, proposed, law_slugs: set, *, mode: str, edited_has_tags: bool) -> list:
    """daily (or any mode when the human owns tags) → add-only union; weekly/monthly auto → full
    replace. Either way, tags no longer in the law are dropped."""
    keep = [t for t in (existing or []) if t in law_slugs]
    new = [t for t in (proposed or []) if t in law_slugs]
    if mode == "daily" or edited_has_tags:
        out = list(keep)
        for t in new:
            if t not in out:
                out.append(t)
        return out
    out = []                                            # weekly/monthly auto: replace
    for t in new:
        if t not in out:
            out.append(t)
    return out


def _resolve_identity(prev: dict, llm_identity, edited: dict):
    """Identity is sticky once set and untouchable once human-edited; otherwise the agent may
    propose one when it is blank."""
    if "identity" in edited or prev.get("identity"):
        return prev.get("identity")
    return llm_identity or None


def _parse_dt(s: Optional[str]) -> Optional[datetime.datetime]:
    if not s:
        return None
    try:
        return datetime.datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None


def _silent_over_30d(last_message_at: str, generated_at: str) -> bool:
    a, b = _parse_dt(generated_at), _parse_dt(last_message_at)
    return a is not None and b is not None and (a - b).total_seconds() / 86400.0 > 30


def _carry(skel: dict, prev: Optional[dict]) -> dict:
    """A fresh record = deterministic facts from ``skel`` + the previous agent-authored fields."""
    rec = dict(skel)
    prev = prev or {}
    for f in _CARRY:
        if f in prev:
            rec[f] = prev[f]
    return rec


def _apply(mode: str, skel: dict, prev: Optional[dict], llm: dict, *, law_slugs: set,
           generated_at: str, note_date: str, now_str: str) -> dict:
    prev = prev or {}
    edited = prev.get("edited") or {}
    rec = _carry(skel, prev)
    rec["last_updated"] = now_str
    has_tags = "tags" in edited

    if mode == "daily":
        rec["daily"] = list(rec.get("daily") or []) + [
            {"date": note_date, "text": _clean(llm.get("daily_note"), "(no note)")}]
        rec["tags"] = _merge_tags(rec.get("tags"), llm.get("tags"), law_slugs,
                                  mode="daily", edited_has_tags=has_tags)
        rec["texts_today"] = []                          # daily reads then clears it
    elif mode == "weekly":
        rec["weekly"] = list(rec.get("weekly") or []) + [
            {"week_of": note_date, "text": _clean(llm.get("weekly_note"), "(no note)")}]
        rec["daily"] = []                                # weekly clears daily
        rec["identity"] = _resolve_identity(prev, llm.get("identity"), edited)
        rec["tags"] = _merge_tags(rec.get("tags"), llm.get("tags"), law_slugs,
                                  mode="weekly", edited_has_tags=has_tags)
    elif mode == "monthly":
        rec["monthly"] = llm.get("monthly") or None
        rec["history"] = list(rec.get("history") or []) + [
            {"date": note_date, "text": _clean(llm.get("history_line"), "not enough context")}]
        rec["weekly"], rec["daily"] = [], []             # monthly clears both
        rec["identity"] = _resolve_identity(prev, llm.get("identity"), edited)
        rec["tags"] = _merge_tags(rec.get("tags"), llm.get("tags"), law_slugs,
                                  mode="monthly", edited_has_tags=has_tags)
        rec["status"] = "dormant" if _silent_over_30d(rec["last_message_at"], generated_at) else "active"
    return rec


def _validate_record(rec: dict, *, law_slugs: set) -> Conversation:
    return Conversation.model_validate(rec, context={"law": law_slugs})


async def _run_one(mode, skel, raw, prev, *, engine, config, law, law_slugs, generated_at, note_date,
                   now_str, agents_dir) -> Conversation:
    raw = _cap_raw(raw, config.engine.max_raw_messages, mode)
    builder = _BUILDERS[mode]
    model = getattr(config.engine.models, mode)
    system, user = builder(skel, raw, prev=prev, law=law, agents_dir=agents_dir)
    last_err: Optional[Exception] = None
    cur_user = user
    for _ in range(2):
        try:
            text = await engine.summarize(system, cur_user, model=model)
            llm = _parse_json(text)
            rec = _apply(mode, skel, prev, llm, law_slugs=law_slugs, generated_at=generated_at,
                         note_date=note_date, now_str=now_str)
            return _validate_record(rec, law_slugs=law_slugs)
        except Exception as e:
            last_err = e
            cur_user = (user + f"\n\nYour previous reply was rejected: {e}\n"
                        "Return ONLY valid JSON in the exact shape.")
    log.warning("%s summary for chat_rowid=%s failed twice (%s); keeping the prior record",
                mode, skel.get("chat_rowid"), last_err)
    return _validate_record(_carry(skel, prev), law_slugs=law_slugs)   # facts refreshed, prose kept


# --------------------------------------------------------------------------- the agents
async def _summarize(mode: str, export: dict, *, engine: Engine, config: Optional[Config] = None,
                     prev_state: Optional[Union[State, dict]] = None, law: Optional[dict] = None,
                     generated_at: Optional[str] = None, limit: Optional[int] = None,
                     agents_dir=None) -> State:
    if config is None:
        config = Config()
    if law is None:
        law = load_law()
    law_slugs = set(law)

    sk = build_skeleton(export, generated_at=generated_at).model_dump()
    generated_at = generated_at or sk["generated_at"]
    note_date, now_str = generated_at[:10], generated_at

    raw_by_id = {c["chat_rowid"]: c["conversation"] for c in export["conversations"]}
    # The delta gate (daily only): skip the LLM call for a conversation with < summarize_floor NEW
    # messages. `new_count` is set only by the raw-store deltas path; absent (chatdb / direct calls) ->
    # ungated. `current_max` advances the per-conversation cursor when a real summary happens.
    new_count_by_id = {c["chat_rowid"]: c.get("new_count") for c in export["conversations"]}
    current_max_by_id = {cid: max((m["message_rowid"] for m in msgs), default=0)
                         for cid, msgs in raw_by_id.items()}
    gate, floor = (mode == "daily"), config.messages.summarize_floor
    prev_dict = prev_state.model_dump() if isinstance(prev_state, State) else (prev_state or {})
    prev_by_id = {c["chat_rowid"]: c for c in prev_dict.get("conversations", [])}

    # Fan out one task per chat_rowid (a partition: two LLMs never touch the same conversation),
    # bounded by a semaphore. Each task is pure — it returns a record, mutates no shared state — and
    # gather preserves order, so out[] matches conversation order. The single write happens in the
    # caller, after all tasks finish (no concurrent writers). StubEngine list-mode assumes sequential
    # consumption; the suite issues <=1 concurrent call, so it stays deterministic.
    sem = asyncio.Semaphore(config.engine.max_concurrency)

    async def _slot(idx, skel):
        cid = skel["chat_rowid"]
        prev = prev_by_id.get(cid)
        if limit is not None and idx >= limit:                  # cost cap: no LLM call beyond limit
            return (prev if prev else dict(skel)), cid
        nc = new_count_by_id.get(cid)
        if gate and nc is not None and nc < floor:              # delta gate: too few new msgs -> ride raw
            return (prev if prev else dict(skel)), cid
        async with sem:
            rec = await _run_one(mode, skel, raw_by_id.get(cid, []), prev,
                                 engine=engine, config=config, law=law, law_slugs=law_slugs,
                                 generated_at=generated_at, note_date=note_date, now_str=now_str,
                                 agents_dir=agents_dir)
        rec = rec.model_dump()
        rec["summarized_through"] = max(rec.get("summarized_through") or 0, current_max_by_id.get(cid, 0))
        return rec, cid

    slots = await asyncio.gather(*(_slot(i, s) for i, s in enumerate(sk["conversations"])))
    out = [rec for rec, _ in slots]
    seen = {cid for _, cid in slots}

    for cid, prec in prev_by_id.items():                        # carry forward idle conversations
        if cid not in seen:
            prec = dict(prec)
            prec["tags"] = [t for t in (prec.get("tags") or []) if t in law_slugs]   # self-heal law edits
            out.append(prec)

    for rec in out:                                             # deterministic: never summarized <-> cursor 0
        rec["new_conversation"] = (rec.get("summarized_through") or 0) == 0

    # daily (incremental/since) carries the prior unresponded list; weekly/monthly recompute it.
    unresponded = (prev_dict.get("unresponded", []) or sk["unresponded"]) if mode == "daily" else sk["unresponded"]
    state = {
        "generated_at": generated_at,
        "watermark": _max_watermark(sk["watermark"], prev_dict.get("watermark")),
        "unresponded": unresponded,
        "conversations": out,
    }
    return validate_state(state, law=law_slugs)


def _max_watermark(new: dict, prev: Optional[dict]) -> dict:
    if not prev:
        return new
    if (new["max_date_raw"], new["max_message_rowid"]) < (prev["max_date_raw"], prev["max_message_rowid"]):
        return prev
    return new


def summarize_daily(export, **kw) -> State:
    return asyncio.run(_summarize("daily", export, **kw))


def summarize_weekly(export, **kw) -> State:
    return asyncio.run(_summarize("weekly", export, **kw))


def summarize_monthly(export, **kw) -> State:
    return asyncio.run(_summarize("monthly", export, **kw))


def build_contexts(mode: str, export: dict, *, config: Optional[Config] = None,
                   prev_state: Optional[Union[State, dict]] = None, law: Optional[dict] = None,
                   generated_at: Optional[str] = None, limit: Optional[int] = None,
                   agents_dir=None) -> list:
    """Assemble the exact ``(system, user, model)`` each conversation WOULD be summarized with — no
    engine, no network. Backs ``--show-context`` so prompt tweaks can be eyeballed before spending
    tokens. Applies the same ``max_raw_messages`` cap and ``limit`` as a real run."""
    if config is None:
        config = Config()
    if law is None:
        law = load_law()
    sk = build_skeleton(export, generated_at=generated_at).model_dump()
    raw_by_id = {c["chat_rowid"]: c["conversation"] for c in export["conversations"]}
    prev_dict = prev_state.model_dump() if isinstance(prev_state, State) else (prev_state or {})
    prev_by_id = {c["chat_rowid"]: c for c in prev_dict.get("conversations", [])}
    builder = _BUILDERS[mode]
    model = getattr(config.engine.models, mode)
    out = []
    for idx, skel in enumerate(sk["conversations"]):
        if limit is not None and idx >= limit:
            break
        cid = skel["chat_rowid"]
        raw = _cap_raw(raw_by_id.get(cid, []), config.engine.max_raw_messages, mode)
        system, user = builder(skel, raw, prev=prev_by_id.get(cid), law=law, agents_dir=agents_dir)
        out.append({"chat_rowid": cid, "name": skel.get("name"), "model": model,
                    "system": system, "user": user, "est_tokens": (len(system) + len(user)) // 4})
    return out


# --------------------------------------------------------------------------------- CLI
def main(argv: Optional[Sequence[str]] = None, *, engine: Optional[Engine] = None) -> int:
    """``text-triage summarize --mode {daily,weekly,monthly}`` — extract the mode's window, run that
    agent, write/update state.json. Reuses an existing ``--out`` file as the previous state."""
    import argparse

    from text_triage.config import load_config
    from text_triage.triage.engine import make_engine
    from text_triage.collect.extract import ADDRESSBOOK_DIR, CHAT_DB, extract
    from text_triage.state.state_io import read_state, write_state

    p = argparse.ArgumentParser(
        prog="text-triage summarize",
        description="Run a daily/weekly/monthly summary agent into a validated state.json.",
    )
    p.add_argument("--mode", choices=["daily", "weekly", "monthly"], default="daily")
    p.add_argument("--since", help="ISO datetime override for daily's incremental window "
                                   "(--source chatdb only; the raw-store path uses per-conversation cursors)")
    p.add_argument("--out", help="state.json to write/update (default: stdout)")
    p.add_argument("--source", choices=["chatdb", "raw-store"], default="chatdb",
                   help="raw source: chatdb (the Mac, default) or raw-store (the server's "
                        "raw_messages.sqlite — how the scheduler runs summaries on the always-on host)")
    p.add_argument("--db", default=CHAT_DB, help="path to chat.db (with --source chatdb)")
    p.add_argument("--raw-store", dest="raw_store",
                   help="path to raw_messages.sqlite (with --source raw-store)")
    p.add_argument("--addressbook", default=ADDRESSBOOK_DIR, help="AddressBook dir for contacts")
    p.add_argument("--config", help="path to conditions.yaml (default: auto-discover)")
    p.add_argument("--watch", help="path to watch.md tag scratchpad (default: auto-discover)")
    p.add_argument("--limit", type=int,
                   help="cost cap: only summarize the N most-recent conversations")
    p.add_argument("--show-context", action="store_true",
                   help="print the exact system/user/model per conversation and exit (no LLM call)")
    args = p.parse_args(argv)

    config = load_config(args.config)
    law = load_law(args.watch)
    prev = read_state(args.out, law=active_slugs(law)) if args.out and Path(args.out).exists() else None

    daily_since = args.since or (datetime.datetime.now() - datetime.timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
    if args.source == "raw-store":  # the server rebuilds the window from its raw store, no chat.db
        from text_triage.server.raw_store import deltas as raw_deltas, export as raw_export
        raw_path = args.raw_store or str(Path.home() / ".text-triage" / "raw_messages.sqlite")
        if args.mode in ("monthly", "weekly"):
            export = raw_export(window=args.mode, config=config, path=raw_path)
        else:  # daily: per-conversation deltas since each conversation's own last-summary cursor
            cursors = {c.chat_rowid: c.summarized_through for c in prev.conversations} if prev else {}
            export = raw_deltas(cursors, path=raw_path)
    elif args.mode == "monthly":
        export = extract(db_path=args.db, addressbook_dir=args.addressbook, window="monthly", config=config)
    elif args.mode == "weekly":
        export = extract(db_path=args.db, addressbook_dir=args.addressbook, window="weekly", config=config)
    else:  # daily: the messages since last run (Step-0 stand-in for the watcher's texts_today)
        export = extract(db_path=args.db, addressbook_dir=args.addressbook, since=daily_since, config=config)

    if args.show_context:                       # inspect the exact prompts; no LLM call, no spend
        ctxs = build_contexts(args.mode, export, config=config, prev_state=prev, law=law, limit=args.limit)
        for c in ctxs:
            print(f"=== chat_rowid {c['chat_rowid']}  {c['name']}  model={c['model']}  ~{c['est_tokens']} tokens ===")
            print("--- SYSTEM ---")
            print(c["system"])
            print("--- USER ---")
            print(c["user"])
            print()
        print(f"[{args.mode}] {len(ctxs)} conversation context(s) shown; no LLM call made.")
        return 0

    if not export["conversations"]:             # nothing new -> never overwrite a good state.json
        print(f"[{args.mode}] source is empty; leaving {args.out or 'state.json'} untouched.")
        return 0

    if engine is None:
        engine = make_engine(config)
    fn = {"daily": summarize_daily, "weekly": summarize_weekly, "monthly": summarize_monthly}[args.mode]
    state = fn(export, engine=engine, config=config, prev_state=prev, law=law, limit=args.limit)

    if args.out:
        write_state(state, args.out, law=active_slugs(law))
        n = sum(1 for c in state.conversations if c.daily or c.weekly or c.monthly or c.identity)
        print(f"Wrote {args.out} [{args.mode}]: {n} enriched / {len(state.conversations)} conversations")
    else:
        print(state.model_dump_json(indent=2))
    return 0
