#!/usr/bin/env bash
# BLM OLV/CLV server watchdog — restart if down
SERVER_PORT=2262
PROJECT_DIR="/home/wa/projects/blm"
PID_FILE="/tmp/blm_server.pid"

# Check if port is listening
if ! ss -tlnp | grep -q ":$SERVER_PORT "; then
    cd "$PROJECT_DIR" || exit 1
    PORT=$SERVER_PORT nohup "$PROJECT_DIR/venv/bin/python" server.py > /tmp/blm_server.log 2>&1 &
    echo $! > "$PID_FILE"
    echo "BLM server restarted on port $SERVER_PORT at $(date)"
else
    # Server already running
    exit 0
fi
