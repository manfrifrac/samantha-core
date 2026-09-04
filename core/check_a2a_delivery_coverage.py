#!/usr/bin/env python3
"""
check_a2a_delivery_coverage.py — verifica SISTEMATICA che la consegna A2A copra
DAVVERO tutti gli agenti isolati attuali, invece di fidarsi di una lista scritta
a mano che invecchia.

PERCHE' ESISTE (task Leo, 10/08/2026, "AFFIDABILITA' DELLA CONSEGNA MESSAGGI SU
DEEP CODE"): in una sola giornata sono emerse 4 varianti dello stesso guasto di
fondo (agenti deepcode isolati che non ricevono/non processano messaggi):
  1. send_a2a._notifica_tmux: falso positivo "generazione partita" su motori
     non-Claude (pattern hardcoded sulla UI di Claude Code) — chiuso da Dario.
  2. sudo -u stripava PGUSER (env_reset di sudo) — chiuso da Dario.
  3. socket tmux vuoto per i mittenti dc-* isolati, campanello mai recapitato
     — chiuso da Marco con a2a_bell_relay.py.
  4. whitelist DC_SLUGS del relay ferma a 3 nomi su 15+ agenti isolati reali
     — chiuso rendendola dinamica da Postgres (unix_user IS NOT NULL).

Il punto (4) sposta il rischio, non lo elimina: ora la whitelist e' sempre
aggiornata SE E SOLO SE il campo `unix_user` in Postgres e' sempre accurato.
Un nuovo agente isolato creato senza valorizzare quel campo (o con un typo)
torna silenziosamente scoperto — stesso sintomo, causa spostata di un livello.
Questo script non fida della dichiarazione (Postgres): la incrocia con la
REALTA' dei processi live, in entrambe le direzioni.

QUATTRO CONTROLLI, tutti read-only (nessuna scrittura, nessun tocco a
processi/tmux/file altrui):

  A) Isolamento dichiarato -> verificato: ogni agente con unix_user impostato
     ha DAVVERO un processo vivo sotto quell'utente Unix nel proprio pannello?
     (agente offline non e' un'anomalia di QUESTO check — lo copre
     check_agenti_vivi.py — qui si segnala solo "non verificabile", non si
     alza un allarme rosso).

  B) Isolamento reale -> dichiarato (il controllo che chiude davvero il
     buco classe-4): ogni processo dc-* trovato VIVO sul sistema, la cui
     provenienza risale a un pannello tmux, ha un agente in Postgres con
     quello stesso unix_user? Se un processo dc-* esiste ma Postgres non lo
     sa, il relay lo salta in silenzio — e' l'anomalia che conta di piu'.

  C) Guardrail PGUSER: per i soli agenti isolati con ruolo Coordinatore
     (unici a cui agent_launcher.py esporta PGUSER), il processo isolato
     vivo lo ha DAVVERO nel proprio ambiente (/proc/<pid>/environ), col
     valore atteso? Verifica che il fix sudo/env_reset non sia regredito.

  D) Il relay e' vivo e il suo insieme dinamico coincide con la realta':
     processo a2a_bell_relay.py in esecuzione (guardia single-instance) +
     chiamata diretta a _dc_slug_set() (stesso codice di produzione, non
     una sua reimplementazione) confrontata con l'insieme reale del check B.

Uso:  ./venv/bin/python3 check_a2a_delivery_coverage.py [--tutti]
      (senza argomenti stampa solo le anomalie; --tutti elenca anche i sani)

LIMITE ONESTO: legge processi/tmux in un istante — un agente che si sta
riavviando nel momento esatto del check puo' dare un falso "non verificabile"
in (A). Non e' una diagnosi da un solo giro, stesso principio di
check_agenti_vivi.py: un'anomalia isolata si ricontrolla prima di agire.
"""
import argparse
import os
import re
import subprocess
import sys

CORE = os.path.dirname(os.path.abspath(__file__))
if CORE not in sys.path:
    sys.path.insert(0, CORE)

import tmux_esatto


def _run(cmd):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True)


def _agenti_postgres():
    import psycopg2
    conn = psycopg2.connect("dbname=betty_db user=betty_user host=localhost port=5432")
    cur = conn.cursor()
    cur.execute(
        "SELECT agent_id, data->>'unix_user', data->>'tmux_window', "
        "data->>'engine', data->>'role' FROM agents;"
    )
    rows = cur.fetchall()
    conn.close()
    out = {}
    for agent_id, unix_user, tmux_window, engine, role in rows:
        out[agent_id] = {
            "unix_user": unix_user or None,
            "tmux_window": tmux_window,
            "engine": engine,
            "coordinatore": bool(role and "coordinat" in role.lower()),
        }
    return out


def _dc_utenti_sistema():
    """Utenti dc-* realmente esistenti su /etc/passwd (verita' del sistema)."""
    utenti = set()
    try:
        with open("/etc/passwd", encoding="utf-8") as f:
            for riga in f:
                nome = riga.split(":", 1)[0]
                if nome.startswith("dc-"):
                    utenti.add(nome)
    except OSError:
        pass
    return utenti


def _pane_pid(tmux_window):
    # 22/08/2026 FIX classe "match per prefisso" (funzione condivisa tmux_esatto):
    # list-panes sul target ESATTO — una finestra assente da' None, mai il pid di
    # una finestra con lo stesso prefisso di nome (es. AGY-PUBLIO vs AGY-PUBLIO-2).
    pid = tmux_esatto.pane_pid_finestra(tmux_window)
    return pid if (pid or "").isdigit() else None


class _TabellaProcessi:
    """Un SOLO `ps -eo` per l'intero check, invece di uno spawn di sottoprocesso
    per ogni PID di ogni agente (prima versione: >1000 subprocess, oltre i 60s).
    Tutto il resto lavora in memoria su questa istantanea."""

    def __init__(self):
        # user:32 e' NECESSARIO: la colonna 'user' di default tronca a 8
        # caratteri con un '+' finale (bug reale trovato testando questo
        # script: 'dc-aurelio-privati' arrivava come 'dc-aure+', nessun
        # confronto con Postgres avrebbe mai potuto combaciare).
        r = _run("ps -eo pid,ppid,user:32,cmd --no-headers")
        self.ppid = {}
        self.user = {}
        self.cmd = {}
        self.figli = {}
        for riga in r.stdout.splitlines():
            campi = riga.split(None, 3)
            if len(campi) < 4:
                continue
            pid, ppid, user, cmd = campi
            self.ppid[pid] = ppid
            self.user[pid] = user
            self.cmd[pid] = cmd
            self.figli.setdefault(ppid, []).append(pid)

    def sottoalbero(self, pid):
        pid = str(pid)
        visti = set()
        coda = [pid]
        while coda:
            p = coda.pop()
            if p in visti:
                continue
            visti.add(p)
            coda.extend(self.figli.get(p, []))
        return visti

    def owner(self, pid):
        return self.user.get(str(pid))

    def comando(self, pid):
        return self.cmd.get(str(pid), "")

    def tutti_i_pid(self):
        return list(self.user.keys())


def _environ(pid):
    try:
        with open(f"/proc/{pid}/environ", "rb") as f:
            raw = f.read()
    except OSError:
        return {}
    env = {}
    for coppia in raw.split(b"\0"):
        if b"=" in coppia:
            k, _, v = coppia.partition(b"=")
            env[k.decode(errors="replace")] = v.decode(errors="replace")
    return env


def _engine_atteso_pattern(engine):
    return {
        "claude": r"(^|/)claude($|\s)",
        "agy": r"(^|/)agy($|\s)",
        "qwen": r"(^|/)qwen($|\s)",
        "deepcode": r"(^|/)node\s.*/deepcode($|\s)",
    }.get(engine)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tutti", action="store_true", help="elenca anche i controlli sani, non solo le anomalie")
    args = parser.parse_args()

    agenti = _agenti_postgres()
    utenti_sistema = _dc_utenti_sistema()
    tabella = _TabellaProcessi()
    anomalie = []
    sani = []

    # --- A) isolamento dichiarato -> verificato, + C) PGUSER, + engine reale ---
    processi_isolati_trovati = {}  # unix_user -> (agent_id, pid) per il check B/D
    for agent_id, info in sorted(agenti.items()):
        unix_user = info["unix_user"]
        tmux_window = info["tmux_window"]
        if not tmux_window:
            continue
        pane_pid = _pane_pid(tmux_window)
        if pane_pid is None:
            if unix_user:
                anomalie.append(f"{agent_id}: unix_user='{unix_user}' dichiarato ma finestra '{tmux_window}' irraggiungibile (agente offline? — vedi check_agenti_vivi.py)")
            continue

        sottoalbero = tabella.sottoalbero(pane_pid)

        if unix_user:
            if unix_user not in utenti_sistema:
                anomalie.append(f"{agent_id}: unix_user='{unix_user}' in Postgres NON esiste su questo sistema (/etc/passwd) — probabile typo")
                continue
            # PID piu' basso fra quelli owned dall'utente isolato = il piu'
            # vecchio del sottoalbero (i PID crescono nel tempo), cioe' il
            # processo lanciato da sudo -u stesso — non un suo figlio (es. un
            # server MCP, che deepcode-cli lancia come proprio discendente e
            # che EREDITA lo stesso utente: preso per errore nella prima
            # versione, dava un falso confronto d'engine su radio).
            pid_owned = sorted((p for p in sottoalbero if tabella.owner(p) == unix_user), key=int)
            if not pid_owned:
                anomalie.append(f"{agent_id}: unix_user='{unix_user}' dichiarato ma NESSUN processo owned by '{unix_user}' trovato nel pannello '{tmux_window}' (non verificabile ora — ricontrollare)")
                continue
            pid_isolato = pid_owned[0]
            processi_isolati_trovati[unix_user] = (agent_id, pid_isolato)
            if args.tutti:
                sani.append(f"{agent_id}: isolamento '{unix_user}' verificato sul processo vivo {pid_isolato}")

            # C) PGUSER guardrail, solo per i coordinatori isolati
            if info["coordinatore"]:
                pguser = _environ(pid_isolato).get("PGUSER")
                if not pguser:
                    anomalie.append(f"{agent_id}: COORDINATORE isolato ('{unix_user}') ma PGUSER assente dal processo {pid_isolato} — guardrail sudo/env_reset regredito")
                elif args.tutti:
                    sani.append(f"{agent_id}: PGUSER='{pguser}' presente sul coordinatore isolato (guardrail sudo intatto)")

            # engine reale: basta che UNO dei processi owned dall'utente
            # isolato corrisponda al binario atteso (non solo il primo/piu'
            # vecchio — un MCP figlio con lo stesso utente non deve far
            # sembrare fallito il confronto se il processo giusto e' un altro
            # nello stesso sottoalbero).
            atteso = _engine_atteso_pattern(info["engine"])
            if atteso and not any(re.search(atteso, tabella.comando(p)) for p in pid_owned):
                anomalie.append(f"{agent_id}: Postgres dice engine='{info['engine']}' ma nessuno dei processi isolati {pid_owned} corrisponde — send_a2a userebbe la logica wait_idle/ESC sbagliata")

    # --- B) isolamento reale -> dichiarato (il controllo che chiude la classe-4) ---
    # Scansione diretta di TUTTI i processi vivi owned da un utente dc-*: se ne
    # esiste uno che il giro sopra (A) non ha gia' abbinato a un agente Postgres
    # coerente, e' un agente isolato REALE che il relay non conosce.
    dc_pid_owner = {}
    for pid in tabella.tutti_i_pid():
        owner = tabella.owner(pid)
        if owner and owner.startswith("dc-"):
            dc_pid_owner.setdefault(owner, []).append(pid)

    # 18/08/2026 (Samantha): IDENTITA' DI SERVIZIO note — utenze dc-* di agenti
    # dismessi RIUSATE da pipeline di sistema, non agenti da campanellare.
    # Caso reale: la pipeline download della radio (yt-dlp/ffmpeg dentro
    # ns_surfshark via setpriv, lanciata da root) gira con l'uid ereditato da
    # dc-redattore (agente dismesso). Senza questa eccezione OGNI download
    # cambiava la firma delle anomalie e ri-allertava Samantha piu' volte al
    # giorno per lavoro legittimo. Se una di queste utenze tornasse a essere
    # un AGENTE vero, va tolta da qui.
    IDENTITA_DI_SERVIZIO = {"dc-redattore"}
    for unix_user, pids in sorted(dc_pid_owner.items()):
        if unix_user in processi_isolati_trovati:
            continue  # gia' abbinato correttamente in (A)
        if unix_user in IDENTITA_DI_SERVIZIO:
            continue  # pipeline di sistema con uid riciclato: non e' un agente
        anomalie.append(f"utente di sistema '{unix_user}' ha {len(pids)} processi vivi (es. PID {pids[0]}) ma NESSUN agente in Postgres ha questo unix_user abbinato a un pannello raggiungibile — il relay lo salterebbe in silenzio")

    # --- D) campanello unificato vivo + insieme dinamico coerente con la realta' ---
    # 22/08/2026 (exec vigile_notifiche): il relay (a2a_bell_relay.py) e il
    # campanellaio del pool sono stati UNIFICATI in vigile_notifiche.py. Il
    # controllo si sposta sul processo VERO (il vigile), stessa funzione _dc_slug_set.
    relay_vivo = _run("pgrep -f '[v]igile_notifiche.py'").stdout.strip()
    if not relay_vivo:
        anomalie.append("vigile_notifiche.py NON risulta in esecuzione — nessun campanello arriva agli agenti che mandano A2A (campanello unificato morto)")
    else:
        if args.tutti:
            sani.append(f"vigile_notifiche.py vivo (pid {relay_vivo.splitlines()[0]})")
        try:
            import vigile_notifiche
            dc_set_relay = vigile_notifiche._dc_slug_set()
        except Exception as e:
            dc_set_relay = None
            anomalie.append(f"impossibile chiamare vigile_notifiche._dc_slug_set() per confronto diretto: {e}")
        if dc_set_relay is not None:
            attesi = {aid for aid, info in agenti.items() if info["unix_user"]}
            mancanti_nel_relay = attesi - dc_set_relay
            extra_nel_relay = dc_set_relay - attesi
            if mancanti_nel_relay:
                anomalie.append(f"_dc_slug_set() del vigile NON include: {sorted(mancanti_nel_relay)} (pur avendo unix_user in Postgres) — cache di 60s scaduta? o query diversa da questo script")
            if extra_nel_relay:
                anomalie.append(f"_dc_slug_set() del vigile include agenti SENZA unix_user secondo questa lettura: {sorted(extra_nel_relay)} (race fra le due letture, o entrambe le liste vanno confrontate a distanza ravvicinata)")
            if not mancanti_nel_relay and not extra_nel_relay and args.tutti:
                sani.append(f"_dc_slug_set() coincide con Postgres: {len(dc_set_relay)} agenti isolati coperti")

    print(f"=== check_a2a_delivery_coverage — {len(agenti)} agenti censiti, {len(processi_isolati_trovati)} isolati verificati vivi ===")
    if args.tutti:
        for s in sani:
            print(f"  ✅ {s}")
    if anomalie:
        print(f"\n⚠️  {len(anomalie)} ANOMALIE:")
        for a in anomalie:
            print(f"  ⚠️  {a}")
        return 1
    print("  Nessuna anomalia.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
