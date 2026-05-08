#!/usr/bin/env bash
# Ensure GPUHub Node Agent is running.
# Intended for manual use or user-level process supervision.

set -euo pipefail

AGENT_LOG="${AGENT_LOG:-$HOME/agent.log}"
PID_FILE="${PID_FILE:-$HOME/.gpuhub-agent.pid}"
GPUHUB_DIR="${GPUHUB_DIR:-$HOME/gpuhub}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
ENTRYPOINT="$GPUHUB_DIR/node_agent/http_server_v4.py"

: "${CONTROL_PLANE_URL:?set CONTROL_PLANE_URL}"
: "${WORKER_TOKEN:?set WORKER_TOKEN}"

export NODE_ID="${NODE_ID:-worker-node-01}"
export FETCH_WAIT_SECONDS="${FETCH_WAIT_SECONDS:-25}"
export FETCH_INTERVAL="${FETCH_INTERVAL:-5}"
export HEARTBEAT_INTERVAL="${HEARTBEAT_INTERVAL:-10}"

if pgrep -f "node_agent/http_server_v4.py" > /dev/null; then
    echo "✅ Node Agent already running"
    pgrep -af "node_agent/http_server_v4.py"
    exit 0
fi

cd "$GPUHUB_DIR"
nohup "$PYTHON_BIN" -u "$ENTRYPOINT" > "$AGENT_LOG" 2>&1 &
echo $! > "$PID_FILE"

sleep 2
if pgrep -f "node_agent/http_server_v4.py" > /dev/null; then
    echo "✅ Node Agent started (PID=$(cat "$PID_FILE"), NODE_ID=$NODE_ID)"
    echo "Log: $AGENT_LOG"
else
    echo "❌ Node Agent failed to start"
    tail -50 "$AGENT_LOG" || true
    exit 1
fi
