"""The server's serving surface — MCP tools over Streamable HTTP + the collector/admin routes.

Runs on the always-on host (a VPS or an always-on Mac mini). It OWNS ``state.json`` (the only writer)
and the raw store, and serves them to MCP clients (Poke, a local client). Four MCP tools:
``list_tags`` / ``get_context`` / ``get_raw_history`` / ``update_conversation``; three HTTP routes:
``POST /ingest`` (the collector's raw push), ``POST /trigger`` (run a summary now), ``GET /health``.

The tool/route LOGIC lives in module-level ``*_impl`` functions so it's unit-testable with no socket.
``fastmcp`` is imported lazily inside :func:`build_app`/:func:`run_server` (like the engine backends),
so importing this module and the whole test suite need neither ``fastmcp`` nor a network.
"""
from __future__ import annotations

import datetime
import os
from pathlib import Path
from typing import Callable, Optional, Union

from text_triage.config import Config, load_config
from text_triage.server import raw_store
from text_triage.state import state_io
from text_triage.triage import tags as tagmod
from text_triage.triage.skeleton import decayed_reply_status

__all__ = ["build_app", "run_server", "authorize",
           "list_tags_impl", "get_context_impl", "get_raw_history_impl",
           "update_conversation_impl", "quickscan_impl", "ingest_impl"]

# Fields a human / the write-agent may edit via MCP. Facts, daily, and the live raw layer are off-limits.
_WRITABLE = {"identity", "monthly", "weekly", "history", "tags"}


def _resolve_since(since: Optional[str], default_lookback_days: Optional[int],
                   base: Optional[datetime.datetime]) -> Optional[str]:
    """The MCP default look-back: a client that passes no ``since`` only sees the last
    ``default_lookback_days`` days (the deep store keeps all; ask for older with an explicit since).
    ``0``/``None`` = no default. ``base`` is the reference 'now' (injectable for tests)."""
    if since is not None or not default_lookback_days:
        return since
    base = base or datetime.datetime.now()
    return (base - datetime.timedelta(days=default_lookback_days)).strftime("%Y-%m-%d %H:%M:%S")


# --------------------------------------------------------------- tool logic (pure)
def list_tags_impl(*, law_path: Optional[Union[str, Path]] = None) -> list[dict]:
    """The active tag law — the filter vocabulary MCP clients may use. The union of the hard-coded
    system law (choice classifications like ``reply_status``) and the watch.md user law."""
    law = tagmod.full_law(tagmod.load_law(law_path))
    return [{"slug": s.slug, "description": s.description, "lifetime": s.lifetime,
             "ttl_days": s.ttl_days, "kind": s.kind, "choices": s.choices, "origin": s.origin}
            for s in law.values()]


def get_context_impl(state_path: Union[str, Path], *, law_path: Optional[Union[str, Path]] = None,
                     raw_path: Optional[Union[str, Path]] = None,
                     tags: Optional[list[str]] = None, since: Optional[str] = None,
                     reply_status: Optional[str] = None, include_dormant: bool = False,
                     default_lookback_days: Optional[int] = None,
                     reply_decay_days: Optional[int] = None,
                     texts_today_cap: Optional[int] = None, as_of=None) -> dict:
    """The layered memory for matching conversations (notes + tags + ``texts_today``). Tag filtering
    uses :func:`effective_tags` (currently-relevant), not the raw stored slugs; defaults to ``active``
    only. ``reply_status`` filters on (and every record surfaces) the query-time DECAYED status — a
    ``waiting_reply`` older than ``reply_decay_days`` presents as ``standby``, never written back.
    With a ``raw_path``, each match's ``texts_today`` is derived live from the raw store (messages
    newer than its ``summarized_through`` cursor, newest ``texts_today_cap`` kept); without one the
    stored field passes through. ``since`` keeps conversations whose ``last_message_at`` is at/after
    that moment; with no ``since`` the ``default_lookback_days`` window applies (the MCP default)."""
    since = _resolve_since(since, default_lookback_days, as_of)
    state = state_io.read_state(state_path)
    law = tagmod.load_law(law_path)
    want = set(tags) if tags else None
    out = []
    for c in state.model_dump()["conversations"]:
        if c["status"] != "active" and not include_dormant:
            continue
        eff_rs = decayed_reply_status(c["reply_status"], c.get("last_message_at"),
                                      decay_days=reply_decay_days or 0, as_of=as_of)
        if reply_status is not None and eff_rs != reply_status:
            continue
        if since is not None and (c.get("last_message_at") or "") < since:
            continue
        eff = tagmod.effective_tags(c, law, as_of=as_of)
        if want is not None and not (want & set(eff)):
            continue
        c["tags"] = eff                      # surface the effective tags, not the raw stored slugs
        c["reply_status"] = eff_rs           # surface the decayed status, not the raw stored one
        if raw_path is not None:             # the live raw layer, derived at query time
            live = raw_store.history(c["chat_rowid"], after_rowid=c.get("summarized_through") or 0,
                                     path=raw_path)
            c["texts_today"] = live[-texts_today_cap:] if texts_today_cap else live
        out.append(c)
    return {"generated_at": state.generated_at, "conversations": out}


def quickscan_impl(state_path: Union[str, Path], raw_path: Optional[Union[str, Path]] = None, *,
                   reply_decay_days: Optional[int] = None, include_dormant: bool = False,
                   as_of=None) -> list[dict]:
    """The fast triage list: one small row per conversation — name, total stored message count,
    most-recent message time, the (decayed) reply status, and the 1-2 line summary. Active only
    unless ``include_dormant``."""
    state = state_io.read_state(state_path)
    n_by_id = raw_store.counts(path=raw_path) if raw_path is not None else {}
    out = []
    for c in state.conversations:
        if c.status != "active" and not include_dormant:
            continue
        out.append({
            "chat_rowid": c.chat_rowid,
            "name": c.name,
            "is_group": c.is_group,
            "message_count": n_by_id.get(c.chat_rowid, 0),
            "last_message_at": c.last_message_at,
            "reply_status": decayed_reply_status(c.reply_status, c.last_message_at,
                                                 decay_days=reply_decay_days or 0, as_of=as_of),
            "summary": c.summary,
        })
    return out


def get_raw_history_impl(raw_path: Union[str, Path], conversation: int, *,
                         since: Optional[str] = None, default_lookback_days: Optional[int] = None,
                         include_deleted: bool = False, now: Optional[datetime.datetime] = None) -> list[dict]:
    """The sparing deep-dive: one conversation's recent raw messages from the raw store. With no
    ``since`` the ``default_lookback_days`` window applies (the MCP default). Deleted/unsent messages are
    hidden unless ``include_deleted`` is set."""
    since = _resolve_since(since, default_lookback_days, now)
    return raw_store.history(conversation, since=since, include_deleted=include_deleted, path=raw_path)


def update_conversation_impl(state_path: Union[str, Path], *, conversation: int, fields: dict,
                             law_path: Optional[Union[str, Path]] = None, stamp: str = "user") -> dict:
    """The human / write-agent correction path. Only :data:`_WRITABLE` fields; ``tags`` is a full
    replace and must be ⊆ the law; each touched field is recorded in ``edited`` so re-derives don't
    clobber it. Never touches facts, ``daily``, or ``texts_today``. The server is the single writer."""
    bad = set(fields) - _WRITABLE
    if bad:
        raise ValueError(f"fields not writable via MCP: {sorted(bad)} (allowed: {sorted(_WRITABLE)})")
    law = tagmod.load_law(law_path)
    if "tags" in fields and law:
        out_of_law = set(fields["tags"]) - tagmod.active_slugs(law)
        if out_of_law:
            raise ValueError(f"tags not in the law: {sorted(out_of_law)}")
    with state_io.state_lock(state_path):
        data = state_io.read_state(state_path).model_dump()
        target = next((c for c in data["conversations"] if c["chat_rowid"] == conversation), None)
        if target is None:
            raise KeyError(f"no conversation with chat_rowid={conversation}")
        for k, v in fields.items():
            target[k] = v
            target.setdefault("edited", {})[k] = stamp
        state_io.write_state(data, state_path, law=tagmod.active_slugs(law) or None)
    return {"chat_rowid": conversation, "edited": sorted(fields)}


def ingest_impl(payload: dict, *, raw_path: Union[str, Path]) -> int:
    """Persist a pushed extractor export into the raw store; returns the count of NEW messages."""
    return raw_store.ingest(payload, path=raw_path)


def authorize(header_value: Optional[str], expected_token: Optional[str]) -> bool:
    """Bearer check for the collector routes. No configured token = open (local loopback / Mac mini)."""
    if not expected_token:
        return True
    return header_value == f"Bearer {expected_token}"


# ------------------------------------------------------------------ server wiring
def build_app(config: Config, *, state_path: Union[str, Path], raw_path: Union[str, Path],
              law_path: Optional[Union[str, Path]] = None, ingest_token: Optional[str] = None,
              mcp_key: Optional[str] = None, on_trigger: Optional[Callable[[str], object]] = None):
    """Construct the FastMCP app: the four tools + the ``/ingest`` ``/trigger`` ``/health`` routes,
    all closing over the resolved paths/secrets. ``fastmcp`` is imported here, not at module load."""
    from fastmcp import FastMCP
    from starlette.responses import JSONResponse, PlainTextResponse

    auth = None
    if mcp_key:
        from fastmcp.server.auth import StaticTokenVerifier
        # Each token's claims MUST carry client_id — StaticTokenVerifier builds the AccessToken with
        # token_data["client_id"], so omitting it 500s every authenticated request (KeyError).
        auth = StaticTokenVerifier(tokens={mcp_key: {"client_id": "mcp-client", "scopes": []}})
    mcp = FastMCP("text-triage", auth=auth)

    @mcp.tool
    def list_tags() -> list:
        """List the active tag law (slug, description, lifetime, ttl_days)."""
        return list_tags_impl(law_path=law_path)

    lookback = config.server.mcp_default_lookback_days

    @mcp.tool
    def get_context(tags: Optional[list] = None, since: Optional[str] = None,
                    reply_status: Optional[str] = None, include_dormant: bool = False) -> dict:
        """The layered memory for matching conversations. Tag-filtered by current relevance; active-only
        by default. `reply_status` filters on one of: needs_response (you owe them a reply),
        waiting_reply (they owe you one), standby (nothing owed; stale waiting_reply decays here).
        Each match carries its live unsummarized messages in `texts_today`. With no `since`, returns
        the last `server.mcp_default_lookback_days` days."""
        return get_context_impl(state_path, law_path=law_path, raw_path=raw_path, tags=tags,
                                since=since, reply_status=reply_status,
                                include_dormant=include_dormant, default_lookback_days=lookback,
                                reply_decay_days=config.messages.reply_decay_days,
                                texts_today_cap=config.server.texts_today_cap)

    @mcp.tool
    def quickscan(include_dormant: bool = False) -> list:
        """The fast triage list: per conversation — name, total message count, most-recent message
        time, reply_status (needs_response / waiting_reply / standby), and the 1-2 line summary."""
        return quickscan_impl(state_path, raw_path,
                              reply_decay_days=config.messages.reply_decay_days,
                              include_dormant=include_dormant)

    @mcp.tool
    def get_raw_history(conversation: int, since: Optional[str] = None,
                        include_deleted: bool = False) -> list:
        """One conversation's recent raw messages (the deep-dive when the notes aren't enough). With no
        `since`, returns the last `server.mcp_default_lookback_days` days. Deleted/unsent messages are
        hidden unless `include_deleted` is true."""
        return get_raw_history_impl(raw_path, conversation, since=since, default_lookback_days=lookback,
                                    include_deleted=include_deleted)

    @mcp.tool
    def update_conversation(conversation: int, fields: dict) -> dict:
        """Correct a conversation's identity/monthly/weekly/history/tags; stamps ``edited``."""
        return update_conversation_impl(state_path, conversation=conversation, fields=fields,
                                        law_path=law_path)

    @mcp.custom_route("/ingest", methods=["POST"])
    async def ingest_route(request):
        if not authorize(request.headers.get("authorization"), ingest_token):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        n = ingest_impl(await request.json(), raw_path=raw_path)
        return JSONResponse({"ingested": n})

    @mcp.custom_route("/trigger", methods=["POST"])
    async def trigger_route(request):
        if not authorize(request.headers.get("authorization"), ingest_token):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        body = await request.json() if (await request.body()) else {}
        mode = body.get("mode", "daily")
        if on_trigger is not None:
            on_trigger(mode)
        return JSONResponse({"triggered": mode})

    @mcp.custom_route("/health", methods=["GET"])
    async def health_route(request):
        return PlainTextResponse("ok")

    return mcp


def run_server(config: Optional[Config] = None, *, state_path: Optional[Union[str, Path]] = None,
               raw_path: Optional[Union[str, Path]] = None, law_path: Optional[Union[str, Path]] = None,
               config_path: Optional[Union[str, Path]] = None,
               on_trigger: Optional[Callable[[str], object]] = None,
               start_scheduler: bool = True) -> None:
    """Resolve paths/secrets, start the cadence scheduler (a daemon thread that spawns the summary
    worker as a separate process), and serve over Streamable HTTP at ``server.bind``. Secrets come from
    the environment (loaded from ``.env`` by the CLI): ``TEXT_TRIAGE_INGEST_TOKEN`` / ``TEXT_TRIAGE_MCP_KEY``."""
    import threading

    from text_triage.server import scheduler

    if config is None:
        config = load_config()
    home = Path.home() / ".text-triage"
    state_path = state_path or home / "state.json"
    raw_path = raw_path or home / "raw_messages.sqlite"

    # /trigger and the timed scheduler both run the summary worker out-of-process (raw-store source),
    # capped per run at server.bootstrap_limit (0 = uncapped).
    limit = config.server.bootstrap_limit or None
    if on_trigger is None:
        on_trigger = scheduler.make_trigger(state_path=state_path, raw_path=raw_path,
                                            config_path=config_path, watch_path=law_path, limit=limit)
    if start_scheduler:
        threading.Thread(
            target=scheduler.run_loop, args=(config,), daemon=True,
            kwargs=dict(state_path=state_path, raw_path=raw_path, config_path=config_path,
                        watch_path=law_path, last_runs_path=Path(raw_path).parent / "scheduler.json",
                        limit=limit),
        ).start()

    app = build_app(
        config, state_path=state_path, raw_path=raw_path, law_path=law_path,
        ingest_token=os.environ.get("TEXT_TRIAGE_INGEST_TOKEN"),
        mcp_key=os.environ.get("TEXT_TRIAGE_MCP_KEY"), on_trigger=on_trigger,
    )
    host, _, port = config.server.bind.rpartition(":")
    app.run(transport="http", host=host or "127.0.0.1", port=int(port))
