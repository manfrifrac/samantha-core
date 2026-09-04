#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

echo "=================================================================="
echo "  Samantha Core — Launching Autonomous Fleet"
echo "=================================================================="

# 1. Launch A2A Bell Relay in background
if ! pgrep -f "a2a_bell_relay.py" >/dev/null; then
    echo "[1/3] Starting A2A Bell Relay daemon..."
    nohup ./venv/bin/python3 core/a2a_bell_relay.py >> logs/a2a_bell_relay.log 2>&1 &
else
    echo "[1/3] A2A Bell Relay daemon already running."
fi

# 2. Launch Self-Healing Supervisor in 'core_system' Tmux session
if ! tmux has-session -t core_system 2>/dev/null; then
    echo "[2/3] Launching Self-Healing Supervisor in Tmux session [core_system]..."
    tmux new-session -d -s core_system -n 'SUPERVISOR' "cd $ROOT_DIR && ./venv/bin/python3 core/self_healing.py"
else
    echo "[2/3] Self-Healing Supervisor already active in Tmux."
fi

# 3. Launch Demo Coordinator
echo "[3/3] Starting Demo Coordinator..."
./venv/bin/python3 core/strumento_agenti.py accendi_agente coord_demo || true

echo ""
echo "=================================================================="
echo "  🎉 Samantha Core Fleet is LIVE!"
echo "  To interact with the Demo Coordinator: tmux attach -t studio_demo"
echo "=================================================================="
