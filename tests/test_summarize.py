"""The daily summarizer (PLAN Step 0): turn an extract export + the previous state into a fresh,
schema-valid state.json. The LLM writes prose (identity/summary/reply_reason/daily note) and
proposes tags; code owns the facts, the merge, the tag-law filter, validation + retry, and never
lets an invalid record land. Tested entirely with a StubEngine — no LLM, no network."""
import json

import pytest

from text_triage.config import Config
from text_triage.engine import StubEngine
from text_triage.schema import State
from text_triage.summarize import build_daily_prompt, summarize_daily

LAW = {"family": "Family members.", "needs-scheduling": "A time/date to set.", "church": "Church."}


# ----------------------------------------------------------------- export/prev helpers
def msg(rowid=10, dt="2026-06-01 10:00", sender="Avery Quinn", text="can we meet thursday?"):
    return {"message_rowid": rowid, "date": 99, "datetime": dt, "sender": sender, "text": text}


def conv(chat_rowid=1, name="Avery Quinn", handle="+15550000201", responded=False,
         is_group=False, members=None, messages=None):
    messages = messages if messages is not None else [msg()]
    return {"chat_rowid": chat_rowid, "name": name, "handle": None if is_group else handle,
            "is_named": True, "is_groupchat": is_group, "responded": responded,
            "members": members if is_group else None, "contact_details": None,
            "window_messages": len(messages), "conversation": messages}


def export_with(convs, generated_at="2026-06-02 09:00", watermark=None, unresponded=None):
    return {"generated_at": generated_at, "window": "monthly", "context_messages": 10,
            "watermark": watermark or {"max_date_raw": 100, "max_message_rowid": 10},
            "conversations": convs, "unresponded": unresponded or []}


def good(summary="They asked to meet Thursday.", identity="A college climbing friend.",
         reply_reason="Direct question about Thursday; their message is last.",
         daily_note="Asked to meet Thursday; unanswered.", tags=None):
    return json.dumps({"identity": identity, "summary": summary, "reply_reason": reply_reason,
                       "daily_note": daily_note, "tags": tags if tags is not None else []})


def valid_prev_record(chat_rowid=999, name="Old Friend", handle="+15550009999"):
    return {"chat_rowid": chat_rowid, "name": name, "is_group": False, "handle": handle,
            "members": None, "status": "active", "last_from": "me",
            "last_message_at": "2026-05-01 10:00", "needs_reply": False,
            "summary": "Carried summary.", "identity": "An old friend.", "tags": [],
            "daily": [], "weekly": [], "monthly": None, "history": [], "edited": {}}


# ----------------------------------------------------------------------------- tests
def test_returns_validated_state_with_llm_fields():
    s = summarize_daily(export_with([conv()]), engine=StubEngine([good()]), config=Config(), law=LAW)
    assert isinstance(s, State)
    c = s.conversations[0]
    assert c.summary == "They asked to meet Thursday."
    assert c.identity == "A college climbing friend."        # proposed (was blank)
    assert c.reply_reason and c.needs_reply is True          # needs_reply 1:1 -> reason required
    assert [n.text for n in c.daily] == ["Asked to meet Thursday; unanswered."]
    assert c.daily[0].date == "2026-06-02"                   # from generated_at
    assert c.last_updated == "2026-06-02 09:00"


def test_prompt_contains_raw_text_identity_and_law():
    p = build_daily_prompt(
        {"name": "Avery Quinn", "is_group": False, "needs_reply": True},
        [msg(text="can we meet thursday?")], prev=None, law=LAW)
    assert "meet thursday" in p
    assert "identity" in p.lower()
    assert "needs-scheduling" in p  # active law slugs offered to the model


def test_invalid_json_then_valid_retries_once():
    eng = StubEngine(["not json at all", good()])
    s = summarize_daily(export_with([conv()]), engine=eng, config=Config(), law=LAW)
    assert len(eng.calls) == 2
    assert s.conversations[0].summary == "They asked to meet Thursday."


def test_two_failures_fall_back_to_skeleton_record():
    # an identity of 5 sentences violates the schema -> invalid both times -> never lands
    bad = good(identity="One. Two. Three. Four. Five.")
    eng = StubEngine([bad, bad])
    s = summarize_daily(export_with([conv()]), engine=eng, config=Config(), law=LAW)
    assert len(eng.calls) == 2
    c = s.conversations[0]
    assert c.summary is None and c.identity is None          # fell back to deterministic skeleton
    assert c.needs_reply is True                             # facts preserved


def test_edited_identity_is_never_overwritten():
    prev = {"conversations": [{**valid_prev_record(chat_rowid=1, name="Avery Quinn",
                                                    handle="+15550000201"),
                               "identity": "Set by the user.",
                               "edited": {"identity": "user:2026-05-20"}}]}
    s = summarize_daily(export_with([conv()]), engine=StubEngine([good()]),
                        config=Config(), prev_state=prev, law=LAW)
    assert s.conversations[0].identity == "Set by the user."


def test_identity_is_sticky_once_set():
    prev = {"conversations": [{**valid_prev_record(chat_rowid=1, name="Avery Quinn",
                                                   handle="+15550000201"),
                               "identity": "Existing identity."}]}
    s = summarize_daily(export_with([conv()]), engine=StubEngine([good(identity="New proposal.")]),
                        config=Config(), prev_state=prev, law=LAW)
    assert s.conversations[0].identity == "Existing identity."


def test_prev_only_conversation_is_carried_forward_without_an_engine_call():
    eng = StubEngine([good()])
    prev = {"conversations": [valid_prev_record(chat_rowid=999)]}
    s = summarize_daily(export_with([conv(chat_rowid=1)]), engine=eng,
                        config=Config(), prev_state=prev, law=LAW)
    assert len(eng.calls) == 1  # only the conversation with new messages
    ids = {c.chat_rowid for c in s.conversations}
    assert ids == {1, 999}
    carried = next(c for c in s.conversations if c.chat_rowid == 999)
    assert carried.summary == "Carried summary."


def test_out_of_vocab_tags_are_dropped():
    s = summarize_daily(export_with([conv()]),
                        engine=StubEngine([good(tags=["family", "not-a-real-tag"])]),
                        config=Config(), law=LAW)
    assert s.conversations[0].tags == ["family"]


def test_needs_reply_stays_deterministic_even_if_llm_disagrees():
    # responded 1:1 -> gate says no reply owed; reply_reason must be dropped, summary still lands
    s = summarize_daily(export_with([conv(responded=True)]),
                        engine=StubEngine([good(reply_reason="I think they're owed one")]),
                        config=Config(), law=LAW)
    c = s.conversations[0]
    assert c.needs_reply is False and c.reply_reason is None
    assert c.summary == "They asked to meet Thursday."


def test_daily_notes_trimmed_to_cap():
    prior = [{"date": f"2026-05-{25 + i}", "text": f"note {i}"} for i in range(7)]  # 25..31, == cap 7
    prev = {"conversations": [{**valid_prev_record(chat_rowid=1, name="Avery Quinn",
                                                   handle="+15550000201"), "daily": prior}]}
    s = summarize_daily(export_with([conv()]), engine=StubEngine([good()]),
                        config=Config(), prev_state=prev, law=LAW)
    dates = [n.date for n in s.conversations[0].daily]
    assert len(dates) == 7                       # capped
    assert dates[0] == "2026-05-26"              # oldest dropped
    assert dates[-1] == "2026-06-02"             # newest appended


# --------------------------------------------------------------------- CLI (`summarize` subcommand)
def _recent_db_date(days_ago):
    """Apple-ns timestamp `days_ago` before *now*, so the message always lands inside the window
    regardless of when the test runs (the CLI uses the real clock)."""
    import datetime
    now = datetime.datetime.now().timestamp()
    return int((now - days_ago * 86400 - 978307200) * 1_000_000_000)


def _cli_setup(tmp_path):
    cfg = tmp_path / "conditions.yaml"
    cfg.write_text("{}\n")                                   # hermetic from the repo config
    watch = tmp_path / "watch.md"
    watch.write_text("- needs-scheduling: set a time\n")     # hermetic from the repo watch.md
    spec = {"identifier": "+15550000201", "display_name": None, "handles": ["+15550000201"],
            "messages": [{"date": _recent_db_date(3), "from_me": False,
                          "handle": "+15550000201", "text": "can we meet thursday?"}]}
    return cfg, watch, spec


def _cli_args(tmp_path, out):
    return ["--window", "monthly", "--db", str(tmp_path / "chat.db"),
            "--addressbook", str(tmp_path / "ab"), "--out", str(out)]


def test_cli_summarize_writes_validated_state(tmp_path, chatdb_factory):
    from text_triage import summarize as S
    cfg, watch, spec = _cli_setup(tmp_path)
    chatdb_factory(tmp_path / "chat.db", [spec])
    out = tmp_path / "state.json"
    rc = S.main(_cli_args(tmp_path, out) + ["--config", str(cfg), "--watch", str(watch)],
                engine=StubEngine([good(tags=["needs-scheduling"])]))
    assert rc == 0
    c = json.loads(out.read_text())["conversations"][0]
    assert c["summary"] == "They asked to meet Thursday."
    assert c["tags"] == ["needs-scheduling"]
    assert c["needs_reply"] is True and c["reply_reason"]


def test_cli_rerun_reads_prev_state_and_appends_a_note(tmp_path, chatdb_factory):
    from text_triage import summarize as S
    cfg, watch, spec = _cli_setup(tmp_path)
    chatdb_factory(tmp_path / "chat.db", [spec])
    out = tmp_path / "state.json"
    extra = ["--config", str(cfg), "--watch", str(watch)]
    S.main(_cli_args(tmp_path, out) + extra, engine=StubEngine([good()]))
    S.main(_cli_args(tmp_path, out) + extra, engine=StubEngine([good(daily_note="second day note")]))
    notes = [n["text"] for n in json.loads(out.read_text())["conversations"][0]["daily"]]
    assert notes == ["Asked to meet Thursday; unanswered.", "second day note"]  # prev read + append
