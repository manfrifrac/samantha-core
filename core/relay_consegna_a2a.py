#!/usr/bin/env python3
"""relay_consegna_a2a.py — Ritenta la CONSEGNA dei messaggi A2A non letti
verso agenti VIVI la cui notifica via paste non era stata elaborata
(25/08/2026, MSG 15958, Manfredo).

PROBLEMA: send_a2a notifica via paste nel CLI; se al deposito il destinatario
non era un CLI pronto (shell, turno lungo), il messaggio resta in inbox e
NESSUN meccanismo lo riconsegna. Questo demone scansiona le inbox degli agenti
vivi e invia un PROMEMORIA breve al CLI del destinatario per fargli leggere la
inbox. Dedup su file di stato: non ripete il promemoria per lo stesso agente
prima di COOLDOWN_S, a meno che non arrivino messaggi NUOVI (tramite eta').

Esclusi: le caselle del pool (gestite da vigile_notifiche) e gli agenti senza
finestra CLI pronta. Non incolla il contenuto, solo un promemoria: niente
duplicati nel contesto del destinatario.
"""
import glob
import json
import os
import subprocess
import sys
import time
from datetime import datetime

CORE = os.path.dirname(os.path.abspath(__file__))
A2A_ROOT = os.path.join(os.path.dirname(CORE), "a2a")
sys.path.insert(0, CORE)

LOOP_S = 60                 # giro del supervisore (while nel pannello)
ETA_MIN_SOGLIA_S = 300      # promemoria solo per messaggi piu' vecchi di 5 min
COOLDOWN_S = 1200           # 20 min tra promemoria per lo stesso agente
STATE_FILE = os.path.join(CORE, "relay_consegna_state.json")
POOL_SLUGS = {"samantha", "samantha_1", "samantha_2", "samantha_3", "publio",
              "auruncia_tech", "cosimo", "zero_one"}


def stato():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {"ultimo_promemoria": {}}


def salva_stato(s):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(s, f)
    os.replace(tmp, STATE_FILE)


def finesta_di(slug, db=None):
    """Risolve la finestra tmux dell'agente dal DB agents (passato dal main)."""
    if db is None:
        try:
            import agent_db
            db = agent_db.get_db()
        except Exception:
            return None
    try:
        d = db.get(slug)
        return d.get("tmux_window") if d else None
    except Exception:
        return None


def cli_pronto(finestra):
    """C'e' un CLI vivo nel sottoalbero del pannello? (riusa la logica del
    guardiano: sottoalbero + motore_reale_da_processo). Un CLI Claude/agy gira
    dentro una shell wrapper, quindi pane_current_command='bash' non basta."""
    try:
        out = subprocess.run(["tmux", "list-panes", "-t", finestra, "-F", "#{pane_pid}"],
                             capture_output=True, text=True, timeout=10)
        if out.returncode != 0:
            return False
        pane_pid = out.stdout.strip().splitlines()[0].strip()
        if not pane_pid:
            return False
        import guardiano as _g
        pids = _g.sottoalbero(pane_pid)
        if not pids:
            return False
        _engine, _model, _provider, cli_pid = _g.motore_reale_da_processo(pids)
        return bool(cli_pid)
    except Exception:
        return False


def notifica(finestra, slug, n_msg):
    testo = f"📬 Hai {n_msg} messaggi A2A non letti in inbox. Leggili e processali."
    try:
        subprocess.run(["tmux", "send-keys", "-t", finestra, testo, "C-m"],
                       capture_output=True, timeout=10)
        return True
    except Exception:
        return False


def main():
    s = stato()
    ora = time.time()
    try:
        import agent_db
        db = agent_db.get_db()
    except Exception:
        db = {}
    for inbox in glob.glob(os.path.join(A2A_ROOT, "*", "inbox")):
        slug = os.path.basename(os.path.dirname(inbox))
        if slug in POOL_SLUGS or not slug:
            continue
        msg = glob.glob(os.path.join(inbox, "*.md"))
        if not msg:
            continue
        vecchi = [m for m in msg if ora - os.path.getmtime(m) > ETA_MIN_SOGLIA_S]
        if not vecchi:
            continue
        finestra = finesta_di(slug, db)
        if not finestra or not cli_pronto(finestra):
            continue
        ultimo = s["ultimo_promemoria"].get(slug, 0)
        if ora - ultimo < COOLDOWN_S:
            continue
        if notifica(finestra, slug, len(vecchi)):
            s["ultimo_promemoria"][slug] = ora
            salva_stato(s)
            print(f"{datetime.now().isoformat()} relay: promemoria a {slug} "
                  f"({len(vecchi)} msg in inbox)", flush=True)


if __name__ == "__main__":
    main()
