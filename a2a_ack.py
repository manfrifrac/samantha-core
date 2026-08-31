#!/usr/bin/env python3
"""a2a_ack.py — DEV-110 §3/§5: segna un messaggio come LETTO.

Sposta il file dall'inbox al read/ (rename atomico): LETTO è l'unico dei tre
stati (DEPOSITATO/NOTIFICATO/LETTO) che è una prova di ricezione vera —
verificabile da chiunque, in qualsiasi momento, con un `ls`, senza toccare
tmux e senza fidarsi del terminale.

Uso: a2a_ack.py <A2A_ID>

Non chiede lo slug del destinatario: lo si RITROVA cercando l'ID in tutte le
caselle (a2a_inbox.trova_in_inbox). Dedurlo dalla cartella di lavoro di chi
lancia lo script sarebbe la trappola §13.16 del disegno: un coordinatore e il
suo dev condividono spesso lo stesso work_dir, e lo scambio ha già causato un
falso allarme reale (Leo scambiato per il fantasma di Dario).
"""
import json
import os
import subprocess
import sys
import time

CORE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, CORE)
import a2a_inbox
import tmux_esatto

LOG = os.path.join(CORE, "a2a_ack.log")


def _chiamante():
    """Chi ha lanciato l'ack, per il log (§10 nucleo: ogni effetto reale va
    loggato con chiamante+orario). Cross-check di due fonti indipendenti
    (§13.17 del disegno) — tty della propria finestra tmux + registrazione
    Postgres — MAI la cartella di lavoro, che è proprio ciò che inganna
    quando due agenti la condividono."""
    pane = os.environ.get("TMUX_PANE")
    if not pane:
        return "sconosciuto (fuori tmux)"
    try:
        finestra = subprocess.run(
            ["tmux", "display-message", "-p", "-t", pane, "#{session_name}:#{window_name}"],
            capture_output=True, text=True, timeout=5).stdout.strip()
        if not finestra:
            return "sconosciuto (finestra tmux non risolta)"
        import psycopg2
        conn = psycopg2.connect("dbname=betty_db user=betty_user host=localhost port=5432")
        cur = conn.cursor()
        # 22/08/2026 FIX classe B (integrazione mandato di samantha_1): MAI
        # fetchone su chiave non univoca — piu' record reali che condividono la
        # finestra (es. gemelli orfani tipo aurelio_1+copia) fanno scegliere al
        # DB il primo a caso. Resolver condiviso tmux_esatto.risolvi_agente_da_finestra:
        # esclude i virtuali (pool_di/alias_di) e, se restano piu' righe, LO DICE.
        agent, ambigui = tmux_esatto.risolvi_agente_da_finestra(finestra, cur)
        conn.close()
        if agent:
            return agent
        if ambigui:
            return (f"sconosciuto (finestra '{finestra}' condivisa da piu' record reali: "
                    f"{', '.join(ambigui)} — non attribuisco a caso)")
        return f"sconosciuto (finestra '{finestra}' non registrata)"
    except Exception as e:
        return f"sconosciuto (errore identificazione: {e})"


def main():
    if len(sys.argv) != 2:
        print("Uso: a2a_ack.py <A2A_ID>", file=sys.stderr)
        return 2
    a2a_id = sys.argv[1]

    slug, path = a2a_inbox.trova_in_inbox(a2a_id)
    if not path:
        print(f"[a2a_ack] Nessun messaggio con id '{a2a_id}' trovato in nessuna inbox "
              f"(già letto? id sbagliato?).", file=sys.stderr)
        return 1

    destinazione = a2a_inbox.segna_letto(slug, path)
    chiamante = _chiamante()

    riga = {"quando": time.strftime("%Y-%m-%dT%H:%M:%S"), "a2a_id": a2a_id,
            "slug_inbox": slug, "chiamante": chiamante, "path": destinazione}
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(riga, ensure_ascii=False) + "\n")

    print(f"[a2a_ack] ✅ LETTO: {destinazione} (chiamante: {chiamante})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
