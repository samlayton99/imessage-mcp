"""Type contract for state.json (schema.py). The validated record IS the product:
every Hardening rule from PLAN must reject a bad record and pass a good one."""
import pytest
from pydantic import ValidationError

from text_triage.state.schema import State, Conversation, validate_state, is_valid_state


def valid_conv(**overrides):
    """A minimal valid 1:1 SKELETON record (no LLM fields set)."""
    base = {
        "chat_rowid": 123,
        "name": "Andrew Marks",
        "is_group": False,
        "handle": "+15184105257",
        "members": None,
        "status": "active",
        "last_from": "them",
        "last_message_at": "2026-05-31 14:02",
        "reply_status": "standby",
    }
    base.update(overrides)
    return base


def valid_state(convs=None, **overrides):
    base = {
        "generated_at": "2026-06-02 14:32",
        "watermark": {"max_date_raw": 802345678901234000, "max_message_rowid": 987654},
        "conversations": convs if convs is not None else [valid_conv()],
    }
    base.update(overrides)
    return base


# ----------------------------------------------------------------- happy paths
def test_minimal_skeleton_record_is_valid():
    Conversation.model_validate(valid_conv())


def test_full_state_round_trips():
    s = validate_state(valid_state())
    assert isinstance(s, State)
    assert s.conversations[0].chat_rowid == 123
    assert s.watermark.max_message_rowid == 987654


def test_skeleton_leaves_llm_fields_empty_by_default():
    c = Conversation.model_validate(valid_conv())
    assert c.identity is None and c.texts_today == []
    assert c.tags == [] and c.daily == [] and c.weekly == [] and c.history == []
    assert c.monthly is None and c.edited == {}


def test_texts_today_on_conversation_and_not_top_level():
    assert Conversation.model_validate(valid_conv(texts_today=[])).texts_today == []
    with pytest.raises(ValidationError):              # no top-level texts_today anymore
        validate_state(valid_state(texts_today={"since": None, "conversations": {}}))


# ----------------------------------------------------------- is_group / handle
def test_group_requires_members_not_handle():
    c = Conversation.model_validate(
        valid_conv(is_group=True, handle=None, members=["A", "B"], last_from="me")
    )
    assert c.members == ["A", "B"]


def test_group_with_handle_set_is_invalid():
    with pytest.raises(ValidationError):
        Conversation.model_validate(valid_conv(is_group=True, members=["A", "B"]))  # handle still set


def test_group_without_members_is_invalid():
    with pytest.raises(ValidationError):
        Conversation.model_validate(valid_conv(is_group=True, handle=None, members=None))


def test_one_to_one_with_members_is_invalid():
    with pytest.raises(ValidationError):
        Conversation.model_validate(valid_conv(members=["A"]))


def test_one_to_one_without_handle_is_invalid():
    with pytest.raises(ValidationError):
        Conversation.model_validate(valid_conv(handle=None))


# ------------------------------------------------------------- identity length
def test_identity_three_sentences_is_valid():
    Conversation.model_validate(valid_conv(identity="One. Two. Three."))


def test_identity_over_three_sentences_is_invalid():
    with pytest.raises(ValidationError):
        Conversation.model_validate(valid_conv(identity="One. Two. Three. Four."))


# ------------------------------------------------------------------- history dated
def test_history_iso_date_valid():
    Conversation.model_validate(valid_conv(history=[{"date": "2026-05-01", "text": "reconnected"}]))


def test_history_not_enough_context_sentinel_valid():
    Conversation.model_validate(valid_conv(history=[{"date": "not enough context", "text": ""}]))


def test_history_undated_is_invalid():
    with pytest.raises(ValidationError):
        Conversation.model_validate(valid_conv(history=[{"date": "last spring", "text": "x"}]))


# --------------------------------------------------------------- tags subset of law
def test_tags_in_law_valid():
    validate_state(
        valid_state([valid_conv(tags=["needs-scheduling"])]),
        law={"needs-scheduling", "owe-money"},
    )


def test_tags_outside_law_invalid():
    with pytest.raises(ValidationError):
        validate_state(
            valid_state([valid_conv(tags=["mystery-tag"])]),
            law={"needs-scheduling"},
        )


def test_empty_tags_always_valid_even_without_law():
    validate_state(valid_state())  # no law passed, tags default []


# ----------------------------------------------------------------- reply_status
def test_reply_status_defaults_to_standby():
    conv = valid_conv()
    conv.pop("reply_status")
    assert Conversation.model_validate(conv).reply_status == "standby"


def test_reply_status_accepts_the_three_states():
    for v in ("standby", "waiting_reply", "needs_response"):
        assert Conversation.model_validate(valid_conv(reply_status=v)).reply_status == v


def test_reply_status_rejects_out_of_vocabulary():
    with pytest.raises(ValidationError):
        Conversation.model_validate(valid_conv(reply_status="shouting"))


def test_legacy_needs_reply_true_migrates_to_needs_response():
    conv = valid_conv(needs_reply=True)
    conv.pop("reply_status")
    c = Conversation.model_validate(conv)
    assert c.reply_status == "needs_response"
    assert "needs_reply" not in c.model_dump()


def test_legacy_needs_reply_false_migrates_to_standby():
    conv = valid_conv(needs_reply=False)
    conv.pop("reply_status")
    assert Conversation.model_validate(conv).reply_status == "standby"


def test_legacy_needs_reply_does_not_clobber_explicit_reply_status():
    # A half-migrated record (both fields) keeps the new field; the legacy one is just dropped.
    c = Conversation.model_validate(valid_conv(needs_reply=True, reply_status="standby"))
    assert c.reply_status == "standby"


# ----------------------------------------------------- summary + reply metadata
def test_summary_and_reply_metadata_default_to_none():
    c = Conversation.model_validate(valid_conv())
    assert c.summary is None and c.last_from_me_at is None and c.last_from_them_at is None


def test_summary_and_reply_metadata_round_trip():
    c = Conversation.model_validate(valid_conv(
        summary="Planning his July move; owes you a date for the 28th.",
        last_from_me_at="2026-05-30 09:00", last_from_them_at="2026-05-31 14:02"))
    assert c.summary.startswith("Planning") and c.last_from_me_at == "2026-05-30 09:00"


# ------------------------------------------------------------------ extra forbidden
def test_unknown_field_is_rejected():
    with pytest.raises(ValidationError):
        Conversation.model_validate(valid_conv(bogus="x"))


# ------------------------------------------------------------------ is_valid_state
def test_is_valid_state_bool():
    assert is_valid_state(valid_state()) is True
    assert is_valid_state({"generated_at": "x"}) is False
