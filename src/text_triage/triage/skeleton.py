"""Deterministic state-skeleton builder.

Transforms an :func:`text_triage.collect.extract.extract` export into the DETERMINISTIC fields of
``state.json`` — facts owned by code (PLAN "The LLM enriches; code owns correctness"). Every
agent-authored field (``identity``, ``summary``, ``tags``, ``daily``/``weekly``/``monthly``,
``history``) is left blank; the summary agents fill them in later.

Pure: no LLM, no file I/O. The returned :class:`State` is fully validated by ``schema.py``.
"""
from __future__ import annotations

import datetime
from typing import Optional

from text_triage.state.schema import State, validate_state

__all__ = ["build_skeleton", "reply_status_gate", "decayed_reply_status"]


def reply_status_gate(*, is_group: bool, responded: bool) -> str:
    """The deterministic reply gate — the fallback spine of ``reply_status``.

    1:1 whose last message is from them -> ``needs_response``; 1:1 whose last message is mine ->
    ``waiting_reply``; groups -> ``standby`` (always ``responded`` in the extractor, never
    deterministically owed a reply). The LLM may refine this for established conversations
    (judging *substance*); for new/short ones this gate is authoritative.
    """
    if is_group:
        return "standby"
    return "waiting_reply" if responded else "needs_response"


def decayed_reply_status(status: str, last_message_at: Optional[str], *, decay_days: int,
                         as_of: Optional[datetime.datetime] = None) -> str:
    """Query-time decay: a ``waiting_reply`` with no response for over ``decay_days`` presents as
    ``standby`` (the thread went quiet; nothing is owed in either direction). Computed on read like
    :func:`text_triage.triage.tags.effective_tags`, never written back. ``decay_days=0`` disables
    decay; an unparseable ``last_message_at`` keeps the status (lenient, like the tag TTLs)."""
    if status != "waiting_reply" or not decay_days:
        return status
    try:
        last = datetime.datetime.fromisoformat(last_message_at)
    except (TypeError, ValueError):
        return status
    as_of = as_of or datetime.datetime.now()
    if (as_of - last).total_seconds() / 86400.0 > decay_days:
        return "standby"
    return status


def _last_at(messages: list[dict], *, from_me: bool) -> Optional[str]:
    """The datetime of the newest message from me (or from them); None if that side is absent."""
    for m in reversed(messages):
        if (m.get("sender") == "me") == from_me:
            return m.get("datetime")
    return None


def build_skeleton(export: dict, *, generated_at: Optional[str] = None) -> State:
    """Build a validated :class:`State` skeleton from an extractor ``export`` dict — the
    deterministic facts only; every agent-authored field is left blank."""
    conversations = []
    for c in export["conversations"]:
        is_group = c["is_groupchat"]
        responded = c["responded"]
        messages = c["conversation"]
        last_message_at = messages[-1]["datetime"] if messages else export["generated_at"]
        conversations.append(
            {
                "chat_rowid": c["chat_rowid"],
                "name": c["name"],
                "is_group": is_group,
                "handle": None if is_group else c["handle"],
                "members": c["members"] if is_group else None,
                "status": "active",
                "last_from": "me" if responded else "them",
                "last_message_at": last_message_at,
                "reply_status": reply_status_gate(is_group=is_group, responded=responded),
                "last_from_me_at": _last_at(messages, from_me=True),
                "last_from_them_at": _last_at(messages, from_me=False),
            }
        )

    data = {
        "generated_at": generated_at or export["generated_at"],
        "watermark": export["watermark"],
        "unresponded": [
            {
                "chat_rowid": u["chat_rowid"],
                "name": u["name"],
                "last_at": u["last_at"],
                "days_waiting": u["days_waiting"],
            }
            for u in export.get("unresponded", [])
        ],
        "conversations": conversations,
    }
    return validate_state(data)
