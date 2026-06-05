"""server/app.py — the MCP serving surface (FastMCP) + the /ingest /trigger /health routes.

The FastMCP wiring is lazy-imported (like the engine backends), so these tests drive the tool LOGIC
and the auth check directly — no socket, no fastmcp install needed. The thin transport layer is proven
by the manual round-trip in the milestone verification. State is the single owner here (state_io).
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
        "last_message_at": "2026-05-30 10:00:00", "needs_reply": True,
        "identity": None, "tags": [], "daily": [], "weekly": [], "monthly": None,
        "history": [], "texts_today": [], "edited": {},
    }
    base.update(over)
    return base


def _state(tmp_path):
    """A 3-conversation state: an active 1:1 owed a reply (recent ttl tag), an active family 1:1
    (no reply needed), and a dormant 1:1 whose ttl tag has aged out."""
    data = {
        "generated_at": "2026-06-01 12:00:00",
        "watermark": {"max_date_raw": 1, "max_message_rowid": 9},
        "unresponded": [],
        "conversations": [
            _conv(10, "Avery", tags=["needs-scheduling"], needs_reply=True, identity="Climbing friend.",
                  daily=[{"date": "2026-05-30", "text": "Asked about Saturday."}],
                  texts_today=[{"message_rowid": 9, "datetime": "2026-06-01 11:00:00",
                                "sender": "Avery", "text": "still on?"}]),
            _conv(20, "Mom", tags=["family"], needs_reply=False, last_from="me",
                  last_message_at="2026-05-15 09:00:00"),
            _conv(30, "Old Coworker", tags=["needs-scheduling"], status="dormant",
                  last_message_at="2026-04-01 10:00:00"),  # 60d ago -> ttl(7) expired
        ],
    }
    path = tmp_path / "state.json"
    state_io.write_state(data, path, law={"family", "needs-scheduling"})
    return path


# ------------------------------------------------------------------- list_tags
def test_list_tags_returns_the_law(tmp_path):
    out = app.list_tags_impl(law_path=_law(tmp_path))
    by_slug = {t["slug"]: t for t in out}
    assert set(by_slug) == {"family", "needs-scheduling"}
    assert by_slug["family"]["lifetime"] == "sticky"
    assert by_slug["needs-scheduling"]["lifetime"] == "ttl"
    assert by_slug["needs-scheduling"]["ttl_days"] == 7


# ------------------------------------------------------------------ get_context
def test_get_context_is_active_only_by_default(tmp_path):
    out = app.get_context_impl(_state(tmp_path), law_path=_law(tmp_path), as_of=AS_OF)
    ids = {c["chat_rowid"] for c in out["conversations"]}
    assert ids == {10, 20}                       # dormant 30 hidden


def test_get_context_include_dormant(tmp_path):
    out = app.get_context_impl(_state(tmp_path), law_path=_law(tmp_path), include_dormant=True, as_of=AS_OF)
    assert {c["chat_rowid"] for c in out["conversations"]} == {10, 20, 30}


def test_get_context_tag_filter_uses_effective_tags(tmp_path):
    # both 10 and 30 carry the stored slug, but 30's ttl has aged out -> only 10 matches
    out = app.get_context_impl(_state(tmp_path), law_path=_law(tmp_path),
                               tags=["needs-scheduling"], include_dormant=True, as_of=AS_OF)
    assert [c["chat_rowid"] for c in out["conversations"]] == [10]
    assert out["conversations"][0]["tags"] == ["needs-scheduling"]   # effective, not raw


def test_get_context_needs_reply_filter(tmp_path):
    out = app.get_context_impl(_state(tmp_path), law_path=_law(tmp_path), needs_reply=True, as_of=AS_OF)
    assert {c["chat_rowid"] for c in out["conversations"]} == {10}


def test_get_context_carries_notes_and_texts_today(tmp_path):
    out = app.get_context_impl(_state(tmp_path), law_path=_law(tmp_path), as_of=AS_OF)
    avery = next(c for c in out["conversations"] if c["chat_rowid"] == 10)
    assert avery["identity"] == "Climbing friend."
    assert avery["daily"][0]["text"] == "Asked about Saturday."
    assert avery["texts_today"][0]["text"] == "still on?"


# --------------------------------------------------- get_context default look-back
def test_get_context_default_lookback_filters_old(tmp_path):
    """With no explicit `since`, the server applies a default look-back window (the MCP default)."""
    out = app.get_context_impl(_state(tmp_path), law_path=_law(tmp_path),
                               default_lookback_days=7, as_of=AS_OF)              # AS_OF = 2026-06-01
    assert {c["chat_rowid"] for c in out["conversations"]} == {10}   # Mom (05-15) is >7d old -> dropped


def test_get_context_explicit_since_overrides_default(tmp_path):
    out = app.get_context_impl(_state(tmp_path), law_path=_law(tmp_path),
                               since="2026-05-01 00:00:00", default_lookback_days=7, as_of=AS_OF)
    assert {c["chat_rowid"] for c in out["conversations"]} == {10, 20}   # explicit window keeps both


# -------------------------------------------------------------- get_raw_history
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
def test_get_raw_history_reads_the_store(tmp_path):
    raw_path = tmp_path / "raw.sqlite"
    raw_store.ingest({"conversations": [
        {"chat_rowid": 10, "name": "Avery", "handle": "+1", "is_named": True, "is_groupchat": False,
         "members": None, "contact_details": None, "conversation": [
             {"message_rowid": 1, "date": 100, "datetime": "2026-05-31 09:00:00",
              "sender": "Avery", "text": "yo"}]}]}, path=raw_path)
    hist = app.get_raw_history_impl(raw_path, 10)
    assert hist[0]["text"] == "yo"


# ----------------------------------------------------------- update_conversation
def test_update_conversation_stamps_edited_and_replaces_tags(tmp_path):
    state_path = _state(tmp_path)
    app.update_conversation_impl(state_path, conversation=10, law_path=_law(tmp_path),
                                 fields={"identity": "Old college friend.", "tags": ["family"]})
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
    for protected in ("daily", "needs_reply", "texts_today", "chat_rowid"):
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
    assert names == {"list_tags", "get_context", "get_raw_history", "update_conversation"}
    routes = {r.path for r in mcp._additional_http_routes}
    assert routes == {"/ingest", "/trigger", "/health"}


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
