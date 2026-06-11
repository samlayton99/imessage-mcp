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

__all__ = ["TagSpec", "WatchDoc", "SYSTEM_LAW", "load_law", "load_watch", "full_law",
           "active_slugs", "effective_tags", "discover_watch_path"]

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
    # kind="choice" is a pick-one classification (choices = the fixed vocabulary); its value is a
    # typed Conversation field, never a slug stored in ``tags``. origin="system" entries are
    # hard-coded here — the future LLM interpreter may curate user tags but cannot touch them.
    kind: str = "freeform"      # "freeform" | "choice"
    choices: Optional[list[str]] = None
    origin: str = "user"        # "user" | "system"


# The hard-coded system law: tags/classifications the interpreter cannot change or drop, unioned
# with the watch.md user law by :func:`full_law`. ``reply_status``'s value lives in the typed
# ``Conversation.reply_status`` field (see schema.REPLY_STATUSES — kept in sync by a test).
SYSTEM_LAW: dict[str, TagSpec] = {
    "reply_status": TagSpec(
        slug="reply_status",
        description=("Always-present reply state. standby: the conversation is at a reasonable "
                     "stopping point. waiting_reply: the last substantive reply is the account "
                     "owner's (decays to standby after a configurable quiet period). "
                     "needs_response: the last substantive reply is the other person's."),
        lifetime="sticky",
        ttl_days=None,
        kind="choice",
        choices=["standby", "waiting_reply", "needs_response"],
        origin="system",
    ),
}


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


@dataclass(frozen=True)
class WatchDoc:
    """watch.md split into its three sections (each "" when absent). ``who_am_i`` feeds the shared
    prompt frame; ``what_to_watch`` holds the tag-law lines; ``what_i_care_about`` is reserved for
    the future interpreter."""
    who_am_i: str = ""
    what_to_watch: str = ""
    what_i_care_about: str = ""


_SECTION_FIELDS = {"who am i": "who_am_i", "what to watch": "what_to_watch",
                   "what i care about": "what_i_care_about"}


def load_watch(path: Optional[Union[str, Path]] = None) -> WatchDoc:
    """Read watch.md into a :class:`WatchDoc`. Sections are ``## <title>`` headers (case-insensitive,
    matching the three known titles); a sectionless file is treated as all "What to watch" — the
    pre-section format keeps working unchanged. Missing file → all empty. Never errors."""
    p = discover_watch_path(path)
    if p is None or not Path(p).exists():
        return WatchDoc()
    parts: dict[str, list[str]] = {f: [] for f in _SECTION_FIELDS.values()}
    current: Optional[str] = None
    saw_section = False
    for line in Path(p).read_text(encoding="utf-8").splitlines():
        title = line.strip().lstrip("#").strip().lower() if line.lstrip().startswith("##") else None
        if title in _SECTION_FIELDS:
            current = _SECTION_FIELDS[title]
            saw_section = True
            continue
        if current is not None:
            parts[current].append(line)
    if not saw_section:
        return WatchDoc(what_to_watch=Path(p).read_text(encoding="utf-8"))
    return WatchDoc(**{f: "\n".join(lines).strip() for f, lines in parts.items()})


def _parse_law_lines(text: str) -> dict[str, TagSpec]:
    """Each ``- <slug>: <description>`` line becomes a TagSpec; comment (``#``), blank, and malformed
    lines are skipped. First definition of a slug wins."""
    law: dict[str, TagSpec] = {}
    for line in text.splitlines():
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


def load_law(path: Optional[Union[str, Path]] = None) -> dict[str, TagSpec]:
    """Parse watch.md's "What to watch" section (or the whole file when sectionless) into
    ``{slug: TagSpec}``. A missing file yields an empty law."""
    return _parse_law_lines(load_watch(path).what_to_watch)


def full_law(user_law: dict[str, TagSpec]) -> dict[str, TagSpec]:
    """The law the MCP surface sees: the system law unioned with the watch.md user law. System
    entries win on a slug collision — user prose can never shadow or redefine them."""
    return {**user_law, **SYSTEM_LAW}


def active_slugs(law: dict[str, TagSpec]) -> set[str]:
    """The freeform slugs a ``tags`` assignment must be drawn from. Choice classifications are
    excluded: their values are typed Conversation fields, so a model emitting one as a tag slug
    is dropped/rejected instead of stored."""
    return {s for s, spec in law.items() if spec.kind == "freeform"}


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
    :class:`~text_triage.state.schema.Conversation`."""
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
