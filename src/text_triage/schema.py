"""The type contract for ``state.json``.

The validated record IS the product (PLAN "Non-negotiables"): every conversation is type-checked
(shape AND cross-field rules) on write and on MCP read, so consumers trust it blindly. The LLM
enriches prose fields; code owns correctness. Invalid records must never land.

Cross-field rules enforced here (PLAN "Hardening"):
  - is_group => non-empty ``members`` and no ``handle``; else ``handle`` and no ``members``
  - ``identity`` <= 3 sentences; ``daily`` <= 7; ``weekly`` <= 5; ``history`` entries dated
  - ``tags`` subset of the active∪retired law (enforced only when a law is supplied via context)
  - ``needs_reply`` => non-empty ``reply_reason`` — scoped to *summarized* records (``summary`` set),
    so deterministic skeleton records (``summary is None``) may carry ``needs_reply`` with no reason.
"""
from __future__ import annotations

import re
from datetime import date
from typing import Literal, Optional

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)

__all__ = [
    "Watermark",
    "Unresponded",
    "DatedNote",
    "WeeklyNote",
    "TodayMessage",
    "TextsTodayConversation",
    "TextsToday",
    "Conversation",
    "State",
    "validate_state",
    "is_valid_state",
    "ValidationError",
]


# Default note-array caps when no config is supplied. In production these are DERIVED from the
# windows in conditions.yaml (daily_cap = weekly_days; weekly_cap = ceil(monthly_days / 7)) and
# injected via validation context, so a cadence change can never conflict with the type contract.
DEFAULT_DAILY_CAP = 7
DEFAULT_WEEKLY_CAP = 5


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Watermark(_Base):
    max_date_raw: int
    max_message_rowid: int


class Unresponded(_Base):
    chat_rowid: int
    name: str
    last_at: str
    days_waiting: int = Field(ge=0)


class DatedNote(_Base):
    """A dated note (``daily`` / ``history``). ``date`` is ISO ``YYYY-MM-DD`` or the literal
    ``"not enough context"`` sentinel (used by sparse history)."""

    date: str
    text: str

    @field_validator("date")
    @classmethod
    def _dated(cls, v: str) -> str:
        if v == "not enough context":
            return v
        try:
            date.fromisoformat(v)
        except ValueError as e:
            raise ValueError(
                f"date must be ISO YYYY-MM-DD or 'not enough context', got {v!r}"
            ) from e
        return v


class WeeklyNote(_Base):
    week_of: str
    text: str


class TodayMessage(_Base):
    message_rowid: int
    datetime: str
    sender: str
    text: str


class TextsTodayConversation(_Base):
    name: str
    messages: list[TodayMessage] = []


class TextsToday(_Base):
    """The live layer: raw unsummarized messages pushed from the Mac, keyed by ``chat_rowid``
    (as str). Empty in a deterministic skeleton; populated by ingest in a later step."""

    since: Optional[str] = None
    conversations: dict[str, TextsTodayConversation] = {}


class Conversation(_Base):
    # --- deterministic facts (owned by code) ---
    chat_rowid: int
    name: str
    is_group: bool
    handle: Optional[str] = None
    members: Optional[list[str]] = None
    status: Literal["active", "dormant"] = "active"
    last_from: Literal["me", "them"]
    last_message_at: str
    last_updated: Optional[str] = None
    needs_reply: bool = False
    # --- LLM-authored (blank in a skeleton record) ---
    identity: Optional[str] = None
    summary: Optional[str] = None
    reply_reason: Optional[str] = None
    tags: list[str] = []
    daily: list[DatedNote] = []
    weekly: list[WeeklyNote] = []
    monthly: Optional[str] = None
    history: list[DatedNote] = []
    edited: dict[str, str] = {}

    @field_validator("identity")
    @classmethod
    def _identity_max_three_sentences(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        sentences = [s for s in re.split(r"[.!?]+", v) if s.strip()]
        if len(sentences) > 3:
            raise ValueError(f"identity must be <= 3 sentences, got {len(sentences)}")
        return v

    @field_validator("daily")
    @classmethod
    def _daily_cap(cls, v: list, info: ValidationInfo) -> list:
        cap = (info.context or {}).get("daily_cap") if info.context else None
        cap = DEFAULT_DAILY_CAP if cap is None else cap
        if len(v) > cap:
            raise ValueError(f"daily capped at {cap}, got {len(v)}")
        return v

    @field_validator("weekly")
    @classmethod
    def _weekly_cap(cls, v: list, info: ValidationInfo) -> list:
        cap = (info.context or {}).get("weekly_cap") if info.context else None
        cap = DEFAULT_WEEKLY_CAP if cap is None else cap
        if len(v) > cap:
            raise ValueError(f"weekly capped at {cap}, got {len(v)}")
        return v

    @field_validator("tags")
    @classmethod
    def _tags_subset_of_law(cls, v: list[str], info: ValidationInfo) -> list[str]:
        law = (info.context or {}).get("law") if info.context else None
        if law is not None:
            extra = set(v) - set(law)
            if extra:
                raise ValueError(f"tags not in active∪retired law: {sorted(extra)}")
        return v

    @model_validator(mode="after")
    def _group_shape(self) -> "Conversation":
        if self.is_group:
            if not self.members:
                raise ValueError("group conversation requires non-empty members")
            if self.handle is not None:
                raise ValueError("group conversation must not have a handle")
        else:
            if not self.handle:
                raise ValueError("1:1 conversation requires a handle")
            if self.members is not None:
                raise ValueError("1:1 conversation must not have members")
        return self

    @model_validator(mode="after")
    def _needs_reply_requires_reason(self) -> "Conversation":
        # Scoped to summarized records: a skeleton (summary is None) may carry needs_reply
        # without a reason; once the LLM writes a summary, a needs_reply must justify itself.
        if self.summary is not None and self.needs_reply:
            if not (self.reply_reason and self.reply_reason.strip()):
                raise ValueError("summarized needs_reply record requires a non-empty reply_reason")
        return self


class State(_Base):
    generated_at: str
    watermark: Watermark
    unresponded: list[Unresponded] = []
    texts_today: TextsToday = Field(default_factory=TextsToday)
    conversations: list[Conversation] = []


def validate_state(
    data: dict,
    *,
    law: Optional[set[str]] = None,
    daily_cap: Optional[int] = None,
    weekly_cap: Optional[int] = None,
) -> State:
    """Validate a ``state.json`` dict into a :class:`State`. Raises ``ValidationError`` on any
    shape or cross-field violation. Pass ``law`` (active∪retired tag slugs) to enforce
    ``tags ⊆ law``; pass ``daily_cap``/``weekly_cap`` (from ``config.Config``) to override the
    default note-array caps. Omitted values fall back to the built-in defaults."""
    return State.model_validate(
        data, context={"law": law, "daily_cap": daily_cap, "weekly_cap": weekly_cap}
    )


def is_valid_state(
    data: dict,
    *,
    law: Optional[set[str]] = None,
    daily_cap: Optional[int] = None,
    weekly_cap: Optional[int] = None,
) -> bool:
    try:
        validate_state(data, law=law, daily_cap=daily_cap, weekly_cap=weekly_cap)
        return True
    except ValidationError:
        return False
