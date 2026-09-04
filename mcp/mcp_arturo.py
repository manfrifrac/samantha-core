# =============================================================================
# ⚠️  QUESTO FILE NON È QUELLO ESEGUITO. NON SERVE A NULLA MODIFICARLO.
#
# Il file VIVO di MCP Arturo è:
#     /root/ecosistema_agenti/core/mcp_arturo.py   (604 righe)
# ed è l'unico referenziato dai .mcp.json che avviano il server:
#     core/.mcp.json:42
#     auruncia_studio/.mcp.json:18
#     core/.agents/betty_studio/.mcp.json:42
#
# Questa copia (218 righe) non è caricata da nessuno: nessun .mcp.json la cita,
# nessun processo la esegue. Chi la modifica NON produce NESSUN EFFETTO —
# crede di aver fatto il lavoro e non l'ha fatto, e il bug che pensava di aver
# chiuso è ancora aperto nel file vero.
#
# Prima di toccare qualunque cosa qui dentro: apri core/mcp_arturo.py.
#
# Etichetta aggiunta il 09/08/2026 da Dario (Studio Leo), task DEV-ARTURO/A,
# su ordine di Betty via Leo, dopo che la duplicazione dei nomi aveva già
# indotto in errore. Il file è lasciato intatto di proposito: non va sistemato,
# va etichettato.
# =============================================================================

import psycopg2
import json
import os
from datetime import datetime
from mcp.server.fastmcp import FastMCP

# Initialize FastMCP Server
mcp = FastMCP("Arturo Indexer")


# Funzione dict_factory non più necessaria per psycopg2 con la logica implementata sopra.

def query_db(query, args=(), fetchall=True):
    try:
        # Nota: Configurazione per Postgres (in futuro usare pool di connessioni)
        con = psycopg2.connect("dbname=betty_db user=betty_user host=localhost")
        cur = con.cursor()
        cur.execute(query, args)
        if fetchall:
            # Per Postgres, recuperiamo i nomi delle colonne per costruire il dict
            colnames = [desc[0] for desc in cur.description]
            rv = [dict(zip(colnames, row)) for row in cur.fetchall()]
        else:
            colnames = [desc[0] for desc in cur.description]
            row = cur.fetchone()
            rv = dict(zip(colnames, row)) if row else None
        con.close()
        return rv
    except Exception as e:
        return {"error": str(e)}

@mcp.tool()
def search_messages(query: str, limit: int = 10, channel: str = "all") -> str:
    """
    Cerca una parola chiave nei messaggi WhatsApp o Email salvati.
    :param query: La parola chiave da cercare.
    :param limit: Numero massimo di risultati da restituire (default 10).
    :param channel: Canale in cui cercare ("whatsapp", "email", o "all").
    """
    results = []
    
    if channel in ["all", "whatsapp"]:
        wa_res = query_db(
            "SELECT 'WhatsApp' as source, chat_name, sender_name, timestamp, body FROM whatsapp_messages WHERE body LIKE ? ORDER BY timestamp DESC LIMIT ?", 
            ('%' + query + '%', limit)
        )
        if isinstance(wa_res, list):
            results.extend(wa_res)
            
    if channel in ["all", "email"]:
        email_res = query_db(
            "SELECT 'Email' as source, from_address, subject, timestamp, body FROM emails WHERE body LIKE ? OR subject LIKE ? ORDER BY timestamp DESC LIMIT ?", 
            ('%' + query + '%', '%' + query + '%', limit)
        )
        if isinstance(email_res, list):
            results.extend(email_res)
            
    # Sort and format results
    if not results:
        return f"Nessun risultato trovato per '{query}'"
        
    results.sort(key=lambda x: x.get('timestamp', 0), reverse=True)
    results = results[:limit]
    
    output = f"Risultati per '{query}':\n\n"
    for r in results:
        dt = datetime.fromtimestamp(r['timestamp']).strftime('%Y-%m-%d %H:%M:%S')
        if r['source'] == 'WhatsApp':
            output += f"[WhatsApp] {dt} - {r['chat_name']} ({r['sender_name']}): {r['body'][:200]}...\n"
        else:
            output += f"[Email] {dt} - Da: {r['from_address']}\nOggetto: {r['subject']}\nAnteprima: {r.get('body', '')[:200]}...\n"
        output += "-"*40 + "\n"
        
    return output

@mcp.tool()
def get_conversation(contact_name: str, limit: int = 50) -> str:
    """
    Recupera gli ultimi messaggi di una determinata chat o mittente (WhatsApp o Email).
    :param contact_name: Nome del contatto, nome del gruppo WhatsApp, o indirizzo email.
    :param limit: Numero di messaggi da recuperare.
    """
    wa_res = query_db(
        "SELECT 'WhatsApp' as source, chat_name, sender_name, timestamp, body FROM whatsapp_messages WHERE chat_name LIKE ? OR sender_name LIKE ? ORDER BY timestamp DESC LIMIT ?", 
        ('%' + contact_name + '%', '%' + contact_name + '%', limit)
    )
    
    email_res = query_db(
        "SELECT 'Email' as source, from_address, subject, timestamp, body FROM emails WHERE from_address LIKE ? ORDER BY timestamp DESC LIMIT ?", 
        ('%' + contact_name + '%', limit)
    )
    
    results = []
    if isinstance(wa_res, list): results.extend(wa_res)
    if isinstance(email_res, list): results.extend(email_res)
    
    if not results:
        return f"Nessuna conversazione trovata per '{contact_name}'"
        
    results.sort(key=lambda x: x.get('timestamp', 0), reverse=True)
    results = results[:limit]
    
    output = f"Storico conversazioni per '{contact_name}':\n\n"
    for r in results:
        dt = datetime.fromtimestamp(r['timestamp']).strftime('%Y-%m-%d %H:%M:%S')
        if r['source'] == 'WhatsApp':
            output += f"[WhatsApp] {dt} - {r['sender_name']}: {r['body']}\n"
        else:
            output += f"[Email] {dt} - Oggetto: {r['subject']}\n"
    return output

@mcp.tool()
def get_gna_project_updates(limit: int = 20) -> str:
    """
    Recupera gli ultimi messaggi/aggiornamenti relativi al progetto Global Nobel Laureates Assembly (GNA).
    """
    query = "gna OR nobel OR assembly OR damas communis OR secretariat"
    return search_messages("nobel", limit=limit, channel="all")

@mcp.tool()
def list_available_agents() -> str:
    """
    Legge la tabella Postgres degli agenti e restituisce un elenco di agenti attivi con il loro slug/agent_id
    (da usare per indirizzare un messaggio A2A diretto via tmux — vedi skill agent_management).
    """
    try:
        # data->>'legacy' esclude gli agent_id storici/superati (marcati così invece
        # di essere cancellati, il 04/08/2026, dopo un audit su richiesta di Manfredo)
        # che altrimenti confonderebbero chi cerca l'agente giusto da contattare.
        rows = query_db("SELECT agent_id, data->>'name' AS name, data->>'tmux_window' AS tmux_window FROM agents WHERE data->>'tmux_window' IS NOT NULL AND COALESCE(data->>'legacy','false') != 'true' ORDER BY agent_id;")
        if isinstance(rows, dict) and "error" in rows:
            return f"Errore nella lettura da Postgres: {rows['error']}"
        if not rows:
            return "Nessun agente trovato in Postgres."
        output = "Agenti registrati nell'ecosistema (agent_id | nome | finestra tmux):\n\n"
        for r in rows:
            output += f"- {r['agent_id']} | {r.get('name') or 'N/D'} | {r.get('tmux_window') or 'N/D'}\n"
        return output
    except Exception as e:
        return f"Errore nella lettura del database: {e}"

OBSIDIAN_VAULT_PATH = "/root/Obsidian-Vault"

@mcp.tool()
def read_obsidian_note(note_name: str, category: str = "") -> str:
    """
    Legge il contenuto di una nota all'interno dell'Obsidian Vault.
    :param note_name: Il nome del file della nota (es. 'Home' o 'Profilo - Manfredo Fraccola'). L'estensione .md è facoltativa.
    :param category: La sotto-cartella facoltativa in cui cercare la nota (es. 'WhatsApp', 'Emails', 'Progetti', 'Agenti').
    """
    if not note_name.endswith(".md"):
        note_name += ".md"
    
    note_path = os.path.join(OBSIDIAN_VAULT_PATH, category, note_name)
    if not os.path.exists(note_path):
        return f"Errore: la nota '{note_name}' non esiste nel percorso '{note_path}'."
        
    try:
        with open(note_path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception as e:
        return f"Errore durante la lettura della nota: {e}"

@mcp.tool()
def write_obsidian_note(note_name: str, content: str, category: str = "") -> str:
    """
    Crea o sovrascrive una nota all'interno dell'Obsidian Vault.
    :param note_name: Il nome della nota (es. 'Diario - 2026-06-29'). L'estensione .md è facoltativa.
    :param content: Il contenuto completo in formato Markdown.
    :param category: La sotto-cartella facoltativa (es. 'Progetti', 'Diario', ecc.).
    """
    if not note_name.endswith(".md"):
        note_name += ".md"
        
    dir_path = os.path.join(OBSIDIAN_VAULT_PATH, category)
    os.makedirs(dir_path, exist_ok=True)
    note_path = os.path.join(dir_path, note_name)
    
    try:
        with open(note_path, "w", encoding="utf-8") as f:
            f.write(content)
        return f"Nota '{note_name}' scritta con successo in '{note_path}'."
    except Exception as e:
        return f"Errore durante la scrittura della nota: {e}"

@mcp.tool()
def search_obsidian_notes(query: str) -> str:
    """
    Cerca una parola chiave all'interno di tutte le note Markdown dell'Obsidian Vault.
    :param query: La parola chiave da cercare.
    """
    results = []
    query_lower = query.lower()
    
    try:
        for root, dirs, files in os.walk(OBSIDIAN_VAULT_PATH):
            for file in files:
                if file.endswith(".md"):
                    file_path = os.path.join(root, file)
                    try:
                        with open(file_path, "r", encoding="utf-8") as f:
                            text = f.read()
                            if query_lower in text.lower():
                                rel_path = os.path.relpath(file_path, OBSIDIAN_VAULT_PATH)
                                for line in text.splitlines():
                                    if query_lower in line.lower():
                                        results.append(f"- **[[{rel_path.replace('.md', '').replace('\\\\', '/').replace('\\', '/')}]]**: ... {line.strip()} ...")
                                        break
                    except Exception:
                        pass
        if not results:
            return f"Nessun risultato trovato per '{query}' nel Vault."
        return f"Risultati della ricerca per '{query}' in Obsidian:\n\n" + "\n".join(results[:20])
    except Exception as e:
        return f"Errore durante la ricerca nel Vault: {e}"

if __name__ == "__main__":
    mcp.run()
