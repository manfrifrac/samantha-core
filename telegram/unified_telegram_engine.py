import os
import sys
import time
import json
import fcntl
from datetime import datetime
import logging
import requests
import subprocess
import glob
import re
import threading
import urllib.parse
import hashlib
import psycopg2

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tmux_esatto
import socket
import uuid

LOCKFILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "unified_telegram_engine.lock")
_lock_fh = None

def acquisisci_istanza():
    global _lock_fh
    _lock_fh = open(LOCKFILE, "w")
    try:
        fcntl.flock(_lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError):
        print(f"[UnifiedTelegramEngine] altra istanza attiva (lock {LOCKFILE}): esco, single-writer preservato.", flush=True)
        sys.exit(0)
    _lock_fh.write(str(os.getpid()))
    _lock_fh.flush()

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
import tts
import posizione_manfredo
import radio_comandi
import openllm_relay
import agent_db
import engine_adapter
from secret_env import load_secret
from secret_patterns import install_secret_masking

# =========================================================
# UNIFIED TELEGRAM MASTER BOT & WATCHER ENGINE
# =========================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s"
)
logger = logging.getLogger("UnifiedTelegramEngine")

# DEV-017: maschera i segreti (token Telegram, chiavi Groq/GCP/AWS/...) in
# QUALUNQUE record di log PRIMA che venga scritto, inclusi quelli prodotti da
# librerie terze (es. eccezioni di requests con l'URL /bot<TOKEN>/...).
# Attaccato agli HANDLER del root logger, non al logger, cosi' intercetta anche
# i record propagati dalle librerie. Fonte unica dei pattern: secret_patterns.py.
install_secret_masking()


def _mask_tokens(s):
    """DEV-016: maschera i token bot Telegram nelle eccezioni PRIMA di loggarle.
    Senza questo, un errore di rete di requests ('Max retries exceeded with url:
    /bot<TOKEN>/getUpdates') scrive il token in chiaro nel log/scrollback — fuga
    rilevata dallo scanner nel pannello services:0 il 05/08/2026."""
    return re.sub(r'[0-9]{8,10}:[A-Za-z0-9_-]{35}', '<TOKEN>', str(s))

# CONFIGURAZIONE DI TUTTI I BOT ED ECOSYSTEMS — 100% da Postgres (tabella
# 'telegram_bots'), zero credenziali/slug hardcoded nel codice. Aggiungere un
# nuovo studio/bot Telegram richiede solo una riga in quella tabella, nessuna
# modifica qui. 'agent_map' parte vuoto e viene popolato interamente da
# sync_agent_threads_from_db() leggendo la tabella 'agents'.
def load_bot_configs():
    configs = {}
    try:
        conn = psycopg2.connect("dbname=betty_db user=betty_user host=localhost port=5432")
        cur = conn.cursor()
        cur.execute("SELECT bot_key, token, chat_id, type, session_name, default_agent FROM telegram_bots;")
        rows = cur.fetchall()
        conn.close()
        for bot_key, token, chat_id, typ, session_name, default_agent in rows:
            configs[bot_key] = {
                "token": token,
                "chat_id": chat_id,
                "type": typ,
                "session_name": session_name,
                "default_agent": default_agent,
                "default_window": None,
                "agent_map": {}
            }
    except Exception as e:
        logger.error(f"⚠️ Impossibile caricare 'telegram_bots' da Postgres: {e}")
    return configs

BOT_CONFIGS = load_bot_configs()

def sync_agent_threads_from_db():
    """Ricostruisce da zero l'agent_map di ogni bot, leggendo SOLO Postgres: la
    sessione tmux dell'agente (prefisso di 'tmux_window') viene incrociata con
    'session_name' di 'telegram_bots' per trovare il bot/chat giusto. Nessuno
    slug, alias o fallback hardcoded — un agente senza sessione riconosciuta
    in 'telegram_bots' resta semplicemente non instradato.

    'session_name' puo' contenere PIU' sessioni separate da virgola: un bot/chat
    Telegram puo' servire più sessioni tmux (es. uno studio che si divide in due
    sessioni separate, come manfrifrac -> manfrifrac + aurelio_studio, restando
    sullo stesso bot/topic Telegram) — bug reale riscontrato: quando Aurelio e'
    passato da manfrifrac ad aurelio_studio, la sua sessione non matchava più
    nessun bot e i suoi messaggi cadevano sul default_agent del bot (Orazio)."""
    try:
        fresh_bots = load_bot_configs()
        for b_key, fcfg in fresh_bots.items():
            if b_key in BOT_CONFIGS:
                BOT_CONFIGS[b_key]["session_name"] = fcfg.get("session_name")
            else:
                BOT_CONFIGS[b_key] = fcfg

        session_to_bot = {}
        for b_key, cfg in BOT_CONFIGS.items():
            for sess in (cfg.get("session_name") or "").split(","):
                sess = sess.strip()
                if sess:
                    session_to_bot[sess] = b_key
        for cfg in BOT_CONFIGS.values():
            cfg["agent_map"] = {}

        conn = psycopg2.connect("dbname=betty_db user=betty_user host=localhost port=5432")
        cur = conn.cursor()
        cur.execute("SELECT agent_id, data FROM agents;")
        rows = cur.fetchall()
        conn.close()

        # FIX 14/08/2026 (bug reale segnalato da Aurelio: topic consolidato "AURELIO"
        # instradava i messaggi IN ARRIVO al sottoposto Regionale invece che a lui).
        # Da oggi più agenti possono condividere lo stesso thread_id (consolidamento
        # topic: un coordinatore + i suoi sottoposti sullo stesso canale) — per i
        # RECAP IN USCITA va bene, ognuno trova se stesso in agent_map. Ma per i
        # messaggi IN ARRIVO il loop sotto (righe ~1122) prende il PRIMO agente che
        # trova con quel thread_id e si ferma: l'ordine dipende da come Postgres
        # restituisce le righe, non deterministico né garantito coordinatore-first.
        # Elenco statico minimo (stesso principio di COORDINATORI_NOTI_STATICI in
        # send_a2a.py): processando questi PER PRIMI, il loro ingresso nel dict
        # dell'agent_map li mette in cima — un dict Python preserva l'ordine di
        # inserimento e non lo cambia se la chiave viene poi solo aggiornata, quindi
        # vincono sempre loro nel match "primo trovato" più sotto.
        COORDINATORI_TOPIC_CONDIVISO = {"aurelio", "domenico", "publio", "samantha", "samantha_1", "auruncia_tech", "auruncia_tech_1", "zero_one", "zero_one_1"}
        rows = sorted(rows, key=lambda r: 0 if r[0] in COORDINATORI_TOPIC_CONDIVISO else 1)

        for agent_id, data in rows:
            # 15/08/2026 (audit Igor): nessun filtro su dismesso/archived qui
            # -> agenti spenti con thread_id+tmux_window ancora valorizzati
            # restavano nell'agent_map e potevano vincere il match di routing
            # su un topic ormai morto, rubandolo a un agente vivo con lo
            # stesso thread_id. 'dismesso' e 'archived' sono due flag DIVERSI
            # nello schema (non sinonimi): un agente puo' avere l'uno senza
            # l'altro, quindi si controllano entrambi.
            if str(data.get("dismesso", "")).lower() == "true":
                continue
            if str(data.get("archived", "")).lower() == "true":
                continue
            t_id = data.get("thread_id")
            win = data.get("tmux_window")
            if not t_id or not win or ":" not in win:
                continue
            session = win.split(":", 1)[0]
            bot_key = session_to_bot.get(session)
            if not bot_key:
                continue
            BOT_CONFIGS[bot_key]["agent_map"][agent_id] = {"window": win, "thread_id": int(t_id)}

        # Finestra di default per bot (usata quando un messaggio arriva fuori
        # da qualsiasi topic/thread specifico), risolta dall'agent_map appena
        # ricostruito invece di un target hardcoded per bot_key.
        for cfg in BOT_CONFIGS.values():
            default_agent = cfg.get("default_agent")
            default_info = cfg["agent_map"].get(default_agent) if default_agent else None
            cfg["default_window"] = default_info.get("window") if default_info else None

        logger.info("✅ Sincronizzazione Dinamica Nativi `thread_id` & `tmux_window` da PostgreSQL completata!")
    except Exception as e:
        logger.error(f"⚠️ Impossibile sincronizzare dati da DB: {e}")

def resolve_agent_window(slug):
    """Risolve lo slug di un agente ('session:window') per lo SWITCH_AGENT:
    prima negli agent_map già sincronizzati, poi con una query diretta a
    Postgres come fallback (per agenti registrati dopo l'ultimo sync)."""
    slug = (slug or "").strip().lower()
    if not slug:
        return None
    for b_cfg in BOT_CONFIGS.values():
        info = b_cfg["agent_map"].get(slug)
        if isinstance(info, dict) and info.get("window"):
            return info["window"]
    try:
        conn = psycopg2.connect("dbname=betty_db user=betty_user host=localhost port=5432")
        cur = conn.cursor()
        cur.execute("SELECT data->>'tmux_window' FROM agents WHERE agent_id = %s;", (slug,))
        row = cur.fetchone()
        conn.close()
        if row and row[0]:
            return row[0]
    except Exception as e:
        logger.error(f"⚠️ Impossibile risolvere finestra per switch-agent '{slug}': {e}")
    return None

def synthesize_voice_ogg(text, agent_slug):
    """Sintetizza `text` con tts.speak() (Fish Audio/ElevenLabs/Google in
    cascata) e ritorna il path locale del file .ogg pronto per sendVoice,
    oppure None se la sintesi fallisce."""
    if not text:
        return None
    try:
        conn = psycopg2.connect("dbname=betty_db user=betty_user host=localhost port=5432")
        cur = conn.cursor()
        cur.execute("SELECT data->>'voice' FROM agents WHERE agent_id = %s;", (agent_slug,))
        row = cur.fetchone()
        conn.close()
        voice_id = row[0] if row and row[0] else "it-IT-ElsaNeural"

        audio_url = tts.speak(text, voice=voice_id, agent_id=agent_slug)
        if not audio_url or "file=" not in audio_url:
            return None

        qs = urllib.parse.parse_qs(urllib.parse.urlparse(audio_url).query)
        fname = qs.get("file", [None])[0]
        if not fname:
            return None

        # FIX 12/08/2026 (exec_fix_voce_google): il file MP3 viene scritto da
        # tts.speak() in tts.get_temp_dir() (quindi in tts_scratch_condiviso),
        # NON in ~/.gemini/antigravity-cli/scratch. Prima si cercava solo li' ->
        # mismatch -> nessun .ogg -> nessun sendVoice (vocale mai inviato).
        # Ora si prova PRIMA il percorso reale di get_temp_dir(), poi il vecchio
        # percorso ~/.gemini/.../scratch come fallback di robustezza.
        mp3_path = os.path.join(tts.get_temp_dir(), fname)
        if not os.path.exists(mp3_path):
            mp3_path = os.path.join(os.path.expanduser("~/.gemini/antigravity-cli/scratch"), fname)
        if not os.path.exists(mp3_path):
            return None

        ogg_path = mp3_path.rsplit(".", 1)[0] + ".ogg"
        subprocess.run(["ffmpeg", "-y", "-i", mp3_path, "-c:a", "libopus", "-b:a", "32k", ogg_path],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return ogg_path if os.path.exists(ogg_path) else None
    except Exception as e:
        logger.error(f"⚠️ Sintesi vocale fallita per '{agent_slug}': {e}")
        return None

RECAPS_DIR = "/tmp/betty_recaps"
TASKLISTS_DIR = "/tmp/betty_tasklists"
_TASKLIST_STATE_PATH = "/tmp/betty_tasklist_state.json"

# Contatore tentativi falliti per (slug, hash del contenuto) — evita che un
# recap che fallisce sempre allo stesso modo (causa non transitoria) venga
# ritentato all'infinito, rigenerando anche l'audio TTS ad ogni giro.
_FAILED_ATTEMPTS = {}
MAX_RECAP_RETRIES = 3

# Domande a scelta (pulsanti inline): slug -> lista opzioni dell'ultima domanda
# inviata. Una sola domanda pendente per agente per volta (sufficiente per il
# caso d'uso: l'agente aspetta la risposta prima di proseguire).
# Persistito su disco: un riavvio del motore (crash, deploy, fix) non deve
# invalidare silenziosamente una domanda con pulsanti gia' inviata all'utente.
_PENDING_QUESTIONS_PATH = "/tmp/betty_pending_questions.json"


def _load_pending_questions():
    try:
        with open(_PENDING_QUESTIONS_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_pending_questions():
    try:
        with open(_PENDING_QUESTIONS_PATH, "w") as f:
            json.dump(_PENDING_QUESTIONS, f)
    except Exception as e:
        logger.warning(f"⚠️ Impossibile salvare _PENDING_QUESTIONS su disco: {e}")


# LOG PERSISTENTE delle scelte sui pulsanti [QUESTION_OPTIONS] (richiesta Alfredo,
# 18/08/2026): senza uno storico su disco era impossibile diagnosticare bug di
# consegna della risposta (es. chosen_text perso al paste tmux). Append ad ogni
# tap risolto, con sender + chiave domanda + opzione + agente destinatario.
_BUTTON_CHOICES_LOG = "/root/ecosistema_agenti/core/logs/telegram_button_choices.log"


def _log_button_choice(sender, question_key, chosen, forwarded_to):
    try:
        os.makedirs(os.path.dirname(_BUTTON_CHOICES_LOG), exist_ok=True)
        # chosen normalizzato a una sola riga: le opzioni potrebbero contenere
        # newline (tag [QUESTION_OPTIONS] multiriga), che romperebbero il log.
        chosen_s = " ".join(chosen.split()).replace("|", ";")
        with open(_BUTTON_CHOICES_LOG, "a") as lf:
            lf.write(f"{datetime.now().isoformat(timespec='seconds')} | sender={sender} | question={question_key} | chosen={chosen_s} | forwarded_to={forwarded_to}\n")
    except Exception as e:
        logger.warning(f"⚠️ Impossibile scrivere log scelta pulsante: {e}")


# FIX 10/08/2026 (task Leo, segnalazione Betty — Manfredo riceve i propri
# messaggi duplicati 2-3 volte, MSG_ID identico): `offset` di getUpdates era
# una variabile locale di run_bot_polling(), tenuta SOLO in memoria — ogni
# riavvio del processo (2 oggi: storico posizioni + il fix ESC-deepcode)
# ripartiva da 0, e Telegram riconsegna dal server tutti gli update ancora
# "non confermati" (offset non ancora superato quel numero), non solo quelli
# arrivati durante il downtime. File separato PER BOT (non un unico file
# condiviso fra i 5 thread paralleli di run_bot_polling): un file solo
# avrebbe un race read-modify-write fra thread diversi che scrivono la
# propria chiave quasi in contemporanea — un file a testa lo esclude del
# tutto, ogni thread e' proprietario esclusivo del proprio.
def _offset_state_path(bot_key):
    return f"/tmp/betty_telegram_offset_{bot_key}.json"


def _load_telegram_offset(bot_key):
    try:
        with open(_offset_state_path(bot_key)) as f:
            return int(json.load(f).get("offset", 0))
    except Exception:
        return 0  # nessuno stato pregresso (primo avvio) o file corrotto: comportamento storico


def _save_telegram_offset(bot_key, offset):
    try:
        with open(_offset_state_path(bot_key), "w") as f:
            json.dump({"offset": offset, "ts": time.time()}, f)
    except Exception as e:
        logger.warning(f"⚠️ Impossibile salvare offset Telegram per {bot_key}: {e}")


_PENDING_QUESTIONS = _load_pending_questions()

_PINNED_MESSAGES_PATH = "/tmp/betty_pinned_messages.json"

def _load_pinned_messages():
    try:
        with open(_PINNED_MESSAGES_PATH) as f:
            return json.load(f)
    except Exception:
        return {}

def _save_pinned_messages(data):
    try:
        with open(_PINNED_MESSAGES_PATH, "w") as f:
            json.dump(data, f)
    except Exception as e:
        logger.warning(f"⚠️ Impossibile salvare _PINNED_MESSAGES su disco: {e}")

# ---------------------------------------------------------
# FEEDBACK VISIVO "IN LAVORAZIONE": la reaction sul messaggio dell'utente
# cambia icona in loop finché l'agente sta elaborando (il testo a schermo
# nel terminale Claude/agy non è un segnale affidabile di "sta ancora
# lavorando" — l'unico segnale certo per l'utente è la reaction Telegram).
# Quando arriva il recap, il loop si ferma e la reaction si blocca su
# un'icona di conclusione (successo o abbandono dopo troppi retry).
# ---------------------------------------------------------
_REACTION_CYCLES = {}  # msg_id -> threading.Event (set = stop)
_REACTION_ICONS_WORKING = ["⚡", "🤔", "👀", "🔥"]
_REACTION_ICON_DONE = "👍"
_REACTION_ICON_FAILED = "🤯"
_REACTION_CYCLE_INTERVAL = 2.5  # secondi tra un cambio icona e il successivo
_REACTION_CYCLE_MAX_SECONDS = 20 * 60  # oltre 20 min il loop si ferma da solo (niente spam API), l'icona resta congelata sull'ultima mostrata

# ---- dedup message_id in ingresso (PUNTO 6 mandato dedup_terza_porta) ----
# Ogni bot gira in un proprio thread di polling; con `offset` non ancora
# superato (vedi righe 262-266) un update puo' essere ri-offerto piu' volte da
# getUpdates -> lo stesso message_id puo' entrare due volte nel loop e finire
# duplicato in coda al prompt dell'agente. Set condiviso thread-safe: se gia'
# visto, si scarta. Cap limite automatico per non far crescere la memoria.
# Attenzione: `message_id` NON e' globale (varia per bot/chat), ma un id
# Telegram e' univoco per chat; per evitare collisioni tra chat diverse si
# chiava con (chat_id, msg_id).  -- CODICE PRONTO, DEMONE DA RIAVVIARE (Samantha)
_SEEN_MSG_LOCK = threading.Lock()
_SEEN_MSG_IDS = set()
_SEEN_MSG_MAX = 20000  # limite cache; oltre, si svuota per evitare crescita

def _gia_visto_message(chat_id, msg_id):
    """True se (chat_id, msg_id) e' gia' stato processato; altrimenti lo marca
    come visto e ritorna False. Thread-safe via lock."""
    global _SEEN_MSG_IDS
    key = (chat_id, msg_id)
    with _SEEN_MSG_LOCK:
        if key in _SEEN_MSG_IDS:
            return True
        _SEEN_MSG_IDS.add(key)
        if len(_SEEN_MSG_IDS) > _SEEN_MSG_MAX:
            _SEEN_MSG_IDS = set()
        return False

def _set_reaction(api_url, chat_id, msg_id, emoji):
    """Ritorna i secondi di backoff da rispettare (0 se nessun rate limit).
    Prima gli errori venivano scartati in silenzio (bug reale riscontrato:
    con tanti agenti/thread attivi insieme, l'API setMessageReaction va in
    429 Too Many Requests e le icone di feedback smettono di comparire per
    l'utente senza che nessun log lo segnali)."""
    try:
        r = requests.post(f"{api_url}/setMessageReaction", json={
            "chat_id": chat_id,
            "message_id": msg_id,
            "reaction": [{"type": "emoji", "emoji": emoji}]
        }, timeout=5)
        if r.status_code == 429:
            retry_after = r.json().get("parameters", {}).get("retry_after", 5)
            logger.warning(f"⚠️ setMessageReaction rate-limited (429), backoff {retry_after}s (chat={chat_id}, msg={msg_id})")
            return retry_after
        if r.status_code != 200:
            logger.warning(f"⚠️ setMessageReaction fallita ({r.status_code}) per chat={chat_id} msg={msg_id}: {r.text[:150]}")
    except Exception as e:
        logger.warning(f"⚠️ Errore chiamata setMessageReaction per chat={chat_id} msg={msg_id}: {e}")
    return 0

def start_reaction_cycle(api_url, chat_id, msg_id):
    """Imposta UNA SOLA icona fissa 'in lavorazione' sul messaggio dell'utente (niente più animazione
    a rotazione ogni 2.5s): su richiesta di Manfredo del 04/08/2026, l'animazione da sola generava
    un volume di chiamate a setMessageReaction che andava sistematicamente in rate-limit (429) con
    più agenti/messaggi attivi insieme — un'icona fissa costa 1 chiamata invece di una ogni 2.5s per
    tutta la durata del turno, stesso feedback utile (lavorazione in corso -> icona finale) con un
    carico sull'API drasticamente inferiore."""
    _REACTION_CYCLES.pop(msg_id, None)  # mantenuto per compatibilità con stop_reaction_cycle
    _set_reaction(api_url, chat_id, msg_id, _REACTION_ICONS_WORKING[0])

def stop_reaction_cycle(api_url, chat_id, msg_id, success=True):
    """Ferma il loop 'in lavorazione' per msg_id e blocca la reaction sull'icona di conclusione."""
    stop_event = _REACTION_CYCLES.pop(msg_id, None)
    if stop_event:
        stop_event.set()
    _set_reaction(api_url, chat_id, msg_id, _REACTION_ICON_DONE if success else _REACTION_ICON_FAILED)


TELEGRAM_TEXT_LIMIT = 4000  # limite reale Telegram 4096, margine di sicurezza

_MDV2_ESCAPE_RE = re.compile(r'([_*\[\]()~`>#+\-=|{}.!\\])')

def _mdv2_escape(text):
    """Escape di tutti i caratteri speciali MarkdownV2 in un tratto di testo
    LETTERALE (fuori da qualunque entità di formattazione riconosciuta)."""
    return _MDV2_ESCAPE_RE.sub(r'\\\1', text)

# Riconosce le sintassi CommonMark che qualunque LLM produce di default nei
# recap (bold/italic/strike/code/link) PIU' le sintassi native MarkdownV2
# (underline __, spoiler ||) per chi le vuole usare esplicitamente. Ordine
# importante: i pattern più lunghi/specifici (**, ~~, ```) vanno prima dei
# corrispettivi corti (*, `) altrimenti l'alternanza regex li spezzerebbe a metà.
_MDV2_TOKEN_RE = re.compile(
    r'(?P<codeblock>```.*?```)'
    r'|(?P<code>`[^`\n]+?`)'
    r'|(?P<bold>\*\*(?:(?!\*\*).)+?\*\*)'
    r'|(?P<link>\[[^\]\n]+?\]\([^)\n]+?\))'
    r'|(?P<strike>~~(?:(?!~~).)+?~~)'
    r'|(?P<underline>__(?:(?!__).)+?__)'
    r'|(?P<spoiler>\|\|(?:(?!\|\|).)+?\|\|)'
    # Corsivo SOLO a confine di parola (05/08/2026). Prima queste due regole non
    # avevano vincoli, e qualunque underscore DENTRO una parola diventava un
    # delimitatore: 'sync_messaggi.py, engine_adapter.py' veniva letto come
    # 'sync' + corsivo + '.py', quindi Telegram MANGIAVA gli underscore e fondeva
    # le parole. Con un numero DISPARI di underscore l'entita' restava aperta,
    # Telegram rifiutava con 400 "Can't find end of Italic entity" e il motore
    # ripiegava sul testo grezzo: l'INTERO messaggio arrivava senza formattazione,
    # con tutti gli asterischi in chiaro. E' il motivo per cui sembrava che certi
    # agenti "non sapessero usare il markdown": scrivevano nomi in snake_case
    # (agent_id, tmux_window, betty_db) — cioe' quasi ogni messaggio tecnico.
    # I lookaround impongono cio' che fa qualunque markdown serio: un underscore
    # attaccato a lettere o cifre e' testo, non enfasi.
    r'|(?P<italic_star>(?<![\w\\])\*(?:(?!\*).)+?\*(?!\w))'
    r'|(?P<italic_us>(?<![\w\\])_(?:(?!_).)+?_(?!\w))',
    re.DOTALL
)

def _process_inline(text):
    """Elabora un tratto di testo SENZA newline di citazione (già separati a
    monte da to_telegram_markdown) per le entità inline. Ricorsiva sul
    contenuto interno di ogni entità: Telegram supporta nativamente
    l'annidamento (verificato via API reale il 04/08/2026 — un bold contenente
    un italic produce due entity MarkdownV2 correttamente sovrapposte, offset
    condivisi), quindi *grassetto con _corsivo_ dentro* non va più appiattito
    con escaping letterale ma rielaborato dallo stesso tokenizer."""
    out = []
    last_end = 0
    for m in _MDV2_TOKEN_RE.finditer(text):
        out.append(_mdv2_escape(text[last_end:m.start()]))
        kind = m.lastgroup
        raw = m.group()
        if kind == 'codeblock':
            inner = raw[3:-3]
            out.append('```' + inner.replace('\\', '\\\\').replace('`', '\\`') + '```')
        elif kind == 'code':
            inner = raw[1:-1]
            out.append('`' + inner.replace('\\', '\\\\').replace('`', '\\`') + '`')
        elif kind == 'bold':
            out.append('*' + _process_inline(raw[2:-2]) + '*')
        elif kind == 'link':
            lm = re.match(r'\[([^\]]+)\]\(([^)]+)\)', raw, re.DOTALL)
            label, url = lm.group(1), lm.group(2)
            out.append('[' + _process_inline(label) + '](' + url.replace('\\', '\\\\').replace(')', '\\)') + ')')
        elif kind == 'strike':
            out.append('~' + _process_inline(raw[2:-2]) + '~')
        elif kind == 'underline':
            out.append('__' + _process_inline(raw[2:-2]) + '__')
        elif kind == 'spoiler':
            out.append('||' + _process_inline(raw[2:-2]) + '||')
        elif kind in ('italic_star', 'italic_us'):
            out.append('_' + _process_inline(raw[1:-1]) + '_')
        last_end = m.end()
    out.append(_mdv2_escape(text[last_end:]))
    return ''.join(out)

def to_telegram_markdown(text):
    """Converte il markdown CommonMark scritto dagli agenti (**bold**, *italic*
    o _italic_, ~~strike~~, `code`, ```block```, [testo](url)) nella sintassi
    Telegram MarkdownV2, con escaping automatico di tutta la punteggiatura
    letterale fuori dalle entità riconosciute (obbligatorio in MarkdownV2,
    altrimenti Telegram rifiuta l'intero messaggio con 400). Supporta anche
    __underline__ e ||spoiler|| nativi MarkdownV2 per chi li vuole usare
    esplicitamente — per questo un agente che intende '__bold__' in stile
    CommonMark otterrebbe invece un sottolineato: usare sempre **bold** per il
    grassetto (documentato in AGENTS.md). Le entità annidate funzionano
    (**grassetto con _corsivo_ dentro**).
    Citazioni: riga che inizia con '> ' -> citazione normale. Righe consecutive
    che iniziano con '>> ' -> citazione ESPANDIBILE (collassata con freccina
    "mostra di più" su mobile, ideale per contenuti lunghi — verificato via
    API reale il 04/08/2026: sintassi Telegram nativa e' '**>' sulla prima riga,
    '>' sulle righe intermedie, '||' in fondo all'ultima riga di contenuto)."""
    lines = text.split('\n')
    out_lines = []
    i = 0
    while i < len(lines):
        stripped = lines[i].lstrip()
        heading_match = re.match(r'#{1,6}\s+(.*)', stripped)
        if heading_match:
            h_text = heading_match.group(1)
            h_text = re.sub(r'\*\*(.*?)\*\*', r'\1', h_text)
            out_lines.append('*' + _process_inline(h_text) + '*')
            i += 1
        elif stripped.startswith('**>'):
            block = []
            while i < len(lines) and lines[i].lstrip().startswith('**>'):
                content = lines[i].lstrip()[3:]
                content = content[1:] if content.startswith(' ') else content
                block.append(_process_inline(content))
                i += 1
            if len(block) == 1:
                out_lines.append('**>' + block[0] + '||')
            else:
                out_lines.append('**>' + block[0])
                for j in range(1, len(block)-1):
                    out_lines.append('>' + block[j])
                out_lines.append('>' + block[-1] + '||')
            continue
        elif stripped.startswith('>>'):
            block = []
            while i < len(lines) and lines[i].lstrip().startswith('>>'):
                content = lines[i].lstrip()[2:]
                content = content[1:] if content.startswith(' ') else content
                block.append(_process_inline(content))
                i += 1
            if len(block) == 1:
                out_lines.append('**>' + block[0] + '||')
            else:
                out_lines.append('**>' + block[0])
                for j in range(1, len(block)-1):
                    out_lines.append('>' + block[j])
                out_lines.append('>' + block[-1] + '||')
            continue
        elif stripped.startswith('>'):
            content = stripped[1:]
            content = content[1:] if content.startswith(' ') else content
            out_lines.append('>' + _process_inline(content))
            i += 1
        else:
            out_lines.append(_process_inline(lines[i]))
            i += 1
    return '\n'.join(out_lines)


def split_telegram_text(text, limit=TELEGRAM_TEXT_LIMIT):
    """Spezza un testo troppo lungo per una singola sendMessage Telegram (limite
    4096 caratteri) in più blocchi, rispettando i paragrafi dove possibile invece
    di tagliare a caso. Senza questo, un messaggio lungo fallisce con HTTP 400 e
    — se il chiamante lo rimette in coda per ritentare — genera un loop infinito
    che rigenera anche l'audio TTS ad ogni tentativo."""
    if len(text) <= limit:
        return [text]

    chunks = []
    paragraphs = text.split("\n\n")
    current = ""
    for p in paragraphs:
        candidate = f"{current}\n\n{p}" if current else p
        if len(candidate) <= limit:
            current = candidate
        else:
            if current:
                chunks.append(current)
            if len(p) <= limit:
                current = p
            else:
                # Paragrafo singolo più lungo del limite: taglio duro necessario
                for i in range(0, len(p), limit):
                    chunks.append(p[i:i + limit])
                current = ""
    if current:
        chunks.append(current)
    return chunks

_window_locks = {}
_window_locks_meta_lock = threading.Lock()

def _get_window_lock(target_window):
    with _window_locks_meta_lock:
        if target_window not in _window_locks:
            _window_locks[target_window] = threading.Lock()
        return _window_locks[target_window]

def _pool_ticket_slug(target_window):
    """FASE 3 pool (19/08/2026, ordine Manfredo MSG 11291, validazione
    Samantha db32 + Simone 9557, trigger automatico al passaggio dei collaudi
    T1-T6): se il target di consegna e' la finestra dichiarata da un record
    VIRTUALE di pool (data->'pool_di' non vuoto) col flag per-agente
    'telegram_deposita_ticket'='true', ritorna lo slug del pool — il
    messaggio NON va incollato a una finestra (sarebbe un lettore unico):
    diventa un TICKET nella coda condivisa. Con flag assente/false ritorna
    None e TUTTO resta come prima (patch flaggata, default = storico).

    FIX 24/08/2026 (exec_routing_pool_tg, incidente reale dello stesso giorno:
    il record virtuale 'samantha' e' rimasto con dismesso='true' mentre
    telegram_deposita_ticket restava 'true' — pool_lib.scopri_pool() lo
    escludeva dai pool serviti da pool_campanellaio.py, MA questa query non
    controllava affatto 'dismesso' e continuava a deviare i messaggi Telegram
    in un ticket su a2a/samantha/inbox/ che nessun campanellaio leggeva piu':
    un messaggio di Manfredo e' rimasto bloccato li' ~5 minuti prima che
    l'incidente fosse notato e corretto a mano). Stesso identico filtro gia'
    usato da pool_lib._pool_da_postgres() e da sync_agent_threads_from_db()
    qui sopra (riga ~138): un pool dismesso non e' 'servito', quindi qui non
    deve nemmeno ricevere ticket — il messaggio ricade sul percorso storico
    (paste diretto in tmux), che ha gia' il suo fallback 'offline' verso
    Manfredo se la finestra non risponde. Questo e' esattamente il
    comportamento di 'fallback obbligatorio' richiesto per il routing
    dinamico dei pool: se la parte dinamica non e' in condizione di operare,
    si ricade sul comportamento storico, mai un messaggio perso in silenzio."""
    try:
        import psycopg2
        conn = psycopg2.connect("dbname=betty_db user=betty_user host=localhost port=5432")
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT agent_id FROM agents "
                "WHERE data->>'tmux_window' = %s "
                "AND COALESCE(data->>'pool_di','') <> '' "
                "AND lower(COALESCE(data->>'telegram_deposita_ticket','')) = 'true' "
                "AND (data->>'dismesso') IS DISTINCT FROM 'true' "
                "LIMIT 1;", (target_window,))
            row = cur.fetchone()
        finally:
            conn.close()
        return row[0] if row else None
    except Exception as e:
        logger.error(f"⚠️ _pool_ticket_slug fallito per {target_window}: {e} — consegna tmux storica")
        return None


def dispatch_prompt_async(target_window, prompt_text, offline_notice=None):
    """Consegna un prompt a una finestra tmux in un thread separato invece di
    farlo in linea nel loop `run_bot_polling`. send_prompt_to_tmux puo' attendere
    FINO A 5 MINUTI se la finestra target e' occupata (per non perdere il
    messaggio pastando durante una generazione) — prima di questo fix quell'attesa
    bloccava l'intero loop del bot, quindi anche messaggi per ALTRI agenti/topic
    sotto lo stesso bot Telegram restavano in coda dietro a un destinatario
    occupato, anche se loro stessi erano liberi (segnalato da Manfredo 04/08/2026).
    Un lock per-finestra (non globale) serializza solo i messaggi diretti alla
    STESSA finestra, cosi' due paste non si accavallano mai sulla stessa sessione
    (causa nota di corruzione input vista sugli specialisti di Aurelio) — finestre
    diverse restano invece completamente indipendenti tra loro.
    offline_notice: dict opzionale {token, chat_id, message_thread_id, text,
    reply_to_message_id (opz.)} da postare su Telegram se la consegna fallisce
    (studio/sessione non attivo) — passato per valore come argomento esplicito,
    non catturato da closure, per evitare il classico bug di late-binding delle
    variabili di loop in Python quando il thread parte in un secondo momento."""
    def _run(target_window=target_window, prompt_text=prompt_text, offline_notice=offline_notice):
        # FASE 3 pool: destinazione = record virtuale di pool col deposito
        # attivo -> il messaggio diventa TICKET (a2a_inbox.deposita, atomico)
        # invece di un paste a una finestra. Il campanellaio del pool
        # (pool_campanellaio.py) svegliera' l'istanza giusta; la consegna
        # vera e' il file su disco (stessa semantica DEV-110 degli A2A).
        pool_slug = _pool_ticket_slug(target_window)
        if pool_slug:
            try:
                import a2a_inbox
                testo = f"[A2A_FROM:manfredo-telegram] [A2A_TYPE:task]\n{prompt_text}"
                a2a_id, path_dep = a2a_inbox.deposita(pool_slug, testo)
                logger.info(f"🎫 [pool {pool_slug}] messaggio Telegram depositato come ticket {a2a_id} ({path_dep})")
                return
            except Exception as e:
                # deposito fallito: NON perdere il messaggio — si degrada
                # rumorosamente alla consegna tmux storica.
                logger.error(f"⚠️ [pool {pool_slug}] deposito ticket FALLITO ({e}) — degrado alla consegna tmux storica")
        lock = _get_window_lock(target_window)
        with lock:
            delivered = send_prompt_to_tmux(target_window, prompt_text)
        if not delivered and offline_notice:
            try:
                notice_token = offline_notice["token"]
                payload = {k: v for k, v in offline_notice.items() if k != "token"}
                requests.post(f"https://api.telegram.org/bot{notice_token}/sendMessage", json=payload, timeout=5)
            except Exception as e:
                logger.error(f"⚠️ Notifica offline fallita per {target_window}: {e}")
    threading.Thread(target=_run, daemon=True).start()

def _resolve_engine_da_finestra(target_window):
    """Motore dell'agente dietro questa finestra tmux (FIX 10/08/2026, stesso
    bug gia' chiuso stanotte su send_a2a.py ed engine_adapter.
    send_via_tmux_buffer — qui e' il canale piu' sensibile di tutti, quello
    che Manfredo guarda per davvero). Riuso di agent_db.get_db() +
    engine_adapter.get_engine(), lo stesso schema gia' in produzione in
    engine_budget.py::agenti_vivi(). Fallback 'claude' se non risolve:
    comportamento storico invariato per chi gia' funzionava."""
    try:
        db = agent_db.get_db()
        for _, ag in db.items():
            if (ag.get("tmux_window") or "").lower() == target_window.lower():
                return engine_adapter.get_engine(ag)
    except Exception:
        pass
    return "claude"


def send_prompt_to_tmux(target_window, prompt_text):
    logger.info(f"🚀 Inoltro Mono-Blocco Bracketed Paste a {target_window}...")
    engine = _resolve_engine_da_finestra(target_window)
    # 22/08/2026 FIX classe "match per prefisso" (funzione condivisa tmux_esatto):
    # normalizzazione UNICA qui — tutti i send-keys/load-buffer/paste-buffer sotto
    # usano da ora il target ESATTO. Prima, un agente con la finestra chiusa faceva
    # consegnare il messaggio di Manfredo alla finestra con lo stesso PREFISSO di
    # nome (es. AGY-PUBLIO -> AGY-PUBLIO-2): messaggio a un ALTRO agente, in
    # silenzio. La risoluzione dell'engine sopra resta sul nome dichiarato.
    target_window = tmux_esatto.target_finestra_esatto(target_window)

    # RIMOSSA (04/08/2026, richiesta esplicita di Manfredo) l'attesa lunga
    # (fino a 5 minuti) prima di incollare quando il pannello risultava
    # "generating". Causava una coda di messaggi visibilmente fermi ogni
    # volta che un agente era impegnato — Manfredo vuole consegna immediata,
    # non un'attesa silenziosa. Claude Code accetta testo incollato anche
    # mentre genera (va nell'input box, in coda per il prossimo turno); il
    # rischio reale che questa attesa mitigava era solo che l'Invio successivo
    # potesse non essere recepito nella finestra di transizione generazione→
    # idle — rischio già coperto dal loop di verifica/re-invio più sotto
    # (righe "for attempt in range(4)"), che ri-incolla e ri-preme Invio se il
    # primo tentativo non risulta partito.

    # Nomi univoci per processo/millisecondo (DEV-006, 05/08/2026): buffer tmux
    # E file di appoggio sono globali al server tmux — con nomi fissi due invii
    # ravvicinati (es. Manfredo che scrive a due topic in rapida sequenza) si
    # sovrascrivono a vicenda e un agente può ricevere l'istruzione destinata a
    # un altro. Nessun lock/attesa aggiunta: ogni invio ha il proprio nome e il
    # paste resta veloce esattamente come prima.
    unique = f"{os.getpid()}_{int(time.time() * 1000)}"
    buf_file = f"/tmp/unified_prompt_paste_{unique}.txt"
    buf_name = f"unified_{unique}"
    with open(buf_file, "w", encoding="utf-8") as f:
        f.write(prompt_text.strip())

    # Sblocco preventivo da eventuali prompt/modalità sospese o bloccate nel
    # terminale — SOLO per agy (commento storico, gia' cosi' da prima): su
    # Claude Code, qwen e deepcode il C-c è la scorciatoia per INTERROMPERE
    # una generazione in corso, e a questo punto il pannello NON è detto sia
    # libero — inviarlo comunque rischierebbe di interrompere una
    # generazione già partita invece di aspettarla.
    # FIX 10/08/2026: il controllo era `not is_claude_pane` ("bypass
    # permissions on" assente), equivalente ad "agy" solo finché esistevano
    # soltanto claude/agy — con qwen e deepcode aggiunti, la condizione si è
    # allargata in silenzio a QUALUNQUE motore diverso da claude: ogni
    # messaggio Telegram per un agente qwen o deepcode mandava un C-c anche
    # a metà generazione, rischiando di interromperla. Ripristinato
    # l'intento originale dichiarato nel commento: SOLO agy.
    if engine == "agy":
        subprocess.run(f"tmux send-keys -t '{target_window}' C-c", shell=True)
        time.sleep(0.1)
    # FIX 10/08/2026 (task Leo, incidente reale "ciao tech" rimasto incollato
    # nel posto sbagliato invece di essere inviato): SOLO per deepcode, ESC
    # PRIMA di incollare — pulizia preventiva di eventuali stati di
    # menu/dialog/AskUserQuestion aperti nel pannello che altrimenti
    # intercettano il paste al posto del campo di testo normale. Diverso dal
    # C-c di agy sopra: qui non si interrompe una generazione (rischio
    # esplicitamente accettato da Manfredo per deepcode, gia' comportamento
    # di default nel campanello A2A di send_a2a.py/a2a_bell_relay.py — stesso
    # pattern, validato da Dario 16/16 test), si ripulisce lo stato UI anche
    # a pannello idle. ESC su un pannello gia' pulito e' un no-op innocuo.
    # goose e' TUI autonoma: niente ESC-preventivo (incollare mentre esegue un
    # tool fallisce), va giu' al loop generale che verifica via is_pane_generating.
    elif engine == "deepcode":
        subprocess.run(f"tmux send-keys -t '{target_window}' Escape", shell=True)
        time.sleep(2.0)

    cmd_load = f"tmux load-buffer -b {buf_name} {buf_file}"
    cmd_paste = f"tmux paste-buffer -p -b {buf_name} -t '{target_window}'"

    def _cleanup():
        # Comandi tmux locali istantanei, non rallentano il percorso di paste.
        subprocess.run(f"tmux delete-buffer -b {buf_name}", shell=True)
        try:
            os.remove(buf_file)
        except OSError:
            pass

    # P3 21/08/2026 (exec_p3_patch_input_box, mandato samantha_1): nella
    # casella c'e' del testo che NON e' nostro? Tipicamente Manfredo che sta
    # scrivendo un ordine a mano in quella finestra tmux. Incollarci sopra glielo
    # ACCODA dentro la frase e l'End+C-m poco piu' sotto la sottomette monca:
    # invio prematuro al posto della perdita silenziosa, cioe' la sovracorrezione
    # da evitare. Si RIMANDA e si torna False — che su questo canale NON e' una
    # perdita silenziosa: il chiamante (riga ~665) fa scattare la notifica
    # offline a Manfredo su Telegram, quindi il messaggio viene dichiarato.
    try:
        from send_a2a import _input_box_e_di_altri, _log_pulizia_input_box, _leggi_input_box
        _altrui = _input_box_e_di_altri(target_window, prompt_text, None)
    except Exception as _e:
        # verifica non disponibile: comportamento storico invariato, si incolla.
        _altrui = False
    if _altrui:
        try:
            _log_pulizia_input_box(target_window, None, _leggi_input_box(target_window),
                                   "RIMANDATO_PRE_PASTE", "unified_telegram_engine:send_prompt_to_tmux")
        except Exception:
            pass
        _found = _leggi_input_box(target_window)
        logger.error(f"⏸️ {target_window}: nell'input box c'e' testo NON nostro (qualcuno "
                     f"sta scrivendo a mano) — Trovato: {_found!r}. NON incollo e NON premo Invio. "
                     f"Messaggio NON consegnato, primi 120 char: {prompt_text.strip()[:120]!r}")
        _cleanup()
        return False

    subprocess.run(cmd_load, shell=True)
    time.sleep(0.1)
    paste_res = subprocess.run(cmd_paste, shell=True)

    # Universal Dynamic Resolution if window target fails
    if paste_res.returncode != 0 and ":" in target_window:
        sess_name, win_name = target_window.split(":", 1)
        logger.warning(f"⚠️ Finestra '{target_window}' non trovata! Avvio Risoluzione Dinamica Universale su sessione '{sess_name}'...")

        # Interroga le finestre realmente esistenti nella sessione Tmux
        res_list = subprocess.run(f"tmux list-windows -t {sess_name} 2>/dev/null", shell=True, capture_output=True, text=True)
        if res_list.returncode == 0 and res_list.stdout.strip():
            lines = res_list.stdout.strip().split("\n")
            first_win_idx = lines[0].split(":")[0]
            resolved_window = f"{sess_name}:{first_win_idx}"
            logger.info(f"⚡ Risoluzione Universale Riuscita! Dirottamento automatico su '{resolved_window}'")

            # Sblocco preventivo anche sulla finestra risolta — SOLO agy
            # (FIX 10/08/2026, stesso motivo della finestra originale sopra:
            # 'resolved_window' può essere un motore qualunque, mai dedotto).
            if _resolve_engine_da_finestra(resolved_window) == "agy":
                subprocess.run(f"tmux send-keys -t '{resolved_window}' C-c", shell=True)
                time.sleep(0.1)
            # Incolla sulla finestra risolta ed avvisa Betty
            # (DEV-006: usa la VARIABILE buf_name, non il nome scritto a mano —
            # col nome fisso questo ramo incollava il buffer di un altro invio)
            subprocess.run(f"tmux paste-buffer -p -b {buf_name} -t '{resolved_window}'", shell=True)
            target_window = resolved_window

            # Notifica Betty dell'avvenuto intervento per l'auto-healing automatico
            alert_msg = f"[SISTEMA AUTO-HEALING TELEGRAM]: La finestra '{target_window}' era disallineata. È stato eseguito il dirottamento dinamico automatico su '{resolved_window}'. Si prega di verificare la configurazione in DB per {sess_name}!"
            logger.warning(alert_msg)
        else:
            # La SESSIONE stessa non esiste (studio spento/always_on=false, non
            # solo una finestra disallineata): prima veniva persa silenziosamente
            # (solo un warning nel log, nessun segnale a chi aveva inviato il
            # messaggio o fatto lo switch — sembrava che l'agente destinatario
            # "non rispondesse" senza nessuna spiegazione visibile).
            logger.error(f"❌ Sessione '{sess_name}' non esiste (studio spento/non attivo): messaggio per '{target_window}' non recapitato.")
            _cleanup()
            return False

    # Claude Code (a differenza di agy) elabora il paste in modo asincrono: un
    # invio troppo rapido del Return lascia il testo incollato ma non inviato
    # (verificato empiricamente su Betty/Valerio). Scaliamo l'attesa con la
    # lunghezza del testo, come in engine_adapter.send_via_tmux_buffer, e
    # premiamo End prima di Return per uscire da un eventuale stato di
    # "selezione" del testo appena incollato (osservato empiricamente:
    # digitare dopo un paste lungo sostituiva il testo invece di aggiungersi).
    settle = min(1.5 + len(prompt_text) / 800, 5.0)
    time.sleep(settle)
    subprocess.run(f"tmux send-keys -t '{target_window}' End", shell=True)
    time.sleep(0.2)

    # Il Return non sempre viene recepito al primo colpo (osservato ripetutamente
    # su sessioni appena riprese con --resume, o con più messaggi ravvicinati):
    # verifica che la generazione sia partita davvero, altrimenti ritenta.
    #
    # IMPORTANTE: un C-m a VUOTO (senza ripassare il testo) spesso NON sottomette
    # il buffer già incollato — bug reale confermato via A2A da più agenti
    # (Aurelio, poi riscontrato anche su Alfredo/altri): la finestra resta ferma
    # indefinitamente col testo visibile ma mai processato. Il workaround
    # verificato è ripassare il TESTO VERO (non solo Invio) ad ogni ritentativo:
    # load-buffer + paste-buffer + End + C-m, la stessa sequenza usata per il
    # primo tentativo, non un Invio isolato.
    #
    # FIX 10/08/2026 (stesso bug gia' chiuso stanotte su send_a2a.py ed
    # engine_adapter.send_via_tmux_buffer, qui sul canale piu' sensibile —
    # l'incidente "Aurelio poi Alfredo" sopra descritto e' con ogni
    # probabilita' proprio questo): il check "generazione partita" era
    # hardcoded sulla UI di Claude Code ("bypass permissions on" mai presente
    # su qwen/deepcode/agy => sempre vero al primo giro, falso positivo). Ora
    # usa engine_adapter.is_pane_generating(engine, ...), engine-aware. In
    # piu' (bug del duplicato trovato da Betty su Regista/A2A la stessa notte):
    # prima di ripassare il paste al tentativo N+1, controlla se il testo e'
    # GIA' visibile in coda al pannello (pannello occupato con un tool lungo,
    # ne' idle ne' rilevato come "sta generando") — se si', salta il paste e
    # manda solo un altro C-m, non impila copie identiche del messaggio.
    marcatore_paste = prompt_text.strip().splitlines()[0][:120] if prompt_text.strip() else None

    # 15/08/2026 (audit Igor + validazione exec, task Samantha 8014): per
    # DEEPCODE "interrompi sempre + single-shot". L'ESC preventivo è già stato
    # inviato a monte (riga 654), quindi il pannello è pulito; il paste e
    # l'End sono già stati fatti sopra. Qui basta UN solo C-m e chiudere:
    # si salta il retry-su-falso-negativo basato su is_pane_generating, che su
    # deepcode è inaffidabile sia sul testo sia sui tool MCP (falso negativo ->
    # re-paste -> doppi turni, come per Cosimo su claude). Resta per agy/qwen.
    # NOTA (17/08/2026, migrazione goose): goose usa il loop generale qui sotto
    # (verifica sottomissione via is_pane_generating), NON questo ramo
    # deepcode single-shot: il suo ESC+ripaste causerebbe a goose un
    # doppio-paste (osservato sul canale A2A nel collaudo e2e). Il solo
    # ESC-preventivo a monte (riga 655) è sufficiente per goose.
    # 18/08/2026 (Samantha, ordine Manfredo "rivedere sto service, ancora
    # incolla piu' messaggi" — caso dal vivo: msg 10254 consegnato DUE volte
    # a Samantha stessa): ESTESO A CLAUDE il ramo single-shot guardato. Prima
    # claude passava dal loop generale a 4 tentativi, dove un falso negativo
    # di is_pane_generating (pannello occupato da un tool lungo, footer non
    # riconosciuto) faceva RIPASSARE il testo gia' sottomesso -> seconda
    # copia in coda -> messaggio doppio a fine turno. Il ramo qui sotto e' la
    # sequenza collaudata su deepcode (ESC verificato + paste + UN solo retry
    # con verifica ritardata, mai 4 re-paste) e le funzioni di verifica sono
    # le stesse di send_a2a: _leggi_input_box PROVATA sul campo sui pani
    # claude oggi (lettura corretta su Luca vuoto e Simone con testo appeso).
    if engine in ("deepcode", "claude"):
        # 17/08/2026 (exec_debug_comunicazione_interna, task Samantha): questo
        # branch era la GEMELLA NON ALLINEATA di send_a2a.py righe 517-552. Il
        # fix di Igor (task 6320) era stato applicato solo al canale A2A: qui,
        # sul canale TELEGRAM, il primo invio era ancora il solo End+C-m su
        # testo già incollato — la sequenza che Igor ha misurato fallire nel
        # ~72% dei casi su deepcode. Misurato sui suoi dati
        # (/tmp/send_a2a_esiti.log, taglio alle 02:32 = mtime del suo fix):
        # CLEAN 31,6% prima -> 78,8% dopo (esclusa la finestra patologica
        # AGY-GIORGIO). Qui si allinea la stessa identica sequenza collaudata:
        # ESC + re-load + re-paste + End + C-m AL PRIMO COLPO, verifica a 5,0s
        # (non 2,0s: a 2s la verifica è ancora un falso "pendente" mentre la
        # UI di deepcode sta ancora ridisegnando), e UN solo retry, mai un
        # loop di 4 re-paste.
        # Le funzioni di verifica vivono in send_a2a.py: sono le STESSE usate
        # dal canale A2A, cosi' i due canali non possono divergere di nuovo
        # (era esattamente questo il bug: la gemella rimasta indietro).
        try:
            from send_a2a import (_leggi_input_box, _input_ha_questo_a2a,
                                  _append_send_a2a_esito, _attendi_sottomissione,
                                  _pulisci_input_box, EsitoPulizia)
            _verifica_ok = True
        except Exception as e:
            _verifica_ok = False
            logger.warning(f"⚠️ deepcode: verifiche da send_a2a non disponibili ({e}) — invio senza guardrail.")

        # 18/08/2026: la vecchia regex r"\[MSG_ID:\s*\d+\]" NON matchava MAI —
        # nel prompt reale MSG_ID sta IN MEZZO alla riga di header, senza
        # parentesi proprie ("... | MSG_ID: 10254 | TMUX_TARGET: ..."), quindi
        # la chiave cadeva sempre sul fallback. Bug documentato dal 17/08,
        # fixato oggi su ordine di Manfredo insieme all'estensione del ramo.
        _m = re.search(r"MSG_ID:\s*\d+", prompt_text)
        _chiave = _m.group(0) if _m else (marcatore_paste or "")

        # GUARDRAIL 17/08/2026: si ripassa il testo SOLO se l'ESC ha davvero
        # ripulito l'input box. Se non l'ha ripulita (pannello occupato/lento)
        # il paste si ACCODA invece di sostituire e il destinatario riceve il
        # messaggio DOPPIO — incidente reale osservato su AGY-GIORGIO.
        if _verifica_ok and _chiave:
            _pulito = _pulisci_input_box(target_window, prompt_text, _chiave)
        else:
            subprocess.run(f"tmux send-keys -t '{target_window}' Escape", shell=True)
            time.sleep(1.5)
            _pulito = True
        # P3 21/08/2026: il vecchio codice premeva End+C-m INCONDIZIONATO qui
        # sotto, qualunque cosa avesse risposto la pulizia. Con la sola guardia
        # dentro _pulisci_input_box avrebbe sottomesso la frase a meta' di
        # Manfredo invece di cancellarla: guasto diverso, non minore.
        if _verifica_ok and _pulito is EsitoPulizia.NON_MIO:
            _append_send_a2a_esito("RIMANDATO", "tg-deepcode", target_window, _chiave)
            logger.error(f"⏸️ {target_window}: testo NON nostro nell'input box — NON premo "
                         f"Invio (sottometterei una frase altrui a meta'). Messaggio NON "
                         f"consegnato, primi 120 char: {prompt_text.strip()[:120]!r}")
            _cleanup()
            return False
        if _pulito:
            subprocess.run(cmd_load, shell=True)
            time.sleep(0.1)
            subprocess.run(f"tmux paste-buffer -p -b {buf_name} -t '{target_window}'", shell=True)
            time.sleep(0.3)
        subprocess.run(f"tmux send-keys -t '{target_window}' End", shell=True)
        time.sleep(0.2)
        subprocess.run(f"tmux send-keys -t '{target_window}' C-m", shell=True)
        logger.info(f"🚀 deepcode single-shot (sequenza allineata a send_a2a.py): prompt inviato a {target_window}.")
        # 16/08/2026 (fix deriva single-shot, task Samantha 6f59): un invio
        # davvero fallito (prompt rimasto incollato-ma-mai-sottomesso) con il
        # single-shot resta bloccato per sempre. Recupero con UNA verifica
        # ritardata dell'input box: se quel prompt è ancora lì NON sottomesso,
        # un solo retry (ri-ESC + ripaste + invio), mai un loop di 4 re-paste.
        # 29/08/2026 (exec_fix_paste_composer_v3, mandato samantha_1): fino ad
        # oggi questo intero branch chiudeva SEMPRE con `return True`, anche
        # sull'esito BLOCCO qui sotto (sottomissione mai avvenuta, pulizia di
        # recupero fallita). dispatch_prompt_async() (riga ~692) usa proprio
        # questo valore per decidere se avvisare Manfredo via offline_notice
        # ("il messaggio non è stato recapitato") — con `True` incondizionato
        # quell'avviso non scattava MAI su un composer rimasto bloccato: il
        # testo restava visibile e fermo per ore, scoperto solo a occhio da un
        # umano (sintomo esatto del mandato). Bug riprodotto e verificato in
        # isolato (monkeypatch, nessuna finestra reale toccata) in
        # /tmp/betty_docs/test_bug_return_true_incondizionato.py.
        # Fix minimo: `_esito_consegna` traccia l'esito VERO e diventa il
        # valore di ritorno. Il ramo "verifica non disponibile" (eccezione)
        # resta True come da comportamento storico invariato — qui si aggiunge
        # solo il segnale mancante per BLOCCO/RETRY, non si tocca nient'altro.
        _esito_consegna = True
        try:
            if not (_verifica_ok and _chiave):
                raise RuntimeError("verifica non disponibile")
            # Etichetta di motore DISTINTA ('tg-deepcode', non 'deepcode')
            # apposta: il canale Telegram deve restare separabile da quello
            # A2A nello stesso log, altrimenti le statistiche sul canale A2A
            # (`engine=deepcode`) verrebbero inquinate da invii che A2A non
            # sono. Additivo: nessuna riga esistente cambia significato.
            if _attendi_sottomissione(target_window, prompt_text, _chiave):
                _append_send_a2a_esito("CLEAN", "tg-deepcode", target_window, _chiave)
            elif not _pulisci_input_box(target_window, prompt_text, _chiave):
                # Non sottomesso E l'ESC non ripulisce: un re-paste qui
                # aggiungerebbe solo una copia a un pannello già ingolfato.
                _append_send_a2a_esito("BLOCCO", "tg-deepcode", target_window, _chiave)
                logger.warning(f"⚠️ {target_window} non sottomette e non onora l'ESC — "
                               f"NON re-incollo (eviterei solo un duplicato).")
                _esito_consegna = False
            else:
                subprocess.run(cmd_load, shell=True)
                time.sleep(0.1)
                subprocess.run(f"tmux paste-buffer -p -b {buf_name} -t '{target_window}'", shell=True)
                time.sleep(0.3)
                subprocess.run(f"tmux send-keys -t '{target_window}' End", shell=True)
                time.sleep(0.2)
                subprocess.run(f"tmux send-keys -t '{target_window}' C-m", shell=True)
                logger.info(f"🚀 deepcode: retry singolo su {target_window} (primo invio non sottomesso).")
                # Il retry va RIVERIFICATO come il primo colpo: prima d'oggi il
                # suo esito non veniva mai controllato, quindi un retry fallito
                # produceva comunque "RETRY" (etichetta ottimistica) e True.
                if _attendi_sottomissione(target_window, prompt_text, _chiave):
                    _append_send_a2a_esito("RETRY", "tg-deepcode", target_window, _chiave)
                else:
                    _append_send_a2a_esito("BLOCCO", "tg-deepcode", target_window, _chiave)
                    logger.warning(f"⚠️ {target_window}: anche il retry singolo non risulta "
                                   f"sottomesso entro il timeout.")
                    _esito_consegna = False
        except Exception as e:
            logger.warning(f"⚠️ deepcode verifica/retry non applicata a {target_window}: {e}")
        _cleanup()
        return _esito_consegna

    for attempt in range(4):
        if attempt > 0:
            pane_prima = ""
            if marcatore_paste:
                try:
                    pane_prima = subprocess.run(["tmux", "capture-pane", "-t", target_window, "-p", "-S", "-30"],
                                                 capture_output=True, text=True, timeout=3).stdout
                except Exception:
                    pane_prima = ""
            # FIX 14/08/2026 (report exec_debug_paste_claude, verificato dal vivo
            # 9/9 su 3 finestre): il marcatore da solo non basta per decidere se
            # ripassare il paste. Se il pannello e' occupato da un tool lungo
            # (is_pane_generating), ripassare accoderebbe una copia vera — li'
            # giusto limitarsi a Invio. Ma se e' IDLE con lo stesso testo gia'
            # incollato e mai sottomesso, un Invio isolato su Claude Code spesso
            # non lo sottomette MAI: serve ripassare lo stesso identico testo
            # prima di Invio, senza produrre doppioni visibili. Il vecchio
            # codice trattava i due casi uguali (skip sempre col marcatore).
            marcatore_presente = bool(marcatore_paste and marcatore_paste in pane_prima)
            pannello_occupato = engine_adapter.is_pane_generating(engine, pane_prima) if marcatore_presente else False
            if marcatore_presente and (pannello_occupato or engine == "agy"):
                logger.info(f"↩️ '{target_window}': testo già presente a pannello ({engine}), non lo ripeto — solo un altro invio.")
            else:
                subprocess.run(cmd_load, shell=True)
                time.sleep(0.1)
                subprocess.run(f"tmux paste-buffer -p -b {buf_name} -t '{target_window}'", shell=True)
                time.sleep(0.3)
            subprocess.run(f"tmux send-keys -t '{target_window}' End", shell=True)
            time.sleep(0.2)
        subprocess.run(f"tmux send-keys -t '{target_window}' C-m", shell=True)
        time.sleep(1.5)
        try:
            pane_check = subprocess.run(["tmux", "capture-pane", "-t", target_window, "-p", "-S", "-30"],
                                         capture_output=True, text=True, timeout=3).stdout
        except Exception:
            break
        if engine_adapter.is_pane_generating(engine, pane_check):
            break  # generazione partita, confermata dal pattern del motore giusto
        # SECONDA LETTURA RITARDATA (14/08/2026, bug reale: messaggi Telegram
        # recapitati piu' volte allo stesso agente — segnalato da Manfredo).
        # Stesso fix gia' fatto stanotte su send_a2a.py: una sola lettura a
        # 1.5s puo' cadere nell'istante in cui la TUI non ha ancora aggiornato
        # il footer dopo il C-m ("foto giusta di uno stato non ancora
        # stabile"), facendo concludere "non partito" quando invece lo era —
        # il ramo sopra ripassa ALLORA il testo (nuovo load+paste), producendo
        # un secondo incollaggio dello stesso messaggio che puo' finire
        # sottomesso anch'esso: e' cosi' che lo stesso messaggio arriva
        # duplicato. Un secondo respiro prima di ripassare riduce il falso
        # negativo alla radice invece di limitarne i danni con il dedup sotto.
        time.sleep(2.0)
        try:
            pane_check = subprocess.run(["tmux", "capture-pane", "-t", target_window, "-p", "-S", "-30"],
                                         capture_output=True, text=True, timeout=3).stdout
        except Exception:
            break
        if engine_adapter.is_pane_generating(engine, pane_check):
            break

    _cleanup()
    return True

# ---------------------------------------------------------
# 1. POLLING LOOP UNIFICATO PER OGNI BOT
# ---------------------------------------------------------
def run_bot_polling(bot_key, cfg):
    token = cfg["token"]
    api_url = f"https://api.telegram.org/bot{token}"
    offset = _load_telegram_offset(bot_key)
    logger.info(f"🤖 Bot Loop avviato per [{bot_key}]... (offset ripreso da disco: {offset})")

    while True:
        try:
            # allowed_updates esplicito è OBBLIGATORIO qui: il parametro è "sticky"
            # lato server Telegram — se anche una sola chiamata storica (di un
            # qualunque script/test/curl, anche anni fa) lo aveva impostato con una
            # lista che escludeva "callback_query", quel filtro resta attivo per
            # sempre finché non lo si sovrascrive esplicitamente, indipendentemente
            # dal client (mobile/desktop) e persino da un riavvio del nostro motore.
            # Causa reale riscontrata il 04/08/2026: i tap sui pulsanti [QUESTION_OPTIONS]
            # non arrivavano MAI (0 volte, verificato anche con getUpdates grezzo via
            # curl bypassando completamente il nostro codice) finché non è stata rifatta
            # una singola chiamata con allowed_updates esplicito — da lì i callback
            # hanno ripreso a funzionare immediatamente, senza toccare altro codice.
            allowed = urllib.parse.quote(json.dumps(["message", "callback_query", "edited_message"]))
            r = requests.get(f"{api_url}/getUpdates?offset={offset}&timeout=20&allowed_updates={allowed}", timeout=25)
            if r.status_code != 200:
                time.sleep(2)
                continue

            res = r.json()
            for update in res.get("result", []):
                update_id = update.get("update_id")
                try:

                    # Tap su un pulsante di una domanda a scelta (vedi QUESTION_OPTIONS)
                    callback_query = update.get("callback_query")
                    if callback_query:
                        cq_data = callback_query.get("data", "")
                        cq_id = callback_query.get("id")
                        cq_message = callback_query.get("message", {}) or {}
                        cq_chat_id = cq_message.get("chat", {}).get("id")
                        cq_msg_id = cq_message.get("message_id")
                        cq_sender = callback_query.get("from", {}).get("first_name", "Utente")
                        # Log immediato ad OGNI tap ricevuto, prima di qualunque parsing/match:
                        # senza questo, un tap che non matcha "Q|slug|idx" (es. pulsante vecchio/
                        # scaduto) spariva senza lasciare traccia nei log, rendendo impossibile
                        # distinguere "il tap non e' mai arrivato" da "e' arrivato ma scartato".
                        logger.info(f"🔘 [{bot_key}] Callback ricevuto da {cq_sender}: data='{cq_data}' (chat={cq_chat_id}, msg={cq_msg_id})")

                        parts = cq_data.split("|")
                        # Formato nuovo (04/08/2026): Q|slug|question_id|idx -- ogni domanda ha
                        # un ID proprio, cosi' piu' domande pendenti dello stesso agente restano
                        # tracciabili indipendentemente invece di sovrascriversi a vicenda (bug
                        # reale: con la sola chiave "slug", 8 domande mandate quasi in contemporanea
                        # lasciavano rispondibile solo l'ultima, le altre "orfane" cliccabili ma
                        # senza effetto). Pulsanti vecchi (formato Q|slug|idx, 3 parti, gia' in
                        # circolazione da prima di questo fix) non trovano piu' nulla sotto la nuova
                        # chiave composita e cadono correttamente nel ramo "domanda scaduta" sotto.
                        if len(parts) == 4 and parts[0] == "Q":
                            q_slug, q_id, q_idx_raw = parts[1], parts[2], parts[3]
                            pending_key = f"{q_slug}:{q_id}"
                            options = _PENDING_QUESTIONS.get(pending_key)
                            try:
                                q_idx = int(q_idx_raw)
                            except ValueError:
                                q_idx = -1
                            chosen_text = options[q_idx] if options and 0 <= q_idx < len(options) else None

                            if chosen_text:
                                requests.post(f"{api_url}/answerCallbackQuery", json={
                                    "callback_query_id": cq_id,
                                    "text": f"Hai scelto: {chosen_text}"
                                }, timeout=5)
                                if cq_chat_id and cq_msg_id:
                                    requests.post(f"{api_url}/editMessageReplyMarkup", json={
                                        "chat_id": cq_chat_id,
                                        "message_id": cq_msg_id,
                                        "reply_markup": {"inline_keyboard": [[{"text": f"✅ {chosen_text}", "callback_data": "noop"}]]}
                                    }, timeout=5)
                                _PENDING_QUESTIONS.pop(pending_key, None)
                                _save_pending_questions()

                                # Stesso feedback visivo "in lavorazione -> spunta finale" delle
                                # risposte a testo libero: senza questo, un tap su un pulsante non
                                # riceveva MAI la spunta di completamento (start_reaction_cycle/
                                # active_source venivano scritti solo nel ramo "message", mai qui).
                                cq_thread_id = cq_message.get("message_thread_id")
                                if cq_chat_id and cq_msg_id:
                                    start_reaction_cycle(api_url, cq_chat_id, cq_msg_id)
                                    thread_key = cq_thread_id if cq_thread_id is not None else "general"
                                    try:
                                        with open(f"/tmp/{bot_key}_{thread_key}_active_source.json", "w") as sf:
                                            json.dump({"chat_id": cq_chat_id, "message_id": cq_msg_id, "thread_id": cq_thread_id}, sf)
                                    except Exception:
                                        pass

                                target_window = resolve_agent_window(q_slug)
                                if target_window:
                                    # FIX 18/08/2026 (bug confermato da Alfredo+Luca): chosen_text era
                                    # su una riga separata dopo \n e si perdeva al paste tmux, lasciando
                                    # all'agente solo l'intestazione. Ora e' inline sulla stessa riga;
                                    # replace dei newline residui come difesa extra per garantire UNA
                                    # sola riga di consegna.
                                    chosen_inline = chosen_text.replace("\n", " ").strip()
                                    answer_prompt = f"[RISPOSTA A DOMANDA TELEGRAM - SENDER: {cq_sender} | OPZIONE SCELTA: {chosen_inline}]"
                                    _log_button_choice(cq_sender, pending_key, chosen_text, target_window)
                                    logger.info(f"🔘 [{bot_key}] {cq_sender} ha scelto '{chosen_text}' per '{q_slug}' -> {target_window}")
                                    notice = {"token": token, "chat_id": cq_chat_id, "message_thread_id": cq_thread_id,
                                              "text": f"⚠️ '{q_slug}' risulta offline in questo momento — la tua scelta non è stata recapitata. Riprova quando lo studio è attivo."} if cq_chat_id else None
                                    dispatch_prompt_async(target_window, answer_prompt, offline_notice=notice)
                                else:
                                    logger.warning(f"⚠️ Impossibile risolvere finestra per risposta a '{q_slug}'")
                            else:
                                logger.warning(f"⚠️ Callback '{cq_data}' non risolvibile: nessuna domanda pendente per '{q_slug}' (indice {q_idx_raw}).")
                                requests.post(f"{api_url}/answerCallbackQuery", json={
                                    "callback_query_id": cq_id,
                                    "text": "⚠️ Domanda scaduta o già risposta."
                                }, timeout=5)
                        continue

                    # Posizione condivisa da Manfredo (05/08/2026). Va intercettata QUI,
                    # prima del filtro sotto: la posizione "in tempo reale" arriva come
                    # `edited_message` (Telegram aggiorna lo stesso messaggio mentre lui si
                    # sposta), e quel tipo di update viene scartato due righe piu' sotto.
                    # salva_posizione ritorna None su tutto cio' che non e' una posizione,
                    # quindi non tocca il flusso normale di testo/vocali.
                    try:
                        msg_pos = update.get("message") or update.get("edited_message") or {}
                        posizione_manfredo.salva_posizione(msg_pos)
                        loc = msg_pos.get("location")
                        if loc:
                            lat, lon = loc.get("latitude"), loc.get("longitude")
                            if lat is not None and lon is not None:
                                pos_data = {
                                    "lat": lat, "lon": lon,
                                    "ts": datetime.now().isoformat(timespec="seconds"),
                                    "live_period": loc.get("live_period"),
                                    "heading": loc.get("heading"),
                                    "sender": msg_pos.get("from", {}).get("first_name", "Utente"),
                                    "chat_id": msg_pos.get("chat", {}).get("id"),
                                    "msg_id": msg_pos.get("message_id"),
                                    "maps": f"https://www.google.com/maps?q={lat},{lon}",
                                }
                                with open("/tmp/manfredo_live_position.json", "w") as f_pos:
                                    json.dump(pos_data, f_pos, ensure_ascii=False, indent=1)
                    except Exception as _e_pos:
                        logger.warning(f"posizione non salvata: {str(_e_pos)[:80]}")

                    message = update.get("message")
                    if not message:
                        continue

                    chat_id = message["chat"]["id"]
                    msg_id = message["message_id"]
                    # PUNTO 6 dedup_terza_porta: scarta i message_id gia'
                    # processati (re-offerta di getUpdates a offset non
                    # superato). Thread-safe, si marca subito: se questo
                    # update viene ri-offerto, il duplicate esce qui.
                    if _gia_visto_message(chat_id, msg_id):
                        logger.info(f"♻️ [{bot_key}] message_id {msg_id} gia' processato (chat {chat_id}) — duplicato Telegram scartato.")
                        continue
                    sender = message.get("from", {}).get("first_name", "Utente")
                    text = ""

                    # 1. Gestione Note Vocali & Audio
                    if "voice" in message or "audio" in message:
                        msg_thread_id = message.get("message_thread_id")
                        logger.info(f"🎙️ [{bot_key}] Ricevuto Messaggio Vocale da {sender}! Avvio trascrizione...")
                        voice_obj = message.get("voice") or message.get("audio")
                        file_id = voice_obj.get("file_id")

                        try:
                            file_r = None
                            for _att in range(3):
                                try:
                                    file_r = requests.get(f"{api_url}/getFile?file_id={file_id}", timeout=10).json()
                                    if file_r.get("ok"): break
                                except Exception: time.sleep(1)

                            file_path = file_r.get("result", {}).get("file_path") if file_r else None
                            if file_path:
                                file_url = f"https://api.telegram.org/file/bot{token}/{file_path}"
                                audio_data = None
                                for _att in range(3):
                                    try:
                                        audio_data = requests.get(file_url, timeout=15).content
                                        if audio_data: break
                                    except Exception: time.sleep(1)

                                if not audio_data:
                                    raise RuntimeError("Impossibile scaricare file audio da Telegram dopo 3 tentativi")

                                ogg_file = f"/tmp/voice_{msg_id}.ogg"
                                wav_file = f"/tmp/voice_{msg_id}.wav"
                                with open(ogg_file, "wb") as f: f.write(audio_data)

                                try:
                                    subprocess.run(["ffmpeg", "-y", "-i", ogg_file, "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", wav_file],
                                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
                                except subprocess.TimeoutExpired:
                                    subprocess.run(["pkill", "-9", "-f", f"ffmpeg.*voice_{msg_id}\\."],
                                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                                    logger.warning(f"⚠️ ffmpeg bloccato per voice_{msg_id} (timeout 30s): vocale saltato.")
                                    requests.post(f"{api_url}/sendMessage", json={
                                        "chat_id": chat_id,
                                        "message_thread_id": msg_thread_id,
                                        "reply_to_message_id": msg_id,
                                        "text": "⚠️ Conversione audio bloccata. Per favore rinvia la nota vocale!"
                                    }, timeout=5)
                                    text = ""

                                try:
                                    groq_key = load_secret("GROQ_API_KEY")
                                    with open(wav_file, "rb") as f_wav:
                                        for _att in range(3):
                                            try:
                                                f_wav.seek(0)
                                                gr = requests.post(
                                                    "https://api.groq.com/openai/v1/audio/transcriptions",
                                                    headers={"Authorization": f"Bearer {groq_key}"},
                                                    files={"file": f_wav},
                                                    data={"model": "whisper-large-v3", "language": "it"},
                                                    timeout=15
                                                )
                                                if gr.status_code == 200:
                                                    text = gr.json().get("text", "").strip()
                                                    logger.info(f"🗣️ Trascrizione Vocale Groq Riuscita: '{text}'")
                                                    break
                                                else:
                                                    time.sleep(1)
                                            except Exception:
                                                time.sleep(1)
                                except Exception as ge:
                                    logger.warning(f"⚠️ Groq API Fallita ({ge}). Avviso utente su Telegram...")
                                    requests.post(f"{api_url}/sendMessage", json={
                                        "chat_id": chat_id,
                                        "message_thread_id": msg_thread_id,
                                        "reply_to_message_id": msg_id,
                                        "text": "⚠️ [Avviso Rete]: Momentanea micro-interruzione della rete API Groq. Riavvio in corso, per favore rinvia la nota vocale!"
                                    }, timeout=5)
                        except Exception as ve:
                            logger.error(f"❌ Errore trascrizione vocale: {ve}")
                            try:
                                requests.post(f"{api_url}/sendMessage", json={
                                    "chat_id": chat_id,
                                    "message_thread_id": msg_thread_id,
                                    "reply_to_message_id": msg_id,
                                    "text": f"⚠️ Errore ricezione vocale ({str(ve)[:60]}). Per favore rinvia il messaggio vocale."
                                }, timeout=5)
                            except Exception: pass

                    elif "text" in message:
                        text = message["text"]

                    # 06/08/2026 (direttiva Manfredo via Simone): le posizioni NON entrano nei prompt
                    # (niente flusso continuo, niente spreco token). Vengono solo persistite su file;
                    # le legge on-demand il tool MCP get_manfredo_location quando un agente ne ha bisogno.
                    elif "location" in message or "venue" in message:
                        lat = lon = None  # garantito definito anche se il try sotto solleva prima di assegnarle
                        try:
                            loc = message.get("location") or {}
                            lat, lon = loc.get("latitude"), loc.get("longitude")
                            if lat is not None and lon is not None:
                                pos = {
                                    "lat": lat, "lon": lon,
                                    "ts": datetime.now().isoformat(timespec="seconds"),
                                    "live_period": loc.get("live_period"),
                                    "heading": loc.get("heading"),
                                    "sender": sender, "chat_id": chat_id, "msg_id": msg_id,
                                    "maps": f"https://www.google.com/maps?q={lat},{lon}",
                                }
                                with open("/tmp/manfredo_live_position.json", "w") as f_pos:
                                    json.dump(pos, f_pos, ensure_ascii=False, indent=1)
                                logger.info(f"📍 [{bot_key}] Posizione GPS da {sender} persistita: {lat},{lon} (live_period={loc.get('live_period')})")

                                # --- START LOCATION TRACKER ---
                                HOME_LAT = 41.902782  # Da aggiornare con le coordinate reali
                                HOME_LON = 12.496366  
                                import math
                                def calc_dist(lat1, lon1, lat2, lon2):
                                    R = 6371000
                                    phi1, phi2 = math.radians(lat1), math.radians(lat2)
                                    dphi, dlam = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
                                    a = math.sin(dphi/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(dlam/2)**2
                                    return R * (2 * math.atan2(math.sqrt(a), math.sqrt(1-a)))
                                
                                if calc_dist(lat, lon, HOME_LAT, HOME_LON) < 100:
                                    # subprocess è già importato a livello di modulo (riga 8) — un
                                    # import locale qui rendeva il nome locale per TUTTA la funzione
                                    # run_bot_polling(), causando UnboundLocalError sull'uso a riga 818
                                    # (trascrizione vocale, eseguito PRIMA nel flusso). Bug reale che ha
                                    # rotto ogni messaggio vocale su tutti i bot per una sera, trovato da
                                    # Betty Gestione Agenti.
                                    msg = "[A2A_FROM:telegram_engine] [A2A_TYPE:report] Notifica di sistema: Manfredo è arrivato a casa (Live Location)."
                                    subprocess.Popen(["/root/ecosistema_agenti/core/venv/bin/python3", "/root/ecosistema_agenti/core/send_a2a.py", "betty:agy-Simone", msg])
                                    logger.info("📍 Location Tracker: Inviato A2A a Simone (a casa).")
                                # --- END LOCATION TRACKER ---

                        except Exception as le:
                            logger.error(f"❌ Errore persistenza posizione: {le}")
                        # 16/08/2026 (bug reale segnalato da Argo): prima qui text veniva
                        # SEMPRE azzerato — la posizione condivisa finiva persistita su
                        # /tmp/manfredo_live_position.json ma MAI inoltrata all'agente del
                        # topic in cui Manfredo l'aveva condivisa (lui vedeva solo un
                        # eventuale testo/caption separato, mai le coordinate). Ora, se le
                        # coordinate sono state lette con successo, le rendiamo leggibili nel
                        # prompt che arriva all'agente — non solo al tracker "arrivo a casa".
                        if lat is not None and lon is not None:
                            text = (f"[Posizione condivisa: lat={lat}, lon={lon} — "
                                    f"https://www.google.com/maps?q={lat},{lon}]")
                        else:
                            text = ""  # nessuna iniezione nel prompt

                    elif "photo" in message or "document" in message or "video" in message or "video_note" in message or "animation" in message:
                        text = message.get("caption", "")
                        try:
                            if "photo" in message:
                                file_id = message["photo"][-1]["file_id"]  # risoluzione piu' alta
                                ext = "jpg"
                            elif "video" in message:
                                vid = message["video"]
                                file_id = vid["file_id"]
                                ext = vid.get("file_name", "").split(".")[-1] if "." in vid.get("file_name", "") else "mp4"
                            elif "video_note" in message:
                                file_id = message["video_note"]["file_id"]
                                ext = "mp4"
                            elif "animation" in message:
                                file_id = message["animation"]["file_id"]
                                ext = "mp4"
                            else:
                                doc = message["document"]
                                file_id = doc["file_id"]
                                ext = os.path.splitext(doc.get("file_name", ""))[1].lstrip(".") or "bin"

                            file_r = requests.get(f"{api_url}/getFile?file_id={file_id}", timeout=10).json()
                            tg_file_path = file_r.get("result", {}).get("file_path")
                            if tg_file_path:
                                file_url = f"https://api.telegram.org/file/bot{token}/{tg_file_path}"
                                media_data = requests.get(file_url, timeout=30).content
                                local_dir = "/tmp/telegram_incoming"
                                os.makedirs(local_dir, exist_ok=True)
                                local_path = f"{local_dir}/{bot_key}_{msg_id}.{ext}"
                                with open(local_path, "wb") as f:
                                    f.write(media_data)
                                logger.info(f"📹 [{bot_key}] Ricevuto video/media da {sender}, salvato in {local_path}")
                                note = f"[FILE RICEVUTO SU TELEGRAM: {local_path}] Usa il tuo tool di lettura file per visualizzarlo prima di rispondere."
                                text = f"{text}\n{note}".strip()
                        except Exception as pe:
                            logger.error(f"❌ Errore download immagine/video/documento: {pe}")

                    if not text:
                        continue

                    # ═══ CORSIA VELOCE RADIO (05/08/2026) ═══
                    # Un "alza il volume" detto a voce impiegava 1,5-2 MINUTI: i secondi
                    # stavano in due turni di LLM in fila (chi inoltra + chi esegue), non
                    # nella rete. Qui gli ordini INEQUIVOCABILI vengono eseguiti subito,
                    # senza svegliare nessun agente: restano solo trascrizione + il poll
                    # da 3s della pagina.
                    # Se la frase non e' un ordine chiaro, interpreta() torna None e il
                    # messaggio prosegue INTATTO come sempre: meglio non intercettare
                    # dieci ordini validi che eseguirne uno sbagliato, perche' qui non
                    # c'e' nessuno che ragiona e possa accorgersi di aver capito male.
                    try:
                        _azione = radio_comandi.interpreta(text)
                        if _azione:
                            _esito = radio_comandi.esegui(_azione)
                            if _esito:
                                logger.info(f"⚡ Corsia veloce radio: '{text[:40]}' -> {_esito}")
                                requests.post(f"{api_url}/sendMessage", json={
                                    "chat_id": chat_id,
                                    "message_thread_id": message.get("message_thread_id"),
                                    "text": _esito,
                                }, timeout=5)
                                continue   # eseguito: NON si inoltra, o l'agente rifarebbe la stessa cosa
                    except Exception as _ce:
                        # Un errore qui non deve mai fermare il messaggio: si prosegue
                        # per la strada normale come se la corsia veloce non esistesse.
                        logger.warning(f"corsia veloce non applicata: {str(_ce)[:80]}")

                    # ═══ CORSIA VELOCE OPENLLM (16/08/2026, task Alfredo 3a77) ═══
                    # Il topic dedicato (thread_id in agent_map per lo slug 'openllm')
                    # va DRITTO all'endpoint del modello attivo su RunPod (Hermes/Flux/
                    # Wan...), non all'agente conversazionale OpenLLM — quello resta
                    # per controllo/debug. Se nessun modello è attivo, relay_to_active_model
                    # ritorna None e il messaggio prosegue sul routing normale (l'agente
                    # OpenLLM può rispondere "nessun modello acceso ora").
                    try:
                        _msg_thread_id_openllm = message.get("message_thread_id")
                        # 18/08/2026 (Samantha, dopo il primo messaggio vero di Manfredo
                        # finito a lei invece che al modello): la corsia NON usa piu'
                        # l'agent_map — il record relay 'openllm' non ha tmux_window (di
                        # proposito) e la sync lo SCARTA (riga ~141), quindi il match
                        # sull'agent_map non scattava mai. Ora e' il file di stato
                        # .agents/openllm_active_model.json a dichiarare chat_id+thread_id
                        # a cui si applica (proposta originale di Alfredo), e si confronta
                        # la COPPIA (chat, thread) — mai il thread da solo, thread_id
                        # uguali esistono in gruppi diversi. File assente o senza campi
                        # = corsia disarmata, il messaggio prosegue sul routing normale.
                        # Storia: il 17/08 la tupla conteneva anche 'corsaro' e il file
                        # poteva dirottare il suo topic; rimosso, e ora non c'e' piu'
                        # nessuna tupla di slug — decide solo il file.
                        _rl_chat, _rl_thread = openllm_relay.destinazione()
                        _is_openllm_thread = (
                            _rl_thread is not None and _rl_chat is not None
                            and _rl_thread == _msg_thread_id_openllm
                            and _rl_chat == chat_id
                        )
                        if _is_openllm_thread:
                            _risposta_modello = openllm_relay.relay_to_active_model(text, msg_id=msg_id)
                            if _risposta_modello is not None:
                                # stringa vuota (modalità agentica) = consegnato al
                                # bridge esterno via dropbox, la risposta arriva da
                                # lui sul canale recap standard — non mandare nulla qui.
                                if _risposta_modello:
                                    logger.info(f"🧠 Corsia veloce OpenLLM: relay diretto al modello attivo (thread {_msg_thread_id_openllm})")
                                    requests.post(f"{api_url}/sendMessage", json={
                                        "chat_id": chat_id,
                                        "message_thread_id": _msg_thread_id_openllm,
                                        "text": _risposta_modello[:4000],
                                    }, timeout=95)
                                else:
                                    logger.info(f"🧠 Corsia veloce OpenLLM: consegnato al bridge agentico esterno (thread {_msg_thread_id_openllm})")
                                continue  # gestito qui: non si inoltra all'agente OpenLLM
                    except Exception as _oe:
                        logger.warning(f"corsia veloce OpenLLM non applicata: {str(_oe)[:80]}")

                    # Reply Telegram ("rispondi a...") a un messaggio precedente: senza
                    # questo, l'agente riceve solo il testo della risposta, senza sapere
                    # a cosa si riferisce — Telegram manda l'originale in reply_to_message,
                    # ma finora veniva ignorato completamente.
                    reply_src = message.get("reply_to_message")
                    if reply_src:
                        orig_sender = reply_src.get("from", {}).get("first_name", "Qualcuno")
                        orig_text = reply_src.get("text") or reply_src.get("caption") or "[messaggio senza testo/media]"
                        orig_msg_id = reply_src.get("message_id")
                        text = (
                            f"[IN RISPOSTA AL MESSAGGIO DI {orig_sender} (MSG_ID ORIGINALE: {orig_msg_id}): \"{orig_text}\"]\n"
                            f"{text}\n"
                            f"(Se la tua risposta è specificamente per {orig_sender}, usa [REPLY_TO_MSG_ID: {orig_msg_id}] "
                            f"nel recap per collegarla visibilmente al suo messaggio su Telegram, non a quello di chi ti ha inoltrato.)"
                        )

                    msg_thread_id = message.get("message_thread_id")
                    logger.info(f"📩 [{bot_key}] Ricevuto da {sender} (Chat: {chat_id} | Thread: {msg_thread_id}): '{text[:40]}...'")

                    # ⚡ FEEDBACK "IN LAVORAZIONE": reaction che cambia icona in loop
                    # finché non arriva il recap dell'agente (vedi stop_reaction_cycle
                    # nel watcher). Sostituisce la vecchia singola reaction statica,
                    # che non distingueva "sta ancora lavorando" da "si è bloccato".
                    start_reaction_cycle(api_url, chat_id, msg_id)

                    # Salva sorgente attiva per la risposta. UN FILE PER TOPIC (non per bot):
                    # con più agenti/topic sullo stesso bot, un singolo file condiviso veniva
                    # sovrascritto da qualunque messaggio arrivasse per ultimo su QUALSIASI
                    # topic, facendo fallire silenziosamente il controllo "thread_id combacia"
                    # più sotto — il messaggio originale restava per sempre con la reaction
                    # "in lavorazione" mai chiusa (bug segnalato: messaggi mai segnati come risposti).
                    cfg["chat_id"] = str(chat_id)
                    msg_thread_id = message.get("message_thread_id")
                    thread_key = msg_thread_id if msg_thread_id is not None else "general"
                    with open(f"/tmp/{bot_key}_{thread_key}_active_source.json", "w") as sf:
                        json.dump({"chat_id": chat_id, "message_id": msg_id, "thread_id": msg_thread_id}, sf)

                    # Routing Agente
                    target_window = None
                    text_lower = text.lower()

                    # Dynamic Topic Thread Routing for all vertical supergroups
                    msg_thread_id = message.get("message_thread_id")
                    topic_name = "General"
                    if msg_thread_id and "agent_map" in cfg:
                        for ag_name, ag_info in cfg["agent_map"].items():
                            if isinstance(ag_info, dict) and ag_info.get("thread_id") == msg_thread_id:
                                target_window = ag_info.get("window")
                                topic_name = ag_name.upper()
                                break

                    _via_default = False
                    if not target_window:
                        target_window = cfg.get("default_window")
                        _via_default = target_window is not None

                    # 18/08/2026 (Samantha, segnalazione Simone dopo il caso reale
                    # "cardinale Tomasi" → Publio): un messaggio scritto FUORI dai
                    # topic in un gruppo a topic cade sul default_agent in
                    # silenzio — per il mittente e' indistinguibile da una
                    # consegna al destinatario giusto, e se il default non c'entra
                    # il messaggio e' di fatto perso (famiglia "invio che promette
                    # un seguito senza raccoglierlo", lato canale di Manfredo).
                    # Ora il mittente riceve un AVVISO in chat con scritto A CHI
                    # e' finito il messaggio. Escluso il tipo 'thread_mapped'
                    # (gruppo Domenico): li' il general E' il canale by-design,
                    # ci scrivono i committenti esterni e l'avviso sarebbe rumore
                    # verso persone che non c'entrano con l'ecosistema.
                    if _via_default and cfg.get("agent_map") and cfg.get("type") != "thread_mapped":
                        try:
                            requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json={
                                "chat_id": chat_id, "reply_to_message_id": msg_id,
                                "text": f"↪️ Messaggio scritto fuori dai topic: consegnato a '{cfg.get('default_agent')}' (destinatario di default del gruppo). Se non era per lui, riscrivilo nel topic giusto."},
                                timeout=10)
                        except Exception:
                            pass  # l'avviso e' un di piu': mai bloccare la consegna

                    prompt = f"[DOMANDA DA TELEGRAM - SENDER: {sender} | CHAT_ID: {chat_id} | TOPIC_NAME: {topic_name} | THREAD_ID: {msg_thread_id or 'General'} | MSG_ID: {msg_id} | TMUX_TARGET: {target_window}]\n{text}"
                    if target_window:
                        notice = {"token": token, "chat_id": chat_id, "message_thread_id": msg_thread_id, "reply_to_message_id": msg_id,
                                  "text": "⚠️ Lo studio/agente di questo topic risulta offline in questo momento — il messaggio non è stato recapitato. Riprova quando è attivo."}
                        dispatch_prompt_async(target_window, prompt, offline_notice=notice)

                except Exception as update_err:
                    logger.error(f"⚠️ Errore durante update {update_id} [{bot_key}]: {update_err} — update salvato in /tmp/telegram_lost_updates.jsonl per recupero")
                    try:
                        with open("/tmp/telegram_lost_updates.jsonl", "a", encoding="utf-8") as _lf:
                            _lf.write(json.dumps({"bot": bot_key, "ts": time.time(), "update": update}, ensure_ascii=False) + "\n")
                    except Exception:
                        pass
                finally:
                    if update_id is not None:
                        offset = update_id + 1
                        _save_telegram_offset(bot_key, offset)
        except requests.exceptions.Timeout as e:
            # DEV-016: timeout del long-polling (getUpdates) — fisiologico, la
            # connessione scade mentre si aspetta un messaggio. NON e' un guasto:
            # DEBUG per non seppellire di rumore gli errori veri nel log.
            logger.debug(f"⏳ Long-polling timeout (fisiologico) [{bot_key}]: {_mask_tokens(e)}")
        except Exception as e:
            logger.error(f"⚠️ Exception in bot loop [{bot_key}]: {_mask_tokens(e)}")
            time.sleep(2)

# ---------------------------------------------------------
# 2. WATCHER RECAP UNIFICATO DISPATCHER
# ---------------------------------------------------------
def run_unified_recap_watcher():
    logger.info("📡 Unified Recap Watcher Loop Avviato...")
    while True:
        try:
            if os.path.exists(RECAPS_DIR):
                for recap_file in glob.glob(os.path.join(RECAPS_DIR, "*.txt")):
                    slug = os.path.basename(recap_file).replace(".txt", "").lower()
                    # FIX recap timestamp: rimuovi il suffisso _<10 cifre unix> (es. regista_1786569450 -> regista)
                    m = re.match(r'^(.*)_\d{10}$', slug)
                    if m:
                        slug = m.group(1)
                    time.sleep(0.2)
                    proc_path = recap_file + ".processing"
                    # PUNTO 6 dedup_terza_porta — prelazione ATOMICA del recap:
                    # os.rename sovrascrive un '.processing' gia' esistente e un
                    # secondo thread/processo potrebbe prendere lo stesso recap.
                    # os.link fallisce (EEXIST) se qualcun altro ha gia' reclamato
                    # il file -> uno e uno solo consuma il recap. Poi si rimuove
                    # il nome originale per non ri-prelarlo al ciclo dopo.
                    try:
                        os.link(recap_file, proc_path)
                        os.remove(recap_file)
                    except (OSError, FileExistsError):
                        continue

                    try:
                        with open(proc_path, "r", encoding="utf-8") as f: content = f.read().strip()
                        if not content:
                            try: os.remove(proc_path)
                            except OSError: pass
                            continue
                        content_hash = hashlib.md5(content.encode("utf-8")).hexdigest()

                        logger.info(f"🏛️ Intercettato recap per '{slug}'")

                        # Cerca il Bot dell'agente
                        bot_cfg = None
                        agent_info = None
                        bot_key_found = None
                        reply_id = None
                        token = None
                        chat_id = None

                        for b_key, b_cfg in BOT_CONFIGS.items():
                            if slug in b_cfg["agent_map"]:
                                bot_cfg = b_cfg
                                agent_info = b_cfg["agent_map"][slug]
                                bot_key_found = b_key
                                break

                        if not bot_cfg:
                            # Sincronizzazione dinamica da Postgres per agenti creati a caldo dopo l'avvio dell'engine
                            sync_agent_threads_from_db()
                            for b_key, b_cfg in BOT_CONFIGS.items():
                                if slug in b_cfg["agent_map"]:
                                    bot_cfg = b_cfg
                                    agent_info = b_cfg["agent_map"][slug]
                                    bot_key_found = b_key
                                    break


                        if bot_cfg:
                            token = bot_cfg["token"]
                            chat_id = bot_cfg.get("chat_id")
                            # Usa tassativamente il thread_id associato all'agente dal suo agent_map
                            thread_id = agent_info.get("thread_id") if isinstance(agent_info, dict) else None

                            # Cerca sorgente attiva per QUESTO topic specifico (file per-thread,
                            # non più condiviso tra tutti i topic dello stesso bot)
                            thread_key = thread_id if thread_id is not None else "general"
                            src_file = f"/tmp/{bot_key_found}_{thread_key}_active_source.json"
                            if os.path.exists(src_file):
                                try:
                                    with open(src_file, "r") as sf:
                                        sdata = json.load(sf)
                                        if sdata.get("thread_id") == thread_id:
                                            reply_id = sdata.get("message_id")
                                except Exception: pass

                            # Strip eventuale eco della direttiva imperativa di sistema
                            content = re.sub(r'\[DIRETTIVA DI SISTEMA IMPERATIVA\]:.*', '', content, flags=re.IGNORECASE).strip()

                            # Parsing eventuale [REPLY_TO_MSG_ID: 123]
                            r_match = re.search(r'\[REPLY_TO_MSG_ID:\s*(\d+)\s*\]', content)
                            if r_match:
                                reply_id = int(r_match.group(1))
                                content = re.sub(r'\[REPLY_TO_MSG_ID:\s*\d+\s*\]', '', content).strip()

                            file_matches = re.findall(r'\[FILE:\s*(.*?)\s*\]', content)
                            clean_content = re.sub(r'\[FILE:\s*.*?\s*\]', '', content).strip()

                            # Tag <VOICE>: sintesi vocale forzata su richiesta esplicita dell'agente
                            audio_ogg_path = None
                            voice_match = re.search(r'<VOICE>(.*?)</VOICE>', clean_content, re.DOTALL | re.IGNORECASE)
                            if voice_match:
                                voice_text = voice_match.group(1).strip()
                                clean_content = re.sub(r'<VOICE>.*?</VOICE>', '', clean_content, flags=re.DOTALL | re.IGNORECASE).strip()
                                audio_ogg_path = synthesize_voice_ogg(voice_text, slug)

                            # Tag [QUESTION_OPTIONS: Opzione A | Opzione B | ...]: l'agente
                            # chiede una scelta a Manfredo. Invece del solo testo, il recap
                            # arriva con pulsanti inline Telegram — un tap = la risposta,
                            # niente da digitare. Vedi callback_query in run_bot_polling.
                            question_options = None
                            question_id = None
                            q_match = re.search(r'\[QUESTION_OPTIONS:\s*(.*?)\s*\]', clean_content, re.IGNORECASE | re.DOTALL)
                            if q_match:
                                question_options = [o.strip() for o in q_match.group(1).split('|') if o.strip()]
                                clean_content = re.sub(r'\[QUESTION_OPTIONS:\s*.*?\s*\]', '', clean_content, flags=re.IGNORECASE | re.DOTALL).strip()
                                if question_options:
                                    # ID univoco per QUESTA domanda specifica (non solo per slug):
                                    # un agente puo' avere piu' domande pendenti in parallelo (es.
                                    # piu' recap ravvicinati con [QUESTION_OPTIONS] ciascuno) -- con
                                    # una chiave per-slug la domanda N+1 sovrascriveva sempre quella
                                    # N, rendendo "orfani" i pulsanti gia' inviati (bug reale
                                    # segnalato da Simone/Manfredo, 04/08/2026: su 8 domande mandate
                                    # quasi in contemporanea solo l'ultima restava rispondibile).
                                    question_id = uuid.uuid4().hex[:8]
                                    _PENDING_QUESTIONS[f"{slug}:{question_id}"] = question_options
                                    _save_pending_questions()
                                    if not clean_content:
                                        clean_content = "❓"

                            # Tag [SWITCH_AGENT:<slug>]: passaggio automatico ad un altro agente via Tmux (nessuna dipendenza da Flask)
                            next_agent_slug = None
                            switch_match = re.search(r'\[SWITCH_AGENT:\s*([^\]]+)\s*\]', clean_content, re.IGNORECASE)
                            if switch_match:
                                next_agent_slug = switch_match.group(1).strip().lower()
                                clean_content = re.sub(r'\[SWITCH_AGENT:\s*[^\]]+\s*\]', '', clean_content, flags=re.IGNORECASE).strip()

                            # Tag [PIN_MESSAGE]: fissa in alto il messaggio nel topic
                            pin_message = False
                            if re.search(r'\[PIN_MESSAGE\]', clean_content, re.IGNORECASE):
                                pin_message = True
                                clean_content = re.sub(r'\[PIN_MESSAGE\]', '', clean_content, flags=re.IGNORECASE).strip()

                            send_success = True
                            if chat_id:
                                if clean_content:
                                    text_chunks = split_telegram_text(clean_content)
                                    if len(text_chunks) > 1:
                                        logger.info(f"✂️ Recap {slug} lungo {len(clean_content)} caratteri, spezzato in {len(text_chunks)} messaggi")
                                    for i, chunk in enumerate(text_chunks):
                                        payload = {"chat_id": chat_id, "text": to_telegram_markdown(chunk), "parse_mode": "MarkdownV2"}
                                        if thread_id: payload["message_thread_id"] = thread_id
                                        if reply_id and i == 0: payload["reply_to_message_id"] = reply_id
                                        if question_options and i == len(text_chunks) - 1:
                                            payload["reply_markup"] = {
                                                "inline_keyboard": [
                                                    [{"text": opt[:80], "callback_data": f"Q|{slug}|{question_id}|{idx}"}]
                                                    for idx, opt in enumerate(question_options)
                                                ]
                                            }

                                        edited = False
                                        if pin_message and i == len(text_chunks) - 1:
                                            pinned_msgs = _load_pinned_messages()
                                            pinned_key = f"{chat_id}:{thread_id}" if thread_id else str(chat_id)
                                            if pinned_key in pinned_msgs:
                                                edit_payload = dict(payload)
                                                edit_payload["message_id"] = pinned_msgs[pinned_key]
                                                edit_payload.pop("message_thread_id", None)
                                                edit_payload.pop("reply_to_message_id", None)
                                                
                                                r = requests.post(f"https://api.telegram.org/bot{token}/editMessageText", json=edit_payload, timeout=10)
                                                if r.status_code == 200:
                                                    edited = True
                                                    logger.info(f"✅ Dashboard aggiornata in-place (Messaggio {pinned_msgs[pinned_key]})")
                                                else:
                                                    logger.warning(f"⚠️ Impossibile editare il messaggio pinnato (potrebbe essere stato cancellato). Rigenero...")
                                        
                                        if not edited:
                                            r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json=payload, timeout=10)
                                            if r.status_code == 400:
                                                logger.warning(f"⚠️ Recap {slug} rifiutato ({r.text[:200]}), ritento senza parse_mode / reply_to...")
                                                payload.pop("parse_mode", None)
                                                payload["text"] = chunk
                                                if "message to be replied not found" in r.text or "reply_to_message_id" in payload:
                                                    payload.pop("reply_to_message_id", None)
                                                r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json=payload, timeout=10)
                                                if r.status_code == 400 and "reply_to_message_id" in payload:
                                                    payload.pop("reply_to_message_id", None)
                                                    r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json=payload, timeout=10)
                                            
                                            logger.info(f"✅ Recap {slug} (blocco {i+1}/{len(text_chunks)}) inviato a Chat {chat_id} (Status: {r.status_code})")
                                            if r.status_code != 200:
                                                logger.error(f"❌ Corpo risposta Telegram: {r.text[:300]}")
                                                send_success = False
                                                break
                                            else:
                                                if pin_message and i == len(text_chunks) - 1:
                                                    try:
                                                        resp_json = r.json()
                                                        if resp_json.get("ok"):
                                                            sent_msg_id = resp_json["result"]["message_id"]
                                                            
                                                            # Rimuove solo il nostro pin precedente per mantenere intatti gli altri pin (es. dashboard)
                                                            pinned_msgs = _load_pinned_messages()
                                                            pinned_key = f"{chat_id}:{thread_id}" if thread_id else str(chat_id)
                                                            if pinned_key in pinned_msgs:
                                                                requests.post(f"https://api.telegram.org/bot{token}/unpinChatMessage", json={"chat_id": chat_id, "message_id": pinned_msgs[pinned_key]}, timeout=5)
                                                            
                                                            pin_payload = {
                                                                "chat_id": chat_id,
                                                                "message_id": sent_msg_id
                                                            }
                                                            if thread_id:
                                                                pin_payload["message_thread_id"] = thread_id
    
                                                            pin_r = requests.post(f"https://api.telegram.org/bot{token}/pinChatMessage", json=pin_payload, timeout=10)
                                                            logger.info(f"📌 Messaggio {sent_msg_id} pinnato (Status: {pin_r.status_code})")
                                                            
                                                            # Salva il nuovo ID
                                                            pinned_msgs[pinned_key] = sent_msg_id
                                                            _save_pinned_messages(pinned_msgs)
                                                    except Exception as e:
                                                        logger.error(f"⚠️ Errore durante il pin del messaggio: {e}")

                                for filepath in file_matches:
                                    if os.path.exists(filepath):
                                        ext = filepath.lower().split('.')[-1]
                                        method, fld = ("sendVideo", "video") if ext in ['mp4', 'mov', 'webm'] else (("sendPhoto", "photo") if ext in ['png', 'jpg', 'jpeg'] else ("sendDocument", "document"))
                                        fdata = {"chat_id": chat_id}
                                        if thread_id: fdata["message_thread_id"] = thread_id
                                        if reply_id: fdata["reply_to_message_id"] = reply_id
                                        with open(filepath, 'rb') as f_d:
                                            r_m = requests.post(f"https://api.telegram.org/bot{token}/{method}", data=fdata, files={fld: f_d}, timeout=30)
                                            if r_m.status_code == 400 and "reply_to_message_id" in fdata:
                                                fdata.pop("reply_to_message_id", None)
                                                f_d.seek(0)
                                                r_m = requests.post(f"https://api.telegram.org/bot{token}/{method}", data=fdata, files={fld: f_d}, timeout=30)
                                            logger.info(f"📸 Media [{method}] per {slug} inviato a Chat {chat_id} (Status: {r_m.status_code})")
                                            if r_m.status_code != 200:
                                                send_success = False

                                if audio_ogg_path and os.path.exists(audio_ogg_path):
                                    try:
                                        with open(audio_ogg_path, 'rb') as af:
                                            v_payload = {"chat_id": chat_id}
                                            if thread_id: v_payload["message_thread_id"] = thread_id
                                            if reply_id: v_payload["reply_to_message_id"] = reply_id
                                            r_v = requests.post(f"https://api.telegram.org/bot{token}/sendVoice", data=v_payload, files={"voice": af}, timeout=20)
                                            if r_v.status_code == 400 and "reply_to_message_id" in v_payload:
                                                v_payload.pop("reply_to_message_id", None)
                                                af.seek(0)
                                                r_v = requests.post(f"https://api.telegram.org/bot{token}/sendVoice", data=v_payload, files={"voice": af}, timeout=20)
                                            logger.info(f"🔊 Nota vocale per {slug} inviata (Status: {r_v.status_code})")
                                            if r_v.status_code != 200:
                                                send_success = False
                                    except Exception as e_voice_send:
                                        logger.error(f"⚠️ Invio nota vocale fallito per {slug}: {e_voice_send}")
                            else:
                                logger.warning(f"⚠️ Chat ID non ancora registrata per {slug}")
                                send_success = False

                            if next_agent_slug:
                                target_window = resolve_agent_window(next_agent_slug)
                                if target_window:
                                    logger.info(f"🔀 [SWITCH_AGENT] {slug} -> {next_agent_slug} ({target_window})")
                                    chain_prompt = f"[ROUTING A2A] L'agente '{slug}' ha completato la sua parte e ti ha passato il compito tramite SWITCH_AGENT. Ecco il suo recap conclusivo su cui devi basarti per continuare il lavoro:\n\n{clean_content}"
                                    notice = {"token": token, "chat_id": chat_id, "message_thread_id": thread_id,
                                              "text": f"⚠️ '{slug}' ha provato a passare il lavoro a '{next_agent_slug}' via SWITCH_AGENT, ma quello studio/agente risulta offline in questo momento — l'handoff non è andato a buon fine."} if (token and chat_id) else None
                                    dispatch_prompt_async(target_window, chain_prompt, offline_notice=notice)
                                else:
                                    logger.warning(f"⚠️ [SWITCH_AGENT] Impossibile risolvere la finestra per '{next_agent_slug}'")
                        else:
                            logger.warning(f"⚠️ Nessun Bot registrato per l'agente '{slug}'")
                            send_success = False

                        # 🏁 Feedback "conclusione": ferma il loop reaction sul messaggio
                        # originale dell'utente, sia in caso di successo che di abbandono
                        # definitivo (per non lasciare l'icona a ciclare all'infinito).
                        if reply_id and token and chat_id:
                            reaction_api_url = f"https://api.telegram.org/bot{token}"

                        if send_success:
                            try: os.remove(proc_path)
                            except OSError: pass
                            _FAILED_ATTEMPTS.pop((slug, content_hash), None)
                            if reply_id and token and chat_id:
                                stop_reaction_cycle(reaction_api_url, chat_id, reply_id, success=True)
                        else:
                            attempts = _FAILED_ATTEMPTS.get((slug, content_hash), 0) + 1
                            _FAILED_ATTEMPTS[(slug, content_hash)] = attempts
                            if attempts >= MAX_RECAP_RETRIES:
                                logger.error(f"❌ Recap {slug} fallito {attempts} volte con lo stesso contenuto (causa non transitoria, es. messaggio troppo lungo o nessun bot registrato per questo slug): abbandono senza ritentare oltre, per non rigenerare l'audio TTS all'infinito.")
                                # MAI distruggere il contenuto: segnalato via A2A da Publio (04/08/2026) —
                                # un file .txt dentro RECAPS_DIR usato come handoff INTERNO tra agenti
                                # (slug senza bot Telegram registrato, es. "edoardo_audit") veniva
                                # cancellato in modo permanente e silenzioso dopo i tentativi falliti,
                                # con perdita reale di un report di lavoro. Spostato in _failed/ invece
                                # di os.remove(), recuperabile a mano.
                                try:
                                    failed_dir = os.path.join(RECAPS_DIR, "_failed")
                                    os.makedirs(failed_dir, exist_ok=True)
                                    failed_dest = os.path.join(failed_dir, f"{slug}_{int(time.time())}.txt")
                                    os.rename(proc_path, failed_dest)
                                    logger.warning(f"📦 Contenuto di '{slug}' preservato in {failed_dest} (non cancellato)")
                                except OSError:
                                    pass
                                _FAILED_ATTEMPTS.pop((slug, content_hash), None)
                                if reply_id and token and chat_id:
                                    stop_reaction_cycle(reaction_api_url, chat_id, reply_id, success=False)
                            else:
                                logger.warning(f"⚠️ Invio fallito per {slug} (tentativo {attempts}/{MAX_RECAP_RETRIES}), ripristino file per riprovare")
                                try: os.rename(proc_path, recap_file)
                                except OSError: pass
                    except Exception as e:
                        logger.error(f"❌ Errore processamento recap {slug}: {e}")
                        try: os.rename(proc_path, recap_file)
                        except OSError: pass

        except Exception as global_e:
            logger.error(f"⚠️ Exception in Watcher Loop: {global_e}")

        time.sleep(2)

def _load_tasklist_state():
    try:
        with open(_TASKLIST_STATE_PATH) as f:
            return json.load(f)
    except Exception:
        return {}

def _save_tasklist_state(state):
    try:
        with open(_TASKLIST_STATE_PATH, "w") as f:
            json.dump(state, f)
    except Exception as e:
        logger.warning(f"⚠️ Impossibile salvare _TASKLIST_STATE su disco: {e}")

def run_tasklist_watcher():
    os.makedirs(TASKLISTS_DIR, exist_ok=True)
    logger.info(f"📋 Tasklist Watcher Avviato su {TASKLISTS_DIR}...")
    tasklist_state = _load_tasklist_state()
    # Mappa slug -> timestamp_modifica per evitare di riprocessare se non cambia
    _last_mtimes = {}

    while True:
        try:
            for fname in os.listdir(TASKLISTS_DIR):
                if not fname.endswith(".json"): continue
                slug = fname[:-5]
                # FIX recap timestamp: rimuovi il suffisso _<10 cifre unix> (es. regista_1786569450 -> regista)
                m = re.match(r'^(.*)_\d{10}$', slug)
                if m:
                    slug = m.group(1)
                # RESTRIZIONE: Solo i Coordinatori possono aggiornare la Tasklist Dinamica (Manfredo 07/08/2026)
                if slug not in ["leo", "simone", "betty", "enrico", "flavio", "redattore", "diana", "ppp_tech"]:
                    try:
                        os.remove(filepath)
                    except:
                        pass
                    continue
                filepath = os.path.join(TASKLISTS_DIR, fname)
                try:
                    mtime = os.path.getmtime(filepath)
                    if _last_mtimes.get(slug) == mtime:
                        continue

                    with open(filepath, "r", encoding="utf-8") as f:
                        data = json.load(f)

                    # Trova il bot_cfg
                    bot_cfg = None
                    agent_info = None
                    for b_key, b_cfg in BOT_CONFIGS.items():
                        if slug in b_cfg["agent_map"]:
                            bot_cfg = b_cfg
                            agent_info = b_cfg["agent_map"][slug]
                            break

                    if bot_cfg:
                        token = bot_cfg["token"]
                        chat_id = bot_cfg.get("chat_id")
                        thread_id = agent_info.get("thread_id") if isinstance(agent_info, dict) else None

                        if chat_id:
                            title = data.get("title", "Tasklist")
                            items = data.get("items", [])
                            is_done = data.get("concluso", False)

                            lines = [f"#{title}"]
                            for item in items:
                                icon = "✅" if item.get("done") else "⬜"
                                lines.append(f"{icon} {item.get('text', '')}")
                            
                            md_text = "\n".join(lines)
                            safe_text = to_telegram_markdown(md_text)
                            
                            msg_id = tasklist_state.get(slug)
                            success = False
                            
                            if msg_id:
                                # Update esistente
                                payload = {"chat_id": chat_id, "message_id": msg_id, "text": safe_text, "parse_mode": "MarkdownV2"}
                                r = requests.post(f"https://api.telegram.org/bot{token}/editMessageText", json=payload, timeout=10)
                                if r.status_code == 200 or "not modified" in r.text.lower():
                                    success = True
                                else:
                                    msg_id = None
                            
                            if not msg_id:
                                # Nuovo messaggio
                                payload = {"chat_id": chat_id, "text": safe_text, "parse_mode": "MarkdownV2"}
                                if thread_id: payload["message_thread_id"] = thread_id
                                r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json=payload, timeout=10)
                                if r.status_code == 200:
                                    msg_id = r.json().get("result", {}).get("message_id")
                                    tasklist_state[slug] = msg_id
                                    _save_tasklist_state(tasklist_state)
                                    success = True
                                    
                                    # Fissa (pin) il messaggio nel topic
                                    try:
                                        pin_payload = {"chat_id": chat_id, "message_id": msg_id}
                                        requests.post(f"https://api.telegram.org/bot{token}/pinChatMessage", json=pin_payload, timeout=10)
                                    except Exception as e_pin:
                                        logger.warning(f"⚠️ Impossibile pinnare la tasklist per {slug}: {e_pin}")

                            
                            if success:
                                _last_mtimes[slug] = mtime
                                if is_done:
                                    try: os.remove(filepath)
                                    except OSError: pass
                                    tasklist_state.pop(slug, None)
                                    _save_tasklist_state(tasklist_state)

                except Exception as e:
                    logger.error(f"❌ Errore processamento tasklist per {slug}: {e}")

        except Exception as global_e:
            logger.error(f"⚠️ Exception in Tasklist Watcher: {global_e}")

        time.sleep(2)

# ---------------------------------------------------------
# 3. MAIN STARTER ENGINE
# ---------------------------------------------------------
if __name__ == "__main__":
    acquisisci_istanza()
    logger.info("🚀 UNIFIED TELEGRAM ENGINE - AVVIO DI TUTTI I BOT ED IL WATCHER...")

    # ⚡ Sincronizza in tempo reale i thread_id ed i dati direttamente dal DB PostgreSQL
    sync_agent_threads_from_db()

    # Avvia 1 thread per il Watcher Recap Unificato
    t_watcher = threading.Thread(target=run_unified_recap_watcher, daemon=True)
    t_watcher.start()

    # Avvia 1 thread per il Tasklist Watcher Dinamico
    t_tasklist = threading.Thread(target=run_tasklist_watcher, daemon=True)
    t_tasklist.start()

    # Avvia 1 thread per il Sync dei nomi Topic con l'attività live tmux/Claude


    # Avvia 1 thread polling per ogni Bot Telegram registrato
    for b_key, b_cfg in BOT_CONFIGS.items():
        t_bot = threading.Thread(target=run_bot_polling, args=(b_key, b_cfg), daemon=True)
        t_bot.start()

    logger.info("✅ Tutti i Bot ed il Watcher sono attivi in multithreading nello stesso processo Engine!")

    # Mantieni il main thread in vita
    while True:
        time.sleep(10)
