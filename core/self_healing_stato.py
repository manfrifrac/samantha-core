#!/usr/bin/env python3
# =============================================================================
# SELF HEALING — MODULO DI STATO GERARCHICO A 5 LIVELLI (L0-L4) + MCP + CRON + NGINX
# Ecosistema Agenti — 29/08/2026
# =============================================================================
import os
import sys
import json
import time
import fcntl
import socket
import urllib.request
import subprocess
import glob
import re
from datetime import datetime, timezone, timedelta
from collections import defaultdict
from typing import Dict, Any, Tuple, Optional, List, Set

from self_healing_config import (
    CORE_DIR, DOCS_DIR, LOGS_DIR, SELF_HEALING_STATE_FILE,
    SELF_HEALING_STATE_FALLBACK_FILE, SELF_HEALING_PID_FILE,
    GUARDIANO_LEDGER_FILE, GUARDIANO_STATO_FILE, AGGREGATED_LOG,
    L0_MAX_CPU_LOAD5, L0_MAX_CPU_SAT_PCT, L0_MIN_RAM_FREE_MB,
    L0_MAX_DISK_ROOT_PCT, L0_MAX_DISK_TMP_PCT,
    L1_MANDATORY_TMUX_SESSIONS, L4_MAX_INBOX_STALL_MIN,
    L4_MAX_CONTEXT_WARN_TOKENS, L4_MAX_CONTEXT_CRIT_TOKENS,
    get_service_registry
)

ROME_TZ = timezone(timedelta(hours=2))

def get_iso_timestamp() -> str:
    return datetime.now(ROME_TZ).strftime("%Y-%m-%dT%H:%M:%S%z")

# -----------------------------------------------------------------------------
# LOCKING GESTIONALE (Singola Istanza e Per-Servizio)
# -----------------------------------------------------------------------------
_SUPERVISOR_LOCK_FILE_HANDLE = None
_SERVICE_LOCK_HANDLES: Dict[str, Any] = {}

def acquire_supervisor_lock(pid_file: str = SELF_HEALING_PID_FILE) -> bool:
    global _SUPERVISOR_LOCK_FILE_HANDLE
    try:
        fh = open(pid_file, "w")
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fh.write(f"{os.getpid()}\n")
        fh.flush()
        _SUPERVISOR_LOCK_FILE_HANDLE = fh
        return True
    except (BlockingIOError, OSError):
        return False

def release_supervisor_lock(pid_file: str = SELF_HEALING_PID_FILE):
    global _SUPERVISOR_LOCK_FILE_HANDLE
    if _SUPERVISOR_LOCK_FILE_HANDLE:
        try:
            fcntl.flock(_SUPERVISOR_LOCK_FILE_HANDLE, fcntl.LOCK_UN)
            _SUPERVISOR_LOCK_FILE_HANDLE.close()
        except Exception:
            pass
        _SUPERVISOR_LOCK_FILE_HANDLE = None
    if os.path.exists(pid_file):
        try:
            os.remove(pid_file)
        except Exception:
            pass

def is_supervisor_running(pid_file: str = SELF_HEALING_PID_FILE) -> Tuple[bool, Optional[int]]:
    for pf in [pid_file, "/tmp/ecosistema_supervisore.pid"]:
        if not os.path.exists(pf):
            continue
        try:
            with open(pf, "r") as f:
                content = f.read().strip()
                pid = int(content) if content.isdigit() else None
        except Exception:
            pid = None
        try:
            test_fh = open(pf, "r")
            fcntl.flock(test_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(test_fh, fcntl.LOCK_UN)
            test_fh.close()
            return False, None
        except (BlockingIOError, OSError):
            return True, pid
    return False, None

def acquire_service_lock(service_name: str) -> bool:
    lock_file = f"/tmp/ecosistema_svc_{service_name}.lock"
    try:
        fh = open(lock_file, "w")
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fh.write(f"{os.getpid()}\n")
        fh.flush()
        _SERVICE_LOCK_HANDLES[service_name] = fh
        return True
    except (BlockingIOError, OSError):
        return False

def release_service_lock(service_name: str):
    if service_name in _SERVICE_LOCK_HANDLES:
        fh = _SERVICE_LOCK_HANDLES.pop(service_name)
        try:
            fcntl.flock(fh, fcntl.LOCK_UN)
            fh.close()
        except Exception:
            pass
    lock_file = f"/tmp/ecosistema_svc_{service_name}.lock"
    if os.path.exists(lock_file):
        try:
            os.remove(lock_file)
        except Exception:
            pass

# -----------------------------------------------------------------------------
# SONDE LIVELLO 0 — SERVER & HARDWARE
# -----------------------------------------------------------------------------
def probe_livello_0_server() -> Dict[str, Any]:
    issues = []
    uptime_s = 0.0
    try:
        with open("/proc/uptime", "r") as f:
            uptime_s = float(f.read().split()[0])
    except Exception as e:
        issues.append(f"Uptime non leggibile: {e}")

    cores = os.cpu_count() or 8
    load1, load5, load15 = 0.0, 0.0, 0.0
    sat_pct = 0.0
    try:
        with open("/proc/loadavg", "r") as f:
            parts = f.read().split()
            load1, load5, load15 = float(parts[0]), float(parts[1]), float(parts[2])
            sat_pct = round((load1 / cores) * 100, 1)
            if load5 > L0_MAX_CPU_LOAD5:
                issues.append(f"Load 5m elevato: {load5:.2f} > {L0_MAX_CPU_LOAD5}")
            if sat_pct > L0_MAX_CPU_SAT_PCT:
                issues.append(f"Saturazione CPU elevata: {sat_pct}%")
    except Exception as e:
        issues.append(f"Loadavg non leggibile: {e}")

    mem_total_mb, mem_avail_mb, swap_total_mb, swap_free_mb = 0, 0, 0, 0
    try:
        with open("/proc/meminfo", "r") as f:
            m = {}
            for line in f:
                parts = line.split(":")
                if len(parts) == 2:
                    k = parts[0].strip()
                    v = parts[1].strip().split()[0]
                    if v.isdigit():
                        m[k] = int(v) // 1024
            mem_total_mb = m.get("MemTotal", 0)
            mem_avail_mb = m.get("MemAvailable", 0)
            swap_total_mb = m.get("SwapTotal", 0)
            swap_free_mb = m.get("SwapFree", 0)
            if mem_avail_mb < L0_MIN_RAM_FREE_MB:
                issues.append(f"RAM disponibile bassa: {mem_avail_mb}MB < {L0_MIN_RAM_FREE_MB}MB")
    except Exception as e:
        issues.append(f"Meminfo non leggibile: {e}")

    disk_root_pct, disk_tmp_pct = 0.0, 0.0
    try:
        st_root = os.statvfs("/")
        root_total = (st_root.f_blocks * st_root.f_frsize) / (1024 * 1024 * 1024)
        root_avail = (st_root.f_bavail * st_root.f_frsize) / (1024 * 1024 * 1024)
        disk_root_pct = round(((root_total - root_avail) / root_total) * 100, 1)
        if disk_root_pct > L0_MAX_DISK_ROOT_PCT:
            issues.append(f"Spazio disco / critico: {disk_root_pct}% usato")
    except Exception as e:
        issues.append(f"Statvfs / fallito: {e}")

    try:
        st_tmp = os.statvfs("/tmp")
        tmp_total = (st_tmp.f_blocks * st_tmp.f_frsize) / (1024 * 1024 * 1024)
        tmp_avail = (st_tmp.f_bavail * st_tmp.f_frsize) / (1024 * 1024 * 1024)
        disk_tmp_pct = round(((tmp_total - tmp_avail) / tmp_total) * 100, 1)
        if disk_tmp_pct > L0_MAX_DISK_TMP_PCT:
            issues.append(f"Spazio disco /tmp critico: {disk_tmp_pct}% usato")
    except Exception:
        pass

    net_dns_ok = False
    net_tailscale_ok = False
    try:
        socket.gethostbyname("api.telegram.org")
        net_dns_ok = True
    except Exception:
        issues.append("Risoluzione DNS fallita su api.telegram.org")

    try:
        with socket.create_connection(("100.75.68.16", 6080), timeout=0.3):
            net_tailscale_ok = True
    except Exception:
        net_tailscale_ok = False

    salute = "CRITICAL" if len(issues) > 1 else ("DEGRADED" if len(issues) == 1 else "HEALTHY")

    return {
        "salute": salute,
        "uptime_s": int(uptime_s),
        "uptime_str": f"{int(uptime_s // 3600)}h {int((uptime_s % 3600) // 60)}m",
        "cpu": {
            "cores": cores,
            "load1": load1,
            "load5": load5,
            "load15": load15,
            "saturazione_pct": sat_pct
        },
        "ram": {
            "totale_mb": mem_total_mb,
            "disponibile_mb": mem_avail_mb,
            "usata_mb": mem_total_mb - mem_avail_mb,
            "swap_usata_mb": swap_total_mb - swap_free_mb
        },
        "disco": {
            "root_usata_pct": disk_root_pct,
            "tmp_usata_pct": disk_tmp_pct
        },
        "rete": {
            "dns_internet": net_dns_ok,
            "tailscale_ip": net_tailscale_ok
        },
        "anomalie": issues
    }

# -----------------------------------------------------------------------------
# SONDE LIVELLO 1 — TMUX & SESSIONI
# -----------------------------------------------------------------------------
def get_tmux_dead_panes() -> Set[str]:
    dead = set()
    try:
        r_panes = subprocess.run(["tmux", "list-panes", "-a", "-F", "#{session_name}:#{window_name}\t#{pane_dead}"],
                                 capture_output=True, text=True)
        if r_panes.returncode == 0:
            for line in r_panes.stdout.splitlines():
                parts = line.strip().split("\t")
                if len(parts) >= 2 and parts[1] == "1":
                    dead.add(parts[0])
    except Exception:
        pass
    return dead

def _sessioni_dedicate_da_db() -> Set[str]:
    """Sessioni tmux dedicate (fuori dalla whitelist fissa) derivate dal roster
    Postgres: always_on=true, dismesso non 'true'. Fix D (29/08/2026, report
    diagnosi radio §6): L1_MANDATORY_TMUX_SESSIONS e' una lista scritta a
    mano che non include (e non includerebbe mai da sola) una sessione
    dedicata come 'betty_radio' — additivo: si aggiunge alla lista fissa, non
    la sostituisce, ed e' a prova di guasto (nessuna sessione in piu' se la
    query fallisce)."""
    extra = set()
    try:
        import psycopg2
        conn = psycopg2.connect("dbname=betty_db user=betty_user host=localhost port=5432")
        cur = conn.cursor()
        cur.execute(
            "SELECT data->>'tmux_window' FROM agents "
            "WHERE (data->>'dismesso') IS DISTINCT FROM 'true' "
            "AND (data->>'always_on') = 'true';"
        )
        for (tw,) in cur.fetchall():
            if tw and ":" in tw:
                extra.add(tw.split(":", 1)[0])
        conn.close()
    except Exception:
        pass
    return extra


def probe_livello_1_tmux() -> Dict[str, Any]:
    issues = []
    sessions_found = {}
    total_panes = 0

    try:
        r_sess = subprocess.run(["tmux", "list-sessions", "-F", "#{session_name}\t#{session_windows}"],
                                capture_output=True, text=True)
        if r_sess.returncode == 0:
            for line in r_sess.stdout.splitlines():
                parts = line.strip().split("\t")
                if len(parts) >= 2:
                    sessions_found[parts[0]] = int(parts[1])
        else:
            issues.append("Server Tmux non risponde")
    except Exception as e:
        issues.append(f"Errore query Tmux: {e}")

    mandatory = sorted(set(L1_MANDATORY_TMUX_SESSIONS) | _sessioni_dedicate_da_db())
    mandatory_status = {}
    for ms in mandatory:
        present = ms in sessions_found
        mandatory_status[ms] = present
        if not present:
            issues.append(f"Sessione obbligatoria assente: {ms}")

    dead_panes_set = get_tmux_dead_panes()
    for dp in sorted(dead_panes_set):
        issues.append(f"Pannello morto rilevato (pane is dead): {dp}")

    try:
        r_tot = subprocess.run(["tmux", "list-panes", "-a", "-F", "#{pane_id}"], capture_output=True, text=True)
        if r_tot.returncode == 0:
            total_panes = len(r_tot.stdout.strip().splitlines())
    except Exception:
        pass

    salute = "CRITICAL" if not sessions_found or len(dead_panes_set) > 2 else ("DEGRADED" if issues else "HEALTHY")

    return {
        "salute": salute,
        "server_vivo": bool(sessions_found),
        "sessioni_totali": len(sessions_found),
        "sessioni_obbligatorie": mandatory_status,
        "pannelli_totali": total_panes,
        "dead_panes": len(dead_panes_set),
        "dead_panes_dettaglio": list(dead_panes_set),
        "anomalie": issues
    }

# -----------------------------------------------------------------------------
# SONDE LIVELLO 2 — SERVIZI INFRASTRUTTURALI
# -----------------------------------------------------------------------------
def tcp_probe(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False

def http_probe(url: str, timeout: float = 1.5) -> bool:
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'SelfHealingProbe/2.0'})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 500
    except urllib.error.HTTPError as e:
        return 200 <= e.code < 500
    except Exception:
        return False

def guardiano_heartbeat_probe(max_age_s: float = 60.0) -> Tuple[bool, float]:
    hb_file = "/tmp/betty_docs/guardiano_heartbeat"
    if not os.path.exists(hb_file):
        return False, 99999.0
    try:
        mtime = os.path.getmtime(hb_file)
        age = time.time() - mtime
        return age <= max_age_s, age
    except Exception:
        return False, 99999.0

def pgrep_probe(pattern: str) -> bool:
    try:
        r = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True)
        return r.returncode == 0 and len(r.stdout.strip().splitlines()) > 0
    except Exception:
        return False

def probe_livello_2_servizi() -> Dict[str, Any]:
    registry = get_service_registry()
    details = {}
    counts = {"RUNNING": 0, "PERIODIC_WAIT": 0, "GATE_HOLD": 0, "DOWN": 0, "STOPPED": 0}
    issues = []

    # Mappatura pattern comandi per pgrep
    SCRIPT_PATTERNS = {
        "ordini": "tampone_ordini_fermi.py",
        "agenti_topic": "agenti_topic_vivo.py",
        "quota_vivo": "quota_vivo.py",
        "vpn": "surfshark_watchdog.py",
        "vigile": "vigile_notifiche.py",
        "unified_telegram_engine": "unified_telegram_engine.py",
        "guardiano": "guardiano_unificato.py"
    }

    for name, cfg in registry.items():
        status = "STOPPED"
        health = "UNKNOWN"

        if cfg.gated:
            gate_active = cfg.gate_file and os.path.exists(cfg.gate_file)
            if not gate_active:
                status = "GATE_HOLD"
                health = "STANDBY"
            else:
                status = "RUNNING"
                health = "HEALTHY"
        elif cfg.periodic:
            status = "PERIODIC_WAIT"
            health = "IDLE"
        else:
            # Controllo diretto dal vivo via porte TCP, HTTP, Heartbeat o Processo
            is_live = False
            if cfg.healthcheck_type == "tcp" and cfg.healthcheck_target:
                host, port = cfg.healthcheck_target.split(":")
                is_live = tcp_probe(host, int(port))
            elif cfg.healthcheck_type == "http" and cfg.healthcheck_target:
                is_live = http_probe(cfg.healthcheck_target)
            elif cfg.healthcheck_type == "heartbeat":
                ok, age = guardiano_heartbeat_probe()
                is_live = ok or pgrep_probe("guardiano_unificato.py")
            elif name in SCRIPT_PATTERNS:
                is_live = pgrep_probe(SCRIPT_PATTERNS[name])

            if is_live:
                status = "RUNNING"
                health = "HEALTHY"
            else:
                status = "DOWN"
                health = "DOWN"
                issues.append(f"Servizio critico down: {name}")

        counts[status] = counts.get(status, 0) + 1
        details[name] = {
            "stato": status,
            "salute": health,
            "pid": None,
            "porte": cfg.ports,
            "restarts": 0,
            "descrizione": cfg.description
        }

    # Controllo autonomo errori ricorrenti nei log di servizio (Fase 9c)
    err_log = probe_errori_ricorrenti_servizi()
    for e in err_log:
        issues.append(e["messaggio"])

    salute = "CRITICAL" if counts.get("DOWN", 0) > 0 else ("DEGRADED" if issues else "HEALTHY")

    return {
        "salute": salute,
        "servizi_totali": len(registry),
        "conteggi": counts,
        "servizi": details,
        "anomalie": issues,
        "errori_ricorrenti_log": err_log
    }

_cache_errori_log = {"ts": 0.0, "mtime": 0.0, "size": 0, "valore": None}

def probe_errori_ricorrenti_servizi(log_path: str = "/root/ecosistema_agenti/logs/ecosistema_aggregato.log", max_lines: int = 15000, finestra_minuti: int = 60, soglia_ripetizioni: int = 2, ttl_s: float = 10.0) -> List[Dict[str, Any]]:
    """Analizza gli errori ricorrenti nei log aggregati dei servizi negli ultimi N minuti (Fase 9c)."""
    if not os.path.exists(log_path):
        return []

    now_t = time.time()
    st_file = None
    try:
        st_file = os.stat(log_path)
        if _cache_errori_log["valore"] is not None and (now_t - _cache_errori_log["ts"] < ttl_s):
            if _cache_errori_log["mtime"] == st_file.st_mtime and _cache_errori_log["size"] == st_file.st_size:
                return list(_cache_errori_log["valore"])
    except Exception:
        pass
    
    anomalie = []
    try:
        with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()[-max_lines:]
    except Exception:
        return []

    now = datetime.now()
    errori_per_servizio = defaultdict(lambda: defaultdict(int))

    pattern_errore = re.compile(r"(invalid_grant|Exception|Traceback|Errore|Error|FATAL|Crash|ConnectionRefused|timed out)", re.I)

    for line in lines:
        parts = [p.strip() for p in line.strip().split("|")]
        if len(parts) < 3:
            continue

        ts_str = parts[0]
        try:
            ts_clean = re.sub(r"([+-]\d{2}):?(\d{2})$", "", ts_str)
            line_dt = datetime.fromisoformat(ts_clean)
            if (now - line_dt).total_seconds() > (finestra_minuti * 60):
                continue
        except Exception:
            continue

        svc = None
        msg = None
        if len(parts) >= 4 and parts[1] in ("[L2]", "L2", "[L1]", "[L3]"):
            svc = parts[2]
            msg = "|".join(parts[3:])
        elif len(parts) >= 3:
            svc = parts[1]
            msg = "|".join(parts[2:])
        
        if not svc or not msg:
            continue

        # Filtri di esclusione
        if "0 ERR" in msg or "0 bloccati" in msg or "SUCCESS" in msg or "[INFO]" in msg or "COMPLETATA CON ERRORI" in msg:
            continue

        if pattern_errore.search(msg):
            if "invalid_grant" in msg:
                sig = f"{svc} invalid_grant (Token Google scaduto/revocato)"
            elif "ConnectionRefused" in msg or "Connection refused" in msg:
                sig = "Connection refused"
            elif "Timeout" in msg or "timed out" in msg:
                sig = "Timeout di connessione"
            else:
                clean_m = re.sub(r"\[.*?\]", "", msg)
                clean_m = re.sub(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}", "", clean_m)
                sig = clean_m[:60].strip()

            errori_per_servizio[svc][sig] += 1

    for svc, sigs in errori_per_servizio.items():
        for sig, count in sigs.items():
            if count >= soglia_ripetizioni:
                anomalie.append({
                    "servizio": svc,
                    "firma": sig,
                    "conteggio": count,
                    "messaggio": f"Servizio '{svc}': errore ricorrente '{sig}' ({count} volte negli ultimi {finestra_minuti}m)"
                })

    _cache_errori_log["ts"] = now_t
    _cache_errori_log["mtime"] = st_file.st_mtime if st_file else 0.0
    _cache_errori_log["size"] = st_file.st_size if st_file else 0
    _cache_errori_log["valore"] = anomalie
    return anomalie

# -----------------------------------------------------------------------------
# SONDE LIVELLO 3 — OUTPUT SEMANTICO DEI SERVIZI
# -----------------------------------------------------------------------------
def probe_livello_3_output_semantico() -> Dict[str, Any]:
    issues = []
    checks = {}

    g_ledger_ok = True
    g_last_action = None
    g_recent_exceptions = 0
    if os.path.exists(GUARDIANO_LEDGER_FILE):
        try:
            with open(GUARDIANO_LEDGER_FILE, "r") as f:
                lines = f.readlines()[-30:]
                for line in reversed(lines):
                    try:
                        entry = json.loads(line)
                        if entry.get("evento") == "azione" and not g_last_action:
                            g_last_action = entry
                        if "name '" in line and "is not defined" in line:
                            g_recent_exceptions += 1
                        if "Traceback" in line:
                            g_recent_exceptions += 1
                    except Exception:
                        pass
        except Exception as e:
            issues.append(f"Lettura guardiano_ledger fallita: {e}")
            g_ledger_ok = False

    if g_last_action and not g_last_action.get("esito_ok", False):
        issues.append(f"Ultima azione guardiano fallita: {g_last_action.get('dettaglio')}")

    checks["guardiano"] = {
        "ledger_integro": g_ledger_ok,
        "eccezioni_recenti": g_recent_exceptions,
        "ultima_azione": g_last_action.get("azione") if g_last_action else None,
        "ultimo_esito_ok": g_last_action.get("esito_ok") if g_last_action else True
    }

    tg_running = False
    try:
        r = subprocess.run(["pgrep", "-f", "unified_telegram_engine.py"], capture_output=True, text=True)
        tg_running = r.returncode == 0 and len(r.stdout.strip().splitlines()) > 0
    except Exception:
        pass
    checks["telegram_engine"] = {
        "motore_vivo": tg_running,
        "polling_attivo": tg_running
    }
    if not tg_running:
        issues.append("Unified Telegram Engine non risulta in esecuzione")

    wa_ok = tcp_probe("127.0.0.1", 3005)
    checks["whatsapp"] = {
        "bridge_online": wa_ok,
        "porta_3005": wa_ok
    }
    if not wa_ok:
        issues.append("WhatsApp Bridge non raggiungibile sulla porta 3005")

    chrome_ok = False
    try:
        req = urllib.request.Request("http://127.0.0.1:9224/json/version")
        with urllib.request.urlopen(req, timeout=1.0) as resp:
            data = json.loads(resp.read().decode())
            if "webSocketDebuggerUrl" in data:
                chrome_ok = True
    except Exception:
        pass
    checks["chrome_gui"] = {
        "cdp_version_ok": chrome_ok,
        "porta_9224": chrome_ok
    }
    if not chrome_ok:
        issues.append("Chrome CDP non restituisce webSocketDebuggerUrl su 9224")

    spend_ok = tcp_probe("127.0.0.1", 8790)
    checks["spend_guard"] = {
        "proxy_spend_ok": spend_ok,
        "porta_8790": spend_ok
    }
    if not spend_ok:
        issues.append("Spend Guard proxy non raggiungibile su 8790")

    salute = "CRITICAL" if len(issues) > 1 else ("DEGRADED" if issues else "HEALTHY")

    return {
        "salute": salute,
        "verifiche": checks,
        "anomalie": issues
    }

# -----------------------------------------------------------------------------
# SONDE LIVELLO 4 — AGENTI & STANZE (Stato e Rilevamento Blocchi)
# -----------------------------------------------------------------------------
def probe_livello_4_agenti() -> Dict[str, Any]:
    issues = []
    agent_list = []
    dead_panes_set = get_tmux_dead_panes()
    
    g_agenti = {}
    g_contesto = {}
    if os.path.exists(GUARDIANO_STATO_FILE):
        try:
            with open(GUARDIANO_STATO_FILE, "r") as f:
                g_data = json.load(f)
                for a in g_data.get("sensori", {}).get("quota", {}).get("agenti", []):
                    tw = a.get("tmux_window")
                    if tw:
                        g_agenti[tw] = a
                for c in g_data.get("sensori", {}).get("contesto", {}).get("voci", []):
                    slug = c.get("slug")
                    if slug:
                        g_contesto[slug] = c
        except Exception:
            pass

    db_agents = []
    seen_windows = set()
    try:
        import psycopg2
        conn = psycopg2.connect("dbname=betty_db user=betty_user host=localhost port=5432")
        cur = conn.cursor()
        cur.execute("SELECT agent_id, data FROM agents WHERE (data->>'dismesso') IS DISTINCT FROM 'true';")
        for row in cur.fetchall():
            slug, data = row[0], row[1] or {}
            # Esclude record virtuali di pool (es. samantha/cosimo/zero_one virtuali) e routing_only
            if data.get("routing_only") or data.get("pool_di") or str(data.get("dismesso")).lower() == "true":
                continue
            tw = data.get("tmux_window")
            if tw:
                seen_windows.add(tw)
                db_agents.append((slug, data))
        conn.close()
    except Exception as e:
        issues.append(f"Query Postgres agents fallita: {e}")

    tot_attivi = 0
    tot_bloccati = 0

    for slug, data in db_agents:
        tw = data.get("tmux_window")
        if not tw:
            continue

        g_info = g_agenti.get(tw, {})
        c_info = g_contesto.get(slug, {})

        is_dead_pane = tw in dead_panes_set or g_info.get("pane_dead", False)
        cli_vivo = g_info.get("cli_vivo", False) and not is_dead_pane
        engine = g_info.get("engine_reale") or data.get("engine") or "claude"
        model = g_info.get("model_reale") or data.get("model") or "unknown"
        tokens = c_info.get("token", 0)

        # Fallback real-time se guardiano non ha ancora indicizzato la finestra (es. nuovo exec o riavvio)
        if not cli_vivo and not is_dead_pane:
            try:
                import tmux_esatto
                pane_pid = tmux_esatto.pane_pid_finestra(tw)
                if pane_pid:
                    sub_pids = tmux_esatto.ottieni_albero_pids([int(pane_pid)])
                    if sub_pids:
                        r_ps = subprocess.run(["ps", "-o", "args=", "-p", ",".join(map(str, sub_pids))],
                                              capture_output=True, text=True)
                        args_out = r_ps.stdout
                        for bin_pat, eng_name in [
                            (r"(^|/)agy($|\s)", "agy"),
                            (r"(^|/)claude($|\s)", "claude"),
                            (r"(^|/)qwen($|\s)", "qwen"),
                            (r"(^|/)node\s+\S*/deepcode($|\s)", "deepcode")
                        ]:
                            if re.search(bin_pat, args_out, re.M):
                                cli_vivo = True
                                engine = eng_name
                                m_match = re.search(r"--model[=\s]+([^\s'\"]+)", args_out)
                                if m_match:
                                    model = m_match.group(1)
                                break
            except Exception:
                pass

        inbox_dir = f"/root/ecosistema_agenti/a2a/{slug}/inbox"
        inbox_count = 0
        inbox_max_age_min = 0.0
        if os.path.exists(inbox_dir):
            try:
                files = [os.path.join(inbox_dir, f) for f in os.listdir(inbox_dir) if f.endswith(".md")]
                inbox_count = len(files)
                if files:
                    oldest_mtime = min(os.path.getmtime(f) for f in files)
                    inbox_max_age_min = round((time.time() - oldest_mtime) / 60, 1)
            except Exception:
                pass

        bloccato = False
        motivo_blocco = None

        if is_dead_pane:
            bloccato = True
            motivo_blocco = "Pannello Tmux morto (pane is dead)"
        elif not cli_vivo and data.get("always_on", False) and not data.get("spento_nota"):
            bloccato = True
            motivo_blocco = "Agente always_on con processo CLI assente (shell nuda)"
        elif inbox_count > 0 and inbox_max_age_min > L4_MAX_INBOX_STALL_MIN and cli_vivo:
            bloccato = True
            motivo_blocco = f"Consegna ferma: {inbox_count} msg in inbox da {inbox_max_age_min}m"
        elif tokens > L4_MAX_CONTEXT_CRIT_TOKENS and cli_vivo:
            bloccato = True
            motivo_blocco = f"Saturazione contesto critica ({tokens} tokens > {L4_MAX_CONTEXT_CRIT_TOKENS})"

        if bloccato:
            tot_bloccati += 1
            issues.append(f"Agente bloccato [{slug}]: {motivo_blocco}")
        elif cli_vivo:
            tot_attivi += 1

        stato_agente = "BLOCKED" if bloccato else ("RUNNING" if cli_vivo else "STANDBY")

        agent_list.append({
            "slug": slug,
            "ruolo": data.get("ruolo", "specialista"),
            "coordinatore": data.get("coordinatore", False),
            "creato_da": data.get("creato_da") or "n/d",
            "tmux_window": tw,
            "engine": engine,
            "model": model,
            "cli_vivo": cli_vivo,
            "tokens_contesto": tokens,
            "inbox_pendenti": inbox_count,
            "inbox_max_age_min": inbox_max_age_min,
            "stato": stato_agente,
            "bloccato": bloccato,
            "motivo_blocco": motivo_blocco
        })

    for dp in dead_panes_set:
        if dp not in seen_windows:
            tot_bloccati += 1
            issues.append(f"Stanza/Exec morta in Tmux [{dp}]: Pannello crashato (pane is dead)")
            agent_list.append({
                "slug": dp.split(":")[-1].lower(),
                "ruolo": "exec_orfano",
                "coordinatore": False,
                "tmux_window": dp,
                "engine": "unknown",
                "model": "unknown",
                "cli_vivo": False,
                "tokens_contesto": 0,
                "inbox_pendenti": 0,
                "inbox_max_age_min": 0.0,
                "stato": "BLOCKED",
                "bloccato": True,
                "motivo_blocco": "Pannello Tmux morto (pane is dead)"
            })

    sotto_soglia = sum(1 for a in agent_list if a["cli_vivo"] and a.get("tokens_contesto", 0) <= L4_MAX_CONTEXT_WARN_TOKENS)
    vicino_limite = sum(1 for a in agent_list if a["cli_vivo"] and L4_MAX_CONTEXT_WARN_TOKENS < a.get("tokens_contesto", 0) <= L4_MAX_CONTEXT_CRIT_TOKENS)
    oltre_limite = sum(1 for a in agent_list if a["cli_vivo"] and a.get("tokens_contesto", 0) > L4_MAX_CONTEXT_CRIT_TOKENS)

    salute = "CRITICAL" if tot_bloccati > 1 or oltre_limite > 0 else ("DEGRADED" if tot_bloccati > 0 or vicino_limite > 0 else "HEALTHY")

    return {
        "salute": salute,
        "agenti_totali": len(agent_list),
        "attivi": tot_attivi,
        "bloccati": tot_bloccati,
        "fasce_contesto": {
            "sotto_soglia": sotto_soglia,
            "vicino_limite": vicino_limite,
            "oltre_limite": oltre_limite,
            "soglia_warn": L4_MAX_CONTEXT_WARN_TOKENS,
            "soglia_crit": L4_MAX_CONTEXT_CRIT_TOKENS
        },
        "agenti": agent_list,
        "anomalie": issues
    }

# -----------------------------------------------------------------------------
# SONDE MCP — SERVER & GESTIONE UNIFICATA (Fase 4)
# -----------------------------------------------------------------------------
def probe_mcp_servers() -> Dict[str, Any]:
    issues = []
    
    long_running_expected = {
        "chrome-contabo": {
            "type": "daemon",
            "ports": [9224],
            "target": "127.0.0.1:9224",
            "desc": "Chrome GUI CDP (:9224) & x11vnc (:15900)",
            "studios_count": 40
        },
        "chrome-local": {
            "type": "tunnel",
            "ports": [9222],
            "target": "127.0.0.1:9222",
            "desc": "Tunnel SSH Surface Chrome CDP (:9222)",
            "studios_count": 15
        },
        "whatsapp-mcp": {
            "type": "daemon",
            "ports": [3005],
            "target": "127.0.0.1:3005",
            "desc": "Baileys WhatsApp Live WebSocket (:3005)",
            "studios_count": 4
        },
        "radio-betty": {
            "type": "gated_daemon",
            "ports": [],
            "target": "Chrome Tab / sveglia_audio",
            "desc": "Radio Betty MCP & Player",
            "studios_count": 1
        }
    }

    stdio_connectors = {
        "arturo": {"script": "/root/ecosistema_agenti/core/mcp_arturo_shared.py", "studios_count": 6},
        "messages-search": {"script": "/root/ecosistema_agenti/mcp_servers/mcp_messages_search.py", "studios_count": 4},
        "gdrive": {"script": "/root/ecosistema_agenti/mcp_servers/mcp_gdrive.py", "studios_count": 4},
        "bandi-search": {"script": "/root/ecosistema_agenti/mcp_servers/mcp_bandi_search.py", "studios_count": 3},
        "pubmed": {"script": "/root/ecosistema_agenti/mcp_servers/mcp_pubmed.py", "studios_count": 1},
        "regista": {"script": "/root/ecosistema_agenti/mcp_servers/mcp_regista.py", "studios_count": 2},
        "web-search": {"script": "/root/ecosistema_agenti/mcp_servers/mcp_web_search.py", "studios_count": 1},
        "manfredo-location": {"script": "/root/ecosistema_agenti/mcp_servers/mcp_manfredo_location.py", "studios_count": 1},
        "domenico-edile-search": {"script": "/root/ecosistema_agenti/core/mcp_edile.py", "studios_count": 1}
    }

    mcp_status_list = []
    vivi_count = 0

    for name, info in long_running_expected.items():
        is_online = False
        if info["ports"]:
            is_online = all(tcp_probe("127.0.0.1", p) for p in info["ports"])
        elif name == "radio-betty":
            is_online = tcp_probe("127.0.0.1", 9224)

        if is_online:
            vivi_count += 1
            st = "RUNNING"
            sal = "HEALTHY"
        else:
            st = "DOWN"
            sal = "CRITICAL"
            issues.append(f"Server MCP long-running non attivo: {name} (target: {info['target']})")

        mcp_status_list.append({
            "name": name,
            "tipo": "LONG-RUNNING",
            "stato": st,
            "salute": sal,
            "target": info["target"],
            "studi_utilizzatori": info["studios_count"],
            "descrizione": info["desc"]
        })

    for name, info in stdio_connectors.items():
        exists = os.path.exists(info["script"])
        st = "STDIO_READY" if exists else "SCRIPT_MISSING"
        sal = "HEALTHY" if exists else "CRITICAL"
        if not exists:
            issues.append(f"Script server MCP stdio mancante su disco: {info['script']}")

        mcp_status_list.append({
            "name": name,
            "tipo": "STDIO_ON_DEMAND",
            "stato": st,
            "salute": sal,
            "target": os.path.basename(info["script"]),
            "studi_utilizzatori": info["studios_count"],
            "descrizione": f"Connector stdio per {name}"
        })

    salute = "CRITICAL" if issues else "HEALTHY"

    return {
        "salute": salute,
        "totali": len(mcp_status_list),
        "long_running_totali": len(long_running_expected),
        "long_running_vivi": vivi_count,
        "stdio_totali": len(stdio_connectors),
        "server": mcp_status_list,
        "anomalie": issues
    }

# -----------------------------------------------------------------------------
# SONDE CRON & SCHEDULER — VISIBILITA' SCHEDULAZIONI (Fase 4)
# -----------------------------------------------------------------------------
def probe_cron_schedulers() -> Dict[str, Any]:
    issues = []
    cron_jobs = []
    active_count = 0
    disabled_count = 0

    try:
        r = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
        if r.returncode == 0:
            lines = r.stdout.splitlines()
            for line in lines:
                l_str = line.strip()
                if not l_str:
                    continue
                if l_str.startswith("#"):
                    if "INCIDENTE_SICUREZZA" in l_str or "DISATTIVATO" in l_str or "IBERNAZIONE" in l_str:
                        disabled_count += 1
                        cron_jobs.append({
                            "sorgente": "crontab (root)",
                            "schedulazione": "DISABLED",
                            "comando": l_str[:70] + "...",
                            "stato": "DISABLED",
                            "descrizione": "Regola commentata / bonificata"
                        })
                else:
                    parts = l_str.split(maxsplit=5)
                    if len(parts) >= 6:
                        cadence = " ".join(parts[:5])
                        cmd = parts[5]
                        active_count += 1
                        
                        target_info = "Infrastruttura"
                        if "sentinella.py" in cmd or "cron_wrapper.py" in cmd:
                            target_info = "Minerva (Interpelli Scuola)"
                        elif "sentinella_contesto.py" in cmd:
                            target_info = "Sentinella Contesto Agenti"
                        elif "garbage_collector_exec.py" in cmd:
                            target_info = "GC Exec Dismessi"
                        elif "sensore_memoria_leggera.py" in cmd:
                            target_info = "Sensore Memoria Leggera"
                        elif "sveglia_betty.py" in cmd:
                            target_info = "Briefing Mattutino Telegram"
                        elif "acme.sh" in cmd:
                            target_info = "Rinnovo Certificati SSL"

                        cron_jobs.append({
                            "sorgente": "crontab (root)",
                            "schedulazione": cadence,
                            "comando": cmd.split(">>")[0].strip()[:60],
                            "stato": "ACTIVE",
                            "descrizione": target_info
                        })
    except Exception as e:
        issues.append(f"Errore lettura crontab: {e}")

    internal_periodic = [
        {"nome": "autorefresh", "intervallo": "Ogni 300s (5m)", "target": "Refresher Memoria Agenti"},
        {"nome": "fondi_flat", "intervallo": "Ogni 300s (5m)", "target": "Probe Consumi e Quote Provider"},
        {"nome": "sync_messaggi", "intervallo": "Ogni 1200s (20m)", "target": "Sync Messaggi WhatsApp/Gmail"}
    ]
    for ip in internal_periodic:
        cron_jobs.append({
            "sorgente": "Self Healing (L2)",
            "schedulazione": ip["intervallo"],
            "comando": ip["nome"],
            "stato": "ACTIVE",
            "descrizione": ip["target"]
        })
        active_count += 1

    return {
        "salute": "HEALTHY",
        "jobs_totali": len(cron_jobs),
        "jobs_attivi": active_count,
        "jobs_disabilitati": disabled_count,
        "jobs": cron_jobs,
        "anomalie": issues
    }

# -----------------------------------------------------------------------------
# SONDE NGINX — SITI PUBBLICATI & VHOST BACKENDS (Fase 4)
# -----------------------------------------------------------------------------
def probe_nginx_vhosts() -> Dict[str, Any]:
    issues = []
    vhosts_list = []
    
    nginx_live = False
    try:
        r = subprocess.run(["systemctl", "is-active", "nginx"], capture_output=True, text=True)
        nginx_live = r.stdout.strip() == "active"
    except Exception:
        pass

    if not nginx_live:
        issues.append("Servizio Nginx non attivo su systemd")

    sites_dir = "/etc/nginx/sites-enabled"
    if os.path.exists(sites_dir):
        for sf in glob.glob(f"{sites_dir}/*"):
            name = os.path.basename(sf)
            try:
                with open(sf, "r") as fp:
                    content = fp.read()
                
                server_names = re.findall(r'server_name\s+([^;]+);', content)
                proxies = re.findall(r'proxy_pass\s+([^;]+);', content)
                roots = re.findall(r'root\s+([^;]+);', content)
                
                s_name = server_names[0].strip() if server_names else name
                
                backends_status = []
                for p in proxies:
                    p_clean = p.strip()
                    m = re.search(r'http://(?:127\.0\.1|localhost|127\.0\.0\.1):(\d+)', p_clean)
                    status = "ONLINE"
                    if m:
                        port = int(m.group(1))
                        status = "ONLINE" if tcp_probe("127.0.0.1", port) else "OFFLINE"
                    backends_status.append({"url": p_clean, "status": status})

                vhosts_list.append({
                    "vhost": s_name,
                    "config": name,
                    "porte": "80 / 443 SSL",
                    "tipo": "REVERSE_PROXY" if proxies else "STATIC_FILES",
                    "root": roots[0].strip().replace("\n", " ") if roots else "N/A",
                    "backends": backends_status
                })
            except Exception as e:
                issues.append(f"Errore lettura vhost {name}: {e}")

    salute = "CRITICAL" if not nginx_live else ("DEGRADED" if issues else "HEALTHY")

    return {
        "salute": salute,
        "nginx_attivo": nginx_live,
        "vhosts_totali": len(vhosts_list),
        "vhosts": vhosts_list,
        "anomalie": issues
    }

# -----------------------------------------------------------------------------
# SONDE SPESA & FONDI — DEEPSEEK & QUOTE FLAT CON COUNTDOWN (Fasi 5-6)
# -----------------------------------------------------------------------------
def calcola_countdown_claude_5h(reset_str: Optional[str]) -> Optional[str]:
    if not reset_str:
        return None
    m = re.search(r"(\d{1,2}):(\d{2})\s*(am|pm)", reset_str, re.I)
    if not m:
        return None
    h, mins, ampm = int(m.group(1)), int(m.group(2)), m.group(3).lower()
    if ampm == "pm" and h < 12:
        h += 12
    elif ampm == "am" and h == 12:
        h = 0

    now = datetime.now()
    target = now.replace(hour=h, minute=mins, second=0, microsecond=0)
    if target < now:
        return "ora"
    diff = target - now
    tot_min = int(diff.total_seconds() // 60)
    dh = tot_min // 60
    dm = tot_min % 60
    return f"{dh}h{dm:02d}m" if dh > 0 else f"{dm}m"

def calcola_countdown_claude_settimana(reset_str: Optional[str]) -> Optional[str]:
    if not reset_str:
        return None
    m = re.search(r"([A-Za-z]{3})\s+(\d{1,2}),?\s+(\d{1,2}):(\d{2})\s*(am|pm)", reset_str, re.I)
    if not m:
        return None
    month_str, day, h, mins, ampm = m.group(1), int(m.group(2)), int(m.group(3)), int(m.group(4)), m.group(5).lower()
    if ampm == "pm" and h < 12:
        h += 12
    elif ampm == "am" and h == 12:
        h = 0

    months = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}
    month = months.get(month_str.lower()[:3], 8)
    now = datetime.now()
    year = now.year
    target = datetime(year, month, day, h, mins)
    if target < now:
        return "scaduto"
    diff = target - now
    giorni = diff.days
    ore = diff.seconds // 3600
    return f"{giorni}g{ore}h" if giorni > 0 else f"{ore}h"

def calcola_countdown_agy(reset_str: Optional[str], eta_s: float = 0.0) -> Optional[str]:
    if not reset_str:
        return None
    m = re.search(r"(\d+)\s*h\s*(\d+)?\s*m?", reset_str, re.I)
    if not m:
        m2 = re.search(r"(\d+)\s*m", reset_str, re.I)
        if m2:
            tot_min = max(0, int(m2.group(1)) - int(eta_s // 60))
            return f"{tot_min}m"
        return reset_str.strip()
    h = int(m.group(1))
    mins = int(m.group(2) or 0)
    tot_min = max(0, (h * 60 + mins) - int(eta_s // 60))
    tot_h = tot_min // 60
    rem_m = tot_min % 60
    if tot_h >= 24:
        g = tot_h // 24
        rh = tot_h % 24
        return f"{g}g{rh}h"
    elif tot_h > 0:
        return f"{tot_h}h{rem_m:02d}m"
    else:
        return f"{rem_m}m"

def probe_spesa_e_fondi() -> Dict[str, Any]:
    spend_file = "/root/ecosistema_agenti/core/deepseek_spend_guard_state.json"
    usage_file = "/root/ecosistema_agenti/logs/guardiano_usage_cache.json"
    sonda_cache_file = "/root/ecosistema_agenti/logs/engine_budget_sonda_cache.json"

    spesa_usd = None
    chiamate = 0
    spendguard_vivo = tcp_probe("127.0.0.1", 8790)

    if os.path.exists(spend_file):
        try:
            with open(spend_file, "r") as f:
                d = json.load(f)
                spesa_usd = d.get("spesa_stimata_usd", 0.0)
                chiamate = d.get("chiamate", 0)
        except Exception:
            pass

    claude_residuo_pct = None
    claude_reset_5h_raw = None
    claude_reset_week_raw = None
    agy_residuo_pct = None
    agy_reset_5h_raw = None
    agy_reset_week_raw = None
    eta_s = 0.0

    if os.path.exists(usage_file):
        try:
            with open(usage_file, "r") as f:
                d = json.load(f)
                c_used = d.get("claude_sessione_pct")
                if c_used is not None:
                    claude_residuo_pct = round(100.0 - float(c_used), 1)
                claude_reset_5h_raw = d.get("claude_reset_sessione")
                for r in d.get("claude_righe", []):
                    if "Current week" in r and "Resets" in r:
                        mr = re.search(r"Resets\s+(.*)", r)
                        if mr:
                            claude_reset_week_raw = mr.group(1).strip()
                a_free = d.get("agy_5h_disponibile_pct")
                if a_free is not None:
                    agy_residuo_pct = round(float(a_free), 1)
                agy_reset_5h_raw = d.get("agy_reset_5h")
                agy_reset_week_raw = d.get("agy_reset_settimana")
                eta_s = max(0.0, time.time() - float(d.get("ts", time.time())))
        except Exception:
            pass

    # Fallback su engine_budget_sonda_cache.json se usage_cache era parziale
    if os.path.exists(sonda_cache_file) and (not claude_reset_week_raw or not agy_reset_week_raw):
        try:
            with open(sonda_cache_file, "r") as f:
                sc = json.load(f)
                c_data = sc.get("claude", {})
                for r in c_data.get("righe", []):
                    if "Current week" in r and "Resets" in r and not claude_reset_week_raw:
                        mr = re.search(r"Resets\s+(.*)", r)
                        if mr:
                            claude_reset_week_raw = mr.group(1).strip()
                    if "Current session" in r and "Resets" in r and not claude_reset_5h_raw:
                        mr = re.search(r"Resets\s+(.*)", r)
                        if mr:
                            claude_reset_5h_raw = mr.group(1).strip()
                a_data = sc.get("agy", {})
                if "ts" in a_data and eta_s == 0.0:
                    eta_s = max(0.0, time.time() - float(a_data["ts"]))
                for r in a_data.get("righe", []):
                    if "Five Hour" in r and ("Refreshes in" in r or "Resets in" in r) and not agy_reset_5h_raw:
                        mr = re.search(r"(?:Refreshes in|Resets in)\s+(.*)", r, re.I)
                        if mr:
                            agy_reset_5h_raw = mr.group(1).strip()
                    if "Weekly" in r and ("Refreshes in" in r or "Resets in" in r) and not agy_reset_week_raw:
                        mr = re.search(r"(?:Refreshes in|Resets in)\s+(.*)", r, re.I)
                        if mr:
                            agy_reset_week_raw = mr.group(1).strip()
        except Exception:
            pass

    # Calcolo countdown tempo reale
    cd_claude_5h = calcola_countdown_claude_5h(claude_reset_5h_raw)
    cd_claude_week = calcola_countdown_claude_settimana(claude_reset_week_raw)
    cd_agy_5h = calcola_countdown_agy(agy_reset_5h_raw, eta_s)
    cd_agy_week = calcola_countdown_agy(agy_reset_week_raw, eta_s)

    # Costruzione stringhe formattate con countdown
    c_parts = []
    if claude_residuo_pct is not None:
        c_parts.append(f"{int(claude_residuo_pct)}%")
    if cd_claude_5h and cd_claude_week:
        c_parts.append(f"(5h: {cd_claude_5h} │ sett: {cd_claude_week})")
    elif cd_claude_5h:
        c_parts.append(f"(5h: {cd_claude_5h})")
    claude_str = " ".join(c_parts) if c_parts else "n/d"

    a_parts = []
    if agy_residuo_pct is not None:
        a_parts.append(f"{int(agy_residuo_pct)}%")
    if cd_agy_5h and cd_agy_week:
        a_parts.append(f"(5h: {cd_agy_5h} │ sett: {cd_agy_week})")
    elif cd_agy_5h:
        a_parts.append(f"(5h: {cd_agy_5h})")
    agy_str = " ".join(a_parts) if a_parts else "n/d"

    saldo_usd = None
    try:
        import motori_fondi
        mf_data = motori_fondi.load()
        ds_dettagli = mf_data.get("motori", {}).get("deepseek", {}).get("dettagli", {})
        saldo_usd = ds_dettagli.get("saldo_usd")
    except Exception:
        pass

    return {
        "deepseek": {
            "spesa_usd": spesa_usd,
            "spesa_str": f"${spesa_usd:.4f}" if spesa_usd is not None else "n/d",
            "saldo_usd": saldo_usd,
            "saldo_str": f"${saldo_usd:.2f}" if saldo_usd is not None else "n/d",
            "chiamate": chiamate,
            "spend_guard_online": spendguard_vivo
        },
        "fondi_flat": {
            "claude_sessione_residuo_pct": claude_residuo_pct,
            "claude_str": claude_str,
            "claude_countdown_5h": cd_claude_5h,
            "claude_countdown_settimana": cd_claude_week,
            "agy_5h_disponibile_pct": agy_residuo_pct,
            "agy_str": agy_str,
            "agy_countdown_5h": cd_agy_5h,
            "agy_countdown_settimana": cd_agy_week
        }
    }

# -----------------------------------------------------------------------------
# SONDA SCADENZE CREDENZIALI & CERTIFICATI VITALI (Fase 9b)
# -----------------------------------------------------------------------------
_cache_scadenze = {"ts": 0.0, "valore": None}

def probe_scadenze_vitali(ttl_s: float = 60.0) -> Dict[str, Any]:
    """Censisce credenziali OAuth, token e certificati SSL con scadenza nota."""
    now_t = time.time()
    if _cache_scadenze["valore"] is not None and (now_t - _cache_scadenze["ts"] < ttl_s):
        return dict(_cache_scadenze["valore"])

    voci = []
    anomalie = []
    now = datetime.now(timezone.utc)

    # 1. Gmail Token OAuth
    gmail_token_path = "/root/ecosistema_agenti/core/google_token.json"
    if os.path.exists(gmail_token_path):
        try:
            with open(gmail_token_path, "r") as f:
                d = json.load(f)
                exp_str = d.get("expiry")
                if exp_str:
                    dt = datetime.fromisoformat(exp_str.replace("Z", "+00:00"))
                    diff = dt - now
                    giorni = diff.days
                    if giorni < 0:
                        st = "EXPIRED"
                        voci.append({
                            "tipo": "OAuth",
                            "target": "Gmail",
                            "scadenza": dt.strftime("%d/%m"),
                            "giorni": giorni,
                            "stato": st,
                            "label": f"🔴 Gmail (SCADUTO da {abs(giorni)}g)"
                        })
                        anomalie.append(f"Token Gmail SCADUTO da {abs(giorni)}g ({dt.strftime('%d/%m')}) — riautorizzazione necessaria")
                    else:
                        st = "WARN" if giorni <= 7 else "OK"
                        badge = "🟡" if st == "WARN" else "🟢"
                        voci.append({
                            "tipo": "OAuth",
                            "target": "Gmail",
                            "scadenza": dt.strftime("%d/%m"),
                            "giorni": giorni,
                            "stato": st,
                            "label": f"{badge} Gmail ({giorni}g)"
                        })
                        if st == "WARN":
                            anomalie.append(f"Token Gmail in scadenza tra {giorni}g ({dt.strftime('%d/%m')})")
        except Exception:
            pass

    # 2. Claude OAuth Token
    claude_cred_path = "/root/.claude/.credentials.json"
    if os.path.exists(claude_cred_path):
        try:
            with open(claude_cred_path, "r") as f:
                d = json.load(f)
                oauth = d.get("claudeAiOauth", {})
                ref_exp_ms = oauth.get("refreshTokenExpiresAt")
                if ref_exp_ms:
                    dt_ref = datetime.fromtimestamp(ref_exp_ms / 1000.0, tz=timezone.utc)
                    diff = dt_ref - now
                    giorni = diff.days
                    if giorni < 0:
                        st = "EXPIRED"
                        voci.append({
                            "tipo": "OAuth",
                            "target": "Claude",
                            "scadenza": dt_ref.strftime("%d/%m"),
                            "giorni": giorni,
                            "stato": st,
                            "label": f"🔴 Claude OAuth (SCADUTO da {abs(giorni)}g)"
                        })
                        anomalie.append(f"Claude OAuth SCADUTO da {abs(giorni)}g ({dt_ref.strftime('%d/%m')})")
                    else:
                        st = "WARN" if giorni <= 7 else "OK"
                        badge = "🟡" if st == "WARN" else "🟢"
                        voci.append({
                            "tipo": "OAuth",
                            "target": "Claude",
                            "scadenza": dt_ref.strftime("%d/%m"),
                            "giorni": giorni,
                            "stato": st,
                            "label": f"{badge} Claude OAuth ({giorni}g)"
                        })
                        if st == "WARN":
                            anomalie.append(f"Claude OAuth in scadenza tra {giorni}g ({dt_ref.strftime('%d/%m')})")
        except Exception:
            pass

    # 3. Certificati SSL HTTPS Let's Encrypt
    certs = sorted(glob.glob("/etc/letsencrypt/live/*/cert.pem"))
    for c in certs:
        domain = c.split("/")[-2]
        try:
            cmd = ["openssl", "x509", "-enddate", "-noout", "-in", c]
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
            m = re.search(r"notAfter=(.*)", r.stdout)
            if m:
                dt = datetime.strptime(m.group(1).strip(), "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
                diff = dt - now
                giorni = diff.days
                st = "EXPIRED" if giorni < 0 else ("WARN" if giorni <= 7 else "OK")
                badge = "🟢" if st == "OK" else ("🟡" if st == "WARN" else "🔴")
                voci.append({
                    "tipo": "SSL HTTPS",
                    "target": domain,
                    "scadenza": dt.strftime("%d/%m/%Y"),
                    "giorni": giorni,
                    "stato": st,
                    "label": f"{badge} SSL {domain.split('.')[0]} ({giorni}g)"
                })
                if st == "EXPIRED":
                    anomalie.append(f"SSL {domain} SCADUTO da {abs(giorni)}g ({dt.strftime('%d/%m/%Y')})")
                elif st == "WARN":
                    anomalie.append(f"SSL {domain} in scadenza tra {giorni}g ({dt.strftime('%d/%m/%Y')})")
        except Exception:
            pass

    # Calcolo salute complessiva scadenze
    has_expired = any(v["stato"] == "EXPIRED" for v in voci)
    has_warn = any(v["stato"] == "WARN" for v in voci)
    salute = "CRITICAL" if has_expired else ("DEGRADED" if has_warn else "HEALTHY")

    res = {
        "salute": salute,
        "anomalie": anomalie,
        "voci": voci,
        "totali": len(voci)
    }
    _cache_scadenze["ts"] = now_t
    _cache_scadenze["valore"] = res
    return res

# -----------------------------------------------------------------------------
# AGGREGATORE UNICO STATO ATOMICO GLOBALE
# -----------------------------------------------------------------------------
def aggrega_stato_globale(scrivi_file: bool = True) -> Dict[str, Any]:
    l0 = probe_livello_0_server()
    l1 = probe_livello_1_tmux()
    l2 = probe_livello_2_servizi()
    l3 = probe_livello_3_output_semantico()
    l4 = probe_livello_4_agenti()
    mcp = probe_mcp_servers()
    cron = probe_cron_schedulers()
    nginx = probe_nginx_vhosts()
    spesa_fondi = probe_spesa_e_fondi()
    scadenze = probe_scadenze_vitali()

    all_salutes = [l0["salute"], l1["salute"], l2["salute"], l3["salute"], l4["salute"], mcp["salute"], nginx["salute"], scadenze["salute"]]
    if "CRITICAL" in all_salutes:
        stato_globale = "RED"
    elif "DEGRADED" in all_salutes:
        stato_globale = "YELLOW"
    else:
        stato_globale = "GREEN"

    payload = {
        "ts": get_iso_timestamp(),
        "versione": "2.0-self-healing-gerarchico",
        "stato_globale": stato_globale,
        "livelli": {
            "l0_server": l0,
            "l1_tmux": l1,
            "l2_servizi": l2,
            "l3_output_semantico": l3,
            "l4_agenti": l4
        },
        "estensioni": {
            "mcp": mcp,
            "cron": cron,
            "nginx": nginx,
            "spesa_e_fondi": spesa_fondi,
            "scadenze": scadenze
        }
    }

    if scrivi_file:
        for target_path in [SELF_HEALING_STATE_FILE, SELF_HEALING_STATE_FALLBACK_FILE]:
            tmp_path = f"{target_path}.tmp.{os.getpid()}"
            try:
                os.makedirs(os.path.dirname(target_path), exist_ok=True)
                with open(tmp_path, "w") as f:
                    json.dump(payload, f, indent=2)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp_path, target_path)
            except Exception:
                if os.path.exists(tmp_path):
                    try:
                        os.remove(tmp_path)
                    except Exception:
                        pass

    return payload
