#!/usr/bin/env python3
"""SENSORE MEMORIA LEGGERA — DEV-110 §12-quater.

A COSA SERVE
------------
Chi deve decidere il proprio rinfresco e' SEMPRE quello con meno margine per
accorgersene, e per costruzione: essere pieni di contesto ed essere occupati
sono la stessa cosa. Il sensore guarda da fuori, e guarda ARTEFATTI SU DISCO
(dimensione del transcript, mtime del file di stato): non chiede a nessuno
quanto si senta pieno, perche' quel dato auto-dichiarato non vale niente — e'
lo stesso denominatore ignoto che rendeva inutile il "50% di contesto".

COSA MISURA
-----------
1. RITARDO SALVA-STATO: l'agente lavora da N ore e il suo file di stato e' piu'
   vecchio dell'inizio della sessione. Se cade adesso, perde tutto.
2. CONTESTO: byte del transcript SEMPRE, token QUANDO il sistema li espone,
   sulla STESSA riga di log. Non e' ridondanza: serve a calibrare fra qualche
   giorno la soglia in byte sui dati veri, invece di indovinare adesso un
   fattore di conversione che diventerebbe la prossima istruzione superata.
3. RINFRESCHI: quando il conv_id di un agente cambia rispetto all'osservazione
   precedente, e' un rinfresco. Si registra chi, quando, a che dimensione, e
   se lo stato era stato salvato PRIMA o DOPO.
4. MODELLO IN ESERCIZIO (09/08/2026, ticket BGA): letto da `message.model`
   nell'ultimo blocco assistant non-sidechain del transcript — un artefatto,
   non un'autodichiarazione. Confrontato col campo Postgres `data->>'model'`
   (l'atteso): se divergono, e' la prova oggettiva che un rilancio non ha
   applicato il `--model`, o che il processo vivo e' partito prima del fix.
   Non decide nulla da solo qui: e' calibrazione/verifica, si vede nel log.

PERCHE' CRON E NON UN DEMONE (decisione di Betty, dominio suo)
--------------------------------------------------------------
E' una lettura periodica senza stato da tenere fra un giro e l'altro. Un
demone servirebbe solo se dovesse ricordare qualcosa: sarebbe un processo in
piu' da sorvegliare, che muore in silenzio e di cui ci si accorge quando
serve. Il cron riparte da solo per costruzione.
La memoria fra un giro e l'altro, dove serve (rinfreschi, anti-ripetizione dei
segnali), si legge dal PROPRIO LOG, che e' un artefatto — non da uno stato
tenuto in RAM. Stesso principio di tutto il disegno.

Riga di cron consigliata (la installa BGA, non l'autore):
    */15 * * * * cd /root/ecosistema_agenti/core && ./venv/bin/python3 \
        sensore_memoria_leggera.py --segnala >> /dev/null 2>&1

SENZA --segnala il sensore MISURA E SCRIVE ma non manda niente a nessuno:
serve per farlo girare in osservazione qualche giro prima di dargli voce.

⚠️ LIMITE DICHIARATO, non da scoprire dopo
-------------------------------------------
Finche' la casella postale di DEV-110 non esiste, il segnale viaggia su
send_a2a verso una finestra tmux: LO STESSO CANALE CHE STIAMO RIPARANDO, con
la stessa consegna non verificabile. E' accettabile, ma va detto: se il
segnale non arriva, il sensore non se ne accorge. E' anche il terzo argomento
a favore dell'inbox.

⚠️ SOGLIE PROVVISORIE
---------------------
Larghe di proposito e da ricalibrare quando ci saranno i dati:
SBAGLIARE IN ECCESSO COSTA RUMORE, SBAGLIARE IN DIFETTO COSTA UNA MEMORIA.
"""

import argparse
import datetime
import fcntl
import json
import os
import subprocess
import sys

sys.path.insert(0, "/root/ecosistema_agenti/core")
import psycopg2
import engine_adapter

DSN = "dbname=betty_db user=betty_user host=localhost"
BASE = "/root/ecosistema_agenti/core"
LOCK = os.path.join(BASE, "sensore_memoria_leggera.lock")
LOG = os.path.join(BASE, "sensore_memoria_leggera.log")
OSSERVAZIONI = os.path.join(BASE, "sensore_osservazioni.jsonl")

# --- SOGLIE (vedi docstring) ---------------------------------------------
# Token: calibrata il 24/08/2026 (mandato 1M token, Manfredo) a 750k (75% di 1M
# di finestra reale), allineando il sensore alla nuova scala di contesto della flotta.
SOGLIA_TOKEN = 750_000
# Byte: derivata da ~5,8 byte/token su 750k token (~4.35 MB, arrotondato a 4.5 MB).
SOGLIA_BYTE = 4_500_000
# Ore di lavoro oltre le quali uno stato non salvato e' un problema.
SOGLIA_ORE_SENZA_SALVATAGGIO = 3.0
# ⚠️ Oltre questa inattivita' l'agente e' considerato FERMO e non viene
# valutato ne' segnalato.
# Perche' esiste: al primo giro il sensore ha annunciato "lavora da 319 ore"
# per una ventina di agenti. Non lavoravano da 319 ore: erano SPENTI da 319
# ore, e quello era il tempo dalla NASCITA della sessione. Misurare l'inizio
# invece dell'ultima attivita' e' di nuovo "la misura giusta sul soggetto
# sbagliato" — e avrebbe svegliato venti agenti dormienti al primo giro,
# bruciando la credibilita' del sensore prima ancora di usarlo.
# Un agente fermo non perde niente restando fermo: il rischio nasce quando
# lavora.
SOGLIA_ORE_INATTIVITA = 2.0
# Non ripetere lo stesso segnale allo stesso agente prima di N ore.
ORE_SILENZIO_DOPO_SEGNALE = 4.0

VAULT_PROGETTI = "/root/Obsidian-Vault/Progetti"

# Stessa radice usata da guardiano.py::_pendenti_a2a — non una copia, la
# convenzione e' quella (a2a/<slug>/{inbox,read}).
STORE_A2A_ROOT = "/root/ecosistema_agenti/a2a"

# Deroga "riservato": materia clinica/riservata salvata fuori dal Vault
# (es. l'agente valerio scrive il proprio stato qui, vedi system_prompt).
RISERVATO = "/root/ecosistema_agenti/riservato"

# Recap che il motore Telegram ha provato a mandare tre volte e ha spostato di
# lato. Al 09/08/2026 ce n'erano 259 accumulati in due giorni senza che nessuno
# se ne accorgesse: la consegna fallita e' oggi l'unica cosa che fallisce e non
# lo dice a nessuno — proprio il canale che serve a dirci le cose.
#
# Perche' lo conta il sensore e non il motore, che pure saprebbe di aver
# fallito: il motore l'artefatto lo produce gia' (il file). Non serve che
# parli lui, serve che qualcuno guardi. E un osservatore ESTERNO e' piu'
# robusto di un allarme interno — se il motore muore, un allarme dentro il
# motore tace insieme a lui, cioe' proprio quando i recap smettono di partire
# del tutto. Toccare il motore, oltretutto, vorrebbe dire riavviare l'unico
# canale con Manfredo per aggiungerci un allarme.
DIR_RECAP_FALLITI = "/tmp/betty_recaps/_failed"


def conta_recap_falliti():
    """(totali, nuovi_dall_ultimo_giro). Il confronto si legge dal PROPRIO
    log, come tutto il resto: nessuno stato tenuto fra un'esecuzione e l'altra.
    `nuovi` e' None al primo giro, che e' diverso da zero e va detto."""
    try:
        totali = sum(1 for n in os.listdir(DIR_RECAP_FALLITI)
                     if os.path.isfile(os.path.join(DIR_RECAP_FALLITI, n)))
    except OSError:
        return None, None

    precedente = None
    if os.path.exists(LOG):
        try:
            with open(LOG, "r", encoding="utf-8", errors="replace") as f:
                for riga in f.readlines()[-200:]:
                    if "failed_totali=" in riga:
                        try:
                            precedente = int(riga.split("failed_totali=", 1)[1].split()[0])
                        except (ValueError, IndexError):
                            pass
        except OSError:
            pass
    nuovi = None if precedente is None else max(0, totali - precedente)
    return totali, nuovi


def adesso():
    return datetime.datetime.now()


def _candidati_stato(agent_id, data):
    """Dove puo' stare il file di stato di questo agente.
    NOTA: oggi la convenzione e' <slug>.md in Progetti/, ma i prompt citano
    alla lettera '<tuo_slug>' (segnaposto mai sostituito), quindi ogni agente
    sceglie da se'. Si guardano piu' candidati e si prende quello che esiste."""
    slug = agent_id.lower()
    cand = []
    esplicito = (data or {}).get("project_file")
    if esplicito:
        cand.append(esplicito)
    cand += [
        os.path.join(RISERVATO, f"{slug}_stato.md"),
        os.path.join(RISERVATO, f"{slug}.md"),
        os.path.join(VAULT_PROGETTI, f"{slug}.md"),
        os.path.join(VAULT_PROGETTI, f"{slug}_stato.md"),
    ]
    return cand


def stato_agente(agent_id, data):
    """(percorso, mtime) del file di stato piu' recente fra i candidati."""
    migliore, mtime_migliore = None, None
    for p in _candidati_stato(agent_id, data):
        try:
            m = os.path.getmtime(p)
        except OSError:
            continue
        if mtime_migliore is None or m > mtime_migliore:
            migliore, mtime_migliore = p, m
    return migliore, mtime_migliore


def misura_transcript(percorso):
    """(byte, mtime, token_o_None, inizio_sessione_o_None, modello_o_None).

    I token si leggono dall'ultimo blocco `usage` del transcript quando
    l'engine li espone (Claude Code): input + cache_creation + cache_read e'
    la dimensione vera del contesto a quel turno. Quando non ci sono, restano
    i byte — ed e' esattamente per questo che si loggano SEMPRE entrambi.

    Il MODELLO segue lo stesso principio (09/08/2026, ticket BGA sul
    `--model` mai applicato al ramo claude): si legge da `message.model`
    dello stesso blocco assistant non-sidechain, MAI dal campo Postgres
    `data->>'model'` e MAI chiesto all'agente. Un modello che dichiara da
    solo il proprio nome non e' una misura — e' esattamente il campo
    Postgres che questo controllo deve verificare dall'ESTERNO: se il
    `--model` sopravvive a un rilancio automatico, qui deve leggersi il
    modello nuovo; se il rilancio lo perde, qui si rilegge il vecchio, e la
    divergenza rispetto a Postgres lo dice da sola (vedi confronto in
    main())."""
    try:
        st = os.stat(percorso)
    except OSError:
        return None, None, None, None, None

    token, inizio = None, None
    try:
        with open(percorso, "r", encoding="utf-8", errors="replace") as f:
            righe = f.readlines()
    except OSError:
        return st.st_size, st.st_mtime, None, None, None

    for r in righe[:40]:
        try:
            ev = json.loads(r)
        except Exception:
            continue
        ts = ev.get("timestamp")
        if ts:
            try:
                inizio = datetime.datetime.fromisoformat(
                    ts.replace("Z", "+00:00")).timestamp()
                break
            except Exception:
                pass
    if inizio is None:
        # ripiego: la nascita del file. Meno preciso ma sempre disponibile.
        inizio = getattr(st, "st_ctime", None)

    # ⚠️ Si prende l'ULTIMO blocco usage, NON la somma della sessione.
    # La somma misura il traffico cumulato: cresce sempre e non torna indietro
    # nemmeno dopo un rinfresco, quindi un agente appena rinfrescato
    # risulterebbe pienissimo e verrebbe segnalato subito DOPO aver fatto la
    # cosa giusta — un allarme che suona forte proprio quando va tutto bene.
    # Misurato su una sessione reale: somma 70.539.468 token contro 343.141 di
    # contesto vero, cioe' 205 volte tanto.
    # input + cache_creation + cache_read = il prompt di quel turno, cioe' il
    # contesto effettivamente in finestra. L'output no: diventa input del turno
    # dopo, e contarlo qui sarebbe contarlo due volte.
    modello = None
    for r in reversed(righe):
        if '"usage"' not in r:
            continue
        try:
            ev = json.loads(r)
        except Exception:
            continue
        # Le voci dei subagenti (sidechain) hanno un contesto tutto loro, piu'
        # piccolo: prenderle vorrebbe dire misurare il contesto di qualcun
        # altro e crederlo quello dell'agente. Oggi non ne ho trovate nei
        # transcript della flotta, ma la guardia non costa niente e toglie di
        # mezzo la stessa classe di errore.
        if ev.get("isSidechain"):
            continue
        msg = ev.get("message") or {}
        # ⚠️ TROVATO 09/08/2026 mentre aggiungevo la lettura del modello:
        # i messaggi iniettati dal sistema (es. avviso di limite raggiunto)
        # hanno `model: "<synthetic>"` e un blocco `usage` PRESENTE ma tutto
        # a zero — non un turno vero. Il controllo `if u:` da solo non li
        # esclude (un dict con valori a zero non e' un dict vuoto), quindi
        # senza questo filtro il piu' recente `<synthetic>` diventava
        # "l'ultimo turno": token letto 0 invece del vero contesto, e
        # modello letto '<synthetic>' invece di quello reale. Bug
        # preesistente sui token, scoperto per un motivo diverso da quello
        # per cui lo cercavo: la stessa classe di errore delle sidechain,
        # un'altra fonte che si finge un turno e non lo e'.
        if msg.get("model") == "<synthetic>":
            continue
        u = msg.get("usage") or {}
        if u:
            token = (u.get("input_tokens", 0) or 0) \
                + (u.get("cache_creation_input_tokens", 0) or 0) \
                + (u.get("cache_read_input_tokens", 0) or 0)
            modello = msg.get("model")
            break

    return st.st_size, st.st_mtime, token, inizio, modello


def ultima_osservazione(agent_id):
    """Ultima riga del PROPRIO log per questo agente. E' cosi' che il sensore
    ricorda senza tenere stato: rilegge un artefatto."""
    if not os.path.exists(OSSERVAZIONI):
        return None
    ultima = None
    try:
        with open(OSSERVAZIONI, "r", encoding="utf-8", errors="replace") as f:
            for riga in f:
                if f'"agente": "{agent_id}"' not in riga:
                    continue
                try:
                    ultima = json.loads(riga)
                except Exception:
                    continue
    except OSError:
        return None
    return ultima


def ultimo_segnale(agent_id):
    """Ultimo record di questo agente con un segnale EFFETTIVAMENTE emesso
    (segnalato_alle non nullo). Il dedup deve guardare QUI, non all'ultimo
    record in assoluto: un record 'silenzioso' (taciuto, senza segnalato_alle)
    non significa 'mai segnalato' — bug 22/08/2026 (publio_1): la memoria del
    segnale spariva e l'ordine tornava ogni ~20 min invece che ogni 4h."""
    if not os.path.exists(OSSERVAZIONI):
        return None
    ultimo = None
    try:
        with open(OSSERVAZIONI, "r", encoding="utf-8", errors="replace") as f:
            for riga in f:
                if f'"agente": "{agent_id}"' not in riga:
                    continue
                try:
                    dati = json.loads(riga)
                except Exception:
                    continue
                if dati.get("segnalato_alle"):
                    ultimo = dati
    except OSError:
        return None
    return ultimo


def taciuto(agent_id, t0):
    """Anti-ripetizione del segnale: True se l'ULTIMO segnale EFFETTIVO di
    questo agente (record con segnalato_alle non nullo, non l'ultimo record
    in assoluto) e' dentro ORE_SILENZIO_DOPO_SEGNALE: si tacita, niente nuovo
    ordine. Ritorna (taciuto, ore_trascorse)."""
    prec = ultimo_segnale(agent_id)
    if not prec or not prec.get("segnalato_alle"):
        return False, None
    try:
        trascorse = (t0 - datetime.datetime.fromisoformat(
            prec["segnalato_alle"])).total_seconds() / 3600.0
    except Exception:
        return False, None
    return trascorse < ORE_SILENZIO_DOPO_SEGNALE, trascorse


def _ha_lavoro_nuovo_dal(agent_id, riferimento_ts):
    """True se questo agente ha ricevuto almeno un messaggio A2A (inbox o
    read) posteriore a riferimento_ts, che NON sia un allarme del sensore
    stesso.

    PERCHE' QUESTO CONTROLLO (segnalazione cosimo_1, 01/09/2026): senza,
    'ore_lavoro' misura ore-da-INIZIO-SESSIONE e non si resetta mai, e il
    gate 'attivo' richiede solo che il transcript sia stato toccato nelle
    ultime SOGLIA_ORE_INATTIVITA ore — condizione che un agente idle ma
    vivo (che risponde a un ping, o riceve l'ordine di questo stesso
    sensore) soddisfa comunque. Risultato: un agente semplicemente in
    attesa, senza nuovi task, veniva segnalato ad ogni giro di cron per
    ore, perche' "e' passato tempo dall'inizio sessione" veniva letto come
    "sta lavorando ininterrottamente" — non e' la stessa cosa. Verificato
    su cosimo (studio ricerca_lavoro): idle dal pomeriggio precedente,
    risposta al ping in <1 minuto, non piantato.

    L'esclusione dei messaggi 'A2A_FROM:sensore_memoria' e' necessaria
    per lo stesso motivo: senza, il proprio ordine di salvare lo stato
    diventerebbe "nuovo lavoro ricevuto" al giro di cron successivo, e
    l'allarme si autoalimenterebbe all'infinito invece di spegnersi.

    riferimento_ts None -> True (nessun riferimento utile: non si puo'
    escludere nulla, si mantiene il comportamento precedente per non
    introdurre falsi negativi su casi non previsti)."""
    if riferimento_ts is None:
        return True
    slug = agent_id.lower()
    for sotto in ("inbox", "read"):
        d = os.path.join(STORE_A2A_ROOT, slug, sotto)
        try:
            nomi = os.listdir(d)
        except OSError:
            continue
        for n in nomi:
            p = os.path.join(d, n)
            try:
                if os.path.getmtime(p) <= riferimento_ts:
                    continue
            except OSError:
                continue
            try:
                with open(p, "r", encoding="utf-8", errors="replace") as f:
                    intestazione = f.readline()
            except OSError:
                continue
            if "A2A_FROM:sensore_memoria" in intestazione:
                continue
            return True
    return False


def coordinatore_di(studio, tutti):
    """Finestra tmux del coordinatore dello studio, o None."""
    for aid, data in tutti:
        d = data or {}
        if d.get("studio") != studio:
            continue
        if "coordinat" in (d.get("role") or "").lower():
            return d.get("tmux_window")
    return None


def manda(finestra, testo, esegui):
    """Invio A2A. NOTA: non si scrive MAI in /tmp/betty_recaps — il sensore
    non parla a Manfredo, per nessun motivo. I destinatari sono l'agente
    (che e' l'unico che puo' agire), il suo coordinatore (che deve vedere,
    non fare da tramite) e Betty solo come ripiego."""
    if not finestra:
        return False, "nessuna finestra"
    if not esegui:
        return False, "non inviato (manca --segnala)"
    try:
        r = subprocess.run(
            [os.path.join(BASE, "venv/bin/python3"),
             os.path.join(BASE, "send_a2a.py"), finestra, testo],
            capture_output=True, text=True, timeout=120)
        return r.returncode == 0, (r.stdout or r.stderr).strip()[:200]
    except Exception as e:
        return False, f"errore invio: {e}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--segnala", action="store_true",
                    help="manda davvero i segnali (senza, misura e scrive soltanto)")
    ap.add_argument("--verboso", action="store_true")
    ap.add_argument("--soglia-inattivita", type=float, default=SOGLIA_ORE_INATTIVITA,
                    metavar="ORE",
                    help="oltre quante ore di inattivita' un agente e' considerato fermo "
                         "(default %(default)s). Serve per la calibrazione e per la "
                         "controprova: alzandola i fermi rientrano nella valutazione.")
    args = ap.parse_args()
    soglia_inattivita = args.soglia_inattivita

    # Guardia single-instance: cron o no, due giri sovrapposti si pestano.
    f_lock = open(LOCK, "w")
    try:
        fcntl.flock(f_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.stderr.write("sensore gia' in esecuzione: esco.\n")
        return 0
    f_lock.write(str(os.getpid()))
    f_lock.flush()

    con = psycopg2.connect(DSN)
    cur = con.cursor()
    cur.execute("SELECT agent_id, data FROM agents WHERE data->>'dismesso' IS DISTINCT FROM 'true' ORDER BY agent_id")
    tutti = cur.fetchall()
    con.close()

    t0 = adesso()
    controllati, in_ritardo, rinfreschi, segnali = 0, [], [], []
    osservazioni = []
    fermi = []
    non_coperti = []
    modello_divergenti = []

    for agent_id, data in tutti:
        d = data or {}
        conv_id = d.get("conv_id")
        engine = engine_adapter.get_engine(d)
        work_dir = d.get("work_dir")
        # I SALTI NON SONO PIU' MUTI (09/08/2026).
        # Il sensore legge tutti e 96 gli agenti e poi ne scarta la maggior
        # parte: prima lo faceva in silenzio, quindi "va tutto bene" e "non sto
        # guardando" erano indistinguibili. Ora ogni salto ha un motivo e
        # finisce nel conteggio del log.
        # Vale lo stesso principio della riga a zero ritardi: UNO ZERO SCRITTO
        # VALE QUANTO UN ALLARME — se un giorno non e' zero, questo e' l'unico
        # posto dove si vedra'.
        if not conv_id:
            non_coperti.append((agent_id, "senza_conv_id"))
            continue
        percorso = engine_adapter.transcript_path(engine, conv_id, work_dir)
        if not percorso or not os.path.exists(percorso):
            non_coperti.append((agent_id, "transcript_assente"))
            continue

        byte, mtime_tr, token, inizio, modello_transcript = misura_transcript(percorso)
        if byte is None:
            continue
        controllati += 1

        # Confronto modello: SOLO per calibrare/verificare, mai per decidere
        # nulla di operativo qui. modello_atteso viene da Postgres (quello che
        # build_launch_cmd avrebbe dovuto applicare); modello_transcript e'
        # l'artefatto vero. Se divergono con entrambi presenti, e' la prova
        # oggettiva che il flag non e' sopravvissuto a un rilancio (o non e'
        # ancora stato applicato perche' il processo vivo e' precedente al
        # fix) — non e' un'autodichiarazione, e' quello che l'engine ha
        # scritto da solo nel transcript.
        modello_atteso = d.get("model")
        modello_diverge = bool(
            modello_atteso and modello_transcript
            and modello_atteso not in modello_transcript
            and modello_transcript not in modello_atteso
        )

        file_stato, mtime_stato = stato_agente(agent_id, d)
        ore_inattivo = (t0.timestamp() - mtime_tr) / 3600.0
        attivo = ore_inattivo < soglia_inattivita
        # Ore di LAVORO = dall'inizio sessione all'ultima attivita' vera, non
        # a "adesso": per un agente fermo da giorni le due cose differiscono
        # di giorni, ed e' la differenza che ci aveva ingannati.
        ore_lavoro = ((mtime_tr - inizio) / 3600.0) if inizio else None
        stato_vecchio = (mtime_stato is None) or (inizio and mtime_stato < inizio)

        oss = {
            "quando": t0.isoformat(timespec="seconds"),
            "agente": agent_id,
            "engine": engine,
            "conv_id": conv_id,
            "transcript_byte": byte,          # SEMPRE
            "transcript_token": token,        # quando esposti
            "ore_lavoro": round(ore_lavoro, 2) if ore_lavoro else None,
            "ore_inattivo": round(ore_inattivo, 2),
            "attivo": attivo,
            "file_stato": file_stato,
            "stato_mtime": (datetime.datetime.fromtimestamp(mtime_stato)
                            .isoformat(timespec="seconds") if mtime_stato else None),
            "stato_piu_vecchio_della_sessione": bool(stato_vecchio),
            "modello_transcript": modello_transcript,  # artefatto letto dal transcript
            "modello_atteso_postgres": modello_atteso,  # autodichiarazione DB, solo per confronto
            "modello_diverge": modello_diverge,
        }
        if modello_diverge:
            modello_divergenti.append(agent_id)

        # --- rinfresco: conv_id cambiato rispetto all'ultima osservazione ---
        prec = ultima_osservazione(agent_id)
        if prec and prec.get("conv_id") and prec["conv_id"] != conv_id:
            rinfreschi.append({
                "agente": agent_id,
                "quando": oss["quando"],
                "conv_id_precedente": prec["conv_id"],
                "conv_id_nuovo": conv_id,
                "byte_prima_del_rinfresco": prec.get("transcript_byte"),
                "token_prima_del_rinfresco": prec.get("transcript_token"),
                "stato_era_salvato": not prec.get("stato_piu_vecchio_della_sessione", True),
            })
            oss["rinfresco_rilevato"] = True

        # --- valutazione: solo per chi sta lavorando davvero ---
        if not attivo:
            oss["motivi"] = []
            oss["escluso_perche"] = f"fermo da {ore_inattivo:.1f} ore"
            fermi.append(agent_id)
            osservazioni.append(oss)
            continue

        motivi = []
        if token is not None and token >= SOGLIA_TOKEN:
            motivi.append(f"contesto {token:,} token (soglia {SOGLIA_TOKEN:,})")
        elif token is None and byte >= SOGLIA_BYTE:
            motivi.append(f"transcript {byte/1e6:.1f} MB (soglia provvisoria "
                          f"{SOGLIA_BYTE/1e6:.1f} MB; token non esposti da questo engine)")
        if stato_vecchio and ore_lavoro and ore_lavoro >= SOGLIA_ORE_SENZA_SALVATAGGIO:
            # Riferimento per "c'e' stato nuovo lavoro?": dall'ultimo
            # salvataggio se esiste, altrimenti dall'inizio sessione. Senza
            # nuovo lavoro dal riferimento, il file di stato vecchio e' la
            # cosa NORMALE per un agente idle, non un sintomo (cosimo_1,
            # 01/09/2026: vedi _ha_lavoro_nuovo_dal).
            riferimento = mtime_stato if mtime_stato is not None else inizio
            lavoro_nuovo = _ha_lavoro_nuovo_dal(agent_id, riferimento)
            oss["lavoro_nuovo_dal_riferimento"] = lavoro_nuovo
            if lavoro_nuovo:
                motivi.append(f"lavora da {ore_lavoro:.1f} ore e il file di stato "
                              f"{'non esiste' if not file_stato else 'e piu vecchio dell inizio sessione'}")
            else:
                oss["escluso_perche_idle"] = (
                    f"lavora da {ore_lavoro:.1f} ore senza salvataggio ma nessun "
                    f"nuovo lavoro A2A dal riferimento: idle, non anomalo")

        oss["motivi"] = motivi
        osservazioni.append(oss)
        if not motivi:
            continue
        in_ritardo.append((agent_id, d, motivi, oss))

    # --- segnali (con anti-ripetizione letta dal proprio log) --------------
    # FIX 22/08/2026 (publio_1): il dedup guarda l'ULTIMO segnale EFFETTIVO
    # (record con segnalato_alle non nullo), non l'ultimo record in assoluto.
    # Un record "silenzioso" (taciuto, senza segnalato_alle) NON significa
    # "mai segnalato": senza il fix l'ordine tornava ogni ~20 min invece che
    # ogni 4h per chi e' sopra soglia.
    for agent_id, d, motivi, oss in in_ritardo:
        taciuto_si, ore_trascorse = taciuto(agent_id, t0)
        if taciuto_si:
            oss["segnale"] = f"taciuto (ultimo {ore_trascorse:.1f}h fa)"
            continue

        elenco = "; ".join(motivi)
        # All'AGENTE: un ORDINE. E' l'unico che puo' agire.
        testo_agente = (
            f"[A2A_FROM:sensore_memoria] [A2A_TYPE:task]\n"
            f"SALVA LO STATO ADESSO, prima di continuare.\n"
            f"Motivo: {elenco}.\n"
            f"Scrivi il tuo file di stato e RILEGGILO per verificare che ci sia davvero, "
            f"poi prosegui (o chiedi il rinfresco, se sei a fine lavoro).\n"
            f"Questo messaggio e' automatico: non rispondere, agisci."
        )
        ok_a, det_a = manda(d.get("tmux_window"), testo_agente, args.segnala)

        # Al COORDINATORE: VISIBILITA'. Deve vedere, non fare da tramite.
        finestra_coord = coordinatore_di(d.get("studio"), tutti)
        ok_c = None
        if finestra_coord and finestra_coord != d.get("tmux_window"):
            testo_coord = (
                f"[A2A_FROM:sensore_memoria] [A2A_TYPE:report]\n"
                f"Per tua visibilita' (nessuna azione richiesta a te): ho ordinato a "
                f"{agent_id} di salvare lo stato.\n"
                f"Motivo: {elenco}.\n"
                f"Dettaglio: {OSSERVAZIONI}"
            )
            ok_c, _ = manda(finestra_coord, testo_coord, args.segnala)

        oss["segnale"] = {"agente": ok_a, "dettaglio": det_a, "coordinatore": ok_c}
        if args.segnala and ok_a:
            oss["segnalato_alle"] = t0.isoformat(timespec="seconds")
        segnali.append(agent_id)

    # --- scritture: SEMPRE, anche quando non c'e' nessuno in ritardo -------
    # Un sensore che parla solo quando ha qualcosa da dire e' indistinguibile
    # da un sensore morto. La riga "0 in ritardo" E' la prova che sta girando.
    with open(OSSERVAZIONI, "a", encoding="utf-8") as f:
        for o in osservazioni:
            f.write(json.dumps(o, ensure_ascii=False) + "\n")
        for r in rinfreschi:
            f.write(json.dumps({"tipo": "rinfresco", **r}, ensure_ascii=False) + "\n")

    # Contatore dei recap non consegnati. Si scrive SEMPRE, anche a zero nuovi:
    # uno zero scritto vale quanto un allarme, perche' e' l'unica forma in cui
    # "nessun recap perso" e "non sto guardando" smettono di somigliarsi.
    falliti_totali, falliti_nuovi = conta_recap_falliti()

    durata = (adesso() - t0).total_seconds()
    riga = (f"{t0.isoformat(timespec='seconds')} controllati={controllati} "
            f"attivi={controllati - len(fermi)} fermi={len(fermi)} "
            f"in_ritardo={len(in_ritardo)} segnali_inviati={len(segnali) if args.segnala else 0} "
            f"rinfreschi_rilevati={len(rinfreschi)} non_coperti={len(non_coperti)} "
            f"modello_diverge={len(modello_divergenti)} "
            + (f"failed_totali={falliti_totali} "
               f"failed_nuovi={'primo_giro' if falliti_nuovi is None else falliti_nuovi} "
               if falliti_totali is not None
               else "failed_totali=cartella_non_leggibile ")
            + f"durata={durata:.1f}s "
            f"modo={'segnala' if args.segnala else 'osservazione'}")
    if in_ritardo:
        riga += " | in_ritardo: " + ", ".join(a for a, _, _, _ in in_ritardo)
    if modello_divergenti:
        riga += " | modello diverge: " + ", ".join(modello_divergenti)
    # I non coperti si riassumono per MOTIVO, non uno per uno: sessantina di
    # nomi ad ogni giro sarebbe rumore che fa smettere di leggere il log.
    if non_coperti:
        per_motivo = {}
        for a, m in non_coperti:
            per_motivo.setdefault(m, []).append(a)
        riga += " | non coperti: " + ", ".join(
            f"{m}={len(v)}" for m, v in sorted(per_motivo.items()))
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(riga + "\n")

    print(riga)
    if args.verboso:
        for o in osservazioni:
            if o.get("motivi"):
                print(f"  {o['agente']}: {'; '.join(o['motivi'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
