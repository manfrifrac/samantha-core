#!/usr/bin/env python3
"""
engine_budget.py — quanto stiamo consumando per ciascun motore, e chi conviene
lanciare adesso.

NATO IL 05/08/2026 su richiesta di Manfredo: "hai modo di capire token rimanenti
dei 3 abbonamenti che ho attivi e gestire chi lanciare?".

COSA SI PUO' SAPERE DAVVERO (verificato, non ipotizzato):
- claude : nessun CLI/API espone il residuo del piano (l'abbonamento risulta
           "pro" in ~/.claude/.credentials.json, ma senza contatore). Il CONSUMO
           pero' e' ricostruibile: ogni messaggio nei transcript
           ~/.claude/projects/<progetto>/<conv_id>.jsonl porta il proprio blocco
           `usage` con input/output/cache.
- qwen   : tiene un registro strutturato suo, ~/.qwen/usage/token-usage-YYYY-MM.jsonl,
           una riga per chiamata con modello, sessione e token. E' il piu' preciso.
- agy    : NON traccia i token da nessuna parte (history.jsonl contiene solo i
           prompt, le conversazioni sono .db opachi). Di agy possiamo sapere solo
           quanti agenti stanno girando, non quanto stanno consumando.

Quindi questo strumento NON stima un "residuo" (sarebbe inventato): misura il
CONSUMO REALE dove esiste, il carico di macchina, e su quella base dice quale
motore conviene usare per il prossimo agente.

Uso:
    ./venv/bin/python3 engine_budget.py            # rapporto di oggi
    ./venv/bin/python3 engine_budget.py --giorni 3 # ultimi 3 giorni
"""
import fcntl
import json
import os
import subprocess
import sys
import time
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tmux_esatto

CLAUDE_PROJECTS = "/root/.claude/projects"
QWEN_USAGE_DIR = "/root/.qwen/usage"
CREDENZIALI_CLAUDE = "/root/.claude/.credentials.json"

# Pesi di CONSUMO relativo, normalizzati al peso input base (Marco,
# 08/08/2026, derivati dal listino pubblico Anthropic Opus 5 — $5/MTok
# input, $25/MTok output — solo per ricavare il RAPPORTO tra le voci, non
# tarati a mano; confermano al decimale la stima indipendente di
# Betty_Ottimizzazione). ⚠️ Il piano e' un abbonamento 'pro' a consumo
# forfettario, NON fatturazione a consumo API (verificato: subscriptionType
# nelle credenziali) — questi pesi NON rappresentano una spesa reale in $,
# nessuna fattura viene emessa su questi numeri. Servono solo a capire il
# RITMO relativo di consumo della quota dell'abbonamento (es. cache_read
# pesa 1/10 di un token input "pieno", non e' gratis ne' e' spreco: e' il
# costo fisiologico del prompt caching, senza cache costerebbe 10x — va
# contato, non ridotto). NON usati per pre-calcolare un totale qui dentro
# (deliberato: i pesi variano per modello — Sonnet/Haiku diversi da Opus —
# e per TTL della cache, un totale gia' fuso perderebbe l'informazione
# senza modo di recuperarla). Restano come costante di riferimento per chi
# pondera a valle i 4 valori grezzi restituiti da
# consumo_claude(per_conv_id=True), cosi' non li reinventa.
PESO_INPUT = 1.0
PESO_CACHE_CREAZIONE_TTL_5MIN = 1.25
PESO_CACHE_CREAZIONE_TTL_1H = 2.0
PESO_CACHE_READ = 0.1
PESO_OUTPUT = 5.0


def _oggi_locale():
    return datetime.now().strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# SONDA /usage — la QUOTA RESIDUA vera
#
# Idea di Manfredo (05/08/2026): "puoi fare un controllo in un terminale per
# tutti e 3 con /usage quando devi fare una ripartizione corretta?". Aveva
# ragione lui e la mia prima risposta era incompleta: il residuo NON e' in
# nessun file, ma i CLI lo sanno e lo mostrano col comando interattivo /usage.
# Qui apriamo una finestra tmux usa-e-getta, lanciamo il CLI, chiediamo /usage,
# leggiamo il pannello e chiudiamo.
#
# Cosa risulta davvero (verificato):
#   claude -> % usata della sessione corrente e della SETTIMANA, con orari di reset
#   agy    -> quota per GRUPPO di modelli (Gemini separato da Claude/GPT), in %
#             DISPONIBILE (attenzione: 100% = tutto libero, non tutto consumato)
#   qwen   -> nessuna quota: /usage mostra solo statistiche della sessione
# ---------------------------------------------------------------------------

SONDA_WIN = "services:PROBE-Usage"
_CLI = {
    "claude": "env -u ANTHROPIC_API_KEY IS_SANDBOX=1 claude --dangerously-skip-permissions",
    "qwen": "qwen --yolo",
    "agy": "agy --dangerously-skip-permissions",
    # 'deepcode' ESCLUSO deliberatamente da questa sonda TUI (gap segnalato da
    # Betty/Leo, 10/08/2026): deepcode-cli non ha un comando interattivo
    # '/usage' come gli altri tre, e la sua vera quota (tetto locale di
    # chiamate) e' gia' misurata altrove in modo piu' diretto e affidabile —
    # vedi consumo_deepcode() sotto, che legge lo stato reale del proxy
    # invece di aprire una sessione TUI usa-e-getta per un comando che non
    # esiste.
}


# ---------------------------------------------------------------------------
# FIX TIMEOUT 18/08/2026 (mandato Samantha, Exec rinforza_probe_quota):
# probe_fondi_flat andava in timeout (200s) nel cron quando il boot del CLI
# sotto carico sforava i tempi FISSI della sonda. Ora:
#   - attesa ADATTIVA (solo claude, l'unico con segnale di ready affidabile):
#     si aspetta is_pane_ready() invece di un sonno cieco — boot lento sotto
#     carico = si aspetta il CLI vero, niente /usage incollato nel vuoto;
#   - margine `timeout` (240s): oltre si alza TimeoutError, il chiamante logga
#     'sonda quota fallita' e ritenta al giro dopo — MAI 'ok' per assenza di dato;
#   - ogni comando tmux ha un timeout proprio (20s): un tmux bloccato non puo'
#     piu' appendere la sonda oltre il margine;
#   - kill-window in `finally`: la finestra usa-e-getta viene SEMPRE chiusa,
#     anche su errore — niente zombie `services:PROBE-Usage`.
# ---------------------------------------------------------------------------
SONDA_TIMEOUT = 240          # margine sicuro per l'intera sonda (default)
_SONDA_TMUX_TIMEOUT = 20     # per singolo comando tmux


# ---------------------------------------------------------------------------
# FIX CONCORRENZA 21/08/2026 (exec_guardiano_f4, mandato samantha_2)
#
# SONDA_WIN era un nome FISSO ('services:PROBE-Usage') con SETTE chiamanti e
# NESSUN lock. sonda_quota() comincia con una kill-window su quel nome: due
# chiamanti in corsa si uccidevano la finestra a vicenda ("can't find window")
# e il perdente tornava con righe vuote o con il pannello dell'altro. Il danno
# non e' il crash — e' che un valore SBAGLIATO o VECCHIO veniva spacciato per
# una lettura fresca, ed e' su letture cosi' che parte un failover a pagamento.
#
# Tre rimedi, qui e non nei chiamanti (erano sette a doverli ricordare):
#   1. FLOCK: una sola sonda per volta su tutta la macchina. Chi trova occupato
#      aspetta la fine dell'altra e ne riusa il risultato invece di rifarla.
#   2. FINESTRA UNIVOCA per chiamata (pid + token): nessuno puo' piu'
#      kill-window addosso alla sonda di un altro processo.
#   3. ETA' DEL DATO + max 1 sonda/ora: la sonda costa fino a 240s per motore.
#      Il risultato sta in cache condivisa; entro l'ora si serve la cache, e
#      chi vuole sapere quanto e' vecchio il numero usa sonda_quota_dettaglio().
# ---------------------------------------------------------------------------
SONDA_LOCK_PATH = "/tmp/engine_budget_sonda.lock"
SONDA_CACHE_PATH = "/root/ecosistema_agenti/logs/engine_budget_sonda_cache.json"
SONDA_INTERVALLO_MIN_S = 3600     # max 1 sonda reale all'ora per motore
_SONDA_ATTESA_LOCK_S = 300        # quanto si aspetta la sonda di un altro processo


def _tmux(cmd):
    """Wrapper dei comandi tmux della sonda: capture_output + timeout proprio,
    cosi' un tmux occupato/busy non puo' bloccare la sonda per sempre.

    22/08/2026 FIX classe "match per prefisso" (funzione condivisa tmux_esatto):
    il valore che segue '-t' viene normalizzato a target ESATTO in UN SOLO punto,
    valido per tutti i comandi della sonda. Il caso pericoloso e' la kill-window
    della finestra usa-e-getta: per prefisso ucciderebbe la finestra di un'altra
    sonda (o di un agente) che comincia con lo stesso nome. Un target di sola
    sessione (senza ':') resta invariato.
    """
    cmd = list(cmd)
    if "-t" in cmd:
        i = cmd.index("-t")
        if i + 1 < len(cmd):
            cmd[i + 1] = tmux_esatto.target_finestra_esatto(cmd[i + 1])
    return subprocess.run(["tmux"] + cmd, capture_output=True, text=True,
                          timeout=_SONDA_TMUX_TIMEOUT)


def _pane(win=None):
    return _tmux(["capture-pane", "-pt", win or SONDA_WIN, "-S", "-40"]).stdout


def _finestra_sonda_univoca():
    """Nome di finestra irripetibile, derivato dal nome base configurato.
    Il prefisso resta 'PROBE-' perche' e' quello che la lista_bianca del
    guardiano usa per NON toccare le finestre di servizio."""
    sess, _, base = SONDA_WIN.partition(":")
    return f"{sess}:{base}-{os.getpid()}-{uuid.uuid4().hex[:6]}"


def _cache_sonda_carica():
    try:
        with open(SONDA_CACHE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _cache_sonda_salva(engine, righe):
    dati = _cache_sonda_carica()
    dati[engine] = {"righe": righe, "ts": time.time()}
    try:
        os.makedirs(os.path.dirname(SONDA_CACHE_PATH), exist_ok=True)
        tmp = SONDA_CACHE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(dati, f, ensure_ascii=False, indent=2)
        os.replace(tmp, SONDA_CACHE_PATH)
    except Exception:
        pass


def _cache_sonda_voce(engine):
    """(righe, eta_s) dalla cache condivisa, o (None, None) se non c'e'."""
    v = _cache_sonda_carica().get(engine) or {}
    if not v.get("ts"):
        return None, None
    return v.get("righe"), max(0.0, time.time() - v["ts"])


def sonda_quota_dettaglio(engine, attesa=26, timeout=SONDA_TIMEOUT, forza=False):
    """Come sonda_quota, ma dichiara VALORE + FONTE + ETA' invece di una lista
    nuda (requisito di tracciabilita' F4).

    -> {righe, eta_s, ts, da_cache, fonte, finestra, errore}
    `eta_s` e' 0 solo per una sonda appena eseguita; per la cache e' l'eta' vera.
    """
    fonte = f"engine_budget.sonda_quota('{engine}') -> /usage"
    righe_cache, eta_cache = _cache_sonda_voce(engine)
    if not forza and righe_cache is not None and eta_cache < SONDA_INTERVALLO_MIN_S:
        return {"righe": righe_cache, "eta_s": eta_cache, "da_cache": True,
                "ts": time.time() - eta_cache, "fonte": fonte + " (cache)",
                "finestra": None, "errore": None}

    fh = open(SONDA_LOCK_PATH, "w")
    preso = False
    scadenza = time.monotonic() + _SONDA_ATTESA_LOCK_S
    while True:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            preso = True
            break
        except (BlockingIOError, OSError):
            if time.monotonic() >= scadenza:
                break
            time.sleep(3)
    if not preso:
        # Un'altra sonda e' in corso da piu' di _SONDA_ATTESA_LOCK_S: NON se ne
        # apre una seconda (era esattamente il guasto). Si torna il dato vecchio
        # DICHIARANDO la sua eta': chi decide sa che non e' fresco.
        fh.close()
        righe_cache, eta_cache = _cache_sonda_voce(engine)
        return {"righe": righe_cache, "eta_s": eta_cache, "da_cache": True,
                "ts": None if eta_cache is None else time.time() - eta_cache,
                "fonte": fonte + " (cache: altra sonda in corso)",
                "finestra": None,
                "errore": "sonda gia' in corso in un altro processo"}
    try:
        # Ricontrollo DOPO il lock: se ho aspettato la sonda di un altro, il suo
        # risultato e' gia' in cache ed e' fresco. Rifarla sarebbe puro spreco.
        righe_cache, eta_cache = _cache_sonda_voce(engine)
        if not forza and righe_cache is not None and eta_cache < SONDA_INTERVALLO_MIN_S:
            return {"righe": righe_cache, "eta_s": eta_cache, "da_cache": True,
                    "ts": time.time() - eta_cache,
                    "fonte": fonte + " (cache, popolata dalla sonda appena conclusa)",
                    "finestra": None, "errore": None}
        win = _finestra_sonda_univoca()
        try:
            righe = _sonda_quota_reale(engine, win, attesa=attesa, timeout=timeout)
        except Exception as e:
            righe_cache, eta_cache = _cache_sonda_voce(engine)
            return {"righe": righe_cache, "eta_s": eta_cache, "da_cache": True,
                    "ts": None if eta_cache is None else time.time() - eta_cache,
                    "fonte": fonte + " (cache: sonda fallita)", "finestra": win,
                    "errore": f"{type(e).__name__}: {e}"}
        _cache_sonda_salva(engine, righe)
        return {"righe": righe, "eta_s": 0.0, "da_cache": False,
                "ts": time.time(), "fonte": fonte, "finestra": win, "errore": None}
    finally:
        try:
            fcntl.flock(fh, fcntl.LOCK_UN)
        finally:
            fh.close()


def sonda_quota(engine, attesa=26, timeout=SONDA_TIMEOUT, forza=False):
    """Righe utili di /usage per il motore. FIRMA COMPATIBILE con i sette
    chiamanti storici: continua a tornare una lista di stringhe.

    Da 21/08/2026 passa da sonda_quota_dettaglio(): flock (una sonda per volta
    su tutta la macchina), finestra tmux univoca, cache condivisa con eta'
    (max 1 sonda reale/ora). Chi deve DECIDERE usi sonda_quota_dettaglio(), che
    dichiara anche quanto e' vecchio il numero: una lista nuda non sa dire se ha
    dieci secondi o sei ore, e un dato vecchio non puo' giustificare un failover.
    Ritorna [] se non c'e' nessun dato (mai None: i chiamanti iterano)."""
    d = sonda_quota_dettaglio(engine, attesa=attesa, timeout=timeout, forza=forza)
    if d.get("errore") and not d.get("righe"):
        raise RuntimeError(d["errore"])
    return d.get("righe") or []


def _sonda_quota_reale(engine, win, attesa=26, timeout=SONDA_TIMEOUT):
    """La sonda VERA. `win` e\' passata dal chiamante ed e\' univoca: prima era la
    globale del modulo, ed e\' per questo che due sonde in corsa si uccidevano la
    finestra a vicenda.

    Per claude: attesa ADATTIVA su is_pane_ready (boot lento sotto carico
    incluso), dialog di avvio gestiti al volo. Per gli altri engine: percorso
    storico a tempi fissi, ma tutto entro il margine `timeout` (default 240s):
    oltre si alza TimeoutError, mai un'attesa infinita. La kill-window e' in
    `finally`: la finestra viene chiusa anche su eccezione.
    """
    import time as _t
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    try:
        import engine_adapter
    except Exception:
        return ["(engine_adapter non importabile)"]
    _tmux(["kill-window", "-t", win])
    _t.sleep(1)
    _tmux(["new-window", "-d", "-t", win.split(":")[0],
           "-n", win.split(":")[1]])
    _tmux(["send-keys", "-t", win,
           f"cd /tmp && {_CLI[engine]}", "C-m"])
    deadline = _t.monotonic() + timeout
    try:
        if engine == "claude":
            # Attesa ADATTIVA: il CLI e' pronto quando is_pane_ready lo dice
            # (segnali testuali reali della TUI), non dopo N secondi fissi.
            # I dialog one-shot che compaiono durante il boot vengono gestiti
            # al volo (una sola volta ciascuno, niente doppi input sul prompt).
            gestiti = set()
            while not engine_adapter.is_pane_ready(engine, _pane(win)):
                if _t.monotonic() >= deadline:
                    raise TimeoutError(
                        f"sonda {engine}: CLI non pronto entro {timeout}s")
                k = engine_adapter.detect_startup_dialog(engine, _pane(win))
                if k and k not in gestiti:
                    engine_adapter.send_via_tmux_buffer(win, k)
                    gestiti.add(k)
                _t.sleep(2)
        else:
            # percorso storico (qwen/agy/...): tempo fisso + dialog, ma se il
            # CLI non risponde entro il margine si esce con errore esplicito.
            _t.sleep(attesa)
            for _ in range(3):
                k = engine_adapter.detect_startup_dialog(engine, _pane(win))
                if not k:
                    break
                engine_adapter.send_via_tmux_buffer(win, k)
                _t.sleep(6)
            if _t.monotonic() >= deadline:
                raise TimeoutError(
                    f"sonda {engine}: CLI non pronto entro {timeout}s")
        if engine == "agy":
            _tmux(["send-keys", "-t", win, "C-m"])
            _t.sleep(8)
        engine_adapter.send_via_tmux_buffer(win, "/usage")
        _t.sleep(16)
        testo = _pane(win)
    finally:
        # SEMPRE chiusa, anche su eccezione: niente finestra usa-e-getta zombie.
        try:
            _tmux(["kill-window", "-t", win])
        except Exception:
            pass
    return _estrai_quota(engine, testo)


def _estrai_quota(engine, testo):
    righe = [r.strip() for r in testo.splitlines() if r.strip()]
    out = []
    if engine == "claude":
        for i, r in enumerate(righe):
            if "% used" in r:
                eti = righe[i - 1] if i else ""
                pct = r.split("%")[0].split()[-1] + "%"
                reset = ""
                for j in (i + 1, i + 2):
                    if j < len(righe) and "Resets" in righe[j]:
                        reset = " — " + righe[j]
                        break
                out.append(f"{eti}: {pct} usata{reset}")
    elif engine == "agy":
        gruppo = ""
        for i, r in enumerate(righe):
            if "MODELS" in r.upper() and "Models within" not in r:
                gruppo = r
            if "%" in r and "[" in r:
                pct = r.split("]")[-1].strip()
                nota = righe[i + 1] if i + 1 < len(righe) else ""
                tipo = ""
                for j in (i - 1, i - 2):
                    if j >= 0 and "Limit" in righe[j]:
                        tipo = righe[j]
                        break
                out.append(f"{gruppo} / {tipo}: {pct} DISPONIBILE — {nota[:48]}")
    elif engine == "qwen":
        out.append("nessuna quota esposta: /usage mostra solo le statistiche di sessione")
    return out or ["(nessun dato leggibile dal pannello)"]


def consumo_claude(giorni=1, per_conv_id=False):
    """Somma i token dai transcript di Claude Code, raggruppati per giorno.

    per_conv_id=False (default, comportamento invariato per i chiamanti
    esistenti): ritorna (per_giorno, per_progetto).

    per_conv_id=True (DEV consumo-per-coordinatore, 08/08/2026, esteso dopo
    verifica dati di Marco + scoperta costo di Betty_Ottimizzazione): ritorna
    anche un terzo dict {giorno: {conv_id: {...}}} con per conv_id:
      - "totale": somma grezza input+output+cache_creation (== vecchia
        metrica "fatturabile", cache_read ESCLUSA — tenuta solo per
        continuita'/debug, NON e' la metrica giusta per un allarme: cache_read
        e' l'80-87% del costo reale sui coordinatori principali, ignorarla
        sottostima il costo di ~8,9x E classifica male chi consuma di piu'
        — Betty_Ottimizzazione + Marco, 08/08, verificato sui dati reali)
      - "input"/"output"/"cache_read"/"cache_creazione": le QUATTRO voci
        grezze separate, non un costo gia' pesato — deliberato (Marco,
        08/08): i moltiplicatori dipendono dal MODELLO (Opus/Sonnet/Haiku
        prezzi diversi) e dal TTL della cache (5min vs 1h), un totale gia'
        fuso qui perderebbe quell'informazione senza modo di recuperarla.
        Chi consuma questo dato applica i pesi giusti per il proprio caso;
        pesi di riferimento verificati sul pricing Anthropic reale (Opus 5):
        input x1.0, cache_creazione x1.25 (TTL 5min, x2.0 se TTL 1h),
        cache_read x0.1, output x5.0 — NON e' spreco da ridurre, e' il
        costo fisiologico del prompt caching (senza cache costerebbe 10x).
      - "per_ora": stesse quattro voci spezzate per ora locale (HH), utile
        per baseline "ora corrente vs ore precedenti dello stesso giorno"
      - "folder": cartella/progetto del transcript
    Tutto raccolto nella STESSA passata di scansione (nessuna doppia
    lettura dei transcript — verificato da Marco che i file sono ~23 e la
    scansione costa 7s, non va raddoppiata). Il conv_id e' il nome del file
    .jsonl senza estensione, coincide col conv_id registrato in Postgres
    (agents.data->>'conv_id') — ma ATTENZIONE, Postgres tiene solo il
    conv_id CORRENTE: un conv_id di oggi puo' non essere piu' quello
    "vivo" per un agente che ha avuto un riavvio in giornata (verificato
    da Marco su Redattore: 3,4M token oggi su un conv_id precedente dello
    stesso giorno, non piu' in Postgres) — chi consuma questo dato deve
    accumulare la propria mappa conv_id->slug nel tempo, non fare solo un
    lookup Postgres istantaneo (vedi token_probe.py::scan_consumo_coordinatori
    per l'implementazione)."""
    limite = datetime.now(timezone.utc) - timedelta(days=giorni)
    per_giorno = defaultdict(lambda: defaultdict(int))
    per_progetto = defaultdict(int)
    _vuoto_ora = lambda: {"input": 0, "output": 0, "cache_read": 0, "cache_creazione": 0}
    per_giorno_conv = defaultdict(lambda: defaultdict(lambda: {
        "totale": 0, "input": 0, "output": 0, "cache_read": 0, "cache_creazione": 0,
        "per_ora": defaultdict(_vuoto_ora), "folder": ""
    }))
    if not os.path.isdir(CLAUDE_PROJECTS):
        return (per_giorno, per_progetto, per_giorno_conv) if per_conv_id else (per_giorno, per_progetto)
    for prog in os.listdir(CLAUDE_PROJECTS):
        d = os.path.join(CLAUDE_PROJECTS, prog)
        if not os.path.isdir(d):
            continue
        for fn in os.listdir(d):
            if not fn.endswith(".jsonl"):
                continue
            p = os.path.join(d, fn)
            conv_id = fn[:-len(".jsonl")]
            try:
                if datetime.fromtimestamp(os.path.getmtime(p), timezone.utc) < limite:
                    continue
            except OSError:
                continue
            try:
                with open(p, encoding="utf-8", errors="ignore") as f:
                    for line in f:
                        try:
                            rec = json.loads(line)
                        except Exception:
                            continue
                        u = (rec.get("message") or {}).get("usage") or rec.get("usage")
                        if not isinstance(u, dict):
                            continue
                        ts = rec.get("timestamp")
                        ora_locale = None
                        try:
                            ts_locale = datetime.fromisoformat(str(ts).replace("Z", "+00:00")).astimezone()
                            g = ts_locale.strftime("%Y-%m-%d")
                            ora_locale = ts_locale.strftime("%H")
                        except Exception:
                            g = "?"
                        tot = (u.get("input_tokens", 0) + u.get("output_tokens", 0)
                               + u.get("cache_creation_input_tokens", 0))
                        per_giorno[g]["input"] += u.get("input_tokens", 0)
                        per_giorno[g]["output"] += u.get("output_tokens", 0)
                        per_giorno[g]["cache_read"] += u.get("cache_read_input_tokens", 0)
                        per_giorno[g]["cache_creazione"] += u.get("cache_creation_input_tokens", 0)
                        per_giorno[g]["fatturabile"] += tot
                        per_progetto[prog] += tot
                        if per_conv_id:
                            in_t = u.get("input_tokens", 0)
                            out_t = u.get("output_tokens", 0)
                            cr_t = u.get("cache_read_input_tokens", 0)
                            cc_t = u.get("cache_creation_input_tokens", 0)
                            voce = per_giorno_conv[g][conv_id]
                            voce["totale"] += tot
                            voce["input"] += in_t
                            voce["output"] += out_t
                            voce["cache_read"] += cr_t
                            voce["cache_creazione"] += cc_t
                            voce["folder"] = prog
                            if ora_locale:
                                vo = voce["per_ora"][ora_locale]
                                vo["input"] += in_t
                                vo["output"] += out_t
                                vo["cache_read"] += cr_t
                                vo["cache_creazione"] += cc_t
            except OSError:
                continue
    return (per_giorno, per_progetto, per_giorno_conv) if per_conv_id else (per_giorno, per_progetto)


def consumo_qwen(giorni=1):
    """Legge il registro nativo di qwen (il piu' affidabile dei tre)."""
    per_giorno = defaultdict(lambda: defaultdict(int))
    per_modello = defaultdict(int)
    if not os.path.isdir(QWEN_USAGE_DIR):
        return per_giorno, per_modello
    giorni_ok = {(datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(giorni)}
    for fn in os.listdir(QWEN_USAGE_DIR):
        if not fn.startswith("token-usage-"):
            continue
        try:
            with open(os.path.join(QWEN_USAGE_DIR, fn), encoding="utf-8", errors="ignore") as f:
                for line in f:
                    try:
                        r = json.loads(line)
                    except Exception:
                        continue
                    g = r.get("localDate", "?")
                    if g not in giorni_ok:
                        continue
                    per_giorno[g]["input"] += r.get("inputTokens", 0)
                    per_giorno[g]["output"] += r.get("outputTokens", 0)
                    per_giorno[g]["cache_read"] += r.get("cachedTokens", 0)
                    per_giorno[g]["fatturabile"] += r.get("totalTokens", 0)
                    per_giorno[g]["chiamate"] += 1
                    per_modello[r.get("model", "?")] += r.get("totalTokens", 0)
        except OSError:
            continue
    return per_giorno, per_modello


def consumo_deepcode(ore=None):
    """Consumo reale di Deep Code (gap segnalato da Betty/Leo, 10/08/2026,
    stessa classe gia' chiusa su rinfrescami.py/MOTORI e mcp_arturo.py/
    ask_agent). deepcode-cli non ha ne' un '/usage' interattivo (sonda_quota
    non lo copre, vedi _CLI sopra) ne' un registro proprio per-chiamata come
    qwen: l'unico dato reale disponibile e' lo stato del proxy locale
    deepseek_spend_guard.py, che TUTTI gli agenti su questo motore
    condividono. Riuso diretto (import, non duplicazione) di MAX_CHIAMATE e
    _leggi_stato(): unica fonte di verita' resta li'.

    ⚠️ Limite strutturale, dichiarato non nascosto: il log del proxy non
    porta un conv_id (e' un reverse-proxy, non sa chi lo sta chiamando),
    quindi qui si puo' dare solo l'AGGREGATO di tutti gli agenti deepcode
    insieme — mai un numero per singolo agente (per quello vedi
    consumo_ultime_ore(), che dichiara esplicitamente 'non disponibile' per
    questo stesso motivo invece di inventare un'attribuzione che il dato non
    permette)."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    try:
        import deepseek_spend_guard as _guard
    except Exception as e:
        return {"disponibile": False, "motivo": f"deepseek_spend_guard.py non importabile: {e}"}
    stato = _guard._leggi_stato()
    out = {
        "disponibile": True,
        "chiamate": stato.get("chiamate", 0),
        "tetto": _guard.MAX_CHIAMATE,
        "spesa_stimata_usd": stato.get("spesa_stimata_usd", 0.0),
    }
    if ore is not None and os.path.exists(_guard.LOG_PATH):
        limite = datetime.now() - timedelta(hours=ore)
        chiamate_finestra = 0
        try:
            with open(_guard.LOG_PATH, encoding="utf-8", errors="ignore") as f:
                for riga in f:
                    if "INOLTRATO" not in riga:
                        continue
                    try:
                        ts = datetime.strptime(riga[:19], "%Y-%m-%dT%H:%M:%S")
                    except Exception:
                        continue
                    if ts >= limite:
                        chiamate_finestra += 1
        except OSError:
            pass
        out["chiamate_ultime_ore"] = chiamate_finestra
        out["ore"] = ore
    return out


def consumo_ultime_ore(engine, conv_id, ore=5):
    """Consumo dello specifico conv_id nelle ultime `ore` ore reali (non
    'oggi', una finestra mobile) — richiesta di Betty_Ottimizzazione
    (08/08/2026) per il log degli eventi 429 reali di dynamic_engine_router.py:
    con una manciata di eventi veri, sapere quanto l'agente aveva consumato
    nelle ore APPENA PRIMA di un 429 fa emergere il limite pratico reale del
    piano, senza dover riconciliare numeri esterni.

    Ritorna {"disponibile": True, "input":.., "output":.., "cache_read":..,
    "cache_creazione":..} oppure {"disponibile": False, "motivo": "..."} —
    MAI un numero finto quando il dato non esiste davvero, per non far
    sembrare misurato cio' che non lo e' (vedi modulo: 'agy: non misurabile'
    e' gia' un fatto noto qui dentro, non un limite di questa funzione).

    - claude: reale, via consumo_claude(per_conv_id=True) + breakdown
      'per_ora' gia' presente per quel conv_id — somma le ultime `ore` ore
      reali (gestisce il giro a cavallo della mezzanotte leggendo anche
      ieri se serve), riusando la scansione esistente invece di inventare
      un secondo meccanismo di tracking.
    - qwen: consumo_qwen() non ha ne' per-conv_id ne' breakdown orario (solo
      per-giorno aggregato su TUTTI gli agenti) — non disponibile a questo
      livello di granularita', dichiarato esplicitamente invece di dare un
      numero aggregato che sembrerebbe di quell'agente e non lo sarebbe.
    - agy: non traccia token da nessuna parte (fatto gia' documentato in
      testa al modulo), non misurabile per costruzione.
    """
    if engine == "agy":
        return {"disponibile": False, "motivo": "agy non traccia token da nessuna parte (limite tecnico noto)"}
    if engine == "qwen":
        return {"disponibile": False, "motivo": "consumo_qwen() non ha breakdown per-conv_id ne' per-ora"}
    if engine == "deepcode":
        return {"disponibile": False, "motivo": "il proxy locale (deepseek_spend_guard) e' condiviso da tutti "
                "gli agenti su questo motore, nessun conv_id nel log — vedi consumo_deepcode(ore=...) per "
                "l'aggregato di tutti insieme"}
    if engine != "claude":
        return {"disponibile": False, "motivo": f"motore '{engine}' sconosciuto"}

    ora = datetime.now().astimezone()
    limite = ora - timedelta(hours=ore)
    # giorni=2 copre anche ieri se la finestra di 'ore' attraversa la
    # mezzanotte (es. evento alle 01:30, finestra che parte dalle 20:30 di ieri)
    _, _, per_giorno_conv = consumo_claude(giorni=2, per_conv_id=True)

    tot = {"input": 0, "output": 0, "cache_read": 0, "cache_creazione": 0}
    trovato = False
    for g, per_conv in per_giorno_conv.items():
        voce = per_conv.get(conv_id)
        if not voce:
            continue
        for hh_str, vo in voce.get("per_ora", {}).items():
            try:
                hh = int(hh_str)
                # Ricostruisce un timestamp locale approssimato per quell'ora
                # nel giorno g, solo per confrontarlo con la finestra 'limite'
                # — sufficiente per un log diagnostico, non serve al minuto.
                ts_ora = datetime.strptime(f"{g} {hh:02d}", "%Y-%m-%d %H").astimezone()
            except Exception:
                continue
            if ts_ora >= limite.replace(minute=0, second=0, microsecond=0):
                trovato = True
                tot["input"] += vo.get("input", 0)
                tot["output"] += vo.get("output", 0)
                tot["cache_read"] += vo.get("cache_read", 0)
                tot["cache_creazione"] += vo.get("cache_creazione", 0)

    if not trovato:
        return {"disponibile": False, "motivo": f"nessun transcript trovato per conv_id={conv_id} nelle ultime {ore}h"}
    tot["disponibile"] = True
    return tot


def agenti_vivi():
    """Quanti agenti stanno girando adesso, per motore (da Postgres + tmux)."""
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import agent_db
        import engine_adapter
        db = agent_db.get_db()
    except Exception:
        return {}, []
    out = subprocess.run(["tmux", "list-windows", "-a", "-F", "#{session_name}:#{window_name}"],
                         capture_output=True, text=True).stdout.split()
    vive = {w.lower() for w in out}
    per_motore = defaultdict(list)
    for slug, ag in db.items():
        w = (ag.get("tmux_window") or "").lower()
        if w and w in vive:
            per_motore[engine_adapter.get_engine(ag)].append(slug)
    return per_motore, out


def risorse():
    m = subprocess.run(["free", "-m"], capture_output=True, text=True).stdout
    disp = tot = 0
    for r in m.splitlines():
        if r.startswith("Mem:"):
            p = r.split()
            tot, disp = int(p[1]), int(p[6])
    with open("/proc/loadavg") as f:
        load = float(f.read().split()[0])
    core = os.cpu_count() or 1
    return disp, tot, load, core


def _fmt(n):
    return f"{n:,}".replace(",", ".")


def main():
    giorni = 1
    if "--giorni" in sys.argv:
        try:
            giorni = int(sys.argv[sys.argv.index("--giorni") + 1])
        except Exception:
            pass

    cl_g, cl_p = consumo_claude(giorni)
    qw_g, qw_m = consumo_qwen(giorni)
    per_motore, finestre = agenti_vivi()
    disp, tot, load, core = risorse()
    oggi = _oggi_locale()

    print("=" * 66)
    print(f" CONSUMO MOTORI — {datetime.now().strftime('%d/%m/%Y %H:%M')} (ultimi {giorni}g)")
    print("=" * 66)

    print("\n🔵 CLAUDE (piano: %s)" % _piano_claude())
    if cl_g:
        for g in sorted(cl_g):
            d = cl_g[g]
            marca = " ← oggi" if g == oggi else ""
            print(f"  {g}: fatturabile {_fmt(d['fatturabile']):>12}  "
                  f"(out {_fmt(d['output'])}, cache letta {_fmt(d['cache_read'])}){marca}")
    else:
        print("  nessun consumo registrato nel periodo")
    if cl_p:
        print("  per progetto:")
        for p, v in sorted(cl_p.items(), key=lambda x: -x[1])[:5]:
            print(f"    {p[:44]:44} {_fmt(v):>12}")

    print("\n🟣 QWEN")
    if qw_g:
        for g in sorted(qw_g):
            d = qw_g[g]
            marca = " ← oggi" if g == oggi else ""
            print(f"  {g}: totale {_fmt(d['fatturabile']):>12}  "
                  f"(out {_fmt(d['output'])}, cache {_fmt(d['cache_read'])}, "
                  f"{d['chiamate']} chiamate){marca}")
        for mo, v in sorted(qw_m.items(), key=lambda x: -x[1])[:4]:
            print(f"    {mo:22} {_fmt(v):>12}")
    else:
        print("  nessun consumo registrato nel periodo")

    print("\n🟠 ANTIGRAVITY (agy)")
    print("  consumo NON misurabile: il CLI non registra i token da nessuna parte.")
    print("  (history.jsonl contiene solo i prompt, le conversazioni sono .db opachi)")

    print("\n🟢 DEEP CODE")
    dc = consumo_deepcode(ore=24)
    if dc.get("disponibile"):
        print(f"  chiamate (da sempre/ultimo reset): {_fmt(dc['chiamate'])}/{_fmt(dc['tetto'])}  "
              f"(~${dc['spesa_stimata_usd']:.3f} stimati)")
        if "chiamate_ultime_ore" in dc:
            print(f"  ultime {dc['ore']}h: {_fmt(dc['chiamate_ultime_ore'])} chiamate "
                  f"(aggregato di TUTTI gli agenti deepcode insieme, il proxy non traccia per-agente)")
    else:
        print(f"  non misurabile: {dc.get('motivo')}")

    print("\n" + "-" * 66)
    print(" AGENTI VIVI ADESSO")
    print("-" * 66)
    if per_motore:
        for eng, lista in sorted(per_motore.items()):
            print(f"  {eng:12} {len(lista):>2} → {', '.join(sorted(lista)[:8])}"
                  + (" …" if len(lista) > 8 else ""))
    else:
        print("  (nessuno rilevato)")
    print(f"  finestre tmux totali: {len(finestre)}")

    print("\n" + "-" * 66)
    print(" MACCHINA")
    print("-" * 66)
    print(f"  RAM disponibile: {disp} MB su {tot} MB   |   load {load:.2f} su {core} core")

    print("\n" + "=" * 66)
    print(" COSA CONVIENE LANCIARE ADESSO")
    print("=" * 66)
    for riga in raccomanda(disp, load, core, per_motore, cl_g.get(oggi, {}), qw_g.get(oggi, {})):
        print("  " + riga)
    print()


def _piano_claude():
    try:
        with open(CREDENZIALI_CLAUDE) as f:
            return json.load(f).get("claudeAiOauth", {}).get("subscriptionType", "?")
    except Exception:
        return "?"


def raccomanda(disp, load, core, per_motore, cl_oggi, qw_oggi):
    """Regole esplicite: meglio un criterio leggibile che un punteggio opaco."""
    r = []
    # 1) La macchina prima di tutto: se non c'e' RAM non conta quale motore scegli.
    if disp < 1200:
        r.append("⛔ RAM sotto 1,2 GB: NON lanciare nulla. Prima spegni un agente non urgente.")
    elif disp < 2500:
        r.append("⚠️  RAM fra 1,2 e 2,5 GB: al massimo UN agente, e solo se serve davvero.")
    else:
        r.append(f"✅ RAM ok ({disp} MB): c'e' spazio per nuovi agenti.")
    if load > core * 2:
        r.append(f"⚠️  Load {load:.1f} alto per {core} core: aspetta che scenda prima di aggiungere carico.")

    # 2) Quale motore: si guarda il consumo di OGGI, non un residuo che nessuno espone.
    cl = cl_oggi.get("fatturabile", 0)
    qw = qw_oggi.get("fatturabile", 0)
    r.append(f"Consumo di oggi — claude: {_fmt(cl)} | qwen: {_fmt(qw)} | agy: non misurabile")
    if cl > qw * 1.5 and cl > 500_000:
        r.append("→ Oggi CLAUDE e' il piu' spremuto: per un agente nuovo non critico preferisci QWEN o AGY.")
    elif qw > cl * 1.5 and qw > 500_000:
        r.append("→ Oggi QWEN e' il piu' spremuto: per un agente nuovo preferisci CLAUDE.")
    else:
        r.append("→ Consumo equilibrato: scegli in base al compito, non al budget.")

    # 3) Il criterio che conta piu' del budget.
    r.append("Regola di merito: CLAUDE per chi deve usare tool MCP in modo affidabile "
             "(qwen perde dal registry i tool gia' usati), AGY/QWEN per lavoro "
             "conversazionale o di lettura dove i tool contano meno.")
    n_claude = len(per_motore.get("claude", []))
    if n_claude >= 12:
        r.append(f"⚠️  Ci sono gia' {n_claude} agenti su claude: distribuisci i prossimi su altri motori.")
    return r


if __name__ == "__main__":
    main()
