"""The model-call seam (PLAN "Engine"): one async interface, two real backends.

``await Engine.summarize(system, user, *, model) -> str`` returns the model's raw text. ``summarize.py``
owns prompt assembly + schema validation; the engine only runs the model. The whole summarizer stays
testable with a :class:`StubEngine` (no LLM, no network), and the backend is a ``conditions.yaml``
choice (``engine.provider``):

* ``litellm`` (default) — any provider via API key (incl. the Claude API), through the battle-tested
  ``litellm`` unifier. Pay-per-use; keys come from the environment (``.env``).
* ``agent_sdk`` — Anthropic only, on a Claude **Max** plan's credit, via ``claude_agent_sdk`` (no API
  key; auth is the logged-in Claude session).

Both deps are imported lazily, so importing this module needs neither installed; tests inject fakes.
Everything is async; ``summarize.py`` fans out per-conversation calls in parallel.
"""
from __future__ import annotations

from typing import Callable, List, Optional, Protocol, Tuple, Union, runtime_checkable

from text_triage.config import Config

__all__ = ["Engine", "StubEngine", "LiteLLMEngine", "AgentSdkEngine", "EngineError", "make_engine"]


class EngineError(RuntimeError):
    """The backend ran but did not return a usable result."""


@runtime_checkable
class Engine(Protocol):
    async def summarize(self, system: str, user: str, *, model: str) -> str:
        """Run ``model`` on the (system, user) prompt pair and return its raw text output."""
        ...


class StubEngine:
    """A deterministic test engine. ``responses`` is either a single string returned for every call,
    or a list consumed in order (an item that is an Exception is raised). Records every
    ``(prompt, model)`` in :attr:`calls`."""

    def __init__(self, responses: Union[str, List[object]]):
        self._const = responses if isinstance(responses, str) else None
        self._queue = None if isinstance(responses, str) else list(responses)
        self.calls: List[Tuple[str, str, str]] = []

    async def summarize(self, system: str, user: str, *, model: str) -> str:
        self.calls.append((system, user, model))
        if self._const is not None:
            return self._const
        if not self._queue:
            raise EngineError("StubEngine: no more responses queued")
        item = self._queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class LiteLLMEngine:
    """Any-provider backend via ``litellm.acompletion`` (OpenAI-style messages; works with the Claude
    API, OpenAI, Gemini, local, …). ``acompletion`` is injectable for tests; the default lazy-imports
    it. ``model`` is the litellm ``<provider>/<model>`` string from ``engine.models``."""

    def __init__(self, *, acompletion: Optional[Callable] = None, max_tokens: int = 1024,
                 num_retries: int = 2):
        self._acompletion = acompletion
        self._max_tokens = max_tokens
        self._num_retries = num_retries

    async def summarize(self, system: str, user: str, *, model: str) -> str:
        acompletion = self._acompletion
        if acompletion is None:
            try:
                import litellm
            except ImportError as e:
                raise EngineError("engine.provider 'litellm' needs `pip install litellm`") from e
            acompletion = litellm.acompletion
        resp = await acompletion(
            model=model,
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": user}],
            max_tokens=self._max_tokens,
            num_retries=self._num_retries,
        )
        try:
            return resp.choices[0].message.content
        except (AttributeError, IndexError, KeyError, TypeError) as e:
            raise EngineError(f"litellm returned an unexpected response shape: {resp!r}") from e


class AgentSdkEngine:
    """Anthropic-Max backend via ``claude_agent_sdk`` — a single tool-less turn billed to the Claude
    subscription's Agent-SDK credit (no API key; auth is the logged-in session). Lazy-imported."""

    def __init__(self, *, max_turns: int = 1):
        self._max_turns = max_turns

    async def summarize(self, system: str, user: str, *, model: str) -> str:
        # VERIFY: option flags + message/text extraction against the installed claude_agent_sdk.
        try:
            from claude_agent_sdk import ClaudeAgentOptions, query
        except ImportError as e:
            raise EngineError("engine.provider 'agent_sdk' needs `pip install claude-agent-sdk`") from e
        options = ClaudeAgentOptions(system_prompt=system, model=model, allowed_tools=[],
                                     max_turns=self._max_turns)
        parts: List[str] = []
        async for message in query(prompt=user, options=options):
            for block in getattr(message, "content", None) or []:
                text = getattr(block, "text", None)
                if isinstance(text, str):
                    parts.append(text)
        out = "".join(parts).strip()
        if not out:
            raise EngineError("agent_sdk returned no text")
        return out


def make_engine(config: Config, *, acompletion: Optional[Callable] = None) -> Engine:
    """Build the engine named by ``config.engine.provider`` (``litellm`` default | ``agent_sdk``)."""
    provider = config.engine.provider
    if provider == "litellm":
        return LiteLLMEngine(acompletion=acompletion)
    if provider == "agent_sdk":
        return AgentSdkEngine()
    raise NotImplementedError(f"unknown engine.provider {provider!r}")
