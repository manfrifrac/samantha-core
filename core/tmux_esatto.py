#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Target tmux a match ESATTO — unica funzione condivisa per tutta la classe
di bug "match per prefisso" (mandato exec_fix_match_prefisso, 22/08/2026).

PROBLEMA
tmux fa match per PREFISSO sui nomi finestra. Un target 'sessione:AGY-PUBLIO'
risolve su 'sessione:AGY-PUBLIO-2' quando la prima non esiste: ogni strumento
che usava `tmux list-panes -t 'sess:win'` per dire "l'agente e' vivo" mentiva
(publio_1 giu' da ore, creduto vivo), e ogni invio A2A con target 'sess:win'
poteva incollare il messaggio nella finestra SBAGLIATA.

FIX SCELTO (una sola forma, una sola funzione condivisa)
Il prefisso '=' nel target tmux: `-t 'sess:=win'` forza il match ESATTO sul
nome finestra. Verificato live in tmux:
  - `list-panes -t 'fiscale:AGY-PUBLIO'`   -> risolve su AGY-PUBLIO-2 (il bug)
  - `list-panes -t 'fiscale:=AGY-PUBLIO'`  -> rc!=0, finestra assente (vero)
  - `capture-pane -pt 'betty:=agy-Samantha-1'` -> contenuto corretto (ok)

Tutti i punti del core che costruiscono un target tmux da tmux_window (o da
sessione:finestra) passano da QUI. Non copiare mai piu' la stringa
'sess:win' nuda in un comando tmux: se devi decidere se una finestra e'
viva, usa pane_pid_finestra; se devi leggerla, capture_pane_finestra; se
devi risolvere finestra->agente, risolvi_agente_da_finestra.

Nessuna dipendenza pesante a import: psycopg2 entra solo dentro
risolvi_agente_da_finestra.
"""

import os
import signal
import subprocess
import time


def target_finestra_esatto(finestra):
    """Ritorna il target tmux a match ESATTO per 'sessione:finestra'.

        'fiscale:AGY-PUBLIO'    -> 'fiscale:=AGY-PUBLIO'
        'fiscale:=AGY-PUBLIO'   -> 'fiscale:=AGY-PUBLIO'   (idempotente)
        'services'              -> 'services'              (sessione nuda)
    """
    if ":" not in finestra:
        return finestra
    sess, _, win = finestra.partition(":")
    win = win.lstrip("=")  # idempotenza: non raddoppiare il '='
    return f"{sess}:={win}"


def pane_pid_finestra(finestra, timeout=20):
    """PID del pane della finestra ESATTA, o None se la finestra esatta non
    esiste. Sostituisce OGNI 'tmux list-panes -t sess:win' usato per decidere
    la liveness: con il target esatto, una finestra assente da' None
    (vivo=FALSO), mai il pid di una finestra con lo stesso prefisso di nome.
    """
    r = subprocess.run(
        ["tmux", "list-panes", "-t", target_finestra_esatto(finestra),
         "-F", "#{pane_pid}"],
        capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        return None
    pids = r.stdout.split()
    return pids[0] if pids else None


def pane_dead_finestra(finestra, timeout=20):
    """Valore '#{pane_dead}' (1 se il pane e' morto) della finestra ESATTA,
    o None se la finestra esatta non esiste. Serve al self-healing: il check
    pane_dead va fatto sul target esatto, non su un target che risolve per
    prefisso su un'altra finestra con lo stesso inizio di nome."""
    r = subprocess.run(
        ["tmux", "list-panes", "-t", target_finestra_esatto(finestra),
         "-F", "#{pane_dead}"],
        capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        return None
    vals = r.stdout.split()
    return vals[0] if vals else None


def finestra_viva(finestra, timeout=20):
    """True se la finestra ESATTA esiste in tmux (ha almeno un pane)."""
    return pane_pid_finestra(finestra, timeout) is not None


def capture_pane_finestra(finestra, righe=None, timeout=20):
    """Contenuto del pane della finestra ESATTA, o None se assente.

    - righe=None -> 'capture-pane -pt sess:=win'  (schermata visibile, default tmux)
    - righe=N    -> 'capture-pane -pt sess:=win -S -N'  (N righe di scrollback)
    """
    cmd = ["tmux", "capture-pane", "-pt", target_finestra_esatto(finestra)]
    if righe:
        cmd += ["-S", f"-{righe}"]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        return None
    return r.stdout


def send_keys_finestra(finestra, *tasti, timeout=20):
    """'tmux send-keys -t sess:=win <tasti...>' sul target ESATTO.
    Ritorna True se tmux ha accettato il comando (rc=0)."""
    r = subprocess.run(
        ["tmux", "send-keys", "-t", target_finestra_esatto(finestra), *tasti],
        capture_output=True, text=True, timeout=timeout)
    return r.returncode == 0


def kill_finestra(finestra, timeout=20):
    """'tmux kill-window -t sess:=win' sul target ESATTO. Ritorna True se tmux
    ha accettato il comando (rc=0). Con il target esatto una finestra assente
    NON uccide per sbaglio una finestra con lo stesso prefisso di nome."""
    r = subprocess.run(
        ["tmux", "kill-window", "-t", target_finestra_esatto(finestra)],
        capture_output=True, text=True, timeout=timeout)
    return r.returncode == 0


def nomi_finestre_sessione(sessione, timeout=20):
    """Insieme dei nomi finestra ESATTI di una sessione (da list-windows,
    mai una decisione di liveness: solo enumerazione)."""
    r = subprocess.run(
        ["tmux", "list-windows", "-t", sessione, "-F", "#{window_name}"],
        capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        return set()
    return set(r.stdout.split())


def risolvi_agente_da_finestra(finestra, cur):
    """Risolve la finestra tmux -> agent_id REALE, senza MAI scegliere a caso
    su una chiave non univoca (classe B, integrazione al mandato di samantha_1
    da finding samantha_2: ogni ack di aurelio attribuito a 'aurelio_1' per
    tre giorni perche' il DB restituiva il primo record che condivideva la
    finestra).

    Regole:
    - Esclude i record VIRTUALI (pool_di, alias_di): condividono per
      costruzione la finestra con la propria istanza _1; il record vero e'
      l'istanza, mai il virtuale.
    - MAI fetchone su chiave non univoca: se restano PIU' righe reali (es.
      gemelli orfani, tipo aurelio_1 + copia), lo DICE — ritorna l'elenco,
      non ne sceglie una.

    Ritorna:
        (agent_id, None)          -> esattamente una riga reale: risolto.
        (None, [id1, id2, ...])   -> AMBIGUO: piu' record reali sulla finestra.
        (None, [])                -> nessun record (finestra non registrata).
    """
    cur.execute(
        "SELECT agent_id FROM agents "
        "WHERE data->>'tmux_window' = %s "
        "AND COALESCE(data->>'pool_di','') = '' "
        "AND COALESCE(data->>'alias_di','') = '';", (finestra,))
    righe = cur.fetchall()
    if len(righe) == 1:
        return righe[0][0], None
    return None, [r[0] for r in righe]


def finestre_sessione_per_nome(sessione, nome_win, timeout=20):
    """Ritorna la lista dei window_id (es. ['@594', '@595']) che matchano ESATTAMENTE
    il nome_win nella sessione specificata. Non fa match per prefisso."""
    r = subprocess.run(
        ["tmux", "list-windows", "-t", sessione, "-F", "#{window_id} #{window_name}"],
        capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        return []
    out = []
    for line in r.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(" ", 1)
        win_id = parts[0]
        win_name = parts[1] if len(parts) > 1 else ""
        if win_name == nome_win:
            out.append(win_id)
    return out


def pane_pids_finestra_id(win_id, timeout=20):
    """Ritorna tutti i PID dei pane appartenenti allo specifico window_id (@id)."""
    r = subprocess.run(
        ["tmux", "list-panes", "-t", win_id, "-F", "#{pane_pid}"],
        capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        return []
    pids = []
    for p in r.stdout.split():
        try:
            pids.append(int(p))
        except ValueError:
            pass
    return pids


def ottieni_albero_pids(root_pids):
    """Dato un PID o una lista/set di PID, ritorna l'insieme di tutti i PID
    e di tutti i loro processi discendenti (figli, nipoti, ecc.)."""
    if isinstance(root_pids, (int, str)):
        root_pids = [root_pids]

    tutti = set()
    coda = []
    for p in root_pids:
        try:
            ip = int(p)
            tutti.add(ip)
            coda.append(ip)
        except (ValueError, TypeError):
            pass

    while coda:
        curr = coda.pop(0)
        children = set()
        task_dir = f"/proc/{curr}/task"
        if os.path.isdir(task_dir):
            try:
                for tid in os.listdir(task_dir):
                    ch_path = os.path.join(task_dir, tid, "children")
                    if os.path.isfile(ch_path):
                        with open(ch_path, "r") as f:
                            for ch in f.read().split():
                                try:
                                    children.add(int(ch))
                                except ValueError:
                                    pass
            except Exception:
                pass
        if not children:
            try:
                out = subprocess.getoutput(f"ps --ppid {curr} -o pid=")
                for line in out.split():
                    try:
                        children.add(int(line))
                    except ValueError:
                        pass
            except Exception:
                pass
        for ch in children:
            if ch not in tutti:
                tutti.add(ch)
                coda.append(ch)
    return tutti


def pid_vivo(pid):
    """Ritorna True se il PID è attivo e non zombie."""
    try:
        with open(f"/proc/{pid}/status", "r") as f:
            for line in f:
                if line.startswith("State:"):
                    state = line.split()[1]
                    return state != "Z"
        return True
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        return False


def pids_vivi(pids):
    """Ritorna la lista dei PID ancora vivi tra quelli specificati."""
    return [p for p in pids if pid_vivo(p)]


def termina_processi(pids, timeout_grace=0.8):
    """Invia SIGTERM e se necessario SIGKILL ai PID specificati per assicurare
    la loro completa terminazione."""
    ancora = pids_vivi(pids)
    if not ancora:
        return []
    # Prima SIGTERM
    for p in ancora:
        try:
            os.kill(p, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except Exception:
            pass
    time.sleep(timeout_grace)
    ancora = pids_vivi(ancora)
    if not ancora:
        return []
    # Poi SIGKILL se necessario
    for p in ancora:
        try:
            os.kill(p, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except Exception:
            pass
    time.sleep(0.3)
    return pids_vivi(ancora)


def chiudi_e_verifica_finestre(sessione, nome_win, timeout=20):
    """Chiude tutte le finestre corrispondenti a nome_win nella sessione tmux,
    terminando ogni finestra per window_id univoco (@id), raccogliendo e
    verificando i PID dei processi, e certificando che nessuna finestra o processo
    rimanga vivo.

    Ritorna un dict di esito:
        {
            "ok": bool,
            "gia_spento": bool,
            "sessione": sessione,
            "nome": nome_win,
            "finestre_spente": int,
            "window_ids": list,
            "pids_raccolti": list,
            "pids_vivi": list,
            "finestre_vive": list,
            "sessione_chiusa": bool,
            "errore": str o None
        }
    """
    win_ids = finestre_sessione_per_nome(sessione, nome_win, timeout=timeout)
    if not win_ids:
        # Nessuna finestra trovata: l'agente è già spento
        return {
            "ok": True,
            "gia_spento": True,
            "sessione": sessione,
            "nome": nome_win,
            "finestre_spente": 0,
            "window_ids": [],
            "pids_raccolti": [],
            "pids_vivi": [],
            "finestre_vive": [],
            "sessione_chiusa": False,
            "errore": None,
        }

    # 1. Raccogliamo i PID di tutti i pane e l'intero albero di processi figli
    pids_pane = []
    for wid in win_ids:
        pids_pane.extend(pane_pids_finestra_id(wid, timeout=timeout))

    albero_pids = ottieni_albero_pids(pids_pane)

    # 2. Kill mirato per ciascun window_id univoco (@id)
    for wid in win_ids:
        subprocess.run(["tmux", "kill-window", "-t", wid],
                       capture_output=True, text=True, timeout=timeout)

    time.sleep(0.5)

    # 3. Se ci sono PID ancora vivi, forziamo la terminazione (SIGTERM -> SIGKILL)
    pids_residui = termina_processi(albero_pids)

    # 4. Gestione sessione: se vuota o terminata, chiudi sessione
    sessione_chiusa = False
    r_sess = subprocess.run(["tmux", "list-windows", "-t", sessione],
                            capture_output=True, text=True, timeout=timeout)
    if r_sess.returncode != 0 or not r_sess.stdout.strip():
        subprocess.run(["tmux", "kill-session", "-t", sessione],
                       capture_output=True, text=True, timeout=timeout)
        sessione_chiusa = True

    # 5. Verifica POST-azione rigorosa
    finestre_residue = []
    if not sessione_chiusa:
        finestre_residue = finestre_sessione_per_nome(sessione, nome_win, timeout=timeout)

    pids_vivi_finali = pids_vivi(albero_pids)

    ok = (len(finestre_residue) == 0 and len(pids_vivi_finali) == 0)
    errore = None
    if not ok:
        errs = []
        if finestre_residue:
            errs.append(f"Finestre ancora presenti: {finestre_residue}")
        if pids_vivi_finali:
            errs.append(f"PID ancora vivi: {pids_vivi_finali}")
        errore = "; ".join(errs)

    return {
        "ok": ok,
        "gia_spento": False,
        "sessione": sessione,
        "nome": nome_win,
        "finestre_spente": len(win_ids),
        "window_ids": win_ids,
        "pids_raccolti": sorted(list(albero_pids)),
        "pids_vivi": pids_vivi_finali,
        "finestre_vive": finestre_residue,
        "sessione_chiusa": sessione_chiusa,
        "errore": errore,
    }
