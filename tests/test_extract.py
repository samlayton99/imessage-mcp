"""The unified extractor: reuse the proven chat.db helpers, add the IDs the design needs
(chat_rowid, per-message message_rowid, raw date) and a (date, message_rowid) watermark, and let
conditions.yaml drive the windows + conversation filter. Tested against a temp chat.db so no Full
Disk Access / real data is required."""
import datetime
import json
import sqlite3

from text_triage.collect import extract as extract_mod
from text_triage.config import Config, Messages
from text_triage.collect.extract import (
    MAC_EPOCH_OFFSET,
    compute_watermark,
    extract,
    iso_to_db,
    main,
    window_days_for,
)

NOW = 1_900_000_000.0  # fixed "now" (unix) so windows are deterministic
DAY = 86400
NS = 1_000_000_000


def dbdate(days_ago, secs=0):
    """Apple absolute-time (ns) integer for a moment `days_ago` before NOW."""
    return int((NOW - days_ago * DAY + secs - MAC_EPOCH_OFFSET) * NS)


def _one_to_one(handle="+15550000001", messages=None, identifier=None):
    return {
        "identifier": identifier or handle,
        "display_name": None,
        "handles": [handle],
        "messages": messages or [],
    }


# --------------------------------------------------------------- pure functions
def test_window_days_for_reads_config():
    assert window_days_for("weekly", Config()) == 7
    assert window_days_for("monthly", Config()) == 30
    custom = Config.model_validate({"messages": {"weekly_days": 14, "monthly_days": 60}})
    assert window_days_for("weekly", custom) == 14
    assert window_days_for("monthly", custom) == 60


def test_iso_to_db_matches_apple_absolute_time():
    iso = "2030-01-01T00:00:00"
    expected = int((datetime.datetime.fromisoformat(iso).timestamp() - MAC_EPOCH_OFFSET) * NS)
    assert iso_to_db(iso, NS) == expected


def test_compute_watermark_picks_max_date_then_max_rowid():
    msgs = [
        {"date": 100, "message_rowid": 5},
        {"date": 200, "message_rowid": 7},
        {"date": 200, "message_rowid": 9},  # same max date, highest rowid
        {"date": 200, "message_rowid": 8},
    ]
    assert compute_watermark(msgs) == {"max_date_raw": 200, "max_message_rowid": 9}


def test_compute_watermark_empty_is_zero():
    assert compute_watermark([]) == {"max_date_raw": 0, "max_message_rowid": 0}


# --------------------------------------------------------- full extract (temp db)
def _run(db, ab, **kw):
    kw.setdefault("now", NOW)
    return extract(db_path=str(db), addressbook_dir=str(ab), **kw)


def test_window_extract_emits_new_ids(tmp_path, chatdb_factory):
    conv = _one_to_one(
        messages=[
            {"date": dbdate(2), "from_me": True, "handle": "+15550000001", "text": "yo"},
            {"date": dbdate(1), "from_me": False, "handle": "+15550000001", "text": "you around?"},
        ]
    )
    chatdb_factory(tmp_path / "chat.db", [conv])
    out = _run(tmp_path / "chat.db", tmp_path / "ab", window="monthly")

    assert out["window"] == "monthly"
    assert out["watermark"] == {"max_date_raw": dbdate(1), "max_message_rowid": 2}
    c = out["conversations"][0]
    assert c["chat_rowid"] == 1
    assert c["handle"] == "+15550000001"
    assert c["is_groupchat"] is False
    assert c["window_messages"] == 2
    assert c["responded"] is False
    last = c["conversation"][-1]
    assert last["message_rowid"] == 2 and last["date"] == dbdate(1) and last["text"] == "you around?"
    assert "datetime" in last


def test_window_includes_context_prefix_but_counts_only_in_window(tmp_path, chatdb_factory):
    conv = _one_to_one(
        messages=[
            {"date": dbdate(41), "from_me": True, "handle": "+15550000001", "text": "old1"},
            {"date": dbdate(40), "from_me": False, "handle": "+15550000001", "text": "old2"},
            {"date": dbdate(1), "from_me": False, "handle": "+15550000001", "text": "recent"},
        ]
    )
    chatdb_factory(tmp_path / "chat.db", [conv])
    out = _run(tmp_path / "chat.db", tmp_path / "ab", window="monthly")  # context_messages=10 (default)
    c = out["conversations"][0]
    assert c["window_messages"] == 1  # only the in-window message
    assert [m["text"] for m in c["conversation"]] == ["old1", "old2", "recent"]  # 2 prefix + 1, chronological


def test_since_extract_drops_prefix_and_unresponded(tmp_path, chatdb_factory):
    conv = _one_to_one(
        messages=[
            {"date": dbdate(3), "from_me": True, "handle": "+15550000001", "text": "before"},
            {"date": dbdate(1), "from_me": False, "handle": "+15550000001", "text": "after"},
        ]
    )
    chatdb_factory(tmp_path / "chat.db", [conv])
    since_iso = datetime.datetime.fromtimestamp(NOW - 1.5 * DAY).isoformat()
    out = _run(tmp_path / "chat.db", tmp_path / "ab", since=since_iso)
    assert out["window"] == {"since": since_iso}
    assert [m["text"] for m in out["conversations"][0]["conversation"]] == ["after"]
    assert out["unresponded"] == []


def test_watermark_breaks_same_timestamp_ties_by_rowid(tmp_path, chatdb_factory):
    conv = _one_to_one(
        messages=[
            {"date": dbdate(1), "from_me": False, "handle": "+15550000001", "text": "a"},
            {"date": dbdate(1), "from_me": False, "handle": "+15550000001", "text": "b"},
        ]
    )
    chatdb_factory(tmp_path / "chat.db", [conv])
    out = _run(tmp_path / "chat.db", tmp_path / "ab", window="monthly")
    assert out["watermark"] == {"max_date_raw": dbdate(1), "max_message_rowid": 2}


def test_unresponded_heuristic_lists_stale_one_to_one(tmp_path, chatdb_factory):
    conv = _one_to_one(
        handle="+15550000099",
        messages=[
            {"date": dbdate(46), "from_me": True, "handle": "+15550000099", "text": "hey"},
            {"date": dbdate(45), "from_me": False, "handle": "+15550000099", "text": "ok ttyl"},
        ],
    )
    chatdb_factory(tmp_path / "chat.db", [conv])
    out = _run(tmp_path / "chat.db", tmp_path / "ab", window="monthly")  # window 30d, lookback 90d
    assert out["conversations"] == []
    assert len(out["unresponded"]) == 1
    unr = out["unresponded"][0]
    assert unr["chat_rowid"] == 1 and unr["name"] == "+15550000099"
    assert unr["last_date_raw"] == dbdate(45) and 44 <= unr["days_waiting"] <= 46


def test_group_chat_shape(tmp_path, chatdb_factory):
    conv = {
        "identifier": "chat-group-1",
        "display_name": "Climbing Crew",
        "handles": ["+15550000010", "+15550000011"],
        "messages": [
            {"date": dbdate(2), "from_me": False, "handle": "+15550000010", "text": "who's in?"},
            {"date": dbdate(1), "from_me": True, "handle": "+15550000010", "text": "me"},
        ],
    }
    chatdb_factory(tmp_path / "chat.db", [conv])
    c = _run(tmp_path / "chat.db", tmp_path / "ab", window="monthly")["conversations"][0]
    assert c["is_groupchat"] is True and c["name"] == "Climbing Crew" and c["handle"] is None
    assert isinstance(c["members"], list) and len(c["members"]) == 2
    assert c["responded"] is True and c["contact_details"] is None


def test_named_contact_resolves(tmp_path, chatdb_factory):
    conv = _one_to_one(
        handle="+15550000007",
        messages=[{"date": dbdate(1), "from_me": False, "handle": "+15550000007", "text": "hi"}],
    )
    chatdb_factory(tmp_path / "chat.db", [conv],
                   contacts={"+15550000007": {"first": "Jamie", "last": "Lee", "org": "Acme"}},
                   addressbook_dir=str(tmp_path / "ab"))
    c = _run(tmp_path / "chat.db", tmp_path / "ab", window="monthly")["conversations"][0]
    assert c["name"] == "Jamie Lee" and c["handle"] == "+15550000007" and c["is_named"] is True
    assert c["contact_details"]["name"] == "Jamie Lee"


def test_decodes_attributedbody_when_text_null(tmp_path, chatdb_factory):
    blob = b"NSString" + b"\x2b" + bytes([5]) + b"hello"
    conv = _one_to_one(messages=[{"date": dbdate(1), "from_me": False, "handle": "+15550000001",
                                  "text": None, "attributed": blob}])
    chatdb_factory(tmp_path / "chat.db", [conv])
    c = _run(tmp_path / "chat.db", tmp_path / "ab", window="monthly")["conversations"][0]
    assert c["conversation"][-1]["text"] == "hello"


def test_tapback_is_labeled(tmp_path, chatdb_factory):
    conv = _one_to_one(messages=[{"date": dbdate(1), "from_me": False, "handle": "+15550000001",
                                  "text": "x", "amt": 2000}])
    chatdb_factory(tmp_path / "chat.db", [conv])
    c = _run(tmp_path / "chat.db", tmp_path / "ab", window="monthly")["conversations"][0]
    assert c["conversation"][-1]["text"] == "[Reacted: Loved]"


# -------------------------------------------------- conversation_filter (from config)
def _cfg(**kw):
    return Config(messages=Messages(**kw))


def test_filter_excludes_groups_when_disabled(tmp_path, chatdb_factory):
    group = {"identifier": "g", "display_name": "Crew", "handles": ["+15550000010", "+15550000011"],
             "messages": [{"date": dbdate(1), "from_me": False, "handle": "+15550000010", "text": "hi"}]}
    chatdb_factory(tmp_path / "chat.db", [group])
    out = _run(tmp_path / "chat.db", tmp_path / "ab", window="monthly", config=_cfg(include_groups=False))
    assert out["conversations"] == []


def test_named_only_excludes_unnamed(tmp_path, chatdb_factory):
    conv = _one_to_one(handle="+15550000003",
                       messages=[{"date": dbdate(1), "from_me": False, "handle": "+15550000003", "text": "hi"}])
    chatdb_factory(tmp_path / "chat.db", [conv])  # no contacts -> unnamed
    out = _run(tmp_path / "chat.db", tmp_path / "ab", window="monthly", config=_cfg(named_only=True))
    assert out["conversations"] == []


def test_shortcode_dropped_by_min_handle_digits(tmp_path, chatdb_factory):
    conv = {"identifier": "38792", "display_name": None, "handles": ["38792"],
            "messages": [{"date": dbdate(1), "from_me": False, "handle": "38792", "text": "code 123"}]}
    chatdb_factory(tmp_path / "chat.db", [conv])
    out = _run(tmp_path / "chat.db", tmp_path / "ab", window="monthly")  # default min_handle_digits=10
    assert out["conversations"] == []


def test_spam_floor_excludes_low_total_conversations(tmp_path, chatdb_factory):
    """The spam floor counts a conversation's ALL-TIME message total: below it the conversation never
    enters the store; at/above it is kept."""
    conv = _one_to_one(messages=[
        {"date": dbdate(2), "from_me": False, "handle": "+15550000001", "text": "a"},
        {"date": dbdate(1), "from_me": False, "handle": "+15550000001", "text": "b"},
    ])
    chatdb_factory(tmp_path / "chat.db", [conv])
    excluded = _run(tmp_path / "chat.db", tmp_path / "ab", window="monthly", config=_cfg(spam_floor=3))
    assert excluded["conversations"] == []                              # 2 total < floor 3
    kept = _run(tmp_path / "chat.db", tmp_path / "ab", window="monthly", config=_cfg(spam_floor=2))
    assert [c["chat_rowid"] for c in kept["conversations"]] == [1]      # 2 total >= floor 2


def test_spam_floor_counts_tapbacks(tmp_path, chatdb_factory):
    """Tapbacks are message rows, so they count toward the spam floor total."""
    conv = _one_to_one(messages=[
        {"date": dbdate(2), "from_me": False, "handle": "+15550000001", "text": "hi"},
        {"date": dbdate(1), "from_me": False, "handle": "+15550000001", "text": "x", "amt": 2000},
    ])
    chatdb_factory(tmp_path / "chat.db", [conv])
    out = _run(tmp_path / "chat.db", tmp_path / "ab", window="monthly", config=_cfg(spam_floor=2))
    assert [c["chat_rowid"] for c in out["conversations"]] == [1]       # text + tapback = 2 >= floor


def test_chat_rowids_filters_to_requested_conversations(tmp_path, chatdb_factory):
    """The collector backfills one conversation at a time via chat_rowids=."""
    a = _one_to_one(handle="+15550000001",
                    messages=[{"date": dbdate(1), "from_me": False, "handle": "+15550000001", "text": "a"}])
    b = _one_to_one(handle="+15550000002",
                    messages=[{"date": dbdate(1), "from_me": False, "handle": "+15550000002", "text": "b"}])
    chatdb_factory(tmp_path / "chat.db", [a, b])                        # chat_rowids 1 and 2
    out = _run(tmp_path / "chat.db", tmp_path / "ab", window="monthly", chat_rowids=[2])
    assert [c["chat_rowid"] for c in out["conversations"]] == [2]


def test_unresponded_excludes_shortcode(tmp_path, chatdb_factory):
    conv = {"identifier": "38792", "display_name": None, "handles": ["38792"],
            "messages": [{"date": dbdate(46), "from_me": True, "handle": "38792", "text": "x"},
                         {"date": dbdate(45), "from_me": False, "handle": "38792", "text": "y"}]}
    chatdb_factory(tmp_path / "chat.db", [conv])
    out = _run(tmp_path / "chat.db", tmp_path / "ab", window="monthly")
    assert out["unresponded"] == []  # shortcode filtered by min_handle_digits=10


def test_recoverable_deletions_reads_recently_deleted_and_unsends(tmp_path, chatdb_factory):
    """The deterministic deleted signal: membership in chat_recoverable_message_join (Recently Deleted)
    or a non-zero message.date_retracted (Unsend), as {chat_rowid, message_rowid} pairs."""
    db = tmp_path / "chat.db"
    conv = _one_to_one(handle="+15550000001", messages=[
        {"date": dbdate(2), "from_me": False, "handle": "+15550000001", "text": "a"},   # rowid 1
        {"date": dbdate(1), "from_me": True, "handle": "+15550000001", "text": "b"},     # rowid 2
    ])
    chatdb_factory(db, [conv])
    con = sqlite3.connect(str(db))
    con.executescript(
        "CREATE TABLE chat_recoverable_message_join (chat_id INTEGER, message_id INTEGER, delete_date INTEGER);"
        "INSERT INTO chat_recoverable_message_join VALUES (1, 1, 123);"     # msg 1 deleted (recoverable)
        "ALTER TABLE message ADD COLUMN date_retracted INTEGER;"
        "UPDATE message SET date_retracted=999 WHERE ROWID=2;")             # msg 2 unsent
    con.commit()
    con.close()
    pairs = {(d["chat_rowid"], d["message_rowid"]) for d in extract_mod.recoverable_deletions(str(db))}
    assert pairs == {(1, 1), (1, 2)}


def test_recoverable_deletions_empty_when_tables_absent(tmp_path, chatdb_factory):
    """Older macOS without those tables/columns -> no signal, no crash."""
    db = tmp_path / "chat.db"
    chatdb_factory(db, [_one_to_one(messages=[
        {"date": dbdate(1), "from_me": False, "handle": "+15550000001", "text": "x"}])])
    assert extract_mod.recoverable_deletions(str(db)) == []


def test_main_writes_out_file(tmp_path, chatdb_factory):
    conv = _one_to_one(messages=[{"date": dbdate(1), "from_me": False, "handle": "+15550000001", "text": "hi"}])
    db, ab, out = tmp_path / "chat.db", tmp_path / "ab", tmp_path / "export.json"
    cfg = tmp_path / "conditions.yaml"
    cfg.write_text("{}\n")  # empty mapping = defaults; keeps this test hermetic from the repo's config
    chatdb_factory(db, [conv])
    rc = main(["--window", "monthly", "--db", str(db), "--addressbook", str(ab),
               "--out", str(out), "--config", str(cfg)])
    assert rc == 0
    data = json.loads(out.read_text())
    assert data["window"] == "monthly"
    assert data["conversations"][0]["conversation"][-1]["text"] == "hi"
