"""The deterministic state-skeleton builder: turn an extract export into the DETERMINISTIC fields
of state.json (facts owned by code), leaving every LLM-authored field blank. No LLM, no I/O."""
import json
from pathlib import Path

import pytest

from text_triage.schema import State
from text_triage.skeleton import build_skeleton, needs_reply_gate

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


# ---------------------------------------------------------------- needs_reply gate
@pytest.mark.parametrize(
    "is_group,responded,expected",
    [
        (False, False, True),   # 1:1, they sent last -> owe a reply
        (False, True, False),   # 1:1, I sent last -> no
        (True, True, False),    # groups are always "responded"
        (True, False, False),   # groups never need a reply
    ],
)
def test_needs_reply_gate(is_group, responded, expected):
    assert needs_reply_gate(is_group=is_group, responded=responded) is expected


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
    assert c.needs_reply is True
    assert c.last_message_at == "2026-05-31 14:02"


def test_llm_fields_left_blank():
    c = build_skeleton(make_export([conv_1to1()])).conversations[0]
    assert c.identity is None and c.summary is None and c.reply_reason is None
    assert c.tags == [] and c.daily == [] and c.weekly == [] and c.history == []
    assert c.monthly is None and c.edited == {}


def test_responded_one_to_one_does_not_need_reply():
    c = build_skeleton(make_export([conv_1to1(responded=True)])).conversations[0]
    assert c.last_from == "me"
    assert c.needs_reply is False


def test_group_skeleton():
    c = build_skeleton(make_export([conv_group()])).conversations[0]
    assert c.is_group is True
    assert c.members == ["Alex", "Bo"]
    assert c.handle is None
    assert c.last_from == "me"
    assert c.needs_reply is False


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


def test_texts_today_is_empty():
    s = build_skeleton(make_export([conv_1to1()]))
    assert s.texts_today.since is None
    assert s.texts_today.conversations == {}


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


def test_build_skeleton_accepts_config():
    from text_triage.config import Config

    s = build_skeleton(make_export([conv_1to1()]), config=Config())
    assert isinstance(s, State)
