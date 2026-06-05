"""Build each summary call's prompt from the markdown templates in the repo's ``agents/`` folder.

The window each agent sees is split into two clearly-separable, inspectable parts:

* **system** = ``agents/_global.md`` (the shared frame — mission, the golden rule, the tag rules +
  the ``${law}``, the output-JSON contract; identical for every agent in a run) + ``agents/<mode>.md``
  (this agent's role + its exact output keys). Steering lives in these files, not in code.
* **user** = ``agents/<mode>.user.md`` (this one conversation's data — name, the matrix-allowed memory
  layers, and the raw messages).

Templates use ``string.Template`` ``${placeholder}`` syntax, which leaves literal ``{ }`` alone (so a
JSON example in a prompt stays intact). Substitution is strict: a placeholder with no value raises,
catching template/code drift early. Put a literal ``$`` as ``$$``.
"""
from __future__ import annotations

import os
import string
from pathlib import Path
from typing import Optional, Union

__all__ = ["render", "build_system", "build_user", "discover_agents_dir"]


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
    return Path(__file__).resolve().parents[3] / "agents"  # src/text_triage/triage/prompts.py -> repo/agents


def render(name: str, mapping: dict, *, agents_dir: Optional[Union[str, Path]] = None) -> str:
    """Render ``agents/<name>.md`` by substituting ``${placeholders}`` from ``mapping`` (strict).

    Raises ``FileNotFoundError`` if the template is missing and ``KeyError`` if it names a placeholder
    absent from ``mapping`` — both real bugs, surfaced loudly. ``name`` may be a compound stem such as
    ``"daily.user"`` (→ ``agents/daily.user.md``)."""
    d = discover_agents_dir(agents_dir)
    template = (d / f"{name}.md").read_text(encoding="utf-8")
    return string.Template(template).substitute(mapping)


def build_system(mode: str, *, law: str, agents_dir: Optional[Union[str, Path]] = None) -> str:
    """The system prompt: the shared global frame (with the tag ``law`` filled in) + this agent's role.
    The global half is identical across all agents in a run (so it's cacheable + can't drift)."""
    glob = render("_global", {"law": law}, agents_dir=agents_dir)
    role = render(mode, {}, agents_dir=agents_dir)
    return f"{glob.rstrip()}\n\n{role.lstrip()}"


def build_user(mode: str, mapping: dict, *, agents_dir: Optional[Union[str, Path]] = None) -> str:
    """The user prompt: this one conversation's data (``agents/<mode>.user.md``)."""
    return render(f"{mode}.user", mapping, agents_dir=agents_dir)
