#!/usr/bin/env bash
set -euo pipefail

echo "=================================================================="
echo "  Samantha Core — Setup Ambiente & Inizializzazione Standalone"
echo "=================================================================="

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

# 1. Creazione cartelle di lavoro
echo "[1/4] Creazione directory operative (a2a, logs, studios, tmp)..."
mkdir -p a2a logs studios /tmp/betty_docs /tmp/betty_recaps

# 2. Setup Virtualenv Python
echo "[2/4] Configurazione ambiente virtuale Python (venv)..."
if [ ! -d "venv" ]; then
    python3 -m venv venv
fi

./venv/bin/pip install --upgrade pip
./venv/bin/pip install -r requirements.txt

# 3. Setup configurazione .env
echo "[3/4] Inizializzazione file di configurazione .env..."
if [ ! -f ".env" ]; then
    cp .env.example .env
    sed -i "s|/opt/samantha-core|$ROOT_DIR|g" .env
    echo "  -> Creato .env personalizzato con SAMANTHA_ROOT=$ROOT_DIR"
fi

# 4. Inizializzazione Schema Database PostgreSQL
echo "[4/4] Applicazione schema database iniziale..."
if command -v psql >/dev/null 2>&1; then
    export PGDATABASE="${PGDATABASE:-samantha_db}"
    psql -f schema_bootstrap.sql 2>/dev/null && echo "  -> Schema PostgreSQL applicato con successo." || echo "  ⚠️ Nota: Assicurati che PostgreSQL sia attivo e accessibile via DATABASE_URL."
else
    echo "  ⚠️ Comando psql non trovato nel PATH: applica 'schema_bootstrap.sql' manualmente sul tuo server PostgreSQL."
fi

echo ""
echo "=================================================================="
echo "  ✅ Setup completato con successo!"
echo "  Per avviare l'ecosistema esegui: ./quickstart.sh"
echo "=================================================================="
