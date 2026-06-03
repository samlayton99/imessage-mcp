"""Load and validate ``conditions.yaml`` — the deterministic steering surface (PLAN
"Steering lives in two files, never in code").

Defaults baked into these models equal the designed behavior, so a missing file or omitted key
falls back to the design. The note-array caps the schema enforces are DERIVED here from the window
sizes (``daily_cap = weekly_days``; ``weekly_cap = ceil(monthly_days / 7)``) so changing a window
in the yaml can never conflict with a hardcoded cap. A malformed or invalid file raises
:class:`ConfigError` loudly rather than silently using stale defaults.
"""
from __future__ import annotations

import math
import os
from pathlib import Path
from typing import Literal, Optional, Union

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

__all__ = ["Config", "ConfigError", "load_config"]


class ConfigError(ValueError):
    """Raised when conditions.yaml is present but malformed or invalid."""


class _CBase(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Windows(_CBase):
    weekly_days: int = Field(default=7, ge=1)
    monthly_days: int = Field(default=30, ge=1)
    context_messages: int = Field(default=10, ge=0)
    raw_store_days: int = Field(default=30, ge=0)  # 0 = keep raw forever (no pruning on the VPS)
    unresponded_lookback_days: int = Field(default=90, ge=1)


class ConversationFilter(_CBase):
    include_groups: bool = True
    named_only: bool = False
    min_handle_digits: int = Field(default=10, ge=0)
    min_messages: int = Field(default=1, ge=0)


class ModelRoles(_CBase):
    daily: str = "claude-sonnet-4-6"
    weekly: str = "claude-sonnet-4-6"
    monthly: str = "claude-opus-4-8"
    curator: str = "claude-sonnet-4-6"


class Engine(_CBase):
    provider: Literal["claude_code", "api_key"] = "claude_code"


class Live(_CBase):
    mode: Literal["interval", "watch"] = "interval"
    interval_seconds: int = Field(default=30, ge=1)


class Schedule(_CBase):
    timezone: str = "auto"  # auto = the Mac's system timezone
    daily: list[str] = ["on_open", "21:00"]
    weekly: list[str] = ["mon 04:00"]
    monthly: list[str] = ["1 04:00"]


class Tags(_CBase):
    auto_apply: bool = True


class Config(_CBase):
    windows: Windows = Field(default_factory=Windows)
    conversation_filter: ConversationFilter = Field(default_factory=ConversationFilter)
    models: ModelRoles = Field(default_factory=ModelRoles)
    engine: Engine = Field(default_factory=Engine)
    live: Live = Field(default_factory=Live)
    schedule: Schedule = Field(default_factory=Schedule)
    tags: Tags = Field(default_factory=Tags)

    @property
    def daily_cap(self) -> int:
        """Max retained daily notes = days between weekly clears."""
        return self.windows.weekly_days

    @property
    def weekly_cap(self) -> int:
        """Max retained weekly notes = weeks between monthly resets."""
        return math.ceil(self.windows.monthly_days / 7)


def _discover_path(explicit: Optional[Union[str, Path]]) -> Optional[Path]:
    if explicit is not None:
        return Path(explicit)
    env = os.environ.get("TEXT_TRIAGE_CONFIG")
    if env:
        return Path(env)
    for cand in (Path.cwd() / "conditions.yaml", Path.home() / ".text-triage" / "conditions.yaml"):
        if cand.exists():
            return cand
    return None


def load_config(path: Optional[Union[str, Path]] = None) -> Config:
    """Load conditions.yaml into a validated :class:`Config`.

    Discovery when ``path`` is omitted: ``$TEXT_TRIAGE_CONFIG`` → ``./conditions.yaml`` →
    ``~/.text-triage/conditions.yaml`` → built-in defaults. An explicitly-passed path that does not
    exist is an error; a present-but-malformed file raises :class:`ConfigError`.
    """
    p = _discover_path(path)
    if p is None:
        return Config()  # nothing discoverable -> designed defaults
    if not p.exists():
        if path is not None:
            raise ConfigError(f"config file not found: {p}")
        return Config()
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"invalid YAML in {p}: {e}") from e
    if not isinstance(raw, dict):
        raise ConfigError(f"config root must be a mapping, got {type(raw).__name__} in {p}")
    try:
        return Config.model_validate(raw)
    except ValidationError as e:
        raise ConfigError(f"invalid config in {p}:\n{e}") from e
