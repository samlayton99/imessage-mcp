"""Type contract for state.json (schema.py). The validated record IS the product:
every Hardening rule from PLAN must reject a bad record and pass a good one."""
import pytest
from pydantic import ValidationError

from text_triage.schema import State, Conversation, validate_state, is_valid_state


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
        "needs_reply": False,
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
    assert c.identity is None and c.summary is None and c.reply_reason is None
    assert c.tags == [] and c.daily == [] and c.weekly == [] and c.history == []
    assert c.monthly is None and c.edited == {}


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


# ----------------------------------------------------------------- daily/weekly caps
def test_daily_at_cap_valid_over_cap_invalid():
    seven = [{"date": "2026-05-%02d" % (i + 1), "text": "n"} for i in range(7)]
    Conversation.model_validate(valid_conv(summary="s", daily=seven))
    with pytest.raises(ValidationError):
        Conversation.model_validate(valid_conv(summary="s", daily=seven + [{"date": "2026-05-08", "text": "n"}]))


def test_weekly_over_cap_invalid():
    six = [{"week_of": "2026-05-%02d" % (i + 1), "text": "n"} for i in range(6)]
    with pytest.raises(ValidationError):
        Conversation.model_validate(valid_conv(summary="s", weekly=six))


# ------------------------------------------------------------------- history dated
def test_history_iso_date_valid():
    Conversation.model_validate(valid_conv(history=[{"date": "2026-05-01", "text": "reconnected"}]))


def test_history_not_enough_context_sentinel_valid():
    Conversation.model_validate(valid_conv(history=[{"date": "not enough context", "text": ""}]))


def test_history_undated_is_invalid():
    with pytest.raises(ValidationError):
        Conversation.model_validate(valid_conv(history=[{"date": "last spring", "text": "x"}]))


# ----------------------------------------------------- needs_reply => reply_reason (scoped)
def test_skeleton_needs_reply_without_reason_is_valid():
    # summary is None => rule is vacuous; deterministic needs_reply may be True with no reason.
    Conversation.model_validate(valid_conv(needs_reply=True, summary=None, reply_reason=None))


def test_summarized_needs_reply_without_reason_is_invalid():
    with pytest.raises(ValidationError):
        Conversation.model_validate(valid_conv(needs_reply=True, summary="A real summary.", reply_reason=None))


def test_summarized_needs_reply_with_reason_is_valid():
    Conversation.model_validate(
        valid_conv(needs_reply=True, summary="A real summary.", reply_reason="He asked a direct question.")
    )


# --------------------------------------------------------------- tags subset of law
def test_tags_in_law_valid():
    validate_state(
        valid_state([valid_conv(summary="s", tags=["needs-scheduling"])]),
        law={"needs-scheduling", "owe-money"},
    )


def test_tags_outside_law_invalid():
    with pytest.raises(ValidationError):
        validate_state(
            valid_state([valid_conv(summary="s", tags=["mystery-tag"])]),
            law={"needs-scheduling"},
        )


def test_empty_tags_always_valid_even_without_law():
    validate_state(valid_state())  # no law passed, tags default []


# ----------------------------------------------- caps derived from config (context-injected)
def test_daily_cap_defaults_to_7_but_is_raisable():
    nine = [{"date": "2026-05-%02d" % (i + 1), "text": "n"} for i in range(9)]
    with pytest.raises(ValidationError):  # default cap 7
        validate_state(valid_state([valid_conv(summary="s", daily=nine)]))
    validate_state(valid_state([valid_conv(summary="s", daily=nine)]), daily_cap=10)  # config raised it


def test_weekly_cap_defaults_to_5_but_is_raisable():
    seven = [{"week_of": "2026-05-%02d" % (i + 1), "text": "n"} for i in range(7)]
    with pytest.raises(ValidationError):  # default cap 5
        validate_state(valid_state([valid_conv(summary="s", weekly=seven)]))
    validate_state(valid_state([valid_conv(summary="s", weekly=seven)]), weekly_cap=9)  # config raised it


# ------------------------------------------------------------------ extra forbidden
def test_unknown_field_is_rejected():
    with pytest.raises(ValidationError):
        Conversation.model_validate(valid_conv(bogus="x"))


# ------------------------------------------------------------------ is_valid_state
def test_is_valid_state_bool():
    assert is_valid_state(valid_state()) is True
    assert is_valid_state({"generated_at": "x"}) is False
