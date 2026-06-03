# CLAUDE.md — text-triage (agent notes)

## Running tests / Python here — READ FIRST (saves a debugging spiral)

`.venv` is **unreliable inside the Claude Code sandbox**: uv's installed packages
(`.venv/lib/.../site-packages`) get dropped between — and sometimes within — Bash tool calls, so a
fresh `uv run pytest` or `.venv/bin/python` fails with `ModuleNotFoundError: pydantic/pytest`.
This is a **sandbox artifact, not a real bug** — on a normal Mac terminal (`uv sync` / `pipx
install`) the venv persists fine. Confirmed against Claude Code docs + open issues
(per-call shells; venv non-persistence: anthropics/claude-code#8855, #9368). **Don't "fix" the venv.**

**Always run Python/tests as ONE Bash call that reinstalls first, in copy mode:**

```bash
export UV_LINK_MODE=copy && uv sync --reinstall >/dev/null 2>&1 && .venv/bin/python -m pytest -q
```

- `UV_LINK_MODE=copy`: uv's default clone/CoW installs come out broken here; real byte copies survive.
- `--reinstall`: packages vanish between calls, so re-materialize them every call.
- one call: install + use must be in the same Bash invocation (cross-call FS state is lost).
- ad-hoc scripts: `PYTHONPATH=src .venv/bin/python ...` (the editable `.pth` isn't honored either).
- NEVER `uv cache clean` (sandbox has no reliable network to re-download → unrecoverable).
- NEVER mix `uv pip install -e .` with `uv sync` (leaves a duplicate `.pth` that breaks imports).

## Tests are hermetic from conditions.yaml
CLI-path tests (`main`/`cli`) pass `--config <temp '{}' file>` so editing the real `conditions.yaml`
(e.g. `min_messages`) can't break the suite. Keep new tests independent of the repo's config —
use `config=Config(...)` or `--config`, never the ambient file.

## Project shape
`src/text_triage/`: `extract` (chat.db → JSON) · `schema` (Pydantic state.json contract) ·
`state_io` (atomic write+lock) · `skeleton` (deterministic builder) · `config` (loads conditions.yaml).
Real exports / `state.json` / secrets / the handoff bundle are gitignored; only PII-free synthetic
fixtures are committed. Build plan + milestones: the handoff `PLAN.md`/`CONTEXT.md` (gitignored).
