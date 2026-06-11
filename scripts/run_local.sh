#!/usr/bin/env bash
# run_local.sh — the full text-triage stack on ONE machine (your laptop now; the Mac mini later).
#
#   scripts/run_local.sh start     start both halves DETACHED (the default with no argument)
#   scripts/run_local.sh stop      kill both cleanly: TERM by recorded PID -> wait -> KILL, then
#                                  sweep any orphaned text_triage.cli processes (survives a killed
#                                  script / lost pidfiles)
#   scripts/run_local.sh status    are they running?
#   scripts/run_local.sh restart   stop + start
#   scripts/run_local.sh logs      tail -f both logs
#
# The two halves:
#   serve         — the server: MCP over HTTP + /ingest + the cadence scheduler (owns state.json)
#   push --watch  — the collector: polls chat.db every live.interval_seconds, pushes raw to /ingest
#
# Topology is the conditions.yaml `server.url` knob: blank = this machine (what this script assumes).
# Logs: ~/.text-triage/logs/{server,collector}.log · PIDs: ~/.text-triage/run/*.pid.
# For boot persistence on a Mac mini, wrap `start` in a launchd agent later — nothing else changes.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
PY="$HOME/.venvs/text-triage/bin/python"
LOGS="$HOME/.text-triage/logs"
RUN="$HOME/.text-triage/run"
export PYTHONPATH="$REPO/src"

alive() {  # alive <pidfile> -> 0 if the recorded process is running
    local f="$1"
    [[ -f "$f" ]] && kill -0 "$(cat "$f")" 2>/dev/null
}

# --------------------------------------------------------------------------- stop
do_stop() {
    local killed=0
    for name in collector server; do            # collector first: stop the pusher before its target
        local f="$RUN/$name.pid"
        if alive "$f"; then
            local pid; pid="$(cat "$f")"
            kill "$pid" 2>/dev/null || true
            for _ in $(seq 1 10); do            # up to 5s of graceful shutdown
                kill -0 "$pid" 2>/dev/null || break
                sleep 0.5
            done
            if kill -0 "$pid" 2>/dev/null; then
                echo "$name (pid $pid) ignored TERM; sending KILL"
                kill -9 "$pid" 2>/dev/null || true
            fi
            echo "stopped $name (pid $pid)"
            killed=1
        fi
        rm -f "$f"
    done
    # Sweep: catch processes orphaned by a killed script / stale or missing pidfiles. Matches only
    # this repo's module invocations, so other python processes are never touched.
    if pkill -f "text_triage.cli serve" 2>/dev/null; then echo "swept an orphaned server"; killed=1; fi
    if pkill -f "text_triage.cli push --watch" 2>/dev/null; then echo "swept an orphaned collector"; killed=1; fi
    [[ "$killed" == 1 ]] || echo "nothing was running"
}

# ------------------------------------------------------------------------- status
do_status() {
    for name in server collector; do
        if alive "$RUN/$name.pid"; then
            echo "$name: running (pid $(cat "$RUN/$name.pid"))"
        else
            echo "$name: not running"
        fi
    done
}

# -------------------------------------------------------------------------- start
do_start() {
    # ---- prechecks
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
    if alive "$RUN/server.pid" || alive "$RUN/collector.pid"; then
        echo "already running — use 'status', or 'restart' to bounce it:" >&2
        do_status >&2
        exit 1
    fi
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
    mkdir -p "$LOGS" "$RUN"
    cd "$REPO"

    # ---- server (detached: nohup-ed, and this script exits, so it survives the terminal closing)
    nohup "$PY" -m text_triage.cli serve >>"$LOGS/server.log" 2>&1 &
    SERVER_PID=$!
    echo "$SERVER_PID" > "$RUN/server.pid"

    for _ in $(seq 1 20); do
        curl -fsS "$HEALTH_URL" >/dev/null 2>&1 && break
        if ! kill -0 "$SERVER_PID" 2>/dev/null; then
            rm -f "$RUN/server.pid"
            echo "ERROR: server died on startup — tail $LOGS/server.log" >&2
            exit 1
        fi
        sleep 0.5
    done
    curl -fsS "$HEALTH_URL" >/dev/null 2>&1 || {
        echo "ERROR: server never answered $HEALTH_URL — stopping it; tail $LOGS/server.log" >&2
        kill "$SERVER_PID" 2>/dev/null || true
        rm -f "$RUN/server.pid"
        exit 1
    }

    # ---- collector (detached)
    nohup "$PY" -m text_triage.cli push --watch >>"$LOGS/collector.log" 2>&1 &
    COLLECTOR_PID=$!
    echo "$COLLECTOR_PID" > "$RUN/collector.pid"

    echo "text-triage running in the background:"
    echo "  server    pid=$SERVER_PID  on http://$BIND  (MCP at /mcp)"
    echo "  collector pid=$COLLECTOR_PID  polling chat.db"
    echo "  logs      $LOGS/server.log  $LOGS/collector.log"
    echo "  stop with:    scripts/run_local.sh stop"
    echo "  watch logs:   scripts/run_local.sh logs"
}

case "${1:-start}" in
    start)   do_start ;;
    stop)    do_stop ;;
    status)  do_status ;;
    restart) do_stop; do_start ;;
    logs)    exec tail -f "$LOGS/server.log" "$LOGS/collector.log" ;;
    *)       echo "usage: $0 {start|stop|status|restart|logs}" >&2; exit 2 ;;
esac
