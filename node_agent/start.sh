#!/usr/bin/env bash
# GPUHub Node Agent startup helper.
#
# Required environment:
#   CONTROL_PLANE_URL=https://your-control-plane.example.com
#   WORKER_TOKEN=<private worker token>
# Optional:
#   NODE_ID=worker-node-01
#   FETCH_WAIT_SECONDS=25
#   FETCH_INTERVAL=5
#   PYTHON_BIN=python3
#   GPUHUB_DIR=$HOME/gpuhub
#   AGENT_LOG=$HOME/agent.log

set -euo pipefail

: "${CONTROL_PLANE_URL:?set CONTROL_PLANE_URL}"
: "${WORKER_TOKEN:?set WORKER_TOKEN}"

export NODE_ID="${NODE_ID:-worker-node-01}"
export FETCH_WAIT_SECONDS="${FETCH_WAIT_SECONDS:-25}"
export FETCH_INTERVAL="${FETCH_INTERVAL:-5}"
export HEARTBEAT_INTERVAL="${HEARTBEAT_INTERVAL:-10}"

GPUHUB_DIR="${GPUHUB_DIR:-$HOME/gpuhub}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
AGENT_LOG="${AGENT_LOG:-$HOME/agent.log}"
ENTRYPOINT="$GPUHUB_DIR/node_agent/http_server_v4.py"

cd "$GPUHUB_DIR"

if pgrep -f "node_agent/http_server_v4.py" > /dev/null; then
    echo "⚠️ Existing GPUHub Node Agent process found. Stop it manually before starting another one."
    pgrep -af "node_agent/http_server_v4.py"
    exit 1
fi

nohup "$PYTHON_BIN" -u "$ENTRYPOINT" > "$AGENT_LOG" 2>&1 &
PID=$!

sleep 2
if kill -0 "$PID" 2>/dev/null; then
    echo "✅ Node Agent started (PID=$PID, NODE_ID=$NODE_ID)"
    echo "Log: $AGENT_LOG"
    tail -20 "$AGENT_LOG" || true
else
    echo "❌ Node Agent failed to start"
    tail -50 "$AGENT_LOG" || true
    exit 1
fi
