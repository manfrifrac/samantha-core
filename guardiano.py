#!/usr/bin/env python3
"""
guardiano.py — SELF-HEALING UNIFICATO dell'ecosistema (quota/token · RAM · CPU).

NATO IL 21/08/2026 (mandato samantha_2, ordine di Manfredo MSG 13952/13956/13959/13962),
dalla specifica /tmp/betty_docs/self_healing_unificato_proposta.md.

PERCHE' ESISTE
--------------
Otto guardiani, otto loop, nessuno che parla con l'altro. La sera del 21/08 cinque
agenti erano fermi da 40 minuti per quota Gemini esaurita: nessun failover, nessun
avviso a Manfredo, e i retry loop di quegli stessi agenti mandavano il load a 30-50 —
il resource_watchdog allertava via A2A la Samantha che era ferma per la stessa quota.
Due sensori, due sintomi, nessuno che li correlava, e l'allerta indirizzata a un
AGENTE invece che a un CANALE. L'ha scoperto Manfredo a occhio.

Qui i tre sensori girano nello stesso ciclo e scrivono nello stesso registro: la
correlazione quota->CPU di quella sera sarebbe stata UNA riga sola.

COSA FA (F1 osservazione + F2 azioni quota + F3 azioni RAM/CPU)
--------------------------------------------------------------
Ogni 60s:
  1. SENSORE QUOTA  — per ogni finestra agente viva: motore e modello REALI letti dal
     PROCESSO nel sottoalbero del pannello (MAI dal DB: il 21/08 il DB diceva
     deepseek/claude per 6 agenti che giravano su agy/Gemini). Pattern d'errore per
     motore nel capture-pane. Saldo DeepSeek dallo spend_guard. Quota Claude/agy dalla
     sonda /usage (max 1 volta/ora: costa una finestra tmux e fino a 240s).
  2. SENSORE RAM    — disponibile, swap, PSI memory, peso per sessione tmux.
  3. SENSORE CPU    — load5, PSI cpu, CPU% REALE per processo CLI (delta di jiffies fra
     due cicli, non la media a vita di `ps %cpu` che su un demone vecchio dice sempre ~0).
  4. Riallineo DB<->realta', decisioni dalla tabella in guardiano.conf.json, azioni,
     alert su canale (ROSSO=Telegram a Manfredo, GIALLO=A2A al coordinatore).

Tutto finisce in:
  - /tmp/betty_docs/guardiano_stato.json          (fotografia dell'ultimo ciclo)
  - /root/ecosistema_agenti/logs/guardiano_ledger.jsonl  (append-only: misure oltre
    soglia e OGNI azione, con chi/quando/perche'/valori)
  - /tmp/betty_docs/guardiano_heartbeat            (mtime = ultimo ciclo concluso)

USO
    ./venv/bin/python3 guardiano.py               # demone (servizio SRV-Guardiano)
    ./venv/bin/python3 guardiano.py --once        # un ciclo solo, azioni ATTIVE
    ./venv/bin/python3 guardiano.py --once --dry  # un ciclo solo, NESSUNA azione ne' alert
"""
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta

CORE = os.path.dirname(os.path.abspath(__file__))
if CORE not in sys.path:
    sys.path.insert(0, CORE)

import tmux_esatto
import engine_adapter

CONF_PATH = os.path.join(CORE, "guardiano.conf.json")
LOCK_PATH = os.path.join(CORE, "guardiano.lock")
STATO_PATH = "/tmp/betty_docs/guardiano_stato.json"
HEARTBEAT_PATH = "/tmp/betty_docs/guardiano_heartbeat"
LEDGER_PATH = "/root/ecosistema_agenti/logs/guardiano_ledger.jsonl"
DEDUP_PATH = os.path.join(CORE, ".guardiano_dedup.json")
PERSIST_PATH = os.path.join(CORE, ".guardiano_persistenza.json")

# RUOLO ROUTER (22/08/2026, direttiva Manfredo): log dedicato, una riga per
# decisione, in SOLA OSSERVAZIONE. La mappa task→sorgente/modello la crea
# l'altro exec (fronte B, exec_abbon_routing) in routing_regole.json; questo
# file ne ha una bozza di riserva se il file manca.
ROUTER_LOG_PATH = "/tmp/self_healing_router.log"
ROUTING_REGOLE_PATH = os.path.join(CORE, "routing_regole.json")

# AZIONI STORE (exec_azioni_guardiano, 22/08/2026) — mandato samantha_1 PARTE 1
# STEP 2. Il ciclo legge provider_stato + agenti_runtime dallo store Postgres e
# decide tre tipi di azione autonome (routing_token, avvio, rinfresco), ognuna
# loggata in azioni_guardiano (ts/azione/dettaglio JSONB/esito). Chiavi API MAI
# in chiaro. Nessuna email/pagamento/ricarica. Soglie dal mandato + routing_regole.
STORE_DSN = "dbname=betty_db user=betty_user password=betty_password host=localhost port=5432"
STORE_A2A_ROOT = "/root/ecosistema_agenti/a2a"
STORE_OBSIDIAN_PROGETTI = "/root/Obsidian-Vault/Progetti"
STORE_SOGLIA_CONTESTO = 200000        # token oltre cui scatta il rinfresco (mandato)
STORE_SALDO_DS_CRITICO = 1.0          # USD sotto cui DeepSeek e' escluso dal routing
STORE_DOWNGRADE_PCT = 80.0            # soglia_downgrade_pct_usata (routing_regole.json)
STORE_DATO_MAX_ETA_S = 3600           # store piu' vecchio di 1h: non giustifica azioni
STORE_STATO_FILE_MAX_ETA_S = 1800     # salva-stato "fresco" = scritto negli ultimi 30 min
STORE_RINFRESCO_MIN_INTERVALLO_S = 7200  # non rinfrescare lo stesso agente piu' di ogni 2h
STORE_LOG_INTERVALLO_S = 1800         # resoconto "tutto ok" per tipo non piu' di ogni 30 min
RINFRESCHI_STATO_PATH = os.path.join(CORE, ".guardiano_rinfreschi.json")
STORE_LOG_PATH = os.path.join(CORE, ".guardiano_store_log.json")

_lock_fh = None

# ---------------------------------------------------------------------------
# CONFIG + LEDGER + STATO
# ---------------------------------------------------------------------------

def carica_conf(path=CONF_PATH):
    """Config ricaricata AD OGNI CICLO: cambiare una soglia ha effetto entro 60s
    senza riavviare il servizio (e senza perdere lo stato di persistenza)."""
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def batti(nota=""):
    """Heartbeat scritto DURANTE il ciclo, non solo alla fine.

    21/08/2026: il guardiano si e' auto-diagnosticato appeso ('heartbeat non
    aggiornato da 356s') mentre stava semplicemente facendo un ciclo lungo. Un
    guardiano che grida al lupo su se stesso e' il modo piu' rapido per far
    smettere di credergli. L'heartbeat deve dire 'sono vivo', non 'ho finito'."""
    try:
        with open(HEARTBEAT_PATH, "w") as f:
            f.write(f"{datetime.now().astimezone().isoformat(timespec='seconds')} {nota}")
    except Exception:
        pass


def ora_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def ledger(evento, **campi):
    """Append-only. Una riga per misura oltre soglia e per OGNI azione.
    Non solleva mai: un registro che rompe il guardiano e' peggio di niente."""
    riga = {"ts": ora_iso(), "evento": evento}
    riga.update(campi)
    try:
        os.makedirs(os.path.dirname(LEDGER_PATH), exist_ok=True)
        with open(LEDGER_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(riga, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"[guardiano] ledger non scrivibile: {e}", flush=True)
    _log_da_ledger(evento, campi)          # F-A: stessa riga, in forma leggibile
    return riga


# ---------------------------------------------------------------------------
# LOG TESTUALE /tmp/guardiano.log  (F-A, 22/08/2026, exec_fa_guardiano)
#
# Il ledger JSONL resta per le macchine. Questo e' per le persone: una riga per
# evento, formato fisso a 9 campi separati da '|':
#     ts|modulo|livello|azione|oggetto|valore|fonte|eta_s|firma
# Debug previsto: tail -f /tmp/guardiano.log | grep '|ROSSO|'
#
# Tre regole (piano di revisione §4.4):
#  - NESSUN ALLARME SENZA CHIUSURA: ogni 'alert' ha, nello stesso file, un
#    'chiuso' (la condizione non si presenta piu') o un 'dedup' (ripetizione
#    soppressa dal cooldown, con contatore). Vedi allerta() e
#    chiudi_alert_rientrati().
#  - il rumore di stato ('decisione_saltata': 6.244 righe su 16.792 nel ledger
#    del 21/08) NON finisce riga per riga: si aggrega in UNA riga DEBUG per
#    ciclo, scritta subito prima della riga 'ciclo'.
#  - non solleva mai: un log che rompe il guardiano e' peggio di nessun log.
# ---------------------------------------------------------------------------

LOG_TESTO_PATH = "/tmp/guardiano.log"
APERTI_PATH = os.path.join(CORE, ".guardiano_alert_aperti.json")

# Contatori del ciclo corrente: 'decisione_saltata' per motivo (svuotato a ogni
# riga 'ciclo') e insieme delle chiavi di alert VISTE nel ciclo (svuotato da
# chiudi_alert_rientrati).
_SALTATE_CICLO = {}
_ALERT_ATTIVI_CICLO = set()
_APERTI = None            # registro persistente degli alert aperti (lazy)

# Modulo (= sensore) di appartenenza per azione decisa e per evento di ledger.
MODULO_PER_AZIONE = {
    "segnala_servizio_giu": "servizi", "segnala_servizio_runaway": "servizi",
    "segnala_servizio_muto": "servizi",
    "ticket_meta_guardiano": "guardiani",
    "ordine_fermo_in_casella": "ordini_fermi",
    "failover": "quota", "stop_retry_loop": "quota", "attendi_reset": "quota",
    "spegni_per_ram": "ram", "sospendi_runaway": "cpu",
    "rilancia_cli_morto": "liveness",
    "segnala_consegna_ferma": "pool", "segnala_pool_fermo": "pool",
    "riallineo_ambiguo": "riallineo",
}
MODULO_PER_EVENTO = {
    "ciclo": "ciclo", "errore_ciclo": "ciclo",
    "meta_guardiani": "guardiani", "meta_guardiani_errore": "guardiani",
    "meta_ticket_rimandati": "guardiani",
    "ordini_fermi_errore": "ordini_fermi", "ordine_fermo_rilevato": "ordini_fermi",
    "sonda_usage_avviata": "quota", "sonda_usage_conclusa": "quota",
    "picco_cpu_atteso": "servizi", "servizio_atteso_spento": "servizi",
    "riallineo": "riallineo", "contesto": "contesto", "contesto_errore": "contesto",
    "alert": "alert",
}
LIVELLI = ("DEBUG", "INFO", "GIALLO", "ROSSO")


def _campo_log(v, max_len=200):
    """Un campo della riga: mai vuoto ('-'), mai con '|' o a capo dentro."""
    if v is None:
        return "-"
    if isinstance(v, (set, frozenset)):
        v = ",".join(str(x) for x in sorted(v))
    elif isinstance(v, (dict, list, tuple)):
        try:
            v = json.dumps(v, ensure_ascii=False, default=str)
        except Exception:
            v = str(v)
    elif isinstance(v, float):
        v = round(v, 1)
    t = " ".join(str(v).replace("|", "/").split())
    return t[:max_len] or "-"


def log_testo(modulo, livello, azione, oggetto=None, valore=None, fonte=None,
              eta_s=None, firma=None, ts=None, path=None):
    """Scrive UNA riga nel log testuale. Ritorna la riga (senza a capo)."""
    riga = "|".join(_campo_log(x) for x in (
        ts or ora_iso(), modulo or "guardiano", livello or "INFO", azione,
        oggetto or "sistema", valore, fonte, eta_s, firma))
    try:
        with open(path or LOG_TESTO_PATH, "a", encoding="utf-8") as f:
            f.write(riga + "\n")
    except Exception as e:
        print(f"[guardiano] log testuale non scrivibile: {e}", flush=True)
    return riga


def _modulo_da_chiave(chiave):
    """'bloccata:failover:agente' -> quota; 'meta:firma' -> guardiani."""
    c = (chiave or "")
    for pref in CHIAVI_ALERT_DI_STATO if "CHIAVI_ALERT_DI_STATO" in globals() else ():
        if c.startswith(pref):
            if pref == "meta:" or pref == "auto_anomalia:":
                return "guardiani"
            c = c[len(pref):]
            break
    azione = c.split(":", 1)[0]
    return MODULO_PER_AZIONE.get(azione, "guardiano")


def _riga_log_da_evento(evento, campi):
    """Mappa un evento del ledger sui 9 campi del log testuale.
    Ritorna una tupla (modulo, livello, azione, oggetto, valore, fonte, eta_s,
    firma) oppure None se l'evento NON va nel log riga per riga."""
    c = campi
    if evento == "decisione_saltata":
        return None                                   # aggregato (vedi ledger)
    modulo = (c.get("sensore") or c.get("modulo")
              or MODULO_PER_AZIONE.get(c.get("azione") or "")
              or MODULO_PER_EVENTO.get(evento) or "guardiano")
    livello = c.get("livello")
    # Il LIVELLO lo porta la riga 'alert' (inviata) o 'contesto'/'azione'.
    # Una decisione ristampata ad ogni ciclo mentre il suo alert e' in dedup
    # non e' un nuovo ROSSO: e' INFO (segnalata) o DEBUG (bloccata dal flag,
    # stato che dura per costruzione). Cosi' `grep '|ROSSO|'` mostra solo cio'
    # che e' successo davvero, non la stessa cosa sessanta volte l'ora.
    if evento == "decisione_segnalata":
        livello = "INFO"
    elif evento == "azione_bloccata_da_flag":
        livello = "DEBUG"
    elif livello not in LIVELLI:
        livello = "ROSSO" if "errore" in evento else "INFO"
    azione = c.get("azione") or evento
    if evento == "azione_bloccata_da_flag":
        azione = f"bloccata:{azione}"
    elif evento == "decisione_segnalata":
        azione = f"segnalata:{azione}"
    if evento not in ("decisione_segnalata", "azione", "azione_bloccata_da_flag"):
        azione = evento if evento in ("alert", "ciclo", "misura_oltre_soglia",
                                      "meta_guardiani", "picco_cpu_atteso",
                                      "ordine_fermo_rilevato") else azione
    oggetto = (c.get("oggetto") or c.get("agente") or c.get("servizio") or c.get("finestra")
               or c.get("tmux_window") or c.get("chiave") or c.get("metrica")
               or "sistema")
    valore = c.get("valore")
    if evento == "ciclo":
        azione = "ciclo"
        valore = (f"agenti={c.get('agenti_osservati')} decisioni={c.get('decisioni')} "
                  f"servizi_giu={c.get('servizi_giu')} cli_morti={c.get('cli_morti')} "
                  f"ram_mb={c.get('ram_mb')} load5={c.get('load5')} "
                  f"durata_s={c.get('durata_s')}")
    elif "errore" in evento:
        valore = c.get("errore")
    elif evento == "azione":
        valore = (f"esito={'ok' if c.get('esito_ok') else 'FALLITA'} "
                  f"{c.get('dettaglio') or ''}")
    elif evento == "alert":
        valore = (f"{(c.get('testo') or '').splitlines()[0] if c.get('testo') else ''} "
                  f"consegnato={c.get('consegnato')}")
    elif evento == "misura_oltre_soglia":
        valore = f"{c.get('valore')} (soglia {c.get('soglia')})" if c.get("soglia") is not None \
            else (c.get("problemi") or c.get("valore") or
                  f"vivo={c.get('vivo')} heartbeat_eta_s={c.get('heartbeat_eta_s')}")
    elif evento == "meta_guardiani":
        valore = (f"sorvegliati={len(c.get('guardiani') or [])} "
                  f"esaminati={c.get('allarmi_esaminati')} verdetti={c.get('verdetti')} "
                  f"rotti={c.get('rotti')} auto_anomalie={c.get('auto_anomalie')}")
    elif valore is None:
        valore = c.get("perche") or c.get("dettaglio") or c.get("motivo")
    fonte = c.get("fonte") or (c.get("canale") if evento == "alert" else None) \
        or (f"sensore {c.get('sensore')}" if c.get("sensore") else None) or "ledger"
    eta = c.get("eta_dato_s")
    if eta is None:
        eta = c.get("eta_s")
    if eta is None:
        eta = c.get("fermo_da_s")
    firma = c.get("firma") or c.get("chiave_dedup") or c.get("chiave")
    return (modulo, livello, azione, oggetto, valore, fonte, eta, firma)


def _svuota_saltate():
    """Riga DEBUG aggregata per le decisioni saltate del ciclo, poi azzera."""
    global _SALTATE_CICLO
    if _SALTATE_CICLO:
        tot = sum(_SALTATE_CICLO.values())
        dett = ";".join(f"{k}={v}" for k, v in sorted(_SALTATE_CICLO.items()))
        log_testo("quota", "DEBUG", "decisione_saltata", "aggregato",
                  f"n={tot} {dett}", "ledger decisione_saltata", None, None)
    _SALTATE_CICLO = {}


def _log_da_ledger(evento, campi):
    """Chiamato da ledger(): non solleva mai."""
    try:
        if evento == "decisione_saltata":
            m = campi.get("motivo") or "altro"
            _SALTATE_CICLO[m] = _SALTATE_CICLO.get(m, 0) + 1
            return
        if evento == "ciclo":
            _svuota_saltate()
        riga = _riga_log_da_evento(evento, campi)
        if riga:
            log_testo(*riga)
    except Exception as e:
        print(f"[guardiano] log testuale: mappatura fallita ({e})", flush=True)


def _carica_aperti():
    global _APERTI
    if _APERTI is None:
        _APERTI = _carica_json(APERTI_PATH, {})
    return _APERTI


def _registra_alert_aperto(composita, livello, chiave, ora=None):
    ap = _carica_aperti()
    ora = time.time() if ora is None else ora
    if composita not in ap:
        ap[composita] = {"ts": ora, "livello": livello, "chiave": chiave, "dedup": 0}
    return ap[composita]


def chiudi_alert_rientrati(ora=None, salva=True):
    """Fine ciclo: ogni alert aperto la cui condizione NON si e' ripresentata
    in questo ciclo viene chiuso con una riga 'chiuso' (durata e ripetizioni
    soppresse). E' la meta' mancante di ogni allarme: senza, chi legge il log
    non sa mai se il problema e' rientrato o se il guardiano ha smesso di
    guardarlo."""
    global _ALERT_ATTIVI_CICLO
    ora = time.time() if ora is None else ora
    ap = _carica_aperti()
    chiusi = []
    for comp in [k for k in ap if k not in _ALERT_ATTIVI_CICLO]:
        d = ap.pop(comp)
        liv, chiave = d.get("livello"), d.get("chiave") or comp
        log_testo(_modulo_da_chiave(chiave), "INFO", "chiuso", chiave,
                  f"rientrato dopo {round(ora - d.get('ts', ora))}s "
                  f"(livello {liv}, ripetizioni soppresse {d.get('dedup', 0)})",
                  "assenza della condizione nel ciclo corrente",
                  round(ora - d.get("ts", ora)), comp)
        chiusi.append(comp)
    _ALERT_ATTIVI_CICLO = set()
    if salva:
        _salva_json(APERTI_PATH, ap)
    return chiusi


def _carica_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return dict(default) if isinstance(default, dict) else default


def _salva_json(path, dati):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(dati, f, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception as e:
        print(f"[guardiano] scrittura {path} fallita: {e}", flush=True)


# ---------------------------------------------------------------------------
# LETTURA DEL MOTORE/MODELLO **REALE** DAL PROCESSO
#
# Il DB mente: il 21/08 diceva engine=deepseek/claude per 6 agenti che giravano
# su agy/Gemini, messi li' dal dynamic_engine_router senza riallineare il record.
# Ogni controllo basato sul DB e' falso finche' non si guarda il processo.
#
# Metodo (stesso di sentinella_contesto.scansiona_processi_claude): si guarda il
# SOTTOALBERO del pannello e si tiene il primo token del cmdline che finisce col
# nome del binario — mai il wrapper `bash -c`, che porta le assegnazioni di env
# INLINE nel comando e quindi ha un environ diverso da quello del CLI vero.
# ---------------------------------------------------------------------------

BINARI = {
    "claude": lambda t0: t0.endswith("claude"),
    "agy":    lambda t0: t0.endswith("agy"),
    "qwen":   lambda t0: t0.endswith("qwen"),
    # deepcode-cli e' uno script con shebang node: argv[0] e' "node", mai "deepcode".
    "deepcode": lambda t0: t0.endswith("node"),
}


def sottoalbero(pid):
    """Tutti i pid discendenti del pannello, a qualunque profondita'.
    (Stessa funzione di check_agenti_vivi.py: il CLI puo' essere il processo del
    pannello OPPURE un discendente dentro un wrapper.)"""
    try:
        righe = subprocess.run(["ps", "-eo", "pid,ppid"], capture_output=True,
                               text=True, timeout=20).stdout.splitlines()[1:]
    except Exception:
        return [pid]
    figli = {}
    for r in righe:
        p = r.split()
        if len(p) >= 2:
            try:
                figli.setdefault(int(p[1]), []).append(int(p[0]))
            except ValueError:
                continue
    visti, da_visitare = [pid], [pid]
    while da_visitare:
        for c in figli.get(da_visitare.pop(), []):
            if c not in visti:
                visti.append(c)
                da_visitare.append(c)
    return visti


def _cmdline(pid):
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return f.read().decode(errors="replace").replace("\0", " ").strip()
    except OSError:
        return ""


def _environ(pid):
    try:
        with open(f"/proc/{pid}/environ", "rb") as f:
            return f.read().decode(errors="replace")
    except OSError:
        return ""


def motore_reale_da_processo(pids):
    """(engine, model, provider, pid) letti dal PROCESSO, non dal DB.
    Ritorna (None, None, None, None) se nel sottoalbero non c'e' nessun CLI vivo —
    che e' un'informazione vera (agente morto in finestra viva), non un errore."""
    for pid in pids:
        cmd = _cmdline(pid)
        if not cmd:
            continue
        toks = cmd.split()
        t0 = toks[0]
        engine = None
        for nome, test in BINARI.items():
            if test(t0):
                engine = nome
                break
        if engine is None:
            continue
        if engine == "deepcode" and "deepcode" not in cmd:
            continue  # un 'node' qualunque non e' un CLI agente
        model = None
        for i, t in enumerate(toks[:-1]):
            if t in ("--model", "-m", "--model-id"):
                model = toks[i + 1]
                break
        if model is None:
            m = re.search(r"--model[= ]([\w.\-]+)", cmd)
            model = m.group(1) if m else None
        provider = None
        env = _environ(pid)
        if engine == "claude":
            m = re.search(r"ANTHROPIC_BASE_URL=([^\0\n]+)", env)
            base = m.group(1) if m else ""
            if "deepseek" in base:
                provider = "deepseek"
            elif "openrouter" in base:
                provider = "openrouter"
            elif ":8790" in base:
                provider = "deepseek"      # proxy locale dello spend_guard
            elif ":4000" in base:
                # 22/08/2026 (exec_adapter_abbonamenti, mandato samantha_1):
                # un agente instradato sul proxy LiteLLM (base_url
                # http://localhost:4000) NON e' deepseek: va classificato come
                # proxy litellm, distinto dallo spend_guard (:8790) e dall'API
                # deepseek diretta. Cosi' non gli si applicano per errore i
                # saldi/soglie deepseek (vedi serbatoi deepseek nel conf).
                provider = "litellm_proxy"
            if model is None:
                m2 = re.search(r"ANTHROPIC_MODEL=([^\0\n]+)", env)
                if m2:
                    model = m2.group(1)
        return engine, model, provider, pid
    return None, None, None, None


# ---------------------------------------------------------------------------
# SENSORE 1 — QUOTA / TOKEN
# ---------------------------------------------------------------------------

# Pattern d'errore PER MOTORE. Cercarli tutti su tutti i pannelli produce falsi
# positivi (un pannello che cita '429' in un log non e' un agente in quota).
PATTERN_QUOTA = {
    "agy": [
        (r"Individual quota reached", "quota_esaurita"),
        (r"Resets in\s+([0-9hm ]+)", "reset_annunciato"),
        (r"Quota exceeded", "quota_esaurita"),
        (r"API Error:|safety violation|Resource has been exhausted", "errore_api"),
    ],
    "claude": [
        (r"continuing shortly|continuing\b|resuming", "attesa_annunciata"),
        (r"session limit|hit your session limit", "quota_esaurita"),
        (r"Invalid model name", "modello_non_valido"),
        (r"\b429\b", "quota_esaurita"),
        (r"rate limit", "quota_esaurita"),
        (r"usage limit", "quota_esaurita"),
        (r"\b402\b", "saldo_esaurito"),
        (r"Insufficient Balance", "saldo_esaurito"),
        (r"empty or malformed response", "risposta_vuota"),
        (r"safeguards flagged this message|safety safeguards", "blocco_sicurezza"),
        (r"API Error:\s*(?:.+safeguards|.+safety|.+overloaded|.+timeout|.+connection lost|.+internal server error|.+5\d\d|[^\n]+)", "errore_api"),
        (r"Connection lost|Connection closed|Request timed out", "errore_connessione"),
    ],
    "qwen": [(r"\b429\b", "quota_esaurita"), (r"quota", "quota_esaurita"), (r"API Error:", "errore_api")],
    "deepcode": [(r"\b402\b", "saldo_esaurito"), (r"\b429\b", "quota_esaurita"), (r"API Error:", "errore_api")],
}


RIGHE_CODA = 15


def coda_pannello(testo, righe=RIGHE_CODA):
    """Le ultime righe NON VUOTE del pannello: riga di stato, footer, ultimo
    messaggio. E' l'unica parte che descrive ADESSO."""
    utili = [r for r in (testo or "").splitlines() if r.strip()]
    return "\n".join(utili[-righe:])


def analizza_pannello(engine, testo):
    """[(pattern, tipo)] trovati per QUEL motore, cercati SOLO nella coda del
    pannello.

    ⚠️ PERCHE' SOLO LA CODA (21/08/2026, guasto reale trovato da samantha_1):
    engine_adapter.is_pane_generating() cercava i suoi marcatori in TUTTO il
    pannello — e il trascritto di un turno gia' concluso continuava a fare match
    per sempre, lasciando cinque pannelli agy dichiarati 'generating' e sette
    messaggi di Manfredo fermi per tre ore. Lo stesso identico errore qui
    varrebbe per i banner di quota: 'Individual quota reached' scritto un'ora fa
    resta nello scrollback, e un agente tornato sanissimo risulterebbe in quota
    per sempre — con failover ripetuti a vuoto. Il capture-pane e' un LOG, non
    uno stato: solo la sua coda descrive il presente."""
    trovati = []
    coda = coda_pannello(testo)
    for pat, tipo in PATTERN_QUOTA.get(engine, []):
        if re.search(pat, coda, re.I):
            trovati.append({"pattern": pat, "tipo": tipo})
    # Una pausa ANNUNCIATA dal CLI non e' un guasto: il CLI ha gia' detto che
    # riprende da solo. Se c'e', tutto il resto della riga va ignorato.
    # ⚠️ ECCEZIONE (23/08, ticket 5ded): il banner di reset di SESSIONE di
    # Claude Code — 'You've hit your session limit · resets <ora> ·
    # Continuing automatically' — contiene 'Continuing' ma NON riprende da solo:
    # resta fermo fino al reset del limite (processo in attesa). Quel
    # 'continuing' e' un falso amico: se nella coda c'e' 'session limit', la
    # quota VINCE sull''attesa_annunciata' e il pannello va diagnosticato.
    if any(t["tipo"] == "attesa_annunciata" for t in trovati) and not \
            re.search(r"session limit", coda, re.I):
        return [t for t in trovati if t["tipo"] == "attesa_annunciata"]
    return trovati


def parsa_reset_gemini(testo):
    """Minuti al reset dal banner agy 'Resets in 1h 23m' / 'Resets in 45m'.
    None se non annunciato: assenza di dato, MAI uno zero inventato."""
    m = re.search(r"Resets? in\s+(?:(\d+)\s*h)?\s*(?:(\d+)\s*m)?", testo, re.I)
    if not m or not (m.group(1) or m.group(2)):
        return None
    return int(m.group(1) or 0) * 60 + int(m.group(2) or 0)


def saldo_deepseek():
    """Saldo REALE dallo stesso posto da cui lo legge deepseek_spend_guard: la riga
    'SALDO PERIODICO ... saldo=$X' del suo log. NON si richiama l'API con la chiave —
    il guard la interroga gia' ogni 300s, farlo due volte e' solo rumore in piu'."""
    out = {"saldo_usd": None, "chiamate": None, "spesa_stimata_usd": None,
           "fonte": None, "eta_s": None}
    try:
        import deepseek_spend_guard as guard
        st = guard._leggi_stato()
        out["chiamate"] = st.get("chiamate")
        out["spesa_stimata_usd"] = st.get("spesa_stimata_usd")
        log_path = guard.LOG_PATH
    except Exception:
        log_path = os.path.join(CORE, "deepseek_spend_guard.log")
    try:
        with open(log_path, encoding="utf-8", errors="replace") as f:
            righe = f.readlines()[-400:]
        for r in reversed(righe):
            m = re.search(r"SALDO PERIODICO.*saldo=\$([\d.]+)", r)
            if m:
                out["saldo_usd"] = float(m.group(1))
                out["fonte"] = "deepseek_spend_guard.log (SALDO PERIODICO)"
                mt = re.match(r"(\d{4}-\d{2}-\d{2}T[\d:]+)", r)
                if mt:
                    try:
                        dt = datetime.fromisoformat(mt.group(1))
                        out["eta_s"] = int((datetime.now() - dt).total_seconds())
                    except ValueError:
                        pass
                break
    except Exception:
        pass
    return out


SONDA_LOCK = os.path.join(CORE, "guardiano_sonda.lock")


def _lancia_sonda_in_background():
    """Un solo sondaggio alla volta: il flock qui e' quello che impedisce di
    aprire due finestre PROBE-Usage in corsa fra loro se un ciclo parte mentre
    il precedente sta ancora sondando."""
    try:
        fh = open(SONDA_LOCK, "w")
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError):
        return False           # sonda gia' in corso: non se ne lancia un'altra
    fh.close()                 # il lock vero lo prende il figlio
    try:
        subprocess.Popen(
            [os.path.join(CORE, "venv", "bin", "python3"),
             os.path.join(CORE, "guardiano.py"), "--sonda-usage"],
            cwd=CORE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True)
        ledger("sonda_usage_avviata", motivo="cache scaduta")
        return True
    except Exception as e:
        ledger("sonda_usage_errore", errore=str(e)[:200])
        return False


def quota_flat_da_sonda(conf, forza=False, dry=False):
    """Quota Claude (sessione/settimana/Fable) e agy dalla sonda /usage GIA' IN
    PRODUZIONE (engine_budget.sonda_quota + probe_fondi_flat.estrai_pct_*).
    Non riscritta: RIUSATA. Costa una finestra tmux e fino a 240s per motore,
    quindi il risultato sta in cache su file e si rifa' al massimo 1 volta/ora."""
    sc = conf.get("sonda_usage", {})
    cache_path = sc.get("cache_path", "/root/ecosistema_agenti/logs/guardiano_usage_cache.json")
    cache = _carica_json(cache_path, {})
    eta = time.time() - cache.get("ts", 0)
    if dry:
        # --dry significa NESSUN effetto: la sonda aprirebbe una finestra tmux e
        # scriverebbe '/usage' dentro un CLI vero — sono effetti, non letture.
        cache["da_cache"] = True
        cache["nota_dry"] = "sonda non eseguita in dry-run (aprirebbe una finestra tmux)"
        cache["eta_s"] = int(eta) if cache.get("ts") else None
        return cache
    if not forza and (not sc.get("abilitata", True) or eta < sc.get("intervallo_min_s", 3600)):
        cache["eta_s"] = int(eta) if cache.get("ts") else None
        cache["da_cache"] = True
        return cache
    if not forza:
        # LA SONDA NON DEVE MAI GIRARE DENTRO IL CICLO. Costa fino a 240s per
        # motore (apre una finestra tmux, aspetta il boot del CLI, incolla
        # /usage): un ciclo da 60s che ne aspetta due resta CIECO per otto
        # minuti — e per giunta smetterebbe di aggiornare il proprio heartbeat,
        # facendosi diagnosticare "appeso" dal proprio sensore servizi.
        # Si lancia in un processo staccato che aggiorna la cache per conto suo;
        # questo ciclo usa il valore precedente e lo dichiara vecchio.
        _lancia_sonda_in_background()
        cache["eta_s"] = int(eta) if cache.get("ts") else None
        cache["da_cache"] = True
        cache["sonda_in_corso"] = True
        return cache
    nuovo = {"ts": time.time(), "da_cache": False, "eta_s": 0}
    try:
        import engine_budget
        import probe_fondi_flat
        # La pezza locale che stava qui (rendere univoca SONDA_WIN solo nel MIO
        # processo) e' stata sostituita dal rimedio strutturale in engine_budget:
        # flock + finestra univoca per chiamata + cache con eta', validi per
        # tutti e sette i chiamanti e non solo per il guardiano.
        d_c = engine_budget.sonda_quota_dettaglio("claude")
        righe_c = d_c.get("righe") or []
        nuovo["claude_righe"] = righe_c
        nuovo["claude_sonda_eta_s"] = d_c.get("eta_s")
        nuovo["claude_sonda_da_cache"] = d_c.get("da_cache")
        if d_c.get("errore"):
            nuovo["claude_sonda_errore"] = str(d_c["errore"])[:200]
        # UNA SOLA implementazione dello split, in probe_fondi_flat: la copia
        # che stava qui (stessa regex, stesse etichette) era destinata a
        # divergere dall'originale al primo cambio di wording di /usage — e due
        # letture diverse della stessa quota sono esattamente il guasto che
        # questo lavoro deve chiudere, non un dettaglio di stile.
        q = probe_fondi_flat.estrai_quote_claude(righe_c)
        nuovo["claude_settimanale_pct"] = q["pct_settimana"]
        nuovo["claude_settimanale_fable_pct"] = q["pct_settimana_fable"]
        nuovo["claude_sessione_pct"] = q["pct_sessione"]
        nuovo["claude_reset_sessione"] = q["reset_sessione"]
        # tenuto SOLO per diagnosi: nessuna decisione lo guarda (vedi
        # serbatoio_disponibile, che usa claude_settimanale_pct)
        nuovo["claude_pct_max"] = q["pct_max"]
        nuovo["claude_fonte"] = q["fonte"]
        if q["righe_non_riconosciute"]:
            nuovo["claude_righe_non_riconosciute"] = q["righe_non_riconosciute"]
        att, nota_att = probe_fondi_flat.sessione_da_attendere(q)
        nuovo["claude_attendi_reset"] = att
        if att:
            nuovo["claude_attendi_reset_nota"] = nota_att
    except Exception as e:
        nuovo["claude_errore"] = str(e)[:200]
    try:
        import engine_budget
        import probe_fondi_flat
        d_a = engine_budget.sonda_quota_dettaglio("agy")
        righe_a = d_a.get("righe") or []
        nuovo["agy_righe"] = righe_a
        nuovo["agy_sonda_eta_s"] = d_a.get("eta_s")
        nuovo["agy_sonda_da_cache"] = d_a.get("da_cache")
        if d_a.get("errore"):
            nuovo["agy_sonda_errore"] = str(d_a["errore"])[:200]
        q_a = probe_fondi_flat.estrai_quote_agy(righe_a)
        nuovo["agy_5h_disponibile_pct"] = q_a.get("pct_5h")
        nuovo["agy_settimanale_disponibile_pct"] = q_a.get("pct_settimana")
        nuovo["agy_disponibile_pct"] = q_a.get("pct_min")
        nuovo["agy_reset_5h"] = q_a.get("reset_5h")
        nuovo["agy_reset_settimana"] = q_a.get("reset_settimana")
        nuovo["agy_claude_disponibile_pct"] = q_a.get("pct_claude_5h") or q_a.get("pct_claude_settimana")
        for r in righe_a:
            if "CLAUDE" in r.upper() or "GPT" in r.upper():
                m = re.search(r"(\d+(?:\.\d+)?)\s*%\s*DISPONIBILE", r)
                if m:
                    v = float(m.group(1))
                    prec = nuovo.get("agy_claude_disponibile_pct")
                    nuovo["agy_claude_disponibile_pct"] = v if prec is None else min(prec, v)
    except Exception as e:
        nuovo["agy_errore"] = str(e)[:200]
    _salva_json(cache_path, nuovo)
    return nuovo


def finestre_agente():
    """[(sessione, finestra, pane_pid)] di tutte le finestre tmux che NON sono
    servizi. Il filtro sui servizi sta in conf (lista_bianca.finestre_prefisso)."""
    out = []
    try:
        righe = subprocess.run(
            ["tmux", "list-panes", "-a", "-F",
             "#{session_name}\t#{window_name}\t#{pane_pid}\t#{pane_dead}"],
            capture_output=True, text=True, timeout=20).stdout.splitlines()
    except Exception:
        return out
    for r in righe:
        p = r.split("\t")
        if len(p) < 3:
            continue
        try:
            out.append((p[0], p[1], int(p[2]), p[3] if len(p) > 3 else "0"))
        except ValueError:
            continue
    return out


def capture_pane(sess, win, righe=60):
    # 22/08/2026 FIX classe "match per prefisso" (funzione condivisa tmux_esatto):
    # capture-pane sul target ESATTO. Il guardiano DECIDE su questo testo (stallo,
    # failover, ESC): leggere per errore il pannello della finestra con lo stesso
    # PREFISSO di nome (es. AGY-PUBLIO -> AGY-PUBLIO-2) significa decidere su un
    # altro agente.
    try:
        return tmux_esatto.capture_pane_finestra(f"{sess}:{win}", righe) or ""
    except Exception:
        return ""


def e_servizio(sess, win, conf):
    lb = conf.get("lista_bianca", {})
    if any(win.startswith(p) for p in lb.get("finestre_prefisso", [])):
        return True
    return sess in lb.get("sessioni", [])


def sensore_quota(conf, db_agenti=None, jiffies_prec=None, intervallo_s=60, dry=False):
    """F1a. Una voce per finestra agente viva, con motore/modello REALI, pattern
    d'errore trovati a schermo e CPU% del CLI. E' anche la fonte del riallineo DB."""
    agenti = []
    for sess, win, pane_pid, dead in finestre_agente():
        if e_servizio(sess, win, conf):
            continue
        pids = sottoalbero(pane_pid)
        engine, model, provider, cli_pid = motore_reale_da_processo(pids)
        pane = capture_pane(sess, win)
        voce = {
            "sessione": sess,
            "finestra": win,
            "tmux_window": f"{sess}:{win}",
            "pane_pid": pane_pid,
            "pane_dead": dead == "1",
            "engine_reale": engine,
            "model_reale": model,
            "provider_reale": provider,
            "cli_pid": cli_pid,
            "cli_vivo": engine is not None,
            "problemi": analizza_pannello(engine, pane) if engine else [],
        }
        if engine == "agy":
            voce["reset_minuti"] = parsa_reset_gemini(coda_pannello(pane))
        if cli_pid:
            voce["cpu_pct"] = cpu_pct_processo(cli_pid, jiffies_prec, intervallo_s)
            voce["rss_mb"] = rss_mb(cli_pid)
        agenti.append(voce)
    return {
        "agenti": agenti,
        "deepseek": saldo_deepseek(),
        "flat": quota_flat_da_sonda(conf, dry=dry),
    }


# ---------------------------------------------------------------------------
# SENSORE 2 — RAM   (misure riusate da resource_watchdog.py, non riscritte)
# ---------------------------------------------------------------------------

def psi(risorsa):
    """Linux PSI: % di tempo in cui almeno un processo era in stallo su quella
    risorsa. Piu' onesto della % di uso: dice se il carico fa MALE, non solo se c'e'."""
    out = {}
    try:
        with open(f"/proc/pressure/{risorsa}", encoding="utf-8") as f:
            for riga in f:
                tipo = riga.split()[0]
                for k in ("avg10", "avg60", "avg300"):
                    m = re.search(rf"{k}=([\d.]+)", riga)
                    if m:
                        out[f"{tipo}_{k}"] = float(m.group(1))
    except Exception:
        pass
    return out


def sensore_ram(conf):
    import resource_watchdog as rw          # RIUSO, non riscrittura
    avail_mb, load1, load5 = rw.get_stats()
    swap_usato_mb = swap_tot_mb = None
    try:
        for riga in subprocess.getoutput("free -m").splitlines():
            if riga.startswith("Swap:"):
                p = riga.split()
                swap_tot_mb, swap_usato_mb = int(p[1]), int(p[2])
    except Exception:
        pass
    return {
        "disponibile_mb": avail_mb,
        "swap_usato_mb": swap_usato_mb,
        "swap_totale_mb": swap_tot_mb,
        "psi_memory": psi("memory"),
        "psi_io": psi("io"),
        "per_sessione": peso_sessioni(),
    }


def peso_sessioni():
    """{sessione: MB}. Stesso calcolo di resource_watchdog.top_sessions_by_mem,
    ma restituito come DATO invece che come stringa gia' formattata: qui serve
    ordinabile per decidere chi spegnere, non solo da stampare in un messaggio."""
    per_sess = {}
    try:
        righe = subprocess.getoutput(
            "tmux list-panes -a -F '#{session_name} #{pane_pid}'").splitlines()
        for riga in righe:
            p = riga.split()
            if len(p) != 2:
                continue
            sess, pid = p[0], p[1]
            rss = subprocess.getoutput(
                f"ps --ppid {pid} -o rss= 2>/dev/null; ps -o rss= -p {pid} 2>/dev/null")
            kb = sum(int(x) for x in rss.split() if x.strip().isdigit())
            per_sess[sess] = per_sess.get(sess, 0) + kb
    except Exception:
        return {}
    return {s: kb // 1024 for s, kb in sorted(per_sess.items(), key=lambda kv: -kv[1])}


def rss_mb(pid):
    try:
        with open(f"/proc/{pid}/statm") as f:
            return int(f.read().split()[1]) * os.sysconf("SC_PAGE_SIZE") // (1024 * 1024)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# SENSORE 3 — CPU
#
# NON si usa `ps -o %cpu`: quella e' la media sull'INTERA VITA del processo, e su
# un CLI acceso da ore un retry loop partito da 5 minuti sparisce dentro la media.
# Qui si legge il contatore di jiffies in /proc/<pid>/stat e si fa la differenza
# fra due cicli: e' il consumo NELL'INTERVALLO, l'unico che riveli un loop.
# ---------------------------------------------------------------------------

def jiffies(pid):
    try:
        with open(f"/proc/{pid}/stat") as f:
            campi = f.read().rsplit(")", 1)[1].split()
        return int(campi[11]) + int(campi[12])       # utime + stime
    except Exception:
        return None


def leggi_jiffies_tutti(pids):
    return {p: j for p in pids if (j := jiffies(p)) is not None}


def cpu_pct_processo(pid, jiffies_prec, intervallo_s):
    """CPU% nell'intervallo. None al primo ciclo (nessun campione precedente):
    un dato mancante e' meglio di uno zero che sembra una misura."""
    if not jiffies_prec or pid not in jiffies_prec or intervallo_s <= 0:
        return None
    ora = jiffies(pid)
    if ora is None:
        return None
    hz = os.sysconf("SC_CLK_TCK")
    return round(100.0 * (ora - jiffies_prec[pid]) / hz / intervallo_s, 1)


def snapshot_jiffies_globale():
    # Il timestamp viaggia con lo snapshot: un ciclo puo' durare 110s invece di
    # 60 sotto carico (misurato il 21/08), e dividere per l'intervallo NOMINALE
    # gonfia la CPU di quasi il doppio — un ciclo ha riportato 146% di
    # saturazione su una macchina che non era affatto a quel punto. Una misura
    # sbagliata in eccesso e' peggio di nessuna misura: fa scattare un ROSSO
    # falso, e un allarme falso consuma la credibilita' di quelli veri.
    """{pid: (comm, jiffies)} per TUTTI i processi. Serve alla vista aggregata:
    la misura del 21/08 (samantha_2, load 43 su 8 core) ha mostrato che non
    c'e' mai un singolo colpevole — agy 236%, python3 184%, claude 126%,
    chrome 106%, postgres 71%. Un top-N di processi singoli non lo avrebbe
    fatto vedere: ogni voce presa da sola sembrava innocua."""
    out = {}
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/stat") as f:
                testo = f.read()
            comm = testo.split("(", 1)[1].rsplit(")", 1)[0]
            campi = testo.rsplit(")", 1)[1].split()
            out[int(pid)] = (comm, int(campi[11]) + int(campi[12]))
        except Exception:
            continue
    out["_ts"] = ("_ts", time.time())
    return out


def mappa_pid_sessione():
    """{pid: sessione tmux} per ogni processo nel sottoalbero di ogni pannello."""
    mappa = {}
    for sess, _win, pane_pid, _dead in finestre_agente():
        for pid in sottoalbero(pane_pid):
            mappa[pid] = sess
    return mappa


def cpu_aggregata(globale_prec, globale_ora, intervallo_s, per_sessione=None):
    """(per_gruppo, per_sessione, totale_pct) in % di UN core (236% = 2,36 core).
    Vuoto al primo ciclo: senza campione precedente non esiste una misura, e uno
    zero sarebbe una bugia con l'aria di un dato."""
    if not globale_prec or intervallo_s <= 0:
        return {}, {}, None
    # tempo DAVVERO trascorso fra i due snapshot (vedi nota in snapshot_jiffies_globale)
    reale = globale_ora.get("_ts", ("", 0))[1] - globale_prec.get("_ts", ("", 0))[1]
    if reale > 0.5:
        intervallo_s = reale
    hz = os.sysconf("SC_CLK_TCK")
    gruppi, sessioni, totale = {}, {}, 0.0
    for pid, (comm, j) in globale_ora.items():
        if pid == "_ts":
            continue
        prec = globale_prec.get(pid)
        if not prec:
            continue
        pct = 100.0 * (j - prec[1]) / hz / intervallo_s
        if pct <= 0.05:
            continue
        gruppi[comm] = round(gruppi.get(comm, 0.0) + pct, 1)
        totale += pct
        sess = (per_sessione or {}).get(pid)
        if sess:
            sessioni[sess] = round(sessioni.get(sess, 0.0) + pct, 1)
    ordina = lambda d: dict(sorted(d.items(), key=lambda kv: -kv[1])[:12])
    return ordina(gruppi), ordina(sessioni), round(totale, 1)


def sensore_cpu(conf, globale_prec=None, globale_ora=None, intervallo_s=60,
                pid_sessione=None):
    import resource_watchdog as rw
    _avail, load1, load5 = rw.get_stats()
    core = os.cpu_count()
    gruppi, sessioni, totale = cpu_aggregata(globale_prec, globale_ora,
                                             intervallo_s, pid_sessione)
    return {"load1": load1, "load5": load5, "psi_cpu": psi("cpu"), "core": core,
            "per_gruppo": gruppi, "per_sessione": sessioni,
            "totale_pct": totale,
            "saturazione_pct": round(totale / core, 1) if totale is not None else None}


# ---------------------------------------------------------------------------
# SENSORE 4 — SERVIZI SRV-*  (heartbeat dell'infrastruttura)
#
# I guardiani hanno lo stesso difetto che sorvegliano: il 18/08 l'health_cron si
# e' appeso per 26 ore e nessuno se n'e' accorto, perche' nessuno guardava LUI.
# Qui si controlla che ogni finestra services:SRV-* abbia un processo vivo, e in
# piu' che i servizi con un heartbeat su file lo stiano davvero aggiornando: una
# finestra viva con dentro un processo appeso e' indistinguibile da una sana
# finche' non si guarda l'orologio del suo heartbeat.
# ---------------------------------------------------------------------------

# Soglia 'appeso' PER SERVIZIO. Per il guardiano non e' un numero scelto a mano:
# 300s erano meno di un suo ciclo lungo reale (110s misurati, sonda esclusa), e
# infatti ha prodotto un falso ROSSO su se stesso il 21/08. Ora e' 3x la durata
# massima osservata negli ultimi cicli, con un minimo di sicurezza.
HEARTBEAT_SERVIZI = {
    "SRV-Guardiano": (HEARTBEAT_PATH, 600),
}


def soglia_heartbeat_guardiano(minimo=600):
    """3x la durata massima dei cicli recenti (dal ledger), mai sotto `minimo`."""
    try:
        durate = []
        with open(LEDGER_PATH, encoding="utf-8") as f:
            for riga in f.readlines()[-300:]:
                d = json.loads(riga)
                if d.get("evento") == "ciclo" and d.get("durata_s"):
                    durate.append(float(d["durata_s"]))
        if durate:
            return max(minimo, round(max(durate[-40:]) * 3))
    except Exception:
        pass
    return minimo


def sensore_pool(conf, slug_vivi=None):
    """Eta' del ticket piu' vecchio non ancora preso in carico, per ogni pool.

    Sintomo nuovo (samantha_1, 21/08): un servizio puo' essere VIVO e NON
    CONSEGNARE. Il campanellaio girava, la finestra c'era, il processo pure — e
    sette messaggi di Manfredo sono rimasti fermi tre ore. Nessun controllo di
    liveness lo avrebbe visto: l'unica prova sta DALL'ALTRA PARTE del confine,
    cioe' nella inbox che non si svuota. Il guardiano NON reimplementa il
    campanellaio: ne legge il risultato."""
    pool = []
    base = os.path.join(os.path.dirname(CORE), "a2a")
    s_ = conf["soglie"]
    try:
        destinatari = sorted(os.listdir(base))
    except OSError:
        return pool
    # SOLO i destinatari VIVI. La inbox piena di un agente SPENTO non e' un
    # guasto di consegna: e' esattamente quello che deve succedere, i messaggi
    # lo aspettano. Senza questo filtro il sensore trovava 40 "pool fermi" su
    # 600 caselle, quasi tutte di agenti spenti da giorni — quaranta allarmi
    # falsi che avrebbero sepolto l'unico vero.
    if slug_vivi is not None:
        destinatari = [d for d in destinatari if d in slug_vivi]
    for dest in destinatari:
        inbox = os.path.join(base, dest, "inbox")
        if not os.path.isdir(inbox):
            continue
        try:
            file_ = [f for f in os.listdir(inbox) if f.endswith(".md")]
        except OSError:
            continue
        if not file_:
            continue
        eta_max, piu_vecchio = 0, None
        for f in file_:
            try:
                eta = time.time() - os.path.getmtime(os.path.join(inbox, f))
            except OSError:
                continue
            if eta > eta_max:
                eta_max, piu_vecchio = eta, f
        # stato del campanellaio, se lo espone (lo definisce exec_campanellaio_dinamico)
        stato_json = os.path.join(CORE, f"campanellaio_stato_{dest}.json")
        stato = _carica_json(stato_json, {}) if os.path.exists(stato_json) else {}
        pool.append({"destinatario": dest, "in_attesa": len(file_),
                     "eta_max_min": round(eta_max / 60, 1),
                     "piu_vecchio": piu_vecchio,
                     "campanellaio": stato or None})
    return [p for p in pool if p["eta_max_min"] >= s_["inbox_giallo_min"]]


def decisioni_pool(pool, conf, max_dettaglio=3):
    """> 20 min senza consegna = GIALLO al destinatario; > 60 min = ROSSO a
    Manfredo (se nessuno consegna da un'ora, avvisare via A2A sarebbe ironico:
    l'avviso finirebbe nella stessa coda che non si svuota)."""
    dec = []
    s_ = conf["soglie"]
    # Se sono tanti insieme il guasto e' UNO (il campanellaio), non venti: si
    # manda un messaggio solo con l'elenco, non venti messaggi identici.
    if len(pool) > max_dettaglio:
        peggiori = sorted(pool, key=lambda x: -x["eta_max_min"])[:5]
        return [{"azione": "segnala_consegna_ferma", "livello": "ROSSO",
                 "agente": None, "ruolo": "coordinatore", "coordinatore": None,
                 "pool_coinvolti": len(pool),
                 "peggiori": [{"chi": x["destinatario"], "min": x["eta_max_min"],
                               "in_attesa": x["in_attesa"]} for x in peggiori],
                 "perche": f"{len(pool)} caselle di agenti VIVI non vengono svuotate: "
                           f"non e' un problema del singolo destinatario, e' la "
                           f"consegna che si e' fermata. Piu' vecchie: "
                           + ", ".join(f"{x['destinatario']} ({x['eta_max_min']} min)"
                                       for x in peggiori)}]
    for p in pool:
        rosso = p["eta_max_min"] >= s_["inbox_rosso_min"]
        dec.append({"azione": "segnala_consegna_ferma" if rosso else "sblocca_consegna_ferma",
                    "livello": "ROSSO" if rosso else "GIALLO",
                    "agente": p["destinatario"], "ruolo": "coordinatore",
                    "coordinatore": p["destinatario"] if not rosso else None,
                    "in_attesa": p["in_attesa"], "eta_max_min": p["eta_max_min"],
                    "perche": f"la inbox di {p['destinatario']} ha {p['in_attesa']} "
                              f"messaggi non presi in carico, il piu' vecchio da "
                              f"{p['eta_max_min']} min: il servizio e' VIVO ma NON CONSEGNA"})
    return dec


def _sonda_probe_viva():
    """Vero se esiste ADESSO una finestra di sonda /usage (PROBE-Usage*). E' la
    prova esterna che il picco di CPU di un servizio e' lavoro previsto e non un
    loop: la sonda apre un CLI vero e lo tiene su per minuti."""
    try:
        out = subprocess.run(["tmux", "list-windows", "-a", "-F", "#{window_name}"],
                             capture_output=True, text=True, timeout=20).stdout
        return any(w.strip().startswith("PROBE-Usage") for w in out.splitlines())
    except Exception:
        return None          # non letto: None, mai un False inventato


def sensore_servizi(conf, cpu_srv=None):
    """cpu_srv: {pid: cpu_pct} dell'ultimo intervallo, per attribuire la CPU anche
    ai servizi. Il 21/08 ecosystem_health_cron.py da solo stava all'82% per
    minuti: un servizio puo' essere runaway esattamente come un agente, e finora
    nessun guardiano lo guardava."""
    servizi = []
    lb = conf.get("lista_bianca", {})
    prefissi = tuple(lb.get("finestre_prefisso", ["SRV-"]))
    for sess, win, pane_pid, dead in finestre_agente():
        if sess not in lb.get("sessioni", ["services"]) or not win.startswith(prefissi):
            continue
        pids = sottoalbero(pane_pid)
        # un pane con nessun figlio e' una shell nuda: la finestra c'e', il
        # servizio no (stessa classe di guasto del 18/08 su auruncia_tech)
        vivo = len(pids) > 1 or bool(_cmdline(pane_pid))
        voce = {"finestra": f"{sess}:{win}", "pane_dead": dead == "1",
                "processi": len(pids), "vivo": vivo and dead != "1"}
        if cpu_srv is not None:
            voce["cpu_pct"] = round(sum(cpu_srv.get(pid_, 0.0) for pid_ in pids), 1)
        # (f) 21/08, samantha_1 via samantha_2: un servizio che per MESTIERE
        # lancia un CLI (health_cron -> sonda /usage, ~3 minuti) produce un picco
        # di CPU PER COSTRUZIONE. Senza questi due campi il picco e'
        # indistinguibile da un loop impazzito, e si segnala lavoro normale come
        # guasto. Sono la FONTE che il ledger deve poter citare.
        voce["figli"] = max(0, len(pids) - 1)
        voce["processi_figli"] = [(_cmdline(pd) or "")[:60] for pd in pids[1:6]]
        voce["sonda_probe_viva"] = _sonda_probe_viva()
        hb = HEARTBEAT_SERVIZI.get(win)
        if hb:
            path, max_eta = hb
            if win == "SRV-Guardiano":
                max_eta = soglia_heartbeat_guardiano(max_eta)
            try:
                eta = time.time() - os.path.getmtime(path)
                voce["heartbeat_eta_s"] = round(eta)
                voce["heartbeat_fresco"] = eta <= max_eta
            except OSError:
                voce["heartbeat_eta_s"] = None
                voce["heartbeat_fresco"] = False
        servizi.append(voce)
    return servizi


def _nome_servizio_norm(nome):
    """'services:SRV-Cosimo_3Min_Watchdog' -> 'cosimo_3min_watchdog'. Stessa
    normalizzazione di scopri_guardiani (prefisso SRV- via, '-' -> '_')."""
    n = str(nome or "").strip().lower().split(":")[-1]
    if n.startswith("srv-"):
        n = n[4:]
    return n.replace("-", "_")


def servizio_atteso_spento(finestra, conf):
    """F-A (mandato, punto 4): un servizio spento DI PROPOSITO in manutenzione
    (lista 'servizi_attesi_spenti' in conf) non e' un guasto: la sua assenza o
    il suo pane senza figli NON producono un ROSSO. Match ESATTO sul nome
    normalizzato, mai per prefisso o sottostringa: 'sensore' non deve
    silenziare 'SRV-Sensore_Altro'. Le voci che iniziano per '_' sono note."""
    lista = conf.get("servizi_attesi_spenti") or []
    n = _nome_servizio_norm(finestra)
    return any(_nome_servizio_norm(x) == n for x in lista
               if isinstance(x, str) and not x.startswith("_"))


def decisioni_servizi(servizi, conf, persistenza=None):
    """Un servizio giu' e' sempre ROSSO: non ci sono coordinatori a cui delegarlo
    e nessuno se ne accorge da solo. Il guardiano NON rilancia i servizi (lo fa
    gia' l'health_cron con la sua required_windows): qui si segnala, per non
    avere due guardiani che rilanciano la stessa finestra in corsa fra loro."""
    dec = []
    soglia_cpu = conf["soglie"]["cpu_runaway_servizio_pct"]
    vita_sonda = conf["soglie"].get("sonda_vita_attesa_s", 300)
    for s_ in servizi:
        if servizio_atteso_spento(s_["finestra"], conf) and \
                (not s_["vivo"] or s_.get("heartbeat_fresco") is False):
            # Spento di proposito (manutenzione 22/08): si registra, non si allarma.
            ledger("servizio_atteso_spento", servizio=s_["finestra"], vivo=s_["vivo"],
                   fonte="conf.servizi_attesi_spenti",
                   perche="servizio in lista servizi_attesi_spenti: assenza attesa, "
                          "nessun allarme")
            continue
        if not s_["vivo"]:
            dec.append({"azione": "segnala_servizio_giu", "livello": "ROSSO",
                        "agente": s_["finestra"], "ruolo": "coordinatore",
                        "tmux_window": s_["finestra"],
                        "perche": f"servizio {s_['finestra']} senza processo vivo "
                                  f"(pane_dead={s_['pane_dead']}, processi={s_['processi']})"})
            continue

        # --- CPU: picco PREVISTO o loop? (integrazione 21/08, samantha_1) ----
        # Un servizio che lancia un CLI per mestiere (health_cron -> sonda
        # /usage, ~3 minuti) sta caldo PER COSTRUZIONE. Segnalarlo al primo
        # campione significa chiamare guasto il lavoro normale — la stessa
        # classe di errore della ripetizione-senza-cambio-di-stato, dove a
        # decidere non e' l'istantanea ma la DURATA.
        # Si segnala solo se: il picco dura oltre la vita attesa della sonda,
        # OPPURE il processo caldo non ha figli e nessuna sonda e' viva (sta
        # bruciando CPU da solo, senza lavoro che lo giustifichi).
        soglia_finestra = soglia_cpu * 3.5 if s_["finestra"] == "services:SRV-Supervisore" else soglia_cpu
        caldo = (s_.get("cpu_pct") is not None and s_["cpu_pct"] > soglia_finestra)
        durata = (persistenza.aggiorna(f"srv_cpu:{s_['finestra']}", caldo)
                  if persistenza is not None else None)
        if caldo:
            ha_lavoro = bool(s_.get("figli")) or s_.get("sonda_probe_viva") is True
            fonte = (f"cpu_srv sul sottoalbero di {s_['finestra']}; "
                     f"figli={s_.get('figli')}; "
                     f"finestra PROBE-Usage viva={s_.get('sonda_probe_viva')}")
            dura_troppo = durata is None or durata >= vita_sonda
            if ha_lavoro and not dura_troppo:
                # Non e' una decisione: e' lavoro in corso. Resta comunque nel
                # ledger CON la durata e la fonte, cosi' se poi sfora si vede da
                # quanto stava gia' andando.
                ledger("picco_cpu_atteso", servizio=s_["finestra"],
                       valore=s_["cpu_pct"], fonte=fonte,
                       durata_picco_s=round(durata), vita_sonda_s=vita_sonda,
                       processi_figli=s_.get("processi_figli"),
                       perche="picco entro la vita attesa della sonda e con lavoro "
                              "vero sotto: attesa, non runaway")
            else:
                motivo = (f"da {round(durata)}s, oltre la vita attesa della sonda "
                          f"({vita_sonda}s)" if durata is not None and dura_troppo
                          else "senza figli ne' sonda viva a giustificarlo")
                dec.append({"azione": "segnala_servizio_runaway", "livello": "GIALLO",
                            "agente": s_["finestra"], "ruolo": "coordinatore",
                            "tmux_window": s_["finestra"],
                            "valore": s_["cpu_pct"], "fonte": fonte,
                            "durata_picco_s": None if durata is None else round(durata),
                            "figli": s_.get("figli"),
                            "sonda_probe_viva": s_.get("sonda_probe_viva"),
                            "perche": f"{s_['finestra']} al {s_['cpu_pct']}% di CPU "
                                      f"(soglia {soglia_cpu}%) {motivo}: un servizio in "
                                      f"loop pesa quanto un agente, ma non si spegne in "
                                      f"automatico — e' infrastruttura condivisa"})

        # Heartbeat: controllo INDIPENDENTE dalla CPU. Un servizio puo' essere
        # insieme caldo e appeso, e prima l'elif faceva vedere solo il primo dei due.
        if s_.get("heartbeat_fresco") is False:
            dec.append({"azione": "segnala_servizio_muto", "livello": "ROSSO",
                        "agente": s_["finestra"], "ruolo": "coordinatore",
                        "tmux_window": s_["finestra"],
                        "valore": s_.get("heartbeat_eta_s"),
                        "fonte": f"mtime del file di heartbeat di {s_['finestra']}",
                        "eta_dato_s": s_.get("heartbeat_eta_s"),
                        "perche": f"{s_['finestra']} e' vivo ma il suo heartbeat non si "
                                  f"aggiorna da {s_.get('heartbeat_eta_s')}s: processo appeso"})
    return dec


# ---------------------------------------------------------------------------
# PERSISTENZA — "sopra soglia DA N minuti"
#
# Una singola lettura non e' una diagnosi (lezione di check_agenti_vivi: un
# pannello letto mentre si ridisegna dichiara morto un agente vivissimo). Qui si
# registra QUANDO una condizione e' comparsa la prima volta e si agisce solo se
# dura da abbastanza: e' cio' che distingue un picco da un guasto.
# ---------------------------------------------------------------------------

class Persistenza:
    def __init__(self, path=PERSIST_PATH, orologio=time.time):
        self.path = path
        self.orologio = orologio
        self.dati = _carica_json(path, {})

    def aggiorna(self, chiave, attiva):
        ora = self.orologio()
        if not attiva:
            self.dati.pop(chiave, None)
            return 0.0
        primo = self.dati.setdefault(chiave, ora)
        return ora - primo

    def salva(self):
        _salva_json(self.path, self.dati)


class Dedup:
    """Cooldown per CONDIZIONE (non per tipo di alert): un'allerta RAM non deve
    silenziare un'allerta quota arrivata dopo. Un allarme che suona sempre smette
    di informare, e il giorno che avra' ragione nessuno gli credera'."""
    def __init__(self, path=DEDUP_PATH, cooldown_s=900, orologio=time.time):
        self.path = path
        self.cooldown_s = cooldown_s
        self.orologio = orologio
        self.dati = _carica_json(path, {})
        # F-A: quante volte la stessa chiave e' stata soppressa dall'ultimo
        # invio. In RAM di proposito: e' un contatore di lettura del log, non
        # uno stato che decide qualcosa.
        self.conteggi = {}

    def consenti(self, chiave):
        ora = self.orologio()
        ultimo = self.dati.get(chiave, 0)
        if ora - ultimo < self.cooldown_s:
            self.conteggi[chiave] = self.conteggi.get(chiave, 0) + 1
            return False
        self.dati[chiave] = ora
        self.conteggi[chiave] = 0
        return True

    def soppresse(self, chiave):
        return self.conteggi.get(chiave, 0)

    def ultimo_invio(self, chiave):
        return self.dati.get(chiave)

    def salva(self):
        _salva_json(self.path, self.dati)


# ---------------------------------------------------------------------------
# CLASSIFICAZIONE AGENTI + SCELTA DEL SERBATOIO
# ---------------------------------------------------------------------------

def classifica_agente(slug, dati_db, conf):
    """exec | coordinatore_critico | coordinatore | sottoposto.
    L'exec si riconosce dallo slug/finestra (convenzione AGY-EXEC_*), il
    coordinatore dal campo 'role' a SUBSTRING (in tutto l'ecosistema 'role' e'
    una descrizione libera: 'Specialista ... coordinatore ...' e' un coordinatore)."""
    critici = conf.get("ruoli", {}).get("coordinatore_critico", {}).get("agenti", [])
    if slug in critici:
        return "coordinatore_critico"
    tw = (dati_db or {}).get("tmux_window", "") or ""
    if slug.startswith("exec_") or slug.startswith("setup_") or "EXEC" in tw.upper():
        return "exec"
    role = ((dati_db or {}).get("role") or "").lower()
    if "coordinatore" in role:
        return "coordinatore"
    return "sottoposto"


def serbatoio_disponibile(nome, misure, conf):
    """(bool, motivo). Le condizioni di PRIMO LIVELLO della tabella: 'saldo
    deepseek sotto soglia', 'Gemini in reset', 'claude oltre soglia settimanale'.
    Se una misura MANCA il serbatoio e' considerato disponibile ma il motivo lo
    dichiara: assenza di lettura non e' prova di esaurimento (lezione del
    probe_fondi_flat, 18/08: il cron uccideva la sonda e l'allarme passava muto)."""
    s = conf["soglie"]
    if nome == "deepseek":
        saldo = misure.get("deepseek_saldo_usd")
        if saldo is None:
            return True, "saldo non letto (fail-safe: non escluso)"
        if saldo <= s["deepseek_saldo_critico_usd"]:
            return False, f"saldo ${saldo:.2f} <= ${s['deepseek_saldo_critico_usd']}"
        if saldo <= s["deepseek_saldo_basso_usd"]:
            return True, f"saldo basso ${saldo:.2f} (usabile, ma ultimo in ordine)"
        return True, f"saldo ${saldo:.2f}"
    if nome == "claude":
        # IL VINCOLO VERO E' IL SETTIMANALE, mai il massimo aggregato.
        # 21/08: estrai_pct_claude() prende il MAX fra sessione 5h, settimana
        # all-models e settimana Fable — e diceva 86% quando il settimanale era
        # al 9%. Quell'86% era la SESSIONE, che si azzerava venti minuti dopo:
        # letto come "claude esaurito" avrebbe mandato la flotta a pagamento
        # con l'abbonamento praticamente intatto.
        if misure.get("claude_stale"):
            return True, "lettura vecchia (stale): non usata per escludere"
        set_pct = misure.get("claude_settimanale_pct")
        if set_pct is None:
            return True, "settimanale non letto (fail-safe: non escluso)"
        if set_pct >= s["claude_settimanale_soglia_pct"]:
            return False, f"settimanale {set_pct}% >= {s['claude_settimanale_soglia_pct']}%"
        ses = misure.get("claude_sessione_pct")
        if ses is not None and ses >= s["claude_sessione_soglia_pct"]:
            # sessione piena ma settimana libera: si ASPETTA il reset, non si paga
            return False, (f"sessione {ses}% piena (settimanale solo {set_pct}%): "
                           f"si attende il reset, non si devia a pagamento")
        return True, f"settimanale {set_pct}%"
    if nome == "agy_claude":
        # Bucket CLAUDE+GPT di Antigravity: flat, separato da quello Gemini.
        disp = misure.get("agy_claude_disponibile_pct")
        if disp is None:
            return True, "bucket CLAUDE+GPT non letto (fail-safe: non escluso)"
        if disp <= s["agy_disponibile_minimo_pct"]:
            return False, f"bucket CLAUDE+GPT disponibile {disp}%"
        return True, f"bucket CLAUDE+GPT disponibile {disp}%"
    if nome == "agy":
        if misure.get("agy_in_reset"):
            return False, f"in reset ({misure.get('agy_reset_minuti')} min)"
        disp_5h = misure.get("agy_5h_disponibile_pct")
        disp_sett = misure.get("agy_settimanale_disponibile_pct")
        disp = misure.get("agy_disponibile_pct")
        min_disp = disp
        if min_disp is None:
            vals = [v for v in (disp_5h, disp_sett) if v is not None]
            min_disp = min(vals) if vals else None
        if min_disp is None:
            return True, "disponibilita' non letta (fail-safe: non escluso)"
        soglia = s.get("agy_disponibile_minimo_pct", 5)
        if disp_5h is not None and disp_5h <= soglia:
            return False, f"finestra 5h esaurita/critica ({disp_5h}% <= {soglia}%)"
        if disp_sett is not None and disp_sett <= soglia:
            return False, f"settimanale esaurito/critico ({disp_sett}% <= {soglia}%)"
        if min_disp <= soglia:
            return False, f"disponibile {min_disp}% <= {soglia}%"
        return True, f"disponibile {min_disp}%"
    return False, "serbatoio sconosciuto"


def scegli_serbatoio(ruolo, misure, conf, escludi=()):
    """POLITICA DEI TRE BUDGET. Ritorna (nome, engine, model, provider, motivo)
    oppure (None, ...) se nessun serbatoio e' disponibile — caso in cui NON si fa
    failover (spostare un agente da una quota esaurita a un 402 non e' un rimedio).

    ORDINE 21/08/2026 (correzione §7 + A2A samantha_2): claude-flat PRIMA di
    deepseek-a-consumo finche' il settimanale all-models e' sotto soglia. La sera
    del 21/08 il settimanale era all'8%: mandare la flotta su DeepSeek col piatto
    pieno sarebbe stato pagare per qualcosa di gia' pagato."""
    rc = conf["ruoli"].get(ruolo) or conf["ruoli"]["sottoposto"]
    for nome in rc["serbatoi"]:
        if nome in escludi:
            continue
        ok, motivo = serbatoio_disponibile(nome, misure, conf)
        if not ok:
            continue
        sb = conf["serbatoi"][nome]
        return (nome, sb["engine"], rc["modelli"].get(nome), sb.get("provider"),
                f"{nome}: {motivo}")
    return None, None, None, None, "nessun serbatoio disponibile"


def azione_permessa(azione, ruolo, conf):
    """I coordinatori non si toccano in automatico: la loro riga e' false in conf
    e serve una conferma ROSSA di Manfredo. Un ruolo sconosciuto e' trattato come
    coordinatore (il caso piu' prudente), mai come exec."""
    classe = "coordinatore" if ruolo.startswith("coordinatore") else ruolo
    return bool(conf["azioni"].get(azione, {}).get(classe, False))


def in_lista_bianca(slug, tmux_window, conf):
    lb = conf.get("lista_bianca", {})
    if slug in lb.get("agenti", []):
        return True
    sess, _, win = (tmux_window or "").partition(":")
    if sess in lb.get("sessioni", []):
        return True
    return any(win.startswith(p) for p in lb.get("finestre_prefisso", []))


# ---------------------------------------------------------------------------
# CANALI — ROSSO=Telegram a Manfredo, GIALLO=A2A al coordinatore
#
# "Avvisa solo Samantha" e' esattamente il difetto che ha reso muto il
# self-healing la sera del 21/08: 42 allerte del resource_watchdog mandate via
# A2A a una Samantha che era giu' per quota. Un'allerta va a un CANALE, non a un
# agente che potrebbe essere la vittima del guasto che si sta segnalando.
# ---------------------------------------------------------------------------

def _token_bot(bot_key="betty_core"):
    try:
        import psycopg2
        conn = psycopg2.connect("dbname=betty_db user=betty_user host=localhost port=5432")
        cur = conn.cursor()
        cur.execute("SELECT token FROM telegram_bots WHERE bot_key = %s;", (bot_key,))
        row = cur.fetchone()
        conn.close()
        return row[0] if row else None
    except Exception as e:
        print(f"[guardiano] token bot non leggibile: {e}", flush=True)
        return None


def telegram_manfredo(testo, conf, dry=False):
    c = conf["canali"]
    corpo = f"{c['prefisso']} {testo}"
    if dry:
        print(f"[guardiano][DRY] TELEGRAM -> {corpo[:400]}", flush=True)
        return True
    tok = _token_bot(c.get("telegram_bot_key", "betty_core"))
    if not tok:
        return False
    try:
        import requests
        r = requests.post(f"https://api.telegram.org/bot{tok}/sendMessage", json={
            "chat_id": c["telegram_chat_id"],
            "message_thread_id": c["telegram_thread_id"],
            "text": corpo,
            "parse_mode": "Markdown",
        }, timeout=10)
        return r.status_code == 200
    except Exception as e:
        try:
            from secret_patterns import mask_secrets
            e = mask_secrets(str(e))
        except Exception:
            pass
        print(f"[guardiano] invio Telegram fallito: {e}", flush=True)
        return False


def a2a(destinatario, testo, dry=False):
    """Payload LEGGERO per contratto: dettagli su file, nel messaggio solo il path
    e due righe di brief (un listato incollato lo paga il destinatario ad ogni
    turno successivo, per sempre)."""
    if dry:
        print(f"[guardiano][DRY] A2A -> {destinatario}: {testo[:300]}", flush=True)
        return True
    try:
        r = subprocess.run(
            [os.path.join(CORE, "venv", "bin", "python3"),
             os.path.join(CORE, "send_a2a.py"), destinatario, testo],
            cwd=CORE, capture_output=True, text=True, timeout=180)
        return r.returncode == 0
    except Exception as e:
        print(f"[guardiano] A2A a {destinatario} fallito: {e}", flush=True)
        return False


# Decisioni che restano SOLO nel registro. 'azione_bloccata_da_flag' e' uno
# STATO che dura per costruzione: ripeterlo ad ogni ciclo e' il comportamento
# voluto, non un difetto — il ledger lo conserva senza svegliare nessuno.
# 'attendi_reset' NON sta piu' qui (23/08, ticket 5ded): un agente flat fermo
# in attesa di reset e' un evento di cui il pool deve sapere. Il rumore storico
# ('arrivava ad ogni ciclo') e' risolto dalla dedup per condizione con cooldown
# 3600s: l'alert parte una volta e si chiude al rientro (chiudi_alert_rientrati).
SOLO_LEDGER = {"azione_bloccata_da_flag"}

# Eventi del ledger che sono AFFERMAZIONI su un guasto (chiedono un'azione o un
# ticket). Tutto il resto — 'ciclo', 'misura_oltre_soglia', 'picco_cpu_atteso',
# 'decisione_saltata' — e' misura o battito: ripeterlo ad ogni ciclo e' il
# mestiere del guardiano, non un suo difetto. Vedi allarmi_dal_ledger().
EVENTI_CHE_CHIEDONO_AZIONE = {"decisione_segnalata", "azione", "ordine_fermo_rilevato",
                              "decisione_rifiutata_dati_mancanti", 
                              "decisione_rifiutata_mancanza_dichiarazione_misure"}

# ⚠️ ESCLUSIONI OBBLIGATORIE, ognuna per un motivo diverso e concreto:
#
# - 'ticket_meta_guardiano' e gli alert 'meta:*' sono le diagnosi F4 STESSE. Se
#   rientrassero fra gli allarmi da esaminare, il guardiano diagnosticherebbe se
#   stesso per essersi diagnosticato, all'infinito: un ciclo di retroazione che
#   cresce da solo ad ogni giro. Visto sul vivo: 13 auto-anomalie diventate 69.
# - 'azione_bloccata_da_flag' (e il suo alert 'bloccata:*') e' uno STATO che dura
#   per costruzione: con tutte le azioni a false, OGNI azione proposta viene
#   bloccata ad ogni ciclo. Ripeterlo e' il comportamento voluto, non un difetto.
# - 'attendi_reset' e le altre di SOLO_LEDGER: stessa ragione.
AZIONI_NON_ALLARME = {"ticket_meta_guardiano", "azione_bloccata_da_flag"}
CHIAVI_ALERT_DI_STATO = ("meta:", "bloccata:", "auto_anomalia:")

# I GIALLI che meritano davvero un A2A: eventi RARI, non stati che perdurano.
GIALLI_CON_CANALE = {"segnala_servizio_runaway", "segnala_consegna_ferma",
                     "riallineo_ambiguo", "stop_retry_loop", "sospendi_runaway",
                     "failover", "rilancia_cli_morto", "sblocca_errore_api",
                     "sblocca_consegna_ferma",
                     # 23/08 (ticket 5ded): 'attendi_reset' era SOLO_LEDGER — un
                     # agente flat fermo fino al reset restava muto. Ora va al
                     # pool una volta per condizione (dedup 3600s + chiusura al
                     # rientro): segnala il blocco senza assordare.
                     "attendi_reset",
                     # F4: la diagnosi su un guardiano difettoso e l'ordine fermo
                     # nella casella sono EVENTI rari, non stati che perdurano —
                     # e sono inutili se restano solo nel ledger, perche' nessun
                     # umano legge il ledger finche' non e' gia' successo qualcosa.
                     "ticket_meta_guardiano", "ordine_fermo_in_casella"}


def allerta(livello, chiave, testo, conf, dedup, dry=False, coordinatore=None):
    """ROSSO -> Telegram diretto a Manfredo. GIALLO -> A2A al coordinatore
    interessato. VERDE -> nulla. Sempre con dedup per condizione."""
    if livello == "VERDE":
        return False
    composita = f"{livello}:{chiave}"
    _ALERT_ATTIVI_CICLO.add(composita)          # la condizione e' viva in questo ciclo
    if not dedup.consenti(composita):
        # F-A: la ripetizione soppressa si VEDE nel log, con contatore ed eta'
        # dall'ultimo invio. Un alert senza 'dedup' ne' 'chiuso' e' un alert
        # di cui nessuno sa piu' nulla.
        try:
            ultimo = dedup.ultimo_invio(composita)
            eta = round(time.time() - ultimo) if ultimo else None
            ap = _registra_alert_aperto(composita, livello, chiave,
                                        ora=ultimo or time.time())
            ap["dedup"] = dedup.soppresse(composita)
            log_testo(_modulo_da_chiave(chiave), "DEBUG", "dedup", chiave,
                      f"ripetizione soppressa n={ap['dedup']} (cooldown {dedup.cooldown_s}s)",
                      "dedup per condizione", eta, composita)
        except Exception as e:
            print(f"[guardiano] log dedup fallito: {e}", flush=True)
        return False
    try:
        _registra_alert_aperto(composita, livello, chiave)
    except Exception:
        pass
    if livello == "ROSSO":
        esito = telegram_manfredo(testo, conf, dry=dry)
        canale = "telegram_manfredo"
    else:
        # I GIALLI del pool vanno alla CASELLA del pool, non a un'istanza: se
        # scrivessi a Samantha-2 e fosse lei quella ferma, l'avviso finirebbe
        # nella stessa coda che sto segnalando come bloccata.
        dest = coordinatore or conf["canali"].get("coordinatore_default", "samantha")
        esito = a2a(dest, f"[A2A_FROM:guardiano] [A2A_TYPE:report]\n{testo}", dry=dry)
        canale = f"a2a:{dest}"
    ledger("alert", livello=livello, chiave=chiave, canale=canale,
           consegnato=esito, dry=dry, testo=testo[:500])
    return esito


# ---------------------------------------------------------------------------
# RIALLINEO DB <-> REALTA'  (F1b)
# ---------------------------------------------------------------------------

# Alias di modello accettati da Claude Code senza provider esterno. Scrivere
# qui dentro un 'deepseek-v4-flash' con provider nullo produce un --model che il
# CLI scarta IN SILENZIO (engine_model_scartato.log): l'agente riparte sul
# modello di default e nessuno se ne accorge.
MODELLI_CLAUDE_NATIVI = {"sonnet", "opus", "fable", "haiku"}
CHIAVI_MIGRAZIONE = ("migrato_", "pre_opus_", "fix_model_")


def record_in_lavorazione(d, ore=24):
    """True se qualcun altro ha toccato il record di recente (chiavi migrato_*,
    pre_opus_*, fix_model_*). Un record appena impostato a opus e NON ANCORA
    RIAVVIATO mostra il vecchio modello nel processo: riallinearlo significa
    cancellare la migrazione di un altro con un dato che e' vero solo perche'
    l'agente non e' ancora ripartito."""
    limite = time.time() - ore * 3600
    for k, v in (d or {}).items():
        if not any(k.startswith(pre) for pre in CHIAVI_MIGRAZIONE):
            continue
        try:
            if isinstance(v, (int, float)):
                if float(v) > limite:
                    return True
            elif isinstance(v, dict) and v:
                ultimo = max(v.keys())
                if datetime.fromisoformat(str(ultimo)).timestamp() > limite:
                    return True
            else:
                if datetime.fromisoformat(str(v)).timestamp() > limite:
                    return True
        except Exception:
            return True          # marcatore illeggibile: si sta prudenti
    return False


def model_scrivibile(model, engine, provider):
    """False se scriverlo produrrebbe un --model che il CLI scarta in silenzio."""
    if engine != "claude" or not model:
        return True
    if provider:
        return True              # con un provider esterno il model e' suo
    return model in MODELLI_CLAUDE_NATIVI


def riallinea_db(agenti, conf, dry=False, conferme=None):
    """Se engine/model in Postgres != processo reale, il DB si aggiorna al
    processo — mai il contrario. Il valore vecchio NON si perde: finisce in
    `guardiano_riallineo_<data>` dentro lo stesso record, cosi' resta ricostruibile
    chi lo aveva messo li' e quando (marcare, non cancellare)."""
    fatti = []
    try:
        import agent_db
        db = agent_db.get_db()
    except Exception as e:
        ledger("riallineo_db_errore", errore=str(e)[:200])
        return fatti
    # Quante RECORD puntano alla stessa finestra. Se sono due o piu' non si sa
    # quale dei due la stia usando davvero, e riallineare significa scrivere il
    # motore di un agente sul record di un altro. Successo il 21/08: aurelio_1 e
    # aurelio condividono AGY-AURELIO, una migrazione in corso stava mettendo
    # claude/opus su 'aurelio', e il guardiano l'ha copiato su 'aurelio_1'.
    per_win, quanti_per_win = {}, {}
    for slug, d in db.items():
        tw = ((d or {}).get("tmux_window") or "").lower()
        if not tw:
            continue
        quanti_per_win[tw] = quanti_per_win.get(tw, 0) + 1
        per_win.setdefault(tw, slug)
    sospesi = conf.get("riallineo_sospeso", {})
    ora_ts = time.time()
    chiave_backup = "guardiano_riallineo_" + datetime.now().strftime("%Y%m%d")
    for a in agenti:
        if not a.get("cli_vivo"):
            continue
        tw_l = a["tmux_window"].lower()
        slug = per_win.get(tw_l)
        if not slug:
            continue
        if quanti_per_win.get(tw_l, 0) > 1:
            ledger("riallineo_saltato", motivo="finestra_condivisa",
                   tmux_window=a["tmux_window"], record_che_la_puntano=quanti_per_win[tw_l],
                   engine_reale=a["engine_reale"], model_reale=a["model_reale"])
            fatti.append({"agente": slug, "tmux_window": a["tmux_window"],
                          "saltato": "finestra_condivisa", "livello": "GIALLO",
                          "record_che_la_puntano": quanti_per_win[tw_l]})
            continue
        scad = sospesi.get(slug)
        if scad and ora_ts < scad:
            ledger("riallineo_saltato", motivo="sospeso", agente=slug,
                   fino_a=datetime.fromtimestamp(scad).isoformat(timespec="seconds"))
            continue
        d = db.get(slug) or {}
        eng_db, mod_db = d.get("engine"), d.get("model")
        eng_re, mod_re = a["engine_reale"], a["model_reale"]
        diff = {}
        if eng_re and eng_db != eng_re:
            diff["engine"] = {"db": eng_db, "reale": eng_re}
        if mod_re and mod_db != mod_re:
            diff["model"] = {"db": mod_db, "reale": mod_re}
        if not diff:
            if conferme is not None:
                conferme.dati.pop(f"riall:{slug}", None)
            continue
        if record_in_lavorazione(d):
            ledger("riallineo_saltato", motivo="record_in_lavorazione", agente=slug,
                   differenze=diff)
            fatti.append({"agente": slug, "saltato": "record_in_lavorazione",
                          "tmux_window": a["tmux_window"]})
            continue
        if "model" in diff and not model_scrivibile(mod_re, eng_re or eng_db,
                                                    a.get("provider_reale")):
            ledger("riallineo_saltato", motivo="model_non_valido_per_claude",
                   agente=slug, model_reale=mod_re, provider=a.get("provider_reale"))
            fatti.append({"agente": slug, "saltato": "model_non_valido_per_claude",
                          "tmux_window": a["tmux_window"]})
            continue
        # La stessa differenza deve reggere per due letture a 10+ minuti: una
        # sola lettura puo' cadere in mezzo a un riavvio, quando il processo
        # vecchio e il record nuovo dicono cose diverse ed entrambi hanno ragione.
        if conferme is not None:
            durata = conferme.aggiorna(f"riall:{slug}", True)
            if durata < conf["soglie"].get("riallineo_conferma_s", 600):
                ledger("riallineo_saltato", motivo="in_attesa_di_conferma",
                       agente=slug, differenze=diff, visto_da_s=round(durata))
                continue
        voce = {"agente": slug, "tmux_window": a["tmux_window"], "differenze": diff,
                "provider_reale": a.get("provider_reale")}
        fatti.append(voce)
        if dry:
            ledger("riallineo_db_rilevato", dry=True, **voce)
            continue
        try:
            d.setdefault(chiave_backup, {})
            d[chiave_backup][ora_iso()] = {"engine": eng_db, "model": mod_db}
            if "engine" in diff:
                d["engine"] = eng_re
            if "model" in diff:
                d["model"] = mod_re
            db[slug] = d
            agent_db.save_db()
            ledger("riallineo_db", chiave_backup=chiave_backup, **voce)
        except Exception as e:
            ledger("riallineo_db_errore", agente=slug, errore=str(e)[:200])
    return fatti


# ---------------------------------------------------------------------------
# TABELLA DECISIONI — F2 (quota) e F3 (RAM/CPU)
#
# Funzioni PURE: ricevono le misure, tornano una lista di decisioni. Nessuna
# tocca il sistema. Sono queste che i test unitari verificano — la parte che
# esegue e' volutamente sottile.
# ---------------------------------------------------------------------------

# Prefisso data "gg/mm/aaaa hh:mm" usato dalle note descrittive scritte a mano
# in migrato_opus/migrazione_ts (es. exec_migra5_opus dal 21/08/2026) quando il
# valore non e' un timestamp ISO pulito.
_RE_DATA_PREFISSO = re.compile(r"^\d{1,2}/\d{1,2}/\d{4} \d{1,2}:\d{2}")


def in_migrazione(slug, conf, db=None):
    """True se l'agente e' in mezzo a una migrazione: elencato in conf
    'in_migrazione', oppure con un marcatore 'migrato_opus' recente sul record.
    Un agente che qualcun altro sta spostando NON va toccato: due mani sullo
    stesso record producono un motore che non e' quello di nessuno dei due."""
    if slug in (conf.get("in_migrazione") or []):
        return True
    d = (db or {}).get(slug) or {}
    for chiave in ("migrato_opus", "migrazione_ts"):
        val = d.get(chiave)
        if not val:
            continue
        try:
            ts = float(val) if isinstance(val, (int, float)) else \
                datetime.fromisoformat(str(val)).timestamp()
        except Exception:
            # Non un timestamp pulito (es. nota descrittiva in testo libero
            # scritta da un exec di migrazione). Proviamo a recuperare una
            # data leggibile dall'INIZIO della stringa (formato "gg/mm/aaaa
            # hh:mm..."); se anche questo fallisce, il marcatore e' vecchio/
            # non affidabile per costruzione e NON deve bloccare per sempre
            # il self-healing (bug reale: prima qui si tornava sempre True).
            m = _RE_DATA_PREFISSO.match(str(val))
            if not m:
                continue
            try:
                ts = datetime.strptime(m.group(0), "%d/%m/%Y %H:%M").timestamp()
            except Exception:
                continue
        if time.time() - ts < conf["soglie"].get("migrazione_grazia_s", 3600):
            return True
    return False


def decisioni_quota(quota, conf, persistenza, classi, misure, db=None):
    """Una decisione per agente in quota. Tipi: stop_retry_loop, failover, attendi."""
    dec = []
    s = conf["soglie"]
    for a in quota["agenti"]:
        _slug_mig = classi.get(a["tmux_window"], {}).get("slug")
        if _slug_mig and in_migrazione(_slug_mig, conf, db):
            ledger("decisione_saltata", motivo="in_migrazione", agente=_slug_mig,
                   tmux_window=a["tmux_window"])
            continue
        problemi = a.get("problemi") or []
        if not problemi:
            persistenza.aggiorna(f"retry:{a['tmux_window']}", False)
            persistenza.aggiorna(f"quota_stallo_cpu:{a['tmux_window']}", False)
            continue
        tipi = {p["tipo"] for p in problemi}
        # attesa_annunciata da SOLA = il CLI riprende da solo: nessuna decisione.
        # Con quota_esaurita/saldo_esaurito accanto invece la quota VINCE (il
        # banner di reset di sessione Claude Code dice 'Continuing' ma resta
        # fermo fino al reset — coerente con analizza_pannello).
        if "attesa_annunciata" in tipi and not (tipi & {"quota_esaurita",
                                                        "saldo_esaurito"}):
            ledger("decisione_saltata", motivo="attesa_annunciata_dal_cli",
                   tmux_window=a["tmux_window"],
                   agente=classi.get(a["tmux_window"], {}).get("slug"))
            continue
        in_quota = bool(tipi & {"quota_esaurita", "saldo_esaurito"})
        if not in_quota:
            continue
        slug = classi.get(a["tmux_window"], {}).get("slug")
        ruolo = classi.get(a["tmux_window"], {}).get("ruolo", "sottoposto")
        cpu = a.get("cpu_pct")
        # GUARDIA 23/08 (falso positivo REALE, ticket 5ded + auto-diagnosi 7db0):
        # un agente con CPU sopra soglia sta LAVORANDO — il pattern quota nel suo
        # pannello e' testo di lavoro (log, citazione, debug di un limite), non
        # un CLI bloccato. Un CLI VERO in attesa di reset ha CPU ~0 (processo
        # fermo, come nel caso sveva del 23/08). Quindi nessuna decisione di
        # blocco (failover/attendi/nessun_serbatoio) su chi sta lavorando.
        # Questo rende anche il vecchio stop_retry_loop su quota inservibile:
        # richiedeva CPU alta, che e' proprio il segnale di chi lavora.
        brucia = cpu is not None and cpu > s["cpu_retry_loop_pct"]
        if brucia:
            # FIX 31/08/2026 (mandato samantha_2, incidente reale samantha_1
            # >45 min bloccata su 'Individual quota reached' in loop di retry):
            # CPU alta NON e' SEMPRE "sta lavorando" — un CLI in loop di retry
            # su un errore di quota irrecuperabile brucia CPU senza produrre
            # alcun progresso reale. Il testo del pannello pero' NON resta
            # identico ad ogni ciclo come si potrebbe pensare (verificato sul
            # ledger reale di oggi): il countdown 'Resets in Nh' e l'Error ID
            # cambiano a ogni tentativo, quindi un confronto per hash del testo
            # grezzo non avrebbe MAI rilevato lo stallo. Si confronta invece la
            # CONDIZIONE (quota_esaurita/saldo_esaurito insieme a CPU sopra
            # soglia): se dura ININTERROTTA da almeno cpu_retry_loop_persistenza_s
            # (gia' in conf, 300s, mai usata finora in questo file) e' un loop
            # di retry vero, non lavoro — si prosegue comunque nella valutazione
            # di failover sotto, RIUSANDO la stessa scelta/applicazione del
            # serbatoio, invariata. Il caso normale (lavoro vero, breve menzione
            # di '429' nel log che scorre via) resta protetto come prima: non
            # dura abbastanza per superare la soglia.
            stallo_s = persistenza.aggiorna(f"quota_stallo_cpu:{a['tmux_window']}", True)
            soglia_stallo = s.get("cpu_retry_loop_persistenza_s", 300)
            if stallo_s < soglia_stallo:
                ledger("decisione_saltata", motivo="quota_nel_testo_di_lavoro_cpu_alta",
                       tmux_window=a["tmux_window"], agente=slug, cpu_pct=cpu,
                       stallo_s=round(stallo_s))
                continue
            ledger("decisione_forzata", motivo="quota_stallo_stesso_errore_cpu_alta",
                   tmux_window=a["tmux_window"], agente=slug, cpu_pct=cpu,
                   stallo_s=round(stallo_s))
        else:
            persistenza.aggiorna(f"quota_stallo_cpu:{a['tmux_window']}", False)
        reset_min = a.get("reset_minuti")
        base = {"agente": slug, "tmux_window": a["tmux_window"], "ruolo": ruolo,
                "coordinatore": classi.get(a["tmux_window"], {}).get("coordinatore"),
                "engine_reale": a["engine_reale"], "provider_reale": a.get("provider_reale"),
                "problemi": sorted(tipi), "cpu_pct": cpu, "reset_minuti": reset_min,
                "cpu_sopra_soglia_da_s": 0,
                "_misure_usate": {"cpu_pct": cpu, "engine_reale": a.get("engine_reale")}}
        # ⚠️ GUARDIA DEL 21/08/2026 (incidente 19:46-19:50, tre failover a
        # pagamento): un agente su CLAUDE la cui SESSIONE 5h e' piena, mentre il
        # SETTIMANALE regge, non va spostato da nessuna parte — si aspetta il
        # reset, che arriva in ore, e l'abbonamento flat resta quello che paga.
        # Prima di questa riga la catena guardava un solo numero aggregato e
        # leggeva "claude 86%" come "claude finito".
        motore_claude = (a.get("engine_reale") == "claude"
                         and not a.get("provider_reale"))
        if motore_claude and misure.get("claude_attendi_reset"):
            nota = misure.get("claude_attendi_reset_nota") or "sessione 5h piena"
            dec.append(dict(base, azione="attendi_reset", livello="GIALLO",
                            valore=misure.get("claude_sessione_pct"),
                            fonte=misure.get("claude_fonte"),
                            eta_dato_s=misure.get("claude_lettura_eta_s"),
                            perche=f"{nota} — nessun failover: la quota che decide "
                                   f"e' il settimanale "
                                   f"({misure.get('claude_settimanale_pct')}%)"))
            continue

        attesa_lunga = reset_min is None or reset_min > s["reset_quota_attesa_max_min"]
        if attesa_lunga:
            nome, eng, mod, prov, motivo = scegli_serbatoio(
                ruolo, misure, conf, escludi=(a.get("provider_reale") or a["engine_reale"],))
            if nome is None:
                dec.append(dict(base, azione="nessun_serbatoio", livello="ROSSO",
                                perche="quota esaurita e NESSUN serbatoio disponibile: "
                                       "spostarlo ora vorrebbe dire mandarlo su un altro muro"))
            else:
                dec.append(dict(base, azione="failover", livello="GIALLO",
                                serbatoio=nome, engine_nuovo=eng, model_nuovo=mod,
                                provider_nuovo=prov,
                                perche=f"reset {'non annunciato' if reset_min is None else str(reset_min)+' min'} "
                                       f"> {s['reset_quota_attesa_max_min']} min; scelto {motivo}"))
        else:
            dec.append(dict(base, azione="attendi_reset", livello="GIALLO",
                            perche=f"reset fra {reset_min} min (<= {s['reset_quota_attesa_max_min']}): "
                                   f"attendere costa meno di un failover con salva-stato"))
    return dec


def decisioni_errori_api(quota, conf, persistenza, classi):
    """F1c. Agente fermo su errore API/safeguard/connessione con messaggi pendenti
    nella inbox A2A: se il CLI e' fermo al prompt idle dopo un errore e ci sono messaggi
    non presi in carico, emette sblocca_errore_api per eseguire auto-remediation attiva."""
    dec = []
    for a in quota.get("agenti", []):
        tw = a.get("tmux_window")
        slug = classi.get(tw, {}).get("slug")
        if not slug:
            continue
        problemi = a.get("problemi") or []
        tipi_err = {p["tipo"] for p in problemi if p.get("tipo") in ("errore_api", "blocco_sicurezza", "errore_connessione", "risposta_vuota")}
        if not tipi_err:
            persistenza.aggiorna(f"errore_api:{tw}", False)
            continue
        cpu = a.get("cpu_pct")
        if cpu is not None and cpu > conf.get("soglie", {}).get("cpu_retry_loop_pct", 20):
            continue
        pendenti = _pendenti_a2a(slug)
        ha_errore = bool(tipi_err and pendenti > 0)
        durata = persistenza.aggiorna(f"errore_api:{tw}", ha_errore)
        if ha_errore and durata >= 60:
            dec.append({
                "azione": "sblocca_errore_api",
                "livello": "GIALLO",
                "agente": slug,
                "tmux_window": tw,
                "ruolo": classi.get(tw, {}).get("ruolo", "sottoposto"),
                "coordinatore": classi.get(tw, {}).get("coordinatore"),
                "problemi": sorted(tipi_err),
                "in_attesa": pendenti,
                "engine": a.get("engine_reale"),
                "_misure_usate": {"cli_vivo": a.get("cli_vivo"), "cpu_pct": cpu},
                "perche": f"rilevato {','.join(sorted(tipi_err))} a schermo con {pendenti} "
                          f"messaggi non presi in carico in a2a/{slug}/inbox/ da {round(durata)}s; sblocco attivo"
            })
    return dec


def decisioni_rientro(classi, misure, conf, db=None):
    """Rientro al flat (failover INVERSO). 25/08/2026 (MSG 15953/15971, Manfredo):
    quando claude flat torna disponibile (settimanale sotto soglia E sessione 5h
    libera), riporta al flat gli agenti che erano stati spostati su deepseek o agy.
    Riconosce:
      - record con motore_originale='claude'
      - agenti presenti in failover_quota_state.json con motore_originale='claude'
      - agenti su provider='deepseek' non nativi
      - agenti su agy migrati da claude
    Salva-stato prima in conv_id_by_engine e rilancio fresco certificato.
    Rispetta la guardia cpu-alta (non tocca chi sta lavorando) e la lista in_migrazione."""
    dec = []
    sett = misure.get("claude_settimanale_pct")
    sess = misure.get("claude_sessione_pct")
    s_ = conf.get("soglie", {})
    soglia_sess = s_.get("claude_sessione_soglia_pct", 90.0)
    soglia_sett = s_.get("claude_settimanale_soglia_pct", 70.0)
    # Tetto di sicurezza: non rientrare se sessione piena, settimanale >= soglia_sett (70%),
    # oppure se le metriche non sono disponibili (fail-safe verso il NON agire: sess is None / sett is None).
    # 26/08/2026 (decisione Manfredo MSG 17:32 / samantha_1 & exec_cancello_none_safe):
    # None-safe gate: "non lo so" vale come "NON rientrare", evitando failover distruttivi a vuoto.
    if sess is None or sess >= soglia_sess:
        return dec
    if sett is None or sett >= soglia_sett:
        return dec
    if misure.get("claude_in_reset"):
        return dec

    if db is None:
        try:
            import agent_db as _adb
            db = _adb.get_db()
        except Exception:
            return dec

    # Carica anche lo stato da failover_quota_state.json se presente
    agenti_fq_migrati = {}
    try:
        if os.path.exists("/tmp/betty_docs/failover_quota_state.json"):
            with open("/tmp/betty_docs/failover_quota_state.json", "r", encoding="utf-8") as f_st:
                agenti_fq_migrati = json.load(f_st).get("agenti_migrati", {})
    except Exception:
        pass

    for tw, info in classi.items():
        slug = info.get("slug")
        if not slug:
            continue
        if in_migrazione(slug, conf, db):
            continue

        eng_reale = info.get("engine_reale")
        prov_reale = info.get("provider_reale")

        d = db.get(slug) if hasattr(db, "get") else None
        if not d:
            continue

        db_eng = d.get("engine")
        db_prov = d.get("provider")

        # Verifica se l'agente è GIÀ su Claude Flat (None-safe con fallback su DB)
        eng_eff = eng_reale or db_eng
        prov_eff = prov_reale or db_prov
        e_gia_su_flat = (eng_eff == "claude" and not prov_eff)
        if e_gia_su_flat:
            continue

        # Verifica se l'agente ha diritto al rientro su Claude Flat
        info_fq = agenti_fq_migrati.get(slug, {})
        motore_orig = d.get("motore_originale") or info_fq.get("motore_originale")
        prov_orig = d.get("provider_originale") or info_fq.get("provider_originale")

        candidato_rientro = False
        if motore_orig == "claude" and not prov_orig:
            # 26/08/2026: rientro solo se effettivamente su motore/provider alternativo
            if prov_eff or eng_eff == "agy":
                candidato_rientro = True
        elif db_prov == "deepseek" or prov_reale == "deepseek":
            # Agente su DeepSeek temporaneo
            if motore_orig in (None, "claude"):
                candidato_rientro = True
        elif eng_reale == "agy" or db_eng == "agy":
            # Agente su AGY che ha come motore di partenza claude
            if motore_orig == "claude":
                candidato_rientro = True

        if not candidato_rientro:
            continue

        cpu = info.get("cpu_pct")
        if cpu is not None and cpu > s_.get("cpu_retry_loop_pct", 20):
            ledger("rientro_saltato_cpu_alta", agente=slug, tmux_window=tw, cpu_pct=cpu)
            continue  # sta lavorando: non si tocca

        ruolo = info.get("ruolo", "sottoposto")
        mod_orig = d.get("model_originale") or info_fq.get("model_originale")
        mod = mod_orig or (conf.get("ruoli", {}).get(ruolo, {}).get("modelli", {}) or {}).get("claude") or "sonnet"

        dec.append({
            "agente": slug, "tmux_window": tw,
            "ruolo": ruolo,
            "coordinatore": info.get("coordinatore"),
            "engine_reale": eng_reale, "provider_reale": prov_reale,
            "problemi": ["rientro_flat"], "cpu_pct": cpu, "reset_minuti": None,
            "cpu_sopra_soglia_da_s": 0,
            "azione": "failover", "livello": "GIALLO",
            "serbatoio": "claude", "engine_nuovo": "claude", "model_nuovo": mod,
            "provider_nuovo": None,
            "_misure_usate": {"claude_settimanale_pct": sett, "claude_sessione_pct": sess},
            "perche": f"rientro al flat: {slug} su {eng_reale}{f'/{prov_reale}' if prov_reale else ''}, flat claude disponibile (sess={sess if sess is not None else 'N/D'}%, sett={sett if sett is not None else 'N/D'}%)",
        })
    return dec


def decisioni_capacita(cpu, quota, conf, persistenza, classi):
    """La flotta e' piu' grande della macchina. NON e' risolvibile in automatico
    (samantha_2, 21/08: 7,3 core saturi su 8, senza un singolo colpevole) — e'
    una decisione di DIMENSIONAMENTO, cioe' di Manfredo. Il guardiano porta il
    dato e la lista di chi spegnere per primo, non spegne mezza flotta da solo."""
    s_ = conf["soglie"]
    sat = cpu.get("saturazione_pct")
    if sat is None:
        return []
    oltre = sat > s_["cpu_saturazione_pct"]
    durata = persistenza.aggiorna("saturazione", oltre)
    if not oltre or durata < s_["load5_persistenza_s"]:
        return []
    candidati = []
    for a in quota["agenti"]:
        info = classi.get(a["tmux_window"], {})
        if info.get("ruolo") != "exec" or info.get("always_on"):
            continue
        if in_lista_bianca(info.get("slug", ""), a["tmux_window"], conf):
            continue
        candidati.append({"agente": info.get("slug"), "tmux_window": a["tmux_window"],
                          "cpu_pct": a.get("cpu_pct"), "rss_mb": a.get("rss_mb")})
    candidati.sort(key=lambda c: -((c["cpu_pct"] or 0) + (c["rss_mb"] or 0) / 100.0))
    return [{"azione": "segnala_flotta_oltre_capacita", "livello": "ROSSO",
             "agente": None, "ruolo": "coordinatore",
             "saturazione_pct": sat, "core": cpu.get("core"),
             "per_gruppo": cpu.get("per_gruppo"),
             "candidati_spegnimento": candidati[:5],
             "perche": f"la flotta usa {cpu.get('totale_pct')}% di CPU su {cpu.get('core')} "
                       f"core ({sat}% di saturazione) da {round(durata)}s. Nessun singolo "
                       f"colpevole: e' una decisione di dimensionamento, non un guasto. "
                       f"Primi candidati allo spegnimento: "
                       f"{', '.join(c['agente'] or c['tmux_window'] for c in candidati[:5]) or 'nessun exec spegnibile'}"}]


def decisioni_liveness(quota, conf, persistenza, classi, db=None):
    """CLI morto dentro una finestra viva: l'agente sembra acceso a tutti e non lo
    e'. Oggi lo copre l'health_cron, ma SOLO per gli always_on e rilanciando col
    motore del DB — che il 21/08 era sbagliato per 7 record su 20. Qui il rilancio
    avviene DOPO il riallineo dello stesso ciclo, quindi col motore REALE."""
    dec = []
    s = conf.get("soglie", {})
    soglia_cpu = s.get("liveness_cpu_soglia_pct", 2.0)
    cicli_richiesti = s.get("liveness_conferme_cicli", 2)
    soglia_sec = (cicli_richiesti - 1) * max(1, conf.get("intervallo_ciclo_s", 60) - 10)

    for a in quota["agenti"]:
        tw = a["tmux_window"]
        vivo = a.get("cli_vivo")
        
        # Protezione: se la CPU del sottoalbero e' >= soglia, consideriamolo VIVO.
        cpu_pct = a.get("cpu_pct")
        cpu_alta = cpu_pct is not None and cpu_pct >= soglia_cpu
        
        morto = not vivo and not cpu_alta
        durata_morto = persistenza.aggiorna(f"liveness_morto:{tw}", morto)
        
        if not morto or durata_morto < soglia_sec:
            continue
            
        info = classi.get(tw, {})
        slug = info.get("slug")
        if not slug:
            continue          # finestra senza record: non e' un agente da rilanciare
            
        dec.append({"azione": "rilancia_cli_morto",
                    "livello": "ROSSO" if info.get("always_on") else "GIALLO",
                    "agente": slug, "tmux_window": tw,
                    "ruolo": info.get("ruolo", "sottoposto"),
                    "always_on": info.get("always_on"),
                    "coordinatore": info.get("coordinatore"),
                    "pane_dead": a.get("pane_dead"),
                    "_misure_usate": {"cli_vivo": vivo, "cpu_pct": cpu_pct},
                    "perche": f"finestra aperta ma nessun CLI vivo (nessun processo CLI e CPU < {soglia_cpu}%) "
                              f"confermato da {round(durata_morto)}s; rilancio col motore REALE"})

    # Fix 29/08/2026 (mandato samantha_2, report diagnosi radio, §6 Fix B):
    # il ciclo sopra parte da quota["agenti"], che viene da 'tmux list-panes
    # -a' — un agente la cui SESSIONE INTERA e' sparita (non solo la
    # finestra) non ci finisce mai dentro, quindi non e' mai vista da questa
    # funzione: 6 always_on esposti (verificato nel report). Qui si confronta
    # il roster Postgres always_on/non-dismesso con le finestre tmux VIVE per
    # trovare chi manca del tutto.
    #
    # Scelta deliberata di scope ridotto rispetto al diff indicativo del
    # report: SOLO segnalazione (nessun esecutore in ESECUTORI per queste due
    # azioni), MAI rilancio automatico. Il report stesso segnalava rischio
    # medio non banale su un rilancio automatico che ricrea un'intera
    # sessione (mai testato in dry=True per un ciclo intero) — "meglio 3 fix
    # solidi che 4 di cui uno rotto" vale anche dentro un singolo fix.
    # L'azione "rilancia_cli_morto" gia' esistente resta riservata al solo
    # caso "finestra viva, CLI morto dentro" per cui e' stata pensata e
    # testata.
    if db:
        finestre_vive = {a["tmux_window"] for a in quota["agenti"]}
        for slug, d in db.items():
            if not isinstance(d, dict) or str(d.get("dismesso")).lower() == "true" or d.get("routing_only") or d.get("pool_di") or not d.get("always_on"):
                continue
            tw = d.get("tmux_window")
            if not tw or tw in finestre_vive:
                continue
            durata = persistenza.aggiorna(f"sessione_sparita:{tw}", True)
            if durata < soglia_sec:
                continue          # si misura, non si allarma al primo giro
            ruolo = classifica_agente(slug, d, conf)
            coordinatore = coordinatore_di(slug, tw, db, conf)
            base = {"agente": slug, "tmux_window": tw, "ruolo": ruolo,
                    "always_on": True, "coordinatore": coordinatore,
                    "sessione": tw.split(":")[0]}
            if d.get("spento_nota"):
                # Vista, ma con uno spegnimento intenzionale dichiarato sul
                # record (stessa convenzione di self_healing_stato.py): non
                # deve urlare — solo restare tracciabile nel ledger/stato.
                # Questo E' il caso di 'radio' oggi: la nota resta stale
                # (Manfredo ha scelto di NON riaccenderla in questo giro), e
                # quindi anche con questo fix l'allarme resta zittito finche'
                # non viene riaccesa — solo la RIACCENSIONE futura chiude la
                # nota (Fix A).
                dec.append(dict(base, azione="segnala_sessione_sparita_silenziata",
                                 livello="GIALLO",
                                 perche=f"sessione '{tw.split(':')[0]}' di {slug} non esiste piu' "
                                        f"da {round(durata)}s, ma il record ha 'spento_nota' "
                                        f"(spegnimento dichiarato intenzionale): vista, non allarmata"))
            else:
                dec.append(dict(base, azione="segnala_sessione_sparita",
                                 livello="ROSSO",
                                 perche=f"sessione '{tw.split(':')[0]}' di {slug} (always_on) non "
                                        f"esiste piu' da {round(durata)}s: nessuna finestra da "
                                        f"rilanciare, va ricreata l'intera sessione"))
    return dec


def decisioni_ram(ram, quota, conf, classi):
    """RAM sotto soglia: si spengono gli exec non-always_on piu' PESANTI, in ordine
    di peso. Coordinatori e always_on MAI in automatico: solo ROSSO a Manfredo."""
    dec = []
    s = conf["soglie"]
    avail = ram.get("disponibile_mb")
    if avail is None or avail >= s["ram_disponibile_mb"]:
        return dec
    livello = "ROSSO" if avail < s["ram_rosso_mb"] else "GIALLO"
    candidati = []
    for a in quota["agenti"]:
        info = classi.get(a["tmux_window"], {})
        if info.get("ruolo") != "exec" or info.get("always_on"):
            continue
        if in_lista_bianca(info.get("slug", ""), a["tmux_window"], conf):
            continue
        candidati.append((a.get("rss_mb") or 0, info.get("slug"), a["tmux_window"]))
    candidati.sort(reverse=True)
    for rss, slug, tw in candidati:
        dec.append({"azione": "spegni_per_ram", "livello": livello, "agente": slug,
                    "tmux_window": tw, "ruolo": "exec", "rss_mb": rss,
                    "coordinatore": classi.get(tw, {}).get("coordinatore"),
                    "_misure_usate": {"ram_disponibile_mb": avail},
                    "perche": f"RAM disponibile {avail}MB < {s['ram_disponibile_mb']}MB; "
                              f"exec non-always_on piu' pesante ({rss}MB)"})
        break                      # uno per ciclo: si rimisura prima di insistere
    if not candidati:
        dec.append({"azione": "segnala_ram", "livello": "ROSSO", "agente": None,
                    "ruolo": "coordinatore", "ram_disponibile_mb": avail,
                    "perche": f"RAM disponibile {avail}MB sotto soglia ma NESSUN exec "
                              f"spegnibile: restano solo coordinatori/always_on, "
                              f"che non si toccano in automatico"})
    return dec


def decisioni_cpu(cpu, quota, conf, persistenza, classi):
    """load5 sostenuto: si elencano i runaway e si sospendono i SOLI exec."""
    dec = []
    s = conf["soglie"]
    load5 = cpu.get("load5")
    alto = load5 is not None and load5 > s["load5"]
    durata = persistenza.aggiorna("load5_alto", alto)
    # I runaway si CONTANO SEMPRE, anche quando il load e' ancora sotto soglia.
    # Tenerli dietro al gate del load5 significava far partire i 5 minuti di CPU
    # solo DOPO i 10 minuti di load: 15 minuti prima di poter agire su un loop
    # che stava bruciando CPU dal primo istante. Il conteggio e' una misura, non
    # un'azione: costa nulla e non ha motivo di aspettare.
    runaway = []
    for a in quota["agenti"]:
        c = a.get("cpu_pct")
        acceso = c is not None and c > s["cpu_runaway_pct"]
        d = persistenza.aggiorna(f"runaway:{a['tmux_window']}", acceso)
        if acceso and d >= s["cpu_runaway_persistenza_s"]:
            runaway.append((c, a["tmux_window"], d))
    if not alto or durata < s["load5_persistenza_s"]:
        return dec          # load non ancora sostenuto: si misura, non si agisce
    runaway.sort(reverse=True)
    for c, tw, d in runaway:
        info = classi.get(tw, {})
        slug = info.get("slug")
        if info.get("ruolo") == "exec" and not in_lista_bianca(slug or "", tw, conf) \
                and not info.get("always_on"):
            dec.append({"azione": "sospendi_runaway", "livello": "GIALLO", "agente": slug,
                        "tmux_window": tw, "ruolo": "exec", "cpu_pct": c,
                        "coordinatore": info.get("coordinatore"),
                        "_misure_usate": {"load5": load5, "cpu_pct": c},
                        "perche": f"load5 {load5} > {s['load5']} da {round(durata)}s e "
                                  f"CPU {c}% da {round(d)}s"})
        else:
            dec.append({"azione": "segnala_runaway", "livello": "GIALLO", "agente": slug,
                        "tmux_window": tw, "ruolo": info.get("ruolo"), "cpu_pct": c,
                        "coordinatore": info.get("coordinatore"),
                        "perche": f"runaway ma NON exec ({info.get('ruolo')}): solo segnalato, "
                                  f"mai sospeso in automatico"})
    return dec


# ---------------------------------------------------------------------------
# ESECUZIONE DELLE AZIONI
# ---------------------------------------------------------------------------

def esegui_stop_retry_loop(dec, dry=False):
    """Ferma il retry loop SENZA uccidere la sessione. Mai C-c su una finestra
    Claude Code: se e' idle equivale a uscire e CHIUDE la sessione (regola
    d'ecosistema). Si manda un solo ESC, che interrompe la generazione in corso
    e lascia il CLI vivo e la memoria intatta."""
    tw = dec["tmux_window"]
    if dry:
        return True, "dry: nessun invio"
    try:
        # 22/08/2026 FIX "match per prefisso": ESC sul target ESATTO — mai
        # interrompere il turno dell'agente nella finestra con lo stesso prefisso.
        tmux_esatto.send_keys_finestra(tw, "Escape")
        return True, "ESC inviato (nessun C-c: chiuderebbe la sessione)"
    except Exception as e:
        return False, str(e)[:200]


def esegui_failover(dec, dry=False):
    """Failover di PROVIDER con salva-stato PRIMA (procedura validata):
      1. il conv_id corrente si archivia in conv_id_by_engine[engine_attuale]
      2. si scrive engine/model/provider nuovi sul record
      3. spegni_agente + riaccensione FRESCA (lancia_agente modalita='fresco':
         stessa via di failover_quota). accendi_agente resta per i riavvii dove
         il resume e' VOLUTO; qui e' un PASSAGGIO ENGINE e deve essere un vero
         rinfrescami.
    FIX 25/08/2026 (ordine Manfredo via samantha_3): prima si RIUSAVA
    conv_id_by_engine[nuovo_eng] come conv corrente — il conv_id VECCHIO di
    quel motore era resumibile e accendi_agente (modalita='resume') ricaricava
    la memoria intera sul motore nuovo, mai fresco. Ora si conia SEMPRE un
    uuid nuovo e il rilancio e' esplicitamente fresco.
    Il passo 1 e' quello che rende il failover non distruttivo: senza, riaccendere
    l'agente sull'altro motore gli cancella la memoria in silenzio."""
    slug = dec.get("agente")
    if not slug:
        return False, "agente non risolto dal DB: nessuna azione"
    if dry:
        return True, f"dry: {slug} -> {dec['serbatoio']} ({dec['engine_nuovo']}/{dec.get('model_nuovo')})"
    try:
        import agent_db
        db = agent_db.get_db()
        d = db.get(slug)
        if not d:
            return False, f"{slug} non nel DB"
        eng_att = dec.get("engine_reale") or d.get("engine")
        conv = d.get("conv_id")
        d.setdefault("conv_id_by_engine", {})
        if conv and eng_att:
            d["conv_id_by_engine"][eng_att] = conv          # SALVA-STATO

        nuovo_eng = dec["engine_nuovo"]
        nuovo_prov = dec.get("provider_nuovo")
        is_rientro = (nuovo_eng == "claude" and nuovo_prov is None and dec.get("serbatoio") == "claude")

        if not is_rientro:
            # 25/08/2026: traccia il motore originale per il RIENTRO al flat
            # (decisioni_rientro): senza, il DB aggiornato a deepseek non permette
            # di sapere dove riportare l'agente quando claude torna.
            d.setdefault("motore_originale", eng_att)
            if d.get("provider"):
                d.setdefault("provider_originale", d.get("provider"))
            if d.get("model"):
                d.setdefault("model_originale", d.get("model"))
        else:
            # Rientro completato: pulizia tracciamento originale
            d.pop("motore_originale", None)
            d.pop("provider_originale", None)
            d.pop("model_originale", None)
            # 26/08/2026: pulizia esplicita nel DB Postgres (la concatenazione JSONB
            # agents.data || EXCLUDED.data in agent_db.save_db() non rimuove chiavi con pop())
            try:
                import agent_db as _adb_clean
                _conn_clean = _adb_clean.get_connection()
                _cur_clean = _conn_clean.cursor()
                _cur_clean.execute(
                    "UPDATE agents SET data = data - 'motore_originale' - 'provider_originale' - 'model_originale' WHERE agent_id = %s;",
                    (slug,)
                )
                _conn_clean.commit()
                _cur_clean.close()
                _conn_clean.close()
            except Exception as _e_clean:
                ledger("failover_clean_db_fallito", agente=slug, errore=str(_e_clean)[:100])

        d["engine"] = nuovo_eng
        if dec.get("model_nuovo"):
            d["model"] = dec["model_nuovo"]
        # Assegna esplicitamente il provider nuovo (None se torniamo a claude flat)
        d["provider"] = nuovo_prov

        # 25/08/2026: conv_id SEMPRE nuovo al failover. Il dict
        # conv_id_by_engine resta SOLO registro storico: si aggiorna, non si
        # legge MAI per fissare il conv corrente.
        import uuid
        conv_nuovo = str(uuid.uuid4())
        d["conv_id_by_engine"][nuovo_eng] = conv_nuovo      # registro storico
        d["conv_id"] = conv_nuovo
        db[slug] = d
        agent_db.save_db()

        # Aggiornamento contestuale di failover_quota_state.json se presente
        if is_rientro:
            try:
                import failover_quota
                st = failover_quota.carica_stato_failover()
                if slug in st.get("agenti_migrati", {}):
                    voce = st["agenti_migrati"].pop(slug)
                    voce["ts_ripristino"] = time.time()
                    voce["data_ripristino"] = datetime.now().isoformat(timespec="seconds")
                    voce["conv_id_ripristinato"] = conv_nuovo
                    st.setdefault("storico_migrazioni", []).append(voce)
                    failover_quota.salva_stato_failover(st)
            except Exception as _e_st:
                ledger("failover_aggiornamento_state_fallito", agente=slug, errore=str(_e_st)[:100])

        py = os.path.join(CORE, "venv", "bin", "python3")
        sa = os.path.join(CORE, "strumento_agenti.py")
        tw = d.get("tmux_window") or f"{d.get('studio') or slug}:AGY-{slug.upper()}"
        sess, nome_win = tw.split(":", 1)
        # FIX 31/08/2026 (mandato exec_fix_guardiano_system_scope): guardiano
        # gira da servizio/cron senza tty/tmux, quindi identifica_chiamante()
        # in strumento_agenti.py nega l'operazione per identita' incerta.
        # AGY_SYSTEM_SCOPE attiva la via di sistema (ESPLICITA/TRACCIABILE/
        # RISTRETTA a root reale, vedi strumento_agenti.py righe 768-809):
        # si ESTENDE l'env del processo, mai sostituito.
        r_spegni = subprocess.run([py, sa, "spegni_agente", slug], cwd=CORE,
                       env={**os.environ, "AGY_SYSTEM_SCOPE": "1"},
                       capture_output=True, text=True, timeout=180)
        # FIX 31/08/2026 (mandato exec_fix_failover_duplicati: 9 finestre
        # duplicate reali oggi, una delle quali ha fatto perdere per sempre un
        # messaggio Telegram di Manfredo): il returncode da solo non e' prova
        # sufficiente che la finestra vecchia sia morta per davvero — verifica
        # DIRETTA con tmux_esatto (stesso controllo usato altrove nel modulo),
        # con un retry breve. Se resta viva, NON si procede a lancia_agente
        # sullo stesso nome finestra: tmux ne creerebbe una SECONDA omonima e
        # il targeting per nome (capture-pane/kill-window/send-keys) si
        # romperebbe per sempre su quella finestra, raggiungibile solo per
        # @window_id.
        ancora_viva = tmux_esatto.finestra_viva(tw)
        if ancora_viva:
            time.sleep(3)
            ancora_viva = tmux_esatto.finestra_viva(tw)
        if ancora_viva:
            ledger("failover_spegnimento_fallito", agente=slug, finestra=tw,
                   returncode=r_spegni.returncode,
                   dettagli=(r_spegni.stdout or r_spegni.stderr or "")[-300:])
            return False, (f"spegnimento non verificato su '{tw}' (rc={r_spegni.returncode}): "
                            f"failover interrotto per '{slug}', si ritenta al prossimo ciclo")
        # 25/08/2026: riaccensione FRESCA esplicita, mai accendi_agente qui
        # (farebbe --resume del conv_id vecchio). Stessa via di failover_quota.
        import strumento_agenti
        _target, consegnato, dett = strumento_agenti.lancia_agente(
            slug, d, sess, nome_win, conv_nuovo, d.get("system_prompt"),
            modalita="fresco")
        ledger("failover_eseguito", agente=slug, da_engine=eng_att, a_engine=nuovo_eng,
               provider=nuovo_prov, conv_id=conv_nuovo, rientro=is_rientro, consegnato=bool(consegnato))
        return bool(consegnato), str(dett)[-300:]
    except Exception as e:
        ledger("failover_errore", agente=slug, errore=str(e)[:300])
        return False, str(e)[:300]


def esegui_spegnimento(dec, dry=False):
    slug = dec.get("agente")
    if not slug:
        return False, "agente non risolto"
    if dry:
        return True, f"dry: spegni {slug}"
    try:
        # FIX 31/08/2026 (mandato exec_fix_guardiano_system_scope): stesso
        # motivo del fix in esegui_failover qui sopra — via di sistema
        # necessaria per un chiamante senza tty/tmux.
        r = subprocess.run(
            [os.path.join(CORE, "venv", "bin", "python3"),
             os.path.join(CORE, "strumento_agenti.py"), "spegni_agente", slug],
            cwd=CORE, env={**os.environ, "AGY_SYSTEM_SCOPE": "1"},
            capture_output=True, text=True, timeout=180)
        return r.returncode == 0, (r.stdout or r.stderr or "")[-300:]
    except Exception as e:
        return False, str(e)[:300]


def esegui_rilancio(dec, dry=False):
    """Rilancio via strumento_agenti (perimetro e ricorsivita' applicati da codice).
    Il record e' gia' stato riallineato al processo reale piu' su nello stesso
    ciclo, quindi non si riaccende sul motore sbagliato — che e' il modo in cui
    un rilancio 'riuscito' cancella una memoria senza nessun errore visibile."""
    slug = dec.get("agente")
    if not slug:
        return False, "agente non risolto"
    if dry:
        return True, f"dry: rilancio {slug}"
    try:
        py = os.path.join(CORE, "venv", "bin", "python3")
        sa = os.path.join(CORE, "strumento_agenti.py")
        tw = dec.get("tmux_window")
        # FIX 31/08/2026 (mandato exec_fix_guardiano_system_scope): via di
        # sistema, stesso motivo di esegui_failover/esegui_spegnimento sopra.
        r_spegni = subprocess.run([py, sa, "spegni_agente", slug], cwd=CORE,
                       env={**os.environ, "AGY_SYSTEM_SCOPE": "1"},
                       capture_output=True, text=True, timeout=180)
        # FIX 31/08/2026 (stesso mandato/stesso pattern di esegui_failover qui
        # sopra): la guardia "GIA' ACCESO" dentro accendi_agente non copre il
        # caso di un pane MORTO ma con la finestra tmux ancora presente
        # (remain-on-exit=on, impostato apposta da lancia_agente per
        # l'hardening exec) — nessun processo motore vivo tra i figli del
        # pane_pid, la guardia non scatta e accendi_agente procederebbe
        # comunque a creare una finestra omonima. Verifica DIRETTA con
        # tmux_esatto, con un retry breve, prima di procedere.
        if tw:
            ancora_viva = tmux_esatto.finestra_viva(tw)
            if ancora_viva:
                time.sleep(3)
                ancora_viva = tmux_esatto.finestra_viva(tw)
            if ancora_viva:
                ledger("rilancio_spegnimento_fallito", agente=slug, finestra=tw,
                       returncode=r_spegni.returncode,
                       dettagli=(r_spegni.stdout or r_spegni.stderr or "")[-300:])
                return False, (f"spegnimento non verificato su '{tw}' (rc={r_spegni.returncode}): "
                                f"rilancio interrotto per '{slug}', si ritenta al prossimo ciclo")
        # FIX 31/08/2026 (mandato exec_fix_guardiano_system_scope): via di
        # sistema, stesso motivo dello spegnimento appena sopra in questa
        # stessa funzione.
        r = subprocess.run([py, sa, "accendi_agente", slug], cwd=CORE,
                           env={**os.environ, "AGY_SYSTEM_SCOPE": "1"},
                           capture_output=True, text=True, timeout=300)
        return r.returncode == 0, (r.stdout or r.stderr or "")[-300:]
    except Exception as e:
        return False, str(e)[:300]


def esegui_sblocca_errore_api(dec, dry=False):
    """Auto-remediation per agente fermo su errore API con messaggi in inbox.
    Pulisce il buffer da dialog/errori residui con Escape + C-u e invia un nudge."""
    tw = dec.get("tmux_window")
    slug = dec.get("agente")
    if not tw or not slug:
        return False, "finestra o slug assente"
    if dry:
        return True, f"dry: sblocco errore API simulato su {tw}"
    try:
        engine = dec.get("engine") or "claude"
        tmux_esatto.send_keys_finestra(tw, "Escape")
        time.sleep(1.0)
        tmux_esatto.send_keys_finestra(tw, "C-u")
        time.sleep(0.3)
        msg = (f"[A2A_FROM:guardiano] [A2A_TYPE:task] 🔔 Sveglia da errore: hai {dec.get('in_attesa', 1)} "
               f"messaggio/i in a2a/{slug}/inbox/ in attesa di presa in carico. Controlla e prosegui.")
        engine_adapter.send_via_tmux_buffer(tw, msg, engine=engine)
        return True, f"inviato sblocco Escape+C-u e campanello risveglio su {tw}"
    except Exception as e:
        return False, f"errore sblocco: {e}"[:300]


def esegui_sblocca_consegna_ferma(dec, dry=False):
    """Auto-remediation per consegna ferma su agente vivo."""
    slug = dec.get("agente")
    if not slug:
        return False, "slug assente"
    if dry:
        return True, f"dry: sblocco consegna ferma simulato per {slug}"
    try:
        tw = dec.get("tmux_window")
        engine = dec.get("engine") or "claude"
        if not tw:
            try:
                import psycopg2
                conn = psycopg2.connect("dbname=betty_db user=betty_user host=localhost port=5432")
                cur = conn.cursor()
                cur.execute("SELECT data->>'tmux_window', data->>'engine' FROM agents WHERE agent_id = %s;", (slug,))
                row = cur.fetchone()
                conn.close()
                if row and row[0]:
                    tw, engine = row[0], row[1] or "claude"
            except Exception:
                pass
        if tw:
            tmux_esatto.send_keys_finestra(tw, "Escape")
            time.sleep(1.0)
            tmux_esatto.send_keys_finestra(tw, "C-u")
            time.sleep(0.3)
            msg = (f"[A2A_FROM:guardiano] [A2A_TYPE:task] 🔔 Sveglia consegna: hai {dec.get('in_attesa', 1)} "
                   f"messaggio/i in a2a/{slug}/inbox/ non presi in carico da >{dec.get('eta_max_min')} min. "
                   f"Leggi la tua inbox e conferma con a2a_ack.py.")
            engine_adapter.send_via_tmux_buffer(tw, msg, engine=engine)
            return True, f"inviato sblocco consegna ferma su {tw}"
        return False, f"nessuna finestra per {slug}"
    except Exception as e:
        return False, f"errore sblocco: {e}"[:300]


ESECUTORI = {
    "rilancia_cli_morto": esegui_rilancio,
    "stop_retry_loop": esegui_stop_retry_loop,
    "failover": esegui_failover,
    "spegni_per_ram": esegui_spegnimento,
    "sospendi_runaway": esegui_spegnimento,
    "sblocca_errore_api": esegui_sblocca_errore_api,
    "sblocca_consegna_ferma": esegui_sblocca_consegna_ferma,
}

FLAG_PER_AZIONE = {
    "rilancia_cli_morto": "rilancia_cli_morto",
    "stop_retry_loop": "stop_retry_loop",
    "failover": "failover_provider",
    "spegni_per_ram": "spegni_per_ram",
    "sospendi_runaway": "sospendi_runaway",
    "sblocca_errore_api": "sblocca_errore_api",
    "sblocca_consegna_ferma": "sblocca_consegna_ferma",
}

AZIONI_DISTRUTTIVE = {
    "rilancia_cli_morto", "stop_retry_loop", "failover", "spegni_per_ram",
    "sospendi_runaway", "avvio", "rinfresco"
}

def _verifica_misure_distruttive(dec):
    """
    B3 - Presidio strutturale fail-safe.
    Se un'azione e' distruttiva, deve dichiarare da quali misure dipende 
    attraverso '_misure_usate': {nome_misura: valore}. Se la chiave manca 
    o se uno dei valori e' None (misura assente/illeggibile), la decisione 
    viene categoricamente rifiutata alla porta.
    Restituisce: (True, "", []) se ammessa, (False, evento, mancanti) se bloccata.
    """
    azione = dec.get("azione")
    if azione not in AZIONI_DISTRUTTIVE:
        return True, "", []
    misure = dec.get("_misure_usate")
    if misure is None:
        return False, "decisione_rifiutata_mancanza_dichiarazione_misure", []
    mancanti = [k for k, v in misure.items() if v is None]
    if mancanti:
        return False, "decisione_rifiutata_dati_mancanti", mancanti
    return True, "", []


def applica(decisioni, conf, dedup, dry=False):
    """Esegue le decisioni ammesse dai flag e allerta sul canale giusto.
    Una decisione BLOCCATA da un flag non sparisce: diventa un ROSSO a Manfredo
    con la richiesta di conferma — e' cosi' che un coordinatore non viene mai
    toccato in automatico senza che nessuno lo sappia."""
    esiti = []
    for dec in decisioni:
        azione = dec["azione"]
        ruolo = dec.get("ruolo") or "sottoposto"
        # La chiave di dedup puo' essere imposta dalla decisione: i verdetti F4
        # si deduplicano per FIRMA (guardiano+tipo+soggetto), non per soggetto —
        # due guasti diversi sullo stesso agente sono due ticket, non uno.
        chiave = dec.get("chiave_dedup") or \
            f"{azione}:{dec.get('tmux_window') or dec.get('agente') or 'sistema'}"
        esecutore = ESECUTORI.get(azione)
        if esecutore is None:                      # sola segnalazione
            if azione in SOLO_LEDGER or (dec["livello"] == "GIALLO"
                                         and azione not in GIALLI_CON_CANALE):
                ledger("decisione_segnalata", canale="solo_ledger", **dec)
                esiti.append(dict(dec, esito="solo_ledger"))
                continue
            allerta(dec["livello"], chiave, _testo_alert(dec, conf), conf, dedup,
                    dry=dry, coordinatore=dec.get("coordinatore"))
            ledger("decisione_segnalata", **dec)
            esiti.append(dict(dec, esito="solo_segnalata"))
            continue
            
        ammessa, evento_rifiuto, mancanti = _verifica_misure_distruttive(dec)
        if not ammessa:
            motivo = f"{evento_rifiuto} ({', '.join(mancanti or [])})"
            ledger(evento_rifiuto, mancanti=mancanti, **dec)
            esiti.append(dict(dec, esito="bloccata_da_presidio_strutturale", dettaglio=motivo))
            continue
            
        if not azione_permessa(FLAG_PER_AZIONE[azione], ruolo, conf):
            testo = (f"AZIONE BLOCCATA DAL FLAG — {azione} su *{dec.get('agente')}* "
                     f"({ruolo}).\n{dec['perche']}\n"
                     f"I coordinatori non si toccano in automatico: serve la tua conferma.")
            # Un'azione bloccata dal flag e' uno STATO che dura (il coordinatore
            # resta in quota finche' non torna): il ROSSO parte una volta per
            # finestra di cooldown, non ad ogni ciclo.
            allerta("ROSSO", f"bloccata:{chiave}", testo, conf, dedup, dry=dry)
            ledger("azione_bloccata_da_flag", **dec)
            esiti.append(dict(dec, esito="bloccata_da_flag"))
            continue
        ok, dettaglio = esecutore(dec, dry=dry)
        ledger("azione", esito_ok=ok, dettaglio=dettaglio[:300], dry=dry, **dec)
        allerta(dec["livello"], chiave, _testo_alert(dec, conf, ok, dettaglio),
                conf, dedup, dry=dry, coordinatore=dec.get("coordinatore"))
        esiti.append(dict(dec, esito="eseguita" if ok else "fallita", dettaglio=dettaglio))
    return esiti


def _testo_alert(dec, conf, ok=None, dettaglio=""):
    """Mobile-friendly: elenco verticale, niente tabelle ASCII (Manfredo legge da
    iPhone e una tabella va a capo diventando illeggibile)."""
    righe = [f"*{dec['azione']}* — {dec.get('agente') or dec.get('tmux_window') or 'sistema'}"]
    if dec.get("ruolo"):
        righe.append(f"• ruolo: {dec['ruolo']}")
    righe.append(f"• perche': {dec['perche']}")
    if dec.get("serbatoio"):
        righe.append(f"• nuovo serbatoio: {dec['serbatoio']} "
                     f"({dec.get('engine_nuovo')}/{dec.get('model_nuovo')})")
    if dec.get("cpu_pct") is not None:
        righe.append(f"• CPU: {dec['cpu_pct']}%")
    # F4 — tracciabilita': un alert che non dice valore, fonte ed eta' non e'
    # verificabile da chi lo riceve, ed e' esattamente per questo che il 21/08
    # quattro guardiani difettosi sono stati creduti sulla parola.
    if dec.get("valore") is not None:
        righe.append(f"• valore: {str(dec['valore'])[:160]}")
    if dec.get("fonte"):
        righe.append(f"• fonte: {dec['fonte']}")
    if dec.get("eta_dato_s") is not None:
        righe.append(f"• eta' del dato: {dec['eta_dato_s']}s")
    if dec.get("ripetizioni"):
        righe.append(f"• ripetizioni della stessa firma: {dec['ripetizioni']} "
                     f"in {dec.get('da_s')}s")
    if dec.get("firma"):
        righe.append(f"• firma: `{dec['firma']}`")
    if dec.get("fermo_da_s") is not None:
        righe.append(f"• fermo da: {dec['fermo_da_s']}s")
    if dec.get("rimedio"):
        righe.append(f"• rimedio noto: {dec['rimedio']}")
    if ok is not None:
        righe.append(f"• esito: {'✅ eseguita' if ok else '❌ fallita'} {dettaglio[:120]}")
    righe.append(f"• registro: {LEDGER_PATH}")
    return "\n".join(righe)


# ---------------------------------------------------------------------------
# CICLO
# ---------------------------------------------------------------------------

def coordinatore_di(slug, tmux_window, db, conf):
    """Il coordinatore INTERESSATO, non un destinatario fisso. Un GIALLO che va
    sempre allo stesso agente e' il difetto che ha reso muto il self-healing il
    21/08: 42 allerte a una Samantha ferma per la stessa quota che segnalavano.
    Criterio: il coordinatore che vive nella STESSA sessione tmux dell'agente
    (convenzione: un exec si crea nella sessione di chi lo ordina); se la sessione
    non ne ha uno, si ricade sul default di conf."""
    sess = (tmux_window or "").split(":", 1)[0].lower()
    if sess:
        for altro, d in (db or {}).items():
            if altro == slug or not d or d.get("dismesso"):
                continue
            tw = (d.get("tmux_window") or "").lower()
            if not tw.startswith(sess + ":"):
                continue
            if classifica_agente(altro, d, conf).startswith("coordinatore"):
                return altro
    return conf["canali"].get("coordinatore_default", "samantha_2")


def mappa_classi(agenti, conf):
    """{tmux_window: {slug, ruolo, always_on}}. Il DB serve SOLO a dare un nome e
    un ruolo alla finestra — motore e modello vengono dal processo, mai da qui."""
    classi = {}
    try:
        import agent_db
        db = agent_db.get_db()
    except Exception:
        db = {}
    per_win = {}
    for slug, d in db.items():
        # FIX 31/08/2026 (mandato samantha_2, causa collegata all'incidente
        # samantha_1): i record VIRTUALI (pool_di, alias_di) condividono per
        # costruzione la tmux_window della loro istanza reale — senza questo
        # filtro possono sovrascrivere l'istanza reale in per_win a seconda
        # dell'ordine di iterazione, facendo risolvere decisioni_quota/failover
        # sul record virtuale invece che su quello vero. Prova reale di oggi:
        # il mass-restart 13:07-13:09 ha lanciato un CLI vero sul record
        # virtuale 'samantha' (pool_di=[samantha_1,2,3]), sessione fantasma poi
        # trovata e spenta a mano. Stesso filtro gia' usato altrove nel core:
        # tmux_esatto.risolvi_agente_da_finestra, decisioni_liveness() qui
        # sotto, strumento_agenti.py, send_a2a.py, a2a_ack.py.
        if not isinstance(d, dict) or d.get("pool_di") or d.get("alias_di"):
            continue
        tw = (d or {}).get("tmux_window")
        if tw:
            per_win[tw.lower()] = (slug, d)
    for a in agenti:
        slug, d = per_win.get(a["tmux_window"].lower(), (None, {}))
        classi[a["tmux_window"]] = {
            "slug": slug,
            "ruolo": classifica_agente(slug or "", d, conf) if slug else "sottoposto",
            "always_on": bool((d or {}).get("always_on")),
            "dismesso": bool((d or {}).get("dismesso")),
            "coordinatore": coordinatore_di(slug, a["tmux_window"], db, conf),
        }
    return classi


def misure_serbatoi(quota, conf):
    """Le misure che decidono la disponibilita' dei tre serbatoi, in un dict piatto
    (e' l'input della scelta serbatoio: cosi' i test la esercitano senza sensori)."""
    flat = quota.get("flat") or {}
    ds = quota.get("deepseek") or {}
    reset_agy = [a.get("reset_minuti") for a in quota["agenti"]
                 if a.get("engine_reale") == "agy" and a.get("reset_minuti")]
    in_reset = any(p["tipo"] == "quota_esaurita" for a in quota["agenti"]
                   if a.get("engine_reale") == "agy" for p in (a.get("problemi") or []))
    # una lettura vecchia non deve decidere nulla: porta con se' la propria eta'
    eta = flat.get("eta_s")
    stale = eta is not None and eta > conf["sonda_usage"].get("stale_dopo_s", 7200)
    return {
        "claude_stale": stale or flat.get("claude_settimanale_pct") is None,
        "claude_lettura_eta_s": eta,
        # SESSIONE PIENA ≠ QUOTA FINITA (21/08). Questo flag e' cio' che
        # trasforma un failover a pagamento in un "attendi reset".
        "claude_attendi_reset": bool(flat.get("claude_attendi_reset")),
        "claude_attendi_reset_nota": flat.get("claude_attendi_reset_nota"),
        "claude_pct_max_solo_diagnosi": flat.get("claude_pct_max"),
        "claude_fonte": flat.get("claude_fonte"),
        "claude_reset_sessione": flat.get("claude_reset_sessione"),
        "agy_claude_disponibile_pct": flat.get("agy_claude_disponibile_pct"),
        "claude_settimanale_pct": flat.get("claude_settimanale_pct"),
        "claude_settimanale_fable_pct": flat.get("claude_settimanale_fable_pct"),
        "claude_sessione_pct": flat.get("claude_sessione_pct"),
        "agy_disponibile_pct": flat.get("agy_disponibile_pct"),
        "agy_5h_disponibile_pct": flat.get("agy_5h_disponibile_pct"),
        "agy_settimanale_disponibile_pct": flat.get("agy_settimanale_disponibile_pct"),
        "agy_reset_5h": flat.get("agy_reset_5h"),
        "agy_reset_settimana": flat.get("agy_reset_settimana"),
        "agy_in_reset": in_reset,
        "agy_reset_minuti": max(reset_agy) if reset_agy else None,
        "deepseek_saldo_usd": ds.get("saldo_usd"),
    }


# ---------------------------------------------------------------------------
# RUOLO ROUTER — SOLA OSSERVAZIONE (22/08/2026, direttiva Manfredo, exec_selfheal_router)
#
# Manfredo ha ribattezzato il guardiano 'SELF HEALING' e gli ha dato il ruolo di
# ROUTER centrale: leggere i residui delle sorgenti, scegliere il modello in base
# al task, decidere a chi vanno i task nei pool.
#
# ⚠️ PERIMETRO: NESSUN effetto reale. Come F-A, ogni ciclo questo modulo produce
# la decisione che AVREBBE preso e la scrive UNA riga per decisione nel log
# dedicato /tmp/self_healing_router.log. Non tocca i pool, non avvia sessioni,
# non apre finestre. Il routing esecutivo sugli abbonamenti lo cura un ALTRO exec
# (fronte B, exec_abbon_routing): qui si OSSERVA la mappa che lui produce.
#
# La mappa task→sorgente/modello sta in routing_regole.json (la crea l'altro
# exec). Se manca, questo modulo usa una bozza locale di riserva (stessa forma).
# Residuo non misurato = 'residuo non disponibile', MAI valori inventati: una
# lettura vecchia o assente non deve decidere nulla (stesso principio di F4-c).
# ---------------------------------------------------------------------------

# Bozza di riserva della mappa task→sorgente/modello, usata SOLO se
# routing_regole.json non esiste. Stessa forma di quella prodotta dall'altro
# exec: 4 classi, ognuna con preferito/alternativa/fallback_finale e soglie.
BOZZA_ROUTING_REGOLE = {
    "versione": "0.9-bozza-selfheal",
    "ultimo_aggiornamento": "2026-08-22",
    "fonte": "bozza di riserva exec_selfheal_router (fronte A) — la mappa vera la crea exec_abbon_routing in routing_regole.json",
    "principio": "ABBONAMENTI PRIMA (claude_flat e antigravity), DeepSeek a consumo SOLO come ultima spiaggia. Il routing avviene a livello SESSIONE/MOTORE.",
    "lettura_residui": {
        "claude_flat": "claude_settimanale_pct (usata) dalla sonda /usage di engine_budget",
        "antigravity": "agy_disponibile_pct (disponibile) dalla sonda /usage",
        "deepseek": "deepseek_saldo_usd (saldo) da deepseek_spend_guard",
    },
    "sorgenti": {
        "claude_flat": {"tipo": "abbonamento", "modello_preferito": "claude-haiku-4-5-20251001",
                        "soglia_downgrade_pct_usata": 80, "soglia_critica_pct_usata": 95,
                        "reset": "settimanale"},
        "antigravity": {"tipo": "abbonamento", "modello_preferito": "gemini-3.7-flash-medium",
                        "soglia_downgrade_pct_usata": 80, "soglia_critica_pct_usata": 95,
                        "reset": "settimanale (~93h55m)"},
        "deepseek": {"tipo": "consumo_fallback", "modello_preferito": "deepseek-chat",
                     "soglia_avviso_usd": 2.0, "soglia_blocco_usd": 0.5, "reset": "nessuno"},
    },
    "classi_task": [
        {"classe": "veloce_economico",
         "descrizione": "Task brevi e a bassa complessita': query puntuali, riassunti brevi, estrazioni.",
         "preferito": {"sorgente": "antigravity", "modello": "gemini-3.7-flash-medium"},
         "alternativa": {"sorgente": "claude_flat", "modello": "claude-haiku-4-5-20251001"},
         "fallback_finale": {"sorgente": "deepseek", "modello": "deepseek-chat"}},
        {"classe": "standard",
         "descrizione": "Task ordinari di un agente: lavoro su file, risposte medie, manutenzione.",
         "preferito": {"sorgente": "claude_flat", "modello": "claude-haiku-4-5-20251001"},
         "alternativa": {"sorgente": "antigravity", "modello": "gemini-3.7-flash-medium"},
         "fallback_finale": {"sorgente": "deepseek", "modello": "deepseek-chat"}},
        {"classe": "complesso",
         "descrizione": "Task complessi: refactor, debugging, codice esteso, integrazioni.",
         "preferito": {"sorgente": "claude_flat", "modello": "claude-sonnet-5"},
         "alternativa": {"sorgente": "antigravity", "modello": "claude-sonnet-4-6"},
         "fallback_finale": {"sorgente": "deepseek", "modello": "deepseek-chat"}},
        {"classe": "critico",
         "descrizione": "Task critici: incidenti, rollback, produzione, dati sensibili.",
         "preferito": {"sorgente": "claude_flat", "modello": "claude-opus-5"},
         "alternativa": {"sorgente": "antigravity", "modello": "claude-opus-4-6-thinking"},
         "fallback_finale": {"sorgente": "deepseek", "modello": "deepseek-chat"}},
    ],
}


def router_legge_regole():
    """Mappa task→sorgente/modello. LEGGE routing_regole.json se esiste (la crea
    l'altro exec); se manca o e' illeggibile usa la bozza di riserva locale.
    MAI scrive su file: la mappa vera e' di proprieta' del fronte B."""
    try:
        if os.path.exists(ROUTING_REGOLE_PATH):
            with open(ROUTING_REGOLE_PATH, encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        print(f"[guardiano] routing_regole.json illeggibile, uso bozza locale: {e}", flush=True)
    return BOZZA_ROUTING_REGOLE


def router_residuo_sorgente(nome, misure, regole):
    """Residuo misurato di una sorgente, per la decisione del router.
    Ritorna un dict {residuo, unita, usata_pct, disponibile, nota, fonte}.
    Residuo NON misurato o lettura vecchia -> 'residuo non disponibile'.
    MAI valori inventati (F4-c): senza numero reale, disponibile=False."""
    s = (regole.get("sorgenti") or {}).get(nome) or {}
    if nome == "claude_flat":
        usata = misure.get("claude_settimanale_pct")
        if usata is None or misure.get("claude_stale"):
            return {"residuo": None, "unita": "pct", "usata_pct": usata,
                    "disponibile": False, "fonte": "sonda /usage",
                    "nota": "residuo non disponibile"}
        soglia = s.get("soglia_downgrade_pct_usata", 80)
        disp = usata < soglia
        return {"residuo": round(100 - usata, 1), "unita": "pct", "usata_pct": usata,
                "disponibile": disp, "fonte": "sonda /usage",
                "nota": f"usata {usata}% (soglia downgrade {soglia}%)"}
    if nome == "antigravity":
        disp_pct = misure.get("agy_disponibile_pct")
        if disp_pct is None or misure.get("claude_stale"):
            return {"residuo": None, "unita": "pct", "usata_pct": None,
                    "disponibile": False, "fonte": "sonda /usage",
                    "nota": "residuo non disponibile"}
        if misure.get("agy_in_reset"):
            return {"residuo": disp_pct, "unita": "pct", "usata_pct": 100 - disp_pct,
                    "disponibile": False, "fonte": "sonda /usage",
                    "nota": f"in reset (atteso {misure.get('agy_reset_minuti')} min)"}
        soglia = s.get("soglia_downgrade_pct_usata", 80)
        disp = (100 - disp_pct) < soglia
        return {"residuo": disp_pct, "unita": "pct", "usata_pct": 100 - disp_pct,
                "disponibile": disp, "fonte": "sonda /usage",
                "nota": f"disponibile {disp_pct}% (usata {100 - disp_pct}%)"}
    if nome == "deepseek":
        saldo = misure.get("deepseek_saldo_usd")
        if saldo is None:
            return {"residuo": None, "unita": "usd", "usata_pct": None,
                    "disponibile": False, "fonte": "deepseek_spend_guard",
                    "nota": "residuo non disponibile"}
        blocco = s.get("soglia_blocco_usd", 0.5)
        disp = saldo > blocco
        return {"residuo": round(saldo, 2), "unita": "usd", "usata_pct": None,
                "disponibile": disp, "fonte": "deepseek_spend_guard",
                "nota": f"saldo {round(saldo, 2)} USD (blocco sotto {blocco})"}
    return {"residuo": None, "unita": None, "usata_pct": None,
            "disponibile": False, "fonte": None, "nota": f"sorgente {nome} ignota"}


def router_decisione_classe(classe, misure, regole):
    """La decisione che il router AVREBBE preso per UNA classe di task.
    Catena preferito -> alternativa -> fallback_finale, su residui misurati.
    Ritorna il dict della decisione; NESSUN effetto reale qui sotto."""
    nome = classe.get("classe", "?")
    perche = []
    for livello in ("preferito", "alternativa", "fallback_finale"):
        scelta = classe.get(livello) or {}
        sorg = scelta.get("sorgente")
        if not sorg:
            continue
        r = router_residuo_sorgente(sorg, misure, regole)
        if r["disponibile"]:
            return {
                "classe": nome, "livello": livello, "sorgente": sorg,
                "modello": scelta.get("modello"), "residuo": r["residuo"],
                "unita": r["unita"], "usata_pct": r["usata_pct"],
                "fonte": r["fonte"], "nota": r["nota"],
                "sola_osservazione": True, "esito": "instraderebbe",
            }
        perche.append(f"{livello}={sorg}: {r['nota']}")
    # nessuna sorgente disponibile: il router segnalerebbe, non forzerebbe
    return {
        "classe": nome, "livello": None, "sorgente": None, "modello": None,
        "residuo": None, "unita": None, "usata_pct": None, "fonte": None,
        "nota": "nessuna sorgente disponibile: " + " | ".join(perche),
        "sola_osservazione": True, "esito": "segnalerebbe_esaurimento_budget",
    }


def router_osserva(misure, conf):
    """Ad ogni ciclo: produce e LOGGA le decisioni che il router avrebbe preso.
    UNA riga per decisione su /tmp/self_healing_router.log (JSON, append-only).
    MAI solleva: un difetto qui non deve spegnere la sorveglianza (F4)."""
    try:
        regole = router_legge_regole()
        decisioni = []
        for cls in regole.get("classi_task") or []:
            d = router_decisione_classe(cls, misure, regole)
            d["ts"] = ora_iso()
            decisioni.append(d)
            try:
                with open(ROUTER_LOG_PATH, "a", encoding="utf-8") as f:
                    f.write(json.dumps(d, ensure_ascii=False, default=str) + "\n")
            except Exception as e:
                print(f"[guardiano] router log non scrivibile: {e}", flush=True)
        return {"abilitato": True, "decisioni": decisioni,
                "fonte_regole": "routing_regole.json" if os.path.exists(ROUTING_REGOLE_PATH) else "bozza_locale",
                "sola_osservazione": True}
    except Exception as e:
        return {"abilitato": False, "decisioni": [], "errore": f"{type(e).__name__}: {e}"[:300],
                "sola_osservazione": True}


# ---------------------------------------------------------------------------
# SENSORE F4 — META-SORVEGLIANZA DEI GUARDIANI (21/08/2026, exec_guardiano_f4)
#
# IL PROBLEMA, IN UNA RIGA: i sorveglianti non sono sorvegliati.
# La sera del 21/08 QUATTRO guardiani su quattro erano difettosi insieme e
# nessuno se n'e' accorto da solo:
#   - deepseek_spend_guard verificava la consegna su una cartella INESISTENTE e
#     rifaceva lo stesso allarme ogni 5 minuti, per ore;
#   - resource_watchdog e check_agenti_vivi hanno prodotto cinque "agente morto"
#     su FANTASMI (finestre che non c'erano piu'), e quelle allerte di carico
#     sono state promosse a "danno vero";
#   - il guardiano stesso ha fatto failover A PAGAMENTO su una soglia di
#     SESSIONE scambiata per quota settimanale;
#   - sentinella_contesto sorvegliava 549 agenti DISMESSI e ordinava rinfreschi
#     a finestre che non esistono.
#
# Il denominatore comune non e' la sfortuna: e' che un allarme non porta con se'
# NIENTE che permetta di dubitarne. Non dice da dove viene il numero, non dice
# quanto e' vecchio, non dice se il soggetto esiste ancora, e ripetuto mille
# volte identico continua a valere come la prima. Da qui i tre controlli:
#
#   (a) RIPETIZIONE  — lo stesso allarme (firma = guardiano+tipo+soggetto)
#       ripetuto oltre N volte MENTRE il soggetto non cambia stato non e'
#       un'emergenza che peggiora: e' un guardiano rotto. Si degrada, non si
#       rilancia, e si apre UN ticket con la diagnosi.
#   (b) FANTASMI     — un allarme su un soggetto che non esiste (agente dismesso,
#       finestra tmux assente, servizio non registrato) misura il nulla.
#   (c) TRACCIABILITA' — ogni decisione porta VALORE + FONTE + ETA'. Un dato piu'
#       vecchio della soglia non puo' giustificare un'azione: diventa
#       "attendi/rimisura". Vale anche per i sensori vecchi (quota, cpu, heartbeat).
#   (d) SE STESSO    — gli stessi tre controlli si applicano al ledger DEL
#       GUARDIANO. Un guardiano che non sa dubitare di se' e' il quinto guasto.
#   (e) ORDINE FERMO IN CASELLA — vedi sensore_ordini_fermi() piu' sotto.
#
# ⚠️ PERIMETRO: questo sensore OSSERVA e SEGNALA. Non spegne, non rilancia, non
# corregge nessun altro guardiano. Il verdetto "guardiano rotto" e' una diagnosi
# per un umano, non un permesso di intervenire.
# ---------------------------------------------------------------------------

REGISTRO_FIRME_PATH = os.path.join(CORE, ".guardiano_firme_f4.json")

# Ultima scansione delle caselle A2A, riusata fra un ciclo e l'altro: vedi la
# nota dentro sensore_guardiani(). In RAM e non su file di proposito — e' una
# cache di prestazione, non uno stato: al riavvio si rifa' e va benissimo.
_CACHE_ALLARMI = None

# Vocabolario dei TIPI di allarme. Serve a rendere la firma STABILE: due
# messaggi scritti in modo diverso che dicono la stessa cosa devono produrre la
# stessa firma, altrimenti la ripetizione non si vede mai (ogni giro sembra
# nuovo). Ordine significativo: vince la prima che fa match.
TIPI_ALLARME = [
    ("agente_morto", r"agente morto|cli morto|non risponde|processo assente|morto\b"),
    ("consegna_fallita", r"consegna|non consegnat|recapit|cartella .*inesistente|no such file"),
    ("quota", r"quota|usage limit|\b429\b|rate limit|settimanal|sessione 5h"),
    ("saldo", r"saldo|\b402\b|insufficient balance|credito"),
    ("cpu", r"\bcpu\b|carico|load\b|runaway|retry loop"),
    ("ram", r"\bram\b|memoria|oom\b|psi[_ ]memory"),
    ("heartbeat", r"heartbeat|appeso|non aggiornato da|silenzio"),
    ("contesto", r"contesto|rinfresc|compattaz|token"),
    ("inbox", r"inbox|coda a2a|messaggi in attesa|non letto"),
    ("servizio_giu", r"servizio.*(giu|fermo|spento)|srv-.*(giu|fermo)"),
]

# Come si riconosce un SOGGETTO dentro un testo libero. Un allarme senza
# soggetto non e' confrontabile con nulla: finisce con soggetto None e viene
# contato, ma non puo' produrre un verdetto di fantasma.
_RE_FINESTRA = re.compile(r"\b([A-Za-z][A-Za-z0-9_.-]*):([A-Za-z0-9_.@-]+)\b")
# Timestamp ISO e orari hanno la stessa forma 'x:y' di una finestra tmux.
# Trovato al primo dry-run: '2026-08-21T19:35' veniva letto come la finestra
# '2026-08-21T19':'35', che ovviamente non esiste — e produceva un verdetto
# "guardiano misura fantasmi" per OGNI riga di ledger con un orario dentro.
_RE_ORARIO = re.compile(r"\d{1,2}:\d{2}(:\d{2})?")
_RE_SLUG = re.compile(r"\b(agente|agent|slug)[:= ]+([a-z0-9_]{3,40})\b", re.I)
_RE_SERVIZIO = re.compile(r"\b(SRV-[A-Za-z0-9_]+)\b")


def normalizza_tipo(testo):
    """Tipo canonico dell'allarme, o 'generico'. E' meta' della firma."""
    t = (testo or "").lower()
    for nome, pat in TIPI_ALLARME:
        if re.search(pat, t, re.I):
            return nome
    return "generico"


# I tag di protocollo ([A2A_FROM:x], [A2A_TYPE:y], [A2A_ID:z]) hanno la stessa
# forma 'qualcosa:qualcosa' di una finestra tmux. Trovato in test: senza
# toglierli, 'A2A_FROM:deepseek_spend_guard' veniva letto come la finestra
# soggetto dell'allarme — e siccome quella finestra non esiste, OGNI ticket A2A
# sarebbe stato classificato "guardiano misura fantasmi". Un meta-sorvegliante
# che diagnostica il guasto sbagliato e' peggio di uno assente.
_RE_TAG_PROTOCOLLO = re.compile(r"\[[A-Z0-9_]+:[^\]]*\]")


def estrai_soggetto(testo):
    """(genere, valore) del soggetto citato: ('finestra', 'sess:WIN') |
    ('servizio', 'SRV-X') | ('agente', 'slug') | (None, None).
    Prima la finestra tmux, che e' il riferimento piu' preciso."""
    t = _RE_TAG_PROTOCOLLO.sub(" ", testo or "")
    m = _RE_SERVIZIO.search(t)
    if m:
        return "servizio", m.group(1)
    for m in _RE_FINESTRA.finditer(t):
        sess, win = m.group(1), m.group(2)
        intero = m.group(0)
        # scarta orari e timestamp ISO: '19:35' e '2026-08-21T19:35' non sono finestre
        if _RE_ORARIO.fullmatch(intero) or win.isdigit():
            continue
        if len(sess) > 1 and len(win) > 1:
            return "finestra", intero
    m = _RE_SLUG.search(t)
    if m:
        return "agente", m.group(2).lower()
    return None, None


def sintesi_allarme(testo, tipo=None, soggetto=None, evento=None):
    """Una riga che dice DI COSA parla l'allarme, da mettere nel campo 'valore'.

    Correzione di samantha_2 (21/08): prima ci finiva il corpo del messaggio
    troncato a 160 caratteri — che nei ticket ripetuti tagliava a meta' parola e
    non diceva nulla ("• valore: [A2A_FR"). Chi legge il ticket deve capire il
    caso dalla riga, senza dover aprire la fonte."""
    t = _RE_TAG_PROTOCOLLO.sub(" ", testo or "")
    riga = ""
    for r in t.splitlines():
        r = r.strip(" •*-\t")
        if len(r) > 12 and not r.startswith("{"):
            riga = r
            break
    if not riga:
        riga = " ".join(t.split())[:90]
    etichetta = "/".join(x for x in (evento, tipo) if x)
    testa = f"{soggetto}: " if soggetto else ""
    return f"{testa}{etichetta + ' — ' if etichetta else ''}{riga}"[:140]


def firma_allarme(guardiano, tipo, soggetto):
    """Firma STABILE guardiano+tipo+soggetto. Non include il testo: un guardiano
    che riscrive lo stesso allarme con un timestamp dentro deve continuare a
    produrre la stessa firma, o la ripetizione resta invisibile per sempre."""
    return f"{guardiano}|{tipo}|{soggetto or '-'}"


# ---------------------------------------------------------------------------
# (c) TRACCIABILITA': valore + fonte + eta'
# ---------------------------------------------------------------------------

def traccia(valore, fonte, eta_s=None, ts=None):
    """Il pacchetto minimo che DEVE accompagnare ogni misura che decide qualcosa.
    Tre campi, nessuno opzionale nel significato:
      valore — il numero/stato misurato;
      fonte  — da DOVE viene (file, comando, query). "lo dice il sensore" non e' una fonte;
      eta_s  — quanti secondi ha il dato. None = eta' ignota, che e' peggio di vecchia."""
    return {"valore": valore, "fonte": fonte,
            "eta_s": None if eta_s is None else round(float(eta_s), 1),
            "ts": ts or ora_iso()}


def dato_utilizzabile(tr, max_eta_s):
    """(bool, motivo). La regola che il 21/08 mancava: un dato vecchio non
    giustifica un'azione, la rimanda.

    Eta' IGNOTA e' trattata come inutilizzabile: e' il caso peggiore, perche' un
    numero senza eta' sembra fresco quanto uno appena letto."""
    if tr is None:
        return False, "misura assente: attendi/rimisura"
    if tr.get("valore") is None:
        return False, f"valore non letto (fonte: {tr.get('fonte')}): attendi/rimisura"
    eta = tr.get("eta_s")
    if eta is None:
        return False, (f"eta' del dato ignota (fonte: {tr.get('fonte')}): "
                       f"attendi/rimisura, un dato senza eta' non puo' decidere")
    if eta > max_eta_s:
        return False, (f"dato vecchio {int(eta)}s > {int(max_eta_s)}s "
                       f"(fonte: {tr.get('fonte')}): attendi/rimisura")
    return True, f"dato fresco {int(eta)}s (fonte: {tr.get('fonte')})"


# ---------------------------------------------------------------------------
# (b) FANTASMI: il soggetto dell'allarme esiste ancora?
# ---------------------------------------------------------------------------

def contesto_soggetti(db=None):
    """Fotografia di cio' che ESISTE adesso: finestre tmux, servizi, agenti (con
    il flag dismesso). Letta UNA volta per ciclo e passata a tutti i controlli —
    interrogare tmux e Postgres per ogni allarme costerebbe piu' del sensore."""
    finestre = set()
    try:
        out = subprocess.run(["tmux", "list-windows", "-a", "-F",
                              "#{session_name}:#{window_name}"],
                             capture_output=True, text=True, timeout=20).stdout
        finestre = {r.strip() for r in out.splitlines() if r.strip()}
    except Exception:
        finestre = set()
    agenti = {}
    if db is None:
        try:
            import agent_db
            db = agent_db.get_db()
        except Exception:
            db = {}
    for slug, d in (db or {}).items():
        d = d or {}
        agenti[slug.lower()] = {
            # CONVENZIONE DISMESSI: la stessa di sentinella_contesto.py:273
            # (COALESCE(data->>'dismesso','false')::boolean). Non se ne inventa
            # una seconda: due definizioni di "dismesso" sono due verita'.
            "dismesso": bool(d.get("dismesso")),
            "tmux_window": d.get("tmux_window"),
        }
    servizi = {w.split(":", 1)[1] for w in finestre if ":" in w and w.split(":", 1)[1].startswith("SRV-")}
    return {"finestre": finestre, "finestre_lower": {w.lower() for w in finestre},
            "agenti": agenti, "servizi": servizi,
            "letto_il": time.time()}


def soggetto_esiste(genere, valore, ctx):
    """(esiste, motivo). 'Non lo so' vale come ESISTE: dichiarare fantasma un
    soggetto solo perche' non si e' riusciti a leggere tmux sarebbe lo stesso
    errore, al rovescio."""
    if not genere or not valore:
        return True, "soggetto non identificato: non si puo' dire che sia un fantasma"
    if genere == "finestra":
        if not ctx.get("finestre"):
            return True, "elenco finestre non letto (fail-safe: non e' un fantasma)"
        return (valore.lower() in ctx["finestre_lower"],
                "finestra presente" if valore.lower() in ctx["finestre_lower"]
                else f"finestra tmux '{valore}' NON esiste")
    if genere == "servizio":
        if not ctx.get("finestre"):
            return True, "elenco finestre non letto (fail-safe: non e' un fantasma)"
        return (valore in ctx["servizi"],
                "servizio registrato" if valore in ctx["servizi"]
                else f"servizio '{valore}' non registrato fra le finestre services:SRV-*")
    if genere == "agente":
        a = ctx.get("agenti", {}).get(valore.lower())
        if a is None:
            if not ctx.get("agenti"):
                return True, "DB agenti non letto (fail-safe: non e' un fantasma)"
            return False, f"agente '{valore}' non esiste nel DB"
        if a["dismesso"]:
            return False, f"agente '{valore}' e' DISMESSO nel DB"
        tw = a.get("tmux_window")
        if tw and ctx.get("finestre") and tw.lower() not in ctx["finestre_lower"]:
            return False, (f"agente '{valore}' non dismesso ma la sua finestra "
                           f"'{tw}' non esiste")
        return True, "agente vivo nel DB"
    return True, "genere di soggetto sconosciuto"


def impronta_soggetto(genere, valore, ctx, misura=None):
    """Impronta CORTA dello stato osservabile del soggetto. Serve a (a): se
    l'impronta cambia, il soggetto e' cambiato e il contatore di ripetizioni
    riparte — l'allarme sta seguendo qualcosa di vivo, non ripetendosi a vuoto.

    `misura` = (etichetta, valore_a_fasce) da valore_misurato(). Senza di essa,
    sette allarmi di carico con load 91/32/43/39/24 avevano tutti impronta
    'esiste=True' e risultavano immobili: verdetto "si ripete senza cambio di
    stato" su sette misure diverse e tutte vere (samantha_2, ticket 2351)."""
    esiste, _ = soggetto_esiste(genere, valore, ctx)
    if genere == "agente":
        a = ctx.get("agenti", {}).get((valore or "").lower()) or {}
        base = f"esiste={esiste};dismesso={a.get('dismesso')};win={a.get('tmux_window')}"
    else:
        base = f"esiste={esiste}"
    if misura:
        base += f";{misura[0]}={misura[1]}"
    return base


# ---------------------------------------------------------------------------
# (a) RIPETIZIONE: lo stesso allarme che torna mentre il soggetto non cambia
# ---------------------------------------------------------------------------

class RegistroFirme:
    """Memoria persistente delle firme viste. Persistente e non in RAM perche' il
    guardiano si riavvia: un contatore che riparte da zero ad ogni riavvio non
    vedrebbe MAI una ripetizione lunga — ed e' proprio quella che conta
    (deepseek_spend_guard ha ripetuto lo stesso allarme per ore)."""

    def __init__(self, path=REGISTRO_FIRME_PATH, orologio=time.time):
        self.path = path
        self.orologio = orologio
        self.dati = _carica_json(path, {})

    def osserva(self, firma, impronta, timestamps=None):
        """Registra le occorrenze reali, non i cicli. Ritorna il dossier:
        {n, impronta, primo_ts, ultimo_ts, span_storico_s, segnalata}."""
        ora = self.orologio()
        timestamps = timestamps or [ora]
        d = self.dati.get(firma)
        if d is None or d.get("impronta") != impronta:
            d = {"n": len(timestamps), "impronta": impronta, "primo_ts": min(timestamps),
                 "ultimo_ts": max(timestamps), "segnalata": False, "visti": list(timestamps)}
        else:
            visti = set(d.get("visti", []))
            nuovi = [t for t in timestamps if t not in visti]
            if nuovi:
                d["n"] += len(nuovi)
                d["ultimo_ts"] = max(d.get("ultimo_ts", ora), max(nuovi))
                d["visti"] = list(visti) + nuovi
        self.dati[firma] = d
        return dict(d, firma=firma, span_storico_s=d["ultimo_ts"] - d["primo_ts"])

    def marca_segnalata(self, firma):
        if firma in self.dati:
            self.dati[firma]["segnalata"] = True

    def dimentica_assenti(self, firme_viste, max_eta_s=86400):
        """Le firme che non si vedono piu' da un giorno spariscono: un registro
        che cresce per sempre finisce per rallentare ogni ciclo."""
        ora = self.orologio()
        for f in [k for k, v in self.dati.items()
                  if k not in firme_viste and ora - v.get("ultimo_ts", ora) > max_eta_s]:
            self.dati.pop(f, None)

    def salva(self):
        _salva_json(self.path, self.dati)


# ---------------------------------------------------------------------------
# RACCOLTA DEGLI ALLARMI ALTRUI
# ---------------------------------------------------------------------------

def scopri_guardiani(conf):
    """Chi sorvegliare: la lista in conf (estendibile a mano) UNITA a quello che
    gira davvero come servizio (services:SRV-*). L'unione, non l'una o l'altra:
    la conf da sola non vede un guardiano nuovo acceso stanotte, e i servizi da
    soli non vedono un guardiano che gira da cron."""
    gc = conf.get("guardiani", {})
    noti = list(gc.get("sorvegliati", []))
    try:
        out = subprocess.run(["tmux", "list-windows", "-t", "services", "-F",
                              "#{window_name}"], capture_output=True, text=True,
                             timeout=20).stdout
        for w in out.splitlines():
            w = w.strip()
            if not w.startswith("SRV-"):
                continue
            slug = w[4:].lower().replace("-", "_")
            if slug not in noti and slug in gc.get("mappa_servizi", {}):
                noti.append(gc["mappa_servizi"][slug])
            elif gc.get("scopri_da_servizi", True) and slug not in noti:
                noti.append(slug)
    except Exception:
        pass
    return sorted(set(noti))


# Mittenti che NON identificano nessuno: i ticket del watchdog arrivano con
# questa intestazione e vanno risolti guardando la firma DICHIARATA nel corpo.
_MITTENTI_VUOTI = {"sconosciuto", "unknown", "", "none", "null"}
# Firma dichiarata in coda al corpo, nella forma '- resource_watchdog]'
_RE_FIRMA_CORPO = re.compile(r"[-–]\s*([a-z0-9_]{4,40})\s*\]")


def _guardiano_del_testo(testo, guardiani):
    """Chi ha emesso l'allarme. Solo fonti DICHIARATE, mai deduzioni dal testo.

    ⚠️ La prima versione, se l'intestazione mancava, prendeva "il primo nome di
    guardiano citato nel corpo". Verificato da samantha_2 sul ticket bdce: un
    allarme di resource_watchdog che nominava anche check_agenti_vivi veniva
    attribuito a check_agenti_vivi — cioe' il meta-sorvegliante accusava il
    guardiano sbagliato. Un allarme mal attribuito e' peggio di uno non
    attribuito: manda a debuggare il componente innocente.
    """
    t = testo or ""
    m = re.search(r"\[A2A_FROM:\s*([a-z0-9_\-]*)\s*\]", t, re.I)
    mittente = (m.group(1).lower().replace("-", "_") if m else "")
    if mittente and mittente not in _MITTENTI_VUOTI:
        return mittente
    # mittente assente o 'sconosciuto': vale la firma DICHIARATA nel corpo
    for fm in _RE_FIRMA_CORPO.finditer(t):
        cand = fm.group(1).lower()
        if cand in guardiani:
            return cand
    return "mittente_non_dichiarato"


def _id_ticket(testo, nome_file):
    """ID della sorgente: [A2A_ID:...] se c'e', altrimenti il nome del file.

    Serve a non contare DUE VOLTE lo stesso ticket: lo stesso messaggio viene
    depositato in piu' caselle, e senza questo finiva in due firme diverse
    (segnalato da samantha_2, ticket bdce)."""
    m = re.search(r"\[A2A_ID:\s*([A-Za-z0-9_\-]+)\s*\]", testo or "")
    return m.group(1) if m else os.path.basename(nome_file or "")


# ===== VALORE MISURATO: l'impronta di un soggetto misurabile deve contenerlo ==
#
# ⚠️ Verificato da samantha_2 sul ticket 2351: sette allarmi di carico in 18 ore
# erano TUTTI VERI e il soggetto cambiava ogni volta (load 91 -> 32 -> 43 -> 39
# -> 24, sessioni diverse), ma l'impronta diceva solo 'esiste=True' e quindi
# risultava immobile: verdetto "si ripete senza cambio di stato" su sette
# misure diverse. Stessa cosa per lo spend_guard (saldo $1.95 -> $1.46 -> $1.34:
# cambia, e' un promemoria legittimo finche' nessuno ricarica).
#
# "Si ripete senza cambio di stato" vale solo se anche il VALORE e' fermo.
# Le fasce servono a non far ripartire il contatore per un decimale.
_RE_LOAD = re.compile(r"load\s*5?\s*[:= ]\s*(\d+(?:[.,]\d+)?)", re.I)
_RE_SALDO = re.compile(r"\$\s*(\d+(?:[.,]\d+)?)")
_RE_PCT = re.compile(r"(\d+(?:[.,]\d+)?)\s*%")
_RE_MB = re.compile(r"(\d+(?:[.,]\d+)?)\s*(?:mb|mib)\b", re.I)


def _num(s):
    try:
        return float(str(s).replace(",", "."))
    except Exception:
        return None


def valore_misurato(testo, tipo=None):
    """(etichetta, valore_a_fasce) del numero che l'allarme sta misurando, o None.

    Fasce: load a 5, saldo a 0.5$, percentuali a 10, RAM a 500MB. Sono grosse di
    proposito: devono distinguere una misura DIVERSA da un decimale che balla."""
    t = testo or ""
    m = _RE_LOAD.search(t)
    if m and (_num(m.group(1)) is not None):
        return ("load", round(_num(m.group(1)) / 5) * 5)
    m = _RE_SALDO.search(t)
    if m and (_num(m.group(1)) is not None):
        return ("saldo_usd", round(_num(m.group(1)) * 2) / 2)
    m = _RE_MB.search(t)
    if m and (_num(m.group(1)) is not None):
        return ("mb", round(_num(m.group(1)) / 500) * 500)
    m = _RE_PCT.search(t)
    if m and (_num(m.group(1)) is not None):
        return ("pct", round(_num(m.group(1)) / 10) * 10)
    return None


# I file A2A si chiamano '20260821T022746Z__mittente__hash.md': il timestamp e'
# NEL NOME. L'mtime NON lo e': a2a_ack.py sposta il file da inbox/ a read/, e
# ogni copia/rsync/backup lo riscrive. Verificato da samantha_1 sul ticket e6f3:
# quattro messaggi realmente distribuiti su DUE GIORNI (19/08 07:57, 20/08 15:18,
# 20/08 22:09, 21/08 02:27) risultavano compressi in 15.521s — e facevano
# scattare un verdetto di "guardiano rotto" su un allarme che era pure vero.
_RE_TS_NOME = re.compile(r"(\d{8})T(\d{6})Z")


def ts_da_nome_file(nome, ripiego=None):
    """Epoch dal nome del file A2A (UTC). `ripiego` (di solito l'mtime) e' usato
    solo se il nome non porta un timestamp — e in quel caso chi legge deve
    saperlo, perche' e' un dato meno affidabile."""
    m = _RE_TS_NOME.search(nome or "")
    if not m:
        return ripiego, "mtime (nome file senza timestamp)"
    try:
        d = datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")
        return d.replace(tzinfo=timezone.utc).timestamp(), "nome file"
    except Exception:
        return ripiego, "mtime (timestamp nel nome illeggibile)"


def caselle_a2a(radice="/root/ecosistema_agenti/a2a", solo=None):
    """Tutte le caselle a2a/<destinatario>/{inbox,read}.

    Perche' TUTTE e non solo quella del pool (richiesta samantha_2, 21/08): il
    pattern vero di un guardiano che si ripete non si vede sulla casella di chi
    riceve le diagnosi, ma su quella del DESTINATARIO degli allarmi. Esempio
    reale: drift_detector manda a enrico lo stesso promemoria ogni 4 ore da
    undici giorni — 33 messaggi identici, zero cambiamenti — e sulla casella del
    pool non se ne vedeva nemmeno uno."""
    fuori = []
    try:
        for nome in sorted(os.listdir(radice)):
            if solo is not None and nome not in solo:
                continue
            base = os.path.join(radice, nome)
            if not os.path.isdir(base) or nome.startswith("_"):
                continue
            for sotto in ("inbox",):
                d = os.path.join(base, sotto)
                if os.path.isdir(d):
                    fuori.append((nome, d))
    except Exception:
        pass
    return fuori


def allarmi_da_ticket(dirs, guardiani, ora=None, max_eta_s=86400, max_file=400):
    """Allarmi trovati nei ticket A2A del pool (inbox + read). E' la fonte piu'
    fedele di cosa i guardiani hanno DETTO davvero, ed e' quella che rende
    visibile la ripetizione: lo stesso allarme depositato cento volte lascia
    cento file."""
    ora = time.time() if ora is None else ora
    fuori = []
    for voce in dirs:
        # accetta sia "percorso" (uso storico) sia ("destinatario", "percorso")
        dest, d = voce if isinstance(voce, (tuple, list)) else (None, voce)
        try:
            nomi = sorted(os.listdir(d))[-max_file:]
        except Exception:
            continue
        for nome in nomi:
            p = os.path.join(d, nome)
            try:
                mt, fonte_ts = ts_da_nome_file(nome, os.path.getmtime(p))
                if mt is None or ora - mt > max_eta_s:
                    continue
                with open(p, encoding="utf-8", errors="replace") as f:
                    testo = f.read(4000)
            except Exception:
                continue
            g = _guardiano_del_testo(testo, guardiani)
            if not g or g not in guardiani:
                continue
            tid = _id_ticket(testo, nome)
            genere, sogg = estrai_soggetto(testo)
            tipo = normalizza_tipo(testo)
            fuori.append({"guardiano": g, "tipo": tipo,
                          "genere": genere, "soggetto": sogg,
                          "fonte": p, "eta_s": ora - mt,
                          "ts": mt, "ts_fonte": fonte_ts,
                          "destinatario": dest, "id_ticket": tid,
                          "sintesi": sintesi_allarme(testo, tipo, sogg),
                          "valore_misurato": valore_misurato(testo, tipo),
                          "estratto": " ".join(testo.split())[:160]})
    return fuori


def allarmi_dal_ledger(path=LEDGER_PATH, ora=None, max_eta_s=86400, max_righe=3000):
    """(d) SE STESSO. Gli allarmi del guardiano stanno nel suo ledger: qui si
    rileggono con gli stessi occhi con cui si guardano quelli degli altri.
    Si leggono solo le ultime righe: il ledger e' append-only e cresce."""
    ora = time.time() if ora is None else ora
    fuori = []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            righe = f.readlines()[-max_righe:]
    except Exception:
        return fuori
    for r in righe:
        try:
            d = json.loads(r)
        except Exception:
            continue
        ev = d.get("evento", "")
        # ⚠️ TARATURA CORRETTA DA samantha_2 (21/08 20:46, ticket b8a4).
        # La prima versione contava come "ripetizione" anche le MISURE DI STATO
        # che il guardiano ristampa ad ogni ciclo per mestiere (cpu, saldo,
        # heartbeat del proprio servizio, attendi_reset). Quattro cicli da 27s
        # producevano "stessa firma 4 volte in 108s" e un verdetto di guardiano
        # rotto: ma una misura ripetuta ad ogni ciclo non e' un guasto, e' il
        # suo lavoro. Su 13 auto-anomalie, 11 erano di questa specie.
        # Contano solo gli eventi che CHIEDONO un'azione o un ticket.
        if ev not in EVENTI_CHE_CHIEDONO_AZIONE:
            continue
        azione = d.get("azione") or ""
        if azione in SOLO_LEDGER or azione in AZIONI_NON_ALLARME:
            continue
        if str(d.get("chiave") or "").startswith(CHIAVI_ALERT_DI_STATO):
            continue
        try:
            ts = datetime.fromisoformat(d["ts"]).timestamp()
        except Exception:
            continue
        if ora - ts > max_eta_s:
            continue
        sogg = d.get("tmux_window") or d.get("agente") or d.get("servizio")
        if sogg and ":" in str(sogg):
            genere = "finestra"
        elif sogg and str(sogg).startswith("SRV-"):
            genere = "servizio"
        elif sogg:
            genere = "agente"
        else:
            genere, sogg = estrai_soggetto(json.dumps(d, ensure_ascii=False))
        descr = f"{d.get('azione','')} {d.get('motivo','')} {d.get('perche','')}".strip()
        tipo = normalizza_tipo(f"{ev} {descr}")
        fuori.append({"guardiano": "guardiano", "tipo": tipo,
                      "genere": genere, "soggetto": sogg,
                      "fonte": f"{path}#{ev}", "eta_s": ora - ts,
                      "sintesi": sintesi_allarme(descr or ev, tipo, sogg, evento=ev),
                      "estratto": json.dumps(d, ensure_ascii=False)[:160],
                      "evento": ev})
    return fuori


def sensore_guardiani(conf, db=None, registro=None, ora=None):
    """F4. Applica (a) ripetizione, (b) fantasmi, (c) tracciabilita' agli allarmi
    di TUTTI i guardiani, compreso (d) se stesso.

    Ritorna {guardiani, verdetti, allarmi_esaminati, ctx_eta_s}: OSSERVAZIONE,
    nessuna azione. I verdetti diventano ticket in decisioni_guardiani()."""
    ora = time.time() if ora is None else ora
    gc = conf.get("guardiani", {})
    if not gc.get("abilitato", True):
        return {"abilitato": False, "verdetti": [], "allarmi_esaminati": 0,
                "guardiani": []}
    guardiani = scopri_guardiani(conf)
    ctx = contesto_soggetti(db)
    registro = registro if registro is not None else RegistroFirme()
    soglia_rip = gc.get("ripetizioni_soglia", 3)
    finestra_min = gc.get("finestra_minima_ripetizione_s", 900)
    intervallo_max = gc.get("intervallo_medio_max_s", 21600)      # 6h
    molte = gc.get("ripetizioni_molte", 8)
    max_eta_allarme = gc.get("allarme_max_eta_s", 86400)
    max_eta_dato = gc.get("dato_max_eta_s", 3600)

    # Le caselle da guardare: quelle elencate in conf PIU', se abilitato, tutte
    # le a2a/<destinatario>/{inbox,read}. Il pattern di un guardiano che si
    # ripete si vede sulla casella di CHI RICEVE i suoi allarmi, non su quella
    # del pool: drift_detector manda a enrico lo stesso promemoria ogni 4 ore da
    # undici giorni, e sul pool non se ne vedeva nemmeno uno (samantha_2, 21/08).
    cartelle = list(gc.get("cartelle_ticket", []))
    if gc.get("tutte_le_caselle", True):
        cartelle += caselle_a2a(gc.get("radice_a2a", "/root/ecosistema_agenti/a2a"))
    # ⚠️ LA SCANSIONE COSTA. 268 caselle x 2 sottocartelle = oltre 500 listdir e
    # qualche migliaio di open: misurato in produzione, un ciclo passava da 15s a
    # 110-300s quando la cache del filesystem era fredda. Un guardiano cieco per
    # cinque minuti e' un problema vero, e per giunta di quelli che questo
    # sensore denuncia negli altri. La meta-sorveglianza non ha bisogno della
    # granularita' del minuto: un guardiano che si ripete lo fa per ore.
    # Fra una scansione e l'altra si riusa il risultato precedente, dichiarandone
    # l'eta' (regola (c): un dato che decide deve dire quanti secondi ha).
    global _CACHE_ALLARMI
    ogni = gc.get("scansione_caselle_ogni_s", 300)
    # La chiave e' l'elenco ESATTO delle cartelle, non il loro numero: due
    # insiemi diversi della stessa dimensione sono due scansioni diverse, e
    # confonderle servirebbe dati di un'altra cartella spacciandoli per questi.
    chiave = tuple(str(c) for c in cartelle)
    if (ogni > 0 and _CACHE_ALLARMI and _CACHE_ALLARMI["chiave"] == chiave
            and ora - _CACHE_ALLARMI["ts"] < ogni):
        allarmi = _CACHE_ALLARMI["allarmi"]
        eta_scansione = ora - _CACHE_ALLARMI["ts"]
    else:
        allarmi = allarmi_da_ticket(cartelle, guardiani, ora=ora,
                                    max_eta_s=max_eta_allarme,
                                    max_file=gc.get("file_per_casella_max", 120))
        if ogni > 0:
            _CACHE_ALLARMI = {"ts": ora, "allarmi": allarmi, "chiave": chiave}
        eta_scansione = 0.0
    if gc.get("include_se_stesso", True):
        allarmi += allarmi_dal_ledger(ora=ora, max_eta_s=max_eta_allarme)

    # ⚠️ SI RAGIONA PER FIRMA, NON PER OCCORRENZA (correzione dopo la prova sul
    # vivo: 13 auto-anomalie erano diventate 69). Ciclando sugli allarmi grezzi
    # una firma vista 35 volte produceva 35 verdetti identici, uno per ogni
    # occorrenza, con il contatore che saliva dentro lo stesso ciclo. Un
    # guardiano che si ripete e' UN problema, e va detto UNA volta: il numero di
    # occorrenze e' un attributo di quel problema, non un moltiplicatore.
    #
    # Le occorrenze nella finestra osservata sono anche la misura ONESTA della
    # ripetizione ("ha rifatto lo stesso allarme 35 volte"), molto piu' del
    # numero di cicli in cui il registro ha visto la firma.
    per_firma = {}
    visti_ticket = set()
    for a in allarmi:
        # DEDUP PER ID TICKET: lo stesso messaggio viene depositato in piu'
        # caselle, e senza questo finiva contato due volte, su firme diverse
        # (samantha_2, ticket bdce).
        tid = a.get("id_ticket")
        if tid:
            if tid in visti_ticket:
                continue
            visti_ticket.add(tid)
        f = firma_allarme(a["guardiano"], a["tipo"], a["soggetto"])
        v = per_firma.get(f)
        if v is None:
            per_firma[f] = {"allarme": a, "occorrenze": 1,
                            "eta_min": a.get("eta_s"), "eta_max": a.get("eta_s"),
                            "campioni": [], "misure": set()}
            v = per_firma[f]
        else:
            v["occorrenze"] += 1
            e = a.get("eta_s")
            if e is not None:
                # si tiene l'allarme PIU' RECENTE come rappresentante: e' quello
                # che descrive cosa sta succedendo adesso
                if v["eta_min"] is None or e < v["eta_min"]:
                    v["eta_min"], v["allarme"] = e, a
                if v["eta_max"] is None or e > v["eta_max"]:
                    v["eta_max"] = e
        # ORARI REALI DI OGNI CAMPIONE. Richiesta esplicita di samantha_2: un
        # errore di conteggio deve potersi vedere dal ticket, senza aprire i file.
        if a.get("ts"):
            v["campioni"].append((a["ts"], a.get("ts_fonte"), a.get("destinatario")))
        vm = a.get("valore_misurato")
        if vm:
            v["misure"].add(vm)

    verdetti, firme_viste, stale_ignorati = [], set(), 0
    for firma, gruppo in per_firma.items():
        a = gruppo["allarme"]
        firme_viste.add(firma)
        # se i valori misurati nella finestra sono PIU' D'UNO, il soggetto si e'
        # mosso: non e' una ripetizione a vuoto, e l'impronta deve dirlo.
        misure = sorted(gruppo.get("misure") or [])
        impronta = impronta_soggetto(a["genere"], a["soggetto"], ctx,
                                     misura=misure[0] if len(misure) == 1 else None)
        if len(misure) > 1:
            impronta += f";valori_diversi={len(misure)}"
        campioni_ts = [c[0] for c in gruppo.get("campioni", [])]
        dossier = registro.osserva(firma, impronta, campioni_ts)
        esiste, motivo_es = soggetto_esiste(a["genere"], a["soggetto"], ctx)
        tr = traccia(a.get("estratto"), a["fonte"], a["eta_s"])
        usabile, motivo_dato = dato_utilizzabile(tr, max_eta_dato)

        eta_ultima = None if gruppo["eta_min"] is None else round(gruppo["eta_min"])
        prima_occorrenza = None if gruppo["eta_max"] is None else round(gruppo["eta_max"])
        span_s = (0 if eta_ultima is None or prima_occorrenza is None
                  else prima_occorrenza - eta_ultima)

        # 🎯 FINESTRA TEMPORALE META-ANALISI
        # Se l'allarme più recente è fuori dalla finestra, il problema è finito
        # e non va segnalato. Se invece l'ultimo allarme è dentro la finestra,
        # il problema è ancora in corso e si valutano tutte le sue occorrenze storiche.
        finestra_meta_ore = gc.get("finestra_meta_ore")
        if finestra_meta_ore is not None and eta_ultima is not None and eta_ultima > finestra_meta_ore * 3600:
            continue

        
        ripetizioni = dossier["n"]
        da_s = dossier["span_storico_s"]

        campioni_orari = [
            datetime.fromtimestamp(c[0]).astimezone().isoformat(timespec="minutes")
            for c in sorted(gruppo["campioni"])[:12]]

        intervallo_medio = (da_s / (ripetizioni - 1)) if ripetizioni > 1 else None
        fitte = intervallo_medio is not None and intervallo_medio <= intervallo_max
        tante = ripetizioni >= molte

        base = {"guardiano": a["guardiano"], "tipo": a["tipo"],
                "soggetto": a["soggetto"], "genere": a["genere"],
                "firma": firma, "ripetizioni": ripetizioni,
                "occorrenze_nella_finestra": gruppo["occorrenze"],
                "da_s": da_s, "span_allarmi_s": span_s,
                "valore": a.get("sintesi") or tr["valore"],
                "fonte": tr["fonte"],
                "eta_dato_s": eta_ultima,
                "eta_ultima_ripetizione_s": eta_ultima,
                "prima_occorrenza_s_fa": prima_occorrenza,
                "impronta": impronta,
                "intervallo_medio_s": None if intervallo_medio is None else round(intervallo_medio),
                "campioni_orari": campioni_orari,
                "campioni_fonte_ts": sorted({c[1] for c in gruppo["campioni"] if c[1]}),
                "destinatari": sorted({c[2] for c in gruppo["campioni"] if c[2]}),
                "valori_misurati": [f"{m[0]}={m[1]}" for m in misure],
                "auto": a["guardiano"] == "guardiano"}

        # (b) FANTASMI — ha la precedenza: un allarme su un soggetto inesistente
        # non va nemmeno contato come emergenza, qualunque sia il conteggio.
        #
        # ⚠️ MA SOLO SU ALLARMI FRESCHI: un allarme di un'ora fa su un exec
        # chiuso regolarmente nel frattempo NON era un fantasma quando e' stato
        # emesso — la finestra c'era. Giudicare la storia con la fotografia di
        # adesso trasformava ogni exec chiuso in un difetto del guardiano che
        # l'aveva correttamente sorvegliato da vivo.
        if not esiste and usabile:
            verdetti.append(dict(base,
                verdetto="auto_anomalia" if base["auto"] else "guardiano_misura_fantasmi",
                livello="GIALLO", perche=f"{motivo_es} — l'allarme misura un fantasma",
                degrada=True))
            continue

        # (a) RIPETIZIONE — stessa firma oltre soglia, soggetto IMMUTATO, e su
        # una finestra di tempo davvero significativa. "Quattro righe" non e' una
        # ripetizione: sono quattro cicli consecutivi (correzione samantha_2).
        misura_ferma = len(misure) <= 1
        if (ripetizioni > soglia_rip and da_s >= finestra_min
                and misura_ferma and (fitte or tante)):
            verdetti.append(dict(base,
                verdetto="auto_anomalia" if base["auto"] else "guardiano_rotto",
                livello="GIALLO",
                perche=(f"stesso allarme {ripetizioni} volte, uno ogni ~"
                        f"{round(intervallo_medio/60) if intervallo_medio else '?'} min: "
                        f"prima occorrenza {prima_occorrenza}s fa, ultima {eta_ultima}s fa, "
                        f"senza alcun cambio di stato del soggetto "
                        f"(impronta invariata: {impronta}) — non e' un'emergenza "
                        f"che peggiora, e' un guardiano che si ripete. "
                        f"Campioni ({len(gruppo['campioni'])} con orario, fonte "
                        f"{'/'.join(sorted({c[1] for c in gruppo['campioni'] if c[1]}) or ['n/d'])}): "
                        + ", ".join(campioni_orari)),
                degrada=True))
            continue

        # (c) TRACCIABILITA' — un allarme semplicemente VECCHIO ma con eta' nota
        # e' storia, non un difetto: alla prima prova ne sono usciti 963 verdetti
        # su 1008 allarmi, rumore che avrebbe sepolto i guasti veri. Resta un
        # verdetto solo quando il dato non e' usabile per un motivo che NON e' il
        # tempo — eta' ignota o valore mai letto: quelli si', perche' un numero
        # senza eta' sembra fresco quanto uno appena misurato.
        if not usabile:
            if tr.get("eta_s") is not None and tr.get("valore") is not None:
                stale_ignorati += 1
            else:
                verdetti.append(dict(base, verdetto="dato_non_utilizzabile",
                    livello="VERDE", perche=f"{motivo_dato} — attendi/rimisura, "
                                            f"non e' materiale per un'azione",
                    degrada=True))

    registro.dimentica_assenti(firme_viste)
    registro.salva()
    return {"abilitato": True, "guardiani": guardiani, "verdetti": verdetti,
            "allarmi_esaminati": len(allarmi),
            "caselle_scandite": len(cartelle),
            "scansione_eta_s": round(eta_scansione, 1),
            "storici_non_giudicati": stale_ignorati,
            "ctx_eta_s": round(time.time() - ctx["letto_il"], 1)}


def decisioni_guardiani(meta, conf, registro=None):
    """Da verdetti a ticket. Tre regole, tutte nate dal primo dry-run reale:

    1. UN ticket per FIRMA, una volta sola finche' lo stato non cambia. Il dedup
       sta nel registro (campo 'segnalata') e non nel Dedup a tempo: qui non
       serve un cooldown, serve "non ripeterlo finche' la situazione e' la
       stessa" — se il soggetto cambia, il contatore riparte e la firma torna
       segnalabile.
    2. LE AUTO-ANOMALIE (d) si aggregano in UN SOLO ticket. Il mandato lo chiede
       ("lo segnala al pool una volta sola") e il dry-run ha mostrato perche':
       rileggendo il proprio ledger il guardiano trovava decine di firme insieme.
    3. TETTO PER CICLO sugli altri. Al primo giro il registro e' vuoto e ogni
       firma storica sembra nuova: senza tetto il primo ciclo dopo il deploy
       avrebbe scaricato quaranta ticket nella casella del pool — cioe' esatta-
       mente il comportamento (allarme ripetuto a raffica) che questo sensore
       esiste per denunciare. Il resto non si perde: finisce nel ledger e nel
       ticket di riepilogo, e i cicli seguenti lo riprendono a poco a poco.
    """
    registro = registro if registro is not None else RegistroFirme()
    gc = conf.get("guardiani", {})
    tetto = gc.get("ticket_per_ciclo_max", 3)
    dec, auto, rimandati = [], [], 0
    for v in meta.get("verdetti", []):
        if v.get("verdetto") == "dato_non_utilizzabile":
            # tracciato nel ledger, non merita un ticket per se': diventa un
            # ticket solo se accompagna una ripetizione o un fantasma
            continue
        d = registro.dati.get(v["firma"]) or {}
        if d.get("segnalata"):
            continue
        if v.get("verdetto") == "auto_anomalia":
            auto.append(v)
            registro.marca_segnalata(v["firma"])
            continue
        if len(dec) >= tetto:
            rimandati += 1
            continue
        dec.append(dict(v, azione="ticket_meta_guardiano"))
        registro.marca_segnalata(v["firma"])

    if auto:
        # (d) UN solo ticket per tutte le anomalie che il guardiano trova su di se'.
        righe = [f"{a['tipo']}/{a['soggetto']}: {a['perche'][:110]}" for a in auto[:8]]
        dec.append({
            "azione": "ticket_meta_guardiano", "livello": "GIALLO",
            "agente": "guardiano", "guardiano": "guardiano",
            "verdetto": "auto_anomalia", "tipo": "auto_anomalia", "soggetto": None,
            "chiave_dedup": f"auto_anomalia:{len(auto)}:{sorted(a['firma'] for a in auto)[0]}",
            "firma": "guardiano|auto_anomalia|aggregata",
            "valore": f"{len(auto)} auto-anomalie",
            "fonte": LEDGER_PATH, "eta_dato_s": 0,
            "perche": ("AUTO-DIAGNOSI: rileggendo il proprio ledger il guardiano ha "
                       f"trovato {len(auto)} allarmi propri che non reggono l'esame "
                       "(soggetto inesistente o stessa firma ripetuta senza cambio "
                       "di stato). Segnalato una volta sola, come da mandato.\n• "
                       + "\n• ".join(righe)),
        })
    if rimandati:
        ledger("meta_ticket_rimandati", quanti=rimandati, tetto=tetto,
               perche="tetto per ciclo: il resto torna nei cicli successivi, "
                      "non si scarica la casella del pool in una volta")
    registro.salva()
    return dec


# ---------------------------------------------------------------------------
# (e) SENSORE 'ordine_fermo_in_casella'
#
# Trovato da samantha_1 il 21/08/2026: 8 pannelli su 38 avevano un ordine di
# Manfredo COMPLETO fermo nella casella di input di Claude Code, mai sottomesso
# al modello, da 6 a 36 minuti. Processo vivo, finestra sana, coda A2A a zero:
# nessun guardiano lo vedeva, perche' tutti guardano il processo o la coda A2A e
# il canale rotto qui e' la TASTIERA — l'unica via che usa Manfredo quando
# scrive a mano in tmux.
#
# ⚠️ QUESTO SENSORE NON SBLOCCA NULLA. Rilanciare un ordine gia' eseguito lo
# esegue DUE VOLTE, e su ordini come "procedi con l'iscrizione al corso" o
# "accendi Orazio, Edoardo ed Ellie" il doppio non e' un fastidio, e' un danno.
# Il rimedio validato 8/8 (C-u -> ri-paste dello STESSO testo -> End -> C-m;
# Invio nudo e End+C-m NON bastano) e' in core/tampone_ordini_fermi.py di
# samantha_1 e nel lavoro di exec_ordini_fermi: qui si RILEVA e si apre un ticket.
# Il flag conf 'sblocca_ordine_fermo' esiste ed e' false: e' il punto in cui
# quella capacita' andra' innestata, quando qualcuno decidera' che puo' agire.
# ---------------------------------------------------------------------------

# Footer e testi della TUI che il parser dell'input box puo' scambiare per un
# ordine. Stessa lista del tampone di samantha_1: due liste diverse darebbero
# due verita' diverse sullo stesso pannello.
RUMORE_INPUT = ("Accept-edits mode", "bypass permissions", "shift+tab", "to cycle",
                "esc to interrupt", "Type your message", "to manage")


def ordine_in_casella(pane):
    """Testo davvero in attesa nella casella, o None. Scarta il rumore di UI."""
    try:
        from send_a2a import _input_box_da_testo
    except Exception:
        return None
    try:
        pend = (_input_box_da_testo(pane) or "").strip()
    except Exception:
        return None
    pend = re.sub(r"^❯\s*", "", pend).strip()
    if not pend or pend.startswith(">"):
        return None
    if any(r in pend for r in RUMORE_INPUT):
        return None
    return pend


def gia_nel_trascritto(pane, testo):
    """Vero se il testo compare GIA' sopra l'ultimo prompt: allora e' stato
    eseguito e non e' un ordine fermo. Prima prova anti-doppia-esecuzione."""
    righe = (pane or "").split("\n")
    idx = max((j for j, x in enumerate(righe) if x.strip().startswith("❯")),
              default=len(righe))
    return bool(testo) and testo[:25] in "\n".join(righe[:idx])


def gia_nel_jsonl(slug, testo, db=None, max_bytes=400000):
    """(trovato, fonte). PROVA DEFINITIVA della spec: il .jsonl della
    conversazione contiene gia' quel messaggio utente?

    Il trascritto a schermo e' uno scrollback che scorre via; il .jsonl e' il
    registro vero della conversazione. Ritorna (None, motivo) quando non si e'
    potuto verificare: assenza di prova NON e' prova di assenza, e chi
    eventualmente agira' deve poter distinguere i due casi."""
    if not testo:
        return None, "nessun testo da cercare"
    try:
        if db is None:
            import agent_db
            db = agent_db.get_db()
        d = (db or {}).get(slug) or {}
        conv = d.get("conv_id")
        wd = d.get("work_dir")
        if not conv or not wd:
            return None, f"conv_id/work_dir assenti nel DB per '{slug}'"
        cartella = "-" + re.sub(r"[^A-Za-z0-9]+", "-", wd.strip("/")).strip("-")
        p = os.path.join("/root/.claude/projects", cartella, f"{conv}.jsonl")
        if not os.path.exists(p):
            return None, f"trascritto jsonl non trovato: {p}"
        dim = os.path.getsize(p)
        with open(p, encoding="utf-8", errors="replace") as f:
            if dim > max_bytes:
                f.seek(dim - max_bytes)
            blob = f.read()
        return (testo[:40] in blob), p
    except Exception as e:
        return None, f"verifica jsonl fallita: {type(e).__name__}: {e}"


def sensore_ordini_fermi(conf, quota, classi, persistenza, db=None, ora=None):
    """(e) Un ordine completo fermo nella casella di input di un pannello idle.

    Condizioni, TUTTE necessarie (la spec di samantha_1):
      1. casella non vuota e pannello NON in generazione;
      2. il testo non compare gia' nel trascritto sopra l'ultimo prompt;
      3. la stessa identica casella persiste oltre la soglia (default 5 min):
         un ordine appena incollato non e' fermo, e' appena arrivato;
      4. prova definitiva: il .jsonl della conversazione non ha quel messaggio.
    Ogni segnalazione porta valore + fonte + eta', come gli altri controlli F4."""
    ora = time.time() if ora is None else ora
    gc = conf.get("guardiani", {})
    if not gc.get("ordini_fermi_abilitato", True):
        return []
    soglia = gc.get("ordine_fermo_soglia_s", 300)
    try:
        from engine_adapter import is_pane_generating
    except Exception:
        is_pane_generating = None
    fuori = []
    for a in quota.get("agenti", []):
        win = a["tmux_window"]
        pane = capture_pane(a["sessione"], a["finestra"], righe=80)
        testo = ordine_in_casella(pane)
        if not testo:
            persistenza.aggiorna(f"ordine_fermo:{win}", False)
            continue
        if is_pane_generating is not None:
            try:
                if is_pane_generating(a.get("engine_reale") or "claude", pane):
                    persistenza.aggiorna(f"ordine_fermo:{win}", False)
                    continue
            except Exception:
                pass
        if gia_nel_trascritto(pane, testo):
            # gia' eseguito: NON e' un ordine fermo, e soprattutto non va rilanciato
            persistenza.aggiorna(f"ordine_fermo:{win}", False)
            continue
        # la persistenza si azzera se il TESTO cambia: e' un ordine diverso, e
        # il cronometro deve ripartire da capo
        chiave = f"ordine_fermo:{win}:{hashlib.sha1(testo.encode('utf-8', 'replace')).hexdigest()[:10]}"
        durata = persistenza.aggiorna(chiave, True)
        if durata < soglia:
            continue
        slug = classi.get(win, {}).get("slug")
        nel_jsonl, fonte_jsonl = (None, "slug sconosciuto")
        if slug:
            nel_jsonl, fonte_jsonl = gia_nel_jsonl(slug, testo, db=db)
        if nel_jsonl is True:
            # il modello l'ha gia' ricevuto: la casella e' un frame fantasma
            persistenza.aggiorna(chiave, False)
            continue
        fuori.append({
            "azione": "ordine_fermo_in_casella", "livello": "GIALLO",
            "agente": slug, "tmux_window": win,
            "valore": testo[:120],
            "fonte": f"tmux capture-pane -t {win} + {fonte_jsonl}",
            "eta_dato_s": 0,
            "fermo_da_s": round(durata),
            "verificato_su_jsonl": nel_jsonl is False,
            "perche": (f"ordine completo fermo nella casella da {round(durata)}s "
                       f"(soglia {soglia}s), pannello non in generazione e testo "
                       f"assente dal trascritto"
                       + (" e dal .jsonl della conversazione"
                          if nel_jsonl is False
                          else f"; .jsonl NON verificato ({fonte_jsonl}) — "
                               f"assenza di prova non e' prova di assenza")),
            "rimedio": ("C-u + ri-paste dello STESSO testo + End + C-m "
                        "(Invio nudo e End+C-m NON sbloccano). "
                        "NON eseguito: azione disabilitata in conf."),
        })
    return fuori


# ---------------------------------------------------------------------------
# SENSORE 8 — CONTESTO (F-A, 22/08/2026, exec_fa_guardiano) — SOLA OSSERVAZIONE
#
# Quello che oggi fanno due cron (sentinella_contesto 4/h, sensore_memoria_leggera
# 6/h) con lo stesso cuore (misura_transcript): quanti token ha in finestra ogni
# agente claude, rispetto alla finestra REALE del suo processo.
#   - ogni `contesto.intervallo_s` (600 s): leggere 32 transcript costa I/O;
#   - soglie PER MODELLO dal processo: CLAUDE_CODE_MAX_CONTEXT_TOKENS dall'environ
#     del CLI (stesso ambiente che il launcher costruisce), ripiego alla config
#     del provider, ripiego alla finestra di default della conf;
#   - dedup per agente+fascia (verde/giallo/rosso): un evento quando la fascia
#     CAMBIA, non a ogni scansione; il rientro e' un evento ('rientrato');
#   - un solo canale, CALCOLATO e scritto nel log (a2a all'agente se idle,
#     altrimenti al suo coordinatore) ma NON USATO.
# ⚠️ SOLA OSSERVAZIONE (mandato F-A): qui non parte nessun A2A di rinfresco e
# non si tocca nessun cron. Le cron sentinella/sensore restano dove sono finche'
# Manfredo non decide. Il drift dei tracker NON e' qui (ordine 14458: autopilot).
# ---------------------------------------------------------------------------

CONTESTO_PATH = os.path.join(CORE, ".guardiano_contesto.json")
_CACHE_CONTESTO = None
_RE_RESUME = re.compile(r"(?:--resume|--session-id)[= ]([0-9a-fA-F-]{8,})")


def _conv_id_da_cmdline(cmd):
    m = _RE_RESUME.search(cmd or "")
    return m.group(1) if m else None


def _cwd_di(pid):
    try:
        return os.readlink(f"/proc/{pid}/cwd")
    except Exception:
        return None


def finestra_per_modello(conf_contesto, *modelli):
    """(max_context, fonte) dalla tabella conf.contesto.finestra_per_modello,
    provando nell'ordine i nomi di modello dati (transcript prima, alias del
    cmdline poi). Match esatto o per prefisso della chiave sul nome."""
    tab = (conf_contesto or {}).get("finestra_per_modello") or {}
    for m in modelli:
        if not m:
            continue
        ml = str(m).lower()
        for k, v in tab.items():
            if k.startswith("_") or not v:
                continue
            if ml == k.lower() or ml.startswith(k.lower()):
                return int(v), f"conf finestra_per_modello[{k}]"
    return None, None


def max_context_dal_processo(cli_pid, provider=None, model=None):
    """(max_context, fonte). Prima l'environ del CLI vivo, poi la config del
    provider (engine_adapter.CLAUDE_PROVIDERS), poi None (finestra grande)."""
    env = _environ(cli_pid) if cli_pid else ""
    m = re.search(r"CLAUDE_CODE_MAX_CONTEXT_TOKENS=(\d+)", env)
    if m:
        return int(m.group(1)), f"environ pid {cli_pid}"
    try:
        import engine_adapter
        for prov, cfg in engine_adapter.CLAUDE_PROVIDERS.items():
            if (provider and prov == provider) or (model and str(model).startswith(prov)):
                if cfg.get("max_context_tokens"):
                    return int(cfg["max_context_tokens"]), f"config provider {prov}"
    except Exception:
        pass
    return None, "finestra di default (nessun limite nel processo)"


def _risolvi_transcript(a, slug, db):
    """(percorso, conv_id, fonte) del transcript dell'agente: conv_id dal
    cmdline del CLI vivo (quello VERO, non il record), work_dir dal cwd del
    processo; il DB e' solo ripiego."""
    import engine_adapter
    rec = (db or {}).get(slug) or {}
    cmd = _cmdline(a.get("cli_pid")) if a.get("cli_pid") else ""
    conv = _conv_id_da_cmdline(cmd) or rec.get("conv_id")
    fonte = "cmdline del CLI" if _conv_id_da_cmdline(cmd) else "record DB"
    work_dir = _cwd_di(a.get("cli_pid")) or rec.get("work_dir")
    if not conv or not work_dir:
        return None, conv, fonte
    return engine_adapter.transcript_path("claude", conv, work_dir), conv, fonte


def fascia_contesto(token, soglia_avviso, soglia_critica):
    if token is None:
        return "ignota"
    if token >= soglia_critica:
        return "rosso"
    if token >= soglia_avviso:
        return "giallo"
    return "verde"


def sensore_contesto(conf, quota, classi, db=None, ora=None, risolvi=None,
                     misura=None, maxctx=None, generando=None):
    """Ritorna {abilitato, ts, misurati, sopra, critici, non_coperti, voci,
    eventi, eta_scansione_s, sola_osservazione}. Le funzioni iniettabili
    (risolvi/misura/maxctx/generando) esistono per i test: in produzione si
    usano quelle vere sopra."""
    global _CACHE_CONTESTO
    cc = conf.get("contesto") or {}
    ora = time.time() if ora is None else ora
    if not cc.get("abilitato", True):
        return {"abilitato": False, "voci": [], "eventi": [], "misurati": 0,
                "sopra": 0, "critici": 0, "non_coperti": 0, "sola_osservazione": True}
    ogni = cc.get("intervallo_s", 600)
    if _CACHE_CONTESTO and ora - _CACHE_CONTESTO["ts"] < ogni:
        return dict(_CACHE_CONTESTO["esito"], eta_scansione_s=round(ora - _CACHE_CONTESTO["ts"]))

    if misura is None:
        import sensore_memoria_leggera as _sml
        misura = _sml.misura_transcript
    risolvi = risolvi or _risolvi_transcript
    maxctx = maxctx or max_context_dal_processo
    if generando is None:
        def generando(a):
            try:
                import engine_adapter
                pane = capture_pane(a["sessione"], a["finestra"], 30)
                return bool(engine_adapter.is_pane_generating("claude", pane))
            except Exception:
                return None
    fr_avv = float(cc.get("frazione_avviso", 0.75))
    fr_cri = float(cc.get("frazione_critica", 0.90))
    default_ctx = int(cc.get("finestra_default_token", 1000000))
    coord_default = conf.get("canali", {}).get("coordinatore_default", "samantha")
    stato_prec = _carica_json(CONTESTO_PATH, {})
    stato_nuovo = {}
    voci, eventi = [], []
    non_coperti = 0
    for a in quota.get("agenti", []):
        cl = classi.get(a.get("tmux_window"), {}) or {}
        slug = cl.get("slug") or a.get("tmux_window")
        if a.get("engine_reale") != "claude" or not a.get("cli_pid"):
            non_coperti += 1
            continue
        percorso, conv, fonte_conv = risolvi(a, slug, db)
        if not percorso or not os.path.exists(percorso):
            non_coperti += 1
            voci.append({"slug": slug, "tmux_window": a.get("tmux_window"),
                         "coperto": False, "motivo": "transcript_assente", "conv_id": conv})
            continue
        byte, mtime, token, _inizio, modello_tr = misura(percorso)
        if byte is None:
            non_coperti += 1
            continue
        ctx, fonte_ctx = maxctx(a.get("cli_pid"), a.get("provider_reale"), a.get("model_reale"))
        if not ctx:
            # Nessun limite nel processo (modelli nativi Claude): la finestra
            # dipende dal MODELLO. Tabella in conf, nome dal transcript prima
            # (e' il modello che ha risposto davvero), alias del cmdline poi.
            ctx, f2 = finestra_per_modello(cc, modello_tr, a.get("model_reale"))
            if ctx:
                fonte_ctx = f2
        base = ctx or default_ctx
        s_avv, s_cri = round(fr_avv * base), round(fr_cri * base)
        fascia = fascia_contesto(token, s_avv, s_cri)
        if token is not None and token > base:
            # I token in finestra SUPERANO la finestra presunta: la presunzione
            # e' sbagliata, non l'agente. Osservato il 22/08: opus a 877k token,
            # vivo, con finestra presunta 200k. Non si inventa un ROSSO su un
            # numero che si sa falso: fascia IGNOTA, e il dato resta nel log
            # perche' e' proprio quello che serve per correggere la tabella.
            fascia = "ignota"
        pct = round(100.0 * token / base, 1) if token is not None and base else None
        eta_tr = round(ora - mtime) if mtime else None
        prec = stato_prec.get(slug) or {}
        voce = {"slug": slug, "tmux_window": a.get("tmux_window"), "coperto": True,
                "token": token, "byte": byte, "pct": pct, "fascia": fascia,
                "finestra_presunta_superata": bool(token is not None and token > base),
                "max_context": base, "fonte_max_context": fonte_ctx,
                "soglia_avviso": s_avv, "soglia_critica": s_cri,
                "modello_transcript": modello_tr, "model_reale": a.get("model_reale"),
                "provider_reale": a.get("provider_reale"),
                "eta_transcript_s": eta_tr, "conv_id": conv, "fonte_conv": fonte_conv,
                "transcript": percorso}
        cambiata = fascia != prec.get("fascia")
        if fascia in ("giallo", "rosso"):
            gen = generando(a)
            coord = cl.get("coordinatore") or coord_default
            voce["generando"] = gen
            voce["canale_previsto"] = (f"a2a:{slug}" if gen is False else f"a2a:{coord}")
            voce["canale_usato"] = None            # SOLA OSSERVAZIONE
        n = 1 if cambiata else int(prec.get("n", 0)) + 1
        stato_nuovo[slug] = {"fascia": fascia, "ts": prec.get("ts", ora) if not cambiata else ora,
                             "n": n, "token": token}
        if cambiata and (fascia in ("giallo", "rosso") or prec.get("fascia") in ("giallo", "rosso")):
            azione = {"rosso": "critico", "giallo": "sopra_soglia"}.get(fascia, "rientrato")
            livello = {"rosso": "ROSSO", "giallo": "GIALLO"}.get(fascia, "INFO")
            firma = f"contesto|{fascia}|{slug}"
            ev = {"azione": azione, "livello": livello, "agente": slug,
                  "tmux_window": a.get("tmux_window"), "fascia": fascia,
                  "fascia_precedente": prec.get("fascia"), "valore": token, "pct": pct,
                  "max_context": base, "soglia_avviso": s_avv, "soglia_critica": s_cri,
                  "fonte": f"{percorso} ({fonte_ctx})", "eta_dato_s": eta_tr,
                  "firma": firma, "canale_previsto": voce.get("canale_previsto"),
                  "canale_usato": None}
            eventi.append(ev)
            ledger("contesto", modulo="contesto", sola_osservazione=True, **ev)
        voci.append(voce)
    sopra = sum(1 for v in voci if v.get("fascia") in ("giallo", "rosso"))
    critici = sum(1 for v in voci if v.get("fascia") == "rosso")
    ignote = sum(1 for v in voci if v.get("fascia") == "ignota")
    misurati = sum(1 for v in voci if v.get("coperto"))
    _salva_json(CONTESTO_PATH, stato_nuovo)
    ledger("contesto", modulo="contesto", azione="scansione", oggetto="flotta",
           valore=f"misurati={misurati} sopra={sopra} critici={critici} ignote={ignote} non_coperti={non_coperti}",
           misurati=misurati, sopra=sopra, critici=critici, ignote=ignote, non_coperti=non_coperti,
           finestra_superata=[f"{v['slug']}:{v.get('token')}>{v.get('max_context')}" for v in voci if v.get("finestra_presunta_superata")],
           agenti_sopra=[f"{v['slug']}:{v.get('pct')}%" for v in voci if v.get("fascia") in ("giallo", "rosso")],
           fonte="transcript jsonl + environ CLI", sola_osservazione=True, eta_dato_s=0)
    esito = {"abilitato": True, "ts": ora_iso(), "misurati": misurati, "sopra": sopra,
             "critici": critici, "ignote": ignote, "non_coperti": non_coperti, "voci": voci,
             "eventi": eventi, "eta_scansione_s": 0, "sola_osservazione": True,
             "intervallo_s": ogni}
    _CACHE_CONTESTO = {"ts": ora, "esito": esito}
    return esito


# ---------------------------------------------------------------------------
# AZIONI STORE (exec_azioni_guardiano, 22/08/2026) — PARTE 1 STEP 2 mandato
# samantha_1. Il ciclo legge provider_stato + agenti_runtime dallo store e
# decide routing_token / avvio / rinfresco. Ogni decisione e' loggata in
# azioni_guardiano. Mai inventa un residuo o un parametro: dato non disponibile
# -> nessuna azione e motivo esplicito.
# ---------------------------------------------------------------------------

def _conn_store():
    import psycopg2
    return psycopg2.connect(STORE_DSN)


def leggi_store():
    """Legge provider_stato + agenti_runtime dallo store. Mai inventa valori:
    campo non disponibile -> None. Ritorna anche eta_max_s (il dato piu' vecchio)."""
    out = {"ts": time.time(), "provider": [], "agenti": [], "eta_max_s": None,
           "errore": None}
    try:
        conn = _conn_store()
        cur = conn.cursor()
        cur.execute("SELECT provider, tipo, residuo_soldi, usata_pct, aggiornato_at "
                    "FROM provider_stato ORDER BY provider")
        for provider, tipo, residuo_soldi, usata_pct, agg in cur.fetchall():
            eta = round(time.time() - agg.timestamp(), 1) if agg else None
            out["provider"].append({
                "provider": provider, "tipo": tipo,
                "residuo_soldi": float(residuo_soldi) if residuo_soldi is not None else None,
                "usata_pct": float(usata_pct) if usata_pct is not None else None,
                "eta_s": eta,
            })
        cur.execute("SELECT agent_id, slug, nome_win, motore, provider, modello, "
                    "stato, studio, ultimo_rinfresco_at, contesto_token, aggiornato_at "
                    "FROM agenti_runtime ORDER BY slug")
        for row in cur.fetchall():
            (agent_id, slug, nome_win, motore, provider, modello, stato, studio,
             ultimo_rinfresco_at, contesto_token, agg) = row
            eta = round(time.time() - agg.timestamp(), 1) if agg else None
            out["agenti"].append({
                "agent_id": agent_id, "slug": slug, "nome_win": nome_win,
                "motore": motore, "provider": provider, "modello": modello,
                "stato": stato, "studio": studio,
                "ultimo_rinfresco_at": str(ultimo_rinfresco_at) if ultimo_rinfresco_at else None,
                "contesto_token": int(contesto_token) if contesto_token is not None else None,
                "eta_s": eta,
            })
        conn.close()
        e_prov = [p["eta_s"] for p in out["provider"] if p["eta_s"] is not None]
        e_ag = [a["eta_s"] for a in out["agenti"] if a["eta_s"] is not None]
        out["eta_provider_s"] = max(e_prov) if e_prov else None
        out["eta_agenti_s"] = max(e_ag) if e_ag else None
        out["eta_max_s"] = max([x for x in (out["eta_provider_s"], out["eta_agenti_s"])
                                if x is not None] or [None])
    except Exception as e:
        out["errore"] = f"{type(e).__name__}: {e}"
    return out


def _sorgente_da_residui(prov):
    """Sorgente col residuo migliore secondo routing_regole.json: claude prima
    (< 80% usata), poi agy, ULTIMA deepseek (solo saldo > critico)."""
    claude = prov.get("claude") or {}
    agy = prov.get("agy") or {}
    ds = prov.get("deepseek") or {}
    claude_ok = claude.get("usata_pct") is not None and claude["usata_pct"] < STORE_DOWNGRADE_PCT
    agy_ok = agy.get("usata_pct") is not None and agy["usata_pct"] < STORE_DOWNGRADE_PCT
    ds_saldo = ds.get("residuo_soldi")
    ds_ok = ds_saldo is not None and ds_saldo > STORE_SALDO_DS_CRITICO
    if claude_ok:
        return "claude", f"claude usata {claude['usata_pct']}% < 80%"
    if agy_ok:
        return "agy", f"claude non usabile, agy usata {agy['usata_pct']}% < 80%"
    if ds_ok:
        return "deepseek", f"abbonamenti saturi, deepseek saldo ${ds_saldo} > critico"
    return None, "tutte le sorgenti sature o dati mancanti: nessun routing"


def _store_fresco(store, tabella="agenti"):
    """Dati store utilizzabili per la tabella indicata: nessun errore e eta della
    tabella <= soglia. Freshness PER TABELLA (provider vs agenti): una riga
    agenti stantia non deve bloccare il routing, e viceversa."""
    if store.get("errore"):
        return False
    chiave = "eta_provider_s" if tabella == "provider" else "eta_agenti_s"
    eta = store.get(chiave)
    return eta is not None and eta <= STORE_DATO_MAX_ETA_S


def _store_agenti_utilizzabili(store):
    """Agenti_runtime 'utilizzabile' se la MAGGIORANZA delle righe e' fresca.
    Una riga fantasma (agente dismesso, non piu' riscritto da scrivi_stato che
    non cancella) non deve bloccare la valutazione degli agenti vivi."""
    etas = [a.get("eta_s") for a in store.get("agenti", []) if a.get("eta_s") is not None]
    if not etas:
        return False
    fresche = sum(1 for e in etas if e <= STORE_DATO_MAX_ETA_S)
    return fresche / len(etas) >= 0.5


def _throttle_store(tipo):
    """True se il tipo ha gia' loggato un resoconto nella finestra."""
    d = _carica_json(STORE_LOG_PATH, {})
    ultimo = d.get(tipo) or 0
    return (time.time() - ultimo) < STORE_LOG_INTERVALLO_S


def _segna_throttle_store(tipo):
    d = _carica_json(STORE_LOG_PATH, {})
    d[tipo] = time.time()
    _salva_json(STORE_LOG_PATH, d)


def decisioni_routing_store(store, conf):
    """ROUTING TOKEN: se un provider e' sotto soglia (routing_regole) o il saldo
    DeepSeek e' critico (< 1.0 USD), instrada nuovi avvii/cambi modello sul
    serbatoio col residuo giusto. Quando va tutto bene logga un resoconto
    periodico (throttle), non una riga ogni ciclo."""
    if store.get("errore"):
        return [{"azione": "routing_token", "agente": "sistema", "livello": "GIALLO",
                 "perche": f"store non leggibile: {store['errore']}",
                 "valore": {"errore": store["errore"]}, "esegui": False,
                 "motivo_esito": "store_non_leggibile"}]
    if not _store_fresco(store, "provider"):
        return [{"azione": "routing_token", "agente": "sistema", "livello": "GIALLO",
                 "perche": f"provider_stato non fresco (eta {store.get('eta_provider_s')}s): nessun routing",
                 "valore": {"eta_provider_s": store.get("eta_provider_s")}, "esegui": False,
                 "motivo_esito": "store_vecchio"}]
    prov = {p["provider"]: p for p in store["provider"]}
    claude = prov.get("claude") or {}
    agy = prov.get("agy") or {}
    ds = prov.get("deepseek") or {}
    ds_saldo = ds.get("residuo_soldi")
    claude_giu = claude.get("usata_pct") is not None and claude["usata_pct"] >= STORE_DOWNGRADE_PCT
    agy_giu = agy.get("usata_pct") is not None and agy["usata_pct"] >= STORE_DOWNGRADE_PCT
    ds_critico = ds_saldo is not None and ds_saldo < STORE_SALDO_DS_CRITICO
    problemi = []
    if ds_critico:
        problemi.append(f"deepseek saldo ${ds_saldo} < {STORE_SALDO_DS_CRITICO} USD")
    if claude_giu:
        problemi.append(f"claude usata {claude['usata_pct']}% >= {STORE_DOWNGRADE_PCT}%")
    if agy_giu:
        problemi.append(f"agy usata {agy['usata_pct']}% >= {STORE_DOWNGRADE_PCT}%")
    sorgente, motivo = _sorgente_da_residui(prov)
    if problemi:
        return [{"azione": "routing_token", "agente": "sistema", "livello": "GIALLO",
                 "perche": "; ".join(problemi) + " -> " + (motivo or "nessuna sorgente usabile"),
                 "valore": {"problemi": problemi, "sorgente_scelta": sorgente,
                            "motivo": motivo, "deepseek_saldo_usd": ds_saldo,
                            "claude_usata_pct": claude.get("usata_pct"),
                            "agy_usata_pct": agy.get("usata_pct")},
                 "esegui": True}]
    if _throttle_store("routing_token"):
        return []
    return [{"azione": "routing_token", "agente": "sistema", "livello": "GIALLO",
             "perche": "nessuna soglia superata: routing invariato",
             "valore": {"claude_usata_pct": claude.get("usata_pct"),
                        "agy_usata_pct": agy.get("usata_pct"),
                        "deepseek_saldo_usd": ds_saldo,
                        "sorgente_scelta": sorgente, "motivo": motivo},
             "esegui": True}]


def _pendenti_a2a(slug):
    """Messaggi non letti nell'inbox A2A dell'agente: il segnale piu' concreto
    di 'lavoro in corso' verificabile da fuori. 0 se inbox assente o vuota."""
    try:
        inbox = os.path.join(STORE_A2A_ROOT, slug, "inbox")
        if not os.path.isdir(inbox):
            return 0
        return len([f for f in os.listdir(inbox)
                    if f.endswith(".md") and not f.startswith(".")])
    except Exception:
        return 0


def decisioni_avvii_store(store, conf):
    """AVVII AUTOMATICI: agente 'spento' in agenti_runtime, attivo nel DB agents
    e con LAVORO IN CORSO (messaggi A2A non letti) -> avvio col modello/provider
    dai residui. Mai a freddo (serve pending), mai lista_bianca."""
    try:
        import agent_db
        db = agent_db.get_db()
    except Exception:
        db = {}
    prov = {p["provider"]: p for p in store["provider"]}
    dec = []
    for a in store["agenti"]:
        if a["stato"] != "spento":
            continue
        # Freshness per-agente: mai avviare su una riga stantia di agenti_runtime.
        if a.get("eta_s") is None or a["eta_s"] > STORE_DATO_MAX_ETA_S:
            continue
        slug = a["slug"]
        if not slug or in_lista_bianca(slug, a.get("nome_win"), conf):
            continue
        rec = db.get(slug) or db.get(a.get("agent_id") or "")
        if not rec or rec.get("dismesso") == "true":
            continue
        pendenti = _pendenti_a2a(slug)
        if pendenti <= 0:
            continue
        sorgente, motivo = _sorgente_da_residui(prov)
        dec.append({"azione": "avvio", "agente": slug, "livello": "GIALLO",
                    "ruolo": "exec", "coordinatore": None,
                    "_misure_usate": {"eta_s": a.get("eta_s"), "pendenti_a2a": pendenti},
                    "perche": f"spento ma con {pendenti} messaggio/i A2A non letti in inbox",
                    "valore": {"pendenti_a2a": pendenti, "sorgente_scelta": sorgente,
                               "motivo_sorgente": motivo},
                    "esegui": True})
    if not dec and _store_agenti_utilizzabili(store) and not _throttle_store("avvio"):
        dec.append({"azione": "avvio", "agente": "sistema", "livello": "GIALLO",
                    "ruolo": "exec", "coordinatore": None,
                    "perche": "nessun agente spento non-lista-bianca con lavoro in corso",
                    "valore": {"spenti_non_wl_senza_pending": True},
                    "esegui": False, "motivo_esito": "non_necessaria"})
    return dec


def _verifica_salva_stato(slug):
    """Protocollo memoria: file di stato scritto (e rileggibile) PRIMA del
    rinfresco. Fresco = scritto negli ultimi STORE_STATO_FILE_MAX_ETA_S s."""
    candidati = (os.path.join(STORE_OBSIDIAN_PROGETTI, f"{slug}_stato.md"),
                 os.path.join(STORE_OBSIDIAN_PROGETTI, f"{slug}.md"))
    for path in candidati:
        if os.path.exists(path):
            eta = time.time() - os.path.getmtime(path)
            if eta <= STORE_STATO_FILE_MAX_ETA_S:
                return True, path, round(eta), "salva-stato fresco"
            return False, path, round(eta), \
                f"salva-stato scritto {int(eta)}s fa (> {STORE_STATO_FILE_MAX_ETA_S}s)"
    return False, candidati[0], None, "file di stato assente"


def _ultimo_rinfresco(slug):
    d = _carica_json(RINFRESCHI_STATO_PATH, {})
    return d.get(slug) or 0


def _registra_rinfresco(slug):
    d = _carica_json(RINFRESCHI_STATO_PATH, {})
    d[slug] = time.time()
    _salva_json(RINFRESCHI_STATO_PATH, d)


def decisioni_rinfreschi_store(store, conf):
    """RINFRESCHI: agente oltre STORE_SOGLIA_CONTESTO (200k) -> rinfresco SOLO
    dopo verifica salva-stato. Senza stato fresco la decisione e' BLOCCATA
    (esegui=False, motivo_esito esplicito): mai un kill silenzioso."""
    dec = []
    for a in store["agenti"]:
        tok = a["contesto_token"]
        if tok is None or tok <= STORE_SOGLIA_CONTESTO:
            continue
        # Freshness per-agente: mai rinfrescare su una riga stantia.
        if a.get("eta_s") is None or a["eta_s"] > STORE_DATO_MAX_ETA_S:
            continue
        slug = a["slug"]
        if not slug or in_lista_bianca(slug, a.get("nome_win"), conf):
            continue
        ok, file, eta, motivo = _verifica_salva_stato(slug)
        base = {"azione": "rinfresco", "agente": slug, "livello": "GIALLO",
                "ruolo": "exec", "coordinatore": None,
                "_misure_usate": {"contesto_token": tok, "eta_s": a.get("eta_s"), "salva_stato_eta_s": eta},
                "valore": {"contesto_token": tok, "file_stato": file,
                           "salva_stato_eta_s": eta, "motivo": motivo}}
        if not ok:
            dec.append(dict(base, esegui=False, motivo_esito="salva_stato_non_verificato",
                            perche=f"contesto {tok} > {STORE_SOGLIA_CONTESTO} ma {motivo}"))
            continue
        ultimo = _ultimo_rinfresco(slug)
        if time.time() - ultimo < STORE_RINFRESCO_MIN_INTERVALLO_S:
            dec.append(dict(base, esegui=False, motivo_esito="rinfresco_recente",
                            perche=f"contesto {tok} > {STORE_SOGLIA_CONTESTO}, ultimo "
                                   f"rinfresco {int(time.time() - ultimo)}s fa"))
            continue
        dec.append(dict(base, esegui=True,
                        perche=f"contesto {tok} > {STORE_SOGLIA_CONTESTO}, {motivo}"))
    if not dec and _store_agenti_utilizzabili(store) and not _throttle_store("rinfresco"):
        dec.append({"azione": "rinfresco", "agente": "sistema", "livello": "GIALLO",
                    "ruolo": "exec", "coordinatore": None,
                    "perche": "nessun agente oltre soglia contesto non in lista bianca",
                    "valore": {"oltre_soglia": False},
                    "esegui": False, "motivo_esito": "non_necessaria"})
    return dec


def _scrivi_azione_guardiano(azione, dettaglio, esito):
    """Log su azioni_guardiano (ts/azione/dettaglio JSONB/esito). Best-effort:
    se la scrittura fallisce il ledger resta la traccia."""
    try:
        conn = _conn_store()
        cur = conn.cursor()
        cur.execute("INSERT INTO azioni_guardiano (azione, dettaglio, esito) "
                    "VALUES (%s, %s::jsonb, %s)",
                    (azione, json.dumps(dettaglio, ensure_ascii=False, default=str), esito))
        conn.commit()
        cur.close()
        conn.close()
        return True
    except Exception as e:
        ledger("azioni_guardiano_log_errore", errore=f"{type(e).__name__}: {e}"[:300])
        return False


def esegui_routing_store(dec, dry=False):
    """La rotta e' gia' calcolata in decisione; qui si logga (via azioni_guardiano).
    Il routing VERO delle sessioni avviene a livello motore (strumento_agenti),
    non HTTP: LiteLLM e' solo l'ultimo miglio one-shot, verificato in PASSO 0."""
    if dec.get("esegui") is False:
        return False, dec.get("motivo_esito") or "non eseguita"
    return True, f"rotta verificata: {dec.get('valore', {}).get('sorgente_scelta')} " \
                 f"({dec.get('perche', '')})"


def esegui_avvio_store(dec, dry=False):
    """Avvio via strumento_agenti accendi_agente, col modello/provider dai residui
    SOLO se il record punta a deepseek critico (mai un cambio cieco)."""
    slug = dec["agente"]
    if dry:
        return True, f"dry: accendi_agente {slug}"
    try:
        import agent_db
        db = agent_db.get_db()
        rec = db.get(slug) or {}
        py = os.path.join(CORE, "venv", "bin", "python3")
        sa = os.path.join(CORE, "strumento_agenti.py")
        cmd = [py, sa, "accendi_agente", slug]
        if rec.get("provider") == "deepseek" and \
                (dec.get("valore") or {}).get("sorgente_scelta") in ("claude", "agy"):
            cmd += ["--provider", "claude", "--model", "claude-haiku-4-5-20251001"]
        # FIX 31/08/2026 (mandato exec_fix_guardiano_system_scope): via di
        # sistema, stesso motivo delle chiamate strumento_agenti.py sopra.
        r = subprocess.run(cmd, cwd=CORE, env={**os.environ, "AGY_SYSTEM_SCOPE": "1"},
                            capture_output=True, text=True, timeout=300)
        return r.returncode == 0, (r.stdout or r.stderr or "")[-300:]
    except Exception as e:
        return False, str(e)[:300]


def esegui_rinfresco_store(dec, dry=False):
    """Rinfresco via refresh_agent_context.py, SOLO con salva-stato verificato
    (gia' nella decisione) e parametri sessione/finestra/workdir risolti dal DB.
    Se un parametro manca -> bloccata, mai indovinare la finestra."""
    slug = dec["agente"]
    if dry:
        return True, f"dry: rinfresco {slug}"
    try:
        import agent_db
        db = agent_db.get_db()
        rec = db.get(slug)
        if not rec:
            return False, f"{slug} non nel DB: rinfresco impossibile"
        nome_win = rec.get("tmux_window") or rec.get("window") or ""
        sess, _, win = nome_win.partition(":")
        work_dir = rec.get("work_dir")
        if not win or not sess or not work_dir:
            return False, f"parametri finestra incompleti (win={win!r} sess={sess!r} wd={work_dir!r})"
        py = os.path.join(CORE, "venv", "bin", "python3")
        ra = os.path.join(CORE, "refresh_agent_context.py")
        cmd = [py, ra, slug, sess, win, work_dir]
        if rec.get("engine"):
            cmd.append(f"engine={rec['engine']}")
        r = subprocess.run(cmd, cwd=CORE, capture_output=True, text=True, timeout=600)
        ok = (r.returncode == 0)
        if ok:
            _registra_rinfresco(slug)
        return ok, (r.stdout or r.stderr or "")[-300:]
    except Exception as e:
        return False, str(e)[:300]


ESECUTORI_STORE = {
    "routing_token": esegui_routing_store,
    "avvio": esegui_avvio_store,
    "rinfresco": esegui_rinfresco_store,
}


def applica_azioni_store(decisioni, dry=False):
    """Esegue le decisioni store (autonome, mandato) e logga OGNI decisione in
    azioni_guardiano con ts/azione/dettaglio/esito. Non usa alert/dedup: la
    traccia e' la tabella + il ledger."""
    esiti = []
    for dec in decisioni:
        azione = dec["azione"]
        esecutore = ESECUTORI_STORE.get(azione)
        if esecutore is None:
            esiti.append(dict(dec, esito="esecutore_sconosciuto"))
            _scrivi_azione_guardiano(azione, dec.get("valore") or {"perche": dec["perche"]},
                                     "esecutore_sconosciuto")
            continue
            
        ammessa, evento_rifiuto, mancanti = _verifica_misure_distruttive(dec)
        if not ammessa:
            motivo = f"{evento_rifiuto} ({', '.join(mancanti or [])})"
            _scrivi_azione_guardiano(azione, dec.get("valore") or {"perche": dec["perche"]}, motivo)
            ledger(evento_rifiuto, mancanti=mancanti, azione=azione, agente=dec.get("agente"), perche=dec.get("perche")[:300], dry=dry)
            esiti.append(dict(dec, esito="bloccata_da_presidio_strutturale", dettaglio=motivo))
            continue
            
        if dec.get("esegui") is False:
            motivo = dec.get("motivo_esito") or "bloccata"
            _scrivi_azione_guardiano(azione, dec.get("valore") or {"perche": dec["perche"]},
                                     motivo)
            ledger("azione_store", azione=azione, agente=dec.get("agente"), esito=motivo,
                   perche=dec.get("perche")[:300], dry=dry)
            esiti.append(dict(dec, esito=motivo))
            _segna_throttle_store(azione)
            continue
        try:
            ok, dettaglio = esecutore(dec, dry=dry)
        except Exception as e:
            ok, dettaglio = False, f"{type(e).__name__}: {e}"[:300]
        esito = "eseguita" if ok else "fallita"
        _scrivi_azione_guardiano(azione, dec.get("valore") or {},
                                 f"{esito}" + (f" — {dettaglio[:200]}" if dettaglio else ""))
        ledger("azione_store", azione=azione, agente=dec.get("agente"), esito=esito,
               dettaglio=dettaglio[:300], perche=dec.get("perche")[:300], dry=dry)
        esiti.append(dict(dec, esito=esito, dettaglio=dettaglio))
        _segna_throttle_store(azione)
    return esiti


def ciclo(conf, dedup, persistenza, jiffies_prec, intervallo_s, dry=False,
          globale_prec=None):
    t0 = time.time()
    batti("quota")
    quota = sensore_quota(conf, jiffies_prec=jiffies_prec, intervallo_s=intervallo_s, dry=dry)
    batti("ram")
    ram = sensore_ram(conf)
    globale_ora = snapshot_jiffies_globale()
    pid_sess = mappa_pid_sessione()
    cpu = sensore_cpu(conf, globale_prec, globale_ora, intervallo_s, pid_sess)
    hz = os.sysconf("SC_CLK_TCK")
    cpu_per_pid = {}
    if globale_prec:
        reale_s = globale_ora.get("_ts", ("", 0))[1] - globale_prec.get("_ts", ("", 0))[1]
        div = reale_s if reale_s > 0.5 else intervallo_s
        for pid, (_c, j) in globale_ora.items():
            if pid == "_ts":
                continue
            pr = globale_prec.get(pid)
            if pr:
                cpu_per_pid[pid] = 100.0 * (j - pr[1]) / hz / max(div, 1)
    classi = mappa_classi(quota["agenti"], conf)
    misure = misure_serbatoi(quota, conf)

    # --- RUOLO ROUTER (22/08/2026): SOLA OSSERVAZIONE, nessun effetto reale ----
    # Produce e logga le decisioni che il router avrebbe preso (una riga per
    # decisione su /tmp/self_healing_router.log). Sta DOPO misure_serbatoi di
    # proposito: si basa sugli stessi residui gia' misurati. Non solleva mai.
    router = {"abilitato": False, "decisioni": [], "sola_osservazione": True}
    try:
        batti("router")
        router = router_osserva(misure, conf)
    except Exception as e:
        ledger("router_errore", errore=f"{type(e).__name__}: {e}"[:300])

    riallineati = riallinea_db(quota["agenti"], conf, dry=dry, conferme=persistenza) \
        if conf["azioni"]["riallinea_db"].get("exec") else []

    batti("servizi")
    servizi = sensore_servizi(conf, cpu_srv=cpu_per_pid or None)
    pool = sensore_pool(conf, slug_vivi={c["slug"] for c in classi.values() if c.get("slug") and not c.get("dismesso")})

    decisioni = []
    try:
        import agent_db as _adb
        _db_dec = _adb.get_db()
    except Exception:
        _db_dec = {}
    decisioni += decisioni_quota(quota, conf, persistenza, classi, misure, db=_db_dec)
    decisioni += decisioni_errori_api(quota, conf, persistenza, classi)
    decisioni += decisioni_rientro(classi, misure, conf, db=_db_dec)
    decisioni += decisioni_liveness(quota, conf, persistenza, classi, db=_db_dec)
    decisioni += decisioni_servizi(servizi, conf, persistenza)
    decisioni += decisioni_pool(pool, conf)
    decisioni += decisioni_ram(ram, quota, conf, classi)
    decisioni += decisioni_cpu(cpu, quota, conf, persistenza, classi)
    decisioni += decisioni_capacita(cpu, quota, conf, persistenza, classi)

    # --- F4: META-SORVEGLIANZA DEI GUARDIANI (compreso questo stesso) ---------
    # Sta DOPO tutti gli altri sensori di proposito: il ledger che rilegge per
    # il controllo (d) contiene gia' le decisioni di questo ciclo, quindi un
    # verdetto sbagliato del guardiano viene esaminato dal giro successivo, non
    # fra un'ora. Non solleva mai: un difetto nella meta-sorveglianza non deve
    # spegnere la sorveglianza normale — sarebbe il guasto peggiore dei due.
    meta = {"abilitato": False, "verdetti": [], "allarmi_esaminati": 0}
    ordini_fermi = []
    try:
        batti("meta-guardiani")
        registro = RegistroFirme()
        meta = sensore_guardiani(conf, db=_db_dec, registro=registro)
        for d in decisioni_guardiani(meta, conf, registro=registro):
            decisioni.append({
                "azione": "ticket_meta_guardiano", "livello": d["livello"],
                "agente": d["guardiano"], "tmux_window": d.get("soggetto"),
                "chiave_dedup": f"meta:{d['firma']}",
                "valore": d.get("valore"), "fonte": d.get("fonte"),
                "eta_dato_s": d.get("eta_dato_s"), "firma": d["firma"],
                "ripetizioni": d.get("ripetizioni"), "da_s": d.get("da_s"),
                "perche": f"[{d['verdetto']}] {d['perche']}",
            })
        ledger("meta_guardiani", guardiani=meta.get("guardiani"),
               allarmi_esaminati=meta.get("allarmi_esaminati"),
               verdetti=len(meta.get("verdetti", [])),
               rotti=sorted({v["guardiano"] for v in meta.get("verdetti", [])
                             if v.get("verdetto") == "guardiano_rotto"}),
               fantasmi=sorted({v["guardiano"] for v in meta.get("verdetti", [])
                                if v.get("verdetto") == "guardiano_misura_fantasmi"}),
               auto_anomalie=sum(1 for v in meta.get("verdetti", [])
                                 if v.get("verdetto") == "auto_anomalia"))
    except Exception as e:
        ledger("meta_guardiani_errore", errore=f"{type(e).__name__}: {e}"[:300])

    # --- F4 (e): ordini fermi nella casella di input --------------------------
    try:
        batti("ordini-fermi")
        ordini_fermi = sensore_ordini_fermi(conf, quota, classi, persistenza,
                                            db=_db_dec)
        for o in ordini_fermi:
            decisioni.append(dict(o, chiave_dedup=f"ordine_fermo:{o['tmux_window']}"))
            ledger("ordine_fermo_rilevato", agente=o.get("agente"),
                   tmux_window=o["tmux_window"], valore=o["valore"],
                   fonte=o["fonte"], fermo_da_s=o["fermo_da_s"],
                   verificato_su_jsonl=o["verificato_su_jsonl"])
    except Exception as e:
        ledger("ordini_fermi_errore", errore=f"{type(e).__name__}: {e}"[:300])

    # --- F-A: SENSORE CONTESTO, sola osservazione (niente decisioni) ----------
    contesto = {"abilitato": False, "voci": [], "eventi": []}
    try:
        batti("contesto")
        contesto = sensore_contesto(conf, quota, classi, db=_db_dec)
    except Exception as e:
        ledger("contesto_errore", errore=f"{type(e).__name__}: {e}"[:300])

    # --- AZIONI STORE (exec_azioni_guardiano 22/08): routing + avvii + rinfreschi
    # Legge provider_stato + agenti_runtime dallo store e decide. Ogni decisione
    # finisce in azioni_guardiano. Incluso in un try: un difetto qui non deve mai
    # far cadere la sorveglianza normale (stesso principio F4).
    store_azioni = {"abilitato": True, "decisioni": [], "esiti": []}
    try:
        batti("store-azioni")
        _store = leggi_store()
        if _store["errore"]:
            ledger("store_azioni_errore", errore=_store["errore"])
        else:
            store_azioni["decisioni"] = (
                decisioni_routing_store(_store, conf)
                + decisioni_avvii_store(_store, conf)
                + decisioni_rinfreschi_store(_store, conf))
            store_azioni["esiti"] = applica_azioni_store(store_azioni["decisioni"], dry=dry)
            ledger("store_azioni", decisioni=len(store_azioni["decisioni"]),
                   esiti=[e["esito"] for e in store_azioni["esiti"]],
                   eta_store_s=_store.get("eta_max_s"))
    except Exception as e:
        ledger("store_azioni_errore", errore=f"{type(e).__name__}: {e}"[:400])

    # Misure oltre soglia: una riga di ledger anche quando non si agisce — e' la
    # serie storica su cui si tarano le soglie, e senza non si sa mai se una
    # soglia e' giusta o solo mai scattata.
    s = conf["soglie"]
    if ram.get("disponibile_mb") is not None and ram["disponibile_mb"] < s["ram_disponibile_mb"]:
        ledger("misura_oltre_soglia", sensore="ram", valore=ram["disponibile_mb"],
               soglia=s["ram_disponibile_mb"], per_sessione=dict(list(ram["per_sessione"].items())[:5]))
    if cpu.get("load5") is not None and cpu["load5"] > s["load5"]:
        ledger("misura_oltre_soglia", sensore="cpu", valore=cpu["load5"], soglia=s["load5"],
               psi_cpu=cpu.get("psi_cpu", {}).get("some_avg10"),
               saturazione_pct=cpu.get("saturazione_pct"),
               per_gruppo=cpu.get("per_gruppo"), per_sessione=cpu.get("per_sessione"))
    if misure.get("deepseek_saldo_usd") is not None \
            and misure["deepseek_saldo_usd"] < s["deepseek_saldo_basso_usd"]:
        ledger("misura_oltre_soglia", sensore="quota", metrica="deepseek_saldo_usd",
               valore=misure["deepseek_saldo_usd"], soglia=s["deepseek_saldo_basso_usd"])
    for a in quota["agenti"]:
        if a.get("problemi"):
            ledger("misura_oltre_soglia", sensore="quota", agente=classi.get(a["tmux_window"], {}).get("slug"),
                   tmux_window=a["tmux_window"], engine_reale=a["engine_reale"],
                   model_reale=a["model_reale"], provider_reale=a.get("provider_reale"),
                   problemi=[p["tipo"] for p in a["problemi"]], cpu_pct=a.get("cpu_pct"),
                   reset_minuti=a.get("reset_minuti"))

    batti("esiti")
    esiti = applica(decisioni, conf, dedup, dry=dry)

    pids = [a["cli_pid"] for a in quota["agenti"] if a.get("cli_pid")]
    stato = {
        "ts": ora_iso(),
        "durata_ciclo_s": round(time.time() - t0, 1),
        "dry": dry,
        "sensori": {"quota": quota, "ram": ram, "cpu": cpu, "servizi": servizi,
                    "pool": pool, "guardiani": meta, "ordini_fermi": ordini_fermi,
                    "contesto": contesto, "router": router,
                    "store_azioni": store_azioni},
        "misure_serbatoi": misure,
        "classi": classi,
        "riallineati": riallineati,
        "decisioni": decisioni,
        "esiti": esiti,
        "conf_usata": {"soglie": conf["soglie"], "azioni": conf["azioni"]},
    }
    _salva_json(STATO_PATH, stato)
    dedup.salva()
    persistenza.salva()
    for s_ in servizi:
        if servizio_atteso_spento(s_["finestra"], conf):
            continue
        if not s_["vivo"] or s_.get("heartbeat_fresco") is False:
            ledger("misura_oltre_soglia", sensore="servizi", finestra=s_["finestra"],
                   vivo=s_["vivo"], heartbeat_eta_s=s_.get("heartbeat_eta_s"))
    try:
        chiudi_alert_rientrati()             # F-A: nessun allarme senza chiusura
    except Exception as e:
        print(f"[guardiano] chiusura alert fallita: {e}", flush=True)
    ledger("ciclo", agenti_osservati=len(quota["agenti"]),
           servizi_giu=sum(1 for x in servizi if not x["vivo"]),
           pool_fermi=len(pool),
           cli_morti=sum(1 for a in quota["agenti"] if not a.get("cli_vivo")),
           con_problemi=sum(1 for a in quota["agenti"] if a.get("problemi")),
           ram_mb=ram.get("disponibile_mb"), load5=cpu.get("load5"),
           cpu_saturazione_pct=cpu.get("saturazione_pct"),
           deepseek_usd=misure.get("deepseek_saldo_usd"),
           decisioni=len(decisioni), riallineati=len(riallineati),
           guardiani_sorvegliati=len(meta.get("guardiani") or []),
           verdetti_meta=len(meta.get("verdetti") or []),
           ordini_fermi=len(ordini_fermi),
           contesto_sopra=contesto.get("sopra"), contesto_critici=contesto.get("critici"),
           durata_s=stato["durata_ciclo_s"], dry=dry)
    batti("ciclo concluso")
    return stato, leggi_jiffies_tutti(pids), globale_ora


def acquisisci_istanza():
    """Guardia single-instance obbligatoria (regola 10): due copie dello stesso
    demone si sovrascrivono a vicenda stato, dedup e persistenza."""
    global _lock_fh
    _lock_fh = open(LOCK_PATH, "w")
    try:
        fcntl.flock(_lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError):
        print(f"[guardiano] altra istanza attiva (lock {LOCK_PATH}): esco.", flush=True)
        sys.exit(0)
    _lock_fh.write(str(os.getpid()))
    _lock_fh.flush()


def sonda_usage_standalone():
    """Modo `--sonda-usage`: esegue la sonda REALE e aggiorna la cache, poi esce.
    Lanciato in background dal ciclo, mai dal ciclo stesso."""
    fh = open(SONDA_LOCK, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError):
        print("[guardiano] sonda gia' in corso: esco.", flush=True)
        return 0
    conf = carica_conf()
    t0 = time.time()
    dati = quota_flat_da_sonda(conf, forza=True)
    ledger("sonda_usage_conclusa", durata_s=round(time.time() - t0, 1),
           claude_settimanale_pct=dati.get("claude_settimanale_pct"),
           claude_sessione_pct=dati.get("claude_sessione_pct"),
           agy_disponibile_pct=dati.get("agy_disponibile_pct"),
           errori=[k for k in dati if k.endswith("_errore")])
    print(json.dumps({k: v for k, v in dati.items()
                      if k not in ("claude_righe", "agy_righe")}, indent=2), flush=True)
    return 0


def main():
    if "--sonda-usage" in sys.argv:
        return sonda_usage_standalone()
    once = "--once" in sys.argv
    dry = "--dry" in sys.argv
    acquisisci_istanza()
    conf = carica_conf()
    intervallo = conf.get("intervallo_ciclo_s", 60)
    dedup = Dedup(cooldown_s=conf.get("cooldown_alert_s", 900))
    persistenza = Persistenza()
    print(f"[guardiano] avviato (once={once} dry={dry} intervallo={intervallo}s)", flush=True)
    jiff = None
    glob = None
    while True:
        t0 = time.time()
        try:
            conf = carica_conf()                     # ricarica: soglie a caldo
            dedup.cooldown_s = conf.get("cooldown_alert_s", 900)
            stato, jiff, glob = ciclo(conf, dedup, persistenza, jiff, intervallo,
                                      dry=dry, globale_prec=glob)
            print(f"[guardiano] ciclo ok: {len(stato['sensori']['quota']['agenti'])} agenti, "
                  f"{len(stato['decisioni'])} decisioni, {stato['durata_ciclo_s']}s", flush=True)
        except Exception as e:
            ledger("errore_ciclo", errore=str(e)[:400])
            print(f"[guardiano] errore nel ciclo: {e}", flush=True)
        if once:
            return 0
        time.sleep(max(1, intervallo - (time.time() - t0)))


if __name__ == "__main__":
    sys.exit(main())
