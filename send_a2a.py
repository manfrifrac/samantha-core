#!/usr/bin/env python3
"""
Invio A2A affidabile verso una finestra Tmux — con verifica e retry automatico.

Nato dal 04/08/2026: il motore Telegram (unified_telegram_engine.py,
send_prompt_to_tmux) ha sempre avuto un ciclo di verifica/retry robusto per i
messaggi in arrivo da Telegram, ma ogni agente che manda A2A ad un altro
tramite `tmux set-buffer`+`paste-buffer`+`send-keys C-m` a mano NON aveva
nessuna di queste garanzie — un Invio perso nella transizione
generazione->idle restava silenziosamente bloccato finché qualcuno non se ne
accorgeva controllando a mano (successo più volte in una sola sessione:
Leo, Publio, poi 10 agenti insieme dopo un mass-restart).

DEV-110 (09/08/2026): il verdetto di consegna non si legge più SOLO dal
terminale (volatile, condiviso, riscritto di continuo). Il corpo del
messaggio viene depositato su disco PRIMA di qualunque tentativo di notifica
(a2a_inbox.py) — quel deposito è la vera consegna, verificabile per
costruzione con un `ls`. Il paste-buffer in tmux resta, invariato nel
meccanismo, ma diventa solo un CAMPANELLO di due righe (path + azione): un
indizio che la notifica è partita, mai la prova che il messaggio sia arrivato
— quella prova la dà solo `a2a_ack.py` quando il destinatario lo legge.

Misura 2 (20/08/2026): guardia payload §4. Un corpo oltre MAX_RIGHE_A2A=5
(conteggiate al netto delle intestazioni `[A2A_...]` e delle righe vuote) NON
entra nella chat del destinatario: il testo pieno viene scritto in
/tmp/betty_docs/ e il messaggio riscritto conserva solo intestazioni + path +
brief. Chi vuole davvero il corpo pieno in chat usa --consenti-payload-lungo.

Uso:
    ./venv/bin/python3 send_a2a.py "<target_window>" "<testo del messaggio>"
    ./venv/bin/python3 send_a2a.py "<target_window>" --file /path/al/messaggio.txt

Ritorna exit code 0 se il messaggio è stato DEPOSITATO su disco (il
messaggio non è perso in nessun caso), non se è stato "ricevuto": quello
richiede a2a_ack.py da parte del destinatario. Se il deposito stesso
fallisce, l'errore è rumoroso e l'exit code è diverso da zero — NESSUN
ripiego sul vecchio comportamento (correzione di Betty, §9 punto 4 del
disegno: un guasto va reso rumoroso, mai ammorbidito).
"""
import re
import enum
import subprocess
import sys
import time
import argparse
import os
import uuid

CORE = os.path.dirname(os.path.abspath(__file__))
if CORE not in sys.path:
    sys.path.insert(0, CORE)
import a2a_inbox
import engine_adapter
import tmux_esatto

# --- misura 2 (20/08/2026): guardia payload A2A ------------------------------
# CLAUDE.md §4: "Niente payload pesanti nel messaggio. Log, listati, report
# lunghi → scrivili su file (/tmp/betty_docs/<nome>.md) e nell'A2A passa solo
# il path più due righe di brief." Applicata a codice QUI, prima del deposito:
# se il CORPO del messaggio supera MAX_RIGHE_A2A, il testo pieno finisce su
# file (destinazione obbligata BETTY_DOCS) e all'invio resta solo
# intestazione + path + brief. Reversibile con --consenti-payload-lungo.
MAX_RIGHE_A2A = 5
BETTY_DOCS = "/tmp/betty_docs"


def _run(cmd):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True)


def resolve_tmux_window(target: str) -> str:
    if ":" in target:
        return target
    try:
        import psycopg2
        conn = psycopg2.connect("dbname=betty_db user=betty_user host=localhost port=5432")
        cur = conn.cursor()
        cur.execute("SELECT data->>'tmux_window' FROM agents WHERE agent_id = %s;", (target,))
        row = cur.fetchone()
        conn.close()
        if row and row[0]:
            return row[0]
    except Exception as e:
        print(f"[send_a2a] ⚠️ Impossibile risolvere finestra per {target}: {e}", file=sys.stderr)
    return target


def resolve_recipient_slug(target: str) -> str:
    """Slug destinatario per il path della casella (§3): indipendente da come
    è stato specificato il target — slug nudo ('dario') o già una finestra
    tmux ('leo_studio:AGY-DARIO').

    DEV-110 seguito (09/08/2026, bug reale trovato da BGA sul target
    'betty:1'): NESSUN ripiego che crei una casella per uno slug senza un
    agente REALE dietro. La versione precedente, quando il target con ':'
    non trovava corrispondenza, sanificava la stringa e depositava comunque
    — 'DEPOSITATO' diceva il vero (il file esisteva), ma nessun agente ha
    mai controllato quella cartella al bootstrap: un guasto silenzioso
    identico a quello che DEV-110 esiste per chiudere, solo spostato di un
    livello (dalla consegna alla creazione della casella). Ora: si verifica
    SEMPRE contro Postgres, in ENTRAMBE le forme — anche lo slug nudo, che
    prima passava senza controllo. Se non risolve a un agent_id vero,
    l'errore risale (rumoroso), la casella non nasce."""
    import psycopg2
    conn = psycopg2.connect("dbname=betty_db user=betty_user host=localhost port=5432")
    try:
        cur = conn.cursor()
        if ":" in target:
            # 19/08/2026 (pool) + 22/08/2026 FIX classe B (integrazione al
            # mandato di samantha_1, finding samantha_2): i record VIRTUALI
            # (alias_di/pool_di) condividono per costruzione la finestra con
            # l'istanza — il target in forma-finestra indica l'ISTANZA, mai il
            # virtuale. Il resolver condiviso tmux_esatto esclude i virtuali e
            # NON usa fetchone su una chiave non univoca: se restano PIU'
            # record reali sulla stessa finestra lo DICE invece di sceglierne
            # uno a caso (è il bug per cui ogni ack di aurelio finiva su
            # 'aurelio_1' per tre giorni).
            agent, ambigui = tmux_esatto.risolvi_agente_da_finestra(target, cur)
            if ambigui is not None:
                if ambigui:
                    raise ValueError(
                        f"finestra '{target}' AMBIGUA: {len(ambigui)} record reali la "
                        f"condividono ({', '.join(ambigui)}) — non risolvo a colpi di "
                        f"fetchone, pulisci i record gemelli")
                raise ValueError(
                    f"'{target}' non corrisponde a nessun agente registrato in "
                    f"Postgres — nessuna casella creata per uno slug non verificato "
                    f"(sarebbe una cartella orfana che nessun bootstrap controlla mai)")
            row = (agent,)
        else:
            cur.execute("SELECT agent_id FROM agents WHERE agent_id = %s;", (target.lower(),))
            row = cur.fetchone()
    finally:
        conn.close()
    if row and row[0]:
        # ALIAS — UN solo hop (19/08/2026, progetto pool samantha, patch
        # approvata da Samantha db32 + Simone 9557): se il record risolto e'
        # un alias (data->>'alias_di'), la casella vera e' quella del target
        # e TUTTE le guardie a valle (gerarchia, dismesso) si riapplicano al
        # target reale. Bug reale che questa patch chiude, trovato da
        # exec_pool_samantha con un dry-run: 'betty' e' alias_di='samantha'
        # MA anche dismesso=true dal 13/08 — verifica_dismesso rifiutava OGNI
        # invio all'alias, che il rename del 15/08 doveva mantenere
        # funzionante. Un solo hop, mai catene: un alias che punta a un altro
        # alias e' un errore di configurazione e deve fallire rumoroso.
        slug_risolto = row[0]
        conn = psycopg2.connect("dbname=betty_db user=betty_user host=localhost port=5432")
        try:
            cur = conn.cursor()
            cur.execute("SELECT data->>'alias_di' FROM agents WHERE agent_id = %s;", (slug_risolto,))
            r_alias = cur.fetchone()
            alias_di = (r_alias[0] or "").strip().lower() if r_alias and r_alias[0] else ""
            if not alias_di:
                return slug_risolto
            cur.execute("SELECT agent_id, data->>'alias_di' FROM agents WHERE agent_id = %s;", (alias_di,))
            r_target = cur.fetchone()
        finally:
            conn.close()
        if not r_target:
            raise ValueError(
                f"'{slug_risolto}' e' alias di '{alias_di}' ma quel target non esiste "
                f"in Postgres — alias rotto, nessun deposito alla cieca"
            )
        if r_target[1]:
            raise ValueError(
                f"'{slug_risolto}' -> '{alias_di}' -> '{r_target[1]}': catena di alias "
                f"non ammessa (un solo hop) — configurazione da correggere, non da seguire"
            )
        return r_target[0]
    raise ValueError(
        f"'{target}' non corrisponde a nessun agente registrato in Postgres — "
        f"nessuna casella creata per uno slug non verificato (sarebbe una "
        f"cartella orfana che nessun bootstrap controlla mai)"
    )


def resolve_engine(agent_id: str) -> str:
    """Motore dell'agente destinatario, per scegliere il pattern giusto di
    rilevamento "sta generando" (FIX 10/08/2026, bug trovato da Betty su
    Regista/Deep Code): il vecchio controllo in _notifica_tmux cercava solo
    stringhe della UI di Claude Code ("esc to interrupt", "bypass permissions
    on"). Su qualunque altro motore quella seconda stringa non compare mai,
    quindi la condizione era vera SEMPRE al primo tentativo — falso positivo
    "notificato" anche quando il testo restava solo incollato, mai sottomesso.
    Fallback 'claude' se l'agente non risolve: comportamento invariato per chi
    già funzionava."""
    import psycopg2
    conn = psycopg2.connect("dbname=betty_db user=betty_user host=localhost port=5432")
    try:
        cur = conn.cursor()
        cur.execute("SELECT data->>'engine' FROM agents WHERE agent_id = %s;", (agent_id,))
        row = cur.fetchone()
    finally:
        conn.close()
    return row[0] if row and row[0] else "claude"


def _mittente_da_testo(testo: str) -> str:
    """Stesso identico pattern di a2a_inbox.py::_mittente_da_testo (non
    importato da lì per non introdurre un accoppiamento fra i due moduli per
    una singola regex) — il mittente A2A e' SEMPRE auto-dichiarato nel tag,
    mai verificato contro chi esegue davvero lo script."""
    m = re.search(r"\[A2A_FROM:([^\]]+)\]", testo[:200])
    return m.group(1) if m else "sconosciuto"


# Coordinatori noti ma NON derivabili dinamicamente da reports_to (10/08/2026,
# decisione esplicita di Leo dopo un bug reale trovato nel mio stesso test:
# Betty->Regista non veniva bloccato perche' oggi NESSUNO ha reports_to='betty'
# in Postgres — reports_to e' ancora troppo sparso, per scelta, per derivare
# "chi e' coordinatore" solo da "qualcuno gli riporta". Betty e Simone sono
# gli UNICI due coordinatori nominati esplicitamente per nome nel nucleo
# CLAUDE.md (§3: "Betty... = infrastruttura e debug tecnico. Simone... =
# progetti, agenda, coordinamento contenuti") — un elenco statico dei DUE, non
# un'inferenza mia su un campo inaffidabile (stesso principio per cui questa
# sessione ha gia' scartato `studio`/`role` come base d'inferenza). NON e' un
# debito tecnico dimenticato: se in futuro reports_to si popola abbastanza da
# coprire anche loro, questo elenco statico diventa ridondante ma resta
# innocuo (basta essere SIA nell'elenco SIA derivato — un OR, mai un blocco in
# piu' per chi gia' passava). Vedi anche verifica_gerarchia() sotto.
COORDINATORI_NOTI_STATICI = frozenset({"betty", "samantha", "simone"})


def verifica_gerarchia(mittente_slug: str, dest_slug: str) -> None:
    """Guardrail coordinatore/sottoposto (10/08/2026, richiesta Betty via Leo,
    causa diretta: Betty stessa ha scritto piu' volte diretto a Regista/
    Redattore invece che passare da Radio, il coordinatore dichiarato).

    LIMITE ONESTO, dichiarato prima di usarlo non dopo: NON e' un confine di
    sicurezza come i guardrail Unix fisici (dc-*) di stanotte — l'identita'
    del mittente A2A e' sempre auto-dichiarata nel tag [A2A_FROM:x], mai
    verificata contro chi esegue realmente lo script (vedi _mittente_da_testo
    sopra e la gemella in a2a_inbox.py). E' un controllo di codice nell'UNICO
    choke-point che tutti usano per mandare A2A (questo file) — soft ma
    proporzionato: il problema reale e' un errore di disciplina di un agente
    legittimo, non un tentativo di aggirare qualcosa.

    Blocca SOLO se ENTRAMBE le condizioni sono vere:
    - il destinatario ha un `reports_to` dichiarato E diverso dal mittente
    - il mittente e' esso stesso un coordinatore — nell'elenco statico
      COORDINATORI_NOTI_STATICI sopra, OPPURE qualcun altro in Postgres ha
      reports_to=mittente (derivato dinamicamente, nessun campo nuovo)

    Fail-open per tutto il resto (§ scope Betty, non generalizzare oltre i
    casi reali, deciso esplicitamente da Leo per NON bloccare traffico
    operativo legittimo come radio_debugger->Regista, ne' automazioni, ne'
    agenti che scrivono per un motivo diverso dal bypassare la gerarchia):
    nessun reports_to dichiarato -> nessun blocco; mittente non-coordinatore
    -> nessun blocco, anche se il destinatario ha un reports_to diverso (es.
    Leo o Marco possono ancora scrivere a Regista)."""
    if mittente_slug in COORDINATORI_NOTI_STATICI:
        mittente_e_coordinatore_statico = True
    else:
        mittente_e_coordinatore_statico = False
    import psycopg2
    conn = psycopg2.connect("dbname=betty_db user=betty_user host=localhost port=5432")
    try:
        cur = conn.cursor()
        cur.execute("SELECT data->>'reports_to' FROM agents WHERE agent_id = %s;", (dest_slug,))
        row = cur.fetchone()
        reports_to = row[0] if row and row[0] else None
        if not reports_to or reports_to == mittente_slug:
            return
        if mittente_e_coordinatore_statico:
            mittente_e_coordinatore = True
        else:
            cur.execute("SELECT 1 FROM agents WHERE data->>'reports_to' = %s LIMIT 1;", (mittente_slug,))
            mittente_e_coordinatore = cur.fetchone() is not None
            if not mittente_e_coordinatore:
                # 19/08/2026 (progetto pool samantha, patch approvata db32+9557):
                # le ISTANZE di un pool di coordinamento (data->>'pool' non
                # vuoto, es. samantha_1/samantha_2) sono coordinatori allo
                # stesso titolo del capofila: senza questa riga il guardrail
                # gerarchico non le vedrebbe e potrebbero scavalcare la catena
                # dichiarata senza il blocco che oggi vale per 'samantha'.
                # Derivato dal campo, mai un elenco statico da tenere allineato.
                cur.execute("SELECT 1 FROM agents WHERE agent_id = %s AND COALESCE(data->>'pool','') <> '' LIMIT 1;", (mittente_slug,))
                mittente_e_coordinatore = cur.fetchone() is not None
    finally:
        conn.close()
    if mittente_e_coordinatore:
        raise ValueError(
            f"'{mittente_slug}' è un coordinatore ma '{dest_slug}' riporta a "
            f"'{reports_to}' — passa da '{reports_to}' invece di scrivere diretto "
            f"(guardrail coordinatore/sottoposto, richiesta Betty 10/08/2026)"
        )


def verifica_dismesso(dest_slug: str) -> None:
    """Rete di sicurezza sulla dismissione (PARTE 3, 11/08/2026, decisione di
    Manfredo: Studio Leo marcato dismesso, "conserva tutto, reversibile").

    Se il destinatario risulta marcato 'dismesso' su Postgres, l'invio viene
    RIFIUTATO esplicitamente prima di qualunque deposito — mai depositato in
    silenzio. PERCHÉ CONTA (bug reale successo stamattina): Alfredo ha scritto
    "coordina con Leo" a leo_studio, lo script ha stampato "DEPOSITATO", lui ha
    creduto di aver delegato, e quel lavoro non è partito MAI. Con questo guardrail
    l'invio fallisce in modo rumoroso e spiega la situazione, invece di fingere
    una consegna impossibile.

    Stesso stile del guardrail coordinatore/sottoposto in questo stesso file:
    verifica dal DB PRIMA del deposito, errore che risale e diventa il messaggio
    '❌ NON DEPOSITATO: ...' nel __main__. Fail-open per ogni destinatario NON
    marcato (nessun reports_to richiesto qui: la dismissione è un flag puntuale,
    non una relazione)."""
    import psycopg2
    conn = psycopg2.connect("dbname=betty_db user=betty_user host=localhost port=5432")
    try:
        cur = conn.cursor()
        cur.execute("SELECT data->>'dismesso' FROM agents WHERE agent_id = %s;", (dest_slug,))
        row = cur.fetchone()
    finally:
        conn.close()
    if row and row[0] and str(row[0]).strip().lower() in ('true', '1'):
        raise ValueError(
            f"l'agente '{dest_slug}' è DISMESSO ({row[0]}): non accetto depositi "
            f"verso uno studio dismesso — il coordinatore resterebbe convinto di "
            f"aver delegato mentre il lavoro non parte mai. Lo sviluppo ora si fa "
            f"con un exec dev usa-e-getta (decisione Manfredo 11/08/2026). "
            f"Per ripristinare, rimuovere il flag 'dismesso' dal record."
        )


def _intestazione_campanello(testo: str, a2a_id: str) -> str:
    """Le sole due righe che restano in chat: FROM/TYPE originali + ID."""
    m = re.match(r"^(\[A2A_FROM:[^\]]+\]\s*\[A2A_TYPE:[^\]]+\])", testo.strip())
    prefisso = m.group(1) if m else "[A2A_FROM:?] [A2A_TYPE:?]"
    return f"{prefisso} [A2A_ID:{a2a_id}]"


def _attendi_pannello_libero(target_window: str, engine: str, timeout_s: float = 90.0, poll_interval: float = 1.5) -> bool:
    """Aspetta (polling) che il pannello risulti DAVVERO libero — idle secondo
    is_pane_ready() e non 'sta generando' secondo is_pane_generating(), stesse
    funzioni engine-aware del fix precedente — prima di incollare qualunque
    cosa. Correzione di fondo richiesta da Manfredo via Betty (10/08/2026,
    priorità urgente): il flusso precedente incollava SUBITO e ritentava a
    scadenza fissa se non partiva — su un pannello occupato con un tool
    lungo (ne' idle ne' rilevato "sta generando") questo produceva copie
    duplicate del messaggio (vedi dedup a marcatore sotto, rimasto come
    seconda rete di sicurezza: non scatta sempre, osservato dal vivo da
    Betty la stessa notte — dipende dal timing esatto fra quando il
    marcatore e' ancora nelle ultime righe catturate e quando e' già
    scorso via). Aspettare il libero vero elimina la corsa alla radice
    invece di limitarne i danni a valle. Ritorna True se visto libero entro
    il tetto, False se il tetto è scaduto — in quel caso il chiamante
    procede comunque (il messaggio non deve mai perdersi per un'attesa
    scaduta, la rete di sicurezza sotto resta invariata)."""
    # 22/08/2026 FIX classe "match per prefisso" (mandato exec_fix_match_prefisso):
    # target a match ESATTO ('sess:=win', funzione condivisa tmux_esatto). Prima
    # un 'sess:win' a prefisso poteva leggere/incollare la finestra SBAGLIATA
    # (es. AGY-PUBLIO che risolveva su AGY-PUBLIO-2). Idempotente: sicuro anche
    # se il chiamante ha gia' convertito.
    target_window = tmux_esatto.target_finestra_esatto(target_window)
    # SEGUITO reale (10/08/2026, stessa notte, caso trovato da Betty sul
    # messaggio 669c Radio->Regista): un SOLO snapshot "libero" non basta.
    # Il transcript di quell'invio mostra la stessa impronta di questo bug:
    # nessun "non risulta libero" (quindi il primo controllo l'ha visto
    # libero), un solo paste (il dedup ha funzionato, niente duplicati), ma
    # 4 C-m su ~6-7s NON hanno fatto partire nulla — Betty l'ha sbloccato
    # dopo con un singolo End+C-m manuale. Lettura piu' probabile: il
    # pannello mostra il testo idle ("Type your message...") un istante
    # PRIMA che la TUI abbia davvero finito di assestarsi dopo il turno
    # precedente — una foto giusta di uno stato non ancora stabile. Si
    # richiede quindi la stessa lettura "libero" su DUE controlli di
    # seguito (non una tantum) prima di fidarsene: un lampo di falso libero
    # nella transizione occupato->libero non supera due letture consecutive
    # distanziate di poll_interval, uno stato genuinamente stabile si'.
    scadenza = time.time() + timeout_s
    consecutivi_liberi = 0
    while time.time() < scadenza:
        check = _run(f"tmux capture-pane -t '{target_window}' -p -S -30")
        if check.returncode != 0:
            # Finestra/sessione inesistente (agente offline — caso reale
            # testato da DEV-110): non "non ancora libero", proprio non
            # c'e' nessun pannello da aspettare. Bail-out immediato, non i
            # timeout_s interi — altrimenti OGNI invio a un agente offline
            # impiegherebbe fino a 90s prima di fallire come sempre nel
            # punto giusto (il paste-buffer sotto, che da' l'errore giusto).
            return False
        if engine_adapter.is_pane_ready(engine, check.stdout) and not engine_adapter.is_pane_generating(engine, check.stdout):
            consecutivi_liberi += 1
            if consecutivi_liberi >= 2:
                return True
        else:
            consecutivi_liberi = 0
        time.sleep(poll_interval)
    return False


_CARATTERI_SEPARATORE = set("─━═──┄┅┈┉╌╍░▒▓█◉▔▁")


def _e_separatore(riga: str) -> bool:
    """True se la riga è (quasi) solo un separatore orizzontale tratteggiato
    (──…──), cioè il cordolo che nel TUI chiude il campo input. Serve come
    confine per isolare l'input box vero dallo scrollback."""
    s = riga.strip()
    if not s:
        return False
    conta = sum(1 for c in s if c in _CARATTERI_SEPARATORE)
    # una riga molto corta o con troppi caratteri estranei non è un cordolo
    # affidabile: richiedi una linea dominata dai trattini
    return len(s) >= 4 and conta >= max(2, int(len(s) * 0.6))


# Barra di stato/sessione di goose (misurata dal vivo 17/08/2026): una riga
# del tipo "  ━╌╌╌╌╌╌╌ … 6% 10k/164k" che goose stampa a delimitare il campo
# input. I caratteri ━/╌ SONO nel set separatore ma la coda "NN% Nk/Mk" ne
# abbassa la densità sotto la soglia di _e_separatore → va riconosciuta a parte.
_GOOSE_BAR_RE = re.compile(r"[\u2501\u254c\u2550\u2500\u2591\u2592\u2593\u2500*_]+\s*\d{1,3}%\s+\d+k/\d+k")


def _e_separatore_goose(riga: str) -> bool:
    s = riga.strip()
    if not s:
        return False
    return bool(_GOOSE_BAR_RE.search(s))


# ---------------------------------------------------------------------------
# 22/08/2026 (exec_fix_guardia_input_box, mandato Manfredo) — DIFETTO A della
# guardia P3: falsi positivi su stringhe di INTERFACCIA di Claude Code.
#
# Claude Code scrive dentro l'input box VUOTO delle stringhe proprie, che non
# sono testo di nessuno e spariscono appena si digita:
#   ❯ Try "how do I log an error?"      (suggerimento a rotazione)
#   ❯ Press up to edit queued messages  (avviso di coda)
# La guardia le leggeva come "testo pendente di un altro" e RIMANDAVA l'invio.
# Prova sul campo: 123 righe RIMANDATO_PRE_PASTE in /tmp/pulizia_input_box.log,
# di cui 15 su 'Press up...' e 12 su 'Try "..."'; caso 01:35 del 22/08, il
# refresh di publio_2 non ha consegnato il bootstrap con pendente_prima =
# '❯\xa0Try "how do I log an error?"' e l'agente e' rimasto senza identita'.
#
# Vanno trattate come SEGNAPOSTO (la casella e' vuota), esattamente come si fa
# gia' per "Type your message" e "YOLO mode".
#
# PERIMETRO STRETTO, di proposito: qui NON si allarga la guardia. Un campanello
# vero (`[A2A_FROM:...]`), un ordine umano ('procedi con i ticket in coda') e
# un dialog di avvio ('Do you trust the files in this folder?') restano testo
# pendente REALE e continuano a far rimandare l'invio — e' il verso "non rubare
# la frase a Manfredo", che non si tocca. Test su ENTRAMBI i versi in
# test_p3_guardia_input_box_entrambi_i_versi.py.
# ---------------------------------------------------------------------------

# Suggerimento a rotazione: sempre la forma Try "<qualcosa>" su una riga sola.
# Ancorato a inizio/fine riga (non `in`): 'Try "..."' dentro una frase piu'
# lunga scritta da un umano NON deve sparire.
_UI_SUGGERIMENTO_RE = re.compile(r'^Try\s+["\u201c].*["\u201d]$')

# Stringhe di stato che Claude Code stampa al posto del cursore quando la
# casella e' vuota. Confronto su riga INTERA normalizzata, mai su sottostringa.
_UI_RIGHE_INTERE = (
    "press up to edit queued messages",
)

# Prefissi di riga di stato del TUI (la riga dei modi, che compare col marker
# '>' al posto del prompt). Sono indicatori di modalita', mai testo digitato.
_UI_PREFISSI_STATO = (
    "accept-edits mode:",
    "plan mode on",
    "bypassing permissions",
)


def _e_segnaposto_ui(resto: str) -> bool:
    """True se `resto` (cio' che resta dopo aver tolto il marker ❯/> del
    prompt) e' una stringa di INTERFACCIA del CLI e non testo di qualcuno.

    Deve restare FALSA su qualunque cosa somigli a un messaggio: campanelli
    A2A, ordini umani, risposte a dialog. In caso di dubbio -> False (si
    rimanda l'invio), perche' l'errore costoso e' cancellare la frase di
    Manfredo, non ritardare di qualche secondo un messaggio."""
    r = " ".join((resto or "").split())
    if not r:
        return True
    if r.startswith("[") or r.startswith("/"):
        # campanello A2A ([A2A_FROM:..., [CAMPANELLO], [POOL ...]) o slash
        # command gia' digitato: e' roba di qualcuno, non UI.
        return False
    basso = r.lower()
    if basso in _UI_RIGHE_INTERE:
        return True
    if any(basso.startswith(p) for p in _UI_PREFISSI_STATO):
        return True
    if _UI_SUGGERIMENTO_RE.match(r):
        return True
    return False


def _input_box_da_testo(pane_text: str) -> str:
    """Parte PURA di estrazione: legge SOLO il testo vero dell'input box
    dentro un capture-pane txt (stesso formato di `tmux capture-pane -p`).

    Tecnica documentata in regole/comunicazione_a2a.md §7 (trovata da Publio
    costruendo un watchdog): NON si cerca nel cap scrollback — dà falsi
    positivi perché ci finiscono anche i prompt storici già inviati. Si legge
    SOLO il testo fra gli ULTIMI DUE separatori tratteggiati (──) del pannello:
    quello è il campo input in diretta.

    Ritorna la stringa pendente NON inviata (testo incollato ma mai sottomesso),
    oppure '' se l'input è vuoto / ha solo il segnaposto del prompt / il
    pannello non è decifrabile (es. nel mezzo di una generazione senza riquadro
    input visibile: in quel caso niente è pendente da deduplicare)."""
    righe = pane_text.rstrip("\n").rsplit("\n")
    if "Enter to send" in pane_text or "goose is ready" in pane_text:
        # Pannello goose: il campo input è delimità dall'ULTIMA barra di stato
        # "  ━╌╌… NN% Nk/Mk" (che _e_separatore non vede per la coda di
        # percentuale) e sta TUTTO sotto quella barra. Se non c'è ancora una
        # barra (primissimo frame), torna al comportamento normale.
        sep_goose = [i for i, r in enumerate(righe) if _e_separatore_goose(r)]
        if sep_goose:
            ultimo = sep_goose[-1]
            testo = "\n".join(righe[ultimo + 1:]).strip()
            if "Enter to send" in testo:
                righe_testo = [r for r in testo.splitlines() if r.strip()]
                if len(righe_testo) == 1 and righe_testo[0].strip().startswith(">"):
                    resto = righe_testo[0].strip().lstrip(">").strip()
                    if not resto:
                        return ""
            return testo
        separatori = [i for i, r in enumerate(righe) if _e_separatore(r)]
    else:
        separatori = [i for i, r in enumerate(righe) if _e_separatore(r)]
    if len(separatori) < 2:
        return ""
    penultimo, ultimo = separatori[-2], separatori[-1]
    if penultimo >= ultimo:
        return ""
    testo = "\n".join(righe[penultimo + 1:ultimo]).strip()
    if not testo:
        return ""
    if testo.startswith("● "):
        return ""
    # segnaposto del prompt (niente testo reale pendente): trattatelo come vuoto.
    # Marker idle dei vari motori: claude `❯ `, agy/qwen `> `, deepcode
    # "Type your message...". ATTENZIONE: `❯ [A2A_FROM:...]` con del testo DOPO
    # il marker è testo pendente REALE (un campanello incollato), NON va
    # scartato — altrimenti il dedup non lo vedrebbe mai e ricadremmo nel bug
    # delle copie. Qui si scarta come placeholder solo se dopo il marker (o
    # dopo le parole d'idle) non resta alcun contenuto.
    UNICA = testo if len(testo.splitlines()) == 1 else ""
    if UNICA:
        _p = UNICA.strip()
        if "Type your message" in _p or "YOLO mode" in _p or "Accept-edits mode" in _p:
            return ""
        if _p.startswith("❯") or _p.startswith(">"):
            # 22/08/2026 DISCRIMINANTE NBSP: la riga ghost/idle della casella
            # usa SEMPRE '❯' + NBSP (U+00A0, \xc2\xa0) subito dopo il marker,
            # anche quando il ghost mostra un testo di senso compiuto in
            # italiano (es. "Attendere risposta da Orazio..."). Il testo VERO
            # incollato usa '❯' + spazio normale (0x20). NBSP dopo il marker
            # => casella VUOTA (segnaposto), niente da deduplicare.
            _dopo = _p.lstrip("❯>")
            if _dopo.startswith(" "):
                return ""
            resto = _dopo.strip()
            if not resto:
                return ""
            # 22/08/2026 DIFETTO A: dopo il marker puo' esserci una stringa di
            # INTERFACCIA (suggerimento a rotazione, avviso di coda, riga dei
            # modi). La casella e' vuota: segnaposto, non testo pendente.
            if _e_segnaposto_ui(resto):
                return ""
        elif _e_segnaposto_ui(_p):
            # stesse stringhe di UI viste senza il marker del prompt (capture
            # preso mentre il cursore non e' disegnato).
            return ""
    return testo


def _leggi_input_box(target_window: str) -> str:
    """Esegue il capture-pane reale e delega la parsificazione a
    _input_box_da_testo()."""
    # 22/08/2026 FIX classe "match per prefisso": target a match ESATTO
    # (funzione condivisa tmux_esatto) — mai leggere una finestra con lo
    # stesso prefisso di nome al posto di quella giusta.
    target_window = tmux_esatto.target_finestra_esatto(target_window)
    out = _run(f"tmux capture-pane -t '{target_window}' -p").stdout
    return _input_box_da_testo(out)


def _a2a_id_da_campanello(campanello: str) -> str:
    m = re.search(r"\[A2A_ID:([^\]]+)\]", campanello)
    return m.group(1) if m else ""


def _input_ha_questo_a2a(input_box: str, campanello: str, a2a_id: str) -> bool:
    """True se l'input box pendente contiene già lo STESSO A2A_ID del campanello
    che stiamo per notificare. Se sì, ri-incollare produrrebbe una COPIA — è
    esattamente il bug da chiudere: il retry deve accorgersene, non moltiplicarlo."""
    if not input_box:
        return False
    if a2a_id and a2a_id in input_box:
        return True
    # fallback sul marcatore (prima riga del campanello) se per qualunque
    # motivo l'A2A_ID non fosse estratto o non fosse presente nel testo
    m0 = campanello.splitlines()[0] if campanello else ""
    return bool(m0 and m0 in input_box)


# ---------------------------------------------------------------------------
# P3 (21/08/2026, exec_p3_patch_input_box, mandato samantha_1): la casella di
# input NON e' solo del demone — a volte ci sta scrivendo Manfredo a mano.
#
# Fino a oggi _pulisci_input_box() mandava ESC + C-u INCONDIZIONATO e solo DOPO
# controllava se il testo era suo. Chiunque stesse scrivendo in quella finestra
# si vedeva sparire la frase a meta' (report P2:
# /tmp/betty_docs/report_p2_ordini_fermi.md, ipotesi PROVATA). Qui sotto le due
# primitive che chiudono il buco: un LOG (osservabilita' del call-site che oggi
# non lascia traccia da nessuna parte) e una domanda esplicita "questo testo e'
# mio?" da fare PRIMA di toccare qualunque tasto.
# ---------------------------------------------------------------------------

_PULIZIA_INPUT_BOX_LOG = "/tmp/pulizia_input_box.log"


def _chiamante_pulizia() -> str:
    """file:riga del chiamante VERO (il primo frame fuori da questo modulo).

    Serve perche' il call-site piu' pericoloso — engine_adapter.py:1421, quello
    usato dal campanellaio — non compare in NESSUN log: e' esattamente il
    motivo per cui in P2 la correlazione oraria caso-per-caso non era
    dimostrabile (solo indiziaria)."""
    try:
        import traceback
        qui = os.path.abspath(__file__)
        for fr in reversed(traceback.extract_stack()[:-1]):
            if os.path.abspath(fr.filename) != qui:
                return f"{os.path.basename(fr.filename)}:{fr.lineno}"
    except Exception:
        pass
    return "?"


def _log_pulizia_input_box(target_window: str, chiave, pendente: str,
                           esito: str, chiamante: str = "") -> None:
    """UNA riga per ogni pulizia tentata, scritta PRIMA di toccare i tasti.

    Non cambia NESSUN comportamento: e' solo osservabilita'. Best-effort — se
    il log non e' scrivibile (agente non-root su un file creato da root) la
    consegna del messaggio non deve fallire per questo."""
    try:
        riga = (f"{time.strftime('%Y-%m-%dT%H:%M:%S')} win={target_window} "
                f"chiave={chiave if chiave else 'None'} esito={esito} "
                f"chiamante={chiamante or _chiamante_pulizia()} "
                f"pendente_prima={(pendente or '')[:80]!r}\n")
        nuovo = not os.path.exists(_PULIZIA_INPUT_BOX_LOG)
        with open(_PULIZIA_INPUT_BOX_LOG, "a", encoding="utf-8") as f:
            f.write(riga)
        if nuovo:
            # il log e' condiviso fra demoni root e agenti dc-*: se lo crea
            # root per primo, gli altri non riuscirebbero piu' a scriverci.
            try:
                os.chmod(_PULIZIA_INPUT_BOX_LOG, 0o666)
            except OSError:
                pass
    except Exception:
        pass


class EsitoPulizia(enum.Enum):
    """Esito di _pulisci_input_box(), a TRE stati e non piu' un bool.

    Il bool non bastava: "non ho pulito perche' non era mio" e "ho provato a
    pulire e non ci sono riuscito" portavano allo stesso False, ma impongono al
    chiamante due condotte OPPOSTE (rimandare / insistere una volta sola).

    Retrocompatibile di proposito: `bool(esito)` e' True SOLO per PULITO,
    cioe' esattamente il vecchio "campo libero, puoi incollare". Un chiamante
    non ancora aggiornato che scriva `if _pulisci_input_box(...)` continua a
    funzionare e sbaglia dalla parte SICURA (non incolla)."""

    PULITO = "pulito"      # il campo ora e' libero: si puo' incollare
    NON_MIO = "non_mio"    # c'e' testo di qualcun altro: NON toccato, rimandare
    FALLITO = "fallito"    # era nostro ma il campo e' ancora sporco: non insistere

    def __bool__(self) -> bool:
        return self is EsitoPulizia.PULITO


def _input_box_e_di_altri(target_window: str, testo: str, chiave) -> bool:
    """True se nella casella c'e' del testo pendente che NON e' riconducibile
    a noi: ne' vuoto ne' nostro, quindi e' di un umano che sta scrivendo.

    Casella VUOTA -> False (non c'e' niente da proteggere): _input_ha_questo_a2a
    torna False anche sul vuoto, quindi il caso va distinto a mano o si
    finirebbe per rifiutarsi di lavorare su ogni pannello pulito."""
    pendente = _leggi_input_box(target_window)
    if not (pendente or "").strip():
        return False
    return not _input_ha_questo_a2a(pendente, testo, chiave)


def _attendi_sottomissione(target_window: str, testo: str, chiave: str,
                           timeout: float = 15.0, passo: float = 1.0) -> bool:
    """True se `testo` risulta SOTTOMESSO (sparito dall'input box) entro
    `timeout` secondi, controllando ogni `passo`.

    17/08/2026 (exec_debug_comunicazione_interna, misurato dal vivo su
    ricerca_lavoro:AGY-GIORGIO): la verifica a SCADENZA FISSA di 5s era un
    falso negativo sistematico sui pannelli con contesto pesante. Su Giorgio
    (202k token di contesto) il C-m sottometteva davvero, ma la UI di deepcode
    impiegava PIU' di 5s a svuotare l'input box: la verifica lo leggeva ancora
    pieno, concludeva "non sottomesso" e faceva scattare un retry inutile —
    0 CLEAN su 27 invii, 25 dei 32 RETRY post-fix erano solo quel pannello.
    Peggio: quel retry gli accodava una SECONDA COPIA del messaggio (vedi il
    guardrail sotto). Sondato a mano: ESC non ripuliva, i tasti si scrivevano,
    e la sottomissione compariva qualche secondo DOPO la finestra dei 5s.

    Attendere "fino a quando risulta sottomesso" invece che "5s fissi" e'
    anche PIU' VELOCE nel caso normale: un pannello sano si svuota in ~1s e
    qui si esce subito, invece di dormire 5s pieni ad ogni invio."""
    scadenza = time.time() + timeout
    while True:
        if not _input_ha_questo_a2a(_leggi_input_box(target_window), testo, chiave):
            return True
        if time.time() >= scadenza:
            return False
        time.sleep(passo)


def _pulisci_input_box(target_window: str, testo: str, chiave: str,
                       attesa: float = 1.5) -> "EsitoPulizia":
    """Svuota l'input box del pannello — SE quel testo e' nostro — e dice come
    e' andata (EsitoPulizia: PULITO / NON_MIO / FALLITO).

    17/08/2026 (exec_debug_comunicazione_interna): guardrail contro il
    duplicato concatenato. Il re-paste (sia del primo colpo sia del retry)
    dava per scontato che l'ESC appena inviato avesse ripulito l'input box,
    cosi' che il paste successivo SOSTITUISSE il testo. **Non e' vero su
    deepcode**: verificato dal vivo su AGY-EXEC_CANDIDATURA_TELUS, dopo che un
    messaggio e' stato sottomesso E il turno si e' concluso, quel testo TORNA
    nell'input box e ci RESTA; digitando un carattere si accoda (quindi e'
    contenuto vero, non un'eco di trascritto) e **ESC non lo rimuove**. Un
    paste sopra quel residuo si ACCODA: e' esattamente cosi' che il pannello
    di Giorgio si e' ritrovato lo stesso campanello incollato due volte di
    fila (A2A_ID 20260817T033911Z_giorgio-sender_b9cd, osservato dal vivo).

    Sequenza: ESC (che serve a INTERROMPERE una generazione in corso — resta,
    e' il comportamento voluto su deepcode) + **C-u**, che e' la primitiva che
    svuota davvero il campo (provata dal vivo: dopo C-u `_leggi_input_box`
    ritorna ''). Se anche cosi' il testo resta, il chiamante NON deve
    incollare di nuovo: aggiungerebbe solo una copia.

    P3 21/08/2026 (exec_p3_patch_input_box, mandato samantha_1) — LA GUARDIA.
    Fino a oggi ESC + C-u partivano SEMPRE, e il controllo di appartenenza
    serviva solo DOPO, per dire se la pulizia "era riuscita". Su una finestra in
    cui Manfredo sta scrivendo un ordine a mano, quel C-u gli cancellava la
    frase a meta' senza lasciare traccia da nessuna parte (report P2). Ora la
    domanda "questo testo e' mio?" si fa PRIMA: se la risposta e' no non si
    tocca NIENTE — nemmeno l'ESC, che su un pannello altrui e' comunque un
    tasto non richiesto — e lo si dice al chiamante con NON_MIO, perche' possa
    RIMANDARE invece di forzare."""
    # 22/08/2026 FIX classe "match per prefisso": target a match ESATTO
    # (funzione condivisa tmux_esatto) — mai inviare tasti/leggere la finestra
    # con lo stesso prefisso di nome al posto di quella giusta.
    target_window = tmux_esatto.target_finestra_esatto(target_window)
    pendente_prima = _leggi_input_box(target_window)
    chiamante = _chiamante_pulizia()

    if (pendente_prima or "").strip() and not _input_ha_questo_a2a(
            pendente_prima, testo, chiave):
        # Testo di qualcun altro (tipicamente: Manfredo che sta scrivendo).
        # NON si tocca nulla. Il messaggio del demone non e' perso: resta in
        # carico al chiamante, che deve RIMANDARE la consegna.
        _log_pulizia_input_box(target_window, chiave, pendente_prima,
                               "NON_MIO_NON_TOCCATO", chiamante)
        return EsitoPulizia.NON_MIO

    _log_pulizia_input_box(target_window, chiave, pendente_prima,
                           "PULISCO", chiamante)
    _run(f"tmux send-keys -t '{target_window}' Escape")
    time.sleep(attesa)
    _run(f"tmux send-keys -t '{target_window}' C-u")
    time.sleep(0.3)
    # 28/08/2026 (exec_fix_paste_composer, mandato samantha_1/Manfredo MSG
    # 17440): riprodotto dal vivo che UN SOLO C-u su un pannello BUSY (comando
    # shell/tool lungo in corso) cancella SOLO l'ULTIMA RIGA LOGICA del campo
    # di input, non l'intero campo multi-riga — un messaggio Telegram/A2A ha
    # SEMPRE almeno 2 righe logiche (header + corpo, per costruzione), quindi
    # restava l'header e il corpo veniva perso. Il chiamante poi premeva
    # comunque Invio sull'header residuo: sottomissione PARZIALE silenziosa
    # (ritorno PULITO/CLEAN apparente, corpo del messaggio sparito per
    # sempre). Su pannello idle un solo C-u basta gia' oggi (il loop esce
    # subito): qui si ripete SOLO finche' la chiave e' ancora presente, con un
    # tetto per non restare bloccati su un campo davvero non pulibile.
    _tentativi_extra = 0
    while (_input_ha_questo_a2a(_leggi_input_box(target_window), testo, chiave)
           and _tentativi_extra < 5):
        _run(f"tmux send-keys -t '{target_window}' C-u")
        time.sleep(0.3)
        _tentativi_extra += 1
    if not _input_ha_questo_a2a(_leggi_input_box(target_window), testo, chiave):
        return EsitoPulizia.PULITO
    return EsitoPulizia.FALLITO


_SEND_A2A_ESITI_LOG = "/tmp/send_a2a_esiti.log"

def _append_send_a2a_esito(esito: str, engine: str, target_window: str, a2a_id: str):
    """Registra su file l'esito di ogni invio A2A per il task 6320 (rinforzo
    Manfredo): traccia BOTH i sintomi (RETRY=primo invio non sottomesso,
    CLEAN=primo invio a segno, DOPPIO=rischiato re-paste) E per CIASCUN
    motore (claude/agy/deepcode), non solo deepcode. Additivo, non-bloccante."""
    try:
        with open(_SEND_A2A_ESITI_LOG, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {esito} engine={engine} target={target_window} a2a_id={a2a_id}\n")
    except OSError:
        pass


def _notifica_tmux(target_window: str, campanello: str, max_attempts: int, engine: str = "claude") -> bool:
    """Il trasporto NON cambia (§2 del disegno): stesso load-buffer +
    paste-buffer + nome-buffer univoco che ha chiuso DEV-006. Cambia solo
    cosa viaggia (il campanello di due righe, non il corpo) e la parola con
    cui si dichiara l'esito: mai più "Confermato" — qui c'è solo un indizio
    letto dal terminale (§5), la prova vera è a2a_ack.py.

    FIX 10/08/2026 (bug reale trovato da Betty a mano su Regista/Deep Code):
    il controllo "sta generando" usa engine_adapter.is_pane_generating(),
    la stessa funzione engine-aware già validata e in produzione altrove
    (agent_monitor.py, failover_manager.py, dynamic_engine_router.py) — non
    più un pattern testuale hardcoded sulla UI di Claude Code, che su ogni
    altro motore risultava vero SEMPRE al primo tentativo (falso positivo:
    "notificato" anche col testo solo incollato, mai sottomesso).

    SEGUITO stessa notte (redesign wait_idle, priorità urgente di Manfredo):
    prima di incollare, ASPETTA che il pannello sia davvero libero invece di
    incollare subito e ritentare a scadenza fissa — vedi
    _attendi_pannello_libero(). Il ciclo di retry+dedup sotto resta
    invariato come rete di sicurezza per il caso (raro, col wait davanti)
    in cui l'attesa scada o il primo Invio non venga recepito.

    SEGUITO stessa notte, terza conferma di Manfredo (definitiva): SOLO per
    engine='deepcode', ESC-poi-messaggio è il comportamento DI DEFAULT, non
    un'eccezione per casi urgenti — ogni invio su questo motore interrompe
    subito invece di aspettare. Rischio esplicitamente accettato da
    Manfredo più volte, non richiesto di nuovo qui. Claude/qwen/agy restano
    col wait_idle sopra, invariati. Verificato dal vivo PRIMA di attivarlo
    (mai assunto): ESC durante una generazione attiva interrompe pulito il
    turno — sessione e memoria intatte, confermato con un prompt successivo
    che rispettava ancora le istruzioni di sistema di partenza; ESC su un
    pannello già idle è un no-op innocuo, nessun effetto collaterale
    osservato."""
    # 22/08/2026 FIX classe "match per prefisso" (funzione condivisa tmux_esatto):
    # il target deve essere a match ESATTO prima di qualunque uso tmux, altrimenti
    # il campanello andrebbe nella finestra con lo stesso PREFISSO di nome (es.
    # AGY-PUBLIO -> AGY-PUBLIO-2). Idempotente: la conversione è già fatta, resta.
    target_window = tmux_esatto.target_finestra_esatto(target_window)
    # FIX 10/08/2026 (task Leo bcfd): un chiamante NON-root (un agente dc-*)
    # esegue `tmux` sul proprio socket (vuoto) invece che sul server di ROOT dove
    # vivono i pannelli: la paste fallirebbe sempre. Si delega il campanello al
    # relay root a2a_bell_relay.py (che polla le inbox e campanella lui stesso).
    # La consegna vera (deposito su disco) e' gia' avvenuta a monte e resta intatta.
    if os.geteuid() != 0:
        print("[send_a2a] euid non-root: delego il campanello al relay root "
              "(a2a_bell_relay.py); il messaggio resta in inbox fino alla lettura.",
              file=sys.stderr)
        return False
    # goose NON prende il bypass ESC-preventivo: diversamente da deepcode,
    # la sua TUI è autonoma (esegue tool per conto proprio) e non ha sempre
    # il footer di input visibile. Se incolliamo mentre goose sta eseguendo
    # un tool, il testo finisce nel pannello sbagliato e la sottomissione
    # fallisce (osservato nel collaudo e2e, 17/08/2026). Per goose si scende
    # quindi al ramo else sotto: si ATTENDE che il pannello sia davvero idle
    # (footer "> Enter to send · Ctrl+J newline", via is_pane_ready) prima di
    # incollare. deepcode resta con ESC-preventivo come da comportamento.
    if engine == "deepcode":
        _run(f"tmux send-keys -t '{target_window}' Escape")
        time.sleep(2.0)  # margine "Interrupting…" -> idle pulito, misurato dal vivo
    else:
        libero = _attendi_pannello_libero(target_window, engine)
        if not libero:
            print(f"[send_a2a] '{target_window}' non risulta libero dopo l'attesa massima — procedo comunque (rete di sicurezza sotto).")

    unique = f"{os.getpid()}_{int(time.time() * 1000)}"
    buf_file = f"/tmp/send_a2a_paste_{unique}.txt"
    buf_name = f"send_a2a_{unique}"

    with open(buf_file, "w", encoding="utf-8") as f:
        f.write(campanello)

    cmd_load = f"tmux load-buffer -b {buf_name} {buf_file}"
    cmd_paste = f"tmux paste-buffer -p -b {buf_name} -t '{target_window}'"

    def _cleanup():
        _run(f"tmux delete-buffer -b {buf_name}")
        try:
            os.remove(buf_file)
        except OSError:
            pass

    try:
        check = _run(cmd_load)
        if check.returncode != 0:
            print(f"[send_a2a] Errore: impossibile caricare il buffer del campanello ({check.stderr.strip()})", file=sys.stderr)
            return False
        time.sleep(0.1)

        a2a_id = _a2a_id_da_campanello(campanello)

        # FIX CONSEGNA A2A (11/08/2026): PRIMA di qualunque paste, controlla se
        # l'input box vero del destinatario contiene GIÀ questo stesso A2A_ID
        # pendente (es. un invio/chiamata precedente l'ha lasciato lì non
        # sottomesso — è esattamente lo scenario delle tre copie di Simone).
        # Se sì, NON incollare MAI: si prova a sottomettere ciò che c'è già,
        # oppure ci si ferma e si segnala — MAI una copia in più.
        # Si legge SOLO il testo fra gli ultimi due separatori (──) del pannello,
        # mai lo scrollback (falsi positivi — tecnica documentata in
        # regole/comunicazione_a2a.md §7, trovata da Publio).
        def _input_pendente_con_questo_a2a() -> bool:
            return _input_ha_questo_a2a(_leggi_input_box(target_window), campanello, a2a_id)

        # P3 21/08/2026 (exec_p3_patch_input_box): la casella e' di qualcun
        # altro? Va chiesto PRIMA di incollare, non solo prima del C-u. Un
        # paste dentro un ordine che Manfredo sta scrivendo a mano gli ACCODA
        # il campanello in mezzo alla frase, e l'End+C-m piu' sotto la
        # sottomette monca: sarebbe una sovracorrezione (invio prematuro) al
        # posto del guasto attuale (perdita silenziosa). Qui si RIMANDA.
        # Il messaggio NON e' perso: e' gia' depositato in inbox/ (la consegna
        # vera e' il file su disco), il campanello si ritenta piu' tardi.
        if _input_box_e_di_altri(target_window, campanello, a2a_id):
            _log_pulizia_input_box(target_window, a2a_id,
                                   _leggi_input_box(target_window),
                                   "RIMANDATO_PRE_PASTE", "send_a2a:notifica")
            _append_send_a2a_esito("RIMANDATO", engine or "?", target_window, a2a_id)
            print(f"[send_a2a] ⏸️ '{target_window}': nell'input box c'e' testo NON nostro "
                  f"(qualcuno sta scrivendo a mano) — NON incollo e NON premo Invio. "
                  f"Il messaggio resta in inbox: la consegna vera e' il file su disco.")
            return False

        if not _input_pendente_con_questo_a2a():
            paste_res = _run(cmd_paste)
            if paste_res.returncode != 0:
                print(f"[send_a2a] Errore: finestra '{target_window}' non trovata ({paste_res.stderr.strip()})", file=sys.stderr)
                return False
            settle = min(1.5 + len(campanello) / 800, 5.0)
            time.sleep(settle)
            _run(f"tmux send-keys -t '{target_window}' End")
            time.sleep(0.2)
        else:
            print(f"[send_a2a] ⚠️ '{target_window}' ha già nell'input box questo stesso A2A_ID "
                  f"({a2a_id or '?'}) non inviato — NON incollo di nuovo: provo a sottomettere ciò che c'è già. "
                  f"Nessuna copia aggiunta.")

        # 15/08/2026 (audit Igor + validazione exec, task Samantha 8014; fix
        # 16/08 per deriva single-shot, task Samantha 6f59): per DEEPCODE
        # "interrompi sempre + single-shot". Con l'ESC preventivo a monte il
        # pannello è già pulito; il paste+invio qui sotto è l'UNICO invio.
        # NON si usa is_pane_generating (inaffidabile su deepcode, sia testo che
        # tool) per non reintrodurre i falsi negativi -> duplicati.
        #
        # MA da solo il single-shot ha una deriva: un invio realmente fallito
        # (messaggio rimasto incollato-ma-mai-sottomesso) resta bloccato per
        # sempre, a differenza del vecchio retry che recuperava ai giri 2-4.
        # Per recuperare i fallimenti isolati SENZA tornare al loop di re-paste:
        # una verifica ritardata sull'input box (il campanello è ancora lì NON
        # sottomesso?) e, se sì, UN solo retry (ESC + ripaste + invio), mai 4.
        # NOTA (17/08/2026, migrazione goose): SOLO deepcode prende questo ramo.
        # goose e' instradato al loop generale qua sotto (verifica di
        # sottomissione via is_pane_generating, pinta unica — senza il
        # doppio-paste che il ramo ESC+ripaste di deepcode causerebbe a goose,
        # osservato nel collaudo e2e). Il solo ESC-preventivo a monte basta.
        if engine == "deepcode":
            # 17/08 (task 6320, approvato Samantha d66e): il semplice End+C-m su
            # testo già incollato NON sottomette affidabilmente su deepcode
            # (~72% di RETRY misurati). Allineata la sequenza di invio a quella
            # del retry che funziona sempre: ESC + re-paste + End + C-m, AL
            # PRIMO colpo, invece del solo End+C-m.
            # GUARDRAIL 17/08/2026 (exec_debug_comunicazione_interna): si
            # ripassa il testo SOLO se l'ESC ha davvero ripulito l'input box.
            # Se non l'ha ripulita (pannello occupato/lento) il paste si
            # ACCODA invece di sostituire, e il destinatario riceve il
            # campanello DOPPIO — incidente reale osservato su
            # ricerca_lavoro:AGY-GIORGIO. In quel caso il testo incollato
            # sopra è comunque già lì: basta sottometterlo.
            # P3 21/08/2026: tre esiti, tre condotte diverse. Il vecchio
            # codice premeva End+C-m INCONDIZIONATO qui sotto: con la sola
            # guardia dentro _pulisci_input_box avrebbe sottomesso la frase a
            # meta' di Manfredo. NON_MIO -> ci si ferma PRIMA dell'Invio.
            _esito_pulizia = _pulisci_input_box(target_window, campanello, a2a_id)
            if _esito_pulizia is EsitoPulizia.NON_MIO:
                _append_send_a2a_esito("RIMANDATO", "deepcode", target_window, a2a_id)
                print(f"[send_a2a] ⏸️ '{target_window}': testo NON nostro nell'input box — "
                      f"NON premo Invio (sottometterei una frase altrui a meta'). "
                      f"Il messaggio resta in inbox e si ritenta piu' tardi.")
                return False
            if _esito_pulizia:
                _run(cmd_load)
                time.sleep(0.1)
                _run(cmd_paste)
                time.sleep(0.3)
            _run(f"tmux send-keys -t '{target_window}' End")
            time.sleep(0.2)
            _run(f"tmux send-keys -t '{target_window}' C-m")
            # 17/08/2026: la verifica non è più a scadenza fissa di 5s (falso
            # negativo sistematico sui pannelli con contesto pesante, vedi
            # _attendi_sottomissione) ma "attendi finché risulta sottomesso,
            # al massimo 15s" — più veloce sul pannello sano, non bugiarda
            # su quello lento.
            if _attendi_sottomissione(target_window, campanello, a2a_id):
                _append_send_a2a_esito("CLEAN", "deepcode", target_window, a2a_id)
                print(f"[send_a2a] 🔔 deepcode single-shot: campanello inviato a '{target_window}'.")
            elif not _pulisci_input_box(target_window, campanello, a2a_id):
                # P3 21/08/2026: NON_MIO e FALLITO sono entrambi falsy e
                # finiscono QUI, ed e' la condotta giusta per tutti e due —
                # questo ramo non incolla e non preme Invio, si limita a
                # dichiarare e lasciare il messaggio in inbox. La distinzione
                # resta leggibile in /tmp/pulizia_input_box.log.
                # Non sottomesso E l'ESC non ripulisce: il pannello non sta
                # onorando gli input. Un re-paste qui aggiungerebbe solo una
                # copia in più a un pannello già ingolfato: ci si ferma e lo
                # si dichiara, invece di moltiplicare il messaggio.
                _append_send_a2a_esito("BLOCCO", "deepcode", target_window, a2a_id)
                print(f"[send_a2a] ⚠️ '{target_window}' non sottomette e non onora l'ESC — "
                      f"NON incollo di nuovo (eviterei solo un duplicato). "
                      f"Il messaggio resta in inbox: la consegna vera è il file su disco.")
            else:
                # ancora pendente ma il pannello risponde all'ESC (che ha
                # appena ripulito): un solo retry extra, mai un loop di 4.
                _run(cmd_load)
                time.sleep(0.1)
                _run(cmd_paste)
                time.sleep(0.3)
                _run(f"tmux send-keys -t '{target_window}' End")
                time.sleep(0.2)
                _run(f"tmux send-keys -t '{target_window}' C-m")
                # task 6320: registro esito
                _append_send_a2a_esito("RETRY", "deepcode", target_window, a2a_id)
                print(f"[send_a2a] 🔔 deepcode: retry (2°) eseguito per '{target_window}' (primo invio non sottomesso).")
            return True

        if engine == "agy":
            # FIX 19/08/2026 (exec_debug_paste_agy, mandato Samantha 8862):
            # per agy la consegna si verifica sulla SOTTOMISSIONE (il testo
            # sparisce dall'input box), NON su is_pane_generating: Gemini 3.7
            # Flash risponde in ~1-2s, spesso PRIMA del primo check a +1.5s
            # del loop generale sotto. Al giro 2 il pannello e' gia' tornato
            # idle, l'input box e' vuoto (sottomesso) e il ramo ELSE RI-INCOLLA
            # il campanello -> DOPPIO (evidenza live 19/08 19:02 su
            # ricerca_lavoro:AGY-RACHELE: 3x DOPPIO + BLOCCO). Single-shot,
            # stessa filosofia di deepcode ma SENZA ESC (su agy annullerebbe
            # una generazione in corso del destinatario): un solo C-m sul testo
            # gia' incollato dal pre-loop, verifica "sottomesso" fino a 15s,
            # UN solo retry di solo-Invio (mai un secondo paste).
            if not _input_pendente_con_questo_a2a():
                _run(cmd_load)
                time.sleep(0.1)
                _run(cmd_paste)
                time.sleep(0.3)
            _run(f"tmux send-keys -t '{target_window}' End")
            time.sleep(0.2)
            _run(f"tmux send-keys -t '{target_window}' C-m")
            if _attendi_sottomissione(target_window, campanello, a2a_id):
                _append_send_a2a_esito("CLEAN", "agy", target_window, a2a_id)
                print(f"[send_a2a] 🔔 agy single-shot: campanello sottomesso a '{target_window}'.")
                return True
            # Non sottomesso al primo C-m: il testo e' gia' nell'input box, si
            # ritenta il solo Invio (NON un secondo paste: accoderebbe una copia).
            _run(f"tmux send-keys -t '{target_window}' End")
            time.sleep(0.2)
            _run(f"tmux send-keys -t '{target_window}' C-m")
            if _attendi_sottomissione(target_window, campanello, a2a_id):
                _append_send_a2a_esito("RETRY", "agy", target_window, a2a_id)
                print(f"[send_a2a] 🔔 agy: retry (2°) — campanello sottomesso a '{target_window}'.")
                return True
            # Non sottomesso dopo due Invio: il pannello non onora l'input. Un
            # re-paste qui accoderebbe solo una copia (regola A2A: mai una copia
            # prima di aver provato l'originale). Il messaggio NON e' perso:
            # resta depositato in inbox/ finche' non viene letto e ackato.
            _append_send_a2a_esito("BLOCCO", "agy", target_window, a2a_id)
            print(f"[send_a2a] ⚠️ '{target_window}' non sottomette dopo due Invio — "
                  f"NON incollo di nuovo (eviterei solo un duplicato). "
                  f"Il messaggio resta in inbox: la consegna vera e' il file su disco.")
            return False

        # FIX 19/08/2026 (exec_debug_paste_agy, mandato Samantha 8862):
        # "era pendente in un giro precedente e ora NON più + pannello idle"
        # = SOTTOMESSO (risposta veloce, es. Gemini 3.7 Flash ~1-2s su agy).
        # Senza questo, il ramo ELSE sotto ri-incollava una COPIA vera.
        visto_pendente = False
        for attempt in range(max_attempts):
            # a ogni retry: il campanello è già pendente nell'input box vero?
            attuale_pendente = _input_pendente_con_questo_a2a()
            if attuale_pendente:
                visto_pendente = True
            # FIX 14/08/2026 (report exec_debug_paste_claude, verificato dal vivo
            # 9/9 su 3 finestre): "già pendente" da solo NON basta per decidere
            # se ripassare il paste. Se il pannello e' occupato da un tool lungo
            # (is_pane_generating), ripassare accoderebbe una copia vera — li'
            # giusto limitarsi a Invio. Ma se il pannello e' IDLE con lo stesso
            # testo gia' incollato e mai sottomesso, un Invio isolato su Claude
            # Code spesso non lo sottomette MAI, nemmeno a ripetizione: serve
            # ripassare lo stesso identico testo prima di Invio, senza produrre
            # doppioni visibili. Il vecchio codice trattava i due casi uguali.
            pannello_occupato = False
            if attuale_pendente:
                try:
                    pane_ora = _run(f"tmux capture-pane -t '{target_window}' -p -S -30").stdout
                    pannello_occupato = engine_adapter.is_pane_generating(engine, pane_ora)
                except Exception:
                    pannello_occupato = True  # in dubbio, comportamento vecchio (piu' prudente)
            # BUG REALE 16/08/2026 (trovato da Samantha correlando /tmp/send_a2a_esiti.log
            # con questo codice, su richiesta Manfredo "controlla che succede agli script"):
            # al primo giro (attempt==0) il paste pre-loop (riga ~490) ha GIA' incollato
            # il testo ma non ha MAI tentato un Invio — quindi attuale_pendente=True e
            # pannello_occupato=False sono garantiti (nulla e' stato ancora sottomesso).
            # La vecchia condizione trattava questo come "pendente ma idle -> ripassa",
            # identico al caso di un retry reale dopo un Invio fallito: risultato, un
            # secondo paste PRIMA che il primo abbia mai avuto un tentativo di Invio.
            # Confermato con dati reali: 2 DOPPIO su 2 invii claude reali oggi, entrambi
            # al primo giro (il print restava silenzioso su attempt==0, la duplicazione no).
            # Fix: al primo giro non si ripassa MAI — il pre-loop l'ha gia' fatto, si tenta
            # solo l'Invio. Solo da attempt>=1 (un Invio e' gia' stato tentato e non e'
            # partito) ripassare torna legittimo.
            if attuale_pendente and (pannello_occupato or attempt == 0):
                # l'A2A_ID è già nell'input box E (il pannello è occupato, OPPURE è il
                # primissimo tentativo dopo il paste pre-loop): NON incollare di nuovo,
                # solo tentare la sottomissione del testo già presente (regola A2A: mai
                # un invio a vuoto, mai una copia prima di aver provato l'originale).
                if attempt > 0:
                    print(f"[send_a2a] Tentativo {attempt+1}/{max_attempts}: A2A_ID già pendente e pannello occupato, "
                          f"non lo ripeto — provo a sottomettere.")
            else:
                # FIX 19/08/2026 (exec_debug_paste_agy): il caso "era pendente in
                # un giro PRECEDENTE e ora NON più su pannello idle" = il campanello
                # e' stato SOTTOMESSO tra un giro e l'altro (risposta veloce).
                # Ri-incollare creerebbe una COPIA — evidenza live su
                # ricerca_lavoro:AGY-RACHELE (engine=claude su pane agy): 3x
                # DOPPIO + BLOCCO il 19/08 19:02. Solo il caso "MAI visto pendente"
                # (paste non arrivato) giustifica il re-paste di recupero.
                if visto_pendente and not attuale_pendente:
                    _append_send_a2a_esito("CLEAN", engine, target_window, a2a_id)
                    print(f"[send_a2a] 🔔 notificato (indizio, non prova, sottomissione rilevata): '{target_window}' "
                          f"ha sottomesso il campanello — nessun ri-incollo.")
                    return True
                # O l'A2A_ID non è pendente (paste non arrivato/senza traccia),
                # O è pendente ma il pannello è IDLE DOPO UN VERO TENTATIVO DI INVIO
                # già fatto in un giro precedente (attempt>=1, il caso del bug 14/08):
                # in entrambi ripassare è legittimo, non crea copie visibili quando il
                # pannello è idle.
                motivo = "pendente ma pannello idle dopo un invio già tentato" if attuale_pendente else "non pendente"
                print(f"[send_a2a] Tentativo {attempt+1}/{max_attempts}: A2A_ID {motivo} — ripasso il campanello.")
                # task 6320 (rinforzo Manfredo): un re-paste su retry è il
                # candidato a DOPPIO invio per claude/agy — traccialo.
                _append_send_a2a_esito("DOPPIO", engine, target_window, a2a_id)
                _run(cmd_load)
                time.sleep(0.1)
                _run(cmd_paste)
                time.sleep(0.3)
            _run(f"tmux send-keys -t '{target_window}' End")
            time.sleep(0.2)
            _run(f"tmux send-keys -t '{target_window}' C-m")
            time.sleep(1.5)
            pane_check = _run(f"tmux capture-pane -t '{target_window}' -p -S -30").stdout
            if engine_adapter.is_pane_generating(engine, pane_check):
                # task 6320: consegna confermata (primo giro = pulito)
                _append_send_a2a_esito("CLEAN", engine, target_window, a2a_id)
                print(f"[send_a2a] 🔔 notificato (indizio, non prova): '{target_window}' risulta aver avviato una generazione.")
                return True
            # SECONDA LETTURA RITARDATA (14/08/2026, bug reale trovato da Betty su
            # sweep flotta: 5 messaggi con lo stesso fingerprint gia' documentato
            # sopra per _attendi_pannello_libero — "una foto giusta di uno stato
            # non ancora stabile" — ma qui sul lato POST-paste, mai corretto prima.
            # Un solo check a 1.5s puo' cadere nell'istante in cui la TUI non ha
            # ancora aggiornato il footer dopo il C-m: prima di concludere "non
            # partito" e ripassare il campanello (rischio di ritentare inutilmente
            # per 4 giri fino ad arrendersi con testo pendente), si concede un
            # secondo respiro e si ricontrolla una volta sola.
            time.sleep(2.0)
            pane_check = _run(f"tmux capture-pane -t '{target_window}' -p -S -30").stdout
            if engine_adapter.is_pane_generating(engine, pane_check):
                print(f"[send_a2a] 🔔 notificato (indizio, non prova, seconda lettura): '{target_window}' risulta aver avviato una generazione.")
                return True

        # Se dopo tutti i tentativi il campanello resta lì MAI sottomesso,
        # basta: il deposito su file resta la garanzia vera e qui NON si
        # aggiunge un'altra copia (bug che toglie al destinatario la capacità
        # di vedere quando arriva davvero). Si segnala e si lascia il testo
        # pendente com'è, recuperabile dal file depositato.
        print(f"[send_a2a] ⚠️ Dopo {max_attempts} tentativi, '{target_window}' non risulta aver avviato una generazione e "
              f"l'input box risulta ancora occupato dallo stesso A2A_ID ({a2a_id or '?'}). "
              f"Mi fermo: NON aggiungo altre copie. Il messaggio NON è perso — resta depositato in "
              f"inbox/ finché non viene letto e ackato.", file=sys.stderr)
        # task 6320: invio fallito dopo tutti i tentativi (blocco effettivo)
        _append_send_a2a_esito("BLOCCO", engine, target_window, a2a_id)
        return False
    finally:
        _cleanup()


def _attendi_file_stabile(path: str, timeout_s: float = 5.0, poll_interval: float = 0.2,
                           letture_stabili_richieste: int = 3) -> None:
    """DEV-071 (10/08/2026): varco plausibile trovato indagando il bug storico
    di Radio (corpo troncato, causa sospetta caratteri non-ASCII nel vecchio
    paste diretto pre-DEV-110, gia' chiuso strutturalmente da quel redesign) —
    un chiamante che scrive il file passato a --file e lancia send_a2a.py
    SENZA aspettare che la scrittura sia finita (es. un `echo ... > file &`
    non sincronizzato) farebbe leggere a `open().read()` un file a meta'.

    Aspetta che la DIMENSIONE del file resti identica per `letture_stabili_
    richieste` letture consecutive (default 3, non 2) prima di considerarlo
    pronto. BUG REALE trovato nel mio stesso test PRIMA di consegnare: con
    solo 2 letture consecutive, uno scrittore che scrive a blocchi con pause
    piu' lunghe del poll_interval veniva scambiato per stabile durante una
    delle sue pause (stessa dimensione su due poll ravvicinati, ma ancora in
    scrittura) — 3 letture consecutive stabili riducono di molto il rischio
    di scambiare una pausa per la fine, nessuna garanzia assoluta possibile
    in linea di principio con un controllo a polling, ma un margine reale.
    Solleva se il file non esiste o non si stabilizza entro il timeout:
    errore rumoroso, non un ripiego silenzioso (§9 punto 4)."""
    inizio = time.time()
    ultima_dim = None
    consecutive_stabili = 0
    while time.time() - inizio < timeout_s:
        if not os.path.exists(path):
            time.sleep(poll_interval)
            continue
        dim = os.path.getsize(path)
        if dim == ultima_dim:
            consecutive_stabili += 1
            if consecutive_stabili >= letture_stabili_richieste:
                return
        else:
            consecutive_stabili = 0
        ultima_dim = dim
        time.sleep(poll_interval)
    raise RuntimeError(
        f"'{path}' non si e' stabilizzato entro {timeout_s}s (ancora in scrittura da "
        f"qualcun altro?) — nessuna lettura di un file potenzialmente a meta'"
    )


# --- misura 2 (20/08/2026): helper della guardia payload ---------------------
def _righe_corpo(testo: str) -> list:
    """Righe significative del messaggio (il CORPO): escluse le intestazioni
    [A2A_...] e le righe vuote. E' questo il metro della guardia payload — le
    intestazioni non contano, contano i contenuti che il destinatario deve
    leggere e che gli mangiano contesto."""
    return [r for r in testo.splitlines()
            if r.strip() and not r.startswith("[A2A_")]


def _scrivi_payload_lungo(mittente: str, testo_pieno: str) -> str:
    """Scrive il corpo COMPLETO su /tmp/betty_docs/ (destinazione obbligata
    dalla misura 2) e restituisce il path. Nome univoco: timestamp + uuid.
    Il file e' la copia vera del messaggio che stava per essere incollato
    nella finestra altrui: e' da li' che il destinatario rilegge tutto."""
    ts = time.strftime("%Y%m%d_%H%M%S")
    nome = f"a2a_payload_{mittente or 'sconosciuto'}_{ts}__{uuid.uuid4().hex[:6]}.md"
    path = os.path.join(BETTY_DOCS, nome)
    with open(path, "w", encoding="utf-8") as f:
        f.write(testo_pieno)
    return path


def _testo_con_brief(testo_pieno: str, path_payload: str) -> str:
    """Riscrive il messaggio nel formato §4: intestazioni originali (in cima,
    cosi' campanello e verifica gerarchia ci lavorano come prima) + path del
    file + brief di 2 righe (le prime righe di corpo del testo pieno).
    Il risultato ha <= MAX_RIGHE_A2A righe di corpo: non ritriggera' mai la
    guardia stessa."""
    intestazioni = [r for r in testo_pieno.splitlines() if r.startswith("[A2A_")]
    prima = _righe_corpo(testo_pieno)
    brief = "\n".join(f"Brief: {r}" for r in prima[:2])
    return "\n".join(intestazioni) + (
        f"\n📄 Messaggio lungo ({len(prima)} righe di corpo > {MAX_RIGHE_A2A}): "
        f"contenuto completo su {path_payload}\n"
        f"{brief}\n"
        f"(regola §4: payload lunghi su file, in chat solo path + brief)"
    )


def send_a2a(target_window: str, text: str, max_attempts: int = 4,
             blocca_payload_lungo: bool = True) -> bool:
    target_window_originale = target_window
    target_window = resolve_tmux_window(target_window)
    text = text.strip()
    if not text:
        print("Errore: messaggio vuoto.", file=sys.stderr)
        return False

    # Cerchiamo il tag solo all'inizio vero e proprio del file
    if re.match(r"^(?:\[A2A_FROM:[^\]]+\]\s*)?\[A2A_TYPE:ack_silent\]", text):
        header_match = re.search(r"\[A2A_FROM:([^\]]+)\]", text[:200])
        sender = header_match.group(1) if header_match else "unknown"
        
        try:
            import psycopg2
            import json
            conn = psycopg2.connect("dbname=betty_db user=betty_user host=localhost port=5432")
            cur = conn.cursor()
            
            # Log the message to a2a_silent_logs
            cur.execute(
                "INSERT INTO a2a_silent_logs (sender, recipient, payload) VALUES (%s, %s, %s);",
                (sender, target_window, text)
            )
            
            # Update status in the sender's jsonb data
            cur.execute(
                "UPDATE agents SET data = jsonb_set(data, '{status}', '\"Acknowledged / Working\"') WHERE agent_id = %s;",
                (sender,)
            )
            conn.commit()
            cur.close()
            conn.close()
            print(f"[send_a2a] 🤫 ACK SILENT rilevato. Messaggio loggato e status di '{sender}' aggiornato su Postgres. Nessun invio Tmux.")
            return True
        except Exception as e:
            print(f"[send_a2a] Errore durante l'elaborazione di ack_silent: {e}", file=sys.stderr)
            return False

    # --- misura 2 (20/08/2026): guardia payload §4 ---------------------------
    # Corpo > MAX_RIGHE_A2A → il testo pieno va su file (BETTY_DOCS, obbligato),
    # all'invio resta intestazione + path + brief. Un destinatario deve poter
    # APRIRE il contenuto vero, non ingoiare 40 righe che gli mangiano il
    # contesto ad ogni turno successivo. La riscrittura preserva le
    # intestazioni: campanello, gerarchia e dismesso a valle lavorano come
    # prima. Disattivabile per i casi espliciti (--consenti-payload-lungo).
    if blocca_payload_lungo:
        righe_corpo = _righe_corpo(text)
        if len(righe_corpo) > MAX_RIGHE_A2A:
            mittente = _mittente_da_testo(text)
            path_payload = _scrivi_payload_lungo(mittente, text)
            print(
                f"[send_a2a] ⚠️ Payload di {len(righe_corpo)} righe di corpo > "
                f"MAX_RIGHE_A2A={MAX_RIGHE_A2A}: testo COMPLETO scritto in "
                f"'{path_payload}' (regola §4). Al destinatario va solo "
                f"intestazione + path + brief. Per il corpo pieno in chat: "
                f"--consenti-payload-lungo.",
                file=sys.stderr)
            text = _testo_con_brief(text, path_payload)

    # --- DEV-110 §3/§5: deposito su disco PRIMA di qualunque tentativo di
    # notifica. Questa è la vera consegna. Nessun try/except qui: se fallisce,
    # l'eccezione risale a chi ha chiamato send_a2a() — errore rumoroso, mai
    # un ripiego silenzioso sul vecchio comportamento (§9 punto 4).
    slug_dest = resolve_recipient_slug(target_window_originale)

    # Guardrail coordinatore/sottoposto (§ scope Betty, vedi verifica_gerarchia
    # sopra) — PRIMA del deposito: se un coordinatore sta scavalcando la
    # catena dichiarata, l'errore risale rumoroso qui, il messaggio non parte
    # affatto (nessuna casella orfana, nessun deposito a metà).
    verifica_gerarchia(_mittente_da_testo(text), slug_dest)

    # Rete di sicurezza sulla dismissione (PARTE 3, vedi verifica_dismesso) —
    # PRIMA del deposito: verso un destinatario marcato 'dismesso' l'invio viene
    # RIFIUTATO esplicitamente, mai depositato in silenzio (che lascerebbe il
    # coordinatore convinto di aver delegato senza che il lavoro parta mai).
    verifica_dismesso(slug_dest)

    a2a_id, path_depositato = a2a_inbox.deposita(slug_dest, text)

    # DEV-071 (10/08/2026): verifica byte-per-byte che il file appena scritto
    # su disco corrisponda DAVVERO al testo inteso, invece di fidarsi che
    # deposita() sia andata bene solo perche' non ha sollevato — a2a_inbox.py
    # scrive gia' in modo atomico (tempfile+fsync+rename, verificato) quindi
    # in condizioni normali questo controllo non trovera' mai una differenza,
    # ma e' la rete di sicurezza che chiude per davvero il ticket storico di
    # Radio (corpo troncato dichiarato "Confermato"): se MAI il contenuto
    # scritto non combacia, l'errore risale rumoroso qui, PRIMA di dichiarare
    # consegna riuscita — non dopo, quando ormai e' tardi.
    with open(path_depositato, "r", encoding="utf-8") as _f_verifica:
        scritto = _f_verifica.read()
    if scritto != text:
        raise RuntimeError(
            f"il file depositato in '{path_depositato}' non corrisponde al testo inteso "
            f"({len(scritto)} caratteri scritti contro {len(text)} attesi) — consegna NON dichiarata"
        )

    print(f"[send_a2a] 📥 DEPOSITATO in '{slug_dest}': {path_depositato}")

    campanello = (
        f"{_intestazione_campanello(text, a2a_id)}\n"
        f"📬 Messaggio su disco: {path_depositato} — leggilo e poi: "
        f"core/venv/bin/python3 a2a_ack.py {a2a_id}"
    )
    engine_dest = resolve_engine(slug_dest)
    _notifica_tmux(target_window, campanello, max_attempts, engine=engine_dest)

    # Il ritorno è sempre True da qui in avanti: il deposito È la garanzia di
    # consegna (§5 — "il messaggio non è perso in nessun caso"). La notifica
    # sopra è solo un indizio in più, non la condizione del successo: se
    # fallisse, il messaggio resta comunque leggibile in inbox/.
    return True


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("target_window", help="Finestra tmux destinazione, es. 'comunicazione:AGY-ENRICO'")
    parser.add_argument("message", nargs="?", help="Testo del messaggio (o usa --file)")
    parser.add_argument("--file", help="Percorso di un file contenente il messaggio")
    parser.add_argument("--consenti-payload-lungo", action="store_true",
                        help="salta la guardia payload §4 (misura 2): spedisce in "
                             "chat corpi oltre MAX_RIGHE_A2A senza scriverli su file. "
                             "Sconsigliato: e' esattamente il caso che la misura 2 "
                             "esiste per bloccare.")
    args = parser.parse_args()

    if args.file:
        try:
            _attendi_file_stabile(args.file)
        except RuntimeError as e:
            print(f"[send_a2a] ❌ NON DEPOSITATO: {e}", file=sys.stderr)
            sys.exit(1)
        with open(args.file, "r", encoding="utf-8") as f:
            msg_text = f.read()
    elif args.message:
        msg_text = args.message
    else:
        parser.error("Serve il testo del messaggio oppure --file <path>")

    try:
        ok = send_a2a(args.target_window, msg_text,
                      blocca_payload_lungo=not args.consenti_payload_lungo)
    except Exception as e:
        # Errore rumoroso, exit non-zero, NESSUN ripiego (§9 punto 4): che la
        # causa sia il deposito su disco o — seguito il 09/08 dopo il bug
        # trovato da BGA su 'betty:1' — uno slug destinatario che non
        # corrisponde a nessun agente registrato, il messaggio non e' stato
        # salvato da nessuna parte che qualcuno controlli davvero. Va
        # rimandato dopo aver risolto la causa, non c'e' niente da
        # recuperare a mano.
        print(f"[send_a2a] ❌ NON DEPOSITATO: {e}", file=sys.stderr)
        sys.exit(1)
    sys.exit(0 if ok else 1)
