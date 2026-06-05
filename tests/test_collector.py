"""collect/collector.py — the Mac-side raw pusher. Polls chat.db (reusing the extractor), pushes new
raw to the server's /ingest, and advances a local watermark. The HTTP ``post`` is injected so these
tests never open a socket; ``server.url`` blank targets the local loopback bind."""
import datetime as dt
import json

from text_triage.collect import collector
from text_triage.config import Config


def _recent_db_date(days_ago):
    now = dt.datetime.now().timestamp()
    return int((now - days_ago * 86400 - 978307200) * 1_000_000_000)


def _spec(text="yo", days_ago=2):
    return {"identifier": "+15550000201", "display_name": None, "handles": ["+15550000201"],
            "messages": [{"date": _recent_db_date(days_ago), "from_me": False,
                          "handle": "+15550000201", "text": text}]}


# ------------------------------------------------------------------ ingest_url
def test_ingest_url_blank_targets_loopback_bind():
    assert collector.ingest_url(Config()) == "http://127.0.0.1:8787/ingest"


def test_ingest_url_remote_when_set():
    c = Config(server={"url": "https://triage.host/"})
    assert collector.ingest_url(c) == "https://triage.host/ingest"


def test_ingest_url_maps_0_0_0_0_bind_to_loopback():
    c = Config(server={"bind": "0.0.0.0:9000"})
    assert collector.ingest_url(c) == "http://127.0.0.1:9000/ingest"


# ------------------------------------------------------------------- push_once
def test_push_once_extracts_new_raw_and_posts(tmp_path, chatdb_factory, monkeypatch):
    monkeypatch.delenv("TEXT_TRIAGE_INGEST_TOKEN", raising=False)
    chatdb_factory(tmp_path / "chat.db", [_spec(text="meet thursday?")])
    calls = []

    def fake_post(url, data, headers):
        calls.append((url, json.loads(data.decode())))
        return 200, '{"ingested": 1}'

    res = collector.push_once(Config(), db_path=str(tmp_path / "chat.db"),
                              addressbook_dir=str(tmp_path / "ab"), state_dir=tmp_path, post=fake_post)
    assert res["pushed"] == 1
    assert len(calls) == 1
    url, payload = calls[0]
    assert url.endswith("/ingest")
    assert payload["conversations"][0]["conversation"][0]["text"] == "meet thursday?"
    assert (tmp_path / "collector.json").exists()      # watermark persisted


def test_push_once_skips_post_when_nothing_new(tmp_path, chatdb_factory, monkeypatch):
    monkeypatch.delenv("TEXT_TRIAGE_INGEST_TOKEN", raising=False)
    chatdb_factory(tmp_path / "chat.db", [_spec(days_ago=2)])
    posts = []

    def fake_post(url, data, headers):
        posts.append(url)
        return 200, "{}"

    kw = dict(db_path=str(tmp_path / "chat.db"), addressbook_dir=str(tmp_path / "ab"),
              state_dir=tmp_path, post=fake_post)
    collector.push_once(Config(), **kw)            # first push sends the message + advances watermark
    assert len(posts) == 1
    collector.push_once(Config(), **kw)            # nothing newer than the watermark -> no post
    assert len(posts) == 1


def test_push_once_first_run_uses_backfill_window_and_admits(tmp_path, monkeypatch):
    """The very first sync mirrors the whole backfill window in one extract (no separate per-conversation
    backfill) and records every emitted conversation as admitted."""
    monkeypatch.delenv("TEXT_TRIAGE_INGEST_TOKEN", raising=False)
    calls = []

    def fake_extract(*, db_path, addressbook_dir, since=None, config=None, chat_rowids=None):
        calls.append({"since": since, "chat_rowids": chat_rowids})
        return {"generated_at": "2026-06-01 12:00:00", "conversations": [
            {"chat_rowid": 9, "name": "A", "handle": "+15550000009", "is_named": True,
             "is_groupchat": False, "responded": False, "members": None, "contact_details": None,
             "conversation": [{"message_rowid": 1, "date": 1, "datetime": "x", "sender": "A", "text": "hi"}]}]}

    posts = []
    now = dt.datetime(2026, 6, 1, 12, 0, 0)
    collector.push_once(Config(), db_path="x", addressbook_dir="y", state_dir=tmp_path,
                        post=lambda u, d, h: (posts.append(json.loads(d.decode())), (200, "{}"))[1],
                        extract_fn=fake_extract, now=now)
    assert len(calls) == 1 and calls[0]["chat_rowids"] is None           # one extract, no backfill pass
    assert calls[0]["since"] == (now - dt.timedelta(days=365 * 3)).strftime("%Y-%m-%d %H:%M:%S")
    state = json.loads((tmp_path / "collector.json").read_text())
    assert state["admitted"] == [9]


def test_push_once_backfills_full_history_on_admission(tmp_path, monkeypatch):
    """A conversation that newly crosses the spam floor (in the delta but not yet admitted) triggers a
    full-history backfill, so the pushed payload carries all of it — not just the one new message."""
    monkeypatch.delenv("TEXT_TRIAGE_INGEST_TOKEN", raising=False)
    (tmp_path / "collector.json").write_text(json.dumps({"since": "2026-05-31 00:00:00", "admitted": [5]}))
    calls = []

    def _conv(cid, msgs):
        return {"chat_rowid": cid, "name": f"c{cid}", "handle": f"+1555000000{cid}", "is_named": True,
                "is_groupchat": False, "responded": False, "members": None, "contact_details": None,
                "conversation": [{"message_rowid": m, "date": m, "datetime": "x", "sender": "x",
                                  "text": f"m{m}"} for m in msgs]}

    def fake_extract(*, db_path, addressbook_dir, since=None, config=None, chat_rowids=None):
        calls.append({"since": since, "chat_rowids": chat_rowids})
        if chat_rowids is not None:                          # backfill: conv 7's full 5-message history
            return {"generated_at": "2026-06-01 12:00:00", "conversations": [_conv(7, [1, 2, 3, 4, 5])]}
        return {"generated_at": "2026-06-01 12:00:00",       # delta: admitted 5 + newly-crossing 7
                "conversations": [_conv(5, [50]), _conv(7, [5])]}

    posts = []
    res = collector.push_once(Config(), db_path="x", addressbook_dir="y", state_dir=tmp_path,
                              post=lambda u, d, h: (posts.append(json.loads(d.decode())), (200, "{}"))[1],
                              extract_fn=fake_extract, now=dt.datetime(2026, 6, 1, 12, 0, 0))
    assert any(c["chat_rowids"] == [7] for c in calls)        # backfill happened for the new conversation
    conv7 = next(c for c in posts[0]["conversations"] if c["chat_rowid"] == 7)
    assert [m["message_rowid"] for m in conv7["conversation"]] == [1, 2, 3, 4, 5]   # full history, not the delta
    assert res["admitted_new"] == [7]
    state = json.loads((tmp_path / "collector.json").read_text())
    assert set(state["admitted"]) == {5, 7}


def test_push_once_attaches_recoverable_deletions(tmp_path, monkeypatch):
    """Each push carries the deleted-message signal (read from chat.db's Recently Deleted / unsends) so
    the server can flag them; the deletions_fn is injected so the test needs no chat.db."""
    monkeypatch.delenv("TEXT_TRIAGE_INGEST_TOKEN", raising=False)
    (tmp_path / "collector.json").write_text(json.dumps({"since": "2026-05-31 00:00:00", "admitted": [5]}))
    posts = []

    def fake_extract(*, db_path, addressbook_dir, since=None, config=None, chat_rowids=None):
        return {"generated_at": "2026-06-01 12:00:00", "conversations": [
            {"chat_rowid": 5, "name": "c5", "handle": "+15550000005", "is_named": True,
             "is_groupchat": False, "responded": True, "members": None, "contact_details": None,
             "conversation": [{"message_rowid": 50, "date": 50, "datetime": "x", "sender": "me", "text": "hi"}]}]}

    collector.push_once(Config(), db_path="x", addressbook_dir="y", state_dir=tmp_path,
                        post=lambda u, d, h: (posts.append(json.loads(d.decode())), (200, "{}"))[1],
                        extract_fn=fake_extract,
                        deletions_fn=lambda db: [{"chat_rowid": 5, "message_rowid": 49}],
                        now=dt.datetime(2026, 6, 1, 12, 0, 0))
    assert posts[0]["deleted"] == [{"chat_rowid": 5, "message_rowid": 49}]


def test_open_trigger_bootstraps_monthly_then_daily(tmp_path):
    """The on-open trigger fires a one-time MONTHLY the first time ever (so a new setup gets a full note
    stack), recorded in collector.json, then DAILY on subsequent process starts."""
    posts = []
    post = lambda u, d, h: (posts.append(json.loads(d.decode())), (200, "{}"))[1]
    assert collector._open_trigger(Config(), tmp_path, post) == "monthly"
    assert posts[-1]["mode"] == "monthly"
    assert json.loads((tmp_path / "collector.json").read_text())["bootstrapped"] is True
    assert collector._open_trigger(Config(), tmp_path, post) == "daily"   # already bootstrapped
    assert posts[-1]["mode"] == "daily"


def test_push_once_sends_bearer_when_token_set(tmp_path, chatdb_factory, monkeypatch):
    monkeypatch.setenv("TEXT_TRIAGE_INGEST_TOKEN", "ing-tok")
    chatdb_factory(tmp_path / "chat.db", [_spec()])
    seen = {}

    def fake_post(url, data, headers):
        seen.update(headers)
        return 200, "{}"

    collector.push_once(Config(), db_path=str(tmp_path / "chat.db"),
                        addressbook_dir=str(tmp_path / "ab"), state_dir=tmp_path, post=fake_post)
    assert seen.get("Authorization") == "Bearer ing-tok"
