"""The model-call seam: one async interface, ``await summarize(system, user, *, model) -> str``.
Backends are ``litellm`` (any provider via API key) and ``agent_sdk`` (Anthropic Max), both
lazy-imported; a ``StubEngine`` keeps the summarizer fully testable with no LLM and no network."""
import asyncio
from types import SimpleNamespace

import pytest

from text_triage.config import Config
from text_triage.triage.engine import AgentSdkEngine, EngineError, LiteLLMEngine, StubEngine, make_engine


# ----------------------------------------------------------------- StubEngine
def test_stub_returns_sequence_and_records_system_user_model():
    e = StubEngine(['{"a":1}', '{"b":2}'])
    assert asyncio.run(e.summarize("s1", "u1", model="m1")) == '{"a":1}'
    assert asyncio.run(e.summarize("s2", "u2", model="m2")) == '{"b":2}'
    assert e.calls == [("s1", "u1", "m1"), ("s2", "u2", "m2")]


def test_stub_constant_string_repeats():
    e = StubEngine("X")
    assert asyncio.run(e.summarize("s", "a", model="m")) == "X"
    assert asyncio.run(e.summarize("s", "b", model="m")) == "X"


def test_stub_raises_an_exception_response():
    e = StubEngine([RuntimeError("boom")])
    with pytest.raises(RuntimeError):
        asyncio.run(e.summarize("s", "u", model="m"))


def test_stub_out_of_responses_raises_engine_error():
    e = StubEngine([])
    with pytest.raises(EngineError):
        asyncio.run(e.summarize("s", "u", model="m"))


# ----------------------------------------------------------------- LiteLLMEngine
def test_litellm_sends_system_and_user_messages_and_returns_text():
    seen = {}

    async def fake_acompletion(**kwargs):
        seen.update(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="HELLO"))])

    eng = LiteLLMEngine(acompletion=fake_acompletion)
    out = asyncio.run(eng.summarize("SYS", "do it", model="anthropic/claude-x"))
    assert out == "HELLO"
    assert seen["model"] == "anthropic/claude-x"
    assert seen["messages"] == [{"role": "system", "content": "SYS"},
                                {"role": "user", "content": "do it"}]


def test_litellm_bad_response_shape_raises_engine_error():
    async def fake_acompletion(**kwargs):
        return SimpleNamespace(choices=[])  # malformed: no message

    eng = LiteLLMEngine(acompletion=fake_acompletion)
    with pytest.raises(EngineError):
        asyncio.run(eng.summarize("s", "x", model="m"))


# ----------------------------------------------------------------- make_engine
def test_make_engine_litellm_is_the_default():
    assert isinstance(make_engine(Config()), LiteLLMEngine)


def test_make_engine_agent_sdk():
    eng = make_engine(Config(engine={"provider": "agent_sdk"}))
    assert isinstance(eng, AgentSdkEngine)
