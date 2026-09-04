"""pool_lib.py — libreria del POOL di coordinamento (progetto piano_pool_samantha.md §2).

PRINCIPIO (MSG 11291): nessun lettore unico. La casella ticket e' l'inbox A2A
del pool (a2a/<pool>/inbox); il file depositato E' il ticket; il CLAIM e' lo
stesso rename atomico inbox/ -> read/ che oggi fa a2a_ack: il primo vince, il
secondo prende FileNotFoundError. Mai due sulla stessa cosa, per costruzione
POSIX (stesso filesystem). La firma (chi-fa-cosa) sta nel ledger claims.jsonl,
le affinita' mittente->istanza in affinity.json: ENTRAMBI scritti solo sotto
flock (§10 del nucleo), perche' dal vincolo 11291 non esiste un'istanza
capofila che possa fare da single-writer.

Nessun fallback silenzioso: ogni esito anomalo o e' un'eccezione tipata
(ClaimRifiutato/GiaPreso/TicketInesistente) o risale intatto al chiamante.
"""
import fcntl
import json
import os
import re
import subprocess
import sys
import tempfile
import time

BASE = "/root/ecosistema_agenti/a2a"
CORE = "/root/ecosistema_agenti/core"
DB = "dbname=betty_db user=betty_user host=localhost port=5432"

sys.path.insert(0, CORE)
import tmux_esatto

# Mittenti ESENTI dalla registrazione di affinita' (FASE 3, 19/08/2026):
# 'manfredo-telegram' e' il flusso del topic Telegram convertito in ticket dal
# motore — legare TUTTO il traffico di Manfredo alla prima istanza che claima
# degenererebbe il pool a istanza singola. I suoi ticket restano
# prendi-chi-e'-libera; l'aggancio dei follow-up allo stesso task arrivera'
# con l'affinita' SEMANTICA della fase B (vettorializzazione, piano §2i).
AFFINITA_ESENTI = frozenset({"manfredo-telegram"})

# Elenco STATICO storico, tenuto solo come rete di sicurezza di ultima istanza
# (vedi scopri_pool()). NON e' piu' la fonte di verita': usare scopri_pool().
#
# 21/08/2026 — perche' e' stato tolto di mezzo (exec_campanellaio_dinamico):
# qui c'era scritto che 'cosimo' era "volutamente escluso perche' ha un demone
# dedicato (cosimo_pool_campanellaio.py, SRV-Pool_Campanellaio_Cosimo)".
# L'esclusione era vera, il demone no: il servizio non era mai stato
# registrato (nessuna unit, nessun processo, nessuna finestra). Risultato: il
# pool cosimo non era servito da NESSUN campanellaio e la sua coda e' rimasta
# ferma 17 ore senza che nulla suonasse. Una lista scritta a mano ha sempre
# questo difetto: dice cosa qualcuno credeva vero quando l'ha scritta.
_POOLS_STATICI = ["samantha", "publio", "auruncia_tech", "zero_one"]
POOLS = _POOLS_STATICI  # compatibilita' con i chiamanti storici

# Cache della scoperta: il campanellaio gira ogni 3s, non ha senso interrogare
# Postgres ad ogni giro. TTL breve perche' un pool nuovo deve essere servito
# senza riavviare niente.
_TTL_SCOPERTA_S = 60
_cache_pool = {"ts": 0, "valore": None, "fonte": None}

# --- ASSEGNAZIONE (29/08/2026, mandato exec_pool_assegnazione) ---------------
# Un ticket da mittente LIBERO (nessuna affinita') viene assegnato all'istanza
# viva meno carica invece di essere messo in palio. L'affinita' resta SEMPRE
# sovrana (vedi claim(): il gate di assegnazione non viene nemmeno guardato
# se il mittente e' gia' legato). Due leve, entrambe configurabili da env
# senza toccare il codice:
#   - ASSEGNAZIONE_FINESTRA_ORE: quanto indietro guardare per misurare il
#     "lavoro attivo" di un'istanza (vedi _carichi_pool).
#   - ASSEGNAZIONE_SCADENZA_MIN: dopo quanti minuti senza claim un'assegnazione
#     decade e il ticket torna libero per chiunque (fail-safe: il lavoro
#     deve essere fatto, mai bloccato su un'istanza che non risponde).
ASSEGNAZIONE_FINESTRA_ORE = float(os.environ.get("POOL_ASSEGNAZIONE_FINESTRA_ORE", "3"))
ASSEGNAZIONE_SCADENZA_MIN = float(os.environ.get("POOL_ASSEGNAZIONE_SCADENZA_MIN", "10"))


def _pool_da_postgres(solo_attivi=True):
    """Pool = record agente con 'pool_di' (l'elenco dei suoi membri).

    Fonte autorevole: e' lo stesso campo che il campanellaio usa per sapere
    CHI svegliare, quindi un pool che sta qui e' per costruzione servibile.

    solo_attivi=False include anche i pool marcati "dismesso": serve alle
    operazioni di pulizia amministrativa (es. release di un'affinita' residua
    rimasta su disco per un pool non piu' servito, vedi
    strumento_agenti.py::_rilascia_affinita_pool_su_elimina) che devono
    raggiungere anche un pool che scopri_pool() normalmente filtra via.
    """
    import psycopg2
    conn = psycopg2.connect(DB)
    try:
        cur = conn.cursor()
        filtro_dismesso = " AND (data->>'dismesso') IS DISTINCT FROM 'true' " if solo_attivi else " "
        cur.execute("SELECT agent_id FROM agents "
                    "WHERE data ? 'pool_di' "
                    "  AND jsonb_array_length(data->'pool_di') > 0 "
                    f"{filtro_dismesso}"
                    "ORDER BY agent_id;")
        return [r[0] for r in cur.fetchall()]
    finally:
        conn.close()


def _pool_da_disco():
    """Ripiego: cartelle a2a/<nome>/pool/ CON inbox accanto.

    Meno affidabile del DB — su disco restano cartelle di prove vecchie (es.
    'pool_test', che non ha nessun record agente) — quindi si usa solo se
    Postgres non risponde, e mai in silenzio.
    """
    out = []
    for nome in sorted(os.listdir(BASE)):
        if os.path.isdir(os.path.join(BASE, nome, "pool")) and \
           os.path.isdir(os.path.join(BASE, nome, "inbox")):
            out.append(nome)
    return out


def scopri_pool(forza=False, solo_attivi=True):
    """Elenco dei pool da servire, ricavato dai DATI e non da una lista a mano.

    Ritorna (pool, fonte) dove fonte e' 'postgres' | 'disco' | 'statico'.
    Il degrado non e' MAI silenzioso (docstring del modulo): chi ripiega lo
    dice al chiamante, che lo scrive nel log e nel registro di stato.

    solo_attivi=False (uso raro, pulizia amministrativa) include anche i pool
    dismessi e bypassa la cache: correttezza prima del TTL, non e' un percorso
    chiamato ad ogni giro come il campanellaio.
    """
    if not solo_attivi:
        fonte, pool = "postgres", None
        try:
            pool = _pool_da_postgres(solo_attivi=False)
            if not pool:
                raise RuntimeError("nessun record con 'pool_di': DB raggiungibile ma vuoto")
        except Exception as e:
            print(f"[pool_lib] scoperta (solo_attivi=False) da Postgres fallita ({e}); ripiego su disco", flush=True)
            try:
                pool, fonte = _pool_da_disco(), "disco"
            except Exception as e2:
                print(f"[pool_lib] anche il ripiego su disco e' fallito ({e2}); "
                      f"uso l'elenco statico storico", flush=True)
                pool, fonte = list(_POOLS_STATICI), "statico"
        return list(pool), fonte

    adesso = time.time()
    if not forza and _cache_pool["valore"] is not None and \
            (adesso - _cache_pool["ts"]) < _TTL_SCOPERTA_S:
        return list(_cache_pool["valore"]), _cache_pool["fonte"]

    fonte, pool = "postgres", None
    try:
        pool = _pool_da_postgres()
        if not pool:
            raise RuntimeError("nessun record con 'pool_di': DB raggiungibile ma vuoto")
    except Exception as e:
        print(f"[pool_lib] scoperta da Postgres fallita ({e}); ripiego su disco", flush=True)
        try:
            pool, fonte = _pool_da_disco(), "disco"
        except Exception as e2:
            print(f"[pool_lib] anche il ripiego su disco e' fallito ({e2}); "
                  f"uso l'elenco statico storico", flush=True)
            pool, fonte = list(_POOLS_STATICI), "statico"

    _cache_pool.update({"ts": adesso, "valore": list(pool), "fonte": fonte})
    return list(pool), fonte


class ClaimRifiutato(Exception):
    """Il mittente del ticket e' legato (affinita') a un'ALTRA istanza."""


class GiaPreso(Exception):
    """Un'altra istanza ha gia' claimato questo ticket (rename perso)."""


class TicketInesistente(Exception):
    """Nessun file in inbox con questo a2a_id."""


class AssegnazioneAltrove(ClaimRifiutato):
    """Il ticket (mittente LIBERO, nessuna affinita') e' assegnato a
    un'altra istanza viva perche' meno carica, ed e' entro la finestra di
    scadenza (ASSEGNAZIONE_SCADENZA_MIN): nessuna gara aperta finche' non
    decade. Sottoclasse di ClaimRifiutato apposta: chi gia' gestisce
    ClaimRifiutato (pool_claim.py, exit 3, "RIFIUTATO") continua a
    funzionare senza modifiche, con un messaggio che pero' distingue
    l'assegnazione dal blocco per affinita'."""


def inbox_dir(pool):
    return os.path.join(BASE, pool, "inbox")


def read_dir(pool):
    return os.path.join(BASE, pool, "read")


def pool_dir(pool):
    return os.path.join(BASE, pool, "pool")


def ledger_path(pool):
    return os.path.join(pool_dir(pool), "claims.jsonl")


def affinity_path(pool):
    return os.path.join(pool_dir(pool), "affinity.json")


def _crea_riservato(path):
    """Stessi permessi delle caselle A2A (0o2770, gruppo dc-agents): il
    materiale del pool sta accanto alle inbox e ne eredita la riservatezza."""
    os.makedirs(path, 0o2770, exist_ok=True)
    try:
        os.chmod(path, 0o2770)
    except OSError:
        pass


class LockPool:
    """flock esclusivo sul pool: serializza claim/release/affinita'.
    Si rilascia DA SOLO alla morte del processo (stesso principio dello
    snippet core/single_instance_snippet.py: mai pidfile stantii)."""

    def __init__(self, pool):
        _crea_riservato(pool_dir(pool))
        self._path = os.path.join(pool_dir(pool), ".lock")
        self._fh = None

    def __enter__(self):
        self._fh = open(self._path, "w")
        fcntl.flock(self._fh, fcntl.LOCK_EX)  # bloccante: un claim dura ms
        return self

    def __exit__(self, *exc):
        try:
            fcntl.flock(self._fh, fcntl.LOCK_UN)
        finally:
            self._fh.close()
        return False


def mittente_da_file(path):
    """[A2A_FROM:x] dal corpo del ticket (stesso pattern di a2a_inbox)."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            testo = f.read(400)
    except OSError:
        return "sconosciuto"
    m = re.search(r"\[A2A_FROM:([^\]]+)\]", testo)
    return m.group(1).strip().lower() if m else "sconosciuto"


def trova_ticket(pool, a2a_id):
    """(nome_file, path) del ticket in inbox, o TicketInesistente.
    Normalizza __/_ come a2a_inbox.trova_in_inbox (fix Valerio 17/08)."""
    box = inbox_dir(pool)
    if not os.path.isdir(box):
        raise TicketInesistente(f"inbox di '{pool}' inesistente: {box}")
    voluto = a2a_id.replace("__", "_")
    for nome in sorted(os.listdir(box)):
        if nome.startswith("."):
            continue
        stem = nome[:-3] if nome.endswith(".md") else nome
        if stem.replace("__", "_") == voluto:
            return nome, os.path.join(box, nome)
    raise TicketInesistente(f"nessun ticket '{a2a_id}' in {box}")


def leggi_affinita(pool):
    try:
        with open(affinity_path(pool), "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def _scrivi_affinita(pool, aff):
    """Scrittura atomica (tempfile+rename) — chiamare SOLO sotto LockPool."""
    _crea_riservato(pool_dir(pool))
    fd, tmp = tempfile.mkstemp(prefix=".tmp_aff_", dir=pool_dir(pool))
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(aff, f, ensure_ascii=False, indent=1)
        f.flush()
        os.fsync(f.fileno())
    os.chmod(tmp, 0o660)
    os.rename(tmp, affinity_path(pool))


def _append_ledger(pool, riga):
    """Append di una riga jsonl — chiamare SOLO sotto LockPool."""
    _crea_riservato(pool_dir(pool))
    with open(ledger_path(pool), "a", encoding="utf-8") as f:
        f.write(json.dumps(riga, ensure_ascii=False) + "\n")
    try:
        os.chmod(ledger_path(pool), 0o660)
    except OSError:
        pass


def leggi_ledger(pool):
    try:
        with open(ledger_path(pool), "r", encoding="utf-8") as f:
            return [json.loads(r) for r in f if r.strip()]
    except FileNotFoundError:
        return []


def chi_ha_claimato(pool, a2a_id):
    voluto = a2a_id.replace("__", "_")
    for r in leggi_ledger(pool):
        if r.get("a2a_id", "").replace("__", "_") == voluto and r.get("evento") == "claim":
            return r.get("istanza")
    return None


def istanza_corrente(esplicita=None):
    """Slug dell'istanza che chiama: esplicito (test/CLI), altrimenti derivato
    dalla PROPRIA finestra tmux via Postgres — stesso metodo gia' in
    produzione in a2a_ack.py::_chiamante (finestra, MAI la cartella di
    lavoro, MAI la sessione intera: e' il difetto 24-bis n.2 da non ripetere).
    Nessun fallback: identita' incerta = errore, mai un'identita' indovinata."""
    if esplicita:
        return esplicita.strip().lower()
    pane = os.environ.get("TMUX_PANE")
    if not pane:
        raise RuntimeError("istanza non determinabile: fuori tmux e nessuna --istanza esplicita")
    finestra = subprocess.run(
        ["tmux", "display-message", "-p", "-t", pane, "#{session_name}:#{window_name}"],
        capture_output=True, text=True, timeout=5).stdout.strip()
    if not finestra:
        raise RuntimeError("istanza non determinabile: finestra tmux non risolta")
    import psycopg2
    conn = psycopg2.connect(DB)
    try:
        cur = conn.cursor()
        # 22/08/2026 FIX classe B (integrazione mandato di samantha_1): MAI
        # fetchone su chiave non univoca. Resolver condiviso
        # tmux_esatto.risolvi_agente_da_finestra: esclude i virtuali
        # (pool_di/alias_di — fino a FASE 3 la finestra di samantha_1 e'
        # condivisa col record pool 'samantha') e, se restano piu' righe REALI
        # sulla stessa finestra, LO DICE — identita' incerta = errore, mai
        # un'identita' indovinata.
        agent, ambigui = tmux_esatto.risolvi_agente_da_finestra(finestra, cur)
    finally:
        conn.close()
    if agent:
        return agent
    if ambigui:
        raise RuntimeError(f"istanza non determinabile: finestra '{finestra}' condivisa da "
                           f"piu' record reali ({', '.join(ambigui)})")
    raise RuntimeError(f"istanza non determinabile: finestra '{finestra}' senza agente in Postgres")


def _epoch_utc(ts_iso):
    """Epoch UTC da un timestamp 'YYYY-MM-DDTHH:MM:SSZ' del ledger, o None."""
    try:
        import calendar
        return calendar.timegm(time.strptime(ts_iso, "%Y-%m-%dT%H:%M:%SZ"))
    except (TypeError, ValueError):
        return None


def _istanze_pool_da_postgres(pool):
    """Membri dichiarati del pool (pool_di), letti DIRETTAMENTE dal record
    'pool' anche se quel record e' dismesso: il dismesso riguarda il POOL
    (es. 'publio', gia' cosi' in produzione), non le singole istanze, che
    restano valide finche' non sono dismesse loro stesse (controllato da
    _istanza_viva)."""
    import psycopg2
    conn = psycopg2.connect(DB)
    try:
        cur = conn.cursor()
        cur.execute("SELECT data->'pool_di' FROM agents WHERE agent_id = %s;", (pool,))
        row = cur.fetchone()
        return list(row[0]) if row and row[0] else []
    finally:
        conn.close()


def _istanza_viva(istanza):
    """True SOLO se l'istanza non e' dismessa E la sua finestra tmux ESATTA
    esiste davvero ORA (tmux_esatto.finestra_viva) — mai fidarsi del solo
    record Postgres (vincolo 3a del mandato di assegnazione: non si assegna
    mai a un'istanza spenta/morta)."""
    import psycopg2
    conn = psycopg2.connect(DB)
    try:
        cur = conn.cursor()
        cur.execute("SELECT data->>'tmux_window', data->>'dismesso' FROM agents WHERE agent_id = %s;", (istanza,))
        row = cur.fetchone()
    finally:
        conn.close()
    if not row:
        return False
    finestra, dismesso = row
    if (dismesso or "").strip().lower() == "true":
        return False
    if not finestra:
        return False
    return tmux_esatto.finestra_viva(finestra)


def istanze_vive_pool(pool):
    """Istanze del pool realmente vive ORA. Fail-safe: qualunque errore di
    lettura (Postgres irraggiungibile, ecc.) fa TRATTARE l'istanza/il pool
    come non assegnabile, mai il contrario — un errore qui deve al massimo
    disattivare l'assegnazione (si torna alla gara libera), mai bloccare un
    ticket su un'istanza che non si e' potuto verificare."""
    try:
        import psycopg2
        conn = psycopg2.connect(DB)
        try:
            cur = conn.cursor()
            cur.execute("SELECT data->'pool_di' FROM agents WHERE agent_id = %s;", (pool,))
            row = cur.fetchone()
            membri = list(row[0]) if row and row[0] else []
            if not membri:
                return []
            cur.execute("SELECT agent_id, data->>'tmux_window', data->>'dismesso' FROM agents WHERE agent_id = ANY(%s);", (membri,))
            rows = {r[0]: (r[1], r[2]) for r in cur.fetchall()}
        finally:
            conn.close()
    except Exception as e:
        print(f"[pool_lib] assegnazione: lettura membri di '{pool}' fallita ({e}); "
              f"nessuna assegnazione per questo giro (fail-safe verso la gara libera)", flush=True)
        return []
    vive = []
    for m in membri:
        info = rows.get(m)
        if not info:
            continue
        finestra, dismesso = info
        if (dismesso or "").strip().lower() == "true":
            continue
        if not finestra:
            continue
        try:
            if tmux_esatto.finestra_viva(finestra):
                vive.append(m)
        except Exception as e:
            print(f"[pool_lib] assegnazione: verifica vitalita' di '{m}' fallita ({e}); "
                  f"trattata come NON viva per cautela", flush=True)
    return vive


def _carichi_pool(pool, finestra_ore):
    """dict istanza -> numero di claim negli ultimi 'finestra_ore' ore
    (criterio di carico: lavoro ATTIVO recente, non le affinita' totali —
    quelle sono un cricchetto storico, vedi docstring ASSEGNAZIONE_*).
    Il ledger e' cronologico in append: si scorre dalla fine e ci si ferma
    al primo evento fuori finestra, costo proporzionale al carico recente."""
    soglia = time.time() - finestra_ore * 3600
    carichi = {}
    for r in reversed(leggi_ledger(pool)):
        ep = _epoch_utc(r.get("ts", ""))
        if ep is None:
            continue
        if ep < soglia:
            break
        if r.get("evento") == "claim":
            ist = r.get("istanza")
            carichi[ist] = carichi.get(ist, 0) + 1
    return carichi


def _tutte_assegnazioni(pool):
    """dict a2a_id -> {'istanza','ts','risolta'} con l'ULTIMA assegnazione
    per ogni ticket, marcata risolta se e' seguita da un claim (unico scan
    del ledger, riusato sia dal gate in claim() sia da stato())."""
    mappa = {}
    for r in leggi_ledger(pool):
        aid = r.get("a2a_id")
        if not aid:
            continue
        aid_norm = aid.replace("__", "_")
        ev = r.get("evento")
        if ev == "assegnazione":
            mappa[aid_norm] = {"istanza": r.get("istanza"), "ts": r.get("ts"), "risolta": False}
        elif ev == "claim" and aid_norm in mappa:
            mappa[aid_norm]["risolta"] = True
    return mappa


def _valuta_assegnazione(voce, scadenza_min):
    """Stato dell'assegnazione di un ticket:
      {'stato': 'nessuna'}   mai assegnato -> va assegnato ora
      {'stato': 'risolta'}   gia' claimato -> nessun gate
      {'stato': 'attiva', 'istanza', 'scade_tra_s'}   entro scadenza -> gate
      {'stato': 'scaduta', 'istanza', 'ts'}   scaduta -> libera per chiunque,
                                               NESSUNA riassegnazione (per
                                               non ri-bloccare in loop sulla
                                               stessa istanza se questa resta
                                               "meno carica" pur non
                                               rispondendo)
    """
    if voce is None:
        return {"stato": "nessuna"}
    if voce.get("risolta"):
        return {"stato": "risolta"}
    ep = _epoch_utc(voce.get("ts", ""))
    if ep is None:
        return {"stato": "nessuna"}
    residuo = scadenza_min * 60 - (time.time() - ep)
    if residuo <= 0:
        return {"stato": "scaduta", "istanza": voce.get("istanza"), "ts": voce.get("ts")}
    return {"stato": "attiva", "istanza": voce.get("istanza"), "scade_tra_s": int(residuo)}


def claim(pool, a2a_id, istanza):
    """Prende in carico un ticket. Ritorna il path del file claimato (in read/).

    Sequenza sotto lock (il lock rende COERENTE l'affinita'; l'atomicita' del
    claim in se' resta il rename, che vincerebbe anche senza lock):
      1. guardia di affinita': mittente legato ad ALTRA istanza -> ClaimRifiutato
      2. rename inbox -> read (il claim vero; ENOENT -> GiaPreso)
      3. firma sul ledger
      4. registrazione/refresh dell'affinita' mittente->istanza
    """
    # Il ticket puo' sparire dall'inbox fra questo sguardo e il lock (un'altra
    # istanza ha vinto il rename): "non in inbox MA claimato a ledger" e'
    # GIA' PRESO, non un id sbagliato — trovato dal pre-collaudo S2, dove il
    # perdente vedeva '❌ INESISTENTE' e avrebbe potuto credere il ticket perso.
    try:
        nome, path = trova_ticket(pool, a2a_id)
    except TicketInesistente:
        vincitore = chi_ha_claimato(pool, a2a_id)
        if vincitore:
            raise GiaPreso(f"ticket {a2a_id} gia' preso da '{vincitore}' — nessuna doppia presa")
        raise
    mitt = mittente_da_file(path)
    with LockPool(pool):
        aff = leggi_affinita(pool)
        legato = (aff.get(mitt) or {}).get("istanza")
        if legato and legato != istanza:
            raise ClaimRifiutato(
                f"il mittente '{mitt}' e' legato a '{legato}' (affinita' vincolante): "
                f"il ticket {a2a_id} spetta a quella istanza. Se e' morta oltre soglia, "
                f"takeover esplicito con pool_release.py --takeover, mai un claim zitto.")

        # ASSEGNAZIONE (29/08/2026): solo sui mittenti LIBERI (legato e' None
        # qui sopra), e solo se il pool ha almeno 2 istanze VIVE — con meno di
        # 2 non c'e' nessuna gara da arbitrare: NO-OP totale, stesso
        # comportamento di sempre (vincolo 4 del mandato).
        # 29/08 samantha_2: gli AFFINITA_ESENTI (manfredo-telegram) devono restare
        # fuori ANCHE dall'assegnazione, non solo dall'affinita' (vedi r.530).
        # Senza questo, ogni messaggio Telegram di Manfredo veniva "prenotato"
        # fino a ASSEGNAZIONE_SCADENZA_MIN su una sola istanza e le altre lo
        # rifiutavano: i suoi messaggi potevano restare fermi fino a 10 minuti.
        if not legato and mitt not in AFFINITA_ESENTI:
            vive = istanze_vive_pool(pool)
            if len(vive) >= 2:
                mappa = _tutte_assegnazioni(pool)
                info = _valuta_assegnazione(mappa.get(a2a_id.replace("__", "_")), ASSEGNAZIONE_SCADENZA_MIN)
                ora = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                if info["stato"] == "scaduta":
                    _append_ledger(pool, {"evento": "scadenza_assegnazione", "a2a_id": a2a_id,
                                          "mittente": mitt, "istanza_scaduta": info["istanza"],
                                          "assegnata_il": info["ts"], "ts": ora})
                    info = {"stato": "libera"}  # decade UNA volta sola: da qui in poi gara libera
                if info["stato"] == "nessuna":
                    carichi = _carichi_pool(pool, ASSEGNAZIONE_FINESTRA_ORE)
                    scelta = sorted(vive, key=lambda i: (carichi.get(i, 0), i))[0]
                    _append_ledger(pool, {"evento": "assegnazione", "a2a_id": a2a_id, "mittente": mitt,
                                          "istanza": scelta, "candidati": vive, "carichi": carichi,
                                          "finestra_ore": ASSEGNAZIONE_FINESTRA_ORE,
                                          "scadenza_min": ASSEGNAZIONE_SCADENZA_MIN, "ts": ora})
                    info = {"stato": "attiva", "istanza": scelta, "scade_tra_s": int(ASSEGNAZIONE_SCADENZA_MIN * 60)}
                if info["stato"] == "attiva" and info["istanza"] != istanza:
                    raise AssegnazioneAltrove(
                        f"ticket {a2a_id} assegnato a '{info['istanza']}' (istanza piu' scarica): "
                        f"nessuna gara aperta per altri {info['scade_tra_s']}s. Se non risponde entro "
                        f"la scadenza il ticket torna libero da solo — nessun'azione da fare.")
                # info['stato'] in ('attiva' con istanza==istanza, 'risolta', 'libera'): via libera.

        _crea_riservato(read_dir(pool))
        destinazione = os.path.join(read_dir(pool), nome)
        try:
            os.rename(path, destinazione)
        except FileNotFoundError:
            vincitore = chi_ha_claimato(pool, a2a_id) or "?"
            raise GiaPreso(f"ticket {a2a_id} gia' preso da '{vincitore}' — nessuna doppia presa")
        _append_ledger(pool, {"evento": "claim", "a2a_id": a2a_id, "istanza": istanza,
                              "mittente": mitt, "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
        if mitt not in AFFINITA_ESENTI:
            voce = aff.get(mitt) or {}
            voce.update({"istanza": istanza,
                         "ultimo_claim": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                         "ultimo_a2a_id": a2a_id})
            aff[mitt] = voce
            _scrivi_affinita(pool, aff)
    return destinazione


def release(pool, mittente, istanza, takeover_da=None):
    """Chiude l'affinita' di un mittente (task finito) o esegue un takeover
    esplicito. Solo l'istanza legata puo' rilasciare; il takeover richiede di
    dichiarare da chi si sta prendendo (annotato nel ledger, mai silenzioso)."""
    mittente = mittente.strip().lower()
    with LockPool(pool):
        aff = leggi_affinita(pool)
        voce = aff.get(mittente)
        if not voce:
            raise ValueError(f"nessuna affinita' registrata per '{mittente}' nel pool '{pool}'")
        legato = voce.get("istanza")
        if takeover_da:
            if legato != takeover_da:
                raise ValueError(f"takeover incoerente: '{mittente}' e' legato a '{legato}', non a '{takeover_da}'")
            evento = {"evento": "takeover", "mittente": mittente, "da": legato, "a": istanza}
            voce["istanza"] = istanza
            aff[mittente] = voce
        else:
            if legato != istanza:
                raise ValueError(f"'{istanza}' non puo' rilasciare '{mittente}': e' legato a '{legato}'")
            evento = {"evento": "release", "mittente": mittente, "istanza": istanza}
            del aff[mittente]
        evento["ts"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        _append_ledger(pool, evento)
        _scrivi_affinita(pool, aff)


def release_amministrativo(pool, mittente, motivo, chi=None):
    """Rilascio AMMINISTRATIVO dell'affinita' di un mittente (28/29-08-2026,
    mandato exec_fix_pool_affinita): usato da `elimina_exec` per chiudere
    l'affinita' dello slug dismesso, e da `pool_gc.py` per i mittenti DISMESSI
    stantii. A differenza di release() NON richiede che il chiamante sia
    l'istanza legata: qui il chiamante e' un'operazione di amministrazione
    (dismissione o pulizia), non un'istanza del pool che chiude il proprio
    task. Per questo e' additiva e non tocca la semantica di release(), che
    resta l'unico modo con cui un'ISTANZA chiude/passa la propria affinita'.

    FAIL-OPEN per costruzione: se il mittente non e' legato a nessuno, non e'
    un errore (nulla da rilasciare) — ritorna None senza toccare il file.
    Se rilascia, ritorna l'istanza che deteneva l'affinita' e logga SEMPRE
    l'evento nel ledger (mai un passaggio di mano silenzioso, stesso principio
    di release())."""
    mittente = mittente.strip().lower()
    with LockPool(pool):
        aff = leggi_affinita(pool)
        voce = aff.get(mittente)
        if not voce:
            return None
        istanza_precedente = voce.get("istanza")
        del aff[mittente]
        evento = {
            "evento": "release_admin",
            "mittente": mittente,
            "istanza_precedente": istanza_precedente,
            "motivo": motivo,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        if chi:
            evento["chi"] = chi
        _append_ledger(pool, evento)
        _scrivi_affinita(pool, aff)
    return istanza_precedente


def stato(pool):
    """Fotografia leggibile: ticket in attesa + affinita' + ultimi claim.
    E' il 'chi-fa-cosa si vede sempre dalla coda' del vincolo 11291."""
    righe = []
    box = inbox_dir(pool)
    attesa = sorted(n for n in os.listdir(box) if not n.startswith(".")) if os.path.isdir(box) else []
    righe.append(f"POOL '{pool}' — {len(attesa)} ticket in attesa")
    mappa_ass = _tutte_assegnazioni(pool) if attesa else {}
    # Normalizzo __/_ su ENTRAMBI i lati prima del confronto, stesso pattern
    # gia' in uso in trova_ticket()/chi_ha_claimato(): il filename in inbox e
    # l'a2a_id scritto nel ledger possono differire solo per il numero di
    # underscore, altrimenti il badge di assegnazione si perde in silenzio
    # (P7, mandato exec_nit_selfheal).
    mappa_ass_norm = {k.replace("__", "_"): v for k, v in mappa_ass.items()}
    for n in attesa:
        riga = f"  ⏳ {n}  (da: {mittente_da_file(os.path.join(box, n))})"
        stem = n[:-3] if n.endswith(".md") else n
        info = _valuta_assegnazione(mappa_ass_norm.get(stem.replace("__", "_")), ASSEGNAZIONE_SCADENZA_MIN)
        if info["stato"] == "attiva":
            riga += f"  🎯 assegnato a '{info['istanza']}' (scade fra {info['scade_tra_s']}s)"
        elif info["stato"] == "scaduta":
            riga += f"  ⌛ assegnazione a '{info['istanza']}' scaduta: libero per chiunque"
        righe.append(riga)
    aff = leggi_affinita(pool)
    righe.append(f"AFFINITA' ({len(aff)} mittenti legati):")
    for m, v in sorted(aff.items()):
        righe.append(f"  🔗 {m} → {v.get('istanza')}  (ultimo claim {v.get('ultimo_claim')})")
    ledger = leggi_ledger(pool)
    righe.append(f"LEDGER (ultimi {min(10, len(ledger))} di {len(ledger)} eventi):")
    for r in ledger[-10:]:
        righe.append("  📝 " + json.dumps(r, ensure_ascii=False))
    return "\n".join(righe)
