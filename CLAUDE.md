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
cd <repo> && ~/.venvs/text-triage/bin/python -m pytest -q     # 124 tests, ~1s
```

The venv lives at `~/.venvs/text-triage` (out of iCloud's reach); the code stays in the repo. pytest
finds the source via `pythonpath=["src"]` (pyproject) — the package is deliberately NOT installed
(avoids `.pth` pain). Ad-hoc scripts (human runs): `PYTHONPATH=src ~/.venvs/text-triage/bin/python ...`.

**Why not uv (decided):** the failure was the venv's *location* (iCloud), not the tool. uv defaults
to a project-local `.venv` — straight back into the iCloud trap — and earlier added its own friction
(editable `.pth` hook, link modes). Plain external venv + pip is working, conservative, tool-agnostic.
`pyproject.toml` stays (standard; drives `pythonpath`); `uv.lock` is left as a harmless artifact for
forkers, but the dev loop does not use uv.

**The agent runs the suite itself** — `~/.venvs/text-triage/bin/python -m pytest -q` (~1s). A global
`allow` rule (`Bash(~/.venvs/*/bin/python -m pytest:*)` in `~/.claude/settings.json`) permits it, and
the agent's Bash tool reaches the external venv fine. Do the real TDD loop: write the failing test,
run it RED, implement, run it GREEN. (The old "agent can't run tests" claim was a misread — iCloud had
*evicted* the repo-local `.venv` files, so "No such file or directory" was literally true; it was
never a sandbox wall. The external `~/.venvs` venv was always reachable.)

**Dead ends — do not retry** (each fails or just band-aids the symptom): a project-local `.venv`
(iCloud eats it), uv in any form (`uv sync`/`uv run`/`UV_LINK_MODE=copy`/`--reinstall`),
pre-importing compiled deps before `pytest.main()`, reinstall-and-retry loops, `uv cache clean`,
editable `pip install -e .`.

## Tests are hermetic from conditions.yaml / watch.md
CLI-path tests pass `--config <temp '{}'>`; summarizer tests pass an explicit `law=`. Keep new tests
independent of the repo's `conditions.yaml` and `watch.md` — never depend on the ambient files
(editing a real knob must not break the suite).

## Project shape
`src/text_triage/`: `extract` (chat.db → JSON) · `schema` (Pydantic state.json contract; no rolling
`summary`, no list caps, `texts_today` on each record) · `state_io` (atomic write+lock) · `skeleton`
(deterministic facts) · `config` (conditions.yaml) · `tags` (watch.md → tag law with lifetimes +
`effective_tags`) · `engine` (async model-call seam; `api_key` default / `agent_sdk` optional, NOT
full Claude Code — being rebuilt from the slow `claude -p`; see PLAN "Engine") · `summarize`
(daily/weekly/monthly agents: assemble → validate → one retry → never land invalid) · `cli`
(`extract`/`summarize --mode`). Two steering files: `conditions.yaml` (knobs) + `watch.md` (tag
scratchpad). Real exports / `state.json` / secrets / the handoff bundle are gitignored; only PII-free
synthetic fixtures are committed. Design + status + decision log: the handoff `PLAN.md` / `CONTEXT.md`
(gitignored).
