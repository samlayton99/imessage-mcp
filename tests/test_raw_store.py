"""server/raw_store.py — the VPS/Mac-mini raw-message store (raw_messages.sqlite).

The collector pushes extractor exports here via /ingest; the scheduler rebuilds summary inputs from
here via ``export()`` (the source-agnostic seam: same export-dict shape as ``collect.extract``, so
``triage.skeleton``/``triage.summarize`` consume it unchanged). Deduped on (chat_rowid, message_rowid).
"""
import datetime as dt

from text_triage.collect.extract import MAC_EPOCH_OFFSET
from text_triage.config import Config
from text_triage.server import raw_store
from text_triage.triage.skeleton import build_skeleton

NOW = dt.datetime(2026, 6, 1, 12, 0, 0).timestamp()


def _apple_ns(days_ago):
    return int((NOW - days_ago * 86400 - MAC_EPOCH_OFFSET) * 1_000_000_000)


def _dtstr(days_ago):
    return dt.datetime.fromtimestamp(NOW - days_ago * 86400).strftime("%Y-%m-%d %H:%M:%S")


def _msg(rowid, days_ago, sender, text):
    return {"message_rowid": rowid, "date": _apple_ns(days_ago),
            "datetime": _dtstr(days_ago), "sender": sender, "text": text}


def sample_export():
    """Three conversations: an active 1:1 (last from them, needs reply, IN window), a group,
    and a STALE 1:1 whose last message is 45d ago (outside the 30d window, inside 90d lookback)."""
    return {
        "generated_at": _dtstr(0),
        "window": "monthly",
        "context_messages": 0,
        "watermark": {"max_date_raw": _apple_ns(1), "max_message_rowid": 4},
        "conversations": [
            {"chat_rowid": 10, "name": "Avery", "handle": "+15551230000",
             "is_named": True, "is_groupchat": False, "responded": False,
             "members": None, "contact_details": None, "window_messages": 3,
             "conversation": [
                 _msg(1, 40, "me", "old note"),         # outside the 30d window
                 _msg(2, 5, "Avery", "you around?"),
                 _msg(3, 2, "me", "yeah what's up"),
                 _msg(4, 1, "Avery", "free sat?"),      # last from them -> needs reply
             ]},
            {"chat_rowid": 20, "name": "Group X", "handle": None,
             "is_named": True, "is_groupchat": True, "responded": True,
             "members": ["Avery", "Sam"], "contact_details": None, "window_messages": 1,
             "conversation": [_msg(5, 3, "Avery", "lunch?")]},
            {"chat_rowid": 30, "name": "Riley", "handle": "+15559990000",
             "is_named": True, "is_groupchat": False, "responded": False,
             "members": None, "contact_details": None, "window_messages": 0,
             "conversation": [
                 _msg(6, 50, "me", "happy birthday!"),
                 _msg(7, 45, "Riley", "thanks!! let's catch up"),  # stale, owed a reply
             ]},
        ],
        "unresponded": [],
    }


def test_ingest_creates_missing_parent_dirs(tmp_path):
    """A fresh server's first /ingest must not 500 because ~/.text-triage/ doesn't exist yet."""
    db = tmp_path / "fresh" / "sub" / "raw.sqlite"
    assert raw_store.ingest(sample_export(), path=db) == 7
    assert db.exists()


def test_ingest_dedups_on_chat_and_message_rowid(tmp_path):
    db = tmp_path / "raw.sqlite"
    n1 = raw_store.ingest(sample_export(), path=db)
    n2 = raw_store.ingest(sample_export(), path=db)  # identical re-push
    assert n1 == 7        # 4 + 1 + 2 messages
    assert n2 == 0        # nothing new on the second push
    assert len(raw_store.history(10, path=db)) == 4


def test_ingest_adds_only_new_messages(tmp_path):
    db = tmp_path / "raw.sqlite"
    raw_store.ingest(sample_export(), path=db)
    follow_up = {
        "conversations": [
            {"chat_rowid": 10, "name": "Avery", "handle": "+15551230000",
             "is_named": True, "is_groupchat": False, "responded": True,
             "members": None, "contact_details": None,
             "conversation": [
                 _msg(4, 1, "Avery", "free sat?"),   # already stored -> ignored
                 _msg(8, 0, "me", "yes! saturday works"),  # new
             ]},
        ],
    }
    assert raw_store.ingest(follow_up, path=db) == 1
    assert [m["message_rowid"] for m in raw_store.history(10, path=db)] == [1, 2, 3, 4, 8]


def test_history_since_filters_and_orders(tmp_path):
    db = tmp_path / "raw.sqlite"
    raw_store.ingest(sample_export(), path=db)
    recent = raw_store.history(10, since=_dtstr(3), path=db)  # within the last 3 days
    assert [m["message_rowid"] for m in recent] == [3, 4]     # 5d-ago and 40d-ago excluded
    assert recent[0]["text"] == "yeah what's up"


def test_history_after_rowid_returns_only_newer_messages(tmp_path):
    """The derived-texts_today read: messages strictly after a conversation's summary cursor."""
    db = tmp_path / "raw.sqlite"
    raw_store.ingest(sample_export(), path=db)                # conv10: rowids 1-4
    assert [m["message_rowid"] for m in raw_store.history(10, after_rowid=2, path=db)] == [3, 4]
    assert raw_store.history(10, after_rowid=4, path=db) == []


def test_export_window_is_consumable_by_skeleton(tmp_path):
    db = tmp_path / "raw.sqlite"
    raw_store.ingest(sample_export(), path=db)
    cfg = Config(messages={"context_messages": 0})  # no pre-window prefix, clean window boundary
    exp = raw_store.export(window="monthly", config=cfg, now=NOW, path=db)

    assert set(exp) >= {"generated_at", "window", "watermark", "conversations", "unresponded"}
    conv10 = next(c for c in exp["conversations"] if c["chat_rowid"] == 10)
    assert [m["message_rowid"] for m in conv10["conversation"]] == [2, 3, 4]  # 40d-ago dropped
    assert conv10["responded"] is False          # last in-window msg is from Avery
    assert conv10["window_messages"] == 3
    assert conv10["text_count"] == 4              # all-time stored count (incl. the 40d-ago message)

    # the stale 1:1 (last 45d ago) is filtered OUT of conversations but surfaces as unresponded
    assert 30 not in [c["chat_rowid"] for c in exp["conversations"]]
    assert any(u["chat_rowid"] == 30 for u in exp["unresponded"])
    # the active 1:1 (last 1d ago) is NOT stale -> not in unresponded
    assert all(u["chat_rowid"] != 10 for u in exp["unresponded"])

    assert exp["watermark"] == {"max_date_raw": _apple_ns(1), "max_message_rowid": 4}
    # the whole point: the rebuilt export drops straight into the deterministic skeleton
    state = build_skeleton(exp)
    assert {c.chat_rowid for c in state.conversations} == {10, 20}


def test_export_since_is_incremental_no_unresponded(tmp_path):
    db = tmp_path / "raw.sqlite"
    raw_store.ingest(sample_export(), path=db)
    exp = raw_store.export(since=_dtstr(3), config=Config(), now=NOW, path=db)
    # since-mode: only messages newer than the cutoff, and no stale-list recompute
    conv10 = next(c for c in exp["conversations"] if c["chat_rowid"] == 10)
    assert [m["message_rowid"] for m in conv10["conversation"]] == [3, 4]
    assert exp["unresponded"] == []


def test_deltas_returns_only_messages_after_each_cursor(tmp_path):
    """Per-conversation deltas: messages with message_rowid strictly greater than each conversation's
    summary cursor. A conversation absent from the cursor map uses 0 (everything is new)."""
    db = tmp_path / "raw.sqlite"
    raw_store.ingest(sample_export(), path=db)          # conv10: rowids 1-4; conv20: 5; conv30: 6,7
    out = raw_store.deltas({10: 2, 30: 0}, path=db)     # 10 after rowid 2, 30 all, 20 absent -> all
    by_id = {c["chat_rowid"]: c for c in out["conversations"]}
    assert [m["message_rowid"] for m in by_id[10]["conversation"]] == [3, 4]
    assert by_id[10]["new_count"] == 2
    assert by_id[10]["text_count"] == 4                 # FULL stored count, not the 2-message delta
    assert [m["message_rowid"] for m in by_id[30]["conversation"]] == [6, 7]
    assert by_id[20]["new_count"] == 1                  # absent cursor = 0 -> the one message is new


def test_deltas_omits_conversations_with_no_new_messages(tmp_path):
    db = tmp_path / "raw.sqlite"
    raw_store.ingest(sample_export(), path=db)
    out = raw_store.deltas({10: 4, 20: 5, 30: 7}, path=db)   # every cursor at its max -> nothing new
    assert out["conversations"] == []


def test_deltas_shape_feeds_skeleton(tmp_path):
    db = tmp_path / "raw.sqlite"
    raw_store.ingest(sample_export(), path=db)
    out = raw_store.deltas({}, path=db)                 # empty cursors -> the whole store is "new"
    assert set(out) >= {"generated_at", "watermark", "conversations", "unresponded"}
    state = build_skeleton(out)
    assert {c.chat_rowid for c in state.conversations} == {10, 20, 30}


def test_prune_drops_messages_older_than_retention(tmp_path):
    db = tmp_path / "raw.sqlite"
    raw_store.ingest(sample_export(), path=db)
    deleted = raw_store.prune(raw_store_days=30, now=NOW, path=db)
    assert deleted == 3   # 40d-ago (conv10) + 50d/45d-ago (conv30)
    assert [m["message_rowid"] for m in raw_store.history(10, path=db)] == [2, 3, 4]
    assert raw_store.history(30, path=db) == []


def test_prune_zero_keeps_forever(tmp_path):
    db = tmp_path / "raw.sqlite"
    raw_store.ingest(sample_export(), path=db)
    assert raw_store.prune(raw_store_days=0, now=NOW, path=db) == 0
    assert len(raw_store.history(10, path=db)) == 4


def test_counts_returns_per_conversation_totals_excluding_deleted(tmp_path):
    db = tmp_path / "raw.sqlite"
    raw_store.ingest(sample_export(), path=db)                       # 4 + 1 + 2 messages
    assert raw_store.counts(path=db) == {10: 4, 20: 1, 30: 2}
    raw_store.ingest({"conversations": [], "deleted": [{"chat_rowid": 10, "message_rowid": 2}]}, path=db)
    assert raw_store.counts(path=db)[10] == 3                        # deleted rows excluded


# --------------------------------------------------------------------------- deleted flag
def test_ingest_flips_deleted_and_history_hides_it(tmp_path):
    db = tmp_path / "raw.sqlite"
    raw_store.ingest(sample_export(), path=db)                       # conv10 rowids 1-4 live
    n = raw_store.ingest({"conversations": [], "deleted": [{"chat_rowid": 10, "message_rowid": 2}]}, path=db)
    assert n == 0                                                    # a deletion is not a new message
    assert 2 not in [m["message_rowid"] for m in raw_store.history(10, path=db)]          # hidden by default
    assert 2 in [m["message_rowid"] for m in raw_store.history(10, include_deleted=True, path=db)]  # opt-in


def test_export_and_deltas_exclude_deleted(tmp_path):
    db = tmp_path / "raw.sqlite"
    raw_store.ingest(sample_export(), path=db)
    raw_store.ingest({"conversations": [], "deleted": [{"chat_rowid": 10, "message_rowid": 4}]}, path=db)
    exp = raw_store.export(window="monthly", config=Config(messages={"context_messages": 0}), now=NOW, path=db)
    conv10 = next(c for c in exp["conversations"] if c["chat_rowid"] == 10)
    assert 4 not in [m["message_rowid"] for m in conv10["conversation"]]   # deleted text never summarized
    d = raw_store.deltas({10: 1}, path=db)
    conv10d = next(c for c in d["conversations"] if c["chat_rowid"] == 10)
    assert [m["message_rowid"] for m in conv10d["conversation"]] == [2, 3]  # 4 excluded
