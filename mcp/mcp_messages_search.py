"""
MCP server per la ricerca in Email (Gmail) e WhatsApp sincronizzati.

Dati alimentati da core/sync_messaggi.py (servizio permanente in
services:SRV-Sync_Messaggi_Periodico, gira ogni 20 minuti) nelle tabelle
Postgres 'emails' e 'whatsapp_messages'. Nato dal 04/08/2026 dopo che la
sincronizzazione Gmail era rimasta ferma per giorni senza che nessuno se ne
accorgesse (nessun servizio la rilanciava) -- vedi AGENTS.md per il runbook
operativo completo su come verificare/riavviare la sincronizzazione.
"""
import psycopg2
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("Messages Search")

DB_DSN = "dbname=betty_db user=betty_user host=localhost port=5432"


def _query(sql, params):
    conn = psycopg2.connect(DB_DSN)
    cur = conn.cursor()
    cur.execute(sql, params)
    rows = cur.fetchall()
    conn.close()
    return rows


@mcp.tool()
async def search_emails(query: str, limit: int = 10) -> str:
    """Cerca nelle email sincronizzate (oggetto, mittente o corpo) per una parola chiave.
    Restituisce oggetto, mittente, data e un estratto del corpo per ogni risultato."""
    rows = _query(
        """SELECT subject, from_address, date_str, LEFT(body, 400)
           FROM emails
           WHERE subject ILIKE %s OR from_address ILIKE %s OR body ILIKE %s
           ORDER BY timestamp DESC LIMIT %s""",
        (f"%{query}%", f"%{query}%", f"%{query}%", limit)
    )
    if not rows:
        return f"Nessuna email trovata per '{query}'."
    out = []
    for subject, sender, date_str, body in rows:
        out.append(f"📧 {subject}\nDa: {sender}\nData: {date_str}\n{body}\n---")
    return "\n".join(out)


@mcp.tool()
async def get_email_thread(subject_exact: str) -> str:
    """Recupera tutte le email di un thread dato l'oggetto esatto (o quasi esatto),
    ordinate cronologicamente -- utile per vedere l'intera conversazione."""
    rows = _query(
        """SELECT subject, from_address, to_address, date_str, body
           FROM emails
           WHERE subject ILIKE %s
           ORDER BY timestamp ASC""",
        (f"%{subject_exact}%",)
    )
    if not rows:
        return f"Nessun thread trovato per '{subject_exact}'."
    out = []
    for subject, sender, to, date_str, body in rows:
        out.append(f"📧 {subject}\nDa: {sender} | A: {to} | {date_str}\n{body}\n---")
    return "\n".join(out)


@mcp.tool()
async def search_whatsapp(query: str, limit: int = 20) -> str:
    """Cerca nei messaggi WhatsApp sincronizzati (chat, mittente o testo) per una parola chiave.
    Restituisce chat, mittente, data e testo per ogni risultato."""
    rows = _query(
        """WITH target_chats AS (
               SELECT chat_id, MAX(COALESCE(NULLIF(sender_name, 'Me'), chat_name)) as inferred_name
               FROM whatsapp_messages
               WHERE chat_name ILIKE %s OR (sender_name ILIKE %s AND sender_name != 'Me')
               GROUP BY chat_id
           )
           SELECT COALESCE(tc.inferred_name, wm.chat_name), wm.sender_name, to_timestamp(wm.timestamp), wm.body
           FROM whatsapp_messages wm
           LEFT JOIN target_chats tc ON wm.chat_id = tc.chat_id
           WHERE wm.chat_name ILIKE %s 
              OR wm.sender_name ILIKE %s 
              OR wm.body ILIKE %s
           ORDER BY wm.timestamp DESC LIMIT %s""",
        (f"%{query}%", f"%{query}%", f"%{query}%", f"%{query}%", f"%{query}%", limit)
    )
    if not rows:
        return f"Nessun messaggio WhatsApp trovato per '{query}'."
    out = []
    for chat, sender, ts, body in rows:
        out.append(f"💬 [{chat}] {sender} ({ts}): {body}")
    return "\n".join(out)


@mcp.tool()
async def get_whatsapp_chat_history(chat_name: str, limit: int = 30) -> str:
    """Recupera la cronologia recente di una specifica chat/gruppo WhatsApp (nome esatto o parziale del contatto o della chat)."""
    rows = _query(
        """WITH target_chats AS (
               SELECT DISTINCT chat_id
               FROM whatsapp_messages
               WHERE chat_name ILIKE %s OR (sender_name ILIKE %s AND sender_name != 'Me')
           )
           SELECT sender_name, to_timestamp(timestamp), body
           FROM whatsapp_messages
           WHERE chat_id IN (SELECT chat_id FROM target_chats)
              OR chat_name ILIKE %s
              OR (sender_name ILIKE %s AND sender_name != 'Me')
           ORDER BY timestamp DESC LIMIT %s""",
        (f"%{chat_name}%", f"%{chat_name}%", f"%{chat_name}%", f"%{chat_name}%", limit)
    )
    if not rows:
        return f"Nessuna chat trovata per '{chat_name}'."
    out = [f"{sender} ({ts}): {body}" for sender, ts, body in reversed(rows)]
    return "\n".join(out)


if __name__ == "__main__":
    mcp.run()
