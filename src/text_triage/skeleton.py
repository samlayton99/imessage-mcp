"""Deterministic state-skeleton builder.

Transforms an :func:`text_triage.extract.extract` export into the DETERMINISTIC fields of
``state.json`` — facts owned by code (PLAN "The LLM enriches; code owns correctness"). Every
LLM-authored field (``identity``, ``summary``, ``reply_reason``, ``tags``, ``daily``/``weekly``/
``monthly``, ``history``) is left blank; the summarizer fills them in a later step.

Pure: no LLM, no file I/O. The returned :class:`State` is fully validated by ``schema.py``.
"""
from __future__ import annotations

from typing import Optional

from text_triage.config import Config
from text_triage.schema import State, validate_state

__all__ = ["build_skeleton", "needs_reply_gate"]


def needs_reply_gate(*, is_group: bool, responded: bool) -> bool:
    """The deterministic reply gate (PLAN "needs_reply gated, not invented").

    True only for a 1:1 whose last substantive message is from them (``responded`` is False).
    Groups are always ``responded`` in the extractor, so they never deterministically need a reply.
    The LLM may later refine this and must justify it with a ``reply_reason``.
    """
    return (not is_group) and (not responded)


def build_skeleton(
    export: dict, *, config: Optional[Config] = None, generated_at: Optional[str] = None
) -> State:
    """Build a validated :class:`State` skeleton from an extractor ``export`` dict.

    ``config`` (a :class:`text_triage.config.Config`) supplies the note-array caps used during
    validation; defaults are used if omitted. Skeleton records carry no notes yet, so the caps
    matter only once the summarizer fills ``daily``/``weekly`` — wiring them here keeps the whole
    validation path config-driven."""
    if config is None:
        config = Config()
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
                "needs_reply": needs_reply_gate(is_group=is_group, responded=responded),
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
    return validate_state(data, daily_cap=config.daily_cap, weekly_cap=config.weekly_cap)
