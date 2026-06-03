"""The model-call seam (PLAN "engine.py"): one interface, two-plus backends.

``Engine.summarize(prompt, *, model) -> str`` returns the model's raw text. ``summarize.py`` owns
prompt assembly and schema validation; the engine only runs the model. This keeps the whole
summarizer testable with a :class:`StubEngine` (no LLM, no network) and makes the backend a
``conditions.yaml`` choice (``engine.provider``).

Step 0 ships ``claude_code`` (headless ``claude -p --output-format json``, authed by a long-lived
OAuth token on the host). The ``api_key`` Anthropic-SDK backend is a later thin adapter behind this
same interface.
"""
from __future__ import annotations

import json
import subprocess
from typing import Callable, List, Optional, Protocol, Tuple, Union, runtime_checkable

from text_triage.config import Config

__all__ = ["Engine", "StubEngine", "ClaudeCodeEngine", "EngineError", "make_engine"]


class EngineError(RuntimeError):
    """The backend ran but did not return a usable result (non-zero exit, error envelope)."""


@runtime_checkable
class Engine(Protocol):
    def summarize(self, prompt: str, *, model: str) -> str:
        """Run ``model`` on ``prompt`` and return its raw text output."""
        ...


class StubEngine:
    """A deterministic test engine. ``responses`` is either a single string returned for every
    call, or a list consumed in order (an item that is an Exception is raised). Records every
    ``(prompt, model)`` in :attr:`calls`."""

    def __init__(self, responses: Union[str, List[object]]):
        self._const = responses if isinstance(responses, str) else None
        self._queue = None if isinstance(responses, str) else list(responses)
        self.calls: List[Tuple[str, str]] = []

    def summarize(self, prompt: str, *, model: str) -> str:
        self.calls.append((prompt, model))
        if self._const is not None:
            return self._const
        if not self._queue:
            raise EngineError("StubEngine: no more responses queued")
        item = self._queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class ClaudeCodeEngine:
    """Headless Claude Code backend: ``claude -p <prompt> --output-format json --model <model>``.

    ``run`` (injected for tests) takes the argv list and returns the process stdout; the default
    shells out via :mod:`subprocess`. The JSON envelope's ``result`` field is the model's text."""

    def __init__(self, run: Optional[Callable[[List[str]], str]] = None):
        self._run = run or _default_run

    def summarize(self, prompt: str, *, model: str) -> str:
        argv = ["claude", "-p", prompt, "--output-format", "json", "--model", model]
        try:
            stdout = self._run(argv)
        except subprocess.CalledProcessError as e:
            raise EngineError(f"claude -p exited {e.returncode}: {e.stderr}") from e
        try:
            env = json.loads(stdout)
        except json.JSONDecodeError as e:
            raise EngineError(f"claude -p returned non-JSON: {stdout[:200]!r}") from e
        if env.get("is_error") or env.get("subtype") not in (None, "success"):
            raise EngineError(f"claude -p error envelope: {env.get('subtype')!r}")
        return env.get("result", "")


def _default_run(argv: List[str]) -> str:
    proc = subprocess.run(argv, capture_output=True, text=True, check=True)
    return proc.stdout


def make_engine(config: Config, *, run: Optional[Callable[[List[str]], str]] = None) -> Engine:
    """Build the engine named by ``config.engine.provider``."""
    provider = config.engine.provider
    if provider == "claude_code":
        return ClaudeCodeEngine(run=run)
    if provider == "api_key":
        raise NotImplementedError(
            "engine.provider 'api_key' is a later step; use 'claude_code' for now"
        )
    raise NotImplementedError(f"unknown engine.provider {provider!r}")
