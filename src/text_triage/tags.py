"""Compile ``watch.md`` (a free-form tag scratchpad) into the active tag law, with **lifetimes**.

Each ``- <slug>: <description>`` line becomes a :class:`TagSpec` whose lifetime is inferred from the
prose: an explicit ``N days`` → ``ttl`` N; words like sticky/indefinite/permanent/always → ``sticky``;
temporary/expires (with no number) → ``ttl`` 14; otherwise ``sticky``. Stored ``Conversation.tags``
stay plain slugs; :func:`effective_tags` computes which are currently relevant at QUERY time
(``sticky`` always; ``ttl`` only while the conversation's ``last_message_at`` is within ``ttl_days``),
so stale time-tags hide themselves without any agent deleting them.

Deterministic only — the hash-gated LLM curator that *refines* this law is a later step; a missing or
malformed file yields whatever can be parsed, never an error.
"""
from __future__ import annotations

import datetime
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Union

__all__ = ["TagSpec", "load_law", "active_slugs", "effective_tags", "discover_watch_path"]

# A slug is lowercase alphanumerics joined by single hyphens (e.g. ``needs-scheduling``).
_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
# A law line is ``- <slug>: <description>`` (leading/trailing whitespace tolerated).
_LINE_RE = re.compile(r"^\s*-\s*([^:]+?)\s*:\s*(.+?)\s*$")

_DAYS_RE = re.compile(r"(\d+)\s*day")
_STICKY_KW = ("sticky", "indefinite", "permanent", "always")
_TEMP_KW = ("temporary", "temp", "expire", "expires", "expiring", "time-sensitive", "short-lived")
_DEFAULT_TEMP_DAYS = 14


@dataclass(frozen=True)
class TagSpec:
    slug: str
    description: str
    lifetime: str               # "sticky" | "ttl"
    ttl_days: Optional[int]     # set iff lifetime == "ttl"


def _infer_lifetime(description: str) -> tuple[str, Optional[int]]:
    """sticky | ttl(N) from the prose. Explicit ``N day(s)`` wins; then sticky keywords; then a
    temporary/expires hint with no number → ttl 14; otherwise sticky."""
    low = description.lower()
    m = _DAYS_RE.search(low)
    if m:
        return "ttl", int(m.group(1))
    if any(k in low for k in _STICKY_KW):
        return "sticky", None
    if any(k in low for k in _TEMP_KW):
        return "ttl", _DEFAULT_TEMP_DAYS
    return "sticky", None


def discover_watch_path(explicit: Optional[Union[str, Path]]) -> Optional[Path]:
    """Resolve the watch.md path: explicit → ``$TEXT_TRIAGE_WATCH`` → ``./watch.md`` →
    ``~/.text-triage/watch.md`` → ``None``. Mirrors :func:`config.load_config` discovery."""
    if explicit is not None:
        return Path(explicit)
    env = os.environ.get("TEXT_TRIAGE_WATCH")
    if env:
        return Path(env)
    for cand in (Path.cwd() / "watch.md", Path.home() / ".text-triage" / "watch.md"):
        if cand.exists():
            return cand
    return None


def load_law(path: Optional[Union[str, Path]] = None) -> dict[str, TagSpec]:
    """Parse watch.md into ``{slug: TagSpec}``. A missing file yields an empty law; comment (``#``),
    blank, and malformed lines are skipped. First definition of a slug wins."""
    p = discover_watch_path(path)
    if p is None or not Path(p).exists():
        return {}
    law: dict[str, TagSpec] = {}
    for line in Path(p).read_text(encoding="utf-8").splitlines():
        if line.lstrip().startswith("#"):
            continue
        m = _LINE_RE.match(line)
        if not m:
            continue
        slug, desc = m.group(1), m.group(2)
        if _SLUG_RE.match(slug) and slug not in law:
            lifetime, ttl_days = _infer_lifetime(desc)
            law[slug] = TagSpec(slug=slug, description=desc, lifetime=lifetime, ttl_days=ttl_days)
    return law


def active_slugs(law: dict[str, TagSpec]) -> set[str]:
    """The set of active tag slugs a tag assignment must be drawn from."""
    return set(law)


def _field(conv, attr: str):
    return conv.get(attr) if isinstance(conv, dict) else getattr(conv, attr, None)


def _parse_dt(s: Optional[str]) -> Optional[datetime.datetime]:
    if not s:
        return None
    try:
        return datetime.datetime.fromisoformat(s)  # handles "YYYY-MM-DD HH:MM:SS" on 3.11+
    except (ValueError, TypeError):
        return None


def effective_tags(conversation, law: dict[str, TagSpec], *,
                   as_of: Optional[datetime.datetime] = None) -> list[str]:
    """The currently-relevant subset of a conversation's ``tags`` (order preserved).

    ``sticky`` tags are always effective; ``ttl`` tags only while ``last_message_at`` is within
    ``ttl_days`` of ``as_of`` (default now). A tag not in the law, or an unparseable
    ``last_message_at``, is kept (lenient — never hide on uncertainty). Accepts a dict or a
    :class:`~text_triage.schema.Conversation`."""
    tags = _field(conversation, "tags") or []
    if as_of is None:
        as_of = datetime.datetime.now()
    last_dt = _parse_dt(_field(conversation, "last_message_at"))
    out = []
    for t in tags:
        spec = law.get(t)
        if spec is None or spec.lifetime != "ttl" or last_dt is None:
            out.append(t)                                   # not-in-law / sticky / undated → keep
            continue
        if (as_of - last_dt).total_seconds() / 86400.0 <= spec.ttl_days:
            out.append(t)
    return out
