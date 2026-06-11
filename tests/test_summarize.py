"""The three summary agents (daily/weekly/monthly). Code owns the facts; each agent writes only its
own fields (the matrix); tags carry lifetimes and the add/delete rules per cadence. Tested entirely
with a StubEngine — no LLM, no network."""
import datetime
import json

from text_triage.config import Config
from text_triage.triage.engine import StubEngine
from text_triage.state.schema import State
from text_triage.triage.summarize import (build_contexts, build_daily_prompt, build_monthly_prompt,
                                    build_weekly_prompt, summarize_daily, summarize_monthly,
                                    summarize_weekly)
from text_triage.triage.tags import TagSpec

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


def daily_json(daily_note="Asked to meet Thursday.", tags=None, summary=None, reply_status=None):
    return json.dumps({"daily_note": daily_note, "tags": tags if tags is not None else [],
                       "summary": summary, "reply_status": reply_status})


def weekly_json(weekly_note="Quiet week; one scheduling thread.", identity=None, tags=None,
                summary=None, reply_status=None):
    return json.dumps({"identity": identity, "weekly_note": weekly_note,
                       "tags": tags if tags is not None else [],
                       "summary": summary, "reply_status": reply_status})


def monthly_json(monthly="Spent the month planning.", history_line="2026-06: planning.",
                 identity=None, tags=None, summary=None, reply_status=None):
    return json.dumps({"identity": identity, "monthly": monthly, "history_line": history_line,
                       "tags": tags if tags is not None else [],
                       "summary": summary, "reply_status": reply_status})


def prev_record(chat_rowid=1, name="Avery Quinn", handle="+15550000201", **over):
    base = {"chat_rowid": chat_rowid, "name": name, "is_group": False, "handle": handle,
            "members": None, "status": "active", "last_from": "them",
            "last_message_at": "2026-06-01 10:00", "reply_status": "needs_response", "texts_today": [],
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


def test_daily_prompt_splits_system_frame_and_user_data():
    system, user = build_daily_prompt({"name": "Avery Quinn", "is_group": False},
                                      [msg(text="meet thursday?")],
                                      prev={"identity": "A friend.", "monthly": "m", "weekly": [],
                                            "daily": [], "history": []}, law=LAW)
    # global frame + role + the tag law live in the SYSTEM prompt
    assert "needs-scheduling (ttl 14d)" in system and "family (sticky)" in system
    assert "assume" in system.lower() and "DAILY" in system
    # this one conversation's data lives in the USER prompt
    assert "Avery Quinn" in user and "A friend." in user and "meet thursday" in user


def test_weekly_user_omits_daily_layer():
    _, user = build_weekly_prompt(
        {"name": "X", "is_group": False}, [msg()],
        prev={"identity": "i", "monthly": "m", "weekly": [{"week_of": "2026-05-26", "text": "W"}],
              "daily": [{"date": "2026-06-01", "text": "DAILYLEAK"}], "history": []}, law=LAW)
    assert "DAILYLEAK" not in user                      # weekly never sees daily notes


def test_monthly_user_omits_weekly_and_daily_layers():
    _, user = build_monthly_prompt(
        {"name": "X", "is_group": False}, [msg()],
        prev={"identity": "i", "monthly": "m", "weekly": [{"week_of": "w", "text": "WEEKLYLEAK"}],
              "daily": [{"date": "2026-06-01", "text": "DAILYLEAK"}], "history": []}, law=LAW)
    assert "WEEKLYLEAK" not in user and "DAILYLEAK" not in user


def test_build_contexts_returns_per_conversation_system_user_model():
    ctxs = build_contexts("daily", export_with([conv()]), config=Config(), law=LAW)
    assert len(ctxs) == 1
    c = ctxs[0]
    assert c["chat_rowid"] == 1 and "Avery Quinn" in c["user"] and "DAILY" in c["system"]
    assert c["model"] == Config().engine.models.daily and c["est_tokens"] > 0


def test_max_raw_messages_caps_what_enters_the_prompt():
    msgs = [msg(rowid=i, text=f"m{i}") for i in range(5)]
    cfg = Config(engine={"max_raw_messages": {"monthly": 2}})
    ctxs = build_contexts("monthly", export_with([conv(messages=msgs)]), config=cfg, law=LAW)
    user = ctxs[0]["user"]
    assert "m4" in user and "m3" in user                # newest 2 kept (oldest-first slice)
    assert "m0" not in user and "m2" not in user


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
                                   monthly="carried", last_from="me", reply_status="waiting_reply")])
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


# ------------------------------------------------------- the daily summarize floor (delta gate)
def test_daily_gate_skips_low_activity_conversation():
    """Daily skips the LLM call for a conversation with fewer than summarize_floor NEW messages (the
    gate keys on the per-conversation ``new_count`` the raw-store deltas path sets); it is carried
    forward as raw only and flagged new_conversation. At/above the floor it is summarized."""
    cfg = Config(messages={"summarize_floor": 5})
    low = conv(chat_rowid=1, name="Low", handle="+15550000001",
               messages=[msg(rowid=i, text=f"m{i}") for i in range(2)])
    low["new_count"], low["text_count"] = 2, 2             # below floor -> skipped; few texts -> new
    high = conv(chat_rowid=2, name="High", handle="+15550000002",
                messages=[msg(rowid=i, text=f"h{i}") for i in range(5)])
    high["new_count"], high["text_count"] = 5, 5           # at floor -> summarized
    eng = StubEngine([daily_json()])                       # one response -> only one call may happen
    s = summarize_daily(export_with([low, high]), engine=eng, config=cfg, law=LAW)
    assert len(eng.calls) == 1
    by_id = {c.chat_rowid: c for c in s.conversations}
    assert by_id[1].daily == [] and by_id[1].new_conversation is True       # skipped; only 2 texts
    assert len(by_id[2].daily) == 1 and by_id[2].new_conversation is False  # summarized
    assert by_id[2].summarized_through == 4                # cursor advanced to the newest rowid


def test_new_conversation_keys_on_text_count_not_summary_status():
    """new_conversation reflects whether the conversation has fewer than summarize_floor RAW texts in
    total -- NOT whether it has ever been summarized. An established conversation that daily skips (few
    NEW messages) is not new; a tiny one is new even though daily also skips it."""
    cfg = Config(messages={"summarize_floor": 5})
    established = conv(chat_rowid=1, name="Established", handle="+15550000001",
                      messages=[msg(rowid=99, text="hey")])
    established["new_count"], established["text_count"] = 1, 50   # 1 new -> skipped, but 50 texts total
    fresh = conv(chat_rowid=2, name="Fresh", handle="+15550000002",
                 messages=[msg(rowid=i, text=f"f{i}") for i in range(3)])
    fresh["new_count"], fresh["text_count"] = 3, 3               # below floor -> skipped, and few texts
    s = summarize_daily(export_with([established, fresh]), engine=StubEngine([]), config=cfg, law=LAW)
    by_id = {c.chat_rowid: c for c in s.conversations}
    assert by_id[1].daily == [] and by_id[1].new_conversation is False      # skipped, but established
    assert by_id[2].daily == [] and by_id[2].new_conversation is True       # skipped, and new


def test_limit_skipped_established_conversation_is_not_new():
    """The bootstrap bug this fixes: a conversation with many texts that a --limit run did NOT summarize
    (so it has no cursor yet) must not be flagged new -- newness is about text count, not the cursor."""
    cfg = Config(messages={"summarize_floor": 5})
    a = conv(chat_rowid=1, name="A", handle="+15550000001"); a["text_count"] = 80
    b = conv(chat_rowid=2, name="B", handle="+15550000002"); b["text_count"] = 120
    s = summarize_monthly(export_with([a, b]), engine=StubEngine([monthly_json()]),
                          config=cfg, law=LAW, limit=1)            # only the first is summarized
    by_id = {c.chat_rowid: c for c in s.conversations}
    assert by_id[1].monthly and by_id[1].new_conversation is False           # summarized
    assert by_id[2].monthly is None and by_id[2].summarized_through == 0      # limit-skipped, no cursor
    assert by_id[2].new_conversation is False                                # but NOT new -- 120 texts


def test_new_conversation_stays_true_for_tiny_conversation_after_summary():
    """A conversation with fewer than summarize_floor total texts stays flagged new even after a
    (non-gated) monthly summary -- the flag is the text count, not summary progress."""
    cfg = Config(messages={"summarize_floor": 5})
    tiny = conv(chat_rowid=1, name="Tiny", handle="+15550000001",
                messages=[msg(rowid=i, text=f"t{i}") for i in range(3)])
    tiny["text_count"] = 3
    s = summarize_monthly(export_with([tiny]), engine=StubEngine([monthly_json()]), config=cfg, law=LAW)
    c = s.conversations[0]
    assert c.monthly and c.summarized_through == 2          # it WAS summarized + cursor advanced
    assert c.new_conversation is True                       # but still new -- only 3 texts


def test_daily_no_new_count_means_no_gate():
    """Exports without ``new_count`` (direct calls, the chatdb path) are not gated — every conversation
    summarizes as before, so the gate never silently changes the legacy path."""
    s = summarize_daily(export_with([conv()]), engine=StubEngine([daily_json()]),
                        config=Config(messages={"summarize_floor": 999}), law=LAW)
    assert len(s.conversations[0].daily) == 1              # summarized despite a huge floor (no new_count)


# ------------------------------------------------------- reply_status (LLM judgment over the gate)
def test_llm_reply_status_overrides_the_gate_per_mode():
    """conv() is 1:1, last from them -> gate says needs_response; the LLM may judge it standby."""
    for fn, payload in ((summarize_daily, daily_json(reply_status="standby")),
                        (summarize_weekly, weekly_json(reply_status="standby")),
                        (summarize_monthly, monthly_json(reply_status="standby"))):
        s = fn(export_with([conv()]), engine=StubEngine([payload]), config=Config(), law=LAW)
        assert s.conversations[0].reply_status == "standby"


def test_null_reply_status_keeps_the_gate_value():
    s = summarize_daily(export_with([conv(responded=True)]),
                        engine=StubEngine([daily_json(reply_status=None)]), config=Config(), law=LAW)
    assert s.conversations[0].reply_status == "waiting_reply"   # gate value, 1:1 I sent last


def test_invalid_reply_status_is_sanitized_not_retried():
    eng = StubEngine([daily_json(reply_status="shouting")])
    s = summarize_daily(export_with([conv()]), engine=eng, config=Config(), law=LAW)
    assert len(eng.calls) == 1                                  # sanitized, no retry burned
    assert s.conversations[0].reply_status == "needs_response"  # falls back to the gate


def test_gate_refreshes_reply_status_when_llm_is_silent():
    """The fresh deterministic gate (not the stale prior) is the base every real run."""
    prev = prev_state([prev_record(reply_status="standby")])    # LLM judged standby yesterday
    s = summarize_daily(export_with([conv(responded=False)]),   # but they texted again
                        engine=StubEngine([daily_json()]), config=Config(), prev_state=prev, law=LAW)
    assert s.conversations[0].reply_status == "needs_response"


def test_skipped_conversation_refreshes_facts_but_keeps_memory():
    """A delta-gated skip keeps the agent-authored memory (summary/notes/cursor) but refreshes the
    DETERMINISTIC facts from the new messages — the gate, last_message_at, the reply metadata. The
    bug this kills: you text someone, daily skips the low-traffic thread, and it kept presenting a
    stale needs_response even though the last message is now yours."""
    cfg = Config(messages={"summarize_floor": 5})
    low = conv(chat_rowid=1, responded=True,                      # I sent last -> gate: waiting_reply
               messages=[msg(rowid=99, dt="2026-06-02 08:00", sender="me", text="hi cutie")])
    low["new_count"], low["text_count"] = 1, 50
    prev = prev_state([prev_record(chat_rowid=1, reply_status="needs_response", summary="Kept.",
                                   summarized_through=42, last_from="them",
                                   daily=[{"date": "2026-06-01", "text": "note"}])])
    s = summarize_daily(export_with([low]), engine=StubEngine([]), config=cfg, prev_state=prev, law=LAW)
    c = s.conversations[0]
    assert c.reply_status == "waiting_reply"                      # fresh gate, not the stale value
    assert c.last_message_at == "2026-06-02 08:00"                # fresh fact
    assert c.summary == "Kept." and [n.text for n in c.daily] == ["note"]   # memory kept
    assert c.summarized_through == 42                             # cursor did NOT advance (still owed a summary)


# ----------------------------------------------------------------- summary (the one-liner)
def test_every_mode_rewrites_the_summary():
    for fn, payload in ((summarize_daily, daily_json(summary="New snapshot.")),
                        (summarize_weekly, weekly_json(summary="New snapshot.")),
                        (summarize_monthly, monthly_json(summary="New snapshot."))):
        prev = prev_state([prev_record(summary="Old snapshot.")])
        s = fn(export_with([conv()]), engine=StubEngine([payload]), config=Config(),
               prev_state=prev, law=LAW)
        assert s.conversations[0].summary == "New snapshot."


def test_blank_summary_keeps_the_previous_one():
    prev = prev_state([prev_record(summary="Kept snapshot.")])
    s = summarize_daily(export_with([conv()]), engine=StubEngine([daily_json(summary=None)]),
                        config=Config(), prev_state=prev, law=LAW)
    assert s.conversations[0].summary == "Kept snapshot."


# ------------------------------------------------------------- deterministic reply metadata
def test_metadata_comes_from_skeleton_and_falls_back_to_prev():
    messages = [msg(rowid=1, dt="2026-06-01 09:00", sender="me", text="hi"),
                msg(rowid=2, dt="2026-06-01 10:00", sender="Avery Quinn", text="yo")]
    s = summarize_daily(export_with([conv(messages=messages)]), engine=StubEngine([daily_json()]),
                        config=Config(), law=LAW)
    c = s.conversations[0]
    assert c.last_from_me_at == "2026-06-01 09:00" and c.last_from_them_at == "2026-06-01 10:00"

    # a delta window with only their messages: my side carries from the previous record
    prev = prev_state([prev_record(last_from_me_at="2026-05-30 08:00")])
    s = summarize_daily(export_with([conv()]), engine=StubEngine([daily_json()]),
                        config=Config(), prev_state=prev, law=LAW)
    c = s.conversations[0]
    assert c.last_from_me_at == "2026-05-30 08:00"              # carried
    assert c.last_from_them_at == "2026-06-01 10:00"            # fresh from the window


# ------------------------------------------------------- who_am_i + today in the context
def test_build_contexts_injects_today_and_who_am_i():
    ctxs = build_contexts("daily", export_with([conv()], generated_at="2026-06-02 09:00"),
                          config=Config(), law=LAW, who_am_i="Sam, a grad student.")
    assert "Today is 2026-06-02 09:00." in ctxs[0]["system"]
    assert "Sam, a grad student." in ctxs[0]["system"]


# ------------------------------------------------------------------------------ weekly
def test_weekly_not_gated_by_summarize_floor():
    """The floor gates daily only; weekly consolidates existing notes, so it summarizes regardless of
    how few new messages a conversation has."""
    low = conv(chat_rowid=1, messages=[msg(rowid=1)])
    low["new_count"] = 1                                   # below the daily floor
    s = summarize_weekly(export_with([low]), engine=StubEngine([weekly_json()]),
                         config=Config(messages={"summarize_floor": 5}), law=LAW)
    assert len(s.conversations[0].weekly) == 1


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
    from text_triage.triage import summarize as S
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


def test_cli_monthly_from_raw_store_writes_validated_state(tmp_path):
    """The server path: no chat.db — the window is rebuilt from raw_messages.sqlite (--source raw-store)
    and run through the same summarizer. This is how the scheduler produces state.json on the host."""
    from text_triage.server import raw_store
    from text_triage.triage import summarize as S
    cfg, watch = _cli(tmp_path)
    db = tmp_path / "raw.sqlite"
    recent = (datetime.datetime.now() - datetime.timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S")
    raw_store.ingest({"conversations": [
        {"chat_rowid": 1, "name": "Avery Quinn", "handle": "+15550000201", "is_named": True,
         "is_groupchat": False, "members": None, "contact_details": None, "conversation": [
             {"message_rowid": 10, "date": _recent_db_date(3), "datetime": recent,
              "sender": "Avery Quinn", "text": "meet thursday?"}]}]}, path=db)
    out = tmp_path / "state.json"
    rc = S.main(["--mode", "monthly", "--source", "raw-store", "--raw-store", str(db),
                 "--out", str(out), "--config", str(cfg), "--watch", str(watch)],
                engine=StubEngine([monthly_json(tags=["needs-scheduling"])]))
    assert rc == 0
    c = json.loads(out.read_text())["conversations"][0]
    assert c["name"] == "Avery Quinn"
    assert c["monthly"] == "Spent the month planning."
    assert c["tags"] == ["needs-scheduling"]


def test_cli_daily_delta_gate_accumulates_until_floor(tmp_path):
    """End-to-end: below the floor, daily skips the conversation and does NOT advance its cursor, so its
    new messages accumulate across runs until they cross the floor and it is summarized."""
    from text_triage.server import raw_store
    from text_triage.triage import summarize as S
    cfg = tmp_path / "conditions.yaml"
    cfg.write_text("messages:\n  summarize_floor: 3\n")
    watch = tmp_path / "watch.md"
    watch.write_text("- needs-scheduling: set a time, about 14 days\n")
    db, out = tmp_path / "raw.sqlite", tmp_path / "state.json"

    def ingest(rowids):
        raw_store.ingest({"conversations": [
            {"chat_rowid": 1, "name": "Avery", "handle": "+15550000201", "is_named": True,
             "is_groupchat": False, "members": None, "contact_details": None,
             "conversation": [{"message_rowid": r, "date": r, "datetime": "2026-06-01 10:00:00",
                               "sender": "Avery", "text": f"m{r}"} for r in rowids]}]}, path=db)

    def run_daily(eng):
        return S.main(["--mode", "daily", "--source", "raw-store", "--raw-store", str(db),
                       "--out", str(out), "--config", str(cfg), "--watch", str(watch)], engine=eng)

    ingest([1, 2])                                  # 2 new (< floor 3)
    eng1 = StubEngine([])                            # must NOT be consumed
    run_daily(eng1)
    assert eng1.calls == []
    c = json.loads(out.read_text())["conversations"][0]
    assert c["daily"] == [] and c["new_conversation"] is True and c["summarized_through"] == 0

    ingest([3])                                     # now 3 accumulated (>= floor 3)
    eng2 = StubEngine([daily_json()])
    run_daily(eng2)
    assert len(eng2.calls) == 1
    c = json.loads(out.read_text())["conversations"][0]
    assert len(c["daily"]) == 1 and c["new_conversation"] is False and c["summarized_through"] == 3


def test_cli_empty_source_leaves_state_untouched(tmp_path):
    """When the source has nothing new (all cursors at their max -> deltas empty), the run is a no-op:
    no LLM call, and a good state.json is left byte-for-byte untouched."""
    from text_triage.server import raw_store
    from text_triage.triage import summarize as S
    cfg, watch = _cli(tmp_path)
    db, out = tmp_path / "raw.sqlite", tmp_path / "state.json"
    recent = (datetime.datetime.now() - datetime.timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S")
    raw_store.ingest({"conversations": [
        {"chat_rowid": 1, "name": "Avery", "handle": "+15550000201", "is_named": True,
         "is_groupchat": False, "members": None, "contact_details": None,
         "conversation": [{"message_rowid": 10, "date": _recent_db_date(3), "datetime": recent,
                           "sender": "Avery", "text": "hi"}]}]}, path=db)
    # a monthly run creates state.json AND advances conv 1's cursor to rowid 10
    S.main(["--mode", "monthly", "--source", "raw-store", "--raw-store", str(db), "--out", str(out),
            "--config", str(cfg), "--watch", str(watch)], engine=StubEngine([monthly_json()]))
    before = out.read_text()
    eng = StubEngine([])                          # must NOT be consumed
    rc = S.main(["--mode", "daily", "--source", "raw-store", "--raw-store", str(db), "--out", str(out),
                 "--config", str(cfg), "--watch", str(watch)], engine=eng)
    assert rc == 0 and eng.calls == []            # nothing new -> no engine call
    assert out.read_text() == before              # state.json untouched


def test_cli_daily_dispatch_writes_a_note(tmp_path, chatdb_factory):
    from text_triage.triage import summarize as S
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


def test_cli_show_context_prints_prompts_and_makes_no_engine_call(tmp_path, chatdb_factory, capsys):
    from text_triage.triage import summarize as S
    cfg, watch = _cli(tmp_path)
    spec = {"identifier": "+15550000201", "display_name": None, "handles": ["+15550000201"],
            "messages": [{"date": _recent_db_date(3), "from_me": False,
                          "handle": "+15550000201", "text": "meet thursday?"}]}
    chatdb_factory(tmp_path / "chat.db", [spec])
    eng = StubEngine([])  # must NOT be consumed
    rc = S.main(["--mode", "monthly", "--show-context", "--db", str(tmp_path / "chat.db"),
                 "--addressbook", str(tmp_path / "ab"), "--config", str(cfg), "--watch", str(watch)],
                engine=eng)
    assert rc == 0
    out = capsys.readouterr().out
    assert "--- SYSTEM ---" in out and "--- USER ---" in out and "meet thursday" in out
    assert eng.calls == []   # no LLM call was made
