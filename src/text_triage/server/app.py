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


# ----------------------------------------------------- the agent-facing presentation
def _fmt_dt(s: Optional[str]) -> Optional[str]:
    """Humanize a stored ``YYYY-MM-DD HH:MM:SS`` timestamp into "May 30, 2026 10:00am" — the one
    timestamp format every MCP response uses. Lenient: a missing or unparseable value passes
    through unchanged."""
    if not s:
        return s
    try:
        d = datetime.datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return s
    clock = d.strftime("%I:%M%p").lstrip("0").lower()
    return f"{d.strftime('%B')} {d.day}, {d.year} {clock}"


def _present_message(m: dict) -> dict:
    """A raw message as the agent sees it: {when, sender, text}. ``sender`` is "me" for the account
    owner. Internal identifiers (message_rowid) never leave the server."""
    return {"when": _fmt_dt(m.get("datetime")), "sender": m.get("sender"), "text": m.get("text")}


def _present_conversation(c: dict, *, reply_status: str, tags: list[str],
                          live_messages: list[dict]) -> dict:
    """One conversation as the agent sees it. ``conversation_id`` is the stable key for the other
    tools; internal fields (chat_rowid/handle/cursors/edited stamps) are dropped; timestamps are
    humanized. ``members`` appears only for groups; ``new_conversation`` only when true (too few
    texts to have summaries yet)."""
    out = {
        "conversation_id": c["chat_rowid"],
        "name": c["name"],
        "is_group": c["is_group"],
        "status": c["status"],
        "reply_status": reply_status,
        "last_message_at": _fmt_dt(c.get("last_message_at")),
        "you_last_sent": _fmt_dt(c.get("last_from_me_at")),
        "they_last_sent": _fmt_dt(c.get("last_from_them_at")),
        "summary": c.get("summary"),
        "identity": c.get("identity"),
        "tags": tags,
        "daily": c.get("daily") or [],
        "weekly": c.get("weekly") or [],
        "monthly": c.get("monthly"),
        "history": c.get("history") or [],
        "unsummarized_messages": [_present_message(m) for m in live_messages],
    }
    if c["is_group"]:
        out["members"] = c.get("members")
    if c.get("new_conversation"):
        out["new_conversation"] = True
    return out


# --------------------------------------------------------------- tool logic (pure)
def list_tags_impl(*, law_path: Optional[Union[str, Path]] = None) -> list[dict]:
    """The active tag vocabulary — the union of the hard-coded system law (choice classifications
    like ``reply_status``) and the watch.md user law, presented for an agent: per entry ``tag``,
    ``description``, ``type`` ("freeform" | "choice", choice adds ``choices``), ``defined_by``
    ("user" | "system"), and a plain-English ``relevance``."""
    law = tagmod.full_law(tagmod.load_law(law_path))
    out = []
    for s in law.values():
        row = {
            "tag": s.slug,
            "type": s.kind,
            "defined_by": s.origin,
            "description": s.description,
            "relevance": ("always relevant" if s.lifetime == "sticky" else
                          f"relevant for ~{s.ttl_days} days after the conversation's last message"),
        }
        if s.kind == "choice":
            row["choices"] = s.choices
        out.append(row)
    return out


def get_context_impl(state_path: Union[str, Path], *, law_path: Optional[Union[str, Path]] = None,
                     raw_path: Optional[Union[str, Path]] = None,
                     conversation_id: Optional[int] = None,
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
    if conversation_id is not None:          # a direct fetch by key: no windowing, dormant included
        since, default_lookback_days, include_dormant = None, None, True
    since = _resolve_since(since, default_lookback_days, as_of)
    state = state_io.read_state(state_path)
    law = tagmod.load_law(law_path)
    want = set(tags) if tags else None
    matched = []
    for c in state.model_dump()["conversations"]:
        if conversation_id is not None and c["chat_rowid"] != conversation_id:
            continue
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
        if raw_path is not None:             # the live raw layer, derived at query time
            live = raw_store.history(c["chat_rowid"], after_rowid=c.get("summarized_through") or 0,
                                     path=raw_path)
            live = live[-texts_today_cap:] if texts_today_cap else live
        else:
            live = c.get("texts_today") or []
        matched.append((c, eff_rs, eff, live))
    matched.sort(key=lambda t: t[0].get("last_message_at") or "", reverse=True)  # most recent first
    return {"generated_at": _fmt_dt(state.generated_at),
            "conversations": [_present_conversation(c, reply_status=rs, tags=eff, live_messages=lv)
                              for c, rs, eff, lv in matched]}


def quickscan_impl(state_path: Union[str, Path], raw_path: Optional[Union[str, Path]] = None, *,
                   reply_decay_days: Optional[int] = None, include_dormant: bool = False,
                   as_of=None) -> list[dict]:
    """The fast triage list: one small row per conversation — name, total stored message count,
    most-recent message time, the (decayed) reply status, and the 1-2 line summary. Active only
    unless ``include_dormant``."""
    state = state_io.read_state(state_path)
    n_by_id = raw_store.counts(path=raw_path) if raw_path is not None else {}
    rows = [c for c in state.conversations if c.status == "active" or include_dormant]
    rows.sort(key=lambda c: c.last_message_at or "", reverse=True)   # most recent first
    return [{
        "conversation_id": c.chat_rowid,
        "name": c.name,
        "is_group": c.is_group,
        "message_count": n_by_id.get(c.chat_rowid, 0),
        "last_message_at": _fmt_dt(c.last_message_at),
        "reply_status": decayed_reply_status(c.reply_status, c.last_message_at,
                                             decay_days=reply_decay_days or 0, as_of=as_of),
        "summary": c.summary,
    } for c in rows]


def get_raw_history_impl(raw_path: Union[str, Path], conversation: int, *,
                         since: Optional[str] = None, default_lookback_days: Optional[int] = None,
                         include_deleted: bool = False, now: Optional[datetime.datetime] = None) -> list[dict]:
    """The sparing deep-dive: one conversation's recent raw messages from the raw store, presented
    as ``{when, sender, text}`` rows (oldest first). With no ``since`` the ``default_lookback_days``
    window applies (the MCP default). Deleted/unsent messages are hidden unless ``include_deleted``."""
    since = _resolve_since(since, default_lookback_days, now)
    rows = raw_store.history(conversation, since=since, include_deleted=include_deleted, path=raw_path)
    return [_present_message(m) for m in rows]


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
    return {"conversation_id": conversation, "edited": sorted(fields)}


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
    mcp = FastMCP("text-triage", auth=auth, instructions="""\
text-triage maintains a curated, LLM-summarized memory of the account owner's iMessage
conversations. You read that memory; you never see or send texts on their behalf.

Recommended call order:
1. list_available_tags — ALWAYS first: learn the tag vocabulary and the reply_status states before
   filtering or interpreting anything.
2. scan_conversations — the cheap triage list over every conversation.
3. get_conversation_context — the full layered memory for the conversations that matter, filterable
   by tags / reply_status.
4. get_message_history — raw texts of ONE conversation; use sparingly, only when the notes aren't enough.
5. update_conversation_memory — only when the owner explicitly corrects something.

Response conventions (identical across all tools):
- conversation_id is the stable key — pass it to get_message_history / update_conversation_memory.
- All timestamps read like "May 7, 2026 12:35pm". Input filters (`since`) instead take ISO
  "YYYY-MM-DD HH:MM:SS".
- Messages are {when, sender, text}; sender "me" is the account owner.
- reply_status is one of: needs_response (the owner owes them a reply), waiting_reply (they owe the
  owner), standby (nothing owed either way).
- unsummarized_messages are texts newer than the conversation's last summary — the live layer.""")

    lookback = config.server.mcp_default_lookback_days

    @mcp.tool
    def list_available_tags() -> list:
        """Call this FIRST, before any other tool: the complete tag vocabulary used everywhere else.
        Returns one entry per tag: `tag` (the value used in filters), `description`,
        `type` ("freeform" = may appear in a conversation's tags list; "choice" = a one-of
        classification such as reply_status, whose `choices` lists the allowed values),
        `defined_by` ("user" tags come from the owner's watch.md; "system" tags are built in),
        and `relevance` (when the tag applies)."""
        return list_tags_impl(law_path=law_path)

    @mcp.tool
    def scan_conversations(include_dormant: bool = False) -> list:
        """The fast triage list — one small row per conversation, most recent first. Use this to get
        the lay of the land before drilling in. Each row: `conversation_id` (the key for the other
        tools), `name`, `is_group`, `message_count` (total stored), `last_message_at`,
        `reply_status`, and `summary` (a 1-2 line current snapshot). Conversations quiet for over a
        month are hidden unless `include_dormant` is true."""
        return quickscan_impl(state_path, raw_path,
                              reply_decay_days=config.messages.reply_decay_days,
                              include_dormant=include_dormant)

    @mcp.tool
    def get_conversation_context(conversation_id: Optional[int] = None,
                                 tags: Optional[list] = None, since: Optional[str] = None,
                                 reply_status: Optional[str] = None,
                                 include_dormant: bool = False) -> dict:
        """The full layered memory for conversations, most recent first. Two ways to call it:
        pass `conversation_id` (from scan_conversations) to fetch ONE conversation directly —
        no other filters needed — or filter the whole set with `tags` (any-of, values from
        list_available_tags), `reply_status` (needs_response / waiting_reply / standby), and
        `since` (ISO "YYYY-MM-DD HH:MM:SS"; without it only the last ~60 days return). Each
        match: `conversation_id`, `name`, `reply_status`, `last_message_at` / `you_last_sent` /
        `they_last_sent`, `summary` (1-2 lines), `identity` (who they are to the owner), `tags`,
        the layered notes (`daily` / `weekly` / `monthly` / `history`), and
        `unsummarized_messages` ({when, sender, text} texts newer than the last summary).
        Prefer this over get_message_history."""
        return get_context_impl(state_path, law_path=law_path, raw_path=raw_path,
                                conversation_id=conversation_id, tags=tags,
                                since=since, reply_status=reply_status,
                                include_dormant=include_dormant, default_lookback_days=lookback,
                                reply_decay_days=config.messages.reply_decay_days,
                                texts_today_cap=config.server.texts_today_cap)

    @mcp.tool
    def get_message_history(conversation_id: int, since: Optional[str] = None,
                            include_deleted: bool = False) -> list:
        """The raw text messages of ONE conversation — the deep dive for when the summarized context
        isn't enough. Returns {when, sender, text} rows, oldest first; sender "me" is the account
        owner; "[Reacted: ...]" rows are tapbacks, not substantive messages. `since` is ISO
        "YYYY-MM-DD HH:MM:SS" (without it, only the last ~60 days). Messages the owner deleted are
        hidden unless `include_deleted` is true."""
        return get_raw_history_impl(raw_path, conversation_id, since=since,
                                    default_lookback_days=lookback,
                                    include_deleted=include_deleted)

    @mcp.tool
    def update_conversation_memory(conversation_id: int, fields: dict) -> dict:
        """Correct a conversation's stored memory when the OWNER says it's wrong — never on your own
        judgment. `fields` may set: `identity` (str, <= 3 sentences), `monthly` (str), `weekly` /
        `history` (lists of dated notes), or `tags` (a FULL replacement list; every value must come
        from list_available_tags). Edited fields are stamped so future summaries never overwrite
        them. Returns {conversation_id, edited}."""
        return update_conversation_impl(state_path, conversation=conversation_id, fields=fields,
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
