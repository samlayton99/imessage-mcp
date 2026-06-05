"""Hard go/no-go acceptance invariants (PLAN "acceptance tests"), wired against the committed
synthetic fixture. Summary *quality* needs a human eye, but these correctness invariants must pass
automatically — never ship on "looks complete".

In scope for the deterministic core (milestone 1):
  1. Watermark never skips messages sharing a timestamp (ordered by (date, message_rowid)).
  2. state.json is written temp -> validate -> rename (never a partial file).
  3. An invalid record is rejected and never lands in state.json.
  4. needs_reply is true only when the deterministic gate allows it.
  5. The unresponded heuristic is reproduced (stale 1:1, last from them, real exchange).

Out of scope (later steps): idempotent SQLite ingest, texts_today clearing, dormant-not-deleted,
and the MCP get_context end-to-end check.
"""
import json
from pathlib import Path

import pytest

from text_triage.state import state_io
from text_triage.state.schema import ValidationError
from text_triage.triage.skeleton import build_skeleton, needs_reply_gate

FIXTURES = Path(__file__).parent / "fixtures"


def load_export():
    return json.loads((FIXTURES / "synthetic_export.json").read_text())


# 1 -------------------------------------------------------------------------------
def test_watermark_orders_by_date_then_rowid():
    export = load_export()
    wm = export["watermark"]
    rowids_at_max = [
        m["message_rowid"]
        for c in export["conversations"]
        for m in c["conversation"]
        if m["date"] == wm["max_date_raw"]
    ]
    assert len(rowids_at_max) >= 2, "fixture must contain a same-timestamp cluster"
    assert wm["max_message_rowid"] == max(rowids_at_max)  # highest rowid at the max date, not date alone


# 2 -------------------------------------------------------------------------------
def test_state_written_atomically(tmp_path, monkeypatch):
    state = build_skeleton(load_export())
    path = tmp_path / "state.json"
    monkeypatch.setattr(
        state_io.os, "replace", lambda *_: (_ for _ in ()).throw(OSError("crash mid-rename"))
    )
    with pytest.raises(OSError):
        state_io.write_state(state, path)
    assert not path.exists()
    assert not any(p.name.endswith(".tmp") for p in tmp_path.iterdir())


# 3 -------------------------------------------------------------------------------
def test_invalid_record_never_lands(tmp_path):
    data = json.loads(build_skeleton(load_export()).model_dump_json())
    data["conversations"][0]["is_group"] = True  # 1:1 -> group with a handle & no members = invalid
    path = tmp_path / "state.json"
    with pytest.raises(ValidationError):
        state_io.write_state(data, path)
    assert not path.exists()


# 4 -------------------------------------------------------------------------------
def test_needs_reply_only_when_gate_allows():
    state = build_skeleton(load_export())
    for c in state.conversations:
        responded = c.last_from == "me"
        assert c.needs_reply is needs_reply_gate(is_group=c.is_group, responded=responded)


# 5 -------------------------------------------------------------------------------
def test_unresponded_heuristic_reproduced():
    state = build_skeleton(load_export())
    names = {u.name for u in state.unresponded}
    assert "Casey Vale" in names
    casey = next(u for u in state.unresponded if u.name == "Casey Vale")
    assert 30 <= casey.days_waiting < 90  # stale: outside the 30d window, inside the 90d lookback
