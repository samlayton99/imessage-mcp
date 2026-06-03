"""The three summary agents (daily/weekly/monthly). Code owns the facts; each agent writes only its
own fields (the matrix); tags carry lifetimes and the add/delete rules per cadence. Tested entirely
with a StubEngine — no LLM, no network."""
import datetime
import json

from text_triage.config import Config
from text_triage.engine import StubEngine
from text_triage.schema import State
from text_triage.summarize import (build_daily_prompt, summarize_daily, summarize_monthly,
                                    summarize_weekly)
from text_triage.tags import TagSpec

LAW = {
    "family": TagSpec("family", "Family.", "sticky", None),
    "needs-scheduling": TagSpec("needs-scheduling", "Sched.", "ttl", 14),
    "church": TagSpec("church", "Church.", "sticky", None),
}


# ----------------------------------------------------------------- export / prev helpers
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
    return {"generated_at": generated_at, "window": "weekly", "context_messages": 10,
            "watermark": watermark or {"max_date_raw": 100, "max_message_rowid": 10},
            "conversations": convs, "unresponded": unresponded or []}


def daily_json(daily_note="Asked to meet Thursday.", tags=None):
    return json.dumps({"daily_note": daily_note, "tags": tags if tags is not None else []})


def weekly_json(weekly_note="Quiet week; one scheduling thread.", identity=None, tags=None):
    return json.dumps({"identity": identity, "weekly_note": weekly_note,
                       "tags": tags if tags is not None else []})


def monthly_json(monthly="Spent the month planning.", history_line="2026-06: planning.",
                 identity=None, tags=None):
    return json.dumps({"identity": identity, "monthly": monthly, "history_line": history_line,
                       "tags": tags if tags is not None else []})


def prev_record(chat_rowid=1, name="Avery Quinn", handle="+15550000201", **over):
    base = {"chat_rowid": chat_rowid, "name": name, "is_group": False, "handle": handle,
            "members": None, "status": "active", "last_from": "them",
            "last_message_at": "2026-06-01 10:00", "needs_reply": True, "texts_today": [],
            "identity": "A friend.", "tags": [], "daily": [], "weekly": [], "monthly": None,
            "history": [], "edited": {}}
    base.update(over)
    return base


def prev_state(records):
    return {"conversations": records}


# ------------------------------------------------------------------------------ daily
def test_daily_appends_note_adds_tags_and_clears_texts_today():
    prev = prev_state([prev_record(tags=["family"], identity="A friend.")])
    s = summarize_daily(export_with([conv()]), engine=StubEngine([daily_json(tags=["needs-scheduling"])]),
                        config=Config(), prev_state=prev, law=LAW)
    assert isinstance(s, State)
    c = s.conversations[0]
    assert [n.text for n in c.daily] == ["Asked to meet Thursday."]
    assert c.tags == ["family", "needs-scheduling"]      # add-only union
    assert c.texts_today == []                           # read then cleared
    assert c.identity == "A friend."                     # daily never touches identity


def test_daily_does_not_delete_tags():
    prev = prev_state([prev_record(tags=["family", "church"])])
    s = summarize_daily(export_with([conv()]), engine=StubEngine([daily_json(tags=[])]),  # proposes none
                        config=Config(), prev_state=prev, law=LAW)
    assert s.conversations[0].tags == ["family", "church"]   # nothing removed


def test_daily_carries_prev_monthly_and_history():
    prev = prev_state([prev_record(monthly="prev monthly", history=[{"date": "2026-05-01", "text": "h"}])])
    s = summarize_daily(export_with([conv()]), engine=StubEngine([daily_json()]),
                        config=Config(), prev_state=prev, law=LAW)
    c = s.conversations[0]
    assert c.monthly == "prev monthly" and [h.text for h in c.history] == ["h"]


def test_daily_prompt_has_raw_identity_law_lifetimes_and_voice():
    p = build_daily_prompt({"name": "Avery Quinn", "is_group": False}, [msg(text="meet thursday?")],
                           prev={"identity": "A friend.", "monthly": "m", "weekly": [], "daily": [],
                                 "history": []}, law=LAW)
    assert "meet thursday" in p and "Avery Quinn" in p and "A friend." in p
    assert "needs-scheduling (ttl 14d)" in p and "family (sticky)" in p
    assert "assume" in p.lower()


def test_carried_tag_not_in_law_is_dropped():
    prev = prev_state([prev_record(tags=["family", "obsolete-tag"])])
    s = summarize_daily(export_with([conv()]), engine=StubEngine([daily_json(tags=[])]),
                        config=Config(), prev_state=prev, law=LAW)
    assert s.conversations[0].tags == ["family"]          # obsolete-tag dropped (not in LAW)


def test_daily_two_failures_fall_back_to_prior_record():
    prev = prev_state([prev_record(monthly="kept", tags=["family"])])
    eng = StubEngine(["not json", "still not json"])
    s = summarize_daily(export_with([conv()]), engine=eng, config=Config(), prev_state=prev, law=LAW)
    c = s.conversations[0]
    assert len(eng.calls) == 2
    assert c.monthly == "kept" and c.daily == [] and c.tags == ["family"]   # prior kept, no new note


def test_prev_only_conversation_carried_forward():
    eng = StubEngine([daily_json()])
    prev = prev_state([prev_record(chat_rowid=999, name="Old", handle="+15550009999",
                                   monthly="carried", last_from="me", needs_reply=False)])
    s = summarize_daily(export_with([conv(chat_rowid=1)]), engine=eng, config=Config(),
                        prev_state=prev, law=LAW)
    assert len(eng.calls) == 1
    assert {c.chat_rowid for c in s.conversations} == {1, 999}
    assert next(c for c in s.conversations if c.chat_rowid == 999).monthly == "carried"


def test_limit_only_summarizes_top_n():
    convs = [conv(chat_rowid=1, name="A", handle="+15550000001"),
             conv(chat_rowid=2, name="B", handle="+15550000002")]
    eng = StubEngine([daily_json()])                      # only one response
    s = summarize_daily(export_with(convs), engine=eng, config=Config(), law=LAW, limit=1)
    assert len(eng.calls) == 1 and len(s.conversations) == 2
    assert len(s.conversations[0].daily) == 1 and len(s.conversations[1].daily) == 0


# ------------------------------------------------------------------------------ weekly
def test_weekly_appends_note_and_clears_daily():
    prev = prev_state([prev_record(daily=[{"date": "2026-06-01", "text": "d1"}])])
    s = summarize_weekly(export_with([conv()]), engine=StubEngine([weekly_json()]),
                         config=Config(), prev_state=prev, law=LAW)
    c = s.conversations[0]
    assert [w.text for w in c.weekly] == ["Quiet week; one scheduling thread."]
    assert c.daily == []                                  # weekly clears daily


def test_weekly_can_delete_tags():
    prev = prev_state([prev_record(tags=["family", "church"])])
    s = summarize_weekly(export_with([conv()]), engine=StubEngine([weekly_json(tags=["family"])]),
                         config=Config(), prev_state=prev, law=LAW)
    assert s.conversations[0].tags == ["family"]          # church dropped (full replace)


def test_weekly_preserves_edited_tags_even_on_replace():
    prev = prev_state([prev_record(tags=["family", "church"], edited={"tags": "user:2026-05-01"})])
    s = summarize_weekly(export_with([conv()]), engine=StubEngine([weekly_json(tags=["needs-scheduling"])]),
                         config=Config(), prev_state=prev, law=LAW)
    assert set(s.conversations[0].tags) == {"family", "church", "needs-scheduling"}  # union, not replace


def test_weekly_identity_sticky_once_set():
    prev = prev_state([prev_record(identity="Existing.")])
    s = summarize_weekly(export_with([conv()]), engine=StubEngine([weekly_json(identity="New proposal.")]),
                         config=Config(), prev_state=prev, law=LAW)
    assert s.conversations[0].identity == "Existing."     # not overwritten


def test_weekly_proposes_identity_when_blank():
    prev = prev_state([prev_record(identity=None)])
    s = summarize_weekly(export_with([conv()]), engine=StubEngine([weekly_json(identity="A new friend.")]),
                         config=Config(), prev_state=prev, law=LAW)
    assert s.conversations[0].identity == "A new friend."


# ------------------------------------------------------------------------------ monthly
def test_monthly_rewrites_condenses_history_and_clears_weekly_and_daily():
    prev = prev_state([prev_record(monthly="old", weekly=[{"week_of": "2026-W22", "text": "w"}],
                                   daily=[{"date": "2026-06-01", "text": "d"}])])
    s = summarize_monthly(export_with([conv()]), engine=StubEngine([monthly_json()]),
                          config=Config(), prev_state=prev, law=LAW)
    c = s.conversations[0]
    assert c.monthly == "Spent the month planning."
    assert [h.text for h in c.history] == ["2026-06: planning."]
    assert c.weekly == [] and c.daily == []               # both cleared


def test_monthly_marks_dormant_when_silent_over_30d():
    old = conv(messages=[msg(dt="2026-04-01 10:00", text="old")])
    s = summarize_monthly(export_with([old], generated_at="2026-06-02 09:00"),
                          engine=StubEngine([monthly_json()]), config=Config(), law=LAW)
    assert s.conversations[0].status == "dormant"


def test_monthly_active_when_recent():
    recent = conv(messages=[msg(dt="2026-06-01 10:00", text="recent")])
    s = summarize_monthly(export_with([recent], generated_at="2026-06-02 09:00"),
                          engine=StubEngine([monthly_json()]), config=Config(), law=LAW)
    assert s.conversations[0].status == "active"


# --------------------------------------------------------------------- CLI (`--mode`)
def _recent_db_date(days_ago):
    now = datetime.datetime.now().timestamp()
    return int((now - days_ago * 86400 - 978307200) * 1_000_000_000)


def _cli(tmp_path, watch_line="- needs-scheduling: set a time, about 14 days"):
    cfg = tmp_path / "conditions.yaml"
    cfg.write_text("{}\n")
    watch = tmp_path / "watch.md"
    watch.write_text(watch_line + "\n")
    return cfg, watch


def test_cli_monthly_writes_validated_state(tmp_path, chatdb_factory):
    from text_triage import summarize as S
    cfg, watch = _cli(tmp_path)
    spec = {"identifier": "+15550000201", "display_name": None, "handles": ["+15550000201"],
            "messages": [{"date": _recent_db_date(3), "from_me": False,
                          "handle": "+15550000201", "text": "meet thursday?"}]}
    chatdb_factory(tmp_path / "chat.db", [spec])
    out = tmp_path / "state.json"
    rc = S.main(["--mode", "monthly", "--db", str(tmp_path / "chat.db"),
                 "--addressbook", str(tmp_path / "ab"), "--out", str(out),
                 "--config", str(cfg), "--watch", str(watch)],
                engine=StubEngine([monthly_json(tags=["needs-scheduling"])]))
    assert rc == 0
    c = json.loads(out.read_text())["conversations"][0]
    assert c["monthly"] == "Spent the month planning."
    assert c["tags"] == ["needs-scheduling"]


def test_cli_daily_dispatch_writes_a_note(tmp_path, chatdb_factory):
    from text_triage import summarize as S
    cfg, watch = _cli(tmp_path)
    spec = {"identifier": "+15550000201", "display_name": None, "handles": ["+15550000201"],
            "messages": [{"date": _recent_db_date(1 / 24.0), "from_me": False,   # ~1 hour ago
                          "handle": "+15550000201", "text": "you around?"}]}
    chatdb_factory(tmp_path / "chat.db", [spec])
    out = tmp_path / "state.json"
    rc = S.main(["--mode", "daily", "--db", str(tmp_path / "chat.db"),
                 "--addressbook", str(tmp_path / "ab"), "--out", str(out),
                 "--config", str(cfg), "--watch", str(watch)],
                engine=StubEngine([daily_json()]))
    assert rc == 0
    convs = json.loads(out.read_text())["conversations"]
    assert convs and convs[0]["daily"][0]["text"] == "Asked to meet Thursday."
