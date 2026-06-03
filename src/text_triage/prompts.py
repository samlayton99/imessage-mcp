"""Load and fill the per-agent prompt templates that live in the repo's ``agents/`` folder.

Each summarizer mode (``daily``, ``weekly``, ``monthly``, the curator later) has a markdown template
at ``agents/<mode>.md``. Prompt *content* is steering — it lives in those files, not in code; edit a
template, not a function. Templates use ``string.Template`` ``${placeholder}`` syntax, which leaves
literal ``{ }`` alone (so the JSON example in a prompt stays intact). Substitution is strict: a
placeholder with no value raises, catching template/code drift early. Put a literal ``$`` as ``$$``.

``${golden_rule}`` is a shared partial auto-injected into every render from ``agents/_golden_rule.md``
(the "never assume" rule that governs all summary agents) — defined once, so it can't drift between
modes. A template that omits the placeholder simply doesn't use it.
"""
from __future__ import annotations

import os
import string
from pathlib import Path
from typing import Optional, Union

__all__ = ["render", "discover_agents_dir"]


def discover_agents_dir(explicit: Optional[Union[str, Path]] = None) -> Path:
    """Resolve the agents/ dir: explicit → ``$TEXT_TRIAGE_AGENTS`` → ``./agents`` → packaged default
    (``<repo>/agents`` next to ``src/``). Mirrors :func:`config.load_config` discovery."""
    if explicit is not None:
        return Path(explicit)
    env = os.environ.get("TEXT_TRIAGE_AGENTS")
    if env:
        return Path(env)
    cwd = Path.cwd() / "agents"
    if cwd.is_dir():
        return cwd
    return Path(__file__).resolve().parents[2] / "agents"  # src/text_triage/prompts.py -> repo/agents


def render(mode: str, mapping: dict, *, agents_dir: Optional[Union[str, Path]] = None) -> str:
    """Render ``agents/<mode>.md`` by substituting ``${placeholders}`` from ``mapping``.

    Raises ``FileNotFoundError`` if the template is missing and ``KeyError`` if the template names a
    placeholder absent from ``mapping`` (both are real bugs, surfaced loudly). ``${golden_rule}`` is
    always available (from ``agents/_golden_rule.md``); ``mapping`` overrides it if it sets the key."""
    d = discover_agents_dir(agents_dir)
    template = (d / f"{mode}.md").read_text(encoding="utf-8")
    full = {"golden_rule": _golden_rule(d), **mapping}
    return string.Template(template).substitute(full)


def _golden_rule(agents_dir: Path) -> str:
    """The shared 'never assume' rule, injected as ``${golden_rule}`` into every summary template."""
    p = agents_dir / "_golden_rule.md"
    return p.read_text(encoding="utf-8").strip() if p.exists() else ""
