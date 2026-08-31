#!/usr/bin/env python3
# =============================================================================
# SELF HEALING — MODULO DI CONFIGURAZIONE UNIFICATA (Livelli 0-4)
# Ecosistema Agenti — 29/08/2026
# =============================================================================
import os
from dataclasses import dataclass, field
from typing import List, Dict, Optional

CORE_DIR = "/root/ecosistema_agenti/core"
DOCS_DIR = "/tmp/betty_docs"
LOGS_DIR = "/root/ecosistema_agenti/logs"
GATE_DIR = "/tmp/betty_docs/sv2_gate"
TRIGGERS_DIR = "/tmp/betty_docs/sv2_triggers"

# File di stato atomico unico (salvato in logs/ per sopravvivere ai reboot del server)
SELF_HEALING_STATE_FILE = os.path.join(LOGS_DIR, "self_healing_stato.json")
SELF_HEALING_STATE_FALLBACK_FILE = os.path.join(DOCS_DIR, "self_healing_stato.json")
SELF_HEALING_PID_FILE = "/tmp/ecosistema_self_healing.pid"
SELF_HEALING_STOP_FILE = "/tmp/ecosistema_self_healing.stop"
AGGREGATED_LOG = os.path.join(LOGS_DIR, "ecosistema_aggregato.log")
GUARDIANO_LEDGER_FILE = os.path.join(LOGS_DIR, "guardiano_ledger.jsonl")
GUARDIANO_STATO_FILE = os.path.join(DOCS_DIR, "guardiano_stato.json")

# -----------------------------------------------------------------------------
# LIVELLO 0 — SOGLIE SERVER & HARDWARE
# -----------------------------------------------------------------------------
L0_MAX_CPU_LOAD5 = 14.0       # Alert se load a 5 min > 14.0 (su 8 core)
L0_MAX_CPU_SAT_PCT = 85.0     # Alert se saturazione CPU complessiva > 85%
L0_MIN_RAM_FREE_MB = 2048     # Alert se RAM disponibile < 2 GB
L0_MAX_DISK_ROOT_PCT = 90.0   # Alert se disco / > 90%
L0_MAX_DISK_TMP_PCT = 85.0    # Alert se disco /tmp > 85%

# -----------------------------------------------------------------------------
# LIVELLO 1 — SESSIONI TMUX OBBLIGATORIE
# -----------------------------------------------------------------------------
L1_MANDATORY_TMUX_SESSIONS = [
    "services",
    "betty",
    "auruncia_team",
    "ricerca_lavoro",
    "scuola_studio"
]

# -----------------------------------------------------------------------------
# LIVELLO 4 — SOGLIE BLOCCHI AGENTI
# -----------------------------------------------------------------------------
L4_MAX_INBOX_STALL_MIN = 15.0       # Alert se un messaggio A2A è in inbox da > 15m su agente vivo
L4_MAX_CONTEXT_WARN_TOKENS = 750000 # Alert giallo saturazione contesto
L4_MAX_CONTEXT_CRIT_TOKENS = 900000 # Alert rosso saturazione contesto

# -----------------------------------------------------------------------------
# LIVELLO 2 — REGISTRO DEI 18 SERVIZI INFRASTRUTTURALI
# -----------------------------------------------------------------------------
@dataclass
class ServiceConfig:
    name: str
    cmd: str
    cwd: str
    runtime: str = "python"
    env_mode: str = "standard"
    oom_score_adj: Optional[int] = None
    depends_on: List[str] = field(default_factory=list)
    ports: List[int] = field(default_factory=list)
    healthcheck_type: Optional[str] = None
    healthcheck_target: Optional[str] = None
    gated: bool = False
    gate_file: Optional[str] = None
    periodic: bool = False
    periodic_interval: Optional[float] = None
    tier: int = 1
    restart_delay: float = 3.0
    tmux_window: Optional[str] = None
    description: str = ""


def get_service_registry() -> Dict[str, ServiceConfig]:
    registry: Dict[str, ServiceConfig] = {}

    # Tier 1: Ausiliari e Non Critici
    registry["ordini"] = ServiceConfig(
        name="ordini",
        cmd="./venv/bin/python3 -u tampone_ordini_fermi.py",
        cwd=CORE_DIR,
        runtime="python",
        tier=1,
        restart_delay=3.0,
        description="Tampone ordini bloccati in casella"
    )

    registry["quota_vivo"] = ServiceConfig(
        name="quota_vivo",
        cmd="./venv/bin/python3 -u quota_vivo.py",
        cwd=CORE_DIR,
        runtime="python",
        tier=1,
        restart_delay=3.0,
        description="Sensore quote e token in tempo reale"
    )

    registry["agenti_topic"] = ServiceConfig(
        name="agenti_topic",
        cmd="./venv/bin/python3 -u agenti_topic_vivo.py",
        cwd=CORE_DIR,
        runtime="python",
        tier=1,
        restart_delay=5.0,
        description="Sincronizzazione topic e presenze agenti su Telegram"
    )

    registry["radio_debug"] = ServiceConfig(
        name="radio_debug",
        cmd="./venv/bin/python3 -u radio_debug_service.py",
        cwd=CORE_DIR,
        runtime="python",
        gated=True,
        gate_file=os.path.join(GATE_DIR, "radio_debug.go"),
        tier=1,
        restart_delay=3.0,
        description="Debugger Radio (on-demand via sv2_gate/radio_debug.go)"
    )

    # Tier 2: Chrome & Tunnel & Radio
    registry["chrome"] = ServiceConfig(
        name="chrome",
        cmd="./venv/bin/python3 -u chrome_gui_service.py",
        cwd=CORE_DIR,
        runtime="python",
        ports=[9224, 15900],
        healthcheck_type="tcp",
        healthcheck_target="127.0.0.1:9224",
        tier=2,
        restart_delay=5.0,
        description="Chrome Headful GUI, CDP :9224, x11vnc :15900, websockify :6080"
    )

    registry["tunnel"] = ServiceConfig(
        name="tunnel",
        cmd="./venv/bin/python3 -u chrome_tunnel_watchdog.py",
        cwd=CORE_DIR,
        runtime="python",
        ports=[9222],
        healthcheck_type="tcp",
        healthcheck_target="127.0.0.1:9222",
        tier=2,
        restart_delay=3.0,
        description="Watchdog tunnel SSH Chrome Surface :9222"
    )

    registry["chrome_fisso"] = ServiceConfig(
        name="chrome_fisso",
        cmd="./venv/bin/python3 -u chrome_fisso_service.py",
        cwd=CORE_DIR,
        runtime="python",
        ports=[9223],
        healthcheck_type="tcp",
        healthcheck_target="127.0.0.1:9223",
        tier=2,
        restart_delay=5.0,
        description="Chrome Headless Fisso :9223 (supervisore proattivo)"
    )

    registry["radio_keeper"] = ServiceConfig(
        name="radio_keeper",
        cmd="./venv/bin/python3 -u radio_keeper.py",
        cwd=CORE_DIR,
        runtime="python",
        gated=True,
        gate_file=os.path.join(GATE_DIR, "radio_keeper.go"),
        depends_on=["chrome"],
        tier=2,
        restart_delay=3.0,
        description="Keeper riproduzione Radio (on-demand via sv2_gate/radio_keeper.go)"
    )

    # Tier 3: Spend Guard, Proxy, Abbonamenti, VPN, Loop Periodici, WhatsApp
    registry["spend_guard"] = ServiceConfig(
        name="spend_guard",
        cmd="./venv/bin/python3 -u deepseek_spend_guard.py",
        cwd=CORE_DIR,
        runtime="python",
        oom_score_adj=-500,
        ports=[8790],
        healthcheck_type="tcp",
        healthcheck_target="127.0.0.1:8790",
        tier=3,
        restart_delay=3.0,
        description="DeepSeek Spend Guard proxy locale (PROTETTO, OOM -500, porta 8790)"
    )

    registry["abbonamenti"] = ServiceConfig(
        name="abbonamenti",
        cmd="bash avvia_adapter_abbonamenti.sh",
        cwd=CORE_DIR,
        runtime="bash",
        env_mode="clean_anthropic",
        ports=[4001, 4002],
        healthcheck_type="tcp",
        healthcheck_target="127.0.0.1:4001",
        tier=3,
        restart_delay=3.0,
        description="Adapter CLI abbonamenti Claude & Antigravity (porte 4001, 4002)"
    )

    registry["proxy"] = ServiceConfig(
        name="proxy",
        cmd="bash avvia_litellm_proxy.sh",
        cwd=CORE_DIR,
        runtime="bash",
        env_mode="litellm_proxy",
        oom_score_adj=-500,
        depends_on=["abbonamenti"],
        ports=[4000],
        healthcheck_type="tcp",
        healthcheck_target="127.0.0.1:4000",
        tier=3,
        restart_delay=3.0,
        description="LiteLLM Proxy upstream router (PROTETTO, OOM -500, porta 4000)"
    )

    registry["vpn"] = ServiceConfig(
        name="vpn",
        cmd="venv/bin/python3 -u surfshark_watchdog.py",
        cwd=CORE_DIR,
        runtime="python",
        tier=3,
        restart_delay=3.0,
        description="Watchdog del canale VPN Surfshark"
    )

    registry["autorefresh"] = ServiceConfig(
        name="autorefresh",
        cmd="./venv/bin/python3 -u autorefresh_vivo.py --esegui",
        cwd=CORE_DIR,
        runtime="python",
        periodic=True,
        periodic_interval=300.0,
        restart_delay=300.0,
        depends_on=["quota_vivo"],
        tier=3,
        description="Refresher periodico contesto/token agenti (ogni 300s)"
    )

    registry["fondi_flat"] = ServiceConfig(
        name="fondi_flat",
        cmd="./venv/bin/python3 -u probe_fondi_flat.py",
        cwd=CORE_DIR,
        runtime="python",
        periodic=True,
        periodic_interval=300.0,
        restart_delay=300.0,
        depends_on=["proxy"],
        tier=3,
        description="Probe periodico fondi flat e stato provider (ogni 300s)"
    )

    registry["whatsapp"] = ServiceConfig(
        name="whatsapp",
        cmd="node start_baileys.js",
        cwd="/root/ecosistema_agenti/mcp_servers/whatsapp-mcp",
        runtime="node",
        ports=[3005],
        healthcheck_type="tcp",
        healthcheck_target="127.0.0.1:3005",
        tier=3,
        restart_delay=3.0,
        description="MCP WhatsApp Live Baileys WebSocket daemon"
    )

    registry["sync"] = ServiceConfig(
        name="sync",
        cmd="./venv/bin/python3 -u sync_messaggi.py",
        cwd=CORE_DIR,
        runtime="python",
        periodic=True,
        periodic_interval=1200.0,
        restart_delay=1200.0,
        tier=3,
        description="Sincronizzazione messaggi WhatsApp e Gmail (ogni 1200s)"
    )

    # Tier 4: Servizi Critici dell'Ecosistema
    registry["vigile"] = ServiceConfig(
        name="vigile",
        cmd="./venv/bin/python3 -u vigile_notifiche.py",
        cwd=CORE_DIR,
        runtime="python",
        tier=4,
        restart_delay=3.0,
        description="Vigile notifiche ed inoltri d'urgenza Telegram"
    )

    registry["unified_telegram_engine"] = ServiceConfig(
        name="unified_telegram_engine",
        cmd="./venv/bin/python3 -u unified_telegram_engine.py",
        cwd=CORE_DIR,
        runtime="python",
        env_mode="telegram",
        tier=4,
        restart_delay=3.0,
        description="Motore Telegram Master unificato per tutti i Bot della flotta"
    )

    registry["guardiano"] = ServiceConfig(
        name="guardiano",
        cmd="./venv/bin/python3 -u guardiano.py",
        cwd=CORE_DIR,
        runtime="python",
        oom_score_adj=-500,
        depends_on=["unified_telegram_engine", "proxy"],
        healthcheck_type="heartbeat",
        healthcheck_target="/tmp/betty_docs/guardiano_heartbeat",
        tmux_window="services:SRV-Guardiano",
        tier=4,
        restart_delay=3.0,
        description="Self-Healing Flotta Agenti (PROTETTO, OOM -500, Heartbeat)"
    )

    return registry
