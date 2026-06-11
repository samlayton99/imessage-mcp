#!/usr/bin/env bash
# run_local.sh — the full text-triage stack on ONE machine (your laptop now; the Mac mini later).
#
#   scripts/run_local.sh start     start everything DETACHED (the default with no argument)
#   scripts/run_local.sh stop      kill everything cleanly: TERM by recorded PID -> wait -> KILL,
#                                  then sweep any orphans (survives a killed script / lost pidfiles)
#   scripts/run_local.sh status    what's running + the public URL
#   scripts/run_local.sh url       print the public MCP URL (what you give Poke)
#   scripts/run_local.sh restart   stop + start
#   scripts/run_local.sh logs      tail -f all logs
#
# The three processes:
#   serve         — the server: MCP over HTTP + /ingest + the cadence scheduler (owns state.json)
#   push --watch  — the collector: polls chat.db every live.interval_seconds, pushes raw to /ingest
#   cloudflared   — the public tunnel, so MCP clients (Poke) can reach /mcp from the internet.
#                   Started ONLY if TEXT_TRIAGE_MCP_KEY is set in .env (never expose an open server).
#
# Tunnel modes:
#   default — a Cloudflare QUICK tunnel: zero setup, but the https://….trycloudflare.com URL is
#             REGENERATED on every start (update it in Poke after a restart; `url` prints it).
#   persistent — needs a domain on Cloudflare (one-time, in a real browser):
#             cloudflared tunnel login
#             cloudflared tunnel create text-triage
#             cloudflared tunnel route dns text-triage triage.yourdomain.com
#         then add to .env:
#             TEXT_TRIAGE_TUNNEL_NAME=text-triage
#             TEXT_TRIAGE_PUBLIC_URL=https://triage.yourdomain.com
#         and the script runs the named tunnel instead — same URL forever.
#
# Logs: ~/.text-triage/logs/{server,collector,tunnel}.log · PIDs/URL: ~/.text-triage/run/.
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

env_get() {  # env_get <VAR> -> its value from .env (repo overrides home, like the CLI), else fail
    local var="$1" f line
    for f in "$REPO/.env" "$HOME/.text-triage/.env"; do
        [[ -f "$f" ]] || continue
        line="$(grep -E "^${var}=" "$f" 2>/dev/null | tail -1 || true)"
        if [[ -n "$line" ]]; then
            printf '%s' "${line#*=}"
            return 0
        fi
    done
    return 1
}

# --------------------------------------------------------------------------- stop
do_stop() {
    local killed=0
    for name in tunnel collector server; do     # outermost first: tunnel, then pusher, then server
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
    rm -f "$RUN/tunnel.url"
    # Sweep: catch processes orphaned by a killed script / stale or missing pidfiles. Matches only
    # this stack's invocations, so other python processes are never touched.
    if pkill -f "text_triage.cli serve" 2>/dev/null; then echo "swept an orphaned server"; killed=1; fi
    if pkill -f "text_triage.cli push --watch" 2>/dev/null; then echo "swept an orphaned collector"; killed=1; fi
    if pkill -f "cloudflared tunnel" 2>/dev/null; then echo "swept an orphaned tunnel"; killed=1; fi
    [[ "$killed" == 1 ]] || echo "nothing was running"
}

# ------------------------------------------------------------------------- status
do_status() {
    for name in server collector tunnel; do
        if alive "$RUN/$name.pid"; then
            echo "$name: running (pid $(cat "$RUN/$name.pid"))"
        else
            echo "$name: not running"
        fi
    done
    if [[ -f "$RUN/tunnel.url" ]]; then
        echo "public MCP URL: $(cat "$RUN/tunnel.url")/mcp"
    fi
}

do_url() {
    if [[ -f "$RUN/tunnel.url" ]] && alive "$RUN/tunnel.pid"; then
        echo "$(cat "$RUN/tunnel.url")/mcp"
    else
        echo "no tunnel running — scripts/run_local.sh start (and check 'status')" >&2
        exit 1
    fi
}

# ------------------------------------------------------------------------- tunnel
start_tunnel() {
    local port="${BIND##*:}"
    if ! command -v cloudflared >/dev/null 2>&1; then
        echo "WARN: cloudflared not installed — no public URL, Poke can't reach this server." >&2
        echo "      brew install cloudflared    then: scripts/run_local.sh restart" >&2
        return 0
    fi
    if ! env_get TEXT_TRIAGE_MCP_KEY >/dev/null || [[ -z "$(env_get TEXT_TRIAGE_MCP_KEY)" ]]; then
        echo "WARN: TEXT_TRIAGE_MCP_KEY is not set in .env — refusing to open a PUBLIC tunnel to an" >&2
        echo "      unauthenticated MCP server. Set one and restart:" >&2
        echo "        echo \"TEXT_TRIAGE_MCP_KEY=\$(openssl rand -hex 24)\" >> $REPO/.env" >&2
        echo "        scripts/run_local.sh restart" >&2
        return 0
    fi

    : > "$LOGS/tunnel.log"
    rm -f "$RUN/tunnel.url"
    local name; name="$(env_get TEXT_TRIAGE_TUNNEL_NAME || true)"
    if [[ -n "$name" ]]; then                    # persistent named tunnel (stable URL)
        nohup cloudflared tunnel run --url "http://127.0.0.1:$port" "$name" >>"$LOGS/tunnel.log" 2>&1 &
        echo $! > "$RUN/tunnel.pid"
        local public; public="$(env_get TEXT_TRIAGE_PUBLIC_URL || true)"
        if [[ -n "$public" ]]; then
            echo "${public%/}" > "$RUN/tunnel.url"
        else
            echo "WARN: TEXT_TRIAGE_TUNNEL_NAME is set but TEXT_TRIAGE_PUBLIC_URL is not — add it to .env" >&2
        fi
    else                                         # quick tunnel (URL changes every start)
        nohup cloudflared tunnel --url "http://127.0.0.1:$port" >>"$LOGS/tunnel.log" 2>&1 &
        echo $! > "$RUN/tunnel.pid"
        local url=""
        for _ in $(seq 1 30); do                 # the URL appears in the log within a few seconds
            url="$(grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' "$LOGS/tunnel.log" 2>/dev/null | head -1 || true)"
            [[ -n "$url" ]] && break
            alive "$RUN/tunnel.pid" || break
            sleep 0.5
        done
        if [[ -n "$url" ]]; then
            echo "$url" > "$RUN/tunnel.url"
        else
            echo "WARN: tunnel started but no URL appeared — tail $LOGS/tunnel.log" >&2
        fi
    fi
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

    # ---- public tunnel (detached; only with an MCP key — see start_tunnel)
    start_tunnel

    echo "text-triage running in the background:"
    echo "  server    pid=$SERVER_PID  on http://$BIND  (MCP at /mcp)"
    echo "  collector pid=$COLLECTOR_PID  polling chat.db"
    if [[ -f "$RUN/tunnel.url" ]]; then
        echo "  public    $(cat "$RUN/tunnel.url")/mcp"
        echo "            ^ give Poke this URL + your TEXT_TRIAGE_MCP_KEY as the auth token"
        if [[ -z "$(env_get TEXT_TRIAGE_TUNNEL_NAME || true)" ]]; then
            echo "            (quick tunnel: this URL CHANGES on every start — 'url' reprints it;"
            echo "             see the header of this script for the persistent-URL setup)"
        fi
    fi
    echo "  logs      $LOGS/{server,collector,tunnel}.log"
    echo "  stop with:    scripts/run_local.sh stop"
    echo "  watch logs:   scripts/run_local.sh logs"
}

case "${1:-start}" in
    start)   do_start ;;
    stop)    do_stop ;;
    status)  do_status ;;
    url)     do_url ;;
    restart) do_stop; do_start ;;
    logs)    mkdir -p "$LOGS"; touch "$LOGS/server.log" "$LOGS/collector.log" "$LOGS/tunnel.log"
             exec tail -f "$LOGS/server.log" "$LOGS/collector.log" "$LOGS/tunnel.log" ;;
    *)       echo "usage: $0 {start|stop|status|url|restart|logs}" >&2; exit 2 ;;
esac
