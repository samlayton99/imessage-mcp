"""The deterministic state-skeleton builder: turn an extract export into the DETERMINISTIC fields
of state.json (facts owned by code), leaving every LLM-authored field blank. No LLM, no I/O."""
import datetime
import json
from pathlib import Path

import pytest

from text_triage.state.schema import State
from text_triage.triage.skeleton import build_skeleton, decayed_reply_status, reply_status_gate

FIXTURES = Path(__file__).parent / "fixtures"


def make_export(conversations=None, unresponded=None, generated_at="2026-06-02 14:32", watermark=None):
    return {
        "generated_at": generated_at,
        "window": "30d",
        "context_messages": 10,
        "watermark": watermark or {"max_date_raw": 802345678901234000, "max_message_rowid": 42},
        "conversations": conversations if conversations is not None else [],
        "unresponded": unresponded if unresponded is not None else [],
    }


def conv_1to1(chat_rowid=1, name="Andrew Marks", handle="+15184105257", responded=False, last_dt="2026-05-31 14:02"):
    return {
        "chat_rowid": chat_rowid,
        "name": name,
        "handle": handle,
        "is_named": True,
        "is_groupchat": False,
        "responded": responded,
        "members": None,
        "contact_details": None,
        "window_messages": 1,
        "conversation": [
            {"message_rowid": 10, "date": 999, "datetime": last_dt, "sender": name, "text": "hey"}
        ],
    }


def conv_group(chat_rowid=2, name="Climbing Crew", members=None, last_dt="2026-05-30 10:00"):
    return {
        "chat_rowid": chat_rowid,
        "name": name,
        "handle": None,
        "is_named": True,
        "is_groupchat": True,
        "responded": True,
        "members": members or ["Alex", "Bo"],
        "contact_details": None,
        "window_messages": 1,
        "conversation": [
            {"message_rowid": 5, "date": 900, "datetime": last_dt, "sender": "Alex", "text": "who's in?"}
        ],
    }


# ---------------------------------------------------------------- reply_status gate
@pytest.mark.parametrize(
    "is_group,responded,expected",
    [
        (False, False, "needs_response"),  # 1:1, they sent last -> I owe a reply
        (False, True, "waiting_reply"),    # 1:1, I sent last -> ball in their court
        (True, True, "standby"),           # groups never deterministically owe a reply
        (True, False, "standby"),
    ],
)
def test_reply_status_gate(is_group, responded, expected):
    assert reply_status_gate(is_group=is_group, responded=responded) == expected


# ------------------------------------------------------------- query-time decay
def test_waiting_reply_decays_to_standby_after_decay_days():
    as_of = datetime.datetime(2026, 6, 10, 12, 0)
    assert decayed_reply_status("waiting_reply", "2026-06-01 09:00",
                                decay_days=7, as_of=as_of) == "standby"


def test_waiting_reply_fresher_than_decay_days_is_kept():
    as_of = datetime.datetime(2026, 6, 10, 12, 0)
    assert decayed_reply_status("waiting_reply", "2026-06-08 09:00",
                                decay_days=7, as_of=as_of) == "waiting_reply"


def test_only_waiting_reply_decays():
    as_of = datetime.datetime(2026, 6, 10, 12, 0)
    assert decayed_reply_status("needs_response", "2026-01-01 09:00",
                                decay_days=7, as_of=as_of) == "needs_response"
    assert decayed_reply_status("standby", "2026-01-01 09:00",
                                decay_days=7, as_of=as_of) == "standby"


def test_decay_disabled_or_unparseable_date_keeps_status():
    as_of = datetime.datetime(2026, 6, 10, 12, 0)
    assert decayed_reply_status("waiting_reply", "2026-01-01 09:00",
                                decay_days=0, as_of=as_of) == "waiting_reply"   # 0 = never decay
    assert decayed_reply_status("waiting_reply", "garbage",
                                decay_days=7, as_of=as_of) == "waiting_reply"   # lenient on bad dates
    assert decayed_reply_status("waiting_reply", None,
                                decay_days=7, as_of=as_of) == "waiting_reply"


# ------------------------------------------------------------------ build_skeleton
def test_returns_validated_state():
    s = build_skeleton(make_export([conv_1to1()]))
    assert isinstance(s, State)


def test_one_to_one_deterministic_fields():
    s = build_skeleton(make_export([conv_1to1(responded=False)]))
    c = s.conversations[0]
    assert c.chat_rowid == 1
    assert c.handle == "+15184105257"
    assert c.is_group is False
    assert c.members is None
    assert c.status == "active"
    assert c.last_from == "them"
    assert c.reply_status == "needs_response"
    assert c.last_message_at == "2026-05-31 14:02"


def test_llm_fields_left_blank():
    c = build_skeleton(make_export([conv_1to1()])).conversations[0]
    assert c.identity is None and c.texts_today == []
    assert c.tags == [] and c.daily == [] and c.weekly == [] and c.history == []
    assert c.monthly is None and c.edited == {}


def test_responded_one_to_one_is_waiting_reply():
    c = build_skeleton(make_export([conv_1to1(responded=True)])).conversations[0]
    assert c.last_from == "me"
    assert c.reply_status == "waiting_reply"


def test_group_skeleton():
    c = build_skeleton(make_export([conv_group()])).conversations[0]
    assert c.is_group is True
    assert c.members == ["Alex", "Bo"]
    assert c.handle is None
    assert c.last_from == "me"
    assert c.reply_status == "standby"


# ------------------------------------------------------ deterministic reply metadata
def test_last_from_each_side_computed_from_messages():
    conv = conv_1to1()
    conv["conversation"] = [
        {"message_rowid": 1, "date": 1, "datetime": "2026-05-29 08:00", "sender": "me", "text": "hi"},
        {"message_rowid": 2, "date": 2, "datetime": "2026-05-30 09:00", "sender": "Andrew Marks", "text": "yo"},
        {"message_rowid": 3, "date": 3, "datetime": "2026-05-31 10:00", "sender": "me", "text": "lunch?"},
    ]
    c = build_skeleton(make_export([conv])).conversations[0]
    assert c.last_from_me_at == "2026-05-31 10:00"
    assert c.last_from_them_at == "2026-05-30 09:00"


def test_last_from_metadata_none_when_side_absent():
    c = build_skeleton(make_export([conv_1to1()])).conversations[0]  # single message from them
    assert c.last_from_me_at is None
    assert c.last_from_them_at == "2026-05-31 14:02"


def test_unresponded_mapped_and_extra_field_dropped():
    unr = [{"chat_rowid": 412, "name": "Rod Mann", "last_at": "2026-04-20 09:11:02",
            "last_date_raw": 736500000000000000, "days_waiting": 43}]
    s = build_skeleton(make_export([], unresponded=unr))
    assert len(s.unresponded) == 1
    u = s.unresponded[0]
    assert u.chat_rowid == 412 and u.name == "Rod Mann" and u.days_waiting == 43
    assert u.last_at == "2026-04-20 09:11:02"


def test_watermark_passthrough():
    wm = {"max_date_raw": 123456789, "max_message_rowid": 77}
    s = build_skeleton(make_export([conv_1to1()], watermark=wm))
    assert s.watermark.max_date_raw == 123456789
    assert s.watermark.max_message_rowid == 77


def test_generated_at_uses_override_then_export():
    assert build_skeleton(make_export([])).generated_at == "2026-06-02 14:32"
    assert build_skeleton(make_export([]), generated_at="2099-01-01 00:00").generated_at == "2099-01-01 00:00"


# ------------------------------------------------ golden regression (committed fixture)
def test_build_skeleton_matches_golden_fixture():
    export = json.loads((FIXTURES / "synthetic_export.json").read_text())
    golden = json.loads((FIXTURES / "synthetic_export.expected_state.json").read_text())
    produced = json.loads(build_skeleton(export).model_dump_json())
    assert produced == golden


def test_every_fixture_record_validates():
    export = json.loads((FIXTURES / "synthetic_export.json").read_text())
    s = build_skeleton(export)  # build_skeleton validates internally; raises if any record is bad
    assert len(s.conversations) == 5
    assert len(s.unresponded) == 1


def test_skeleton_texts_today_is_empty():
    c = build_skeleton(make_export([conv_1to1()])).conversations[0]
    assert c.texts_today == []
