"""Compile ``watch.md`` (a free-form tag scratchpad) into the active tag law.

Step 0 is the deterministic half only: the whole file *is* the active law — each
``- <slug>: <description>`` line contributes one active tag. The summarizer proposes tags only from
this set and drops anything out-of-vocabulary; ``schema.py`` enforces ``tags ⊆ law`` on write/read.
The hash-gated LLM curator (add/retire only, malformed file keeps the last-good law) is Step 5; for
now a missing or malformed file simply yields whatever can be parsed — never an error.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional, Union

__all__ = ["load_law", "active_slugs", "discover_watch_path"]

# A slug is lowercase alphanumerics joined by single hyphens (e.g. ``needs-scheduling``).
_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
# A law line is ``- <slug>: <description>`` (leading/trailing whitespace tolerated).
_LINE_RE = re.compile(r"^\s*-\s*([^:]+?)\s*:\s*(.+?)\s*$")


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


def load_law(path: Optional[Union[str, Path]] = None) -> dict[str, str]:
    """Parse watch.md into ``{slug: description}`` for every valid ``- slug: description`` line.
    A missing file yields an empty law; comment (``#``), blank, and malformed lines are skipped."""
    p = discover_watch_path(path)
    if p is None or not Path(p).exists():
        return {}
    law: dict[str, str] = {}
    for line in Path(p).read_text(encoding="utf-8").splitlines():
        if line.lstrip().startswith("#"):
            continue
        m = _LINE_RE.match(line)
        if not m:
            continue
        slug, desc = m.group(1), m.group(2)
        if _SLUG_RE.match(slug):
            law.setdefault(slug, desc)
    return law


def active_slugs(law: dict[str, str]) -> set[str]:
    """The set of active tag slugs a tag assignment must be drawn from."""
    return set(law)
