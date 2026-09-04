import os
import sys
import json
import psycopg2
from psycopg2.extras import RealDictCursor

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_CONFIG = "dbname=betty_db user=betty_user password=betty_password host=localhost port=5432"

_AGENTS_DB = {}

def get_connection():
    return psycopg2.connect(DB_CONFIG)

def load_db():
    global _AGENTS_DB
    try:
        conn = get_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT * FROM agents")
        rows = cur.fetchall()
        _AGENTS_DB.clear()
        for row in rows:
            _AGENTS_DB[row['agent_id']] = row['data']
        cur.close()
        conn.close()
        print(f"[Agent DB] ✅ Caricati {len(_AGENTS_DB)} agenti DIRETTAMENTE da PostgreSQL (betty_db).", file=sys.stderr)
        return _AGENTS_DB
    except Exception as e:
        print(f"[Agent DB Error] Errore connessione PostgreSQL betty_db: {e}", file=sys.stderr)
        return _AGENTS_DB

def get_db():
    return load_db()


def save_db(remove_keys=None):
    """Salva le modifiche direttamente ed esclusivamente su PostgreSQL betty_db."""
    try:
        conn = get_connection()
        cur = conn.cursor()
        for agent_id, data in _AGENTS_DB.items():
            # Merge JSONB (agents.data || EXCLUDED.data), non sostituzione totale:
            # protegge da chiamanti che passano un dict per-agente parziale invece
            # di quello completo appena caricato da load_db() — bug reale
            # riscontrato stanotte (system_prompt sparito su 5 agenti diversi
            # per questo stesso pattern altrove nel codice).
            cur.execute("""
                INSERT INTO agents (agent_id, data) VALUES (%s, %s)
                ON CONFLICT (agent_id) DO UPDATE SET data = agents.data || EXCLUDED.data;
            """, (agent_id, json.dumps(data)))
            
            # Rimuove esplicitamente le chiavi indicate per ovviare al comportamento dell'operatore ||
            if remove_keys and agent_id in remove_keys:
                for key in remove_keys[agent_id]:
                    cur.execute("UPDATE agents SET data = data - %s WHERE agent_id = %s;", (key, agent_id))
                    # Rimuove la chiave anche dalla memoria per evitare che venga re-inserita al prossimo save_db()
                    if key in _AGENTS_DB.get(agent_id, {}):
                        del _AGENTS_DB[agent_id][key]
                        
        conn.commit()
        cur.close()
        conn.close()
        print(f"[Agent DB] ✅ Database sincronizzato su PostgreSQL (betty_db).", file=sys.stderr)
    except Exception as e:
        print(f"[Agent DB Error] Errore salvataggio su PostgreSQL: {e}", file=sys.stderr)

def resolve_agent_id(key):
    """
    Risolve chiavi flessibili (slug, conv_id UUID, nome) nell'agent_id slug permanente.
    Ad es. '396c769a-1950-4b50-a1a7-807b62b85b00' -> 'betty'
    """
    if not key:
        return "betty"
    key_str = str(key).strip().lower()
    db = get_db()
    if key_str in db:
        return key_str
    for slug, data in db.items():
        if data.get("conv_id", "").lower() == key_str:
            return slug
        if data.get("name", "").lower() == key_str:
            return slug
    return "betty"

# Inizializzazione tabelle se non esistono
try:
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("CREATE TABLE IF NOT EXISTS agents (agent_id VARCHAR(255) PRIMARY KEY, data JSONB)")
    cur.execute("CREATE TABLE IF NOT EXISTS agent_location_history (id SERIAL PRIMARY KEY, agent_id VARCHAR(255) NOT NULL, host_machine VARCHAR(255) NOT NULL, changed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
    conn.commit()
    cur.close()
    conn.close()
except Exception:
    pass

def log_host_machine_history(agent_id, host_machine):
    """Traccia gli spostamenti di host_machine per l'agente nella tabella dedicata."""
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("SELECT host_machine FROM agent_location_history WHERE agent_id = %s ORDER BY changed_at DESC LIMIT 1;", (agent_id,))
        row = cur.fetchone()
        if not row or row[0] != host_machine:
            cur.execute("INSERT INTO agent_location_history (agent_id, host_machine) VALUES (%s, %s);", (agent_id, host_machine))
            conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print(f"[Agent DB Error] Errore salvataggio storico host_machine: {e}", file=sys.stderr)

load_db()
