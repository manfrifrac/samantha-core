#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

echo "=================================================================="
echo "  Samantha Core — Avvio Ecosistema Standalone"
echo "=================================================================="

# 1. Avvio A2A Bell Relay in background
if ! pgrep -f "a2a_bell_relay.py" >/dev/null; then
    echo "[1/3] Avvio demone A2A Bell Relay..."
    nohup ./venv/bin/python3 core/a2a_bell_relay.py >> logs/a2a_bell_relay.log 2>&1 &
else
    echo "[1/3] A2A Bell Relay già attivo."
fi

# 2. Avvio Self-Healing Supervisor in sessione Tmux 'core_system'
if ! tmux has-session -t core_system 2>/dev/null; then
    echo "[2/3] Avvio Supervisore Self-Healing in sessione Tmux [core_system]..."
    tmux new-session -d -s core_system -n 'SUPERVISOR' "cd $ROOT_DIR && ./venv/bin/python3 core/self_healing.py"
else
    echo "[2/3] Supervisore Self-Healing già attivo in Tmux."
fi

# 3. Avvio Coordinatore Demo
echo "[3/3] Accensione Coordinatore Demo..."
./venv/bin/python3 core/strumento_agenti.py accendi_agente coord_demo || true

echo ""
echo "=================================================================="
echo "  🎉 Ecosistema Samantha Core Operativo!"
echo "  Per interagire con il Coordinatore Demo: tmux attach -t studio_demo"
echo "=================================================================="
