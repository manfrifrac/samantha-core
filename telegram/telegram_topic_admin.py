"""
Utility per gestire i topic (forum thread) Telegram: creazione/cancellazione.

Nato dal 04/08/2026: Manfredo pensava che la cancellazione dei topic non
funzionasse. Verificato che l'API `deleteForumTopic` funziona correttamente
(il bot ha già i permessi necessari) -- semplicemente non esisteva ancora
nessun tool/script per farlo. Usare queste funzioni invece di richiamare
curl a mano ogni volta.

LIMITE REALE DELLA TELEGRAM BOT API: non esiste nessun metodo per ELENCARE i
topic esistenti di una chat (niente "listForumTopics") -- si possono gestire
solo topic di cui si conosce gia' il thread_id (es. dalla tabella Postgres
'agents', campo 'thread_id'). Un audit "trova tutti i topic orfani di tutte
le chat" non e' automatizzabile al 100%: prima di cancellare un thread_id,
verificare sempre che nessun agente NON legacy lo condivida ancora (query
su Postgres), altrimenti si rischia di cancellare un topic in uso.
"""
import requests
import psycopg2


def _resolve_token(token_or_bot_key):
    """Se e' gia' un token Telegram (contiene ':') lo usa cosi' com'e',
    altrimenti lo risolve come bot_key leggendolo da Postgres -- niente
    token duplicati/hardcoded qui, unica fonte di verita' e' 'telegram_bots'."""
    if ":" in token_or_bot_key:
        return token_or_bot_key
    conn = psycopg2.connect("dbname=betty_db user=betty_user host=localhost port=5432")
    cur = conn.cursor()
    cur.execute("SELECT token FROM telegram_bots WHERE bot_key = %s;", (token_or_bot_key,))
    row = cur.fetchone()
    conn.close()
    if not row:
        raise ValueError(f"bot_key '{token_or_bot_key}' non trovato in telegram_bots")
    return row[0]


def create_topic(chat_id, name, icon_color=None, custom_emoji_id=None, token_or_bot_key="betty_core"):
    token = _resolve_token(token_or_bot_key)
    payload = {"chat_id": chat_id, "name": name}
    if icon_color is not None:
        payload["icon_color"] = int(icon_color)
    if custom_emoji_id is not None:
        payload["icon_custom_emoji_id"] = str(custom_emoji_id)
    r = requests.post(
        f"https://api.telegram.org/bot{token}/createForumTopic",
        json=payload, timeout=10
    )
    return r.json()


def create_or_update_agent_topic(agent_slug, chat_id, name, icon_color=None, custom_emoji_id=None, token_or_bot_key="betty_core"):
    """
    Crea un nuovo topic per un agente o lo aggiorna (edit_topic) se esiste già nel DB.
    Evita il bug della creazione di topic duplicati (es. 'due Diane') riutilizzando il thread_id esistente.
    Se crea un nuovo topic, salva automaticamente il thread_id nel database Postgres.
    """
    import json
    conn = psycopg2.connect("dbname=betty_db user=betty_user host=localhost port=5432")
    cur = conn.cursor()
    cur.execute("SELECT data FROM agents WHERE agent_id = %s;", (agent_slug,))
    row = cur.fetchone()
    
    agent_data = row[0] if row else {}
    thread_id = agent_data.get("thread_id")
    
    if thread_id:
        res = edit_topic(chat_id, thread_id, name, custom_emoji_id, token_or_bot_key)
    else:
        res = create_topic(chat_id, name, icon_color, custom_emoji_id, token_or_bot_key)
        if res.get("ok"):
            new_thread_id = res["result"]["message_thread_id"]
            agent_data["thread_id"] = new_thread_id
            cur.execute("""
                INSERT INTO agents (agent_id, data) VALUES (%s, %s)
                ON CONFLICT (agent_id) DO UPDATE SET data = agents.data || EXCLUDED.data;
            """, (agent_slug, json.dumps({"thread_id": new_thread_id})))
            conn.commit()
    
    cur.close()
    conn.close()
    return res


def edit_topic(chat_id, thread_id, name=None, custom_emoji_id=None, token_or_bot_key="betty_core"):
    """Modifica un topic esistente: nome e/o icona custom_emoji_id.
    Telegram non permette di cambiare icon_color dopo la creazione."""
    token = _resolve_token(token_or_bot_key)
    payload = {"chat_id": chat_id, "message_thread_id": int(thread_id)}
    if name is not None:
        payload["name"] = name
    if custom_emoji_id is not None:
        payload["icon_custom_emoji_id"] = str(custom_emoji_id)
    r = requests.post(
        f"https://api.telegram.org/bot{token}/editForumTopic",
        json=payload, timeout=10
    )
    return r.json()


def rename_topic(chat_id, thread_id, new_name, token_or_bot_key="betty_core"):
    """Rinomina un topic esistente (editForumTopic) senza toccarne icona/colore."""
    return edit_topic(chat_id, thread_id, name=new_name, token_or_bot_key=token_or_bot_key)


def delete_topic(chat_id, thread_id, token_or_bot_key="betty_core"):
    token = _resolve_token(token_or_bot_key)
    r = requests.post(
        f"https://api.telegram.org/bot{token}/deleteForumTopic",
        json={"chat_id": chat_id, "message_thread_id": int(thread_id)}, timeout=10
    )
    return r.json()


def close_topic(chat_id, thread_id, token_or_bot_key="betty_core"):
    """Chiude un topic (resta visibile/consultabile ma non accetta nuovi
    messaggi da non-admin) -- alternativa piu' soft alla cancellazione."""
    token = _resolve_token(token_or_bot_key)
    r = requests.post(
        f"https://api.telegram.org/bot{token}/closeForumTopic",
        json={"chat_id": chat_id, "message_thread_id": int(thread_id)}, timeout=10
    )
    return r.json()
