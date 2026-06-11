#!/usr/bin/env bash
# run_local.sh — the full text-triage stack on ONE machine (your laptop now; the Mac mini later).
#
# Starts both halves on loopback:
#   serve         — the server: MCP over HTTP + /ingest + the cadence scheduler (owns state.json)
#   push --watch  — the collector: polls chat.db every live.interval_seconds, pushes raw to /ingest
#
# Topology is the conditions.yaml `server.url` knob: blank = this machine (what this script assumes).
# Logs: ~/.text-triage/logs/{server,collector}.log. Ctrl-C stops both. For boot persistence on a
# Mac mini, wrap this same pair in launchd agents later — nothing else changes.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
PY="$HOME/.venvs/text-triage/bin/python"
LOGS="$HOME/.text-triage/logs"
export PYTHONPATH="$REPO/src"

# ---------------------------------------------------------------- prechecks
if [[ ! -x "$PY" ]]; then
    echo "ERROR: venv python not found at $PY" >&2
    echo "One-time setup (see CLAUDE.md):" >&2
    echo "  mkdir -p ~/.venvs && python3 -m venv ~/.venvs/text-triage" >&2
    echo "  ~/.venvs/text-triage/bin/python -m pip install --upgrade pip pydantic pyyaml litellm 'fastmcp>=3'" >&2
    exit 1
fi

"$PY" -c "import fastmcp" 2>/dev/null || {
    echo "ERROR: fastmcp not installed in the venv (the server needs it):" >&2
    echo "  $PY -m pip install 'fastmcp>=3' litellm" >&2
    exit 1
}

if [[ ! -f "$REPO/.env" && ! -f "$HOME/.text-triage/.env" ]]; then
    echo "WARN: no .env found ($REPO/.env or ~/.text-triage/.env)." >&2
    echo "      Without ANTHROPIC_API_KEY (or another provider key) the scheduled summaries will fail;" >&2
    echo "      without TEXT_TRIAGE_INGEST_TOKEN the loopback routes run open (fine locally)." >&2
fi

"$PY" - <<'EOF' 2>/dev/null || {
import os, sqlite3
p = os.path.expanduser("~/Library/Messages/chat.db")
sqlite3.connect(f"file:{p}?mode=ro", uri=True).execute("select 1")
EOF
    echo "WARN: cannot read ~/Library/Messages/chat.db — the collector will fail until you grant" >&2
    echo "      Full Disk Access to this terminal (or to $PY) in" >&2
    echo "      System Settings > Privacy & Security > Full Disk Access, then restart it." >&2
}

BIND="$("$PY" -c "from text_triage.config import load_config; print(load_config().server.bind)")"
HEALTH_URL="http://${BIND/0.0.0.0/127.0.0.1}/health"
mkdir -p "$LOGS"
cd "$REPO"

# ---------------------------------------------------------------- start both halves
"$PY" -m text_triage.cli serve >>"$LOGS/server.log" 2>&1 &
SERVER_PID=$!

for _ in $(seq 1 20); do
    curl -fsS "$HEALTH_URL" >/dev/null 2>&1 && break
    kill -0 "$SERVER_PID" 2>/dev/null || { echo "ERROR: server died on startup — tail $LOGS/server.log" >&2; exit 1; }
    sleep 0.5
done
curl -fsS "$HEALTH_URL" >/dev/null 2>&1 || {
    echo "ERROR: server never answered $HEALTH_URL — tail $LOGS/server.log" >&2
    kill "$SERVER_PID" 2>/dev/null || true
    exit 1
}

"$PY" -m text_triage.cli push --watch >>"$LOGS/collector.log" 2>&1 &
COLLECTOR_PID=$!

cleanup() {
    echo
    echo "stopping (server=$SERVER_PID collector=$COLLECTOR_PID)..."
    kill "$COLLECTOR_PID" "$SERVER_PID" 2>/dev/null || true
    wait 2>/dev/null || true
}
trap cleanup INT TERM

echo "text-triage running:"
echo "  server    pid=$SERVER_PID  on http://$BIND  (MCP at /mcp)"
echo "  collector pid=$COLLECTOR_PID  polling chat.db"
echo "  logs      $LOGS/server.log  $LOGS/collector.log"
echo "Ctrl-C stops both."
wait
