#!/usr/bin/env python3
# =============================================================================
# SELF HEALING CORE — ORCHESTRATORE GERARCHICO DEI SERVIZI DELL'ECOSISTEMA
# Ecosistema Agenti — 29/08/2026
# =============================================================================
import os
import re
import sys
import socket
import json
import time
import signal
import threading
import subprocess
from datetime import datetime, timezone, timedelta
from typing import Dict, Optional, List

from self_healing_config import (
    CORE_DIR, DOCS_DIR, LOGS_DIR, GATE_DIR, TRIGGERS_DIR,
    SELF_HEALING_STATE_FILE, SELF_HEALING_PID_FILE, SELF_HEALING_STOP_FILE,
    AGGREGATED_LOG, ServiceConfig, get_service_registry
)
from self_healing_stato import (
    acquire_supervisor_lock, release_supervisor_lock, is_supervisor_running,
    acquire_service_lock, release_service_lock, aggrega_stato_globale,
    get_iso_timestamp
)

ROME_TZ = timezone(timedelta(hours=2))

class ManagedService:
    def __init__(self, config: ServiceConfig):
        self.config = config
        self.proc: Optional[subprocess.Popen] = None
        self.pgid: Optional[int] = None
        self.status = "STOPPED"  # RUNNING, PERIODIC_WAIT, GATE_HOLD, DOWN, STOPPED, LOCK_WAIT, PROT_WAIT
        self.attempt = 0
        self.restarts = 0
        self.last_exit_code: Optional[int] = None
        self.started_at: Optional[str] = None
        self.stopped_at: Optional[str] = None
        self.lock_acquired = False
        self.thread: Optional[threading.Thread] = None

class SelfHealingCore:
    def __init__(self, services_filter: Optional[List[str]] = None):
        self.registry = get_service_registry()
        self.running = False
        self.services_filter = services_filter
        self.services: Dict[str, ManagedService] = {}
        self.log_file = None
        self.log_lock = threading.Lock()
        self._sync_lock = threading.Lock()
        self._last_audit_ts = 0.0
        # Fix 29/08/2026 (mandato samantha_2, report diagnosi radio, §6 Fix C):
        # dedup per FIRMA (insieme di slug+motivo_blocco bloccati) con cooldown
        # 900s, stessa disciplina di guardiano.py (il disastro del 21/08 con 42
        # alert identici allo stesso destinatario) — un canale che urla viene
        # silenziato dagli umani, ed e' peggio di prima.
        self._l4_alert_last: Dict[str, float] = {}
        self._l4_alert_cooldown_s = 900.0

        # Filtra servizi se richiesto
        for name, cfg in self.registry.items():
            if self.services_filter is None or name in self.services_filter:
                self.services[name] = ManagedService(cfg)

    def _open_log(self):
        os.makedirs(LOGS_DIR, exist_ok=True)
        os.makedirs(DOCS_DIR, exist_ok=True)
        os.makedirs(GATE_DIR, exist_ok=True)
        os.makedirs(TRIGGERS_DIR, exist_ok=True)
        self._rotate_log_if_needed()
        self.log_file = open(AGGREGATED_LOG, "a", buffering=1, encoding="utf-8", errors="replace")

    def _rotate_log_if_needed(self):
        max_bytes = 100 * 1024 * 1024  # 100 MB
        if os.path.exists(AGGREGATED_LOG):
            try:
                if os.path.getsize(AGGREGATED_LOG) > max_bytes:
                    rot_file = f"{AGGREGATED_LOG}.1"
                    if os.path.exists(rot_file):
                        os.remove(rot_file)
                    os.rename(AGGREGATED_LOG, rot_file)
            except Exception:
                pass

    def log_line(self, service_name: str, line: str, level: str = "L2"):
        ts = get_iso_timestamp()
        # Formato gerarchico rigoroso: TIMESTAMP|[LEVEL]|SERVIZIO| RIGA
        formatted = f"{ts}|[{level}]|{service_name}| {line}\n"
        with self.log_lock:
            if self.log_file:
                try:
                    self.log_file.write(formatted)
                    self.log_file.flush()
                except Exception:
                    pass

    def log_event(self, service_name: str, event_type: str, details: str = "", level: str = "L2"):
        line = f"{event_type}|{details}" if details else event_type
        ts = get_iso_timestamp()
        formatted = f"{ts}|[{level}]|{service_name}|{line}\n"
        with self.log_lock:
            if self.log_file:
                try:
                    self.log_file.write(formatted)
                    self.log_file.flush()
                except Exception:
                    pass
            print(formatted.strip())

    def _build_env(self, cfg: ServiceConfig) -> Dict[str, str]:
        env = os.environ.copy()
        if cfg.env_mode == "clean_anthropic":
            for k in ["ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "ANTHROPIC_MODEL"]:
                env.pop(k, None)
            env["HOST_MACHINE"] = "VPS"
        elif cfg.env_mode == "telegram":
            for k in ["ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "ANTHROPIC_MODEL"]:
                env.pop(k, None)
            env["PYTHONUNBUFFERED"] = "1"
        elif cfg.env_mode == "litellm_proxy":
            env["HOST_MACHINE"] = "VPS"
        return env

    def _set_preexec(self, oom_score_adj: Optional[int]):
        def preexec():
            os.setsid()
            if oom_score_adj is not None:
                try:
                    with open("/proc/self/oom_score_adj", "w") as f:
                        f.write(f"{oom_score_adj}\n")
                except Exception:
                    pass
        return preexec

    def _is_gated_ready(self, cfg: ServiceConfig) -> bool:
        if not cfg.gated:
            return True
        if not cfg.gate_file:
            return False
        return os.path.exists(cfg.gate_file)

    def _check_port_collision(self, cfg: ServiceConfig) -> bool:
        if not cfg.ports:
            return False
        for port in cfg.ports:
            try:
                with socket.create_connection(('127.0.0.1', port), timeout=0.3):
                    return True
            except Exception:
                pass
        return False

    def _run_service_loop(self, name: str):
        ms = self.services[name]
        cfg = ms.config

        while self.running and not os.path.exists(SELF_HEALING_STOP_FILE):
            # 1. Check Gating
            if cfg.gated and not self._is_gated_ready(cfg):
                ms.status = "GATE_HOLD"
                self.sync_state()
                while self.running and not os.path.exists(SELF_HEALING_STOP_FILE) and not self._is_gated_ready(cfg):
                    time.sleep(1.0)
                if not self.running or os.path.exists(SELF_HEALING_STOP_FILE):
                    break

            # 2. Check collisione porte
            if self._check_port_collision(cfg):
                ms.status = "PROT_WAIT"
                self.sync_state()
                self.log_event(name, "PORT_COLLISION", f"Porte {cfg.ports} occupate da processo esterno — attendo", level="L2")
                while self.running and not os.path.exists(SELF_HEALING_STOP_FILE) and self._check_port_collision(cfg):
                    time.sleep(2.0)
                if not self.running or os.path.exists(SELF_HEALING_STOP_FILE):
                    break

            # 3. Verifica dipendenze
            if cfg.depends_on:
                deps_ready = True
                for dep in cfg.depends_on:
                    if dep in self.services:
                        dep_ms = self.services[dep]
                        if dep_ms.status not in ("RUNNING", "GATE_HOLD", "PERIODIC_WAIT"):
                            deps_ready = False
                if not deps_ready:
                    self.log_event(name, "DEP_WAIT", f"In attesa delle dipendenze: {cfg.depends_on}", level="L2")
                    time.sleep(3.0)
                    continue

            # 4. Avvio del processo
            ms.attempt += 1
            ms.started_at = get_iso_timestamp()
            ms.status = "RUNNING"
            env = self._build_env(cfg)
            preexec = self._set_preexec(cfg.oom_score_adj)

            self.log_event(name, "START", f"attempt={ms.attempt} restarts={ms.restarts} gated={int(cfg.gated)}", level="L2")
            self.sync_state()

            try:
                ms.proc = subprocess.Popen(
                    cfg.cmd,
                    shell=True,
                    cwd=cfg.cwd,
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    preexec_fn=preexec
                )
                ms.pgid = os.getpgid(ms.proc.pid)
                self.sync_state()

                # Thread trigger riavvio a caldo
                def watch_restart_trigger():
                    restart_file = os.path.join(TRIGGERS_DIR, f"restart_{name}")
                    while ms.proc and ms.proc.poll() is None:
                        if os.path.exists(restart_file):
                            try:
                                os.remove(restart_file)
                            except Exception:
                                pass
                            self.log_event(name, "RESTART_TRIGGER", "Riavvio manuale richiesto via trigger", level="L2")
                            if ms.pgid:
                                try:
                                    os.killpg(ms.pgid, signal.SIGTERM)
                                except Exception:
                                    pass
                            break
                        time.sleep(1.0)

                t_trig = threading.Thread(target=watch_restart_trigger, daemon=True)
                t_trig.start()

                # Lettura streaming output con prefisso gerarchico L2
                for line in iter(ms.proc.stdout.readline, ''):
                    if not line:
                        break
                    self.log_line(name, line.rstrip('\r\n'), level="L2")

                ms.proc.stdout.close()
                rc = ms.proc.wait()
                ms.last_exit_code = rc
                ms.stopped_at = get_iso_timestamp()

            except Exception as e:
                self.log_event(name, "ERROR", f"Eccezione avvio: {e}", level="L2")
                rc = -1
                ms.last_exit_code = rc

            finally:
                release_service_lock(name)
                ms.lock_acquired = False
                ms.proc = None
                ms.pgid = None

            if not self.running or os.path.exists(SELF_HEALING_STOP_FILE):
                ms.status = "STOPPED"
                self.log_event(name, "STOP", "Arresto richiesto", level="L2")
                break

            # 6. Valutazione Uscita
            if cfg.periodic and rc == 0:
                ms.status = "PERIODIC_WAIT"
                self.log_event(name, "CYCLE_OK", f"Ciclo completato regolarmente (exit 0) — attesa {cfg.periodic_interval}s", level="L2")
                self.sync_state()
                time.sleep(cfg.periodic_interval or cfg.restart_delay)
            elif cfg.gated and not self._is_gated_ready(cfg):
                ms.status = "GATE_HOLD"
                self.log_event(name, "GATE_HOLD", "Gate assente dopo l'uscita — resto in attesa", level="L2")
                self.sync_state()
                time.sleep(cfg.restart_delay)
            else:
                ms.restarts += 1
                ms.status = "DOWN"
                self.log_event(name, "DOWN", f"exit={rc} attempt={ms.attempt} restarts={ms.restarts}", level="L2")
                self.sync_state()
                time.sleep(cfg.restart_delay)

        ms.status = "STOPPED"
        self.sync_state()

    @staticmethod
    def _normalizza_causa_l4(motivo_blocco):
        """Toglie i valori volatili (conteggi, minuti, decimali) da 'motivo_blocco'
        per usarlo nella firma di dedup, conservando il testo della causa cosi'
        da distinguere ancora cause diverse sullo stesso agente (29/08/2026,
        fix dedup alert L4: la firma includeva i numeri e non deduplicava mai)."""
        return re.sub(r"\d+([.,]\d+)?", "", str(motivo_blocco))

    def _invia_alert_l4(self, bloccati):
        """Megafono per L4 (29/08/2026, Fix C): probe_livello_4_agenti() gia'
        calcola correttamente 'bloccato' per un always_on con CLI assente via
        roster Postgres — gli mancava solo un canale verso un umano, restava
        solo in dashboard/log aggregato (conteggio senza nomi). Manda un A2A
        alla CASELLA DEL POOL Samantha (infrastruttura condivisa, non un
        singolo agente: se scrivessi a un'istanza specifica e fosse proprio
        lei quella bloccata, l'avviso finirebbe nella stessa coda ferma che
        sto segnalando)."""
        try:
            firma = "|".join(sorted(
                f"{a.get('slug')}:{self._normalizza_causa_l4(a.get('motivo_blocco'))}"
                for a in bloccati))
            now = time.time()
            ultimo = self._l4_alert_last.get(firma)
            if ultimo and (now - ultimo) < self._l4_alert_cooldown_s:
                return
            self._l4_alert_last[firma] = now
            righe = [f"[A2A_FROM:self_healing_core] [A2A_TYPE:report]",
                     f"⚠️ L4 self-healing: {len(bloccati)} agente/i bloccato/i:"]
            for a in bloccati[:10]:
                righe.append(f"- {a.get('slug')}: {a.get('motivo_blocco')}")
            if len(bloccati) > 10:
                righe.append(f"... e altri {len(bloccati) - 10}")
            testo = "\n".join(righe)
            r = subprocess.run(
                [os.path.join(CORE_DIR, "venv", "bin", "python3"),
                 os.path.join(CORE_DIR, "send_a2a.py"), "samantha", testo],
                cwd=CORE_DIR, capture_output=True, text=True, timeout=60)
            self.log_line("AGENTI", f"alert L4 inviato (firma={firma[:80]}, "
                           f"esito={'ok' if r.returncode == 0 else 'fallito'})",
                           level="L4")
        except Exception as e:
            self.log_line("AGENTI", f"alert L4 fallito: {e}", level="L4")

    def sync_state(self):
        """Aggiorna lo stato globale atomico a 5 livelli e registra l'audit a livelli nel log."""
        with self._sync_lock:
            try:
                # Aggiorna anche supervisore_stato.json per retrocompatibilita'
                s_dict = {}
                for name, ms in self.services.items():
                    s_dict[name] = {
                        "status": ms.status,
                        "pid": ms.proc.pid if ms.proc else None,
                        "attempt": ms.attempt,
                        "restarts": ms.restarts,
                        "started_at": ms.started_at,
                        "stopped_at": ms.stopped_at,
                        "last_exit_code": ms.last_exit_code,
                        "gated": ms.config.gated
                    }
                old_state_file = os.path.join(LOGS_DIR, "supervisore_stato.json")
                tmp_old = f"{old_state_file}.tmp.{os.getpid()}.{threading.get_ident()}"
                try:
                    with open(tmp_old, "w") as f:
                        json.dump({
                            "ts": get_iso_timestamp(),
                            "supervisor_pid": os.getpid(),
                            "services": s_dict
                        }, f, indent=2)
                        f.flush()
                        os.fsync(f.fileno())
                    os.replace(tmp_old, old_state_file)
                except Exception:
                    if os.path.exists(tmp_old):
                        try:
                            os.remove(tmp_old)
                        except Exception:
                            pass

                # Scrive stato gerarchico completo a 5 livelli in /root/ecosistema_agenti/logs/self_healing_stato.json
                state = aggrega_stato_globale()

                # Periodic Audit Log a livelli ogni 60 secondi
                now = time.time()
                if now - self._last_audit_ts >= 60.0:
                    self._last_audit_ts = now
                    l0 = state["livelli"]["l0_server"]
                    l1 = state["livelli"]["l1_tmux"]
                    l3 = state["livelli"]["l3_output_semantico"]
                    l4 = state["livelli"]["l4_agenti"]
                    
                    self.log_line("SERVER", f"Salute={l0['salute']} CPU_Sat={l0['cpu']['saturazione_pct']}% RAM_Libera={l0['ram']['disponibile_mb']}MB Disco={l0['disco']['root_usata_pct']}%", level="L0")
                    self.log_line("TMUX", f"Salute={l1['salute']} Sessioni={l1['sessioni_totali']} DeadPanes={l1['dead_panes']}", level="L1")
                    self.log_line("OUTPUT", f"Salute={l3['salute']} Ledger_Integro={l3['verifiche']['guardiano']['ledger_integro']} TG={l3['verifiche']['telegram_engine']['polling_attivo']} WA={l3['verifiche']['whatsapp']['bridge_online']}", level="L3")
                    self.log_line("AGENTI", f"Salute={l4['salute']} Attivi={l4['attivi']} Bloccati={l4['bloccati']}", level="L4")
                    bloccati = [a for a in l4.get("agenti", []) if a.get("bloccato")]
                    if bloccati:
                        self._invia_alert_l4(bloccati)

            except Exception:
                pass

    def start(self):
        if not acquire_supervisor_lock():
            is_run, pid = is_supervisor_running()
            print(f"❌ Errore: Un'altra istanza di Self Healing è già attiva (PID {pid}).")
            sys.exit(1)

        self.running = True
        self._open_log()
        self.log_event("SELF_HEALING", "START", f"Self Healing avviato con successo (PID {os.getpid()})", level="CORE")

        # Ordina per Tier e dipendenze
        sorted_services = sorted(self.services.keys(), key=lambda s: (self.services[s].config.tier, len(self.services[s].config.depends_on)))

        # Avvio thread per ciascun servizio
        for name in sorted_services:
            ms = self.services[name]
            t = threading.Thread(target=self._run_service_loop, args=(name,), name=f"svc-{name}", daemon=True)
            ms.thread = t
            t.start()
            time.sleep(0.2)

        # Loop Principale di Aggregazione e Autoremediation
        try:
            while self.running and not os.path.exists(SELF_HEALING_STOP_FILE):
                time.sleep(5.0)
                self.sync_state()
        except KeyboardInterrupt:
            self.stop()

    def stop(self):
        self.log_event("SELF_HEALING", "STOP", "Arresto generale del Self Healing e di tutti i servizi", level="CORE")
        self.running = False
        
        # Termina tutti i process group dei servizi
        for name, ms in self.services.items():
            if ms.proc and ms.pgid:
                try:
                    os.killpg(ms.pgid, signal.SIGTERM)
                except Exception:
                    pass

        time.sleep(2.0)

        for name, ms in self.services.items():
            if ms.proc and ms.pgid:
                try:
                    os.killpg(ms.pgid, signal.SIGKILL)
                except Exception:
                    pass

        release_supervisor_lock()
        if os.path.exists(SELF_HEALING_STOP_FILE):
            try:
                os.remove(SELF_HEALING_STOP_FILE)
            except Exception:
                pass

        if self.log_file:
            try:
                self.log_file.close()
            except Exception:
                pass
        print("✅ Self Healing e tutti i servizi terminati regolarmente.")
