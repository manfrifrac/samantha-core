#!/usr/bin/env python3
"""a2a_bell_relay.py — campanello A2A per agenti dc-isolati (task Leo bcfd, 10/08).

Contesto: l'invio A2A deposita il messaggio su disco (consegna vera, `a2a_inbox.py`)
e poi `send_a2a.py` fa la paste del CAMPANELLO nel pannello tmux del destinatario per
risvegliarlo. Ma i pannelli degli agenti dc-isolati (redattore/regista/radio) vivono
nel server tmux di ROOT (/tmp/tmux-0), mentre un mittente dc-* (non-root) esegue
`tmux` sul proprio socket VUOTO (/tmp/tmux-<uid>) e non raggiunge il server: la paste
fallisce e il destinatario non viene mai svegliato (messaggio su disco ma non letto).

La via "sudo verso root" (proposta vecchia di Dario) NON e' praticabile: i dc-* non
hanno permessi sudo (verificato: sudo: a password is required) e dar loro accesso al
socket di root sarebbe una pericolosa espansione di privilegi.

Questo processo gira come ROOT e fa da RELAY del campanello: ogni POLL_INTERVAL
scorre le inbox di tutti gli agenti; per ogni messaggio nuovo campanella lui stesso
il pannello del destinatario (root raggiunge il server). Idempotente: non reinventa
se il marcatore del campanello e' gia' visibile nel pannello (casi gia' notificati
dal mittente root) ne' se il file e' gia' campanellato da questo relay (file di stato).
"""

import datetime
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tmux_esatto

BASE = "/root/ecosistema_agenti/a2a"
STATE_FILE = "/tmp/a2a_bell_relay_state"
POLL_INTERVAL = int(os.environ.get("A2A_BELL_POLL_INTERVAL", "8"))
LOCK_PATH = "/root/ecosistema_agenti/core/a2a_bell_relay.lock"
# FIX 11/08/2026 (effetto collaterale Betty, task exec_dev_consegna): il relay
# deve campanellare i messaggi NUOVI, non l'arretrato storico. Un messaggio piu'
# vecchio di MAX_ETA_NUOVO_SECONDS resta in inbox ma non e' una "notizia": viene
# aggregato in un UNICO avviso invece di uno spam per messaggio.
MAX_ETA_NUOVO_SECONDS = int(os.environ.get("A2A_BELL_MAX_ETA_NUOVO", "1800"))  # 30 min
# Marcatore dell'avviso aggregato (univoco per destinatario) per il dedup-in-pane.
AGG_MARKER_TMPL = "[A2A_AGGREGATO:{slug}]"
MAX_AGE_SECONDS = 600  # mantenuto per compat' col docstring; il vero gate e' "ancora in inbox"

# AUTO-SCALING (fix urgente task Leo 509c, 10/08): prima c'era una whitelist fissa
# DC_SLUGS={redattore,regista,radio}, ferma a 3 mentre la flotta dc-isolata e' a 15+.
# Ora il set dei destinatari e' letto DINAMICAMENTE da Postgres, con cache breve
# per non martellare il DB pur seguendo i nuovi agenti.
#
# FOLLOW-UP 11/08/2026 (richiesta Betty, task exec_dev_consegna): il criterio
# precedente era "unix_user valorizzato" = SOLO gli agenti dc-isolati. Ma i
# mittenti ROOT (che NON hanno unix_user: betty, alfredo, gli exec root, in
# generale chi gira come root nel server tmux) venivano saltati dal relay — e
# per loro vale SOLO il campanello diretto di send_a2a, che e' proprio quello
# fragile che si perde (bug delle 3 copie di Simone). Risultato: betty restava
# cieca ai campanelli finche' non guardava la cartella a mano.
# Criterio NUOVO, piu' ampio e additivo: campanella chiunque abbia una
# `tmux_window` NON vuota (root o dc che sia), cosi' il relay diventa la rete di
# sicurezza del campanello per TUTTI, non solo per i dc-isolati. La dedup su
# `stato` + la guardia "marcatore in pane" evitano doppioni e reinventate.
_DC_SLUG_CACHE = {"ts": 0.0, "set": frozenset()}


def _dc_slug_set():
    # cache TTL (default 60s): i nuovi agenti compaiono entro un minuto.
    import time as _t
    now = _t.time()
    if now - _DC_SLUG_CACHE["ts"] < 60 and _DC_SLUG_CACHE["set"]:
        return _DC_SLUG_CACHE["set"]
    import psycopg2
    try:
        conn = psycopg2.connect("dbname=betty_db user=betty_user host=localhost port=5432")
        cur = conn.cursor()
        # FIX (11/08/2026): destinatari da campanellare = chi HA una finestra
        # tmux (tmux_window non vuota), NON piu' solo chi ha unix_user. Cosi'
        # entrano anche i coordinatori/exec che girano come root e sono ciechi
        # al campanello diretto. Tutti i 16 dc-isolati hanno tmux_window, quindi
        # il nuovo criterio e' un'estensione ADDITIVA, nessuna regressione.
        # 19/08/2026 (pool samantha, finding reale di samantha_1 6f07): i
        # record VIRTUALI di pool (pool_di) restano ESCLUSI anche se hanno
        # una tmux_window (in transizione puntano alla finestra di
        # un'istanza per l'inbound Telegram). La loro coda ha un campanellaio
        # dedicato (pool_campanellaio.py) che indica lo strumento giusto
        # (pool_claim): il relay storico ci suonava SOPRA suggerendo
        # a2a_ack — doppio campanello con istruzioni contraddittorie, e
        # a2a_ack sulla coda pool scavalcherebbe ledger e affinita'.
        cur.execute("SELECT agent_id FROM agents "
                    "WHERE data->>'tmux_window' IS NOT NULL "
                    "AND data->>'tmux_window' != '' "
                    "AND COALESCE(data->>'pool_di','') = '';")
        s = frozenset(r[0] for r in cur.fetchall())
        conn.close()
    except Exception:
        return _DC_SLUG_CACHE["set"]  # se il DB fallisce, usa l'ultimo set valido
    _DC_SLUG_CACHE["ts"] = now
    _DC_SLUG_CACHE["set"] = s
    return s


def _run(cmd, **kw):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True, **kw)


def _slug_da_path(path):
    # BASE/<slug>/inbox/<file>
    rel = os.path.relpath(path, BASE)
    parts = rel.split(os.sep)
    return parts[0]


def _risolvi_finestra(slug):
    """agent_id -> tmux_window (inverso di resolve_recipient_slug)."""
    import psycopg2
    try:
        conn = psycopg2.connect("dbname=betty_db user=betty_user host=localhost port=5432")
        cur = conn.cursor()
        cur.execute("SELECT data->>'tmux_window', data->>'engine' FROM agents WHERE agent_id = %s;", (slug,))
        row = cur.fetchone()
        conn.close()
        return (row[0], row[1]) if row else (None, None)
    except Exception:
        return None, None


def _intestazione_campanello(testo, a2a_id):
    m = re.match(r"^(\[A2A_FROM:[^\]]+\]\s*\[A2A_TYPE:[^\]]+\])", testo.strip())
    prefisso = m.group(1) if m else "[A2A_FROM:?] [A2A_TYPE:?]"
    return f"{prefisso} [A2A_ID:{a2a_id}]"


def _marcatore_campanello(testo, a2a_id):
    return _intestazione_campanello(testo, a2a_id)


def _eta_secondi(path):
    """Eta' in secondi (dal deposito) di un file inbox, parsando il timestamp
    UTC che apre il filename (es. 20260811T154418Z__betty__2e68.md).
    Ritorna None se il nome non ha il formato atteso (fail-open: chi chiama
    decide come trattare un file senza eta' calcolabile)."""
    base = os.path.basename(path)
    m = re.match(r"^(\d{8}T\d{6}Z)__", base)
    if not m:
        return None
    try:
        ts = datetime.datetime.strptime(m.group(1), "%Y%m%dT%H%M%SZ")
        ts = ts.replace(tzinfo=datetime.timezone.utc)
        return (datetime.datetime.now(datetime.timezone.utc) - ts).total_seconds()
    except ValueError:
        return None


def _mittente_da_testo(testo):
    """Mittente auto-dichiarato nel tag [A2A_FROM:...] (first 200 char, stesso
    pattern di send_a2a/a2a_inbox). Ritorna '' se assente."""
    m = re.search(r"\[A2A_FROM:([^\]]+)\]", testo[:200])
    return m.group(1).strip() if m else ""


_DISMESSI_CACHE = {"ts": 0.0, "set": frozenset()}


def _agenti_dismessi():
    """Set di agent_id DISMESSI in Postgres (dismesso='true'), con cache breve.

    Stesso pattern di inbox_guard.agentidismessi (fix 11/08): un MITTENTE
    dismesso non esiste piu' e non aspetta nulla → i suoi messaggi in inbox NON
    vanno campanellati (sarebbero rumore, non notizie). Fail-open onesto: se il
    DB fallisce, nessuna esclusione (comportamento storico)."""
    now = time.time()
    if 0 < now - _DISMESSI_CACHE["ts"] < 300 and _DISMESSI_CACHE["set"]:
        return _DISMESSI_CACHE["set"]
    import psycopg2
    try:
        conn = psycopg2.connect("dbname=betty_db user=betty_user host=localhost port=5432")
        cur = conn.cursor()
        cur.execute("SELECT agent_id, data->>'dismesso' FROM agents")
        out = set()
        for rid, dismesso in cur.fetchall():
            if str(dismesso).strip().lower() == "true":
                out.add((rid or "").strip().lower())
        conn.close()
    except Exception:
        return _DISMESSI_CACHE["set"]
    _DISMESSI_CACHE["ts"] = now
    _DISMESSI_CACHE["set"] = out
    return out


def _campanella(finestra, campanello, engine="claude"):
    """Replica il trasporto di send_a2a._notifica_tmux su tmux di ROOT."""
    # 22/08/2026 FIX classe "match per prefisso" (funzione condivisa tmux_esatto):
    # normalizzazione UNICA a inizio funzione — tutti i send-keys/paste-buffer
    # sotto usano da qui il target ESATTO, mai una finestra con lo stesso
    # PREFISSO di nome (es. AGY-PUBLIO -> AGY-PUBLIO-2).
    finestra = tmux_esatto.target_finestra_esatto(finestra)
    if engine == "deepcode":
        _run(f"tmux send-keys -t '{finestra}' Escape")
        time.sleep(2.0)
    unique = f"{os.getpid()}_{int(time.time() * 1000)}"
    buf_file = f"/tmp/a2a_bell_paste_{unique}.txt"
    buf_name = f"a2a_bell_{unique}"
    try:
        with open(buf_file, "w", encoding="utf-8") as f:
            f.write(campanello)
        if _run(f"tmux load-buffer -b {buf_name} {buf_file}").returncode != 0:
            return False
        time.sleep(0.1)
        if _run(f"tmux paste-buffer -p -b {buf_name} -t '{finestra}'").returncode != 0:
            return False
        time.sleep(min(1.5 + len(campanello) / 800, 5.0))
        _run(f"tmux send-keys -t '{finestra}' End")
        time.sleep(0.2)
        _run(f"tmux send-keys -t '{finestra}' C-m")
        return True
    finally:
        _run(f"tmux delete-buffer -b {buf_name}")
        try:
            os.remove(buf_file)
        except OSError:
            pass


def _stato():
    s = set()
    if os.path.isfile(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            for riga in f:
                riga = riga.strip()
                if riga:
                    s.add(riga)
    return s


def _salva_stato(s):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        for x in sorted(s):
            f.write(x + "\n")


def main():
    # guardia single-instance (nucleo SS10)
    with open(LOCK_PATH, "w") as lf:
        import fcntl
        try:
            fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return 0

    while True:
        try:
            stato = _stato()
            now = time.time()
            # pulizia stato: rimuovi filename che non esistono piu' (letti/rimossi)
            vivi = set()
            for root_dir, _, files in os.walk(BASE):
                if os.path.basename(root_dir) != "inbox":
                    continue
                for f in files:
                    if f.endswith(".md"):
                        vivi.add(f)
            stato = {x for x in stato if x in vivi}
            dc_set = _dc_slug_set()   # destinatari da campanellare = chi ha una tmux_window (root o dc)
            dismessi = _agenti_dismessi()
            # slug che hanno gia' ricevuto l'avviso aggregato dell'arretrato in
            # questa run del processo: evita di re-inviarlo ad ogni ciclo.
            aggregato_emesso = set()
            # arretrato[slug] = quanti messaggi VECCHI (> soglia) non ancora gestiti
            arretrato = {}

            for root_dir, _, files in os.walk(BASE):
                if os.path.basename(root_dir) != "inbox":
                    continue
                for f in files:
                    if not f.endswith(".md") or f.startswith(".tmp_"):
                        continue
                    path = os.path.join(root_dir, f)
                    if f in stato:
                        continue  # gia' gestito da questo relay
                    slug = _slug_da_path(path)
                    if slug not in dc_set:
                        # nessuna tmux_window registrata: non campanellabile dal relay
                        continue
                    finestra, engine = _risolvi_finestra(slug)
                    if not finestra:
                        continue
                    try:
                        with open(path, encoding="utf-8") as fh:
                            testo = fh.read()
                    except OSError:
                        continue
                    # FIX 11/08/2026 (Betty): NON campanellare i messaggi il cui
                    # MITTENTE e' DISMESSO — non aspettano nulla, sono rumore.
                    mitt = _mittente_da_testo(testo).strip().lower()
                    if mitt and mitt in dismessi:
                        stato.add(f)
                        print(f"{datetime.datetime.now().isoformat(timespec='seconds')} "
                              f"[relay] saltato {slug} da {path} (mittente {mitt} dismesso)", flush=True)
                        continue
                    eta = _eta_secondi(path)
                    if eta is not None and eta > MAX_ETA_NUOVO_SECONDS:
                        # arretrato storico: NON campanello individuale (sarebbe
                        # rumore se ne arrivano decine insieme), lo conto per
                        # l'avviso aggregato qui sotto. Marca gestito cosi' non
                        # lo ricampionella ne' lo riconta.
                        arretrato[slug] = arretrato.get(slug, 0) + 1
                        stato.add(f)
                        continue
                    # messaggio NUOVO: campanello individuale, uno alla volta.
                    a2a_id = f[:-3]  # togli .md -> ts__mitt__rand
                    campanello = _intestazione_campanello(testo, a2a_id)
                    marcatore = _marcatore_campanello(testo, a2a_id)
                    # 22/08/2026 FIX "match per prefisso": lettura sul target ESATTO.
                    pane = tmux_esatto.capture_pane_finestra(finestra, 6) or ""
                    if marcatore in pane:
                        # gia' incollato dal mittente: non reinventare
                        stato.add(f)
                        continue
                    # FIX 17/08/2026 (dato di Simone: 7 turni a vuoto in una sera
                    # sulla quota Opus): MAI campanellare un pane OCCUPATO. Il
                    # paste su un pane in generazione viene ACCODATO dal CLI e
                    # consegnato al turno successivo, quando il file e' quasi
                    # sempre gia' letto e ackato -> un turno intero (contesto
                    # completo ricaricato) bruciato per un non-evento. Si salta
                    # SENZA marcare gestito: al prossimo ciclo, se l'agente ha
                    # gia' letto il file, questo non e' piu' in inbox e il
                    # campanello muore da se'; se e' ancora li', si suona a pane
                    # libero. "esc to interrupt" = generazione in corso (claude);
                    # su altri motori il marcatore manca e si suona come prima.
                    if "esc to interrupt" in pane:
                        continue
                    # anti-race: il file potrebbe essere stato ackato fra il walk
                    # e questo punto — un campanello per un file gia' in read/ e'
                    # esattamente il rumore che stiamo eliminando.
                    if not os.path.exists(path):
                        stato.add(f)
                        continue
                    if _campanella(finestra, campanello, engine or "claude"):
                        stato.add(f)
                        print(f"{datetime.datetime.now().isoformat(timespec='seconds')} "
                              f"[relay] campanellato {slug} da {path}", flush=True)

            # Avvisi AGGREGATI per l'arretrato: UN SOLO campanello per destinatario
            # che ha messaggi vecchi non gestiti, invece di uno per messaggio.
            for slug, n in arretrato.items():
                if slug in aggregato_emesso:
                    continue
                finestra, engine = _risolvi_finestra(slug)
                if not finestra:
                    continue
                marcatore = AGG_MARKER_TMPL.format(slug=slug)
                # 22/08/2026 FIX "match per prefisso": lettura sul target ESATTO.
                pane = tmux_esatto.capture_pane_finestra(finestra, 6) or ""
                if marcatore in pane:
                    aggregato_emesso.add(slug)
                    continue
                msg = (f"[A2A_AGGREGATO:{slug}] Hai {n} messaggio/i arretrati "
                       f"(>{MAX_ETA_NUOVO_SECONDS // 60} min) in a2a/{slug}/inbox/ — "
                       f"vecchio backlog, probabilmente gia' risolto. Controlla a mano se serve.")
                if _campanella(finestra, msg, engine or "claude"):
                    aggregato_emesso.add(slug)
                    print(f"{datetime.datetime.now().isoformat(timespec='seconds')} "
                          f"[relay] avviso aggregato {slug} ({n} arretrati)", flush=True)
            _salva_stato(stato)
            time.sleep(POLL_INTERVAL)
        except KeyboardInterrupt:
            return 0
        except Exception as e:
            print(f"[a2a_bell_relay] ERRORE (prima del prossimo ciclo): {e}", file=sys.stderr, flush=True)
            time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    sys.exit(main())
