#!/usr/bin/env bash
set -euo pipefail

echo "=================================================================="
echo "  Samantha Core — Automated Fleet Setup & Environment Bootstrap"
echo "=================================================================="

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

# 1. Create working runtime directories
echo "[1/4] Creating runtime directories (a2a, logs, studios, tmp)..."
mkdir -p a2a logs studios /tmp/betty_docs /tmp/betty_recaps

# 2. Python Virtual Environment Setup
echo "[2/4] Setting up Python virtual environment (venv)..."
if [ ! -d "venv" ]; then
    python3 -m venv venv
fi

./venv/bin/pip install --upgrade pip
./venv/bin/pip install -r requirements.txt

# 3. Environment configuration (.env)
echo "[3/4] Initializing environment configuration (.env)..."
if [ ! -f ".env" ]; then
    cp .env.example .env
    sed -i "s|/opt/samantha-core|$ROOT_DIR|g" .env
    echo "  -> Created custom .env with SAMANTHA_ROOT=$ROOT_DIR"
fi

# 4. Initialize PostgreSQL Database Schema
echo "[4/4] Applying initial PostgreSQL database schema..."
if command -v psql >/dev/null 2>&1; then
    export PGDATABASE="${PGDATABASE:-samantha_db}"
    psql -f schema_bootstrap.sql 2>/dev/null && echo "  -> PostgreSQL schema applied successfully." || echo "  ⚠️ Note: Verify PostgreSQL service status and DATABASE_URL in .env."
else
    echo "  ⚠️ psql command not found in PATH: please execute 'schema_bootstrap.sql' on your PostgreSQL server."
fi

echo ""
echo "=================================================================="
echo "  ✅ Samantha Core Setup completed successfully!"
echo "  To launch the fleet, run: ./quickstart.sh"
echo "=================================================================="
