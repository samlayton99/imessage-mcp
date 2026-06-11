# text-triage — Current Roadmap

Path from "built + tested" to "anyone can install it." Ordered by what gates a clean v1 release.

**Status:** full pipeline built, 250+ tests green. Milestone 6 (the v1 feature core) landed: the
tag/classification law (system tags + the `reply_status` choice classification with query-time decay),
the per-conversation `summary` one-liner + `quickscan` MCP tool, deterministic reply metadata,
watch.md's 3-section split (Who am I / What to watch / What I care about) with who-am-I + current
date injected into every prompt, `texts_today` derived live from the raw store at MCP read, and
`scripts/run_local.sh` (the one-script local/Mac-mini deploy). The LLM interpreter is deferred to
dogfooding (deterministic law parse stays; the TagSpec contract is interpreter-ready).
**Next: run `scripts/run_local.sh` and dogfood on real texts** (VPS or Mac mini later — same script).

## Roadmap

1. **Validate the live loop** — DONE. Storage mirror, spam floor, backfill/admission, bootstrap monthly
   + cap, forward-only boot, the delta gate, count-based `new_conversation`, and the MCP 60-day default
   all confirmed on real data.
2. **Dogfood deploy: laptop → VPS, live on real texts** ← **NEXT.** Stand up a VPS, run `serve` there,
   push from your laptop, point Poke at it — and live on it for a few weeks of real-use tuning. The
   immediate priority; it gates all the hands-on iteration below. See **Deployment**.
3. **Feature changes — v1 core** — the tag/classification system + the deterministic wins, built *during*
   dogfooding and informed by what you see in real use. See the **Feature backlog**. (Subsumes the old
   deferred **interpreter** + **mcp-write** agents.)
4. **Prompt tuning** — ongoing against your real texts via `--show-context`; tightened as you dogfood.
5. **Engine: `agent_sdk` billing swap** — when the **June 15 2026** Agent SDK credit billing lands, verify
   the backend (it exists, marked `# VERIFY`) and offer it as the provider, so summaries draw your Claude
   **Max** credit instead of pay-per-use. Event-gated; folds in when it ships.
6. **Mac-side daemon polish** — the collector as a launchd agent (push-on-wake), FSEvents `live: watch`,
   token-usage logging. Needed for unattended laptop / Mac-mini operation (a minimal version rides in the
   dogfood deploy; this is the hardened form).
7. **Distribution / packaging (the public v1)** — turn the hand-rolled deploy into something a stranger
   can install: Mac → PyPI + `text-triage init` (scaffold config/watch.md, install launchd, walk FDA);
   Server → Docker + Caddy/TLS + a one-command VPS install script. The real shippable gate.
8. **Longevity hardening** (post-ship) — swap the hand-rolled `attributedBody` byte-scan for a maintained
   typedstream decoder (`py-typedstream`); optional server-side prune for "delete to forget."

**The journey:** 1 (done) → **2 (get live on a VPS now)** → dogfood for weeks while building 3 + 4 →
5 + 6 land as they're ready (SDK ~Jun 15; Mac mini ~3 wks) → **7 (package for the public)** → 8.
**You are here:** just finished 1, about to start 2.

## Deployment (VPS now, Mac mini later)

**Topology — one knob.** Laptop = **collector** (`push --watch`: reads chat.db, holds no model keys) →
VPS = **server** (`serve`: owns `state.json` + raw store, runs the LLM summaries on schedule, serves
MCP/Poke over HTTPS). The laptop's `server.url` points at the VPS. When the **Mac mini** arrives it runs
both halves on loopback — flip `server.url` back to blank and nothing else changes.

**Dogfood deploy (near-term — get live for weeks of testing):**
1. Provision a small VPS (Ubuntu, 1-2 GB RAM, Python 3.12+); open **443 only**.
2. Install the server: clone, external venv, `pip install` the server deps (fastmcp/uvicorn/litellm);
   `.env` (`TEXT_TRIAGE_INGEST_TOKEN`, `TEXT_TRIAGE_MCP_KEY`, `ANTHROPIC_API_KEY`); `conditions.yaml` with
   `server.bind: 0.0.0.0:8787`, `server.url: ""`.
3. Run `serve` under **systemd** (auto-restart, start-on-boot) — the VPS analog of launchd.
4. **Caddy** reverse proxy → automatic HTTPS in front of uvicorn, so Poke reaches `https://<host>/mcp`.
   (A Cloudflare tunnel is a simpler interim if you don't want to point a domain yet.)
5. Laptop: set `server.url: https://<host>`, grant FDA to the venv python, run `push --watch` (ideally a
   launchd agent so it pushes on wake). The ingest token authenticates it to the VPS.
6. Point **Poke** at `https://<host>/mcp` with the MCP key.
7. Tune for weeks: prompts (`--show-context`), `conditions.yaml` knobs, `watch.md`; watch real `state.json`.

**Security.** Public surface is only 443; `TEXT_TRIAGE_INGEST_TOKEN` gates `/ingest`+`/trigger`,
`TEXT_TRIAGE_MCP_KEY` gates `/mcp`; secrets stay in per-host `.env`. (fail2ban / rate-limiting later.)

**Migration to Mac mini (~3 weeks):** run both halves on the mini, flip `server.url` to blank (loopback);
copy `raw_messages.sqlite` + `state.json` over to keep history (or just re-backfill). Keep the VPS as the
public MCP host, or decommission it.

## Feature backlog (roadmap item 3)

**M6 status:** every `[core]` + `[near]` item below is BUILT except the LLM **interpreter**
(deliberately deferred to dogfooding — the deterministic law parse is the law; the
`TagSpec(kind/choices/origin)` + `SYSTEM_LAW`/`full_law` contract is ready for it to slot in).
Notes: reply-status decay is computed at QUERY time (like `effective_tags`), not a sweep job; the
short summary is rewritten by EVERY agent that runs on a conversation (daily keeps it freshest).

Priority: **[core]** = default behavior, build first · **[near]** = soon after · **[future]** = post-v1.

**v1 scope = `[core]` + `[near]`** — the tag/classification core plus the cheap deterministic wins: a
complete, sharp product. Every **`[future]`** item is its own post-v1 milestone (each is a new subsystem,
not an enhancement) — do not let them block the v1 line.

### Tags & classifications (the heart of it)

**The law = two origins, one contract.** `watch.md`'s "What to watch" still defines the
**non-deterministic** tags — loose user prose the interpreter compiles, refines, adds to, and retires.
**System tags** (deterministic, incl. the multiple-choice classifications below) are hard-coded in the
contract and the interpreter **cannot touch** them. The law the MCP sees is the **union of both**, always
available with descriptions.

- **[core] System tags** — hard-coded tags / choices the interpreter cannot change or drop. Documented
  explicitly so users and MCP clients know the fixed vocabulary.
- **[core] Multiple-choice classifications** — pick-one from a fixed set, as a first-class tag type
  alongside today's freeform/optional tags. Default, not opt-in.
- **[core] Reply-status tag** — replace the deterministic `needs_reply` with an always-present
  multiple-choice status:
  - `standby` — the conversation is at a reasonable stopping point.
  - `waiting_reply` — else, the last *substantive* reply is from you.
  - `needs_response` — else, the last *substantive* reply is from them.

  Hybrid ownership: for new/short conversations (`new_conversation` — too few texts to summarize) use the
  **deterministic** last-reply gate as the fallback; for established conversations the **LLM** judges by
  substance. So it degrades to the deterministic spine exactly when there isn't enough signal for the model.
  Decay: a `waiting_reply` with no response for a **configurable** number of days decays to `standby` —
  a deterministic time sweep (no new message would otherwise re-trigger the agent to re-evaluate it).
- **[core] Rich interpreter** — the interpreter's tag set + per-tag explanations are **exposed to MCP
  clients** so they know how to query. The law needs descriptions, not just slugs — make the interpreter
  output deliberately rich; its quality is what the MCP surface depends on.

### watch.md + global context
- **[near] Split `watch.md`** into three sections: "Who am I" / "What to watch" / "What I care about."
- **[near] Interpreter parses "Who am I"** → injects it into the shared global system frame (per run).
- **[near] Inject current date + time** into the model context.

### Conversation memory
- **[near] Short summary** — a 1-2 line one-liner per conversation (powers `quickscan`). Re-adds the
  rolling one-liner, scoped tight. **Open: which agent writes/refreshes it (daily? weekly? monthly?) —
  decide before building.**
- **[near] Conversation metadata** in `state.json` — when they last replied, when you last replied, who
  last replied (deterministic).
- **[future] "What you learn about me"** — a dedicated agent accruing insight about the *user* from their
  conversations; MCP-queryable later.
- **[future] "How I text"** — capture the user's texting style as a tool future drafting agents read.

### MCP features
- **[near] `quickscan` tool** — returns, per conversation: name, total message count, most-recent message
  time, and the 1-2 line short summary. A fast triage list. (BUILT as `scan_conversations`.)
- **[near] Per-message sender identifiers** — from Poke dogfooding: group-chat senders are display-name
  strings only, so an agent matching "Braden Hancock" to an email for scheduling is doing name-matching
  by vibes. Store the raw sender handle alongside the display name (extractor + raw store) and surface
  a `sender_id` / the 1:1 `handle` over MCP. (Declined from the same feedback: raw ISO timestamps next
  to the humanized ones — humanized-only is the deliberate presentation; revisit only if an agent
  genuinely needs time arithmetic.)
- **[future] MCP correction loop** — when the interpreter tags something wrong, the user corrects it
  through the MCP and that feedback flows back (into the interpreter and into `state.json`) so it stops
  repeating the mistake. This *will* be built — it's how steering works in practice — just post-v1; builds
  on the existing `update_conversation` write surface.
- **[future] Central feedback channel** — the store behind the loop above: one place every MCP-sourced
  correction / piece of guidance accumulates, which the interpreter reads on its next pass.

### Drafting
- **[future] Draft-message tool** — take LLM-written text and rewrite it in the user's texting style
  (reads "How I text").

## Bugs / hardening

- ~~**`raw_store._connect` doesn't `mkdir -p` its parent**~~ — FIXED (with a test): `_connect` now
  mkdirs the parent before `sqlite3.connect`.
