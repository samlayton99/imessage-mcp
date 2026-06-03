"""The model-call seam. One interface, ``summarize(prompt, *, model) -> str`` (PLAN "engine.py").
Step 0 ships the ``claude_code`` backend (headless ``claude -p``) plus a ``StubEngine`` so the
summarizer's logic is fully testable with no LLM, no network. The ``api_key`` SDK backend is a
later, thin same-interface adapter."""
import json

import pytest

from text_triage.config import Config
from text_triage.engine import ClaudeCodeEngine, EngineError, StubEngine, make_engine


def test_stub_returns_sequence_and_records_calls():
    e = StubEngine(['{"summary":"a"}', '{"summary":"b"}'])
    assert e.summarize("p1", model="m1") == '{"summary":"a"}'
    assert e.summarize("p2", model="m2") == '{"summary":"b"}'
    assert e.calls == [("p1", "m1"), ("p2", "m2")]


def test_stub_constant_string_repeats():
    e = StubEngine("X")
    assert e.summarize("a", model="m") == "X"
    assert e.summarize("b", model="m") == "X"


def test_stub_raises_an_exception_response():
    e = StubEngine([RuntimeError("boom")])
    with pytest.raises(RuntimeError):
        e.summarize("p", model="m")


def test_claude_code_builds_argv_and_parses_result():
    seen = {}

    def fake_run(argv):
        seen["argv"] = argv
        return json.dumps({"type": "result", "subtype": "success",
                           "result": "HELLO", "total_cost_usd": 0.01})

    out = ClaudeCodeEngine(run=fake_run).summarize("do it", model="claude-opus-4-8")
    assert out == "HELLO"
    argv = seen["argv"]
    assert argv[0] == "claude" and "-p" in argv and "do it" in argv
    assert "--model" in argv and "claude-opus-4-8" in argv
    assert "--output-format" in argv and "json" in argv


def test_claude_code_raises_on_error_envelope():
    def fake_run(argv):
        return json.dumps({"type": "result", "subtype": "error_during_execution",
                           "is_error": True, "result": ""})

    with pytest.raises(EngineError):
        ClaudeCodeEngine(run=fake_run).summarize("x", model="m")


def test_make_engine_claude_code_default():
    assert isinstance(make_engine(Config()), ClaudeCodeEngine)


def test_make_engine_api_key_not_yet():
    with pytest.raises(NotImplementedError):
        make_engine(Config(engine={"provider": "api_key"}))
