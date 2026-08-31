#!/usr/bin/env python3
"""
guardiano_inbox.py — Check di stato A2A: rileva messaggi depositati ma mai letti.

Problema che risolve (finding di Igor, 15/08/2026): send_a2a.py deposita sempre
in a2a/<dest>/inbox/ (DEV-110) ma NESSUN processo controlla se il file viene letto.
Se il destinatario è spento/inesistente, il messaggio resta "appeso" in eterno.
Questo script è il tassello mancante: un guardiano periodico che
  1) scansiona tutte le a2a/*/inbox/
  2) segnala i file non letti da piu' di una soglia
  3) verifica lo stato del destinatario (sessione tmux viva / esistente non-dismesso)
  4) per file critici non letti verso destinatari senza sessione, produce un alert.

E' un tool NUOVO e indipendente: NON modifica send_a2a.py né il motore A2A.
Dry-run con --dry-run per debug. Uso (dai core):
    ./venv/bin/python3 guardiano_inbox.py [--soglia-ore 6] [--dry-run] [--alert-out FILE]
"""

import os
import sys
import glob
import time
import argparse

A2A_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "a2a")

def eta_ore(path):
    try:
        return (time.time() - os.path.getmtime(path)) / 3600.0
    except OSError:
        return -1.0

def destinatario_vivo(slug):
    """Vero se esiste una sessione tmux che contiene la slug o una finestra ad essa riferita."""
    # parsimonioso: usa un tmux ls e cerca la slug; fallisce dolcemente se tmux non c'e'
    try:
        import subprocess
        r = subprocess.run(["tmux", "ls"], capture_output=True, text=True, timeout=5)
        if r.returncode != 0:
            return None  # tmux non disponibile -> sconosciuto
        return slug.lower() in (r.stdout or "").lower()
    except Exception:
        return None

def destinatario_esiste_pg(slug):
    """Vero se l'agente esiste in Postgres e non e' dismesso."""
    try:
        import psycopg2
        conn = psycopg2.connect("dbname=betty_db user=betty_user host=localhost port=5432")
        cur = conn.cursor()
        cur.execute("SELECT data->>'dismesso' FROM agents WHERE agent_id = %s;", (slug,))
        row = cur.fetchone()
        conn.close()
        if row is None:
            return False, "non in DB"
        return (str(row[0]).lower() != "true"), ("dismesso" if str(row[0]).lower()=="true" else "attivo")
    except Exception as e:
        return None, f"pg-err({e})"

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--soglia-ore", type=float, default=6.0)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--alert-out", default=None)
    args = ap.parse_args()

    if not os.path.isdir(A2A_ROOT):
        print(f"[guardiano] ERRORE: {A2A_ROOT} non esiste")
        return 2

    appesi = []
    for inbox in sorted(glob.glob(os.path.join(A2A_ROOT, "*", "inbox"))):
        slug = os.path.basename(os.path.dirname(inbox))
        for f in sorted(glob.glob(os.path.join(inbox, "*"))):
            if not os.path.isfile(f):
                continue
            h = eta_ore(f)
            if h < 0 or h < args.soglia_ore:
                continue
            vivo = destinatario_vivo(slug)
            esiste, stato = destinatario_esiste_pg(slug)
            critico = (vivo is not True) and (esiste is not True)
            appesi.append({
                "dest": slug, "path": f, "ore": round(h,1),
                "vivo_tmux": vivo, "pg": f"{stato}", "critico": critico,
            })

    if not appesi:
        print(f"[guardiano] OK: nessun messaggio appeso oltre {args.soglia_ore}h")
        return 0

    print(f"[guardiano] {len(appesi)} messaggi appesi oltre {args.soglia_ore}h:")
    for m in appesi:
        tag = "CRITICO" if m["critico"] else "info"
        print(f"  [{tag}] {m['ore']}h  {m['dest']:22} tmux={m['vivo_tmux']} pg={m['pg']}  {m['path']}")

    if args.dry_run:
        print("[guardiano] DRY-RUN: nessun alert emesso.")
        return 0

    # emetti alert (per file critici non letti)
    if args.alert_out:
        lines = ["ALERT GUARDIANO A2A: messaggi depositati ma mai letti",
                 f"- soglia: {args.soglia_ore} ore - {time.strftime('%Y-%m-%d %H:%M')}"]
        for m in appesi:
            if m["critico"]:
                lines.append(f"- {m['ore']}h -> {m['dest']} (pg={m['pg']}): {m['path']}")
        with open(args.alert_out, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines))
        print(f"[guardiano] alert scritto su {args.alert_out} ({len(appesi)} critici)")
    return 0

if __name__ == "__main__":
    sys.exit(main())
