"""Atomic, validated state IO. state.json is the product: it is written
temp -> validate -> fsync -> rename, never partially, and invalid records never land."""
import json

import pytest

from text_triage.state import state_io
from text_triage.state.schema import State, ValidationError, validate_state


def good_state_dict():
    return {
        "generated_at": "2026-06-02 14:32",
        "watermark": {"max_date_raw": 802345678901234000, "max_message_rowid": 987654},
        "conversations": [
            {
                "chat_rowid": 123,
                "name": "Andrew Marks",
                "is_group": False,
                "handle": "+15184105257",
                "members": None,
                "status": "active",
                "last_from": "them",
                "last_message_at": "2026-05-31 14:02",
                "needs_reply": True,
            }
        ],
    }


# --------------------------------------------------------------------- round-trip
def test_write_then_read_round_trips(tmp_path):
    path = tmp_path / "state.json"
    state = validate_state(good_state_dict())
    state_io.write_state(state, path)
    loaded = state_io.read_state(path)
    assert isinstance(loaded, State)
    assert loaded.conversations[0].chat_rowid == 123
    assert loaded.watermark.max_message_rowid == 987654


def test_write_accepts_a_plain_dict(tmp_path):
    path = tmp_path / "state.json"
    state_io.write_state(good_state_dict(), path)
    assert state_io.read_state(path).generated_at == "2026-06-02 14:32"


def test_written_file_is_valid_json_on_disk(tmp_path):
    path = tmp_path / "state.json"
    state_io.write_state(good_state_dict(), path)
    on_disk = json.loads(path.read_text())
    assert on_disk["conversations"][0]["name"] == "Andrew Marks"


# ------------------------------------------------------- atomicity (temp->rename)
def test_failed_rename_leaves_no_partial_and_no_temp(tmp_path, monkeypatch):
    path = tmp_path / "state.json"

    def boom(_src, _dst):
        raise OSError("simulated crash during rename")

    monkeypatch.setattr(state_io.os, "replace", boom)
    with pytest.raises(OSError):
        state_io.write_state(good_state_dict(), path)

    assert not path.exists()  # target never created
    leftovers = [p.name for p in tmp_path.iterdir()]
    assert not any(n.endswith(".tmp") for n in leftovers), f"temp left behind: {leftovers}"


def test_failed_rename_keeps_previous_file_intact(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    state_io.write_state(good_state_dict(), path)  # first good write
    original = path.read_text()

    monkeypatch.setattr(state_io.os, "replace", lambda _s, _d: (_ for _ in ()).throw(OSError("boom")))
    newer = good_state_dict()
    newer["generated_at"] = "2099-01-01 00:00"
    with pytest.raises(OSError):
        state_io.write_state(newer, path)

    assert path.read_text() == original  # untouched


# ----------------------------------------------------- invalid never lands
def test_invalid_record_is_rejected_before_any_file_appears(tmp_path):
    path = tmp_path / "state.json"
    bad = good_state_dict()
    bad["conversations"][0]["is_group"] = True  # group with handle + no members => invalid
    with pytest.raises(ValidationError):
        state_io.write_state(bad, path)
    assert not path.exists()


def test_write_enforces_tag_law(tmp_path):
    path = tmp_path / "state.json"
    data = good_state_dict()
    data["conversations"][0]["tags"] = ["mystery"]
    with pytest.raises(ValidationError):
        state_io.write_state(data, path, law={"needs-scheduling"})
    assert not path.exists()


def test_read_validates_and_rejects_corrupt_state(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"generated_at": "x"}))  # missing watermark
    with pytest.raises(ValidationError):
        state_io.read_state(path)


# ------------------------------------------------------------------ lock
def test_lock_excludes_second_holder(tmp_path):
    path = tmp_path / "state.json"
    with state_io.state_lock(path):
        with pytest.raises(state_io.StateLockedError):
            with state_io.state_lock(path):
                pass


def test_lock_is_released_after_context(tmp_path):
    path = tmp_path / "state.json"
    with state_io.state_lock(path):
        pass
    with state_io.state_lock(path):  # re-acquire fine
        pass
