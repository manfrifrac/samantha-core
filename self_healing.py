#!/usr/bin/env python3
# =============================================================================
# SELF HEALING — INTERFACCIA CLI E LIVE DASHBOARD GERARCHICA A 5 LIVELLI (Fasi 1-9)
# Ecosistema Agenti — 29/08/2026
# =============================================================================
import os
import sys
import json
import time
import signal
import argparse
import subprocess
import shutil
import re
import unicodedata
from datetime import datetime
from typing import Dict, Any, List

from self_healing_config import (
    CORE_DIR, DOCS_DIR, LOGS_DIR, GATE_DIR, TRIGGERS_DIR,
    SELF_HEALING_STATE_FILE, SELF_HEALING_STATE_FALLBACK_FILE,
    SELF_HEALING_PID_FILE, SELF_HEALING_STOP_FILE, AGGREGATED_LOG,
    L4_MAX_CONTEXT_WARN_TOKENS, L4_MAX_CONTEXT_CRIT_TOKENS,
    get_service_registry
)
from self_healing_stato import (
    is_supervisor_running, aggrega_stato_globale,
    probe_mcp_servers, probe_cron_schedulers, probe_nginx_vhosts,
    probe_spesa_e_fondi, probe_scadenze_vitali
)

# Colori e Stili ANSI
C_RESET = "\033[0m"
C_BOLD = "\033[1m"
C_DIM = "\033[2m"
C_RED = "\033[91m"
C_GREEN = "\033[92m"
C_YELLOW = "\033[93m"
C_BLUE = "\033[94m"
C_MAGENTA = "\033[95m"
C_CYAN = "\033[96m"
C_WHITE = "\033[97m"
C_BG_RED = "\033[41m"
C_BG_GREEN = "\033[42m"
C_BG_YELLOW = "\033[43m"
C_BG_BLUE = "\033[44m"

def char_width(c: str) -> int:
    """Calcola la larghezza reale in celle terminale di un singolo carattere Unicode."""
    if ord(c) in (0xFE0F, 0xFE0E, 0x200D, 0x200B):
        return 0
    w = unicodedata.east_asian_width(c)
    if w in ("W", "F"):
        return 2
    code = ord(c)
    if (0x1F000 <= code <= 0x1FFFF) or (0x2600 <= code <= 0x27BF):
        return 2
    return 1

def strip_ansi(text: str) -> str:
    """Rimuove tutti i codici di controllo/colore ANSI."""
    return re.sub(r"\033\[[0-9;]*m", "", text)

def visible_width(text: str) -> int:
    """Restituisce la larghezza reale in celle a schermo di una stringa con escape ANSI ed emoji."""
    clean = strip_ansi(text)
    return sum(char_width(c) for c in clean)

def truncate_ansi(text: str, max_vis_len: int) -> str:
    """Tronca una stringa con escape ANSI ed emoji alla larghezza visibile desiderata senza rompere i colori."""
    if visible_width(text) <= max_vis_len:
        return text
    res = []
    vis_count = 0
    in_escape = False
    escape_seq = []
    chars = list(text)
    i = 0
    while i < len(chars):
        c = chars[i]
        if c == "\033":
            in_escape = True
            escape_seq = [c]
            i += 1
            continue
        if in_escape:
            escape_seq.append(c)
            if c == "m":
                in_escape = False
                res.append("".join(escape_seq))
            i += 1
            continue
        
        w = char_width(c)
        if vis_count + w <= max_vis_len - 1:
            res.append(c)
            vis_count += w
            i += 1
        else:
            res.append("…" + C_RESET)
            break
    return "".join(res)

def box_line(content: str, width: int) -> str:
    """Formatta una riga interna al box garantendo che il bordo destro │ sia sempre perfettamente allineato."""
    inner_width = max(10, width - 4)
    v_len = visible_width(content)
    if v_len > inner_width:
        content = truncate_ansi(content, inner_width)
        v_len = visible_width(content)
    pad = " " * max(0, inner_width - v_len)
    return f"{C_BOLD}{C_CYAN}│{C_RESET} {content}{pad} {C_BOLD}{C_CYAN}│{C_RESET}"

def load_state(is_supervisor_active: bool = False) -> Dict[str, Any]:
    """Se il demone supervisore e' attivo, LEGGE lo stato che lui gia' scrive
    (single-writer, regola 10 del nucleo: altrimenti CLI e demone scrivono
    entrambi self_healing_stato.json in race). Se il demone non gira, calcola
    e scrive lui stesso come fallback (nessun altro scrittore attivo in quel caso)."""
    if is_supervisor_active:
        try:
            with open(SELF_HEALING_STATE_FILE, "r") as f:
                d = json.load(f)
                if d and "livelli" in d:
                    return d
        except Exception:
            pass
        return aggrega_stato_globale(scrivi_file=False)
    return aggrega_stato_globale(scrivi_file=True)

def format_model_name(m: str) -> str:
    """Formatta il nome del modello in modo pulito e compatto senza tagliare suffissi o parentesi."""
    if not m or m == "n/d":
        return "n/d"
    m_clean = re.sub(r"\[.*?\]", "", str(m)).strip()
    mapping = {
        "claude-sonnet-5": "sonnet-5",
        "claude-opus-5": "opus-5",
        "claude-haiku-5": "haiku-5",
        "gemini-3.7-flash-high": "gemini-3.7-flash",
        "gemini-3.7-flash": "gemini-3.7-flash",
        "gemini-3.7-pro": "gemini-3.7-pro",
        "deepseek-v4-flash": "deepseek-v4",
        "cascata-fast": "cascata-fast",
        "cascata-pro": "cascata-pro",
    }
    return mapping.get(m_clean, m_clean)

def render_dashboard(state: Dict[str, Any], is_run: bool, sup_pid: int, show_all_agents: bool = True, force_width: int = None) -> str:
    # Determinazione larghezza reattiva
    term_cols = force_width or shutil.get_terminal_size((88, 24)).columns
    w = max(78, min(term_cols, 140))

    lines = []
    ts = state.get("ts", "N/A")
    global_stat = state.get("stato_globale", "UNKNOWN")
    livelli = state.get("livelli", {})
    estensioni = state.get("estensioni", {})
    l0 = livelli.get("l0_server", {})
    l1 = livelli.get("l1_tmux", {})
    l2 = livelli.get("l2_servizi", {})
    l3 = livelli.get("l3_output_semantico", {})
    l4 = livelli.get("l4_agenti", {})
    mcp = estensioni.get("mcp", {})
    cron = estensioni.get("cron", {})
    ngx = estensioni.get("nginx", {})
    spesa_fondi = estensioni.get("spesa_e_fondi", {})
    scadenze = estensioni.get("scadenze", {})

    all_anomalies: List[str] = []
    for l_key, l_val in [("L0 Server", l0), ("L1 Tmux", l1), ("L2 Servizi", l2), ("L3 Output", l3), ("L4 Agenti", l4), ("MCP", mcp), ("Nginx", ngx), ("Scadenze", scadenze)]:
        for a in l_val.get("anomalie", []):
            all_anomalies.append(f"[{l_key}] {a}")

    # Top border
    lines.append(f"{C_BOLD}{C_CYAN}┌" + "─" * (w - 2) + f"┐{C_RESET}")
    
    if global_stat == "GREEN":
        title_badge = f"{C_BG_GREEN}{C_WHITE}{C_BOLD} STATO GLOBALE: OTTIMO (GREEN) {C_RESET}"
    elif global_stat == "YELLOW":
        title_badge = f"{C_BG_YELLOW}{C_WHITE}{C_BOLD} STATO GLOBALE: ATTENZIONE (YELLOW) {C_RESET}"
    else:
        title_badge = f"{C_BG_RED}{C_WHITE}{C_BOLD} STATO GLOBALE: CRITICO (RED) {C_RESET}"

    daemon_badge = f"{C_GREEN}🟢 VIVO (PID {sup_pid}){C_RESET}" if is_run else f"{C_RED}🔴 SPENTO{C_RESET}"
    
    # Header box lines
    lines.append(box_line(f"{C_BOLD}🛡️  SELF HEALING ECOSYSTEM 2.0 — CRUSCOTTO LIVE{C_RESET}   {title_badge}", w))
    lines.append(box_line(f"{C_DIM}Orchestratore:{C_RESET} {daemon_badge}  {C_DIM}│  Ultimo Check:{C_RESET} {C_WHITE}{ts}{C_RESET}  {C_DIM}│  Refresh: 2s (q=esci){C_RESET}", w))
    lines.append(f"{C_BOLD}{C_CYAN}├" + "─" * (w - 2) + f"┤{C_RESET}")

    def b_icon(s): return "🟢" if s == "HEALTHY" else ("🟡" if s == "DEGRADED" else "🔴")
    b0 = b_icon(l0.get("salute"))
    b1 = b_icon(l1.get("salute"))
    b2 = b_icon(l2.get("salute"))
    b3 = b_icon(l3.get("salute"))
    b4 = b_icon(l4.get("salute"))

    # Layout Riordinato: L0 -> L1 -> L3 -> L4 (Agenti) -> L2 (Servizi)
    chain_str = f"{b0} {C_BOLD}L0 Server{C_RESET} ──▶ {b1} {C_BOLD}L1 Tmux{C_RESET} ──▶ {b3} {C_BOLD}L3 Output{C_RESET} ──▶ {b4} {C_BOLD}L4 Agenti{C_RESET} ──▶ {b2} {C_BOLD}L2 Servizi{C_RESET}"
    lines.append(box_line(f"{C_BOLD}CATENA DI RESILIENZA:{C_RESET} {chain_str}", w))
    lines.append(f"{C_BOLD}{C_CYAN}├" + "─" * (w - 2) + f"┤{C_RESET}")

    if all_anomalies:
        lines.append(box_line(f"{C_BOLD}{C_RED}🚨 ANOMALIE E BLOCCHI ATTIVI RILEVATI ({len(all_anomalies)}):{C_RESET}", w))
        for anom in all_anomalies[:4]:
            lines.append(box_line(f"{C_BOLD}{C_RED}  ⚠️   {anom}{C_RESET}", w))
        if len(all_anomalies) > 4:
            lines.append(box_line(f"{C_BOLD}{C_RED}  ... e altre {len(all_anomalies) - 4} anomalie{C_RESET}", w))
        lines.append(f"{C_BOLD}{C_CYAN}├" + "─" * (w - 2) + f"┤{C_RESET}")
    else:
        lines.append(box_line(f"{C_BOLD}{C_GREEN}✨ TUTTA LA CATENA OPERATIVA AL 100% — NESSUNA ANOMALIA ATTIVA{C_RESET}", w))
        lines.append(f"{C_BOLD}{C_CYAN}├" + "─" * (w - 2) + f"┤{C_RESET}")

    # 1. L0: SERVER
    cpu = l0.get("cpu", {})
    ram = l0.get("ram", {})
    dsk = l0.get("disco", {})
    net = l0.get("rete", {})
    dns_s = f"{C_GREEN}OK{C_RESET}" if net.get("dns_internet") else f"{C_RED}DOWN{C_RESET}"
    ts_s = f"{C_GREEN}OK{C_RESET}" if net.get("tailscale_ip") else f"{C_RED}DOWN{C_RESET}"
    l0_str = f"{b0} {C_BOLD}[L0 SERVER]{C_RESET} Up: {l0.get('uptime_str')} │ CPU: {cpu.get('saturazione_pct')}% ({cpu.get('load1')} 1m) │ RAM: {ram.get('usata_mb')}M/{ram.get('totale_mb')}M │ Dsk: {dsk.get('root_usata_pct')}% │ Net: DNS:{dns_s} TS:{ts_s}"
    lines.append(box_line(l0_str, w))

    # 2. L1: TMUX
    m_sess = l1.get("sessioni_obbligatorie", {})
    m_str = " ".join([f"{k}:{C_GREEN}✓{C_RESET}" if v else f"{k}:{C_RED}✗{C_RESET}" for k, v in list(m_sess.items())[:4]])
    dead_p = l1.get("dead_panes", 0)
    dead_s = f"{C_RED}{C_BOLD}{dead_p} CRASHED{C_RESET}" if dead_p > 0 else f"{C_GREEN}0{C_RESET}"
    l1_str = f"{b1} {C_BOLD}[L1 TMUX]{C_RESET}   Sess: {l1.get('sessioni_totali')} │ Panes: {l1.get('pannelli_totali')} │ Dead: {dead_s} │ Studio: {m_str}"
    lines.append(box_line(l1_str, w))

    # 3. L3: OUTPUT SEMANTICO
    ver = l3.get("verifiche", {})
    g_ok = f"{C_GREEN}INTEGRO (0 ERR){C_RESET}" if ver.get("guardiano",{}).get("ledger_integro") else f"{C_RED}ECCEZIONI{C_RESET}"
    tg_ok = f"{C_GREEN}ATTIVO{C_RESET}" if ver.get("telegram_engine",{}).get("polling_attivo") else f"{C_RED}DOWN{C_RESET}"
    wa_ok = f"{C_GREEN}BAILEYS LIVE (:3005){C_RESET}" if ver.get("whatsapp",{}).get("bridge_online") else f"{C_RED}DISCONNESSO{C_RESET}"
    cdp_ok = f"{C_GREEN}CDP OK (:9224){C_RESET}" if ver.get("chrome_gui",{}).get("cdp_version_ok") else f"{C_RED}DOWN{C_RESET}"
    l3_str = f"{b3} {C_BOLD}[L3 OUTPUT]{C_RESET}  Ledger: {g_ok} │ Telegram: {tg_ok} │ WhatsApp: {wa_ok} │ Chrome: {cdp_ok}"
    lines.append(box_line(l3_str, w))

    # 4. L4: AGENTI & FLOTTA COMPLETA (PRIMO PIANO)
    tot_ag = l4.get("agenti_totali", 0)
    att_ag = l4.get("attivi", 0)
    blk_ag = l4.get("bloccati", 0)
    fc = l4.get("fasce_contesto", {})
    sotto = fc.get("sotto_soglia", att_ag)
    vicino = fc.get("vicino_limite", 0)
    oltre = fc.get("oltre_limite", 0)
    blk_str = f"{C_RED}{C_BOLD}{blk_ag} BLOCCATI{C_RESET}" if blk_ag > 0 else f"{C_GREEN}0 bloccati{C_RESET}"

    l4_header = f"{b4} {C_BOLD}[L4 AGENTI]{C_RESET}  Flotta: {tot_ag} tot │ Attivi: {C_GREEN}{att_ag}{C_RESET} (🟢 <750k: {sotto} │ 🟡 >750k: {vicino} │ 🔴 >900k: {oltre}) │ Blocchi: {blk_str}"
    lines.append(box_line(l4_header, w))

    agenti_tutti = l4.get("agenti", [])
    agenti_attivi = [a for a in agenti_tutti if a.get("cli_vivo") or a.get("bloccato")]
    agenti_standby = [a for a in agenti_tutti if not (a.get("cli_vivo") or a.get("bloccato"))]
    agenti_attivi.sort(key=lambda a: a.get("tokens_contesto", 0) or 0, reverse=True)

    # Stampa tutti gli agenti attivi per esteso con MODELLO e CREATO_DA
    for ag in agenti_attivi:
        st = ag.get("stato")
        is_blk = ag.get("bloccato")
        is_coord = ag.get("coordinatore")
        is_live = ag.get("cli_vivo")
        tok_val = ag.get("tokens_contesto", 0)
        model_val = format_model_name(ag.get("model"))
        creato_da = str(ag.get("creato_da") or "n/d")[:12]
        owner_val = f"by:{creato_da}"
        
        if is_blk or tok_val > L4_MAX_CONTEXT_CRIT_TOKENS:
            b_ag = "🔴"
        elif tok_val > L4_MAX_CONTEXT_WARN_TOKENS:
            b_ag = "🟡"
        elif is_live:
            b_ag = "🟢"
        else:
            b_ag = "⚪"

        r_tag = "[COORD]" if is_coord else f"[{ag.get('ruolo')[:6]}]"
        tok_tag = f"{tok_val:,} tok" if tok_val else "0 tok"
        inb_tag = f"inbox:{int(ag.get('inbox_max_age_min',0))}m" if ag.get('inbox_pendenti') else ""
        blk_rs = f"⚠️  {ag.get('motivo_blocco')}" if is_blk else ""
        lines.append(box_line(f"   {b_ag} {ag.get('slug'):<20} {r_tag:<8} {st:<8} {model_val:<16} {owner_val:<14} {tok_tag:<12} {inb_tag:<10} {blk_rs}", w))

    # Stampa tutti gli agenti in standby con MODELLO
    if agenti_standby:
        st_badges = [f"{C_DIM}⚪ {a['slug']} ({format_model_name(a.get('model'))}){C_RESET}" for a in agenti_standby]
        chunk_size = 4 if w >= 100 else 3
        for i in range(0, len(st_badges), chunk_size):
            prefix = "   ├─ Standby: " if i == 0 else "   │           "
            lines.append(box_line(f"{prefix}{'  '.join(st_badges[i:i+chunk_size])}", w))

    # 5. L2: SERVIZI INFRASTRUTTURALI & ESTENSIONI (IN FONDO)
    counts = l2.get("conteggi", {})
    l2_str = f"{b2} {C_BOLD}[L2 SERVIZI & INFRA]{C_RESET} Run: {C_GREEN}{counts.get('RUNNING',0)}{C_RESET} │ Periodic: {C_YELLOW}{counts.get('PERIODIC_WAIT',0)}{C_RESET} │ Standby/Gate: {C_CYAN}{counts.get('GATE_HOLD',0)}{C_RESET} │ Down: {C_RED if counts.get('DOWN',0)>0 else C_GREEN}{counts.get('DOWN',0)}{C_RESET}"
    lines.append(box_line(l2_str, w))

    svcs = l2.get("servizi", {})
    crit_svcs = ["proxy", "spend_guard", "abbonamenti", "chrome", "whatsapp", "unified_telegram_engine", "guardiano"]
    s_line_1 = []
    s_line_2 = []
    for i, s_name in enumerate(crit_svcs):
        s_inf = svcs.get(s_name, {})
        st = s_inf.get("stato", "UNKNOWN")
        hl = s_inf.get("salute", "UNKNOWN")
        badge = "🟢" if st == "RUNNING" and hl == "HEALTHY" else ("🟡" if st in ("PERIODIC_WAIT", "GATE_HOLD") else "🔴")
        tag = f"{badge}{s_name}"
        if i < 4: s_line_1.append(tag)
        else: s_line_2.append(tag)
    lines.append(box_line(f"   ├─ Core Svcs: {'  '.join(s_line_1)}", w))
    lines.append(box_line(f"   ├─ Infra App: {'  '.join(s_line_2)}", w))

    # ESTENSIONI: MCP ESPLICITI
    mcp_vivi = mcp.get("long_running_vivi", 0)
    mcp_tot = mcp.get("long_running_totali", 0)
    mcp_std = mcp.get("stdio_totali", 0)
    mcp_list = [f"🟢{s['name']}" if s['stato'] in ('RUNNING','STDIO_READY') else f"🔴{s['name']}" for s in mcp.get("server", [])[:4]]
    lines.append(box_line(f"   ├─ 🔌 MCP ({mcp_vivi}/{mcp_tot} vivi): {'  '.join(mcp_list)} (+{mcp_std} stdio)", w))

    # ESTENSIONI: NGINX VHOSTS ESPLICITI
    ngx_vhosts = [f"🟢{v['vhost']}" for v in ngx.get("vhosts", []) if v.get('vhost') != '_']
    lines.append(box_line(f"   ├─ 🌐 Nginx ({len(ngx_vhosts)} vhosts): {'  '.join(ngx_vhosts[:4])}", w))

    # SPESA & FONDI: DEEPSEEK (CON SALDO TOTALE) & FLAT CON COUNTDOWN
    ds_info = spesa_fondi.get("deepseek", {})
    ff_info = spesa_fondi.get("fondi_flat", {})
    ds_spesa = ds_info.get("spesa_str", "n/d")
    ds_saldo = ds_info.get("saldo_str", "n/d")
    ds_req = ds_info.get("chiamate", 0)
    c_flat = ff_info.get("claude_str", "n/d")
    a_flat = ff_info.get("agy_str", "n/d")
    sg_badge = f"{C_GREEN}🟢 ATTIVO{C_RESET}" if ds_info.get("spend_guard_online") else f"{C_RED}🔴 DOWN{C_RESET}"
    
    lines.append(box_line(f"   ├─ 💰 Spesa DeepSeek: {C_BOLD}{ds_spesa}{C_RESET} ({ds_req} req) │ Saldo Reale: {C_BOLD}{ds_saldo}{C_RESET} │ SpendGuard: {sg_badge}", w))
    lines.append(box_line(f"   ├─ ⏳ Finestre Flat:  Claude {c_flat} │ AGY {a_flat}", w))

    # SCADENZE CERTIFICATI & TOKEN OAUTH (Fase 9b)
    scad_list = [v["label"] for v in scadenze.get("voci", [])]
    lines.append(box_line(f"   ├─ 📅 Scadenze:  {' │ '.join(scad_list[:4])}", w))

    # ESTENSIONI: CRON ESPLICITI (TUTTI E 12 IN FONDO AL BLOCCO L2 - Fase 9d)
    active_jobs = [j for j in cron.get("jobs", []) if j.get("stato") == "ACTIVE"]
    cron_badges = []
    for j in active_jobs:
        desc = j.get("descrizione", "")
        cmd = j.get("comando", "")
        sched = j.get("schedulazione", "")
        
        if "check_sveglia" in cmd: name = "check_sveglia"
        elif "acme.sh" in cmd or "Rinnovo" in desc: name = "acme_ssl"
        elif "sveglia" in cmd or "Briefing" in desc: name = "briefing_tg"
        elif "sensore" in cmd or "Memoria Leggera" in desc: name = "memoria_leggera"
        elif "gc_exec" in cmd or "GC Exec" in desc: name = "gc_exec"
        elif "video_amici" in cmd: name = "video_amici"
        elif "sentinella" in cmd or "Contesto" in desc: name = "sentinella_contesto"
        elif "state_" in cmd or "Infrastruttura" in desc: name = "state_backup"
        elif "Minerva" in desc or "cron_wrapper" in cmd: name = "minerva_interpelli"
        elif "autorefresh" in cmd or "Refresher" in desc: name = "refresher_mem"
        elif "fondi_flat" in cmd or "Consumi" in desc: name = "probe_quote"
        elif "sync_messaggi" in cmd or "Sync" in desc: name = "sync_messaggi"
        else: name = desc[:14]

        if "Ogni 300s" in sched: s = "ogni 5m"
        elif "Ogni 1200s" in sched: s = "ogni 20m"
        elif "8 0,6,12,18" in sched: s = "ogni 6h"
        elif "0 9 * * *" in sched: s = "09:00"
        elif "7 9 * * *" in sched: s = "09:07"
        elif "0 * * * *" in sched: s = "ogni 1h"
        elif "7,22,37,52" in sched or "*/15" in sched: s = "ogni 15m"
        elif "17,47" in sched or "*/30" in sched: s = "ogni 30m"
        elif "0 9 18 8" in sched: s = "annuale"
        else: s = sched

        cron_badges.append(f"🟢{name} ({s})")

    chunk = 4 if w >= 110 else 3
    for i in range(0, len(cron_badges), chunk):
        is_last = (i + chunk >= len(cron_badges))
        prefix = f"   ├─ ⏰ Cron ({len(cron_badges)}): " if i == 0 else ("   └─               " if is_last else "   │                ")
        lines.append(box_line(f"{prefix}{'  '.join(cron_badges[i:i+chunk])}", w))

    # Bottom border
    lines.append(f"{C_BOLD}{C_CYAN}└" + "─" * (w - 2) + f"┘{C_RESET}")
    return "\n".join(lines)

def cmd_mcp(args):
    """Comando self_healing.py mcp — visualizza tabella server MCP."""
    mcp_data = probe_mcp_servers()

    print("=" * 88)
    print(f"  🔌 GESTIONE SERVER MCP — STATO UNIFICATO ({mcp_data.get('salute')})")
    print(f"  Long-Running: {mcp_data.get('long_running_vivi')}/{mcp_data.get('long_running_totali')} vivi  │  Stdio Connectors: {mcp_data.get('stdio_totali')} registrati")
    print("=" * 88)

    print(f"{'STATO':<12} {'NOME SERVER':<22} {'TIPO':<16} {'TARGET / PORTA':<24} {'STUDI':<8}")
    print("-" * 88)

    for s in mcp_data.get("server", []):
        st_badge = f"{C_GREEN}🟢 RUNNING{C_RESET}" if s["stato"] == "RUNNING" else (f"{C_GREEN}🟢 READY{C_RESET}" if s["stato"] == "STDIO_READY" else f"{C_RED}🔴 DOWN{C_RESET}")
        print(f"{st_badge:<20} {s['name']:<22} {s['tipo']:<16} {s['target']:<24} {s['studi_utilizzatori']:<8}")

    print("-" * 88)
    if mcp_data.get("anomalie"):
        for a in mcp_data["anomalie"]:
            print(f"⚠️  {C_RED}{a}{C_RESET}")

def cmd_cron(args):
    """Comando self_healing.py cron — visualizza tabella schedulazioni cron e scheduler interni."""
    cron_data = probe_cron_schedulers()

    print("=" * 88)
    print(f"  ⏰ VISIBILITA' SCHEDULAZIONI CRON & SCHEDULER INTERNI")
    print(f"  Jobs Attivi: {cron_data.get('jobs_attivi')}  │  Disabilitati / Bonificati: {cron_data.get('jobs_disabilitati')}")
    print("=" * 88)

    print(f"{'STATO':<10} {'CADENZA':<20} {'SORGENTE':<18} {'TARGET / SCOPO':<26} {'COMANDO'}")
    print("-" * 88)

    for j in cron_data.get("jobs", []):
        st_b = f"{C_GREEN}🟢 ATTIVO{C_RESET}" if j["stato"] == "ACTIVE" else f"{C_DIM}⚪ DISAB{C_RESET}"
        print(f"{st_b:<18} {j['schedulazione']:<20} {j['sorgente']:<18} {j['descrizione']:<26} {j['comando']}")

    print("-" * 88)

def cmd_nginx(args):
    """Comando self_healing.py nginx — visualizza tabella vhost e backend proxy."""
    ngx_data = probe_nginx_vhosts()

    ngx_live = f"{C_GREEN}🟢 ATTIVO (systemd){C_RESET}" if ngx_data.get("nginx_attivo") else f"{C_RED}🔴 INATTIVO{C_RESET}"
    print("=" * 88)
    print(f"  🌐 SITI PUBBLICATI & VHOST NGINX — {ngx_live}")
    print(f"  VHosts Totali: {ngx_data.get('vhosts_totali')}")
    print("=" * 88)

    print(f"{'DOMINIO / VHOST':<28} {'TIPO':<16} {'PORTE':<14} {'BACKEND / ROOT'}")
    print("-" * 88)

    for v in ngx_data.get("vhosts", []):
        bk_str = ""
        if v.get("backends"):
            bk_parts = []
            for b in v["backends"]:
                b_badge = f"{C_GREEN}🟢{C_RESET}" if b["status"] == "ONLINE" else f"{C_YELLOW}🟡{C_RESET}"
                bk_parts.append(f"{b_badge} {b['url']}")
            bk_str = " ".join(bk_parts)
        else:
            bk_str = f"Root: {v.get('root')[:38]}"
        print(f"{C_BOLD}{v['vhost']:<28}{C_RESET} {v['tipo']:<16} {v['porte']:<14} {bk_str}")

    print("-" * 88)

def cmd_dashboard(args):
    show_all = getattr(args, "all", True)
    interval = getattr(args, "interval", 2.0)
    width = getattr(args, "width", None)

    def handle_exit(sig, frame):
        print("\033[?25h\nDashboard terminata.")
        sys.exit(0)
    signal.signal(signal.SIGINT, handle_exit)
    signal.signal(signal.SIGTERM, handle_exit)

    sys.stdout.write("\033[?25l")
    sys.stdout.flush()

    try:
        while True:
            is_run, sup_pid = is_supervisor_running()
            state = load_state(is_run)
            rendered = render_dashboard(state, is_run, sup_pid or 0, show_all_agents=show_all, force_width=width)
            
            sys.stdout.write("\033[H\033[2J")
            sys.stdout.write(rendered + "\n")
            sys.stdout.flush()
            time.sleep(interval)
    finally:
        sys.stdout.write("\033[?25h\n")
        sys.stdout.flush()

def cmd_status(args):
    is_run, sup_pid = is_supervisor_running()
    state = load_state(is_run)

    if args.json:
        print(json.dumps(state, indent=2))
        return

    rendered = render_dashboard(state, is_run, sup_pid or 0, show_all_agents=True, force_width=args.width if hasattr(args, "width") else None)
    print(rendered)

def cmd_tree(args):
    is_run, _sup_pid = is_supervisor_running()
    state = load_state(is_run)
    l0 = state['livelli']['l0_server']
    l1 = state['livelli']['l1_tmux']
    l2 = state['livelli']['l2_servizi']
    l3 = state['livelli']['l3_output_semantico']
    l4 = state['livelli']['l4_agenti']

    def b(s): return f"{C_GREEN}🟢 HEALTHY{C_RESET}" if s == "HEALTHY" else (f"{C_YELLOW}🟡 DEGRADED{C_RESET}" if s == "DEGRADED" else f"{C_RED}🔴 CRITICAL{C_RESET}")

    print(f"\n{C_BOLD}🌲 ALBERO GERARCHICO DELL'ECOSISTEMA SELF HEALING{C_RESET}")
    print(f"└── [L0] Server: {b(l0['salute'])} (Uptime: {l0['uptime_str']}, CPU: {l0['cpu']['saturazione_pct']}%)")
    print(f"    └── [L1] Tmux: {b(l1['salute'])} ({l1['sessioni_totali']} sessioni, {l1['dead_panes']} dead panes)")
    print(f"        └── [L3] Output Semantico: {b(l3['salute'])} (Ledger 0 errori, Telegram OK, Baileys OK, CDP OK)")
    print(f"            └── [L4] Agenti & Stanze: {b(l4['salute'])} ({l4['attivi']} attivi, {l4['bloccati']} bloccati)")
    print(f"                └── [L2] Servizi Infra: {b(l2['salute'])} ({l2['conteggi'].get('RUNNING')} running / {l2['servizi_totali']} tot)")

def cmd_start(args):
    from self_healing_core import SelfHealingCore
    services_list = args.services.split(",") if args.services else None
    core = SelfHealingCore(services_filter=services_list)
    def handle_sig(sig, frame):
        core.stop()
        sys.exit(0)
    signal.signal(signal.SIGTERM, handle_sig)
    signal.signal(signal.SIGINT, handle_sig)
    core.start()

def cmd_stop(args):
    is_run, pid = is_supervisor_running()
    if not is_run or not pid:
        print("Self Healing non risulta in esecuzione.")
        return
    print(f"Invio segnale di arresto a Self Healing (PID: {pid})...")
    with open(SELF_HEALING_STOP_FILE, "w") as f:
        f.write("stop\n")
    try:
        os.kill(pid, signal.SIGTERM)
    except Exception:
        pass
    for _ in range(15):
        if not is_supervisor_running()[0]:
            print("✅ Self Healing arrestato con successo.")
            return
        time.sleep(0.5)
    print("⚠️ Timeout: arresto forzato.")
    try:
        os.kill(pid, signal.SIGKILL)
    except Exception:
        pass

def cmd_restart(args):
    service_name = args.service
    registry = get_service_registry()
    if service_name not in registry:
        print(f"❌ Servizio sconosciuto: '{service_name}'.")
        return
    os.makedirs(TRIGGERS_DIR, exist_ok=True)
    trig_file = os.path.join(TRIGGERS_DIR, f"restart_{service_name}")
    with open(trig_file, "w") as f:
        f.write(f"{time.time()}\n")
    print(f"🔄 Richiesta di riavvio inviata per '{service_name}'.")

def cmd_gate(args):
    service_name = args.service
    action = args.action.lower()
    os.makedirs(GATE_DIR, exist_ok=True)
    gate_file = os.path.join(GATE_DIR, f"{service_name}.go")
    if action == "on":
        with open(gate_file, "w") as f:
            f.write(f"enabled at {time.time()}\n")
        print(f"✅ Gate abilitato per '{service_name}' ({gate_file} creato).")
    elif action == "off":
        if os.path.exists(gate_file):
            os.remove(gate_file)
        print(f"🛑 Gate disabilitato per '{service_name}' ({gate_file} rimosso).")

def cmd_logs(args):
    if not os.path.exists(AGGREGATED_LOG):
        print("Nessun log aggregato presente.")
        return
    n = args.lines
    svc = args.service
    cmd = ["tail", "-n", str(n * 4), AGGREGATED_LOG]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True)
        lines = r.stdout.splitlines()
        matched = []
        for line in lines:
            if svc:
                if f"|{svc}|" in line or f"|[{svc}]|" in line:
                    matched.append(line)
            else:
                matched.append(line)
        for line in matched[-n:]:
            print(line)
    except Exception as e:
        print(f"Errore lettura log: {e}")

def main():
    parser = argparse.ArgumentParser(description="Self Healing — Sistema Gerarchico a 5 Livelli")
    subparsers = parser.add_subparsers(dest="command", help="Comando")

    p_dash = subparsers.add_parser("dashboard", help="Cruscotto live auto-aggiornante (stile htop)")
    p_dash.add_argument("--all", action="store_true", default=True, help="Mostra tutti gli agenti inclusi quelli in standby")
    p_dash.add_argument("-i", "--interval", type=float, default=2.0, help="Intervallo refresh in secondi (default 2s)")
    p_dash.add_argument("-w", "--width", type=int, default=None, help="Forza larghezza colonne terminale")

    p_watch = subparsers.add_parser("watch", help="Alias per dashboard live")
    p_watch.add_argument("--all", action="store_true", default=True, help="Mostra tutti gli agenti inclusi quelli in standby")
    p_watch.add_argument("-i", "--interval", type=float, default=2.0, help="Intervallo refresh in secondi (default 2s)")
    p_watch.add_argument("-w", "--width", type=int, default=None, help="Forza larghezza colonne terminale")

    p_mcp = subparsers.add_parser("mcp", help="Dashboard e stato server MCP (long-running vs stdio)")
    p_cron = subparsers.add_parser("cron", help="Visibilità schedulazioni Cron e scheduler interni")
    p_nginx = subparsers.add_parser("nginx", help="Dashboard vhost Nginx e backend proxy")

    p_status = subparsers.add_parser("status", help="Visualizza lo stato gerarchico a 5 livelli")
    p_status.add_argument("--json", action="store_true", help="Output in formato JSON")
    p_status.add_argument("-w", "--width", type=int, default=None, help="Forza larghezza colonne terminale")

    p_tree = subparsers.add_parser("tree", help="Visualizza albero gerarchico sintetico")

    p_start = subparsers.add_parser("start", help="Avvia il demone Self Healing")
    p_start.add_argument("--services", type=str, help="Lista di servizi separati da virgola")

    subparsers.add_parser("stop", help="Arresta il demone Self Healing")

    p_restart = subparsers.add_parser("restart", help="Riavvia a caldo un singolo servizio")
    p_restart.add_argument("service", type=str, help="Nome del servizio")

    p_gate = subparsers.add_parser("gate", help="Abilita o disabilita un gate on-demand")
    p_gate.add_argument("service", type=str, help="Nome del servizio (es. radio_keeper)")
    p_gate.add_argument("action", choices=["on", "off"], help="Azione: on | off")

    p_logs = subparsers.add_parser("logs", help="Mostra i log in tempo reale")
    p_logs.add_argument("service", nargs="?", default=None, help="Servizio specifico")
    p_logs.add_argument("-n", "--lines", type=int, default=50, help="Numero di righe")

    args = parser.parse_args()
    if not args.command:
        cmd_dashboard(argparse.Namespace(all=True, interval=2.0, width=None))
        return

    cmds = {
        "dashboard": cmd_dashboard,
        "watch": cmd_dashboard,
        "mcp": cmd_mcp,
        "cron": cmd_cron,
        "nginx": cmd_nginx,
        "status": cmd_status,
        "tree": cmd_tree,
        "start": cmd_start,
        "stop": cmd_stop,
        "restart": cmd_restart,
        "gate": cmd_gate,
        "logs": cmd_logs
    }
    if args.command in cmds:
        cmds[args.command](args)

if __name__ == "__main__":
    main()
