"""a2a_inbox.py — DEV-110 §3: la casella postale su disco.

Struttura:
    /root/ecosistema_agenti/a2a/<slug_destinatario>/
        inbox/   <ts>__<mittente>__<rand>.md   ← depositato, da leggere
        read/    <stesso file>                  ← letto (spostato dal destinatario)

Stato del messaggio = POSIZIONE del file. Un solo movimento (inbox → read),
nessuno stato ambiguo in mezzo.

⚠️ REQUISITO DI RISERVATEZZA (§9 punto 1 del disegno, non negoziabile): questo
path sta FUORI da /root/Obsidian-Vault apposta — le inbox contengono corpi
interi di messaggi, alcuni con materia personale. Non spostarlo nel vault "per
comodità": lo si toglierebbe dalla portata del grep dal vivo di arturo solo
finché resta qui.

Deposito: file temporaneo nella STESSA cartella di destinazione → fsync →
os.rename atomico. Il destinatario non vede mai un file a metà (stessa
filesystem, stessa directory: os.rename è atomico per costruzione POSIX).
"""
import os
import re
import tempfile
import time
import uuid

BASE = "/root/ecosistema_agenti/a2a"


def path_inbox(slug):
    return os.path.join(BASE, slug, "inbox")


def path_read(slug):
    return os.path.join(BASE, slug, "read")


def _crea_riservato(path):
    """os.makedirs(path, mode=0o2770) applica quel mode SOLO alla cartella
    foglia: le intermedie (qui: BASE/ e BASE/<slug>/) nascono con lo 0o777
    di default attenuato solo dall'umask — tipicamente 0o755, leggibile da
    chiunque sulla macchina. Contro il requisito di riservatezza (§9 punto 1,
    non negoziabile) non basta restringere la foglia: bisogna richiuderle
    dietro di se'.

    FIX 10/08/2026 (regressione reale, priorità massima di Manfredo): era
    0o700 — chiudeva fuori TUTTI tranne root, incluso il gruppo 'dc-agents'
    creato lo stesso giorno per i guardrail Unix (Redattore/Regista/Radio
    lanciati come utenti dedicati non-root). Un chmod fatto a mano da fuori
    su a2a/ veniva silenziosamente ANNULLATO al primo deposito successivo
    (di chiunque, anche root) perché questa funzione gira ad ogni deposita()
    e riporta tutto a 0700 — non un bug, era la guardia di riservatezza
    stessa; ma quella guardia doveva già includere il gruppo fidato, non
    fermarsi al solo owner. 0o2770 (setgid incluso) mantiene lo stesso
    confine di riservatezza contro "chiunque sulla macchina" (0 bit per
    other, invariato) ed estende l'accesso reale al gruppo 'dc-agents' che
    è il canale condiviso previsto dal disegno originale di Betty."""
    os.makedirs(path, 0o2770, exist_ok=True)
    p = path
    while p not in (BASE, os.path.dirname(BASE), "/", ""):
        try:
            os.chmod(p, 0o2770)
        except OSError:
            pass
        p = os.path.dirname(p)
    try:
        os.chmod(BASE, 0o2770)
    except OSError:
        pass


def _nuovo_id(mittente):
    """(ts, mittente_sanificato, rand). Separati e MAI ricomposti con uno
    split: un mittente con underscore nel nome (es. 'aurelio_edilizia') non
    deve rompere il parsing altrove — vedi id_stringa/nome_file sotto.

    10/08/2026 (bug reale segnalato da BGA, causa trovata da Dario): il
    timestamp e' sempre stato UTC (time.gmtime()), ma senza marcatore chi
    legge un nome file tipo "20260810T184336" lo scambia per ora locale —
    su un server in CEST (UTC+2) un messaggio appena depositato sembra
    "vecchio di 2 ore", causa diretta di un allarme falso su Enrico (agente
    creduto irraggiungibile, in realta' i messaggi erano appena arrivati).
    Suffisso "Z" esplicito (standard ISO 8601 per UTC) — SOLO sui nuovi ID,
    quelli vecchi restano invariati (nessuna migrazione, nessun rischio sugli
    ID gia' in circolazione). Verificato prima di aggiungerlo: nessuno script
    nell'ecosistema fa parsing aritmetico della porzione timestamp del nome
    file (solo confronto di stringa esatta in trova_in_inbox), quindi il
    suffisso non rompe nulla."""
    ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    mitt = re.sub(r"[^a-zA-Z0-9-]", "-", mittente or "sconosciuto").strip("-") or "sconosciuto"
    rand = uuid.uuid4().hex[:4]
    return ts, mitt, rand


def id_stringa(ts, mitt, rand):
    return f"{ts}_{mitt}_{rand}"


def nome_file(ts, mitt, rand):
    return f"{ts}__{mitt}__{rand}.md"


def _mittente_da_testo(testo):
    m = re.search(r"\[A2A_FROM:([^\]]+)\]", testo[:200])
    return m.group(1) if m else "sconosciuto"


def deposita(slug_destinatario, testo):
    """Scrive `testo` nell'inbox di `slug_destinatario`. Ritorna (a2a_id, path).

    NESSUN FALLBACK: se un qualunque passaggio fallisce, l'eccezione risale
    al chiamante intatta (§9 punto 4, correzione di Betty: 'un guasto va reso
    rumoroso, mai ammorbidito' — il comportamento vecchio, quello silenzioso,
    è esattamente quello che mente)."""
    inbox = path_inbox(slug_destinatario)
    _crea_riservato(inbox)

    ts, mitt, rand = _nuovo_id(_mittente_da_testo(testo))
    a2a_id = id_stringa(ts, mitt, rand)
    destinazione = os.path.join(inbox, nome_file(ts, mitt, rand))

    fd, tmp_path = tempfile.mkstemp(prefix=".tmp_a2a_", dir=inbox)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(testo)
            f.flush()
            os.fsync(f.fileno())
        # FIX 10/08/2026 (seguito del fix su _crea_riservato): mkstemp crea
        # SEMPRE il file a 0600, a prescindere dai permessi della cartella —
        # è un default di sicurezza della stdlib, non legge la umask né la
        # directory. os.rename() non cambia i bit del file spostato: senza
        # questo chmod, la cartella diventava leggibile dal gruppo 'dc-agents'
        # (fix precedente) ma ogni singolo messaggio restava illeggibile da
        # chiunque tranne l'owner — trovato da BGA su un messaggio reale
        # rimasto bloccato in inbox di Regista. 0o660: stesso confine di
        # riservatezza di _crea_riservato (0 bit per "other", invariato).
        os.chmod(tmp_path, 0o660)
        os.rename(tmp_path, destinazione)
    except Exception:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise

    return a2a_id, destinazione


def trova_in_inbox(a2a_id):
    """Cerca `a2a_id` in TUTTE le inbox — non lo si deduce dalla cartella di
    lavoro di chi chiama: è esattamente la trappola §13.16 del disegno
    (coordinatore e dev che condividono work_dir vengono scambiati).
    Ritorna (slug, path) o (None, None). Solleva se trova più di un file con
    lo stesso id: non deve poter succedere per costruzione (timestamp al
    secondo + 4 hex random), e se succede è un guasto da dire, non da
    risolvere indovinando quale dei due prendere."""
    if not os.path.isdir(BASE):
        return None, None
    trovati = []
    for slug in os.listdir(BASE):
        inbox = path_inbox(slug)
        if not os.path.isdir(inbox):
            continue
        for nome in os.listdir(inbox):
            stem = nome[:-3] if nome.endswith(".md") else nome
            # 17/08/2026 (bug trovato da Valerio): il nome file e l'header
            # [A2A_ID:...] usano il DOPPIO underscore, ma qui si normalizzava
            # solo il nome file — chi incollava l'ID cosi' come pubblicato si
            # sentiva dire "messaggio inesistente" e l'ack (unica prova reale
            # di ricezione, DEV-110) saltava. Si normalizzano ENTRAMBI i lati.
            if stem.replace("__", "_") == a2a_id.replace("__", "_"):
                trovati.append((slug, os.path.join(inbox, nome)))
    if len(trovati) > 1:
        raise RuntimeError(f"ID '{a2a_id}' trovato in piu' di un'inbox: {trovati} — non scelgo da solo")
    if not trovati:
        return None, None
    return trovati[0]


def segna_letto(slug, path):
    """Sposta da inbox/ a read/: LETTO, verificabile da chiunque con un `ls`,
    in qualsiasi momento, senza toccare tmux."""
    read_dir = path_read(slug)
    _crea_riservato(read_dir)
    destinazione = os.path.join(read_dir, os.path.basename(path))
    os.rename(path, destinazione)
    return destinazione
