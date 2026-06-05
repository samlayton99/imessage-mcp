"""The type contract for ``state.json``.

The validated record IS the product (PLAN "Non-negotiables"): every conversation is type-checked
(shape AND cross-field rules) on write and on MCP read, so consumers trust it blindly. The LLM
enriches prose fields; code owns correctness. Invalid records must never land.

Cross-field rules enforced here (PLAN "Hardening", as amended):
  - is_group => non-empty ``members`` and no ``handle``; else ``handle`` and no ``members``
  - ``identity`` <= 3 sentences; ``history`` / ``daily`` entries dated
  - ``tags`` subset of the law slugs (enforced only when a law is supplied via context)

There is no rolling ``summary`` and no ``daily``/``weekly`` array caps (memory is the layered notes
``identity`` + ``daily``/``weekly``/``monthly``/``history``; the cascade clears the lists). The live
raw layer ``texts_today`` lives ON each conversation record, not at the top level.
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
    "Conversation",
    "State",
    "validate_state",
    "is_valid_state",
    "ValidationError",
]


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
    """One raw, unsummarized message in a conversation's live ``texts_today`` layer."""

    message_rowid: int
    datetime: str
    sender: str
    text: str


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
    # The max message_rowid this conversation's last summary saw; the daily delta gate measures
    # "new since last summary" against it. 0 = never summarized.
    summarized_through: int = 0
    # Admitted to the store but never summarized yet (rides as raw only). Set deterministically from
    # the cursor: new_conversation == (summarized_through == 0).
    new_conversation: bool = False
    # --- live raw layer (code-owned; watcher pushes, the daily agent reads then clears) ---
    texts_today: list[TodayMessage] = []
    # --- agent-authored (blank in a skeleton record) ---
    identity: Optional[str] = None
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

    @field_validator("tags")
    @classmethod
    def _tags_subset_of_law(cls, v: list[str], info: ValidationInfo) -> list[str]:
        law = (info.context or {}).get("law") if info.context else None
        if law is not None:
            extra = set(v) - set(law)
            if extra:
                raise ValueError(f"tags not in the law: {sorted(extra)}")
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


class State(_Base):
    generated_at: str
    watermark: Watermark
    unresponded: list[Unresponded] = []
    conversations: list[Conversation] = []


def validate_state(data: dict, *, law: Optional[set[str]] = None) -> State:
    """Validate a ``state.json`` dict into a :class:`State`. Raises ``ValidationError`` on any shape
    or cross-field violation. Pass ``law`` (the active tag slugs) to enforce ``tags ⊆ law``; omit it
    and tags are unchecked."""
    return State.model_validate(data, context={"law": law})


def is_valid_state(data: dict, *, law: Optional[set[str]] = None) -> bool:
    try:
        validate_state(data, law=law)
        return True
    except ValidationError:
        return False
