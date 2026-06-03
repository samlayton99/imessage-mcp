"""The daily summarizer (PLAN Step 0, daily mode only).

Turns an :func:`text_triage.extract.extract` export plus the previous ``state.json`` into a fresh,
fully-validated :class:`~text_triage.schema.State`. The split PLAN demands:

  * **code owns the facts** — the deterministic skeleton (who texted last, ``needs_reply``,
    identity/members, watermark) comes from :func:`build_skeleton`; the LLM never moves it.
  * **the LLM enriches prose** — ``identity`` (only when blank, sticky once set), ``summary``,
    ``reply_reason`` (only when a reply is owed), one ``daily`` note, and *proposes* ``tags``.
  * **invalid never lands** — each merged record is schema-validated with one correction retry; on
    persistent failure the conversation falls back to its deterministic skeleton (no prose), so a
    single bad LLM return can never corrupt the batch.

Step 0 reads the raw texts from the extract *export* (the live ``texts_today`` layer is Steps 1-2);
when that lands, only the raw source changes — assembly/validation/merge here is unchanged.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Optional, Sequence, Union

from text_triage.config import Config
from text_triage.engine import Engine
from text_triage.schema import Conversation, State, validate_state
from text_triage.skeleton import build_skeleton
from text_triage.tags import load_law

__all__ = ["summarize_daily", "build_daily_prompt", "main"]

log = logging.getLogger(__name__)


def build_daily_prompt(skel: dict, raw: list, *, prev: Optional[dict], law: dict) -> str:
    """Assemble the per-conversation daily prompt: who they are + prior notes + the active tag law +
    the raw messages + the exact JSON shape to return. ``skel`` carries the deterministic facts
    (``name``, ``is_group``, ``needs_reply``)."""
    prev = prev or {}
    identity = prev.get("identity") or "(none yet — propose one if the messages support it)"
    prev_summary = prev.get("summary") or "(none yet)"
    prev_daily = "\n".join(f"  - {n['date']}: {n['text']}" for n in (prev.get("daily") or [])) or "  (none)"
    prev_monthly = prev.get("monthly") or "(none)"
    law_lines = "\n".join(f"  - {s}: {d}" for s, d in sorted(law.items())) or "  (none)"
    msg_lines = "\n".join(f"  [{m['datetime']}] {m['sender']}: {m['text']}" for m in raw) or "  (no messages)"
    reply_clause = (
        "A reply IS owed (the last message is theirs). Provide a one-sentence `reply_reason`."
        if skel.get("needs_reply") else
        "No reply is owed. Set `reply_reason` to null."
    )
    return f"""You maintain a concise, factual memory record for one iMessage conversation.

Conversation: {skel.get('name')} ({'group' if skel.get('is_group') else '1:1'})
Their current identity note: {identity}
Previous rolling summary: {prev_summary}
Previous daily notes:
{prev_daily}
Previous monthly note: {prev_monthly}

Active tags you may apply (use ONLY these slugs; omit the rest):
{law_lines}

Raw messages (oldest first):
{msg_lines}

{reply_clause}

Return ONLY a JSON object, no prose, with exactly these keys:
{{
  "identity": "<= 3 sentences on who they are, or null if unknown",
  "summary": "the new rolling summary (read first by an agent)",
  "reply_reason": "one sentence on why a reply is owed, or null",
  "daily_note": "one short dated-style line capturing what happened",
  "tags": ["slug", ...]   // subset of the active tags above, or []
}}"""


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


def _merge(skel: dict, prev: Optional[dict], llm: dict, *, law_slugs: set, config: Config,
           note_date: str, now_str: str) -> dict:
    """Build a candidate record: deterministic facts from ``skel``, prose from ``llm``, prior
    notes/edited carried from ``prev``. Never overwrites an ``edited`` field; identity is sticky."""
    prev = prev or {}
    edited = prev.get("edited") or {}
    rec = dict(skel)  # facts + blank LLM fields

    prev_identity = prev.get("identity")
    if prev_identity:                                   # sticky once set
        rec["identity"] = prev_identity
    elif "identity" not in edited:                      # propose only when blank + not user-owned
        rec["identity"] = llm.get("identity") or None

    rec["summary"] = (llm.get("summary") or None)
    rec["reply_reason"] = ((llm.get("reply_reason") or None) if rec.get("needs_reply") else None)

    note = {"date": note_date, "text": (llm.get("daily_note") or "").strip() or "(no note)"}
    rec["daily"] = (list(prev.get("daily") or []) + [note])[-config.daily_cap:]

    rec["weekly"] = list(prev.get("weekly") or [])      # daily mode does not touch these
    rec["monthly"] = prev.get("monthly")
    rec["history"] = list(prev.get("history") or [])
    rec["edited"] = edited

    rec["tags"] = [t for t in (llm.get("tags") or []) if t in law_slugs]
    rec["last_updated"] = now_str
    return rec


def _validate_record(rec: dict, *, law_slugs: set, config: Config) -> Conversation:
    return Conversation.model_validate(
        rec, context={"law": law_slugs, "daily_cap": config.daily_cap, "weekly_cap": config.weekly_cap}
    )


def _summarize_one(skel: dict, raw: list, prev: Optional[dict], *, engine: Engine, config: Config,
                   law: dict, law_slugs: set, note_date: str, now_str: str) -> Conversation:
    prompt = build_daily_prompt(skel, raw, prev=prev, law=law)
    last_err: Optional[Exception] = None
    for _ in range(2):
        try:
            text = engine.summarize(prompt, model=config.models.daily)
            llm = _parse_json(text)
            rec = _merge(skel, prev, llm, law_slugs=law_slugs, config=config,
                         note_date=note_date, now_str=now_str)
            return _validate_record(rec, law_slugs=law_slugs, config=config)
        except Exception as e:  # parse / validation / engine failure -> correct and retry
            last_err = e
            prompt = (build_daily_prompt(skel, raw, prev=prev, law=law)
                      + f"\n\nYour previous reply was rejected: {e}\nReturn ONLY valid JSON in the exact shape.")
    log.warning("daily summary for chat_rowid=%s failed twice (%s); keeping deterministic skeleton",
                skel.get("chat_rowid"), last_err)
    return _validate_record(skel, law_slugs=law_slugs, config=config)  # facts only, prose blank


def summarize_daily(export: dict, *, engine: Engine, config: Optional[Config] = None,
                    prev_state: Optional[Union[State, dict]] = None, law: Optional[dict] = None,
                    generated_at: Optional[str] = None, limit: Optional[int] = None) -> State:
    """Produce a fresh, validated :class:`State` from ``export`` and the previous state.

    Calls ``engine`` once (plus at most one retry) per conversation **with new messages**;
    conversations present only in ``prev_state`` are carried forward untouched. ``law`` is the active
    tag law (``{slug: description}``); when omitted it is loaded from ``watch.md``. ``limit`` is a
    cost cap: only the first ``limit`` conversations (the export is recency-sorted) get the LLM pass;
    the rest are emitted untouched (their previous record, or a deterministic skeleton) with no call.
    """
    if config is None:
        config = Config()
    if law is None:
        law = load_law()
    law_slugs = set(law)

    sk = build_skeleton(export, config=config).model_dump()
    generated_at = generated_at or sk["generated_at"]
    note_date, now_str = generated_at[:10], generated_at

    raw_by_id = {c["chat_rowid"]: c["conversation"] for c in export["conversations"]}
    prev_dict = prev_state.model_dump() if isinstance(prev_state, State) else (prev_state or {})
    prev_by_id = {c["chat_rowid"]: c for c in prev_dict.get("conversations", [])}

    out, seen = [], set()
    for idx, skel in enumerate(sk["conversations"]):
        cid = skel["chat_rowid"]
        if limit is not None and idx >= limit:           # cost cap: no LLM call beyond the limit
            prev = prev_by_id.get(cid)
            out.append(prev if prev else
                       _validate_record(skel, law_slugs=law_slugs, config=config).model_dump())
            seen.add(cid)
            continue
        rec = _summarize_one(skel, raw_by_id.get(cid, []), prev_by_id.get(cid),
                             engine=engine, config=config, law=law, law_slugs=law_slugs,
                             note_date=note_date, now_str=now_str)
        out.append(rec.model_dump())
        seen.add(cid)

    for cid, prec in prev_by_id.items():               # carry forward idle conversations
        if cid not in seen:
            out.append(prec)

    state = {
        "generated_at": generated_at,
        "watermark": sk["watermark"],
        "unresponded": sk["unresponded"],
        "conversations": out,
    }
    return validate_state(state, law=law_slugs, daily_cap=config.daily_cap, weekly_cap=config.weekly_cap)


def main(argv: Optional[Sequence[str]] = None, *, engine: Optional[Engine] = None) -> int:
    """``text-triage summarize`` — extract a window, run the daily LLM summary, write state.json.

    Reuses the existing state file (``--out``, if present) as the previous state so re-derives fold
    in. ``engine`` is injectable for tests; in production it is built from ``engine.provider``.
    """
    import argparse

    from text_triage.config import load_config
    from text_triage.engine import make_engine
    from text_triage.extract import ADDRESSBOOK_DIR, CHAT_DB, extract
    from text_triage.state_io import read_state, write_state
    from text_triage.tags import active_slugs, load_law

    p = argparse.ArgumentParser(
        prog="text-triage summarize",
        description="Daily LLM summary of recent iMessages into a validated state.json.",
    )
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--window", choices=["weekly", "monthly"],
                   help="window days come from conditions.yaml weekly_days/monthly_days")
    g.add_argument("--since", help="ISO datetime; incremental summary since this moment")
    p.add_argument("--out", help="state.json to write/update (default: stdout)")
    p.add_argument("--db", default=CHAT_DB, help="path to chat.db")
    p.add_argument("--addressbook", default=ADDRESSBOOK_DIR, help="AddressBook dir for contacts")
    p.add_argument("--config", help="path to conditions.yaml (default: auto-discover)")
    p.add_argument("--watch", help="path to watch.md tag scratchpad (default: auto-discover)")
    p.add_argument("--limit", type=int,
                   help="cost cap: only summarize the N most-recent conversations (rest left untouched)")
    args = p.parse_args(argv)

    config = load_config(args.config)
    law = load_law(args.watch)
    caps = {"law": active_slugs(law), "daily_cap": config.daily_cap, "weekly_cap": config.weekly_cap}
    if engine is None:
        engine = make_engine(config)

    export = extract(db_path=args.db, addressbook_dir=args.addressbook,
                     window=args.window, since=args.since, config=config)
    prev = read_state(args.out, **caps) if args.out and Path(args.out).exists() else None
    state = summarize_daily(export, engine=engine, config=config, prev_state=prev, law=law,
                            limit=args.limit)

    if args.out:
        write_state(state, args.out, **caps)
        n_sum = sum(1 for c in state.conversations if c.summary is not None)
        print(f"Wrote {args.out}: {n_sum} summarized / {len(state.conversations)} conversations")
    else:
        print(state.model_dump_json(indent=2))
    return 0
