"""Collaudo DEV-110 §11 — non asserzioni astratte: i fatti osservabili
pretesi da Betty.
  1. un messaggio DEPOSITATO — il file esiste in inbox/, dichiarato senza
     aver letto il terminale;
  2. un messaggio LETTO — spostato in read/ da a2a_ack.py, verificabile con
     un ls a distanza di tempo;
  3. deposito indipendente dalla notifica — un agente reale ma OFFLINE (nessun
     pannello tmux vivo) non impedisce il deposito (exit resta 0);
  4. prova negativa — deposito strutturalmente impossibile: errore RUMOROSO,
     exit non-zero, NESSUN ripiego silenzioso;
  5. seguito 09/08 (bug reale trovato da BGA su 'betty:1'): NESSUNA casella
     orfana per uno slug che non corrisponde a un agente registrato — ne'
     nella forma 'sessione:finestra' ne' nella forma slug nudo."""
import os
import shutil
import subprocess
import sys

sys.path.append("/root/ecosistema_agenti/core")
import a2a_inbox

ok = True


def check(label, cond, extra=""):
    global ok
    status = "PASS" if cond else "FAIL"
    if not cond:
        ok = False
    print(f"[{status}] {label} {extra}")


SLUG_TEST = "test_dev110_collaudo"
INBOX = a2a_inbox.path_inbox(SLUG_TEST)
READ = a2a_inbox.path_read(SLUG_TEST)

# Agente REALE (registrato in Postgres) ma ARCHIVIATO/offline: nessun pannello
# tmux vivo con questo nome. Serve per §3 — dimostrare che il deposito riesce
# anche quando la notifica tmux fallisce, usando uno slug che la nuova
# validazione (punto 5) accetta comunque, perché esiste davvero in Postgres.
AGENTE_OFFLINE = "diana"
AGENTE_OFFLINE_WINDOW = "marketing:AGY-DIANA"


def pulisci():
    shutil.rmtree(os.path.join(a2a_inbox.BASE, SLUG_TEST), ignore_errors=True)
    shutil.rmtree(os.path.join(a2a_inbox.BASE, AGENTE_OFFLINE), ignore_errors=True)


pulisci()
try:
    # --- 1. DEPOSITATO --------------------------------------------------
    testo = "[A2A_FROM:test_mittente] [A2A_TYPE:task]\nCorpo di collaudo DEV-110, riga 2."
    a2a_id, path = a2a_inbox.deposita(SLUG_TEST, testo)
    check("depositato: il file esiste in inbox/", os.path.isfile(path), path)
    check("depositato: nome file coerente con l'id", os.path.basename(path).replace("__", "_") == f"{a2a_id}.md")
    with open(path, encoding="utf-8") as f:
        check("depositato: contenuto intatto (nessuna shell in mezzo)", f.read() == testo)

    # --- 2. LETTO (via a2a_ack.py, CLI reale — non la funzione diretta) --
    r = subprocess.run(
        [sys.executable, "a2a_ack.py", a2a_id],
        capture_output=True, text=True, cwd="/root/ecosistema_agenti/core")
    check("a2a_ack.py: exit 0", r.returncode == 0, r.stdout.strip() + r.stderr.strip())
    check("letto: il file NON è più in inbox/", not os.path.exists(path))
    path_letto = os.path.join(READ, os.path.basename(path))
    check("letto: il file è in read/ — verificabile con un ls, a distanza di tempo", os.path.isfile(path_letto))
    slug_ritrovato, path_ritrovato = a2a_inbox.trova_in_inbox(a2a_id)
    check("letto: non più trovabile in NESSUNA inbox (si è spostato, non duplicato)", path_ritrovato is None)

    # --- 3. Deposito indipendente dalla notifica -------------------------
    r2 = subprocess.run(
        [sys.executable, "send_a2a.py", AGENTE_OFFLINE_WINDOW,
         "[A2A_FROM:test_mittente] [A2A_TYPE:task]\nSeconda riga di corpo, agente reale ma offline."],
        capture_output=True, text=True, cwd="/root/ecosistema_agenti/core")
    check("send_a2a su agente reale ma offline: exit 0 (depositato comunque)",
          r2.returncode == 0, f"rc={r2.returncode} stdout={r2.stdout[-300:]} stderr={r2.stderr[-300:]}")
    check("send_a2a: dichiara DEPOSITATO", "DEPOSITATO" in r2.stdout, r2.stdout[-200:])
    check("send_a2a: MAI la parola 'Confermato' (bug §5 del disegno)", "Confermato" not in r2.stdout)
    check("send_a2a: la notifica NON è confermata (nessun pannello tmux vivo)",
          "notificato" not in r2.stdout.lower())
    inbox_offline = a2a_inbox.path_inbox(AGENTE_OFFLINE)
    depositati = os.listdir(inbox_offline) if os.path.isdir(inbox_offline) else []
    check("send_a2a: il file è comunque finito nell'inbox reale di diana", len(depositati) == 1, depositati)

    # --- 4. PROVA NEGATIVA: deposito strutturalmente impossibile ---------
    # ⚠️ chmod 000 sull'inbox NON basta come prova negativa in QUESTO
    # ambiente: questi processi girano come root, e root scavalca i permessi
    # DAC — un test costruito su chmod supererebbe silenziosamente senza
    # provare niente (falso verde). Serve un guasto che fallisca DAVVERO
    # anche per root: un FILE occupa il punto dove dovrebbe nascere inbox/,
    # cosi' os.makedirs non puo' riuscire per nessun utente — conflitto
    # strutturale, non un permesso aggirabile.
    shutil.rmtree(os.path.join(a2a_inbox.BASE, SLUG_TEST), ignore_errors=True)
    os.makedirs(os.path.join(a2a_inbox.BASE, SLUG_TEST), exist_ok=True)
    with open(INBOX, "w", encoding="utf-8") as f:
        f.write("blocco deliberato per il collaudo: qui dovrebbe esserci una CARTELLA")

    errore_sollevato = False
    eccezione = None
    try:
        a2a_inbox.deposita(SLUG_TEST, "[A2A_FROM:x] [A2A_TYPE:task]\nnon deve mai arrivare")
    except Exception as e:
        errore_sollevato = True
        eccezione = e
    check("deposita() con deposito impossibile: solleva (rumoroso), non ripiega silenziosamente",
          errore_sollevato, str(eccezione) if errore_sollevato else "NESSUNA ECCEZIONE — BUG")

    # --- 5. Seguito 09/08: nessuna casella orfana per uno slug non reale --
    # Bug trovato da BGA: 'betty:1' (stile finestra, senza corrispondenza in
    # Postgres) creava comunque una cartella 'betty-1/' — depositato con
    # successo, ma nessun agente reale controlla mai quella cartella. Ora
    # DEVE fallire, rumorosamente, in ENTRAMBE le forme del target.
    for target_finto, motivo in [
        ("betty:1", "stile finestra senza corrispondenza (il bug reale di BGA)"),
        ("agente-che-non-esiste-collaudo", "slug nudo senza corrispondenza"),
    ]:
        cartella_finta = os.path.join(a2a_inbox.BASE, target_finto.replace(":", "-"))
        shutil.rmtree(cartella_finta, ignore_errors=True)
        r5 = subprocess.run(
            [sys.executable, "send_a2a.py", target_finto,
             "[A2A_FROM:x] [A2A_TYPE:task]\nnon deve mai creare una casella orfana"],
            capture_output=True, text=True, cwd="/root/ecosistema_agenti/core")
        check(f"send_a2a su '{target_finto}' ({motivo}): exit NON-zero", r5.returncode != 0, f"rc={r5.returncode}")
        check(f"send_a2a su '{target_finto}': errore rumoroso, non un agente registrato",
              "non corrisponde a nessun agente registrato" in r5.stderr, r5.stderr[-300:])
        esiste_cartella_slug = os.path.isdir(os.path.join(a2a_inbox.BASE, target_finto))
        esiste_cartella_sanificata = os.path.isdir(cartella_finta)
        check(f"send_a2a su '{target_finto}': NESSUNA cartella orfana creata",
              not esiste_cartella_slug and not esiste_cartella_sanificata)

    # Stessa prova negativa, ma passando dalla CLI e con un AGENTE REALE
    # (diana, gia' validato al punto 3): cosi' l'errore che arriva su stderr
    # e' davvero quello del deposito (conflitto strutturale), non quello
    # della validazione slug appena provata sopra — i due guasti restano
    # distinguibili.
    shutil.rmtree(inbox_offline, ignore_errors=True)
    os.makedirs(os.path.dirname(inbox_offline), exist_ok=True)
    with open(inbox_offline, "w", encoding="utf-8") as f:
        f.write("blocco deliberato: qui dovrebbe esserci una CARTELLA")

    r3 = subprocess.run(
        [sys.executable, "send_a2a.py", AGENTE_OFFLINE_WINDOW,
         "[A2A_FROM:x] [A2A_TYPE:task]\nnon deve mai arrivare"],
        capture_output=True, text=True, cwd="/root/ecosistema_agenti/core")
    check("send_a2a CLI (agente reale) con deposito impossibile: exit NON-zero", r3.returncode != 0, f"rc={r3.returncode}")
    check("send_a2a CLI: errore rumoroso su stderr, non un successo silenzioso",
          "NON DEPOSITATO" in r3.stderr, r3.stderr[-300:])
    check("send_a2a CLI: NESSUN 'DEPOSITATO' stampato (nessun ripiego che finge successo)",
          "DEPOSITATO" not in r3.stdout, r3.stdout[-200:])

finally:
    pulisci()

print()
print("RISULTATO:", "TUTTI I TEST PASSANO" if ok else "TEST FALLITI")
sys.exit(0 if ok else 1)
