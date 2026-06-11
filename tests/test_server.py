"""server/app.py — the MCP serving surface (FastMCP) + the /ingest /trigger /health routes.

The FastMCP wiring is lazy-imported (like the engine backends), so these tests drive the tool LOGIC
and the auth check directly — no socket, no fastmcp install needed. The thin transport layer is proven
by the manual round-trip in the milestone verification. State is the single owner here (state_io).

The impls return the AGENT-FACING presentation (the MCP response contract): `conversation_id` (never
the internal chat_rowid), humanized timestamps ("May 30, 2026 10:00am"), no internal fields
(message_rowid / summarized_through / handle / edited / last_updated), live raw under
`unsummarized_messages` as {when, sender, text}.
"""
import datetime as dt

import pytest

from text_triage.server import app, raw_store
from text_triage.state import state_io

AS_OF = dt.datetime(2026, 6, 1, 12, 0, 0)


def _law(tmp_path):
    p = tmp_path / "watch.md"
    p.write_text("- family: close kin, sticky\n- needs-scheduling: pending plans, 7 days\n")
    return p


def _conv(rowid, name, **over):
    base = {
        "chat_rowid": rowid, "name": name, "is_group": False, "handle": f"+1555000{rowid:04d}",
        "members": None, "status": "active", "last_from": "them",
        "last_message_at": "2026-05-30 10:00:00", "reply_status": "needs_response",
        "identity": None, "tags": [], "daily": [], "weekly": [], "monthly": None,
        "history": [], "texts_today": [], "edited": {},
    }
    base.update(over)
    return base


def _state(tmp_path):
    """A 3-conversation state: an active 1:1 owed a reply (recent ttl tag), an active family 1:1
    awaiting Mom's reply since 05-15 (17d before AS_OF), and a dormant 1:1 whose ttl tag has aged out."""
    data = {
        "generated_at": "2026-06-01 12:00:00",
        "watermark": {"max_date_raw": 1, "max_message_rowid": 9},
        "unresponded": [],
        "conversations": [
            _conv(10, "Avery", tags=["needs-scheduling"], reply_status="needs_response",
                  identity="Climbing friend.",
                  daily=[{"date": "2026-05-30", "text": "Asked about Saturday."}],
                  texts_today=[{"message_rowid": 9, "datetime": "2026-06-01 11:00:00",
                                "sender": "Avery", "text": "still on?"}]),
            _conv(20, "Mom", tags=["family"], reply_status="waiting_reply", last_from="me",
                  last_message_at="2026-05-15 09:00:00"),
            _conv(30, "Old Coworker", tags=["needs-scheduling"], status="dormant",
                  last_message_at="2026-04-01 10:00:00"),  # 60d ago -> ttl(7) expired
        ],
    }
    path = tmp_path / "state.json"
    state_io.write_state(data, path, law={"family", "needs-scheduling"})
    return path


# -------------------------------------------------------- the humanized timestamp
def test_fmt_dt_humanizes_timestamps():
    assert app._fmt_dt("2026-05-30 10:00:00") == "May 30, 2026 10:00am"
    assert app._fmt_dt("2026-06-01 23:16:57") == "June 1, 2026 11:16pm"
    assert app._fmt_dt("2026-12-07 12:35:00") == "December 7, 2026 12:35pm"
    assert app._fmt_dt("2026-01-02 00:05:00") == "January 2, 2026 12:05am"
    assert app._fmt_dt("not a date") == "not a date"     # lenient: pass through
    assert app._fmt_dt(None) is None


# ------------------------------------------------------------------- list tags
def test_list_tags_returns_the_full_law_with_system_tags(tmp_path):
    out = app.list_tags_impl(law_path=_law(tmp_path))
    by_tag = {t["tag"]: t for t in out}
    assert set(by_tag) == {"reply_status", "family", "needs-scheduling"}
    fam = by_tag["family"]
    assert fam["type"] == "freeform" and fam["defined_by"] == "user"
    assert fam["relevance"] == "always relevant"
    assert "choices" not in fam                                       # freeform: no choices key
    sched = by_tag["needs-scheduling"]
    assert sched["relevance"] == "relevant for ~7 days after the conversation's last message"
    rs = by_tag["reply_status"]
    assert rs["type"] == "choice" and rs["defined_by"] == "system"
    assert rs["choices"] == ["standby", "waiting_reply", "needs_response"]


# ------------------------------------------------------------------ get_context
def test_get_context_is_active_only_by_default(tmp_path):
    out = app.get_context_impl(_state(tmp_path), law_path=_law(tmp_path), as_of=AS_OF)
    ids = {c["conversation_id"] for c in out["conversations"]}
    assert ids == {10, 20}                       # dormant 30 hidden


def test_get_context_include_dormant(tmp_path):
    out = app.get_context_impl(_state(tmp_path), law_path=_law(tmp_path), include_dormant=True, as_of=AS_OF)
    assert {c["conversation_id"] for c in out["conversations"]} == {10, 20, 30}


def test_get_context_presents_the_agent_facing_shape(tmp_path):
    """The response contract: conversation_id, humanized timestamps, no internal fields."""
    out = app.get_context_impl(_state(tmp_path), law_path=_law(tmp_path), as_of=AS_OF)
    avery = next(c for c in out["conversations"] if c["conversation_id"] == 10)
    assert avery["last_message_at"] == "May 30, 2026 10:00am"
    assert avery["identity"] == "Climbing friend."
    assert avery["daily"][0]["text"] == "Asked about Saturday."
    assert avery["unsummarized_messages"] == [
        {"when": "June 1, 2026 11:00am", "sender": "Avery", "text": "still on?"}]
    for internal in ("chat_rowid", "handle", "summarized_through", "edited", "last_updated",
                     "last_from", "texts_today", "watermark", "members", "new_conversation"):
        assert internal not in avery, internal    # members omitted for a 1:1; new_conversation when false
    assert out["generated_at"] == "June 1, 2026 12:00pm"


def test_get_context_sorts_most_recent_first(tmp_path):
    out = app.get_context_impl(_state(tmp_path), law_path=_law(tmp_path), include_dormant=True, as_of=AS_OF)
    assert [c["conversation_id"] for c in out["conversations"]] == [10, 20, 30]  # 05-30 > 05-15 > 04-01


def test_get_context_tag_filter_uses_effective_tags(tmp_path):
    # both 10 and 30 carry the stored slug, but 30's ttl has aged out -> only 10 matches
    out = app.get_context_impl(_state(tmp_path), law_path=_law(tmp_path),
                               tags=["needs-scheduling"], include_dormant=True, as_of=AS_OF)
    assert [c["conversation_id"] for c in out["conversations"]] == [10]
    assert out["conversations"][0]["tags"] == ["needs-scheduling"]   # effective, not raw


def test_get_context_reply_status_filter(tmp_path):
    out = app.get_context_impl(_state(tmp_path), law_path=_law(tmp_path),
                               reply_status="needs_response", as_of=AS_OF)
    assert {c["conversation_id"] for c in out["conversations"]} == {10}


# -------------------------------------------------------- reply_status query-time decay
def test_get_context_decays_stale_waiting_reply_to_standby(tmp_path):
    """Mom's waiting_reply is 17 days old; with a 7-day decay it presents as standby — in the
    returned value AND for filtering (never written back)."""
    out = app.get_context_impl(_state(tmp_path), law_path=_law(tmp_path),
                               reply_decay_days=7, as_of=AS_OF)
    mom = next(c for c in out["conversations"] if c["conversation_id"] == 20)
    assert mom["reply_status"] == "standby"
    out = app.get_context_impl(_state(tmp_path), law_path=_law(tmp_path),
                               reply_status="waiting_reply", reply_decay_days=7, as_of=AS_OF)
    assert out["conversations"] == []                      # decayed away from waiting_reply
    out = app.get_context_impl(_state(tmp_path), law_path=_law(tmp_path),
                               reply_status="standby", reply_decay_days=7, as_of=AS_OF)
    assert {c["conversation_id"] for c in out["conversations"]} == {20}


def test_get_context_fresh_waiting_reply_is_kept(tmp_path):
    out = app.get_context_impl(_state(tmp_path), law_path=_law(tmp_path),
                               reply_decay_days=30, as_of=AS_OF)   # 17d < 30d window
    mom = next(c for c in out["conversations"] if c["conversation_id"] == 20)
    assert mom["reply_status"] == "waiting_reply"


# ----------------------------------- unsummarized_messages derived from the raw store
def _seed_raw(tmp_path):
    raw_path = tmp_path / "raw.sqlite"
    raw_store.ingest({"conversations": [
        {"chat_rowid": 10, "name": "Avery", "handle": "+15550000010", "is_named": True,
         "is_groupchat": False, "members": None, "contact_details": None, "conversation": [
             {"message_rowid": 7, "date": 7, "datetime": "2026-05-30 10:00:00", "sender": "Avery", "text": "summarized already"},
             {"message_rowid": 8, "date": 8, "datetime": "2026-06-01 10:00:00", "sender": "me", "text": "fresh from me"},
             {"message_rowid": 9, "date": 9, "datetime": "2026-06-01 11:00:00", "sender": "Avery", "text": "fresh from them"}]}]},
        path=raw_path)
    return raw_path


def test_get_context_derives_live_messages_from_raw_store(tmp_path):
    """With a raw_path the live raw layer is computed at query time: every stored message newer than
    the conversation's summarized_through cursor — the stored (always-empty in production) field is
    ignored."""
    raw_path = _seed_raw(tmp_path)
    state_path = tmp_path / "state.json"
    data = state_io.read_state(_state(tmp_path)).model_dump()
    for c in data["conversations"]:
        if c["chat_rowid"] == 10:
            c["summarized_through"], c["texts_today"] = 7, []   # cursor at 7; stored field empty
    state_io.write_state(data, state_path)
    out = app.get_context_impl(state_path, law_path=_law(tmp_path), raw_path=raw_path, as_of=AS_OF)
    avery = next(c for c in out["conversations"] if c["conversation_id"] == 10)
    assert [m["text"] for m in avery["unsummarized_messages"]] == ["fresh from me", "fresh from them"]
    assert avery["unsummarized_messages"][0]["when"] == "June 1, 2026 10:00am"


def test_get_context_live_messages_respect_cap_and_deleted(tmp_path):
    raw_path = _seed_raw(tmp_path)
    raw_store.ingest({"conversations": [],
                      "deleted": [{"chat_rowid": 10, "message_rowid": 8}]}, path=raw_path)
    out = app.get_context_impl(_state(tmp_path), law_path=_law(tmp_path), raw_path=raw_path,
                               texts_today_cap=1, as_of=AS_OF)
    avery = next(c for c in out["conversations"] if c["conversation_id"] == 10)
    assert [m["text"] for m in avery["unsummarized_messages"]] == ["fresh from them"]  # 8 deleted; newest-1


def test_get_context_without_raw_path_presents_stored_texts_today(tmp_path):
    out = app.get_context_impl(_state(tmp_path), law_path=_law(tmp_path), as_of=AS_OF)
    avery = next(c for c in out["conversations"] if c["conversation_id"] == 10)
    assert avery["unsummarized_messages"][0]["text"] == "still on?"    # impl-level legacy behavior


# --------------------------------------------------- get_context default look-back
def test_get_context_default_lookback_filters_old(tmp_path):
    """With no explicit `since`, the server applies a default look-back window (the MCP default)."""
    out = app.get_context_impl(_state(tmp_path), law_path=_law(tmp_path),
                               default_lookback_days=7, as_of=AS_OF)              # AS_OF = 2026-06-01
    assert {c["conversation_id"] for c in out["conversations"]} == {10}   # Mom (05-15) >7d old -> dropped


def test_get_context_explicit_since_overrides_default(tmp_path):
    out = app.get_context_impl(_state(tmp_path), law_path=_law(tmp_path),
                               since="2026-05-01 00:00:00", default_lookback_days=7, as_of=AS_OF)
    assert {c["conversation_id"] for c in out["conversations"]} == {10, 20}   # explicit window keeps both


# ------------------------------------------------------------ get message history
def test_get_raw_history_presents_when_sender_text(tmp_path):
    raw_path = tmp_path / "raw.sqlite"
    raw_store.ingest({"conversations": [
        {"chat_rowid": 10, "name": "Avery", "handle": "+1", "is_named": True, "is_groupchat": False,
         "members": None, "contact_details": None, "conversation": [
             {"message_rowid": 1, "date": 100, "datetime": "2026-05-31 09:00:00",
              "sender": "Avery", "text": "yo"}]}]}, path=raw_path)
    hist = app.get_raw_history_impl(raw_path, 10)
    assert hist == [{"when": "May 31, 2026 9:00am", "sender": "Avery", "text": "yo"}]


def test_get_raw_history_include_deleted(tmp_path):
    raw_path = tmp_path / "raw.sqlite"
    raw_store.ingest({"conversations": [
        {"chat_rowid": 10, "name": "A", "handle": "+1", "is_named": True, "is_groupchat": False,
         "members": None, "contact_details": None, "conversation": [
             {"message_rowid": 1, "date": 1, "datetime": "2026-05-30 09:00:00", "sender": "A", "text": "live"},
             {"message_rowid": 2, "date": 2, "datetime": "2026-05-30 09:01:00", "sender": "A", "text": "gone"}]}]},
        path=raw_path)
    raw_store.ingest({"conversations": [], "deleted": [{"chat_rowid": 10, "message_rowid": 2}]}, path=raw_path)
    assert [m["text"] for m in app.get_raw_history_impl(raw_path, 10)] == ["live"]              # default hides
    assert [m["text"] for m in app.get_raw_history_impl(raw_path, 10, include_deleted=True)] == ["live", "gone"]


def test_get_raw_history_default_lookback_hides_old(tmp_path):
    raw_path = tmp_path / "raw.sqlite"
    raw_store.ingest({"conversations": [
        {"chat_rowid": 10, "name": "Avery", "handle": "+1", "is_named": True, "is_groupchat": False,
         "members": None, "contact_details": None, "conversation": [
             {"message_rowid": 1, "date": 1, "datetime": "2026-04-01 09:00:00", "sender": "A", "text": "old"},
             {"message_rowid": 2, "date": 2, "datetime": "2026-05-30 09:00:00", "sender": "A", "text": "new"}]}]},
        path=raw_path)
    hist = app.get_raw_history_impl(raw_path, 10, default_lookback_days=7, now=AS_OF)
    assert [m["text"] for m in hist] == ["new"]      # 04-01 is older than 7d before 06-01 -> hidden


# ----------------------------------------------------------------------- quickscan
def test_quickscan_returns_the_triage_list(tmp_path):
    raw_path = _seed_raw(tmp_path)                                   # conv 10: 3 stored messages
    rows = app.quickscan_impl(_state(tmp_path), raw_path=raw_path, as_of=AS_OF)
    by_id = {r["conversation_id"]: r for r in rows}
    assert set(by_id) == {10, 20}                                    # active only by default
    avery = by_id[10]
    assert avery["name"] == "Avery" and avery["is_group"] is False
    assert avery["message_count"] == 3
    assert avery["last_message_at"] == "May 30, 2026 10:00am"
    assert avery["reply_status"] == "needs_response"
    assert "summary" in avery and "chat_rowid" not in avery
    assert by_id[20]["message_count"] == 0                           # absent from the raw store


def test_quickscan_sorts_most_recent_first_and_decays(tmp_path):
    rows = app.quickscan_impl(_state(tmp_path), reply_decay_days=7, include_dormant=True, as_of=AS_OF)
    assert [r["conversation_id"] for r in rows] == [10, 20, 30]      # most recent first
    by_id = {r["conversation_id"]: r for r in rows}
    assert by_id[20]["reply_status"] == "standby"                    # 17d-old waiting_reply decayed


# ----------------------------------------------------------- update_conversation
def test_update_conversation_stamps_edited_and_replaces_tags(tmp_path):
    state_path = _state(tmp_path)
    out = app.update_conversation_impl(state_path, conversation=10, law_path=_law(tmp_path),
                                       fields={"identity": "Old college friend.", "tags": ["family"]})
    assert out["conversation_id"] == 10
    after = state_io.read_state(state_path)
    conv = next(c for c in after.conversations if c.chat_rowid == 10)
    assert conv.identity == "Old college friend."
    assert conv.tags == ["family"]                    # full-replace
    assert conv.edited.get("identity") and conv.edited.get("tags")
    assert [d.text for d in conv.daily] == ["Asked about Saturday."]   # daily untouched


def test_update_conversation_rejects_out_of_law_tags(tmp_path):
    state_path = _state(tmp_path)
    with pytest.raises(ValueError):
        app.update_conversation_impl(state_path, conversation=10, law_path=_law(tmp_path),
                                     fields={"tags": ["not-a-real-tag"]})


def test_update_conversation_rejects_protected_fields(tmp_path):
    state_path = _state(tmp_path)
    for protected in ("daily", "reply_status", "texts_today", "chat_rowid"):
        with pytest.raises(ValueError):
            app.update_conversation_impl(state_path, conversation=10, law_path=_law(tmp_path),
                                         fields={protected: "x"})


# -------------------------------------------------------------------- auth / ingest
def test_authorize_open_when_no_token():
    assert app.authorize(None, None) is True            # local loopback / Mac mini: no token set


def test_authorize_checks_bearer():
    assert app.authorize("Bearer s3cret", "s3cret") is True
    assert app.authorize("Bearer wrong", "s3cret") is False
    assert app.authorize(None, "s3cret") is False


def test_ingest_impl_writes_to_store(tmp_path):
    raw_path = tmp_path / "raw.sqlite"
    n = app.ingest_impl({"conversations": [
        {"chat_rowid": 5, "name": "X", "handle": "+1", "is_named": True, "is_groupchat": False,
         "members": None, "contact_details": None, "conversation": [
             {"message_rowid": 1, "date": 100, "datetime": "2026-05-31 09:00:00",
              "sender": "X", "text": "hi"}]}]}, raw_path=raw_path)
    assert n == 1
    assert app.get_raw_history_impl(raw_path, 5)[0]["text"] == "hi"


# ---------------------------------------------- the FastMCP wiring (needs the optional `fastmcp` extra)
def test_build_app_registers_the_tools_and_routes(tmp_path):
    import asyncio

    pytest.importorskip("fastmcp")
    from text_triage.config import Config

    mcp = app.build_app(Config(), state_path=tmp_path / "s.json", raw_path=tmp_path / "r.sqlite",
                        law_path=tmp_path / "w.md", ingest_token="t", mcp_key="k")
    names = {t.name for t in asyncio.run(mcp.list_tools())}
    assert names == {"list_available_tags", "scan_conversations", "get_conversation_context",
                     "get_message_history", "update_conversation_memory"}
    routes = {r.path for r in mcp._additional_http_routes}
    assert routes == {"/ingest", "/trigger", "/health"}


def test_build_app_tool_descriptions_steer_the_call_order(tmp_path):
    """The tool descriptions are the agent's only manual: list_available_tags must say it comes
    first; every reading tool must name the response shape it returns."""
    import asyncio

    pytest.importorskip("fastmcp")
    from text_triage.config import Config

    mcp = app.build_app(Config(), state_path=tmp_path / "s.json", raw_path=tmp_path / "r.sqlite",
                        law_path=tmp_path / "w.md", ingest_token="t", mcp_key="k")
    desc = {t.name: (t.description or "") for t in asyncio.run(mcp.list_tools())}
    assert "first" in desc["list_available_tags"].lower()
    assert "conversation_id" in desc["scan_conversations"]
    assert "conversation_id" in desc["get_conversation_context"]
    assert "when" in desc["get_message_history"]


def test_build_app_auth_verifies_the_configured_key(tmp_path):
    """A valid bearer token must verify without raising — the StaticTokenVerifier claims have to carry
    `client_id`, else every AUTHENTICATED request 500s (KeyError: 'client_id') and an MCP client like
    Poke reads that as an invalid server. A non-matching token verifies to None (a clean 401)."""
    import asyncio

    pytest.importorskip("fastmcp")
    from text_triage.config import Config

    mcp = app.build_app(Config(), state_path=tmp_path / "s.json", raw_path=tmp_path / "r.sqlite",
                        law_path=tmp_path / "w.md", ingest_token="t", mcp_key="secret-key")
    token = asyncio.run(mcp.auth.verify_token("secret-key"))
    assert token is not None and token.client_id        # no KeyError building the AccessToken
    assert asyncio.run(mcp.auth.verify_token("wrong-key")) is None
