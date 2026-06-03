# CLAUDE.md — text-triage (agent notes)

## Running tests / Python here — READ FIRST (this ate a long spiral; do not repeat it)

**Root cause — CONFIRMED, not theory.** This repo lives under `~/Desktop`, which has iCloud
"Desktop & Documents" sync ON (`brctl status` shows the `com.apple.CloudDocs` daemon actively
scanning and scheduling **cleanup** on `.../imessage-mcp/.venv` and `.venv/lib`). iCloud
dehydrates/evicts those files, silently gutting a project-local `.venv` — pip + site-packages vanish
while `.venv/bin` is left behind. So the venv MUST live OUTSIDE the iCloud tree (`~/Desktop`,
`~/Documents`). It's a plain venv + pip — **NOT uv** (see below). One-time setup, in a real Terminal:

```bash
mkdir -p ~/.venvs && python3 -m venv ~/.venvs/text-triage
~/.venvs/text-triage/bin/python -m pip install --upgrade pip pydantic pyyaml pytest
cd <repo> && ~/.venvs/text-triage/bin/python -m pytest -q     # 116 tests, ~1s
```

The venv lives at `~/.venvs/text-triage` (out of iCloud's reach); the code stays in the repo. pytest
finds the source via `pythonpath=["src"]` (pyproject) — the package is deliberately NOT installed
(avoids `.pth` pain). Ad-hoc scripts (human runs): `PYTHONPATH=src ~/.venvs/text-triage/bin/python ...`.

**Why not uv (decided):** the failure was the venv's *location* (iCloud), not the tool. uv defaults
to a project-local `.venv` — straight back into the iCloud trap — and earlier added its own friction
(editable `.pth` hook, link modes). Plain external venv + pip is working, conservative, tool-agnostic.
`pyproject.toml` stays (standard; drives `pythonpath`); `uv.lock` is left as a harmless artifact for
forkers, but the dev loop does not use uv.

**The agent CANNOT run the suite itself — the human runs it and pastes the result.** The agent's
sandboxed Bash can't see `.venv/lib/.../site-packages` (even sandbox-off; reports `No such file or
directory`), and the external venv is outside its view too. Workflow: agent writes test + code
(TDD), **human runs `~/.venvs/text-triage/bin/python -m pytest -q` at a checkpoint and pastes**;
agent fixes from that. Batch work to keep checkpoints infrequent. NOTE: Claude Code's in-session `!`
shell is sandboxed (output lands in the chat) — it also can't see the venv; use a real Terminal.

**Dead ends — do not retry** (each fails or just band-aids the symptom): a project-local `.venv`
(iCloud eats it), uv in any form (`uv sync`/`uv run`/`UV_LINK_MODE=copy`/`--reinstall`),
pre-importing compiled deps before `pytest.main()`, reinstall-and-retry loops, `uv cache clean`,
editable `pip install -e .`.

## Tests are hermetic from conditions.yaml / watch.md
CLI-path tests pass `--config <temp '{}'>`; summarizer tests pass an explicit `law=`. Keep new tests
independent of the repo's `conditions.yaml` and `watch.md` — never depend on the ambient files
(editing a real knob must not break the suite).

## Project shape
`src/text_triage/`: `extract` (chat.db → JSON) · `schema` (Pydantic state.json contract) ·
`state_io` (atomic write+lock) · `skeleton` (deterministic facts) · `config` (conditions.yaml) ·
`tags` (watch.md → active tag law) · `engine` (model-call seam: claude_code + StubEngine) ·
`summarize` (daily LLM summary: assemble → validate → one retry → never land invalid) ·
`cli` (subcommand dispatch). Two steering files: `conditions.yaml` (deterministic knobs) +
`watch.md` (tag scratchpad). Real exports / `state.json` / secrets / the handoff bundle are
gitignored; only PII-free synthetic fixtures are committed. Roadmap + milestones: the handoff
`PLAN.md` / `CONTEXT.md` (gitignored).
