import os
import sys
import json
import psycopg2
from psycopg2.extras import RealDictCursor

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
import secret_env

_AGENTS_DB = {}

def get_connection():
    db_url = os.environ.get("DATABASE_URL") or secret_env.load_secret("DATABASE_URL")
    if db_url:
        return psycopg2.connect(db_url)
    db_config = os.environ.get("DB_CONFIG") or secret_env.load_secret("DB_CONFIG")
    if db_config:
        return psycopg2.connect(db_config)
    # Default local configuration
    return psycopg2.connect("dbname=betty_db user=betty_user password=betty_password host=localhost port=5432")

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
        return _AGENTS_DB
    except Exception as e:
        print(f"[Agent DB Error] Connection error: {e}", file=sys.stderr)
        return _AGENTS_DB

def get_db():
    return load_db()

def save_db(remove_keys=None):
    """Saves modifications directly to PostgreSQL agents table."""
    try:
        conn = get_connection()
        cur = conn.cursor()
        for agent_id, data in _AGENTS_DB.items():
            cur.execute("""
                INSERT INTO agents (agent_id, data) VALUES (%s, %s)
                ON CONFLICT (agent_id) DO UPDATE SET data = agents.data || EXCLUDED.data;
            """, (agent_id, json.dumps(data)))
            
            if remove_keys and agent_id in remove_keys:
                for key in remove_keys[agent_id]:
                    cur.execute("UPDATE agents SET data = data - %s WHERE agent_id = %s;", (key, agent_id))
                    if key in _AGENTS_DB.get(agent_id, {}):
                        del _AGENTS_DB[agent_id][key]
                        
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print(f"[Agent DB Error] Save error: {e}", file=sys.stderr)

def resolve_agent_id(key):
    """
    Resolves flexible keys (slug, conv_id UUID, name) into the permanent agent_id slug.
    """
    if not key:
        return "samantha"
    key_str = str(key).strip().lower()
    db = get_db()
    if key_str in db:
        return key_str
    for slug, data in db.items():
        if data.get("conv_id", "").lower() == key_str:
            return slug
        if data.get("name", "").lower() == key_str:
            return slug
    return "samantha"

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
    """Tracks host_machine migrations in the dedicated history table."""
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
        print(f"[Agent DB Error] History log error: {e}", file=sys.stderr)

load_db()
