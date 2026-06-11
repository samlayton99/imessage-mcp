"""Load and validate ``conditions.yaml`` — the deterministic steering surface (PLAN
"Steering lives in two files, never in code").

Defaults baked into these models equal the designed behavior, so a missing file or omitted key
falls back to the design. A malformed or invalid file raises :class:`ConfigError` loudly rather than
silently using stale defaults.
"""
from __future__ import annotations

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


# ── 1. text-message rules ─ what gets read & summarized ──────────────────────
class Messages(_CBase):
    """Extract windows + the conversation filter (one flat 'what to read' surface)."""
    weekly_days: int = Field(default=7, ge=1)
    monthly_days: int = Field(default=30, ge=1)
    # EXTRACTION lead-in: older msgs pulled in just before a window (NOT an LLM-input cap; see RawCaps).
    context_messages: int = Field(default=10, ge=0)
    unresponded_lookback_days: int = Field(default=90, ge=1)
    include_groups: bool = True
    named_only: bool = False
    min_handle_digits: int = Field(default=10, ge=0)
    # STORAGE filter: a conversation with fewer than this many ALL-TIME messages (tapbacks included)
    # never enters the server store. Below it is treated as spam / one-off; at/above it is mirrored.
    spam_floor: int = Field(default=1, ge=0)
    # How far back the collector's first/admission backfill reaches when mirroring a conversation's
    # full history to the server.
    backfill_years: int = Field(default=3, ge=1)
    # SUMMARIZE floor: daily skips the LLM call for a conversation with fewer than this many NEW
    # messages since its last summary (it rides in the raw layer until it accumulates). 0 = no gate.
    summarize_floor: int = Field(default=5, ge=0)
    # REPLY DECAY: a waiting_reply older than this many days presents as standby at query time
    # (nothing is owed once the thread goes quiet). Computed on read, never written. 0 = never decay.
    reply_decay_days: int = Field(default=7, ge=0)


# ── 2. models & billing ─ who runs each summary, and how you pay ─────────────
class ModelRoles(_CBase):
    daily: str = "anthropic/claude-sonnet-4-6"
    weekly: str = "anthropic/claude-opus-4-8"
    monthly: str = "anthropic/claude-opus-4-8"
    curator: str = "anthropic/claude-opus-4-8"


class RawCaps(_CBase):
    """LLM-INPUT cap: most raw msgs from a window packed into one summary call, per cadence; 0 = no cap.
    Distinct from Messages.context_messages (extraction lead-in) and Server.raw_store_days (server retention)."""
    daily: int = Field(default=0, ge=0)
    weekly: int = Field(default=0, ge=0)
    monthly: int = Field(default=0, ge=0)


class Engine(_CBase):
    # litellm = any provider via API key (incl. the Claude API); agent_sdk = Anthropic on a Claude Max plan.
    provider: Literal["litellm", "agent_sdk"] = "litellm"
    max_concurrency: int = Field(default=8, ge=1)
    models: ModelRoles = Field(default_factory=ModelRoles)
    max_raw_messages: RawCaps = Field(default_factory=RawCaps)


# ── 3. SERVER ─ where state.json is owned & served (VPS or an always-on Mac mini) ──
class Schedule(_CBase):
    timezone: str = "auto"  # auto = the Mac's system timezone
    daily: list[str] = ["on_open", "21:00"]
    weekly: list[str] = ["mon 03:00"]
    monthly: list[str] = ["1 03:00"]


class Live(_CBase):
    mode: Literal["interval", "watch"] = "interval"
    interval_seconds: int = Field(default=30, ge=1)


class Server(_CBase):
    # COLLECTOR knob: where the chat.db host pushes raw. blank = the local server on THIS machine
    #   (loopback / all-in-one Mac mini); a URL = a remote server, e.g. https://triage.yourhost.com.
    url: str = ""
    # SERVER knob: host:port the `serve` process listens on. 0.0.0.0 to expose on a LAN/VPS.
    bind: str = "127.0.0.1:8787"
    raw_store_days: int = Field(default=0, ge=0)   # SERVER RETENTION: how long raw text is kept on the server; 0 = forever
    # MCP default look-back: get_context / get_raw_history with no explicit `since` only return the last
    # N days (the deep store keeps everything; clients opt into older with an explicit since). 0 = no default.
    mcp_default_lookback_days: int = Field(default=60, ge=0)
    # Caps the conversations per UNATTENDED summary run (the scheduler + /trigger spawns) — set low
    # (e.g. 20) for cheap dev testing, including the one-time bootstrap monthly. 0 = uncapped.
    bootstrap_limit: int = Field(default=0, ge=0)
    # Live-raw cap: get_context derives each conversation's texts_today from the raw store at query
    # time (messages newer than its summary cursor); keep only the newest N. 0 = uncapped.
    texts_today_cap: int = Field(default=25, ge=0)
    schedule: Schedule = Field(default_factory=Schedule)
    live: Live = Field(default_factory=Live)


class Config(_CBase):
    messages: Messages = Field(default_factory=Messages)
    engine: Engine = Field(default_factory=Engine)
    server: Server = Field(default_factory=Server)


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
