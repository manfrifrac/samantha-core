"""
engine_adapter.py
==================
Punto unico di verità per tutto ciò che differisce tra i CLI-engine
supportati per lanciare un agente in una finestra Tmux: `agy` (Antigravity,
comportamento storico, invariato), `claude` (Claude Code CLI), `qwen`
(Qwen Code CLI, aggiunto il 05/08/2026) e `deepcode` (Deep Code / DeepSeek,
aggiunto il 09/08/2026 — ⚠️ nessun resume nativo da CLI, ogni lancio è
sempre una sessione fresca: vedi il commento nel ramo `deepcode` di
build_launch_cmd per il perché).

Ogni chiamante (agent_launcher.py, agent_monitor.py, mcp_arturo.py,
extract_memories.py, cleanup_tmux.py, restart_ecosystem_dynamic.py,
create_new_agent_or_studio.py, refresh_agent_context.py) legge
`agent_data.get("engine", "agy")` dal record Postgres dell'agente e delega
a questo modulo — nessuno ricostruisce più stringhe/pattern agy o claude a mano.

Se il campo "engine" è assente, il comportamento è sempre e comunque
identico a quello storico basato su `agy` (opt-in per agente).
"""

import os
import uuid
import glob
import re
import sys
import json
import shlex
import shutil
import tempfile
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tmux_esatto

HOME = os.path.expanduser("~")
AGY_BIN = shutil.which("agy") or os.path.join(HOME, ".local/bin/agy")


def _find_qwen_bin():
    """qwen vive dentro nvm (path non garantito in ogni processo chiamante):
    prima `which`, poi fallback esplicito sulle installazioni nvm."""
    found = shutil.which("qwen")
    if found:
        return found
    import glob
    candidates = sorted(glob.glob(os.path.join(HOME, ".nvm/versions/node/*/bin/qwen")))
    return candidates[-1] if candidates else "qwen"


QWEN_BIN = _find_qwen_bin()


def _find_deepcode_bin():
    """Stessa logica di _find_qwen_bin: deepcode vive nello stesso nvm."""
    found = shutil.which("deepcode")
    if found:
        return found
    import glob
    candidates = sorted(glob.glob(os.path.join(HOME, ".nvm/versions/node/*/bin/deepcode")))
    return candidates[-1] if candidates else "deepcode"


DEEPCODE_BIN = _find_deepcode_bin()


def _find_goose_bin():
    """goose e' un binario Rust singolo installato in /usr/local/bin (dal
    benchmark test_goose, v1.46.0, installata da GitHub releases block/goose):
    `which` prima, poi il path canonico esplicito. Nessun nvm coinvolto.
    """
    found = shutil.which("goose")
    if found:
        return found
    return "/usr/local/bin/goose"


GOOSE_BIN = _find_goose_bin()
GOOSE_PROJECTS_DIR = os.path.join(HOME, ".local", "share", "goose", "sessions")
BRAIN_DIR = os.path.join(HOME, ".gemini", "antigravity-cli", "brain")
CLAUDE_PROJECTS_DIR = os.path.join(HOME, ".claude", "projects")
QWEN_PROJECTS_DIR = os.path.join(HOME, ".qwen", "projects")
DEEPCODE_PROJECTS_DIR = os.path.join(HOME, ".deepcode", "projects")
USER_DEEPCODE_SETTINGS = os.path.join(HOME, ".deepcode", "settings.json")


def _deepcode_global_override():
    """Legge thinkingEnabled/reasoningEffort dal .deepcode/settings.json GLOBALE
    (chmod 600 root-only: i motori dc-isolati che girano come utenti NON lo
    leggono e ripiegherebbero sul default del CLI, thinkingEnabled:true +
    reasoningEffort:max — piu' lento e piu' caro sul proxy DeepSeek). Ritorna
    dict parziale, o None se il file non e' leggibile. Task Leo eb9e: l'override
    viene propagato nel settings di PROGETTO a ogni lancio (vedi
    build_launch_cmd, ramo deepcode)."""
    try:
        with open(USER_DEEPCODE_SETTINGS, encoding="utf-8") as f:
            d = json.load(f) or {}
        out = {}
        if "thinkingEnabled" in d:
            out["thinkingEnabled"] = d["thinkingEnabled"]
        if "reasoningEffort" in d:
            out["reasoningEffort"] = d["reasoningEffort"]
        return out
    except Exception:
        return None


# Endpoint del reverse-proxy locale (deepseek_spend_guard.py): deepcode-cli
# NON deve MAI puntare direttamente su api.deepseek.com — il tetto di spesa
# nostro (DEV seguito, 09/08/2026, priorita' di Manfredo: 1 solo euro sulla
# chiave) vive li'. Se il proxy non gira, il lancio fallisce con connessione
# rifiutata: fallimento rumoroso, mai un bypass silenzioso verso l'API vera.
DEEPSEEK_GUARD_URL = "http://127.0.0.1:8790"

# DEV seguito (08/08/2026, Betty Gestione Agenti + Leo): segnalato che il
# failover automatico verso claude "va sempre a sessione fresca, mai
# --resume". Verificato PRIMA di toccare la logica di selezione conv_id
# (failover_manager.py, conv_id_by_engine): riprodotta empiricamente due
# escalation successive sullo stesso agente di test, il conv_id claude
# GIA' USATO viene correttamente riproposto la seconda volta — quel pezzo
# non e' rotto. Se il sintomo reale e' comunque "sempre fresca", il guasto
# vero sta qui sotto, nel comando shell (resume || fresh): senza una
# traccia persistente di QUANDO/PERCHE' 'resume' fallisce (es. work_dir
# incoerente -> "No conversation found" anche per un id vero, gia'
# documentato altrove), non si puo' distinguere "non e' mai stato rotto"
# da "si rompe silenziosamente ogni volta". Log minimo, non invasivo: si
# scrive SOLO quando il ramo fresh scatta davvero (mai sul percorso felice).
RESUME_FALLBACK_LOG = os.path.join(HOME, "ecosistema_agenti", "core", "engine_resume_fallback.log")

VALID_ENGINES = ("agy", "claude", "qwen", "deepcode", "goose", "deepseek")

# DEV seguito (09/08/2026, trovato da Leo SUBITO dopo il fix --model qui
# sotto — non prima, perche' prima il campo era ignorato e quindi innocuo).
# 11 agenti hanno data->>'model'='flash' in Postgres: residuo dell'epoca in
# cui giravano su agy (Gemini), mai stato un modello Claude valido. Applicare
# --model alla lettera lo avrebbe fatto scoprire nel modo peggiore possibile:
# un lancio claude che fallisce, letto dal resume_guard come "sessione
# ripartita vuota" — la stessa famiglia di allarme fuorviante che la notte ha
# gia' passato ore a inseguire. Whitelist minima, non un parser dei modelli
# disponibili (che cambierebbe ad ogni nuovo modello): alias noti o pattern
# 'claude-<qualcosa>', scartato altrimenti — MAI passato alla lettera.
MODEL_ALIASES_CLAUDE = {"sonnet", "opus", "fable", "haiku"}
MODEL_PATTERN_CLAUDE = re.compile(r"^claude-[a-z0-9][a-z0-9.\-]*$", re.IGNORECASE)
MODEL_SCARTATO_LOG = os.path.join(HOME, "ecosistema_agenti", "core", "engine_model_scartato.log")


def modello_valido_per_claude(model):
    """True se `model` e' un alias o nome che il CLI claude accetta davvero
    per --model. Non valida contro un elenco di modelli ESISTENTI (che
    invecchierebbe), solo contro la FORMA che un modello claude ha sempre:
    o e' uno degli alias corti, o comincia per 'claude-' (incluso eventuale
    suffisso '[1m]' per sbloccare la finestra 1M). Basta a scartare
    residui come 'flash' senza dover inseguire ogni nuovo nome di modello."""
    if not model:
        return False
    m = model.strip().lower()
    base = re.sub(r"\[1m\]$", "", m, flags=re.IGNORECASE)
    return base in MODEL_ALIASES_CLAUDE or bool(MODEL_PATTERN_CLAUDE.match(base))


def _log_model_scartato(conv_id, agent_slug, model):
    """Scarto silenzioso in codice, ma MAI silenzioso su disco (SS13.10 del
    disegno DEV-110: uno zero scritto vale quanto un allarme — qui vale anche
    al contrario, un residuo scartato deve vedersi). Append-only, mai una
    riga sovrascritta."""
    riga = (f"{datetime.now().isoformat(timespec='seconds')} MODEL_SCARTATO "
            f"agente={agent_slug or '?'} conv_id={conv_id} valore='{model}' "
            f"motivo='non e alias ne nome claude riconosciuto, --model omesso'")
    try:
        with open(MODEL_SCARTATO_LOG, "a", encoding="utf-8") as f:
            f.write(riga + "\n")
    except OSError:
        pass


def get_engine(agent_data):
    """Ritorna l'engine di un agente ('agy' di default se il campo è assente)."""
    engine = (agent_data or {}).get("engine", "agy")
    return engine if engine in VALID_ENGINES else "agy"


# ---------------------------------------------------------------------------
# Costruzione comando di lancio
# ---------------------------------------------------------------------------

def _claude_cwd_to_project_slug(work_dir):
    """Replica l'encoding che Claude Code usa per la cartella di sessione:
    sostituisce '/', '_' e '.' con '-'.

    ⚠️ BUG CORRETTO IL 09/08/2026: il punto NON veniva convertito, quindi per
    i work_dir con una cartella nascosta si costruiva un percorso che non
    esiste:
        cercato: .../-root-ecosistema-agenti-core-.agents-betty-studio/
        reale:   .../-root-ecosistema-agenti-core--agents-betty-studio/
    Claude codifica `core/.agents` come `core--agents`: un trattino per lo
    slash e UNO PER IL PUNTO. L'adapter convertiva solo lo slash.

    Conseguenza misurata: chiunque legga il transcript per conv_id — il
    sensore memoria leggera, ask_agent di mcp_arturo — SALTAVA IN SILENZIO
    ogni agente con una cartella nascosta nel work_dir. La notte del 09/08
    l'unico vivo non coperto era BGA (`core/.agents/betty_studio`): il sensore
    era cieco proprio su chi lo stava installando, e non lo diceva.

    Il caso generale (piu' cartelle nascoste nel path) e' coperto: la
    sostituzione e' globale, non sul primo punto."""
    return work_dir.replace("_", "-").replace("/", "-").replace(".", "-")


# DEV-097 (09/08/2026). Il log qui sotto esiste dall'08/08 e ha permesso a
# Elisa di diagnosticare 8 fallback in una mattina — ma e' un file che nessuno
# guarda mentre succede: al momento del fallback la memoria e' gia' persa e
# nessuno se ne accorge. Con questo flag attivo, il ramo fresh chiama anche
# resume_guard.py, che stampa un banner nel pannello dell'agente, registra
# l'evento e avvisa Betty via A2A (con rate-limit) — e soprattutto dice se la
# memoria e' DAVVERO persa o se esiste in un'altra cartella e si sta per
# abbandonarla, che e' la sola domanda utile in quel momento.
# Il flag e' un file: assente = comportamento identico a prima, in tutto e per
# tutto. Il rollout e' una decisione di Betty (codice di lancio agenti), non di
# chi ha scritto il fix.
RESUME_GUARD_FLAG = os.path.join(HOME, "ecosistema_agenti", "core", "dev097_resume_guard_attivo")
RESUME_GUARD_BIN = os.path.join(HOME, "ecosistema_agenti", "core", "resume_guard.py")
PY_BIN = os.path.join(HOME, "ecosistema_agenti", "core", "venv", "bin", "python3")


def conv_id_resumibile(conv_id):
    """True se `conv_id` puo' essere passato a `claude --resume` SENZA rischiare
    il selettore interattivo.

    FIX 24/08/2026 (exec_fix_lancio_fresco, ticket 435a — incidente reale su
    alfredo/auruncia_tech_1/auruncia_tech_2). Misurato sul CLI vero
    (claude 2.1.238), non dedotto dalla doc:

      --resume '<UUID valido ma inesistente>'  -> exit 1, "No conversation
          found with session ID: ..." — il `||` scatta, tutto regolare.
      --resume ''                              -> SELETTORE interattivo.
      --resume 'conv_8557d673b1f54d08'         -> SELETTORE interattivo, con
          l'id finito nella casella di ricerca ("No sessions match ...").

    Se l'argomento non e' un UUID, Claude Code non lo tratta come un id ma come
    STRINGA DI RICERCA e apre il picker: il processo NON esce mai, quindi il
    `||` non scatta MAI e l'agente resta senza bootstrap, senza identita' e
    bloccato a tempo indefinito. E' esattamente il guasto che il 24/08/2026 ha
    fermato 3 agenti su 3 lanci freschi.

    Vale per il ramo claude (e per le sue cascate). Gli altri motori hanno
    parser diversi e non sono toccati da questa funzione."""
    try:
        uuid.UUID(str(conv_id))
        return True
    except Exception:
        return False


def resume_guard_attivo():
    return os.path.exists(RESUME_GUARD_FLAG)


def _pulisci_per_echo(testo):
    """Il log e' scritto da una `echo "..."` di shell: via i caratteri che la
    shell interpreterebbe dentro doppi apici (", $, `, \\) e gli a-capo."""
    return "".join(c for c in str(testo) if c not in '"$`\\\n\r')


def _agente_da_registro(conv_id, agent_slug=""):
    """(slug, data) dell'agente in Postgres: per slug se dato, altrimenti per
    conv_id. Mai un errore: in caso di dubbio ('', {}) — il log serve ad
    avvisare, non deve MAI diventare il motivo per cui un agente non parte."""
    try:
        import agent_db
        db = agent_db.get_db() or {}
        if agent_slug and agent_slug in db:
            return agent_slug, (db.get(agent_slug) or {})
        trovati = [(a, d or {}) for a, d in db.items() if (d or {}).get("conv_id") == conv_id]
        if len(trovati) == 1:
            return trovati[0]
    except Exception:
        pass
    return agent_slug, {}


def classifica_fallback(engine, conv_id, work_dir, agent_slug="", modalita="ignoto"):
    """FIX 22/08/2026 (exec_fix_rinfresco; suggerimento di publio_2, condiviso
    da publio_1): un FALLBACK-FRESH NON e' sempre un danno. Distingue:

      PRIMA-VITA     conv_id senza nessun transcript e agente senza vita
                     precedente (exec appena nato): avvio normale, innocuo.
      FRESCO-VOLUTO  sessione azzerata di proposito (modalita='fresco' dal
                     chiamante, oppure `rinfresco_modalita`='fresco' scritto
                     in Postgres da rinfrescami.py --fresco): innocuo.
      STORIA-PERSA   danno reale: o il transcript di QUESTO conv_id esiste
                     (in work_dir o in un'altra cartella) e il resume e'
                     fallito lo stesso, oppure il conv_id non ha transcript
                     ma l'agente HA una vita precedente (chiavi conv_id_pre_*
                     in Postgres: il vecchio bug del conv coniato al rinfresco).

    Ritorna (classe, slug, dettaglio). Il controllo e' fatto quando si
    costruisce il comando, cioe' un istante prima del lancio: il transcript
    o c'e' gia' o non c'e'."""
    slug, data = _agente_da_registro(conv_id, agent_slug)
    atteso = transcript_path(engine, conv_id, work_dir=work_dir)
    if atteso and os.path.exists(atteso):
        return ("STORIA-PERSA", slug,
                f"transcript PRESENTE ({atteso}, {os.path.getsize(atteso)} byte) ma resume fallito")
    altrove = []
    if engine == "claude" and conv_id:
        try:
            altrove = glob.glob(os.path.join(CLAUDE_PROJECTS_DIR, "*", f"{conv_id}.jsonl"))
        except Exception:
            altrove = []
    if altrove:
        return ("STORIA-PERSA", slug,
                f"transcript in ALTRA cartella ({altrove[0]}): work_dir incoerente, resume impossibile da qui")
    if modalita == "fresco" or data.get("rinfresco_modalita") == "fresco":
        return ("FRESCO-VOLUTO", slug, "sessione azzerata di proposito, nessun transcript atteso")
    pre = sorted(k for k in data if k.startswith("conv_id_pre_"))
    if pre:
        return ("STORIA-PERSA", slug,
                f"conv_id senza transcript ma agente con vita precedente ({len(pre)} chiavi, ultima {pre[-1]}={data.get(pre[-1])})")
    return ("PRIMA-VITA", slug, "nessun transcript e nessuna vita precedente: avvio normale")


def _log_resume_fallback(engine, conv_id, work_dir, fresh_cmd, agent_slug="", modalita="ignoto"):
    """Antepone al comando 'fresh' una riga di log persistente (append, mai
    sovrascritto) — scatta SOLO quando 'resume' e' davvero fallito (exit non
    zero) ed e' il momento esatto in cui si perde la memoria pregressa.
    echo con singoli apici: work_dir/conv_id non contengono mai un apice
    singolo letterale (path/UUID), quindi non serve shlex.quote qui.

    `modalita` (DEV-110 §7, 09/08/2026): certa nel punto di chiamata di
    build_launch_cmd, si perdeva passando di qui. Senza, il guard non puo'
    distinguere un fresco VOLUTO (refresh pianificato: transcript assente per
    costruzione) da un resume fallito davvero — e suonava l'allarme pieno
    anche sui rinfreschi riusciti (§13.18).

    FIX 22/08/2026 (exec_fix_rinfresco): la riga base resta IDENTICA
    (`FALLBACK-FRESH engine= conv_id= work_dir=`), in CODA si aggiungono
    `classe= agent_slug= modalita= dettaglio=` (vedi classifica_fallback).
    L'allarme resume_guard (se il flag DEV-097 e' attivo) suona SOLO per
    classe=STORIA-PERSA: PRIMA-VITA e FRESCO-VOLUTO sono righe informative,
    senza allarme — un exec appena nato non e' una memoria persa."""
    classe, slug, dettaglio = classifica_fallback(engine, conv_id, work_dir, agent_slug, modalita)
    riga = (f"$(date '+%Y-%m-%d %H:%M:%S') FALLBACK-FRESH engine={engine} conv_id={conv_id} work_dir='{work_dir}'"
            f" classe={classe} agent_slug={_pulisci_per_echo(slug)} modalita={_pulisci_per_echo(modalita)}"
            f" dettaglio='{_pulisci_per_echo(dettaglio)}'")
    log_line = f"echo \"{riga}\" >> '{RESUME_FALLBACK_LOG}'"
    if classe == "STORIA-PERSA" and resume_guard_attivo():
        # `|| true`: la guardia serve ad avvisare, non deve MAI diventare essa
        # stessa il motivo per cui un agente non riparte.
        slug_arg = f" --agente '{slug}'" if slug else ""
        allarme = (f"'{PY_BIN}' '{RESUME_GUARD_BIN}' --allarme '{engine}' '{conv_id}' "
                   f"'{work_dir}'{slug_arg} --modalita '{modalita}' || true")
        return f"({log_line}; {allarme}; {fresh_cmd})"
    return f"({log_line}; {fresh_cmd})"


# ---------------------------------------------------------------------------
# Provider OpenAI-compatibile custom per il motore qwen (DEV seguito,
# 09/08/2026 — stima DeepSeek, "procedi" di Manfredo via Leo).
#
# Qwen Code CLI supporta nativamente provider OpenAI-compatibili diversi dal
# Qwen/DashScope di default, via OPENAI_API_KEY/OPENAI_BASE_URL/OPENAI_MODEL
# + `--auth-type openai` (verificato sulla documentazione ufficiale
# QwenLM/qwen-code, NON ANCORA su un lancio reale: manca la chiave — primo
# test vero da fare appena arriva). DeepSeek e' OpenAI-compatible anche sul
# tool/function calling (fino 128 tool, pattern tool_calls standard), quindi
# il mapping MCP che Qwen Code gia' fa per i modelli Qwen ha buone
# probabilita' di reggere — ma resta un'ipotesi da provare, non un fatto.
#
# Whitelist minima come per MODEL_ALIASES_CLAUDE: nessun base_url arbitrario
# letto da Postgres, solo provider NOTI e dichiarati qui.
QWEN_PROVIDERS = {
    "deepseek": {
        "base_url": "https://api.deepseek.com",
        "api_key_secret": "DEEPSEEK_API_KEY",  # nome del segreto in secret_env.load_secret()
        "default_model": "deepseek-v4-flash",  # nome modello attuale, deepseek-chat era superato
    },
}


def _provider_env_qwen(provider, model):
    """(env_extra, cleanup_extra) per il ramo qwen quando serve un provider
    OpenAI-compatibile custom (es. DeepSeek) al posto del Qwen/DashScope di
    default. provider=None -> ("", "") comportamento storico invariato.

    ⚠️ La CHIAVE non deve MAI finire letterale nel comando che viene
    INCOLLATO nella finestra tmux dell'agente (segnalazione di Betty sulla
    chiave DeepSeek, incollata in chiaro su Telegram una volta di troppo
    stanotte): sarebbe visibile sullo schermo, nello scrollback, e a
    chiunque faccia `tmux capture-pane` su quella finestra — pratica
    routinaria in questo ecosistema (Betty/BGA la fanno di continuo). Stesso
    trucco gia' in uso qui per il system prompt del ramo qwen: un file
    temporaneo 0600 letto con `$(cat ...)` al momento dell'ESECUZIONE — nel
    testo incollato compare solo il path, mai il segreto."""
    if not provider:
        return "", ""
    cfg = QWEN_PROVIDERS.get(provider)
    if not cfg:
        raise ValueError(f"provider qwen sconosciuto: '{provider}' (noti: {sorted(QWEN_PROVIDERS)})")

    import secret_env
    key = secret_env.load_secret(cfg["api_key_secret"])
    if not key:
        raise RuntimeError(
            f"provider '{provider}' richiede il segreto '{cfg['api_key_secret']}' "
            f"(env o core/.env) — non trovato, nessun lancio senza autenticazione"
        )

    tf = tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", delete=False,
                                      prefix="qwen_provkey_", suffix=".txt")
    tf.write(key)
    tf.close()
    os.chmod(tf.name, 0o600)  # ridondante coi default di NamedTemporaryFile, esplicito apposta

    modello = model or cfg["default_model"]
    env_extra = (f" OPENAI_API_KEY=\"$(cat '{tf.name}')\" OPENAI_BASE_URL='{cfg['base_url']}' "
                 f"OPENAI_MODEL='{modello}'")
    cleanup_extra = f"; rm -f '{tf.name}'"
    return env_extra, cleanup_extra


# Provider per il motore claude (Claude Code CLI). Verificato dal vivo
# dall'exec motore_unico_cc (17/08/2026, report
# /tmp/betty_docs/report_motore_unico_claude_code.md, evidenze in
# /tmp/betty_docs/evidenze_motore_unico/): DeepSeek e OpenRouter espongono
# endpoint in formato Anthropic Messages nativo, e Claude Code ci parla
# direttamente via ANTHROPIC_BASE_URL + ANTHROPIC_AUTH_TOKEN. Provati e
# passati: tool use reale a disco, streaming SSE, MCP stdio, system prompt
# 9k, --resume multi-turno (il transcript e' locale del CLI, quindi il
# resume funziona con QUALUNQUE provider), prompt cache DeepSeek.
# `max_context_tokens` e' OBBLIGATORIO per provider: senza, il CLI assume
# 200k per i modelli che non riconosce — per deepseek-chat (finestra vera
# ~128k) e' TROPPO GRANDE e l'auto-compact scatterebbe troppo tardi
# (errore API prima della compattazione). Rimedio verificato tra i 4
# suggeriti dal CLI stesso: CLAUDE_CODE_MAX_CONTEXT_TOKENS col valore vero.
# Stessa logica di QWEN_PROVIDERS: whitelist minima, nessun base_url
# arbitrario letto da Postgres.
CLAUDE_PROVIDERS = {
    "deepseek": {
        "base_url": "https://api.deepseek.com/anthropic",
        "api_key_secret": "DEEPSEEK_API_KEY",
        "default_model": "deepseek-v4-flash",   # nome modello attuale (verificato via GET /models); deepseek-chat era superato
        "max_context_tokens": 131072,          # solo fallback: la finestra vera la decide DEEPSEEK_MODEL_WINDOWS per modello
    },
    "openrouter": {
        "base_url": "https://openrouter.ai/api",
        "api_key_secret": "OPENROUTER_API_KEY",
        "default_model": "deepseek/deepseek-chat",
        # OpenRouter instrada a modelli con finestre diverse: 131072 e' il
        # valore prudente per il default deepseek; se si usa un modello con
        # finestra maggiore, va alzato QUI insieme al modello, non a mano.
        "max_context_tokens": 131072,
    },
}

# 23/08/2026 (mandato refactor_adapter_pool): il proxy LiteLLM NON sta più in
# mezzo al traffico degli agenti (perdite di identità e problemi col CLI Claude
# Code — verificato da exec_fix_adapter_identita che LiteLLM scarta header,
# metadata e user: l'identità dell'agente NON viaggia affatto attraverso il
# proxy). Le cascate che prima erano model group del proxy (cascata-pro /
# cascata-fast) ora sono gestite DALL'AVVIATORE: il CLI parla diretto al
# provider con la sua identità nativa (quota abbonamento claude) e la caduta di
# modello avviene qui, a livello di comando di lancio.
# Ordine = priorità di caduta: si prova il primo modello; se il CLI esce in
# errore al lancio (modello non disponibile, quota esaurita) si passa al
# successivo. I modelli sono quelli del flat plan (stessa whitelist di
# adapter_cli_abbonamenti.CLAUDE_ALIAS).
CASCATE_CLAUDE = {
    "cascata-pro": [
        "claude-opus-5[1m]",
        "claude-sonnet-5[1m]",
        "claude-haiku-4-5-20251001",
        "deepseek-v4-pro",
    ],
    "cascata-fast": [
        "claude-sonnet-5[1m]",
        "claude-haiku-4-5-20251001",
        "deepseek-v4-flash",
    ],
}

# Cascate native per il motore agy (antigravity). Stesso principio: si prova
# il modello primario, se il CLI esce in errore si passa al successivo. I nomi
# sono quelli esposti da `agy models` (stessa whitelist di
# adapter_cli_abbonamenti.AGY_MODELS).
CASCATE_AGY = {
    "cascata-pro": [
        "gemini-3.1-pro-high",
        "gemini-3.7-flash-high",
        "deepseek-v4-pro",
    ],
    "cascata-fast": [
        "gemini-3.7-flash-high",
        "gemini-3.7-flash-medium",
        "deepseek-v4-flash",
    ],
}

# 31/08/2026 (mandato exec_fix_cascata_l3_deepseek, autorizzato da Manfredo in
# risposta alla crisi quota Gemini/agy fleet-wide del 31/08): il terzo livello
# DeepSeek a consumo, deliberatamente lasciato fuori il 23/08 (nota storica
# sotto), è ora implementato. cascata-pro-l3 = deepseek-v4-pro, cascata-fast-l3
# = deepseek-v4-flash — nomi identici a quelli della vecchia config proxy, come
# richiesto. A differenza dei livelli 1-2 (stesso CLI, modello diverso), questo
# livello CAMBIA CLI (agy -> claude) o provider (claude nativo -> claude+
# deepseek): _build_cascade_agy e _build_cascade_claude riconoscono i nomi
# 'deepseek-*' e costruiscono quel gradino con _build_claude_provider_leg,
# SEMPRE attraverso il proxy deepseek_spend_guard (via_guard=True su
# _provider_env_claude) — mai la chiave DeepSeek diretta. Vedi report
# /tmp/betty_docs/report_exec_fix_cascata_l3_deepseek.md per la verifica e per
# un gap pre-esistente scoperto (non toccato qui, fuori scope): il canale
# claude+provider='deepseek' NON-cascata (agente con provider esplicito in
# Postgres) punta oggi diretto su api.deepseek.com, bypassando il guard.
#
# NOTA STORICA (mandato refactor_adapter_pool, 23/08/2026): la config del
# vecchio proxy aveva per ogni cascata questo terzo livello su DeepSeek a
# consumo. Non venne replicato al lancio nativo perché "il fallback a un
# provider a consumo a metà di una cascata è una decisione di costo che
# spetta a samantha/Manfredo" — decisione presa il 31/08/2026 (sopra).


def cascata_per_engine(engine, provider, model):
    """Lista modelli della cascata nativa per engine, o None se né provider né
    model indicano una cascata. La cascata può arrivare dal campo provider
    (forma canonica nuova: provider='cascata-pro') O dal campo model del DB
    (vecchi record provider='litellm_proxy' + model='cascata-X', riconosciuti
    senza toccare i record)."""
    mappa = None
    if engine == "claude":
        mappa = CASCATE_CLAUDE
    elif engine == "agy":
        mappa = CASCATE_AGY
    if not mappa:
        return None
    for campo in (provider, model):
        nome = (campo or "").strip().lower()
        if nome in mappa:
            return mappa[nome]
    return None


# 22/08/2026 (exec_contesto, ordine Manfredo "compatta troppo presto").
# Finestre REALI dei modelli deepseek serviti dal nostro provider, verificate
# contro la documentazione del provider (2026), NON stimate:
#   - deepseek-v4-flash : 1.000.000 token (DeepSeek: "1M context e' il default
#     su tutti i servizi DeepSeek ufficiali"; Vercel AI Gateway FAQ, EmpirioLabs,
#     Alibaba Cloud, OpenRouter). E' il modello di default delle sessioni deepseek.
#   - deepseek-chat     : 131.072 token (128K, mappa su V3.x; vedi report
#     exec_contesto_2208 §5). Resta per le sessioni storiche che lo nominano.
# Il CLI Claude Code NON ha auto-discovery per ID non-"claude-": se qui non
# dichiariamo la finestra vera, assume 200K (CLAUDE_CODE_MAX_CONTEXT_TOKENS
# assente) o usa la prima riga di default — con deepseek-v4-flash a 1M reale,
# dichiarare 131072 significa compattare a ~1/8 della finestra. Chiave = nome
# modello che arriva dal campo model del DB (o default del provider).
DEEPSEEK_MODEL_WINDOWS = {
    "deepseek-v4-flash": 1_000_000,
    "deepseek-v4-pro": 1_000_000,
    "deepseek-chat": 131_072,
}


def _provider_env_claude(provider, model, via_guard=False):
    """(env_extra, cleanup_extra, modello_effettivo) per il ramo claude quando
    dietro c'e' un provider alternativo (DeepSeek/OpenRouter) o il provider nativo
    Anthropic (provider=None).

    `via_guard` (31/08/2026, mandato exec_fix_cascata_l3_deepseek): SOLO per
    provider='deepseek'. False (default, comportamento INVARIATO byte per
    byte) = comando diretto su api.deepseek.com/anthropic con la chiave vera
    nel temp file — esattamente come oggi per un agente con provider=
    'deepseek' esplicito in Postgres (canale gia' verificato dal vivo, vedi
    report motore_unico_cc citato sopra). True (usato SOLO dal livello 3
    delle cascate, vedi _build_claude_provider_leg) = ANTHROPIC_BASE_URL
    punta al proxy deepseek_spend_guard invece che all'endpoint diretto: la
    chiave vera resta SOLO nel proxy (mai letta qui, nessun temp file,
    nessuna chiave nel comando incollato in tmux — il client passa un
    placeholder, il proxy lo sostituisce prima di inoltrare, vedi
    deepseek_spend_guard.py). Il proxy e' ENGINE-AGNOSTICO e inoltra
    self.path VERBATIM sopra UPSTREAM ("https://api.deepseek.com"): puntare
    a '<guard>/anthropic' produce lo STESSO url finale upstream
    ('.../anthropic/v1/messages') del canale diretto, quindi nessuna
    differenza di formato lato DeepSeek.

    24/08/2026 (mandato 1M token, Manfredo): per provider=None (quota abbonamento
    Claude flat nativo), impostiamo CLAUDE_CODE_MAX_CONTEXT_TOKENS=1000000 e
    CLAUDE_CODE_AUTO_COMPACT_WINDOW=800000 (80%), per portare sistemicamente
    tutti gli agenti Claude alla finestra di almeno 1.000.000 di token.

    Stesso vincolo di _provider_env_qwen: la CHIAVE non deve mai finire
    letterale nel comando incollato in tmux — temp file 0600 letto con
    $(cat ...) all'esecuzione, nel testo visibile compare solo il path.

    Ritorna anche il modello effettivo perche' col provider il campo model
    di Postgres contiene un nome NON-claude (es. 'deepseek-chat') che la
    guardia modello_valido_per_claude() scarterebbe: la validazione giusta
    qui e' "usa quello del DB se c'e', altrimenti il default del provider"."""
    if not provider:
        finestra = 1_000_000
        acw = int(finestra * 0.8)
        env_extra = (f" CLAUDE_CODE_MAX_CONTEXT_TOKENS={finestra} "
                     f"CLAUDE_CODE_AUTO_COMPACT_WINDOW={acw}")
        # 24/08/2026: normalizzazione [1m] per sbloccare la finestra 1M su Claude Code CLI.
        # Haiku non supporta la beta 1M (400 error provato su questo abbonamento) -> resta senza [1m].
        # Sonnet e Opus supportano 1M -> normalizziamo con suffisso [1m].
        # Se model e' None (maggioranza degli agenti), default esplicito a claude-sonnet-5[1m].
        if not model:
            modello = "claude-sonnet-5[1m]"
        else:
            m = model.strip()
            m_lower = m.lower()
            if m_lower.endswith("[1m]") or "haiku" in m_lower:
                modello = m
            elif m_lower in ("sonnet", "claude-sonnet", "claude-sonnet-5",
                             "claude-3-7-sonnet-20250219", "claude-3-5-sonnet-20241022") or \
                 m_lower.startswith("claude-sonnet"):
                modello = "claude-sonnet-5[1m]" if m_lower in ("sonnet", "claude-sonnet") else f"{m}[1m]"
            elif m_lower in ("opus", "claude-opus", "claude-opus-5", "claude-3-opus-20240229") or \
                 m_lower.startswith("claude-opus"):
                modello = "claude-opus-5[1m]" if m_lower in ("opus", "claude-opus") else f"{m}[1m]"
            else:
                modello = m
        return env_extra, "", modello
    cfg = CLAUDE_PROVIDERS.get(provider)
    if not cfg:
        raise ValueError(f"provider claude sconosciuto: '{provider}' (noti: {sorted(CLAUDE_PROVIDERS)})")

    modello = model or cfg["default_model"]
    # 22/08/2026 (exec_contesto): la finestra DICHIARATA deve essere quella
    # REALE del modello, non una costante conservativa di provider. Con
    # deepseek-v4-flash (1M reale) dichiarato 131072, l'auto-compact scattava
    # a ~1/8 della finestra: era il "compatta troppo presto" di Manfredo.
    # Per deepseek la finestra la decide DEEPSEEK_MODEL_WINDOWS per modello;
    # il max_context_tokens del provider resta solo come fallback per modelli
    # non in mappa (es. openrouter). AUTO_COMPACT_WINDOW = ~80% della finestra
    # reale (soglia sana, non 100%: evita di arrivare rasente all'errore API).
    if provider == "deepseek":
        finestra = DEEPSEEK_MODEL_WINDOWS.get(modello, cfg["max_context_tokens"])
    else:
        finestra = cfg["max_context_tokens"]
    acw = int(finestra * 0.8)  # 80% della finestra reale

    if via_guard:
        if provider != "deepseek":
            raise ValueError(f"via_guard e' supportato solo per provider='deepseek' (ricevuto '{provider}')")
        env_extra = (f" ANTHROPIC_BASE_URL='{DEEPSEEK_GUARD_URL}/anthropic' "
                     f"ANTHROPIC_AUTH_TOKEN='spend-guard-managed' "
                     f"CLAUDE_CODE_MAX_CONTEXT_TOKENS={finestra} "
                     f"CLAUDE_CODE_AUTO_COMPACT_WINDOW={acw}")
        return env_extra, "", modello

    import secret_env
    key = secret_env.load_secret(cfg["api_key_secret"])
    if not key:
        raise RuntimeError(
            f"provider '{provider}' richiede il segreto '{cfg['api_key_secret']}' "
            f"(env o core/.env) — non trovato, nessun lancio senza autenticazione"
        )

    tf = tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", delete=False,
                                      prefix="claude_provkey_", suffix=".txt")
    tf.write(key)
    tf.close()
    os.chmod(tf.name, 0o600)

    env_extra = (f" ANTHROPIC_BASE_URL='{cfg['base_url']}' "
                 f"ANTHROPIC_AUTH_TOKEN=\"$(cat '{tf.name}')\" "
                 f"CLAUDE_CODE_MAX_CONTEXT_TOKENS={finestra} "
                 f"CLAUDE_CODE_AUTO_COMPACT_WINDOW={acw}")
    cleanup_extra = f"; rm -f '{tf.name}'"
    return env_extra, cleanup_extra, modello


# Provider per il motore goose (launch tramite `goose run -s`, vedi ramo
# goose in build_launch_cmd). Provato sul campo dal benchmark test_goose
# (17/08/2026): OpenRouter + modello deepseek/deepseek-chat con la chiave
# passata solo via env. Stessa logica di QWEN_PROVIDERS: whitelist minima, no
# base_url arbitrari da Postgres, segreti letti da secret_env (core/.env).
GOOSE_PROVIDERS = {
    "openrouter": {
        "provider": "openrouter",
        "api_key_secret": "OPENROUTER_API_KEY",
        "env_var": "OPENROUTER_API_KEY",  # la var che goose legge per questo provider
        "default_model": "deepseek/deepseek-chat",
    },
    "deepseek": {
        # DeepSeek via API diretta: goose lo tratta come provider openai-compatibile.
        "provider": "openai",
        "api_key_secret": "DEEPSEEK_API_KEY",
        "env_var": "GOOSE_API_KEY",
        "api_host": "api.deepseek.com",
        "default_model": "deepseek-chat",
    },
}


def _provider_env_goose(provider, model):
    """(env_extra, cleanup_extra) per il ramo goose. provider=None -> si usa il
    default dichiarato in /root/.config/goose/config.yaml (openrouter: il
    benchmark test_goose l'ha provato con deepseek/deepseek-chat). Come per
    qwen, la CHIAVE non deve MAI finire letterale nel comando incollato nella
    finestra tmux: si scrive in un temp file 0600 e la si legge con `$(cat ...)`
    al momento dell'esecuzione.

    ⚠️ Verificato dal vivo (collaudo 17/08/2026): goose NON legge la chiave da
    config.yaml/keychain per il provider openrouter — pretende la var d'ambiente
    col nome ESATTO del provider (es. OPENROUTER_API_KEY). Senza quella var
    muore subito con "Configuration value not found: OPENROUTER_API_KEY".
    Quindi si inietta `env_var` di quel provider (oltre a GOOSE_API_KEY)."""
    import secret_env

    if provider is None:
        provider = "openrouter"  # default dichiarato da config.yaml
    cfg = GOOSE_PROVIDERS.get(provider)
    if not cfg:
        raise ValueError(f"provider goose sconosciuto: '{provider}' (noti: {sorted(GOOSE_PROVIDERS)})")

    key = secret_env.load_secret(cfg["api_key_secret"])
    if not key:
        raise RuntimeError(
            f"provider '{provider}' richiede il segreto '{cfg['api_key_secret']}' "
            f"(env o core/.env) — non trovato, nessun lancio senza autenticazione"
        )

    tf = tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", delete=False,
                                      prefix="goose_provkey_", suffix=".txt")
    tf.write(key)
    tf.close()
    os.chmod(tf.name, 0o600)

    modello = model or cfg["default_model"]
    env_extra = (f" GOOSE_PROVIDER='{cfg['provider']}' "
                 f"GOOSE_MODEL='{modello}' "
                 f"{cfg['env_var']}=\\\"$(cat '{tf.name}')\\\"")
    if cfg["env_var"] != "GOOSE_API_KEY":
        # tieni anche la var generica aperta (goose la usa su alcuni provider)
        env_extra += f" GOOSE_API_KEY=\\\"$(cat '{tf.name}')\\\""
    if cfg.get("api_host"):
        env_extra += f" GOOSE_API_HOST='{cfg['api_host']}'"
    cleanup_extra = f"; rm -f '{tf.name}'"
    return env_extra, cleanup_extra


def _build_claude_provider_leg(conv_id, work_dir, provider, modello, host_machine="VPS",
                                agent_slug="", modalita="ignoto", sp_flag=""):
    """31/08/2026 (mandato exec_fix_cascata_l3_deepseek): coppia (resume,
    fresh_logged, cleanup_extra) per UN gradino claude+provider — oggi usato
    solo per il salto DeepSeek di livello 3 nelle cascate (via_guard=True
    SEMPRE, mai la chiave diretta). Stessa identica costruzione di comando
    del ramo claude+provider NON-cascata (_build_launch_cmd_raw, ramo
    'claude' con provider valorizzato): stesso _provider_env_claude, stesso
    PATH nvm, stesso pattern --resume/--session-id. Riusata sia da
    _build_cascade_claude (salto di provider, stesso CLI: agy resta fuori)
    sia da _build_cascade_agy (salto di CLI, da agy a claude+deepseek) — per
    questo NON assume l'env_prefix nativo delle cascate (niente proxy):
    quello è specifico dei livelli 1-2, qui serve ANTHROPIC_BASE_URL."""
    provider_env, provider_cleanup, modello_eff = _provider_env_claude(
        provider, modello, via_guard=True)
    node_bin = os.path.dirname(DEEPCODE_BIN)  # stessa dir nvm di claude
    claude_path = (f"{node_bin}:/usr/local/sbin:/usr/local/bin:"
                   f"/usr/sbin:/usr/bin:/sbin:/bin")
    env_prefix = (f"env -u ANTHROPIC_API_KEY IS_SANDBOX=1 DISABLE_AUTOUPDATER=1 "
                  f"PATH='{claude_path}' HOST_MACHINE='{host_machine}'{provider_env}")
    model_flag = f" --model '{modello_eff}'"
    resume = f"{env_prefix} claude --resume '{conv_id}' --dangerously-skip-permissions{model_flag}{sp_flag}"
    fresh = f"{env_prefix} claude --session-id '{conv_id}' --dangerously-skip-permissions{model_flag}{sp_flag}"
    fresh_logged = _log_resume_fallback("claude", conv_id, work_dir, fresh, agent_slug, modalita)
    return resume, fresh_logged, provider_cleanup


def _build_cascade_claude(conv_id, work_dir, modelli, system_prompt=None, host_machine="VPS",
                          agent_slug="", modalita="ignoto"):
    """Comando di lancio a CASCATA per il motore claude: si prova ogni modello
    della cascata in sequenza, col CLI che parla DIRETTO al provider con la sua
    identità nativa (nessun ANTHROPIC_BASE_URL, nessun proxy locale: stesso
    env_prefix del ramo claude senza provider). La `work_dir` è preservata dal
    `cd` iniziale.

    Ordine delle prove (memoria PRIMA del modello):
      - prima TUTTI i --resume, uno per modello in ordine di priorità: se il
        resume fallisce per modello non disponibile, si riprova a riprendere
        col modello successivo (la memoria resta);
      - solo se ogni --resume è fallito, i --session-id (fresco) nello stesso
        ordine di modelli (è qui che scatta il log/guardia FALLBACK-FRESH di
        _log_resume_fallback, come nel ramo singolo).
    Il comando è una singola shell: `(r1 || r2 || r3 || f1 || f2 || f3)`; il
    primo CLI che resta vivo vince e gli altri non partono.

    ⚠️ Limite dichiarato del fallback a livello di processo: se il CLI PARTE
    ma il modello fallisce a metà turno (il CLI non esce), la caduta non
    scatta. È il compromesso del disegno 'cascate nell'avviatore' (mandato
    refactor_adapter_pool): la caduta vera avviene al lancio."""
    sp_flag = ""
    cleanup = ""
    if system_prompt:
        tf = tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", delete=False,
                                          prefix="claude_sysprompt_", suffix=".txt")
        tf.write(system_prompt)
        tf.close()
        sp_flag = f" --append-system-prompt-file '{tf.name}'"
        cleanup = f"; rm -f '{tf.name}'"

    _node_bin = os.path.dirname(DEEPCODE_BIN)  # stessa dir nvm di claude
    _claude_path = (f"{_node_bin}:/usr/local/sbin:/usr/local/bin:"
                    f"/usr/sbin:/usr/bin:/sbin:/bin")
    # identità nativa: il CLI usa il login OAuth del piano abbonamento;
    # nessuna variabile provider in più (niente ANTHROPIC_BASE_URL).
    # 24/08/2026 (mandato 1M token): CLAUDE_CODE_MAX_CONTEXT_TOKENS=1000000
    # e CLAUDE_CODE_AUTO_COMPACT_WINDOW=800000 forzati anche sulle cascate native.
    env_prefix = (f"env -u ANTHROPIC_API_KEY IS_SANDBOX=1 DISABLE_AUTOUPDATER=1 "
                  f"PATH='{_claude_path}' HOST_MACHINE='{host_machine}' "
                  f"CLAUDE_CODE_MAX_CONTEXT_TOKENS=1000000 "
                  f"CLAUDE_CODE_AUTO_COMPACT_WINDOW=800000")

    # FIX 24/08/2026 (exec_fix_lancio_fresco, ticket 435a): stessa identica
    # correzione del ramo singolo — e qui era anche PIU' grave, perche' la
    # cascata mette in fila PIU' `--resume` (uno per modello) prima dei
    # `--session-id`: bastava che il PRIMO aprisse il selettore interattivo per
    # bloccare tutta la catena. Il comando a 4 rami dell'incidente del
    # 24/08/2026 (r-opus || r-haiku || f-opus || f-haiku) nasceva proprio qui.
    salta_resume = (modalita == "fresco") or (not conv_id_resumibile(conv_id))

    livelli_resume = []
    livelli_fresh = []
    extra_cleanup = ""
    for modello in modelli:
        if modello in DEEPSEEK_MODEL_WINDOWS:
            # LIVELLO 3 (31/08/2026, mandato exec_fix_cascata_l3_deepseek):
            # salto di PROVIDER, stesso CLI claude — a differenza dei livelli
            # 1-2 (env_prefix nativo sopra, nessun ANTHROPIC_BASE_URL), qui
            # serve il ramo claude+deepseek via guard (mai la chiave diretta).
            resume, fresh_logged, leg_cleanup = _build_claude_provider_leg(
                conv_id, work_dir, "deepseek", modello, host_machine=host_machine,
                agent_slug=agent_slug, modalita=modalita, sp_flag=sp_flag)
            extra_cleanup += leg_cleanup
        else:
            model_flag = f" --model '{modello}'"
            resume = (f"{env_prefix} claude --resume '{conv_id}' "
                      f"--dangerously-skip-permissions{model_flag}{sp_flag}")
            fresh = (f"{env_prefix} claude --session-id '{conv_id}' "
                     f"--dangerously-skip-permissions{model_flag}{sp_flag}")
            fresh_logged = _log_resume_fallback("claude", conv_id, work_dir, fresh,
                                                agent_slug, modalita)
        if not salta_resume:
            livelli_resume.append(resume)
        livelli_fresh.append(fresh_logged)
    catena = " || ".join(livelli_resume + livelli_fresh)
    return f"cd '{work_dir}' && ({catena}){cleanup}{extra_cleanup}"


def _build_cascade_agy(conv_id, work_dir, modelli, host_machine="VPS",
                        agent_slug="", modalita="ignoto"):
    """Comando di lancio a CASCATA per il motore agy: si prova ogni modello in
    sequenza, sempre con l'identità nativa del CLI (nessun proxy). agy usa un
    solo flag `--conversation` per ripresa e sessione nuova insieme, quindi la
    catena è `(m1 || m2 || ...)` senza rami resume/fresh separati.

    31/08/2026 (mandato exec_fix_cascata_l3_deepseek): un modello 'deepseek-*'
    nella lista è il LIVELLO 3 — a differenza degli altri livelli, questo
    CAMBIA CLI (da agy a claude+deepseek via guard, mai la chiave diretta):
    quel gradino usa _build_claude_provider_leg (stesso conv_id/work_dir del
    chiamante, con resume||fresh perché claude, a differenza di agy, distingue
    i due casi) invece del lancio nativo AGY_BIN."""
    env_prefix = f"env HOST_MACHINE='{host_machine}'"
    salta_resume = (modalita == "fresco") or (not conv_id_resumibile(conv_id))
    livelli = []
    extra_cleanup = ""
    for modello in modelli:
        if modello in DEEPSEEK_MODEL_WINDOWS:
            resume, fresh_logged, leg_cleanup = _build_claude_provider_leg(
                conv_id, work_dir, "deepseek", modello, host_machine=host_machine,
                agent_slug=agent_slug, modalita=modalita)
            extra_cleanup += leg_cleanup
            leg = fresh_logged if salta_resume else f"({resume} || {fresh_logged})"
            livelli.append(leg)
        else:
            livelli.append(f"{env_prefix} '{AGY_BIN}' --conversation='{conv_id}' "
                           f"--dangerously-skip-permissions --model='{modello}'")
    catena = " || ".join(livelli)
    return f"cd '{work_dir}' && ({catena}){extra_cleanup}"


def _build_launch_cmd_raw(engine, conv_id, work_dir, system_prompt=None, host_machine="VPS", model=None,
                     agent_slug="", modalita="ignoto", provider=None, unix_user=None):
    """Costruisce il comando shell completo (cd + CLI) da passare a `tmux send-keys`.

    `agent_slug` (DEV-097) e' solo per l'avviso di memoria persa: serve a dire
    CHI e' ripartito vuoto invece di un conv_id nudo. Aggiunto in CODA alla
    firma, cosi' nessun chiamante esistente si rompe (tutti passano keyword
    args; stessa cautela gia' usata per `model` in DEV-020).

    `modalita` (DEV-110 §7/§8, 09/08/2026): 'fresco' se il chiamante sta
    coniando apposta un conv_id nuovo (un refresh pianificato — transcript
    assente e' normale), 'resume' se sta tentando di riprendere un conv_id
    esistente (transcript assente e' un guasto vero), 'ignoto' se non lo sa
    (default: nessun chiamante esistente si rompe, il guard usa la sua rete
    di sicurezza sull'mtime). Passata SOLO a resume_guard, nessun effetto sul
    comando di lancio in se'.

    `provider` (09/08/2026, stima DeepSeek; esteso a claude il 17/08/2026,
    exec migra_cc_pilota): per engine='qwen' un nome in QWEN_PROVIDERS, per
    engine='claude' un nome in CLAUDE_PROVIDERS (es. 'deepseek'), per
    engine='goose' un nome in GOOSE_PROVIDERS. None (default) = comportamento
    storico del motore (qwen: DashScope; claude: quota abbonamento Anthropic,
    comando INVARIATO byte per byte). Ignorato silenziosamente per gli altri
    engine (non ha senso li', non vale la pena farne un errore).

    `unix_user` (10/08/2026, guardrail fisico richiesto da Manfredo dopo
    l'incidente Regista/Redazione): SOLO per engine='deepcode'. None
    (default) = comportamento storico, processo lanciato come l'utente del
    chiamante (oggi sempre root). Un nome di utente Unix reale = il
    processo deepcode-cli parte sotto QUELL'utente via `sudo -u`, cosi' i
    permessi Unix standard tornano efficaci contro un agente che tenta di
    leggere/scrivere la cartella di lavoro di un ALTRO agente — cosa che
    root attraverserebbe comunque (verificato stanotte: chmod/chown non
    fermano un processo che gira come root). HOME resta forzato a '/root'
    (non quello di `unix_user`, che non ha una home directory reale — creata
    apposta solo così, per non disperdere i transcript in path nuovi che
    engine_adapter non saprebbe piu' ritrovare): la cartella
    `/root/.deepcode/projects/<codice-progetto>/` di QUESTO specifico agente
    va pre-creata e chownata all'utente dedicato PRIMA del primo lancio
    (fuori da questa funzione — qui si assume gia' fatto), cosi'
    DEEPCODE_PROJECTS_DIR resta lo stesso path che transcript_path() sa gia'
    calcolare, senza toccare quella funzione. Verificato dal vivo in un
    dry-run isolato (non sul work_dir reale): sessione completa, transcript
    scritto correttamente e di proprieta' del nuovo utente, nessun errore.
    Ignorato silenziosamente per gli altri engine."""
    # 25/08/2026 (samantha_3, incidente failover su agy): 'deepseek' come
    # engine di lancio NON esiste — e' claude su provider deepseek. Quando
    # scegli_motore()/router_fondi lo restituiscono (fallback per fondo
    # esaurito), senza normalizzazione il ramo non matcha e si cade sul
    # default agy -> gli agenti migrati venivano rilanciati su un motore
    # esaurito (Gemini) invece che su deepseek. Si normalizza qui, a monte
    # del ramo claude.
    if engine == "deepseek":
        engine = "claude"
        provider = provider or "deepseek"
        model = CLAUDE_PROVIDERS["deepseek"]["default_model"]

    if engine == "claude":
        # 23/08/2026 (mandato refactor_adapter_pool): se l'agente usa una
        # cascata, si costruisce un comando a LIVELLI (un modello per livello)
        # con l'identità nativa del CLI — niente proxy, niente env con
        # ANTHROPIC_BASE_URL. La cascata si riconosce dal campo provider
        # (forma canonica: 'cascata-pro'/'cascata-fast') o dal campo model
        # (vecchi record provider='litellm_proxy' + model='cascata-X').
        cascata = cascata_per_engine("claude", provider, model)
        if cascata:
            return _build_cascade_claude(conv_id, work_dir, cascata,
                                         system_prompt=system_prompt,
                                         host_machine=host_machine,
                                         agent_slug=agent_slug,
                                         modalita=modalita)
        sp_flag = ""
        cleanup = ""
        if system_prompt:
            tf = tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", delete=False,
                                              prefix="claude_sysprompt_", suffix=".txt")
            tf.write(system_prompt)
            tf.close()
            sp_flag = f" --append-system-prompt-file '{tf.name}'"
            cleanup = f"; rm -f '{tf.name}'"

        # DEV seguito (09/08/2026, ticket BGA): il campo Postgres 'model' non
        # produceva NESSUN effetto sul ramo claude — solo il ramo agy (sotto)
        # costruiva un model_flag. Un agente passato a un modello diverso
        # (es. /model sonnet in sessione) tornava quindi al default al primo
        # rilancio automatico, senza errore visibile: sembrava semplicemente
        # ripartito. `--model` e' un flag di sessione valido sia su
        # `--resume` che su `--session-id` (accetta alias 'sonnet'/'opus'/
        # 'fable' o il nome pieno del modello).
        # Collegamento provider (17/08/2026, exec migra_cc_pilota, mandato
        # Manfredo "il resto claude"): con un provider in CLAUDE_PROVIDERS
        # la guardia modello_valido_per_claude NON si applica — il modello
        # legittimo e' quello del provider (campo model del DB, altrimenti il
        # default del provider), gia' deciso da _provider_env_claude. La
        # guardia resta INTATTA sul canale Anthropic (provider assente):
        # allargarla farebbe passare un residuo sporco anche li'.
        provider_env, provider_cleanup, model = _provider_env_claude(provider, model)
        cleanup += provider_cleanup
        if not provider:
            if model and not modello_valido_per_claude(model):
                _log_model_scartato(conv_id, agent_slug, model)
                model = None
        model_flag = f" --model '{model}'" if model else ""
        # FIX 11/08/2026 (exec_dev_pathnode): stessa classe di guasto di
        # deepcode — claude ha shebang env node e il comando usa `claude` nudo
        # (risolto via PATH). Nei lanci via sudo il PATH e' il secure_path dei
        # sudoers (senza /root/.nvm) e `env node` prenderebbe node v18 di
        # sistema. PATH con il bin node nvm in testa, derivato in un solo
        # punto (stesso nvm di deepcode): nessuna stringa di versione a mano.
        _node_bin = os.path.dirname(DEEPCODE_BIN)  # stessa dir nvm di claude
        _claude_path = (f"{_node_bin}:/usr/local/sbin:/usr/local/bin:"
                        f"/usr/sbin:/usr/bin:/sbin:/bin")
        # DISABLE_AUTOUPDATER (17/08/2026, exec_update_cc): niente auto-update
        # spontaneo per-agente — l'update e' centralizzato e schedulato da
        # Samantha (runbook_update_claude_code.md). Senza, l'updater aggiorna
        # il doppione nvm a vuoto e stampa il banner "Auto-update failed"
        # sugli agenti. Vale per OGNI lancio claude, provider o no.
        env_prefix = f"env -u ANTHROPIC_API_KEY IS_SANDBOX=1 DISABLE_AUTOUPDATER=1 PATH='{_claude_path}' HOST_MACHINE='{host_machine}'{provider_env}"
        resume = f"{env_prefix} claude --resume '{conv_id}' --dangerously-skip-permissions{model_flag}{sp_flag}"
        fresh = f"{env_prefix} claude --session-id '{conv_id}' --dangerously-skip-permissions{model_flag}{sp_flag}"
        fresh_logged = _log_resume_fallback("claude", conv_id, work_dir, fresh, agent_slug, modalita)
        # FIX 24/08/2026 (exec_fix_lancio_fresco, ticket 435a): il ramo
        # `--resume` si OMETTE quando non puo' che fare danno. Due casi, in OR:
        #   1) modalita == 'fresco': il chiamante sta coniando ORA un conv_id
        #      nuovo (crea_exec, rinfrescami --fresco, failover_quota). Non c'e'
        #      NIENTE da riprendere per costruzione: il resume e' solo un giro a
        #      vuoto con il rischio del picker.
        #   2) conv_id non-UUID (o vuoto): vedi conv_id_resumibile() —
        #      Claude Code lo tratta come stringa di ricerca e apre il
        #      SELETTORE INTERATTIVO, che non esce mai e blocca l'agente per
        #      sempre. Qui va saltato a PRESCINDERE dalla modalita', perche' e'
        #      un blocco certo, non un rischio.
        # 'resume' e 'ignoto' con un UUID valido restano INVARIATI byte per
        # byte: e' il percorso che usa tutta la flotta ogni giorno.
        salta_resume = (modalita == "fresco") or (not conv_id_resumibile(conv_id))
        if salta_resume:
            return f"cd '{work_dir}' && {fresh_logged}{cleanup}"
        return f"cd '{work_dir}' && ({resume} || {fresh_logged}){cleanup}"

    if engine == "qwen":
        # Qwen Code CLI: --yolo equivale al --dangerously-skip-permissions di
        # Claude Code. Non esiste --append-system-prompt-file: il system prompt
        # si passa inline, letto da un temp file con $(cat ...) al lancio.
        # Verificato empiricamente (05/08/2026): --resume con ID inesistente
        # esce con codice 1, quindi il fallback || crea la sessione ex novo
        # con --session-id usando lo stesso conv_id.
        sp_flag = ""
        cleanup = ""
        if system_prompt:
            tf = tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", delete=False,
                                              prefix="qwen_sysprompt_", suffix=".txt")
            tf.write(system_prompt)
            tf.close()
            sp_flag = f" --append-system-prompt \"$(cat '{tf.name}')\""
            cleanup = f"; rm -f '{tf.name}'"

        # provider OpenAI-compatibile custom (DeepSeek ecc., vedi
        # _provider_env_qwen sopra): puo' sollevare (provider sconosciuto o
        # segreto assente) — nessun lancio silenzioso senza autenticazione.
        provider_env, provider_cleanup = _provider_env_qwen(provider, model)
        cleanup += provider_cleanup
        auth_flag = " --auth-type openai" if provider else ""

        # FIX 11/08/2026 (exec_dev_pathnode): stessa classe di guasto di
        # deepcode — qwen ha shebang env node. PATH con il bin node nvm in
        # testa (derivato in un solo punto, stesso nvm di deepcode), per non
        # trovarsi node v18 di sistema nei lanci via sudo.
        _node_bin = os.path.dirname(DEEPCODE_BIN)
        _qwen_path = (f"{_node_bin}:/usr/local/sbin:/usr/local/bin:"
                      f"/usr/sbin:/usr/bin:/sbin:/bin")
        env_prefix = f"env HOME='/root' PATH='{_qwen_path}' HOST_MACHINE='{host_machine}'{provider_env}"
        resume = f"{env_prefix} '{QWEN_BIN}' --resume '{conv_id}' --yolo{auth_flag}{sp_flag}"
        fresh = f"{env_prefix} '{QWEN_BIN}' --session-id '{conv_id}' --yolo{auth_flag}{sp_flag}"
        fresh_logged = _log_resume_fallback("qwen", conv_id, work_dir, fresh, agent_slug, modalita)
        return f"cd '{work_dir}' && ({resume} || {fresh_logged}){cleanup}"

    if engine == "deepcode":
        # Deep Code (deepcode-cli, @vegamo/deepcode-cli) — motore aggiunto
        # 09/08/2026 dopo un collaudo dal vivo riuscito (tool-calling reale
        # con DeepSeek confermato, non dedotto dalla doc).
        #
        # ⚠️ NESSUN (resume || fresh): verificato sul SORGENTE del binario
        # (dist/cli.js), non sulla doc — `process.argv` viene controllato
        # SOLO per --version/--help, nessun altro flag esiste. `--resume`
        # e' raggiungibile SOLO dal menu interattivo dentro la TUI. Ogni
        # lancio da qui e' quindi SEMPRE una sessione fresca: non e' una
        # scelta di questo adapter, e' un limite del binario terzo. Chi
        # rilancia un agente su questo motore lo trova sempre senza
        # memoria nativa — la continuita' deve venire dalla memoria
        # leggera esterna (stato su Obsidian), non dal motore.
        #
        deepcode_dir = os.path.join(work_dir, ".deepcode")
        os.makedirs(deepcode_dir, exist_ok=True)

        # System prompt: nessun --append-system-prompt-file. L'equivalente
        # e' un file AGENTS.md letto ad ogni avvio (livello progetto:
        # <work_dir>/.deepcode/AGENTS.md, prevale su quello utente).
        #
        # 10/08/2026 (Betty via Leo, terza occorrenza dello stesso bug — Marco,
        # Regista, ora Simone): i messaggi A2A mandati via --file da agenti
        # deepcode arrivano SENZA l'header [A2A_FROM:...], finiscono
        # classificati "sconosciuto" e restano invisibili nel flusso normale
        # (nel caso di Simone: 8 messaggi rimasti ore nell'inbox di Leo).
        # Causa di fondo, non un typo di un singolo agente: deepcode-cli NON
        # legge CLAUDE.md (dove vive la regola del formato, §4 comunicazione
        # A2A) — solo Claude Code lo fa. Un agente deepcode non ha NESSUN modo
        # di conoscere quella regola a meno che non gli venga ripetuta qui,
        # nel suo unico canale di istruzioni. Promemoria aggiunto in coda al
        # system_prompt di OGNI agente deepcode (fix sistemico, non per singolo
        # agente — cosi' vale anche per chi verra' dopo, non solo per Simone).
        _promemoria_a2a_deepcode = (
            "\n\n---\n"
            "📌 FORMATO OBBLIGATORIO per ogni messaggio A2A (send_a2a.py --file):\n"
            "la PRIMA riga del file deve essere ESATTAMENTE\n"
            "[A2A_FROM:<tuo_slug>] [A2A_TYPE:task|question|report|handoff]\n"
            "seguita dal corpo del messaggio. Senza questa riga il mittente "
            "risulta 'sconosciuto' e il messaggio puo' restare invisibile per "
            "ore a chi lo riceve (successo per davvero, non un rischio teorico)."
        )
        if system_prompt:
            with open(os.path.join(deepcode_dir, "AGENTS.md"), "w", encoding="utf-8") as f:
                f.write(system_prompt + _promemoria_a2a_deepcode)

        # Override comportamento deepcode (task Leo eb9e, 10/08): il GLOBALE
        # ~/.deepcode/settings.json ha thinkingEnabled:false + reasoningEffort
        # ma e' chmod 600 root-only, quindi i motori dc-isolati (utenti non-root)
        # NON lo leggono e ripiegherebbero sul default del CLI (thinkingEnabled:
        # true + reasoningEffort:max — piu' lento e piu' caro sul proxy). Si
        # PROPAGA qui l'override NEL settings di PROGETTO, a ogni lancio.
        # Eseguito SEMPRE (anche senza .mcp.json), cosi' vale anche per studi
        # senza config MCP (es. redattore_studio, che non aveva nemmeno il file).
        _od = _deepcode_global_override()
        sp_path = os.path.join(deepcode_dir, "settings.json")
        sp = {}
        if os.path.isfile(sp_path):
            try:
                with open(sp_path, encoding="utf-8") as f:
                    sp = json.load(f) or {}
            except Exception:
                sp = {}
        # Override comportamento (se il globale ha thinking/reasoning).
        if _od:
            if "thinkingEnabled" in _od:
                sp["thinkingEnabled"] = _od["thinkingEnabled"]
            if "reasoningEffort" in _od:
                sp["reasoningEffort"] = _od["reasoningEffort"]
        # PRIORITA' ALTA (10/08/2026, task Leo 10df/0f95): dialog
        # ask_permission di Deep Code ricorrente quando i dc-* scrivono in
        # /tmp/betty_recaps/<slug>.txt — "always allow" non persiste sulle
        # sessioni deepcode effimere. Soluzione strutturale: pre-autorizza
        # SEMPRE (anche senza override thinking) gli scope out-cwd (read+write
        # fuori dalla work_dir, dove vivono i recap e lo scambio file condiviso)
        # nel settings di PROGETTO, a ogni lancio. NON tocco defaultMode e uso
        # solo scope stretti (read/write-out-cwd): non apro network/mcp/delete.
        _perms = sp.get("permissions") or {}
        _allow = list(_perms.get("allow") or [])
        for _sc in ("read-out-cwd", "write-out-cwd"):
            if _sc not in _allow:
                _allow.append(_sc)
        _perms["allow"] = _allow
        # conserva deny/ask/defaultMode gia' presenti (se l'utente li ha
        # configurati); popoliamo SOLO allow, mai li azzeriamo.
        sp["permissions"] = _perms
        with open(sp_path, "w", encoding="utf-8") as f:
            json.dump(sp, f, indent=2)

        # MCP (DEV seguito, 10/08/2026 — domanda di Betty/Manfredo su Alfredo:
        # "senza arturo/messages-search non sa fare niente"). Verificato sul
        # SORGENTE (mcp-client.ts): deepcode-cli parla stdio+JSON-RPC via
        # spawn(command, args, env) — ESATTAMENTE il trasporto dei nostri
        # server MCP, stesso schema {command,args,env} di .mcp.json. La
        # differenza e' che deepcode NON fa auto-discovery di .mcp.json come
        # claude/qwen: mergeMcpServers legge SOLO da mcpServers dentro il
        # proprio settings.json. Si inietta qui, ad ogni lancio, cosi' non
        # serve mantenere una copia a mano che diverge dal .mcp.json vero
        # (SS13.8 del disegno DEV-110: due copie che concordano non dicono
        # niente finche' non divergono). Letto-uniti-scritto, non
        # sovrascritto: preserva eventuali altre chiavi gia' in
        # .deepcode/settings.json di progetto (model/thinkingEnabled ecc.).
        mcp_json_path = os.path.join(work_dir, ".mcp.json")
        if os.path.isfile(mcp_json_path):
            try:
                with open(mcp_json_path, encoding="utf-8") as f:
                    mcp_servers = (json.load(f) or {}).get("mcpServers")
                if mcp_servers:
                    deepcode_settings_path = os.path.join(deepcode_dir, "settings.json")
                    settings_progetto = {}
                    if os.path.isfile(deepcode_settings_path):
                        with open(deepcode_settings_path, encoding="utf-8") as f:
                            settings_progetto = json.load(f) or {}
                    settings_progetto["mcpServers"] = mcp_servers
                    with open(deepcode_settings_path, "w", encoding="utf-8") as f:
                        json.dump(settings_progetto, f, indent=2)
            except Exception as e:
                # Un MCP mancante in silenzio e' la stessa classe di guasto
                # di stanotte (Alfredo senza Regola Zero, senza che nessuno
                # se ne accorgesse): rumoroso su stderr, ma non deve MAI
                # impedire il lancio dell'agente per un .mcp.json rotto.
                print(f"[engine_adapter] ⚠️ deepcode: impossibile collegare MCP da "
                      f"'{mcp_json_path}': {e} — l'agente parte comunque, ma SENZA i tool MCP",
                      file=sys.stderr)

        # Guardrail fisico (10/08/2026): AGENTS.md/settings.json sopra sono
        # sempre scritti da QUESTA funzione, che gira come root — se
        # 'deepcode_dir' non esisteva ancora, os.makedirs() lo crea come
        # root, indipendentemente dal proprietario di 'work_dir'. Senza
        # questo chown, un agente con unix_user impostato non riuscirebbe a
        # LEGGERE (o a scrivere eventuali file propri dentro .deepcode/, es.
        # stato di sessione) una cartella che resterebbe di root — stesso
        # buco del caso "permessi non allineati" gia' visto stanotte, qui
        # prevenuto invece di scoperto dopo. Nessun effetto se 'deepcode_dir'
        # esisteva gia' con l'utente giusto (chown ripetuto e' innocuo).
        if unix_user:
            try:
                shutil.chown(work_dir, user=unix_user, group=unix_user)
                for root_dir, dirs, files in os.walk(deepcode_dir):
                    for nome in dirs + files:
                        shutil.chown(os.path.join(root_dir, nome), user=unix_user, group=unix_user)
                shutil.chown(deepcode_dir, user=unix_user, group=unix_user)
            except (LookupError, PermissionError, OSError) as e:
                print(f"[engine_adapter] ⚠️ deepcode: chown a '{unix_user}' fallito su "
                      f"'{deepcode_dir}': {e} — l'agente potrebbe non riuscire a leggere/scrivere "
                      f"la propria cartella di lavoro", file=sys.stderr)

        # DEEPCODE_MODEL/BASE_URL/API_KEY hanno priorita' MASSIMA sul merge
        # interno delle impostazioni (verificato sul sorgente:
        # resolveSettingsSources valuta prima systemEnv, cioe' i process env
        # DEEPCODE_*, in ogni catena ??/||) — nessun bisogno di toccare
        # ~/.deepcode/settings.json per agente, un env-prefix basta.
        #
        # BASE_URL punta SEMPRE al proxy locale (deepseek_spend_guard.py),
        # MAI su api.deepseek.com direttamente — e' il tetto di spesa voluto
        # da Manfredo (1 solo euro sulla chiave). Se il proxy non gira, il
        # lancio fallisce con connessione rifiutata: rumoroso, mai un
        # bypass silenzioso verso l'API vera con la chiave reale. La chiave
        # qui e' un placeholder innocuo: il proxy sostituisce quella vera
        # prima di inoltrare, il client non la vede mai.
        modello_ds = model or "deepseek-v4-flash"
        sudo_prefix = ""
        extra_env = ""
        # FIX 11/08/2026 (exec coord muoiono all'avvio, exec_dev_pathnode):
        # deepcode-cli ha shebang `#!/usr/bin/env node`, quindi il runtime node
        # viene risolto via PATH. Nei lanci fatti dai COORDINATORI lo strumento
        # gira via `sudo -n` (regola sudoers), che con `Defaults env_reset` +
        # `secure_path` AZZERA il PATH togliendo /root/.nvm — la shell della
        # finestra tmux lancia quindi deepcode con node di sistema v18, e il
        # bundle JS muore subito ('Invalid flags supplied to RegExp', 'Pane is
        # dead'). Fix: PATH con il bin di node nvm IN TESTA viene forzato
        # SEMPRE (sia con che senza `unix_user`). Il path è derivato in UN SOLO
        # punto da DEEPCODE_BIN (gia' ricavato da ~/.nvm/versions/node/* via
        # _find_deepcode_bin): nessuna stringa di versione scritta a mano,
        # quando si aggiorna node il PATH segue da solo.
        node_bin_dir = os.path.dirname(DEEPCODE_BIN)
        path_ristretto = (f"{node_bin_dir}:/usr/local/sbin:/usr/local/bin:"
                          f"/usr/sbin:/usr/bin:/sbin:/bin")
        if unix_user:
            # Guardrail fisico (10/08/2026): PATH esplicito perche' `sudo -u`
            # NON eredita il PATH interattivo di root (verificato dal vivo —
            # senza questo, il node di sistema v18 viene trovato al posto di
            # quello nvm v20 richiesto da deepcode-cli, che fallisce subito
            # con 'Invalid flags supplied to RegExp'). HOME=/root esplicito
            # per lo stesso motivo descritto nel docstring sopra. Un solo
            # `env` finale (non uno annidato dentro l'altro): piu' pulito da
            # leggere, stesso identico effetto.
            sudo_prefix = f"sudo -u {unix_user} "
            # FIX 10/08/2026 (bug reale trovato preparando il controllo
            # drift euser/PGUSER, non solo teorico): `sudo` azzera l'intero
            # ambiente per default (env_reset), incluso PGUSER=betty_coordinator
            # esportato dal wrapper ESTERNO di agent_launcher.py per gli
            # agenti coordinatori — verificato sui processi VERI di Redattore
            # e Regista (entrambi coordinatori + dc-*): /proc/<pid>/environ
            # non conteneva PGUSER affatto, il guardrail DB-read-only del
            # coordinatore risultava silenziosamente non applicato, non solo
            # non monitorato. "$PGUSER" e' espansione di shell al momento
            # dell'esecuzione: prende cio' che il wrapper esterno ha
            # esportato PRIMA in QUESTA stessa bash -c (se e' un coordinatore),
            # stringa vuota altrimenti (nessun effetto per chi non lo e').
        extra_env = f"HOME='/root' PATH='{path_ristretto}'"
        if unix_user:
            extra_env += f" PGUSER=\\\"$PGUSER\\\" "
        else:
            extra_env += " "
        env_prefix = (f"{sudo_prefix}env {extra_env}HOST_MACHINE='{host_machine}' "
                      f"DEEPCODE_MODEL='{modello_ds}' "
                      f"DEEPCODE_BASE_URL='{DEEPSEEK_GUARD_URL}' "
                      f"DEEPCODE_API_KEY='local-proxy-nessuna-chiave-reale-qui'")
        return f"cd '{work_dir}' && {env_prefix} '{DEEPCODE_BIN}'"

    if engine == "goose":
        # Goose CLI (v1.46.0, binario Rust singolo da /usr/local/bin) —
        # aggiunto 17/08/2026 come motore di riferimento per i profili basati
        # su chiavi OpenRouter/DeepSeek (dismissione progressiva di deepcode).
        #
        # Feedback eriptico dal benchmark test_goose: goose e' un CLI
        # BATCH-FIRST. `goose run` da solo esce dopo avere processato il primo
        # input; per tenerlo vivo in una finestra tmux in attesa dei prompt
        # incollati via paste-buffer serve `-s` (--interactive) ACCANTO a un
        # primo input (`-t/--text`): con `goose run -s -t "<primer>"` il CLI
        # risponde al primer e poi resta sulla riga di prompt interattiva
        # (`> Enter to send · Ctrl+J newline`), pronto a ricevere i messaggi
        # che engine_adapter/send_a2a/unified_telegram_engine incollano.
        # Misurato dal vivo in un pannello tmux di probe il 17/08/2026.
        #
        # ⚠️ PRIMO AVVIO one-shot: la prima volta goose mostro' il dialog di
        # telemetria ("Share anonymous usage data to help improve goose?").
        # Lo gestisce detect_startup_dialog (vedi sotto), NON questo comando.
        #
        # `provider` (opzionale): nome in GOOSE_PROVIDERS (es. 'openrouter' /
        # 'deepseek'). None = default, quello gia' in config.yaml (openrouter).
        # La chiave viaggia via env letta da `$(cat ...)` su un temp file 0600,
        # mai letterale nel comando incollato (regola chiavi, vedi qwen).
        provider_env, provider_cleanup = _provider_env_goose(provider, model)
        #
        # Il primer all'avvio: testo minimo che fa accendere la TUI senza far
        # partita una vera generazione di task. Viene poi sovrascritto dal
        # bootstrap iniettato da strumento_agenti.send_via_tmux_buffer quando
        # il pannello risulta pronto.
        primer = "pronto"
        nvm_node_dir = os.path.dirname(DEEPCODE_BIN)
        path_bin = (f"{nvm_node_dir}:/usr/local/sbin:/usr/local/bin:"
                    f"/usr/sbin:/usr/bin:/sbin:/bin")
        # OPENROUTER segue anche il nome keychain ufficiale di goose; lo
        # passiamo tramite GOOSE_API_KEY (usato dai provider openai-compat).
        env_prefix = (f"env HOME='/root' PATH='{path_bin}' "
                      f"HOST_MACHINE='{host_machine}'{provider_env}")
        launch = (f"{env_prefix} '{GOOSE_BIN}' run -s -t {shlex.quote(primer)}")
        return f"cd '{work_dir}' && ({launch}){provider_cleanup}"

    # agy (comportamento storico, invariato)
    # 23/08/2026 (mandato refactor_adapter_pool): cascata nativa agy se
    # provider/model indica una cascata — niente proxy, modelli provati in
    # sequenza con l'identità nativa del CLI. Solo in quel caso si cambia
    # percorso; per tutti gli altri agy il ramo resta identico byte per byte.
    cascata_agy = cascata_per_engine("agy", provider, model)
    if cascata_agy:
        return _build_cascade_agy(conv_id, work_dir, cascata_agy, host_machine=host_machine,
                                   agent_slug=agent_slug, modalita=modalita)
    env_prefix = f"env HOST_MACHINE='{host_machine}'"
    model_flag = f" --model='{model}'" if model else ""
    return f"cd '{work_dir}' && {env_prefix} '{AGY_BIN}' --conversation='{conv_id}' --dangerously-skip-permissions{model_flag}"


# ---------------------------------------------------------------------------
# Cap di memoria PER AGENTE (21/08/2026, dopo l'OOM delle 23:47 del 20/08:
# un solo claude.exe arrivato a 12,4GB ha fatto saltare per OOM l'intera
# unit ecosistema-services, tmux server e sessione betty compresi).
#
# Ogni comando di lancio viene avvolto in uno scope systemd transiente
# dentro ecosistema-agents.slice: il kernel applica memory.max al SOLO
# processo-agente (e ai suoi figli); se sfora, muore quel processo e basta,
# non l'ecosistema. La slice mette un tetto aggregato a tutti gli agenti
# (vedi /etc/systemd/system/ecosistema-agents.slice).
#
# AGENT_MEMORY_MAX / AGENT_SWAP_MAX (env, es. '5G'): override del default.
# AGENT_MEMORY_MAX=0 disattiva il wrapping (debug / emergenza).
# ---------------------------------------------------------------------------
AGENT_MEMORY_MAX_DEFAULT = "5G"
AGENT_SWAP_MAX_DEFAULT = "1G"
AGENT_SLICE = "ecosistema-agents.slice"


def memory_cap_prefix(tag=""):
    """Prefisso `systemd-run --scope ...` da anteporre a un comando di lancio
    agente. Stringa vuota se disattivato (AGENT_MEMORY_MAX=0) o se systemd-run
    non e' disponibile (macchine senza systemd: comportamento storico)."""
    mem = os.environ.get("AGENT_MEMORY_MAX", AGENT_MEMORY_MAX_DEFAULT)
    if mem in ("0", "", "off", "none") or not shutil.which("systemd-run"):
        return ""
    swap = os.environ.get("AGENT_SWAP_MAX", AGENT_SWAP_MAX_DEFAULT)
    import uuid
    tag = re.sub(r"[^A-Za-z0-9_.-]", "-", str(tag or "agent"))[:40]
    unit = f"agent-{tag}-{uuid.uuid4().hex[:6]}"
    return (f"systemd-run --scope --quiet --collect --slice={AGENT_SLICE} "
            f"--unit={shlex.quote(unit)} -p MemoryMax={shlex.quote(mem)} "
            f"-p MemorySwapMax={shlex.quote(swap)}")


def build_launch_cmd(engine, conv_id, work_dir, system_prompt=None, host_machine="VPS", model=None,
                     agent_slug="", modalita="ignoto", provider=None, unix_user=None):
    """Come _build_launch_cmd_raw (stessa firma, stesso comando byte per byte
    per ogni motore) ma avvolto nel cap di memoria per agente. Il comando raw
    resta un'unica stringa `bash -c '<raw>'` cosi' `cd`, `&&`, `||`, `$(cat)`
    e i cleanup finali girano nella stessa shell come prima: l'unica cosa che
    cambia e' il cgroup in cui vive il processo."""
    raw = _build_launch_cmd_raw(engine, conv_id, work_dir, system_prompt=system_prompt,
                                host_machine=host_machine, model=model, agent_slug=agent_slug,
                                modalita=modalita, provider=provider, unix_user=unix_user)
    prefix = memory_cap_prefix(agent_slug or (conv_id or "")[:8])
    if not prefix:
        return raw
    return f"{prefix} bash -c {shlex.quote(raw)}"


# ---------------------------------------------------------------------------
# Rilevazione dialog one-shot di Claude Code (solo al primissimo avvio di
# processo: "trust this folder" si ripresenta ad ogni lancio, "bypass
# permissions" è invece persistito globalmente dopo la prima accettazione)
# ---------------------------------------------------------------------------

def detect_claude_dialog(pane_text):
    """Se rileva un dialog interattivo one-shot di Claude Code, ritorna la
    sequenza di tasti da inviare per proseguire ('1' o '2'), altrimenti None."""
    if "Yes, I trust this folder" in pane_text:
        return "1"
    if "Bypass Permissions mode" in pane_text and "Yes, I accept" in pane_text:
        return "2"
    # Dialog "Resume from summary" (sessioni --resume vecchie/pesanti, oltre
    # una certa dimensione): senza gestirlo la finestra resta bloccata in
    # attesa di conferma manuale invece di ripartire da sola dopo un riavvio.
    # L'opzione 1 (default evidenziato, "Resume from summary") basta un Enter.
    if "Resuming the full session will consume" in pane_text and "Resume from summary" in pane_text:
        return "1"
    return None


def detect_qwen_dialog(pane_text):
    """Dialog one-shot di Qwen Code. Con --yolo su cartella già usata non ne
    sono stati osservati (probe 05/08/2026), ma il CLI ha un trust-prompt
    ("Do you trust this folder?") che bloccherebbe il lancio automatico."""
    if "Do you trust this folder?" in pane_text:
        return "1"
    return None


def detect_deepcode_dialog(pane_text):
    """Dialog one-shot di deepcode-cli osservato dal vivo (09/08/2026, primo
    lancio reale di Alfredo): controllo aggiornamento npm al primissimo
    avvio ("Deep Code latest version has been released..."), navigazione a
    frecce Su/Giu + Invio — MAI una scelta numerica come i dialog di
    claude/qwen. Il footer stesso lo dice: "Esc to ignore once". Ritorna un
    sentinel 'KEY:<nome>' (vedi send_startup_dialog_response): un tasto
    speciale, non testo da incollare — incollare la stringa "Escape" come
    testo non premerebbe affatto il tasto Esc."""
    if "latest version has been released" in pane_text and "Ignore once" in pane_text:
        return "KEY:Escape"
    return None


def detect_goose_dialog(pane_text):
    """Dialog one-shot di goose osservato dal vivo (17/08/2026, probe reale
    in un pannello tmux): al PRIMISSIMO avvio mostra il consenso telemetria —
    "Share anonymous usage data to help improve goose?" con menu ◆·◇ a frecce,
    default "● Yes" selezionato. Per procedere basta Invio ("Yes" evidenziato).
    Sentinella 'KEY:Enter': NIENTE testo da incollare — un paste qui
    scriverebbe nel campo anziché confermare la selezione."""
    if "Share anonymous usage data to help improve goose?" in pane_text:
        return "KEY:Enter"
    return None


def detect_startup_dialog(engine, pane_text):
    """Dispatch della rilevazione dialog one-shot di avvio per engine."""
    if engine == "qwen":
        return detect_qwen_dialog(pane_text)
    if engine == "deepcode":
        return detect_deepcode_dialog(pane_text)
    if engine == "goose":
        return detect_goose_dialog(pane_text)
    return detect_claude_dialog(pane_text)


# DEV seguito (09/08/2026, primo lancio reale di Alfredo su deepcode):
# il tempo di avvio della TUI NON e' uguale per tutti gli engine — quello di
# deepcode-cli osservato dal vivo e' ~30s (nodo+bundle JS piu' pesante da
# avviare), contro i pochi secondi di claude/qwen. Un'attesa fissa a 15s
# (agent_launcher.py, storica) lasciava passare il timeout PRIMA che la TUI
# fosse pronta: il prompt iniziale veniva incollato nel vuoto, agente vivo
# ma muto — sembrava un lancio riuscito e non lo era (successo davvero su
# Alfredo, corretto a mano da BGA). Centralizzato qui, non un numero
# indovinato dentro agent_launcher.py: e' esattamente cio' che questo modulo
# esiste per fare (vedi docstring in cima al file).
STARTUP_WAIT_SECONDS = {"deepcode": 45, "goose": 20}
STARTUP_WAIT_SECONDS_DEFAULT = 15


def startup_wait_seconds(engine):
    return STARTUP_WAIT_SECONDS.get(engine, STARTUP_WAIT_SECONDS_DEFAULT)


def send_startup_dialog_response(target_window, dialog_key, engine=None):
    """Invia la risposta a un dialog one-shot rilevato da detect_startup_dialog.
    Due forme, MAI confuse fra loro:
      - 'KEY:<nome>' -> tasto speciale vero (es. 'KEY:Escape'), inviato con
        `tmux send-keys` sul nome del tasto — NON e' testo da incollare.
      - qualunque altra stringa -> comportamento storico invariato: testo
        (tipicamente una cifra, "1"/"2") incollato via buffer + Invio, per i
        dialog numerici di claude/qwen gia' in produzione.
    `engine` passato a valle a send_via_tmux_buffer per il rilevamento
    "sta generando" corretto (vedi lì il FIX 10/08/2026)."""
    import subprocess
    # 22/08/2026 FIX classe "match per prefisso" (funzione condivisa tmux_esatto):
    # target a match ESATTO — un tasto mandato al pannello sbagliato (finestra con
    # lo stesso PREFISSO di nome) risponde a un dialog che non e' quello letto.
    target_window = tmux_esatto.target_finestra_esatto(target_window)
    if dialog_key.startswith("KEY:"):
        tasto = dialog_key[len("KEY:"):]
        subprocess.run(["tmux", "send-keys", "-t", target_window, tasto])
        return
    # P3 21/08/2026: qui si risponde a un DIALOG one-shot, non si consegna un
    # messaggio: il "pendente" e' il riquadro del dialog, non input umano.
    # Con la protezione attiva ci si rifiuterebbe di rispondere e l'agente
    # resterebbe appeso al dialog per sempre.
    send_via_tmux_buffer(target_window, dialog_key, engine=engine,
                         proteggi_input_umano=False)


# ---------------------------------------------------------------------------
# Ready / generating state detection (pattern testuali della UI del pannello)
# ---------------------------------------------------------------------------

# 15/08/2026 (indagine Igor, causa reale duplicati Telegram su Remo/agy): la
# fase "Loading..." di Gemini/agy dura ~7s e usa un frame-set di dot-spinner
# (⠋⠙⠹...) mai coperto qui - is_pane_generating tornava False durante quella
# finestra, facendo scattare il re-paste in send_prompt_to_tmux/send_a2a fino
# a 4 copie identiche dello stesso messaggio. Aggiunta additiva, testata 14/14
# stati (0 falsi positivi su idle) prima di attivarla.
_AGY_SPINNER_RE = re.compile(r'(?:Generating\.\.\.|Thinking\.\.\.|Loading\.\.\.|⣾|⣽|⣻|⢿|⡿|⣟|⣯|⣷|⠋|⠙|⠹|⠸|⠼|⠴|⠦|⠧|⠇|⠏)', re.IGNORECASE)


# --- agy: stato live nel FOOTER, non nel trascritto (fix 21/08/2026) --------
# Quante righe non vuote in fondo al pannello contano come "zona footer". La
# riga di stato e' l'ultima; sopra ci sono i separatori e la casella di input.
_AGY_FOOTER_LINES = 6

# Slot sinistro della riga di stato quando agy sta LAVORANDO. "esc to cancel"
# compare durante la generazione e anche mentre esegue un tool (misurato dal
# vivo su auruncia_team:AGY-AURUNCIA_TECH il 21/08 alle 17:06, con un grep
# ancora "running" e lo spinner "⡿ Generating..." a schermo). "Press up to
# edit queued messages" = input gia' accodato: il pannello non e' libero.
_AGY_FOOTER_BUSY_RE = re.compile(
    r"esc to cancel|Press up to edit queued messages", re.IGNORECASE)

# Slot sinistro quando agy e' FERMO sul prompt, pronto a ricevere input.
_AGY_FOOTER_IDLE_RE = re.compile(r"\? for shortcuts")

# Righe transitorie di generazione (spinner + verbo di stato): vivono solo
# durante il turno e spariscono quando finisce.
_AGY_LIVE_RE = re.compile(
    r"Thinking\.\.\.|Generating\.\.\.|Loading\.\.\.|Running\.\.\.|"
    r"⣾|⣽|⣻|⢿|⡿|⣟|⣯|⣷|⠋|⠙|⠹|⠸|⠼|⠴|⠦|⠧|⠇|⠏", re.IGNORECASE)

# Marcatori di TRASCRITTO: restano a schermo dopo la fine del turno. Valgono
# come indizio di attivita' SOLO quando non c'e' un footer riconoscibile.
_AGY_TRANSCRIPT_RE = re.compile(
    r"●\s*(?:Bash|Read|View|Edit|Write|Schedule|Command|Search)|"
    r"▸\s*Thought|Thought for|\(ctrl\+o to expand\)", re.IGNORECASE)


def _agy_footer(pane_text):
    """Ultime righe non vuote del pannello: la zona dove agy scrive lo stato."""
    righe = [r for r in pane_text.splitlines() if r.strip()]
    return "\n".join(righe[-_AGY_FOOTER_LINES:])


def is_pane_ready(engine, pane_text):
    """True se il pannello è fermo sul prompt idle, pronto a ricevere input."""
    if engine == "claude":
        return "bypass permissions on" in pane_text and "esc to interrupt" not in pane_text
    if engine == "qwen":
        # Footer idle: "YOLO mode (shift + tab to cycle)". Durante la
        # generazione compare "Enter to steer" nella stessa riga di stato.
        return "YOLO mode" in pane_text and "Enter to steer" not in pane_text
    if engine == "deepcode":
        # Placeholder idle: "Type your message...". Verificato dal vivo
        # (collaudo 09/08/2026): durante l'elaborazione compare "Thinking..."
        # nel pannello (vedi is_pane_generating), mai insieme al placeholder.
        return "Type your message" in pane_text and "Thinking..." not in pane_text
    if engine == "goose":
        # Footer idle misurato dal vivo (probe 17/08/2026): la riga di prompt
        # interattiva mostra "> Enter to send · Ctrl+J newline". Durante la
        # generazione quella riga scompare e compare l'indicatore di stato
        # "(Ctrl+C to interrupt)". Non basta "Enter to send": escludiamo
        # esplicitamente lo stato generating (vedi is_pane_generating)."
        return "Enter to send" in pane_text and not is_pane_generating(engine, pane_text)
    if engine == "agy":
        # FIX 19/08/2026 (exec_debug_paste_agy, mandato Samantha 8862): il
        # ramo agy richiede "? for shortcuts" (marker SOLO-ready) E il non in
        # generazione. Il vecchio ramo generico sotto usava `or "accept-edits"`:
        # quel marker compare ANCHE nel trust dialog di primo avvio (footer
        # "accept-edits · Gemini 3.7 Flash · low", senza "? for shortcuts") ->
        # falso positivo "pronto" -> lancia_agente (strumento_agenti.py) iniettava
        # il bootstrap in uno stato NON bracketed-paste-ready -> il testo
        # multiriga veniva digerito per righe/sezioni (coordinatore enrico,
        # DB 156df50e: 66 user step, bootstrap spezzato). Misurato dal vivo il
        # 19/08: footer dialog senza "? for shortcuts", footer ready con entrambi.
        return "? for shortcuts" in pane_text and not is_pane_generating(engine, pane_text)
    # 15/08/2026 (audit Igor, richiesta Manfredo): il vecchio `or ">" in
    # pane_text` dava falso positivo "pronto" anche mentre l'agente stava
    # generando (es. testo con "->" o "x>0" nell'output, o il prompt
    # "> [DOMANDA]" ancora visibile durante l'elaborazione). Tolto il bare
    # ">"; aggiunto "not is_pane_generating" per escludere esplicitamente la
    # finestra di generazione, testato 6/6 casi.
    return (("? for shortcuts" in pane_text or "accept-edits" in pane_text)
            and not is_pane_generating(engine, pane_text))


def is_pane_generating(engine, pane_text):
    """True se l'agente sta ancora generando la risposta."""
    if engine == "claude":
        # FIX 15/08/2026 (audit Igor, task urgente duplicati su Cosimo/CLAUDE):
        # il vecchio test era solo `"esc to interrupt" in pane_text` — ma Claude
        # Code moderno, durante l'elaborazione vera, mostra SPINNER di stato
        # diversi da 'esc to interrupt' (visti dal vivo: "✶ Quantumizing… (38s · ↓
        # 792 tokens)", "✽ Metamorphosing… (52s · ↓ 581 tokens)"). In quelle fasi
        # is_pane_generating tornava False -> il loop retry di send_prompt_to_tmux
        # ripassava il paste+invio => doppia/multipla sottomissione reale (3 turni
        # su Cosimo per un solo messaggio, MSG_ID 8774). Esteso con gli indicatori
        # reali, testato 8/8 (0 falsi positivi su idle).
        return bool(re.search(
            r"esc to interrupt|Quantumizing|Metamorphosing|Thinking|Reflecting|"
            r"Streaming|Processing|✶|✽|✸|⬢|⏳|\d+s\s*·\s*↓\s*\d+\s*tokens",
            pane_text, re.IGNORECASE))
    if engine == "qwen":
        return "Enter to steer" in pane_text
    if engine == "deepcode":
        # Verificato sul sorgente (dist/cli.js): l'indicatore di elaborazione
        # e' testualmente "Thinking... (Ns) · ↓ N tokens".
        return "Thinking..." in pane_text
    if engine == "goose":
        # Indicatore di stato misurato dal vivo (probe 17/08/2026): durante
        # l'elaborazione goose mostra una riga di stato con spinner e verbo
        # ("◓ Molting old code...  (Ctrl+C to interrupt)") e MAI il footer
        # idle "Enter to send". Segnale robusto: "(Ctrl+C to interrupt)" (o
        # variante "Ctrl-c") presente, oppure spinner + verbo di attività.
        return bool(re.search(
            r"Ctrl\+C to interrupt|Ctrl-c to interrupt|Molting|Rethinking|"
            r"Reviewing|Thinking|Processing|Cooking|Baking|"
            r"◓|◔|◕|◑|◒|◐|⣾|⣽|⣻|⢿|⡿|⣟|⣯|⣷",
            pane_text, re.IGNORECASE))
    if engine == "agy":
        # FIX 21/08/2026 (exec_campanellaio_dinamico, mandato samantha_1):
        # il vecchio ramo cercava in TUTTO il pannello anche i marcatori di
        # TRASCRITTO ("● Bash/Read/Edit", "▸ Thought for", "(ctrl+o to
        # expand)"). Quelle righe NON sono stato live: sono la storia di un
        # turno GIA' CONCLUSO e restano a schermo per sempre -> agy fermo sul
        # prompt = "sta generando" per sempre -> is_pane_ready sempre False ->
        # il campanellaio non trovava mai un'istanza libera e la coda del pool
        # moriva di fame in silenzio (21/08: 6 messaggi di Manfredo fermi in
        # a2a/cosimo/inbox/, il piu' vecchio da 169 min, nessun claim da 17h;
        # 5 pannelli agy coinvolti: COSIMO-2, ENRICO, AURELIO, AURUNCIA_TECH-2,
        # RADIO). Misurato su ricerca_lavoro:AGY-COSIMO-2 fermo sul prompt.
        #
        # Lo stato VIVO di agy sta nello slot sinistro della riga di footer,
        # non nel corpo del trascritto. Tre valori osservati dal vivo, mutuamente
        # esclusivi: "? for shortcuts" (idle), "esc to cancel" (in generazione,
        # anche mentre gira un tool), "Press up to edit queued messages"
        # (input accodato, pannello occupato).
        #
        # NON viene rimosso il `and not is_pane_generating(...)` di
        # is_pane_ready (fix 19/08 sul trust dialog): quel ramo resta intatto.
        # Qui si toglie solo il falso "occupato" permanente, mantenendo il
        # falso "pronto" gia' escluso.
        #
        # Ordine di valutazione (dal segnale piu' affidabile al piu' debole):
        #   1) footer di stato "occupato"  -> True
        #   2) spinner/verbo live ovunque  -> True   (invariato: quelle righe
        #      sono transitorie, spariscono a turno finito, quindi non danno
        #      falsi "occupato"; serve a non perdere la protezione anti-doppio
        #      invio dei fix 15/08)
        #   3) footer di stato "idle"      -> False  (il trascritto e' storia)
        #   4) nessun footer riconoscibile -> comportamento storico (prudente:
        #      trust dialog / boot restano "occupati" come prima)
        footer = _agy_footer(pane_text)
        if _AGY_FOOTER_BUSY_RE.search(footer):
            return True
        if _AGY_LIVE_RE.search(pane_text):
            return True
        if _AGY_FOOTER_IDLE_RE.search(footer):
            return False
        return bool(_AGY_TRANSCRIPT_RE.search(pane_text))
    return bool(_AGY_SPINNER_RE.search(pane_text))


# ---------------------------------------------------------------------------
# Estrazione dell'ultimo turno di conversazione da una cattura tmux del
# pannello (usato da agent_monitor.py per rilevare risposte complete da
# inoltrare a dashboard/TTS). Il marker del prompt utente e le parole di
# rumore da escludere differiscono tra agy ('> ' + status-bar agy) e
# claude ('❯ ' + status-bar Claude Code).
# ---------------------------------------------------------------------------

_AGY_PROMPT_MARKER = ">"
_CLAUDE_PROMPT_MARKER = "❯"
_QWEN_PROMPT_MARKER = ">"
_GOOSE_PROMPT_MARKER = ">"
_AGY_NOISE_WORDS = ['Accept-edits', 'shortcuts', 'shift+tab', 'Gemini']
_CLAUDE_NOISE_WORDS = ['bypass permissions', 'shortcuts', 'shift+tab', 'esc to interrupt', 'for agents']
_QWEN_NOISE_WORDS = ['YOLO mode', 'shift + tab', 'Enter to steer', 'Type your message', 'Context', 'shortcuts']
_GOOSE_NOISE_WORDS = ['Enter to send', 'Ctrl+J', 'newline', 'goose is ready', 'new session', 'interrupt']


def extract_last_turn(engine, pane_text):
    """Estrae l'ultimo turno (prompt utente + risposta) da una cattura tmux
    del pannello. Ritorna (last_turn_text, lines_after_prompt); (None, [])
    se nessun turno valido è stato trovato."""
    if engine == "claude":
        marker, noise_words = _CLAUDE_PROMPT_MARKER, _CLAUDE_NOISE_WORDS
    elif engine == "qwen":
        marker, noise_words = _QWEN_PROMPT_MARKER, _QWEN_NOISE_WORDS
    elif engine == "goose":
        marker, noise_words = _GOOSE_PROMPT_MARKER, _GOOSE_NOISE_WORDS
    else:
        marker, noise_words = _AGY_PROMPT_MARKER, _AGY_NOISE_WORDS

    # qwen indenta il prompt di 2 spazi ("  > testo"): la lookahead deve
    # tollerare il whitespace iniziale, a differenza di claude/agy (colonna 0).
    if engine == "qwen":
        pattern = re.compile(r'\n─{5,}\n|\n\s*(?=' + re.escape(marker) + r'\s+[^\n]+)')
    else:
        pattern = re.compile(r'\n─{5,}\n|\n(?=' + re.escape(marker) + r'\s+[^\n]+)')
    turns = [t.strip() for t in pattern.split(pane_text) if t.strip()]

    valid_turns = []
    for t in turns:
        first_line = t.splitlines()[0].strip() if t.splitlines() else ''
        if first_line.startswith(marker + ' ') and not any(k in first_line for k in noise_words):
            valid_turns.append(t)

    if not valid_turns:
        return None, []

    last_turn = valid_turns[-1]
    turn_lines = last_turn.splitlines()
    lines_after_prompt = [
        l.strip() for l in turn_lines[1:]
        if l.strip()
        and not l.strip().startswith('?')
        and not l.strip().startswith('➜')
        and 'accept-edits' not in l.lower()
        and 'bypass permissions' not in l.lower()
        and 'yolo mode' not in l.lower()
        and 'enter to steer' not in l.lower()
        and 'Flash' not in l
        and not set(l.strip()) <= {'─', '=', '-', ' '}
    ]
    return last_turn, lines_after_prompt


# ---------------------------------------------------------------------------
# Invio messaggio via Tmux paste-buffer (bracketed paste mono-blocco, come
# da regola imperativa del master AGENTS.md) — utile per l'A2A su claude,
# che non ha un binario 'agentapi' equivalente per bypassare tmux.
# ---------------------------------------------------------------------------

def send_via_tmux_buffer(target_window, text, engine=None, esc_first=False,
                         proteggi_input_umano=True):
    """Invia `text` alla finestra `target_window` (formato 'session:window')
    tramite tmux load-buffer/paste-buffer + Return, senza bypassare tmux.

    `engine`, se noto al chiamante, sceglie il rilevamento "sta generando"
    giusto (FIX 10/08/2026, bug reale trovato da Betty su Regista/Deep Code,
    stesso pattern duplicato qui e in send_a2a.py): usa is_pane_generating(),
    la stessa funzione engine-aware gia' in produzione altrove. Se `engine`
    non e' passato (chiamante non ancora aggiornato), resta il vecchio
    controllo testuale hardcoded su Claude Code come fallback esplicito —
    nessun comportamento cambiato per chi non lo passa.

    `esc_first` (10/08/2026, task Leo, incidente reale "ciao tech" rimasto
    incollato nel posto sbagliato): se True E engine=='deepcode', preme ESC
    prima di incollare — pulizia preventiva di eventuali stati di
    menu/dialog/AskUserQuestion aperti nel pannello che altrimenti
    intercettano il paste al posto del campo di testo normale. Stesso
    pattern gia' validato (Dario, 16/16 test) in send_a2a.py/
    a2a_bell_relay.py per il campanello A2A: ESC su un pannello gia' idle e'
    un no-op innocuo, nessun effetto collaterale osservato. DEFAULT False e
    NON dedotto automaticamente da `engine`, a differenza degli altri due
    file: questa funzione e' condivisa anche da chiamanti che rispondono a
    un dialog ONE-SHOT (send_startup_dialog_response e affini, righe piu'
    sotto) — per loro un ESC preventivo cancellerebbe il dialog stesso a cui
    si sta rispondendo, invece di ripulirlo. Passalo True solo se questa
    chiamata consegna un messaggio vero (come mcp_arturo.py), mai per una
    risposta a un dialog interattivo.

    `proteggi_input_umano` (P3, 21/08/2026, exec_p3_patch_input_box, mandato
    samantha_1): se True (default) e nella casella c'e' del testo pendente che
    NON e' nostro — tipicamente Manfredo che sta scrivendo un ordine a mano —
    questa funzione NON incolla, NON preme Invio e torna False, lasciando il
    messaggio al chiamante. E' il call-site piu' pericoloso di tutti: ci passa
    il campanellaio (pool_campanellaio.py:173), che rincuora QUALUNQUE finestra
    della flotta, compresa quella in cui Manfredo sta scrivendo. Va messo a
    False SOLO quando si risponde a un dialog one-shot di avvio (vedi
    send_startup_dialog_response), dove il "pendente" non e' input umano ma il
    riquadro del dialog stesso."""
    import subprocess
    import time as _time

    # 22/08/2026 FIX classe "match per prefisso" (funzione condivisa tmux_esatto):
    # UN SOLO punto di normalizzazione per tutta la funzione — da qui in giu'
    # ogni load-buffer/paste-buffer/send-keys usa il target ESATTO. Prima, una
    # finestra assente faceva incollare il messaggio nella finestra con lo stesso
    # PREFISSO di nome (es. AGY-PUBLIO -> AGY-PUBLIO-2): consegna a un altro
    # agente, silenziosa. Idempotente: se il chiamante l'ha gia' convertito, resta.
    target_window = tmux_esatto.target_finestra_esatto(target_window)

    if esc_first and engine == "deepcode":
        subprocess.run(f"tmux send-keys -t '{target_window}' Escape", shell=True)
        _time.sleep(2.0)  # margine "Interrupting…" -> idle pulito, stessa misura di send_a2a.py

    # P3 21/08/2026 (exec_p3_patch_input_box): PRIMA di incollare, la casella
    # e' di qualcun altro? Va chiesto qui, non solo prima del C-u in fondo: un
    # paste dentro la frase che Manfredo sta scrivendo gliela corrompe, e
    # l'End+C-m subito dopo la sottomette monca. Se non e' nostro si RIMANDA:
    # il messaggio resta al chiamante, che lo riproporra' al giro successivo.
    if proteggi_input_umano:
        _altrui = False
        try:
            try:
                from core.send_a2a import (_input_box_e_di_altri,
                                           _leggi_input_box,
                                           _log_pulizia_input_box)
            except ImportError:
                from send_a2a import (_input_box_e_di_altri,
                                      _leggi_input_box,
                                      _log_pulizia_input_box)
            # ESENZIONE (il verso opposto, obbligatorio): questa funzione serve
            # ANCHE a rispondere ai dialog one-shot di avvio, e i bordi ─── di
            # quel riquadro possono essere letti come i separatori dell'input
            # box. Rifiutare li' lascerebbe l'agente appeso al dialog per
            # sempre, in fase di creazione. Si riusa il rilevatore GIA'
            # esistente (detect_startup_dialog), non una regola nuova.
            try:
                _pane_ora = subprocess.run(
                    ["tmux", "capture-pane", "-t", target_window, "-p"],
                    capture_output=True, text=True, timeout=3).stdout
            except Exception:
                _pane_ora = ""
            if not (_pane_ora and detect_startup_dialog(engine, _pane_ora)):
                _altrui = _input_box_e_di_altri(target_window, text, None)
        except Exception:
            _altrui = False  # verifica non disponibile: comportamento storico
        if _altrui:
            try:
                _log_pulizia_input_box(target_window, None,
                                       _leggi_input_box(target_window),
                                       "RIMANDATO_PRE_PASTE",
                                       "engine_adapter:send_via_tmux_buffer")
            except Exception:
                pass
            print(f"[engine_adapter] ⏸️ '{target_window}': input box con testo NON nostro "
                  f"(qualcuno sta scrivendo a mano) — NON incollo e NON premo Invio. "
                  f"Messaggio non consegnato ora, primi 120 char: {text.strip()[:120]!r}")
            return False

    buf_file = tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", delete=False,
                                            prefix="engine_adapter_paste_", suffix=".txt")
    buf_file.write(text.strip())
    buf_file.close()
    # Nome buffer univoco per invio (DEV-006, 05/08/2026): i buffer tmux sono
    # globali al server, col nome fisso due invii ravvicinati si sovrascrivono
    # a vicenda e un agente puo' ricevere il messaggio destinato a un altro.
    buf_name = f"engine_adapter_{os.getpid()}_{int(_time.time() * 1000)}"
    try:
        subprocess.run(f"tmux load-buffer -b {buf_name} '{buf_file.name}'", shell=True)
        _time.sleep(0.1)
        subprocess.run(f"tmux paste-buffer -p -b {buf_name} -t '{target_window}'", shell=True)
        # Claude Code (a differenza di agy) elabora il paste in modo asincrono: un
        # invio troppo rapido del Return lascia il testo incollato ma non inviato
        # (verificato empiricamente: <0.3s fallisce, ~1.5s e' affidabile per testi
        # brevi; i prompt lunghi che collassano in "[Pasted text #N]" richiedono
        # piu' margine, quindi scaliamo con la lunghezza del testo).
        settle = min(1.5 + len(text) / 800, 5.0)
        _time.sleep(settle)
        subprocess.run(f"tmux send-keys -t '{target_window}' End", shell=True)
        _time.sleep(0.2)

        # L'Enter non sempre viene recepito al primo colpo (osservato ripetutamente
        # su sessioni appena riprese con --resume, o con più messaggi ravvicinati):
        # verifica che la generazione sia partita davvero, altrimenti ritenta.
        # _estrai_input va inizializzata qui, PRIMA del for: e' referenziata
        # anche dal blocco di pulizia post-for, e se la generazione parte al
        # primo Invio (attempt=0) il for fa break senza passare mai dal ramo
        # che la definisce -> altrimenti NameError (fix 17/08/2026).
        _estrai_input = None
        for attempt in range(4):
            if attempt > 0 and engine is not None:
                # FIX 14/08/2026 (report exec_debug_paste_claude, verificato dal
                # vivo 9/9 su 3 finestre): un Invio isolato su un pannello IDLE
                # con paste gia' incollato spesso non sottomette mai, nemmeno a
                # ripetizione. Ripassare lo stesso identico testo (bracketed
                # paste) prima di Invio sblocca sempre, senza creare doppioni
                # visibili nello scrollback. Il ripasso va fatto SOLO se il
                # pannello e' davvero idle (non is_pane_generating): se e'
                # occupato da un tool lungo, ripassare accoderebbe una copia
                # vera — li' basta il solo Invio, comportamento invariato.
                try:
                    pane_prima = subprocess.run(["tmux", "capture-pane", "-t", target_window, "-p", "-S", "-4"],
                                                 capture_output=True, text=True, timeout=3).stdout
                except Exception:
                    pane_prima = ""

                # FIX 17/08/2026 (dedup terza porta, chiude il duplicato):
                # il controllo "sta generando" (is_pane_generating) da solo NON
                # basta. Il pannello puo' essere non-generating E avere il testo
                # GIA' DENTRO l'input box (Invio non recepito, o marker
                # generating non ancora comparso). Due domande diverse:
                #   D1 = sta generando?        -> is_pane_generating()
                #   D2 = input box gia' pieno? -> _input_box_da_testo() (la
                #        stessa funzione scritta il 17/08 in send_a2a.py,
                #        RIUSATA qui, non una quarta variante). Import lazy:
                #        send_a2a importa engine_adapter, quindi a tempo di
                #        modulo creerebbe un ciclo; dentro la funzione no.
                input_box_pieno = False
                try:
                    from core.send_a2a import _input_box_da_testo as _estrai_input
                except ImportError:
                    try:
                        from send_a2a import _input_box_da_testo as _estrai_input
                    except ImportError:
                        _estrai_input = None
                if _estrai_input is not None and pane_prima:
                    pendente = (_estrai_input(pane_prima) or "").strip()
                    input_box_pieno = bool(pendente)
                if input_box_pieno:
                    # Testo gia' presente nell'input box: SOLO Invio, MAI un
                    # secondo paste (ri-incollare accoderebbe una COPIA vera).
                    subprocess.run(f"tmux send-keys -t '{target_window}' End", shell=True)
                    _time.sleep(0.2)
                elif not is_pane_generating(engine, pane_prima):
                    # Input box VUOTO e pannello realmente idle: ripasso come
                    # oggi (fallback 14/08 per l'Invio isolato non recepito).
                    subprocess.run(f"tmux load-buffer -b {buf_name} '{buf_file.name}'", shell=True)
                    _time.sleep(0.1)
                    subprocess.run(f"tmux paste-buffer -p -b {buf_name} -t '{target_window}'", shell=True)
                    _time.sleep(0.3)
                    subprocess.run(f"tmux send-keys -t '{target_window}' End", shell=True)
                    _time.sleep(0.2)
            subprocess.run(f"tmux send-keys -t '{target_window}' C-m", shell=True)
            _time.sleep(1.5)
            try:
                pane_check = subprocess.run(["tmux", "capture-pane", "-t", target_window, "-p", "-S", "-4"],
                                             capture_output=True, text=True, timeout=3).stdout
            except Exception:
                break
            if engine is not None:
                if is_pane_generating(engine, pane_check):
                    break  # generazione partita, confermata dal pattern del motore giusto
                if engine in ("agy", "claude", "deepcode"):
                    # P3 21/08/2026 (exec_p3_patch_input_box) — DIFETTO §4:
                    # questa uscita anticipata esisteva SOLO per agy. Per
                    # claude il loop non aveva NESSUNA condizione di uscita
                    # (is_pane_generating mockato/in falso negativo non scatta
                    # mai) e reinviava lo stesso messaggio ad ogni giro:
                    # QUATTRO copie sottomesse, misurate. E' lo stesso guasto
                    # gia' chiuso il 17-18/08 su send_a2a.py e
                    # unified_telegram_engine.py col single-shot, che qui non
                    # era mai arrivato. Si estende il criterio gia' collaudato
                    # ("input box vuota dopo il C-m = sottomesso") invece di
                    # inventarne uno nuovo. deepcode incluso: se dopo il turno
                    # il testo TORNA nell'input box (comportamento noto della
                    # sua TUI) la casella non e' vuota e l'uscita non scatta,
                    # quindi il comportamento resta quello di prima.
                    # qwen/goose restano fuori: nessuna prova raccolta sul
                    # loro comportamento, non si allarga alla cieca.
                    # FIX 19/08/2026 (exec_debug_paste_agy, mandato Samantha
                    # 8862): su agy la SOTTOMISSIONE svuota l'input box SUBITO
                    # (il testo non resta in coda come su claude). Se dopo il
                    # C-m l'input box e' vuoto e il pannello NON sta generando,
                    # il messaggio e' stato consegnato — Gemini 3.7 Flash
                    # risponde in ~1-2s, spesso PRIMA del check a +1.5s: qui
                    # il vecchio codice tornava al giro 2 e RI-INCOLLAVA una
                    # copia vera (falso negativo di is_pane_generating su
                    # risposta veloce). Si esce con consegna data.
                    box_ora = ""
                    try:
                        if _estrai_input is None:
                            try:
                                from core.send_a2a import _input_box_da_testo as _estrai_input
                            except ImportError:
                                from send_a2a import _input_box_da_testo as _estrai_input
                        box_ora = (_estrai_input(pane_check) or "").strip()
                    except Exception:
                        box_ora = ""
                    if not box_ora:
                        break
            elif "esc to interrupt" in pane_check or "bypass permissions on" not in pane_check:
                break  # legacy: nessun engine passato, vecchio pattern hardcoded su claude

        # FIX 17/08/2026 (punto 3 del mandato dedup terza porta): se dopo i
        # tentativi il testo e' ancora nell'input box e non e' stato sottomesso
        # (niente generazione partita), i soli Invio non bastano. Si fa la
        # pulizia VERA riusando _pulisci_input_box() di send_a2a.py (ESC + C-u,
        # mai C-c: su un pannello Claude Code idle C-c equivarrebbe a uscire e
        # chiuderebbe la sessione) e SOLO se e' riuscita si ripassa pulito.
        if engine is not None and _estrai_input is not None:
            try:
                pane_finale = subprocess.run(["tmux", "capture-pane", "-t", target_window, "-p", "-S", "-4"],
                                             capture_output=True, text=True, timeout=3).stdout
            except Exception:
                pane_finale = ""
            if (pane_finale and (_estrai_input(pane_finale) or "").strip()
                    and not is_pane_generating(engine, pane_finale)):
                svuotato = False
                try:
                    from core.send_a2a import _pulisci_input_box as _pulisci
                except ImportError:
                    try:
                        from send_a2a import _pulisci_input_box as _pulisci
                    except ImportError:
                        _pulisci = None
                if _pulisci is not None:
                    svuotato = _pulisci(target_window, text, None)
                if svuotato:
                    # campo realmente svuotato: ripaste pulito una sola volta
                    subprocess.run(f"tmux load-buffer -b {buf_name} '{buf_file.name}'", shell=True)
                    _time.sleep(0.1)
                    subprocess.run(f"tmux paste-buffer -p -b {buf_name} -t '{target_window}'", shell=True)
                    _time.sleep(0.3)
                    subprocess.run(f"tmux send-keys -t '{target_window}' End", shell=True)
                    _time.sleep(0.2)
                    subprocess.run(f"tmux send-keys -t '{target_window}' Enter", shell=True)
                    _time.sleep(1.5)
    finally:
        subprocess.run(f"tmux delete-buffer -b {buf_name}", shell=True)
        try:
            os.remove(buf_file.name)
        except OSError:
            pass
    # 22/08/2026 (mandato samantha_1, bug crea_exec segnalato da auruncia_tech_1):
    # la funzione tornava False sul rifiuto della guardia input-box ma None sul
    # percorso riuscito — indistinguibili per un chiamante che volesse SAPERE se
    # il messaggio e' partito. Ora il percorso riuscito dichiara True: e' cio' che
    # permette a strumento_agenti.lancia_agente di non dichiarare un successo piu'
    # ottimista di quello che e' successo davvero. Additivo: nessun chiamante
    # esistente controllava il valore di ritorno del percorso riuscito.
    return True


# ---------------------------------------------------------------------------
# Transcript path resolution (memoria/MemPalace, A2A reply polling, cleanup)
# ---------------------------------------------------------------------------

def _deepcode_cwd_to_project_code(work_dir):
    """Replica ESATTA dell'encoding di deepcode-cli (letto dal sorgente,
    dist/cli.js, funzione getProjectCode): SOLO '/' -> '-' e ':' rimosso.
    A DIFFERENZA di Claude/Qwen, NON tocca underscore ne' punti — usare
    _claude_cwd_to_project_slug qui produrrebbe un path che non esiste mai."""
    return work_dir.replace("\\", "-").replace("/", "-").replace(":", "")


def transcript_path(engine, conv_id, work_dir=None, full=True):
    """Path del file JSONL di trascrizione della sessione, per engine.
    Per 'claude' serve `work_dir` (determina la cartella di progetto)."""
    if not conv_id:
        return None
    if engine == "claude":
        if not work_dir:
            return None
        slug = _claude_cwd_to_project_slug(work_dir)
        return os.path.join(CLAUDE_PROJECTS_DIR, slug, f"{conv_id}.jsonl")
    if engine == "qwen":
        if not work_dir:
            return None
        # Encoding dello slug identico a Claude Code (verificato empiricamente:
        # /root/scratch/qwen_probe_dir -> -root-scratch-qwen-probe-dir).
        slug = _claude_cwd_to_project_slug(work_dir)
        return os.path.join(QWEN_PROJECTS_DIR, slug, "chats", f"{conv_id}.jsonl")
    if engine == "deepcode":
        if not work_dir:
            return None
        # ⚠️ Utile SOLO se conv_id combacia con un sessionId reale generato
        # da deepcode-cli — cosa che oggi NON e' garantita: il binario non
        # accetta un id imposto dall'esterno (vedi build_launch_cmd), genera
        # sempre il suo con crypto.randomUUID() al primo prompt. Path
        # corretto per costruzione, ma serve una sincronizzazione futura
        # (leggere sessions-index.json e riscrivere conv_id su Postgres) per
        # essere utile davvero al sensore/monitoraggio. Dichiarato, non
        # nascosto: e' un limite del binario terzo, non di questa funzione.
        code = _deepcode_cwd_to_project_code(work_dir)
        return os.path.join(DEEPCODE_PROJECTS_DIR, code, f"{conv_id}.jsonl")
    if engine == "goose":
        # goose persiste le sessioni in un DB SQLite (~/.local/share/goose/
        # sessions/sessions.db), NON in file JSONL per-turno come claude/
        # qwen/deepcode (verificato nel benchmark test_goose). Finché non
        # si implementa la sincronizzazione sessioni->Postgres (come per
        # deepcode), qui non c'e' un transcript JSONL da puntare: None,
        # dichiarato e non nascosto.
        return None
    fname = "transcript_full.jsonl" if full else "transcript.jsonl"
    return os.path.join(BRAIN_DIR, conv_id, ".system_generated", "logs", fname)


def _claude_content_to_text(content):
    """Estrae il testo dai blocchi di content di un messaggio Claude Code
    (lista di blocchi {'type': 'text'|'thinking'|..., 'text': ...})."""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                t = block.get("text", "")
                if t:
                    parts.append(t)
        return "\n".join(parts).strip()
    return ""


def _qwen_parts_to_text(parts):
    """Estrae il testo dalle parts di un messaggio Qwen Code
    (lista di {'text': ..., 'thought': bool?}). I blocchi di thinking
    ('thought': true) sono esclusi: interessa solo l'output reale."""
    if isinstance(parts, str):
        return parts.strip()
    if isinstance(parts, list):
        texts = []
        for part in parts:
            if isinstance(part, dict) and not part.get("thought"):
                t = part.get("text", "")
                if t:
                    texts.append(t)
        return "\n".join(texts).strip()
    return ""


def format_transcript_date(raw):
    """Normalizza un timestamp (ISO-8601 o già formattato) in 'YYYY-MM-DD HH:MM:SS'."""
    if not raw:
        return "N/D"
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(raw)


def _clean_agy_tag(text):
    if not text:
        return ""
    text = re.sub(r"<USER_REQUEST>\s*", "", text)
    text = re.sub(r"\s*</USER_REQUEST>", "", text)
    text = re.sub(r"<ADDITIONAL_METADATA>.*?</ADDITIONAL_METADATA>", "", text, flags=re.DOTALL)
    return text.strip()


def extract_turns(engine, transcript_file):
    """Estrae i turni utente/agente da un transcript, nel formato comune
    [{'user':.., 'agent':.., 'user_time':.., 'agent_time':..}, ...], a
    prescindere dal formato nativo (agy: USER_INPUT/PLANNER_RESPONSE con
    campo 'content' piatto; claude: type user/assistant con
    message.content a blocchi)."""
    if not transcript_file or not os.path.exists(transcript_file):
        return []

    turns = []
    current = {"user": None, "agent": None, "user_time": None, "agent_time": None}

    def flush():
        nonlocal current
        turns.append(current)
        current = {"user": None, "agent": None, "user_time": None, "agent_time": None}

    with open(transcript_file, "r", encoding="utf-8") as tf:
        for line in tf:
            if not line.strip():
                continue
            try:
                step = json.loads(line)
            except Exception:
                continue

            if engine in ("claude", "qwen"):
                # claude e qwen condividono la struttura a step type/role:
                # cambiano solo il campo contenuto ('content' a blocchi vs
                # 'parts') e il role dell'assistente ('assistant' vs 'model').
                step_type = step.get("type")
                if step_type not in ("user", "assistant"):
                    continue
                msg = step.get("message") or {}
                role = msg.get("role")
                if engine == "claude":
                    text = _claude_content_to_text(msg.get("content"))
                else:
                    text = _qwen_parts_to_text(msg.get("parts"))
                if not text:
                    continue
                ts = format_transcript_date(step.get("timestamp"))
                if role == "user":
                    if current["user"] is not None:
                        flush()
                    current["user"] = text
                    current["user_time"] = ts
                elif role in ("assistant", "model"):
                    if current["user"] is None:
                        continue
                    current["agent"] = text
                    current["agent_time"] = ts
                    flush()
            elif engine == "deepcode":
                # DEV seguito (10/08/2026, bug trovato verificando ask_agent
                # per Valerio -> deepcode): schema DIVERSO da claude/qwen,
                # verificato sul sorgente (buildUserMessage/
                # buildAssistantMessage): role/content in CIMA all'oggetto,
                # NON annidati sotto 'message'. content e' gia' testo
                # semplice (nessun blocco da appiattire). Timestamp:
                # 'createTime', non 'timestamp'. role puo' anche essere
                # 'tool'/'system' (skippati, come sopra per claude/qwen).
                role = step.get("role")
                if role not in ("user", "assistant"):
                    continue
                text = (step.get("content") or "").strip()
                if not text:
                    continue
                ts = format_transcript_date(step.get("createTime"))
                if role == "user":
                    if current["user"] is not None:
                        flush()
                    current["user"] = text
                    current["user_time"] = ts
                else:
                    if current["user"] is None:
                        continue
                    current["agent"] = text
                    current["agent_time"] = ts
                    flush()
            else:
                step_type = step.get("type")
                created_at = format_transcript_date(step.get("created_at"))
                content = (step.get("content") or "").strip()
                if not content:
                    continue
                if step_type == "USER_INPUT":
                    clean_content = _clean_agy_tag(content)
                    if not clean_content:
                        continue
                    if current["user"] is not None:
                        flush()
                    current["user"] = clean_content
                    current["user_time"] = created_at
                elif step_type == "PLANNER_RESPONSE":
                    if current["user"] is None:
                        continue
                    current["agent"] = content
                    current["agent_time"] = created_at
                    flush()

    if current["user"] is not None or current["agent"] is not None:
        turns.append(current)
    return turns


def count_user_messages(engine, transcript_file):
    """Conta i turni utente in un transcript (usato dal cleanup per inattività)."""
    if not transcript_file or not os.path.exists(transcript_file):
        return 0
    if engine == "goose":
        # Nessun transcript JSONL (stesso limite di transcript_path): i
        # messaggi vivono nel DB SQLite delle sessioni sole.
        return 0
    if engine == "claude":
        marker = '"type":"user"'
    elif engine == "qwen":
        # Solo i turni utente reali (non i tool-result, che in qwen sono
        # comunque step type=user ma senza provenance real_user).
        marker = '"provenance":"real_user"'
    elif engine == "deepcode":
        # Verificato sul sorgente (buildUserMessage): i turni utente reali
        # hanno "role":"user" — stesso campo/valore di un formato OpenAI-style.
        marker = '"role":"user"'
    else:
        marker = '"type":"USER_INPUT"'
    count = 0
    with open(transcript_file, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            if marker in line:
                count += 1
    return count
