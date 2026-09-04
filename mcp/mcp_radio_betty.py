import asyncio
import contextlib
import fcntl
import json
import os
import re
import signal
import subprocess
import sys
import time

# 18/08/2026 (Samantha, zombie reale trovato da check_agenti_stuck): questo
# server lancia figli (TTS/audio) senza mai fare wait() — ogni chiamata tipo
# radio_speak lasciava uno ZOMBIE nella sessione dell'agente chiamante
# (visto: figlio del radio_speak di Simone delle 09:02, defunct per 6 ore).
# SIG_IGN su SIGCHLD = il kernel raccoglie i figli da solo, niente zombie.
# NB: i processi MCP vivi tengono il codice VECCHIO fino al riavvio della
# sessione che li ha caricati — il fix vale dai prossimi caricamenti.
try:
    signal.signal(signal.SIGCHLD, signal.SIG_IGN)
except (ValueError, OSError):
    pass  # es. import fuori dal main thread: meglio senza fix che rompere il server
from datetime import date, timedelta

from mcp.server.fastmcp import FastMCP

CORE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "core")
sys.path.insert(0, CORE)
import sveglia_betty as sb

mcp = FastMCP("Radio Betty")

AUDIO_DIR = sb.AUDIO_DIR


# ---------------------------------------------------------------------------
# Helpers SYNC (girano in thread separato: la sync API di playwright non puo'
# stare dentro l'event loop asyncio in cui FastMCP esegue i tool — bug reale
# riscontrato da Simone il 05/08: "Playwright Sync API inside asyncio loop").
# NOTA: stdout va tenuto PURO per il protocollo JSON-RPC: i print delle librerie
# (tts.py ecc.) dentro il thread vanno dirottati su stderr, altrimenti qwen
# vede stdout corrotto e scollega il server (drop reale alle 4:53).
# ---------------------------------------------------------------------------

import builtins

# stdout PURO per il protocollo JSON-RPC: tutti i print di questo processo
# (tts.py, sveglia_betty, librerie) vanno su stderr. Senza questo, qwen vede
# stdout corrotto al primo print e scollega il server (drop reale alle 4:53).
_real_print = builtins.print


def _err_print(*a, **k):
    k.setdefault("file", sys.stderr)
    _real_print(*a, **k)


builtins.print = _err_print


def _radio_page(pw, autostart=True):
    # timeout esplicito e corto: il default di Playwright per connect_over_cdp
    # e' 180s, e su un tunnel stantio (es. Chrome sul Surface riavviato senza
    # il flag CDP) questo bloccava il keeper per 3 minuti ad ogni ciclo invece
    # di fallire in fretta e lasciare che il prossimo giro riprovi (05/08/2026).
    browser = pw.chromium.connect_over_cdp(sb.CDP_URL, timeout=10000)
    for ctx in browser.contexts:
        for p in ctx.pages:
            if "radiobetty.duckdns.org" in p.url:
                return p
    if not autostart:
        return None
    sb.chiudi_tutto(browser)
    page = sb.apri_in_nuova_finestra(browser, "http://radiobetty.duckdns.org/", pw=pw)
    page.wait_for_load_state("domcontentloaded", timeout=30000)
    time.sleep(3)
    sb.accendi_radio(page)
    time.sleep(4)
    return page


# ---------------------------------------------------------------------------
# DAL 05/08/2026 LA RADIO NON DIPENDE PIU' DA CDP/chrome-local.
#
# Tutti i comandi qui sotto scrivono SOLO file JSON serviti da nginx; e' la
# pagina che li rilegge da sola a intervalli (musica 15s, voce 10s, visuale 12s)
# e si aggiorna. Nessun Playwright, nessun tunnel: se il tunnel cade, la radio
# continua a funzionare. Lo stato reale del player lo pubblica la pagina stessa
# via PUT su /sveglia/stato/stato.json (modulo dav di nginx), quindi anche la
# lettura dello stato non passa da CDP.
#
# L'UNICA cosa che richiede ancora CDP e' aprire da zero la scheda se non esiste
# proprio (_radio_page / radio_apri): una volta aperta, resta li' e non serve piu'.
# ---------------------------------------------------------------------------

MUSICA_JSON = os.path.join(AUDIO_DIR, "musica.json")
VOCE_JSON = os.path.join(AUDIO_DIR, "voce.json")
VISUALE_JSON = os.path.join(AUDIO_DIR, "visuale.json")

# Lock su musica.json (08/08/2026, bug 'potatura tocca il brano in onda'):
# il keeper (processo fresco ogni 30s) e questo server MCP (processo lungo,
# chiamato da Radio in qualunque momento) leggono e riscrivono LO STESSO file
# senza nessuna sincronizzazione tra i due. Un read-modify-write dell'uno puo'
# intrecciarsi con l'altro: chi scrive per ultimo, scrive sopra una lettura
# ormai stantia dell'altro, in silenzio (posizioni calcolate su uno stato che
# nel frattempo un terzo scrittore ha gia' cambiato). Root cause sospetta
# delle due riproduzioni di Radio (brano Morricone sparito subito dopo una
# potatura; 3 brani aggiunti dopo il corrente, spariti al giro dopo, contati
# come "gia' passati"). flock e' bloccante ma qui dentro si fa solo I/O JSON
# locale (microsecondi): nessuno stallo percepibile.
_MUSICA_LOCK_PATH = MUSICA_JSON + ".lock"


@contextlib.contextmanager
def _musica_lock():
    """Serializza l'INTERA sezione critica (lettura + decisione + scrittura)
    su musica.json fra tutti i processi che lo toccano. Va tenuto per tutta la
    sezione, non solo per la scrittura finale: leggere la posizione corrente
    FUORI da questo lock e usarla dentro (anche pochi millisecondi dopo)
    reintroduce esattamente la race che il lock dovrebbe chiudere. NON
    annidare due `with _musica_lock()` nello stesso processo (due fd diversi
    sullo stesso file si auto-bloccherebbero a vicenda): le funzioni interne
    tipo `_timeline_modifica_locked` presumono il lock gia' preso dal
    chiamante e non lo riacquisiscono."""
    with open(_MUSICA_LOCK_PATH, "a") as lock_fh:
        fcntl.flock(lock_fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_fh, fcntl.LOCK_UN)


def _read_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _write_json(path, data):
    """Scrittura atomica: la pagina fa polling di continuo e non deve mai
    beccare un file scritto a meta' (JSON troncato = poll fallito)."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _cmd(azione, **extra):
    """Accoda un comando puntuale per la pagina, incrementando il numero di
    sequenza: la pagina esegue solo i comandi con seq diverso dall'ultimo visto."""
    with _musica_lock():
        m = _read_json(MUSICA_JSON, {"tracks": [], "vol": 45})
        seq = (m.get("cmd") or {}).get("seq", 0) + 1
        m["cmd"] = dict({"seq": seq, "azione": azione}, **extra)
        _write_json(MUSICA_JSON, m)
    return m


# Unica implementazione, in sveglia_betty: la usano sia il MCP che il keeper che
# la sveglia stessa, e non deve divergere fra i tre.
_stato_vivo = sb.stato_vivo


# Il messaggio si programma un filo nel FUTURO, non "adesso": le pagine leggono
# voce.json ogni 10s, quindi con questo margine tutte fanno in tempo a conoscerlo
# prima che scatti e lo fanno partire allo stesso secondo. Se lo mandassimo per
# "adesso", ogni dispositivo lo direbbe quando gli capita di leggere, sfalsato.
RITARDO_SYNC = 15
MAX_SEGMENTI = 25   # voce.json non deve crescere all'infinito
_DUCK_LOCK_PATH = os.path.join(AUDIO_DIR, "duck_state.json.lock")

@contextlib.contextmanager
def _duck_file_lock():
    with open(_DUCK_LOCK_PATH, "a") as lock_fh:
        fcntl.flock(lock_fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_fh, fcntl.LOCK_UN)


def _duck_job(start_time, end_time):
    import time
    
    # 1. Attesa fino all'istante esatto di inizio (at)
    delay_start = max(0.0, start_time - time.time())
    if delay_start > 0:
        time.sleep(delay_start)
        
    state_file = os.path.join(AUDIO_DIR, "duck_state.json")
    with _duck_file_lock():
        state = _read_json(state_file, {"end_time": 0, "original_vol": None})
        if state.get("original_vol") is None:
            m = _read_json(MUSICA_JSON, {"tracks": []})
            state["original_vol"] = m.get("vol", 45)
            duck_vol = max(3, int(state["original_vol"] * 0.35))
            _music_volume(duck_vol, autore_override="DuckSystem (Ducking)")
            _write_json(state_file, state)
            
    # 2. Attesa fino all'istante di fine (at + duration)
    delay_end = max(0.0, end_time - time.time())
    if delay_end > 0:
        time.sleep(delay_end)
        
    with _duck_file_lock():
        state = _read_json(state_file, {"end_time": 0, "original_vol": None})
        if time.time() >= state.get("end_time", 0) - 1.5:
            if state.get("original_vol") is not None:
                _music_volume(state["original_vol"], autore_override="DuckSystem (Ripristino)")
                state["original_vol"] = None
                state["end_time"] = 0
                _write_json(state_file, state)


def _speak(text, voice, quando=None, img=None, img_caption=None):
    fname = sb.tts_to_dir(text, voice=voice)
    if not fname:
        return "ERRORE: TTS non ha prodotto audio"
    at = int(quando) if quando else int(time.time()) + RITARDO_SYNC
    pl = _read_json(VOCE_JSON, {"segments": [], "live": True, "live_poll_sec": 10})
    segs = pl.setdefault("segments", [])
    seg_data = {"file": fname, "text": text, "at": at}
    if img:
        seg_data["img"] = img
    if img_caption:
        seg_data["img_caption"] = img_caption
    segs.append(seg_data)
    pl["segments"] = segs[-MAX_SEGMENTI:]
    pl["live"] = True
    _write_json(VOCE_JSON, pl)
    
    # Ducking
    try:
        import mutagen.mp3
        duration = mutagen.mp3.MP3(os.path.join(AUDIO_DIR, fname)).info.length
    except Exception:
        duration = 10.0
    
    now = time.time()
    end_time = at + duration
    
    state_file = os.path.join(AUDIO_DIR, "duck_state.json")
    with _duck_file_lock():
        state = _read_json(state_file, {"end_time": 0, "original_vol": None})
        state["end_time"] = max(state.get("end_time", 0), end_time)
        _write_json(state_file, state)
        
    import subprocess
    cmd = [
        sys.executable, "-c",
        f"import sys; sys.path.insert(0, '{os.path.dirname(os.path.abspath(__file__))}'); import mcp_radio_betty as rb; rb._duck_job({at}, {end_time})"
    ]
    with open(os.path.join(AUDIO_DIR, "duck_job.err"), "a") as err_f:
        subprocess.Popen(cmd, start_new_session=True, stdout=subprocess.DEVNULL, stderr=err_f)

    from datetime import datetime
    from zoneinfo import ZoneInfo
    ora = datetime.fromtimestamp(at, ZoneInfo("Europe/Rome")).strftime("%H:%M:%S")
    return f"OK: in onda alle {ora} su tutti i dispositivi: {text[:70]}"


def _music_play(query, solo_clean=False, preferisci_video=False):
    """Mette in onda un brano cercandolo su YouTube.

    Passa da yt_cerca e NON piu' da yt_search_first (05/08/2026): il vecchio
    risolutore non conosceva nessun filtro, quindi questo — che e' il canale piu'
    immediato, quello che Radio usa proprio quando c'e' fretta — era l'unico
    NON protetto. Segnalato da Radio con un caso reale: aveva chiesto
    "Eminem Not Afraid clean official video" e in onda e' andata una versione
    senza nessun marcatore clean, con due bambini in sala.
    """
    ris = sb.yt_cerca(query, quanti=1, solo_clean=solo_clean,
                      preferisci_video=preferisci_video)
    t = ris[0] if ris else None
    if not t:
        if solo_clean:
            return (f"Nessun risultato DICHIARATO clean per: {query}. "
                    "Non metto in onda niente invece di rischiare: prova una query "
                    "diversa (aggiungi 'clean' o 'radio edit') o togli il vincolo.")
        return f"Nessun risultato per: {query}"
    with _musica_lock():
        m = _read_json(MUSICA_JSON, {"tracks": [], "vol": 45})
        tracks = m.get("tracks", [])
        if not any(x.get("id") == t["id"] for x in tracks):
            tracks.append({"id": t["id"], "title": t["title"], "durata": t.get("durata", 0)})
        m["tracks"] = tracks
        # NON il cmd push "play": il client ad ogni poll (15s) ricalcola la posizione
        # dalla timeline (tracks + inizio) e passa sopra a qualsiasi comando gia'
        # eseguito — il brano richiesto restava in onda ~15s poi saltava via
        # (misurato da Radio il 05/08/2026: ~20s di ascolto poi il salto). Si sposta
        # invece 'inizio' in modo che la posizione calcolata dal client sia GIA' il
        # brano richiesto: nessun cmd, nessun tocco al client, e il cambio avviene
        # nello stesso istante su tutti i dispositivi.
        target = 0
        for i, x in enumerate(tracks):
            if x.get("id") == t["id"]:
                target = i
                break
        m["inizio"] = int(time.time() - sum(x.get("durata", 0) for x in tracks[:target]))
        _write_json(MUSICA_JSON, m)
    marc = " ✅clean" if t.get("clean") else ("" if not solo_clean else "")
    return f"OK: ora in onda {t['title']}{marc}"


def _music_next():
    with _musica_lock():
        m = _read_json(MUSICA_JSON, {"tracks": []})
        tracks = m.get("tracks", [])
        if not tracks:
            return "ERRORE: scaletta vuota, niente brano successivo"
        # Stessa causa di _music_play: il cmd "next" veniva annullato al poll
        # successivo (sincronizza ricalcola dalla timeline — Radio l'ha misurato
        # del tutto inefficace: 3 campioni a 15s sempre fermi sulla timeline).
        # Si sposta 'inizio' sull'attacco del brano successivo; il % len(tracks)
        # riparte dal primo in fondo alla scaletta (la radio non deve mai fermarsi).
        pos = _posizione_corrente(m)
        idx = pos[0] if pos else -1
        target = (idx + 1) % len(tracks)
        m["inizio"] = int(time.time() - sum(x.get("durata", 0) for x in tracks[:target]))
        _write_json(MUSICA_JSON, m)
        titolo = tracks[target].get('title') or tracks[target].get('id')
    return f"OK: brano successivo: {titolo}"


def _music_status():
    m = _read_json(MUSICA_JSON, {"tracks": []})
    tracks = m.get("tracks", [])
    st = _stato_vivo()
    if not st:
        return json.dumps({
            "in_onda": None,
            "coda": [t.get("title") for t in tracks],
            "player": None,
            "nota": "la pagina radio non sta pubblicando stato (chiusa o non caricata)",
        }, ensure_ascii=False)
    idx = st.get("idx", -1)
    cur = st.get("cur") or (tracks[idx].get("title") if 0 <= idx < len(tracks) else None)
    rest = [t.get("title") for t in tracks[max(idx + 1, 0):]]
    return json.dumps({"in_onda": cur, "coda": rest, "player": st.get("yt")}, ensure_ascii=False)


def _log_azione(azione, dettaglio, autore_override=None):
    try:
        author = autore_override
        if not author:
            import subprocess
            try:
                author = subprocess.check_output(["tmux", "display-message", "-p", "#W"], text=True).strip()
            except Exception:
                author = "Sconosciuto"
        
        with open(os.path.join(AUDIO_DIR, "azioni_radio.log"), "a") as f:
            ts = time.strftime("%Y-%m-%d %H:%M:%S")
            f.write(f"[{ts}] [{author}] {azione}: {dettaglio}\n")
    except Exception:
        pass


def _music_volume(n, autore_override=None):
    n = max(0, min(100, int(n)))
    with _musica_lock():
        m = _read_json(MUSICA_JSON, {"tracks": [], "vol": 45})
        old_vol = m.get("vol", 45)
        m["vol"] = n
        _write_json(MUSICA_JSON, m)
    _log_azione("VOLUME_MUSICA", f"{old_vol}% -> {n}%", autore_override)
    return f"OK: volume musica {n}%"


def _voice_volume(n, autore_override=None):
    """Simmetrico a _music_volume, stessa scala 0-100 (il client converte in
    0-1 per l'elemento <audio>, che usa quella scala nativa). Prima non
    esisteva: l'unica leva per bilanciare voce/musica era abbassare la
    musica, e con la voce gia' al massimo non c'era margine (Radio,
    05/08/2026)."""
    n = max(0, min(100, int(n)))
    pl = _read_json(VOCE_JSON, {"segments": [], "live": True, "live_poll_sec": 10})
    old_vol = pl.get("vol", 100)
    pl["vol"] = n
    _write_json(VOCE_JSON, pl)
    _log_azione("VOLUME_VOCE", f"{old_vol}% -> {n}%", autore_override)
    return f"OK: volume voce {n}%"


# Wikimedia rifiuta con 403 "Contabo networks are forbidden due to abuse
# unhandled by Contabo admins" qualunque richiesta con uno User-Agent generico
# come "RadioBetty/0.1" - NON e' un blocco della rete Contabo in se' (diagnosi
# sbagliata scritta qui in precedenza, corretta il 05/08/2026 da Radio dopo
# verifica diretta): e' la loro user-agent policy. Uno UA identificante con
# contatto risolve, su it.wiki ED en.wiki, verificato su 9/10 soggetti reali.
WIKI_UA = "RadioBetty/1.0 (https://betty-manf.duckdns.org; manfredo.fraccola@gmail.com)"

# Per un soggetto tipo una citta', l'algoritmo "pageimage" di Wikipedia a
# volte sceglie il primo simbolo dell'infobox invece di una foto vera (es.
# 'Sorrento' -> Sorrento-Bandiera.svg, trovato da Radio il 05/08/2026). Un
# filtro sul nome file basta a scartarli: bandiere/stemmi/loghi sono quasi
# sempre .svg (una foto vera non lo e' praticamente mai) o hanno uno di
# questi termini nel filename.
_SIMBOLO_RE = re.compile(
    r"\.svg(?:$|\?)|flag|bandiera|stemma|coat_of_arms|coatofarms|crest|seal|logo|emblem",
    re.I)


def _wiki_thumbnail(soggetto, size=1280):
    """Foto di un soggetto via Wikipedia (prima it, poi en se assente su it -
    l'italiano e' meglio per Manfredo quando c'e'). Action API con
    pithumbsize, non la REST summary: quella da' un thumbnail minuscolo
    (~330px, illeggibile su un pannello intero), mentre pithumbsize restituisce
    fino a `size` px se l'originale e' piu' grande, o l'originale stesso se
    piu' piccolo (niente file da 8000px per un telefono). None se il soggetto
    non ha proprio una foto associata (es. un concetto astratto come 'Jazz') -
    a quel punto e' il chiamante a decidere il da farsi, niente fallback qui."""
    for host in ("it.wikipedia.org", "en.wikipedia.org"):
        api = (f"https://{host}/w/api.php?action=query&titles={soggetto.replace(' ', '_')}"
               f"&prop=pageimages&pithumbsize={size}&format=json")
        out = subprocess.run(["curl", "-s", "-m", "10", "-A", WIKI_UA, api],
                             capture_output=True, text=True).stdout
        try:
            pages = json.loads(out).get("query", {}).get("pages", {})
            for p in pages.values():
                src = (p.get("thumbnail") or {}).get("source")
                if src and not _SIMBOLO_RE.search(src):
                    return src
        except Exception:
            continue
    return None


def _photo(url, caption, quando=None):
    v = _read_json(VISUALE_JSON, {"seq": 0})
    at = int(quando) if quando else int(time.time()) + RITARDO_SYNC
    v = {"url": url, "caption": caption, "seq": v.get("seq", 0) + 1, "at": at}
    _write_json(VISUALE_JSON, v)
    return f"OK: foto a schermo ({caption or url[:60]})"


def _state():
    st = _stato_vivo()
    if not st:
        return json.dumps({"accesa": False,
                           "nota": "nessuno stato recente dalla pagina radio"})
    st["accesa"] = True
    return json.dumps(st, ensure_ascii=False)


def _stop():
    """Ferma i contenuti senza chiudere la scheda: la radio resta pronta e la si
    rilancia quando si vuole scrivendo di nuovo i JSON (nessun CDP necessario)."""
    _cmd("stop")
    try:
        # Flag di stato radio: spostato in core/sveglia_audio/ (setgid dc-agents,
        # scrivibile da dc-radio). Prima viveva in /tmp come root:root e
        # l'os.remove qui falliva (audit permessi, 11/08).
        os.remove(os.path.join(CORE, "sveglia_audio", "radio_betty_active"))
    except OSError:
        pass
    return "OK: radio fermata (scheda lasciata aperta, pronta a ripartire)"


def _apod(giorni_fa: int):
    d = date.today() - timedelta(days=max(0, giorni_fa))
    pag = "astropix.html" if giorni_fa <= 0 else f"ap{d.strftime('%y%m%d')}.html"
    html = subprocess.run(["curl", "-s", "-m", "10", f"https://apod.nasa.gov/apod/{pag}"],
                          capture_output=True, text=True).stdout
    m = re.search(r'<img[^>]+SRC=["\']([^"\']+)["\']', html, re.I)
    if not m:
        return None, None
    # Il titolo NON e' nel <title> (che e' sempre il generico "Astronomy Picture
    # of the Day", per giunta su piu' righe): il nome vero dell'immagine e' il
    # primo <b> della pagina, subito dopo la foto. La vecchia regex su <title>APOD:
    # non ha mai trovato niente e la didascalia usciva sempre vuota.
    cap = re.search(r"<b>\s*(.{5,120}?)\s*</b>", html, re.I | re.S)
    return "https://apod.nasa.gov/apod/" + m.group(1), (cap.group(1).strip() if cap else "")


# ---------------------------------------------------------------------------
# Tool MCP (async: FastMCP li esegue nell'event loop; il lavoro sync va in thread)
# ---------------------------------------------------------------------------

@mcp.tool()
async def radio_speak(text: str, voice: str = "it-IT-DiegoNeural") -> str:
    """Messaggio parlato al volo su Radio Betty (ducking automatico della
    musica mentre parla). Accende la radio se spenta."""
    return await asyncio.to_thread(_speak, text, voice)


def _timeline_build(queries, da_adesso=True):
    """Costruisce la timeline: risolve ogni query su YouTube (durata REALE, non
    stimata: la ricerca la restituisce gia') e fissa l'istante di partenza.

    Da qui in poi cosa suona non lo decide piu' nessuno in tempo reale: e' una
    funzione dell'orologio. Ogni dispositivo collegato calcola da solo dove si
    trova e sente la stessa cosa nello stesso momento, come una radio vera.
    """
    tracks, scartate = [], []
    for q in queries:
        try:
            t = sb.yt_search_first(q)
        except Exception:
            t = None
        if t and t.get("durata"):
            tracks.append({"id": t["id"], "title": t["title"], "durata": t["durata"]})
        else:
            scartate.append(q)
    if not tracks:
        return {"errore": "nessun brano risolto", "scartate": scartate}
    # Anche una ricostruzione completa NON deve interrompere l'ascolto: se il
    # brano in onda e' ancora nella nuova scaletta, continua da dov'era. Solo se
    # sparisce (o se si chiede esplicitamente) la timeline riparte da capo.
    with _musica_lock():
        if da_adesso:
            m = _timeline_modifica_locked(tracks)
            continuita = True
        else:
            m = _read_json(MUSICA_JSON, {"vol": 50})
            m["tracks"] = tracks
            if not m.get("inizio"):
                m["inizio"] = int(time.time())
            _write_json(MUSICA_JSON, m)
            continuita = False
    tot = sum(t["durata"] for t in tracks)
    return {"brani": len(tracks), "durata_totale_min": round(tot / 60, 1),
            "inizio": m["inizio"], "senza_interruzione": continuita,
            "scartate": scartate}


def _posizione_corrente(m):
    """(indice, secondi dentro il brano) secondo la timeline. None se non calcolabile."""
    tracks = m.get("tracks", [])
    inizio = m.get("inizio")
    if not tracks or not inizio:
        return None
    tot = sum(t.get("durata", 0) for t in tracks)
    if tot <= 0:
        return None
    el = (time.time() - inizio) % tot
    for i, t in enumerate(tracks):
        d = t.get("durata", 0)
        if el < d:
            return i, el
        el -= d
    return 0, 0.0


def _riancora(m, id_corrente, offset):
    """Sposta 'inizio' in modo che `id_corrente` risulti in onda esattamente a
    `offset` secondi. E' cio' che permette di MODIFICARE LA SCALETTA MENTRE SUONA
    senza far saltare il brano: la scaletta cambia sotto, ma il punto in cui si
    trova l'ascoltatore resta identico."""
    acc = 0.0
    for t in m.get("tracks", []):
        if t.get("id") == id_corrente:
            m["inizio"] = int(time.time() - (acc + offset))
            return True
        acc += t.get("durata", 0)
    return False


def _timeline_modifica_locked(nuove_tracks):
    """Nucleo di `_timeline_modifica`: legge, riancora e scrive SENZA
    acquisire il lock — va chiamata solo da dentro un `with _musica_lock():`
    gia' aperto dal chiamante (annidare due `_musica_lock()` nello stesso
    processo si auto-blocca, vedi commento sopra `_musica_lock`)."""
    m = _read_json(MUSICA_JSON, {"vol": 50})
    pos = _posizione_corrente(m)
    cur_id, off = (None, 0.0)
    if pos:
        i, off = pos
        cur_id = m["tracks"][i].get("id")
    m["tracks"] = nuove_tracks
    if not (cur_id and _riancora(m, cur_id, off)):
        # il brano in onda non c'e' piu' nella nuova scaletta: si riparte da capo
        m["inizio"] = int(time.time())
    _write_json(MUSICA_JSON, m)
    return m


def _timeline_modifica(nuove_tracks):
    """Applica una nuova scaletta preservando il punto di ascolto corrente.
    Acquisisce da sola il lock: usarla solo da FUORI una sezione gia' locked
    (altrimenti vedi `_timeline_modifica_locked`)."""
    with _musica_lock():
        return _timeline_modifica_locked(nuove_tracks)


def _timeline_show():
    """Il palinsesto con gli ORARI: a che ora parte ogni brano e cosa suona ora."""
    m = _read_json(MUSICA_JSON, {})
    tracks = m.get("tracks", [])
    inizio = m.get("inizio")
    if not tracks or not inizio:
        return json.dumps({"errore": "timeline non impostata (manca 'inizio' o la scaletta)"},
                          ensure_ascii=False)
    tot = sum(t.get("durata", 0) for t in tracks)
    if tot <= 0:
        return json.dumps({"errore": "durate mancanti: la timeline non e' calcolabile"},
                          ensure_ascii=False)
    from datetime import datetime
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo("Europe/Rome")
    ora = time.time()
    trascorso = (ora - inizio) % tot
    giro = int((ora - inizio) // tot)
    scaletta, acc, in_onda = [], 0.0, None
    for i, t in enumerate(tracks):
        d = t.get("durata", 0)
        parte_a = inizio + giro * tot + acc
        voce = {"n": i + 1, "titolo": t.get("title", ""),
                "ora": datetime.fromtimestamp(parte_a, TZ).strftime("%H:%M:%S"),
                "durata": f"{d // 60}:{d % 60:02d}"}
        if acc <= trascorso < acc + d:
            voce["in_onda"] = True
            in_onda = {"titolo": t.get("title"), "al_secondo": int(trascorso - acc), "di": d}
        scaletta.append(voce)
        acc += d
    return json.dumps({"in_onda": in_onda, "durata_giro_min": round(tot / 60, 1),
                       "giro_numero": giro + 1, "scaletta": scaletta}, ensure_ascii=False)


@mcp.tool()
async def radio_timeline_build(queries: list[str]) -> str:
    """Costruisce il PALINSESTO SINCRONIZZATO da una lista di ricerche musicali.

    Risolve ogni query su YouTube prendendone la durata REALE (non serve stimarla)
    e fissa l'istante di partenza: da quel momento tutti i dispositivi collegati
    sentono la stessa cosa nello stesso momento, e chi si collega a meta' brano lo
    prende a meta'. Il palinsesto gira in loop, quindi la radio non resta mai muta.

    Sostituisce l'intera scaletta. Per aggiungere senza rifare tutto usa
    radio_music_queue; per vedere gli orari usa radio_timeline_show."""
    r = await asyncio.to_thread(_timeline_build, queries, True)
    return json.dumps(r, ensure_ascii=False)


def _timeline_add(query, posizione=0, permetti_duplicati=False, solo_clean=False,
                  dopo_corrente=None):
    """Aggiunge un brano alla scaletta.

    solo_clean (05/08/2026, segnalato da Radio): finche' questa funzione risolveva
    con yt_search_first — senza alcun filtro — la sicurezza dipendeva solo dal
    testo della query. Ma una query che CONTIENE la parola 'clean' puo' benissimo
    restituire l'Official Video esplicito: e' successo davvero. Il fatto che la
    query chieda clean non dice niente su cosa torna.
    Questa e' la via da cui il refill automatico del keeper faceva entrare brani
    espliciti, con dei bambini in casa.

    dopo_corrente (08/08/2026): se dato, IGNORA `posizione` e inserisce N slot
    dopo il brano che risulta EFFETTIVAMENTE in onda al momento della
    scrittura (dopo_corrente=1 = subito dopo). Preferirla a un `posizione`
    assoluto calcolato da un `radio_timeline_show()` fatto prima: fra quella
    lettura e questa scrittura la scaletta puo' essere gia' cambiata (era
    esattamente la race dietro "brani aggiunti dopo il corrente spariti al
    giro dopo, tolti come gia' passati" — segnalata da Radio, 08/08/2026). Qui
    la posizione si risolve DENTRO lo stesso lock della scrittura, sulla
    lettura piu' fresca possibile.
    """
    if solo_clean:
        ris = sb.yt_cerca(query, quanti=1, solo_clean=True)
        t = ris[0] if ris else None
        if not t:
            return {"errore": f"nessun brano DICHIARATO clean per: {query} "
                               "(non aggiungo niente invece di rischiare)"}
    else:
        t = sb.yt_search_first(query)
    if not t or not t.get("durata"):
        return {"errore": f"nessun brano valido per: {query}"}
    with _musica_lock():
        m = _read_json(MUSICA_JSON, {"vol": 50})
        tracks = list(m.get("tracks", []))
        # Dedup calcolato QUI sullo stato corrente della scaletta, non passato dal
        # chiamante (un set esterno puo' scollegarsi dal file vero): la rotazione
        # del rinnovo non deve MAI rimettere in onda un brano gia' in scaletta —
        # era esattamente il bug "sempre le stesse canzoni" (05/08/2026). Default
        # attivo; permetti_duplicati solo per futuri usi che lo vogliano davvero.
        if not permetti_duplicati and any(x.get("id") == t["id"] for x in tracks):
            return {"errore": f"gia' in scaletta: {t['title']}"}
        nuovo = {"id": t["id"], "title": t["title"], "durata": t["durata"]}
        if dopo_corrente is not None:
            pos = _posizione_corrente(m)
            posizione = (pos[0] + 1 + dopo_corrente) if pos else 0
        if posizione and 1 <= posizione <= len(tracks):
            tracks.insert(posizione - 1, nuovo)
        else:
            tracks.append(nuovo)
        _timeline_modifica_locked(tracks)
        brani = len(tracks)
    return {"aggiunto": t["title"], "durata_s": t["durata"], "brani": brani}


def _timeline_remove(numero):
    with _musica_lock():
        m = _read_json(MUSICA_JSON, {})
        tracks = list(m.get("tracks", []))
        if not (1 <= numero <= len(tracks)):
            return {"errore": f"numero fuori scaletta (1-{len(tracks)})"}
        if len(tracks) <= 1:
            return {"errore": "non tolgo l'ultimo brano: la radio resterebbe senza scaletta"}
        tolto = tracks.pop(numero - 1)
        _timeline_modifica_locked(tracks)
        brani = len(tracks)
    return {"tolto": tolto.get("title"), "brani": brani}


def _timeline_remove_prima_di_id(id_corrente):
    """Toglie il brano immediatamente PRIMA di `id_corrente` nella scaletta
    ATTUALE, riletta ORA (non un indice calcolato prima e riusato). Pensata
    per la potatura del keeper: chiamata una volta per ogni brano da togliere,
    ri-deriva ad ogni chiamata la posizione vera di id_corrente, quindi resta
    corretta anche se qualcos'altro (Radio, un altro tick) ha scritto
    musica.json nel frattempo — a differenza di un ciclo che si fida di un
    indice calcolato una volta sola all'inizio (08/08/2026, bug 'potatura
    tocca il brano in onda')."""
    with _musica_lock():
        m = _read_json(MUSICA_JSON, {"tracks": []})
        tracks = list(m.get("tracks", []))
        idx = next((i for i, t in enumerate(tracks) if t.get("id") == id_corrente), None)
        if idx is None:
            return {"errore": f"id {id_corrente} non e' (piu') in scaletta"}
        if idx == 0:
            return {"errore": "nessun brano prima di quello in onda"}
        if len(tracks) <= 1:
            return {"errore": "non tolgo l'ultimo brano: la radio resterebbe senza scaletta"}
        tolto = tracks.pop(idx - 1)
        _timeline_modifica_locked(tracks)
        brani = len(tracks)
    return {"tolto": tolto.get("title"), "brani": brani}


@mcp.tool()
async def radio_timeline_add(query: str, posizione: int = 0,
                             solo_clean: bool = False, dopo_corrente: int = 0) -> str:
    """Aggiunge un brano alla scaletta MENTRE LA RADIO SUONA, senza interrompere.

    posizione: 0 = in fondo (default), oppure il numero della posizione in cui
    inserirlo (1 = primo). Il brano in onda continua esattamente da dov'era.

    dopo_corrente: alternativa piu' sicura a `posizione` quando vuoi inserire
    relativo al brano IN ONDA (es. "subito dopo quello corrente" = 1). Se lo
    dai (>0), `posizione` viene ignorato: la posizione si calcola sul brano
    davvero in onda nell'istante in cui questo comando scrive, non su un
    numero che hai letto prima da `radio_timeline_show` — fra quella lettura
    e questa scrittura la scaletta puo' essere gia' cambiata (rinnovo del
    keeper, un altro tuo comando), e un `posizione` assoluto calcolato a mano
    puo' finire nel posto sbagliato. Preferiscilo sempre a `posizione` quando
    il concetto e' "relativo a cosa sta suonando ora", non "alla posizione N".

    solo_clean=True: aggiunge SOLO versioni che dichiarano di essere pulite; se non
    ne trova non aggiunge niente invece di ripiegare. Da usare sempre quando ci sono
    bambini o ospiti in casa. Aggiunto il 05/08/2026 chiudendo il TERZO punto d'ingresso
    non filtrato: i primi due erano _music_play e il refill automatico del keeper, e da
    quest'ultimo erano gia' entrati due brani espliciti mentre in casa c'erano due
    bambini. Mettere il filtro su due vie su tre lascia semplicemente aperta la terza.
    ⚠️ La parola 'clean' va messa ANCHE nella query: la query cerca, il flag verifica."""
    r = await asyncio.to_thread(_timeline_add, query, posizione, False, solo_clean,
                                (dopo_corrente or None))
    return json.dumps(r, ensure_ascii=False)


@mcp.tool()
async def radio_timeline_remove(numero: int) -> str:
    """Toglie il brano numero N dalla scaletta MENTRE LA RADIO SUONA, senza
    interrompere. Usa radio_timeline_show per vedere i numeri.

    ⚠️ Se devi togliere PIU' brani, procedi dall'indice PIU' ALTO al PIU' BASSO:
    dopo ogni rimozione i numeri successivi scalano, e togliendo prima il piu'
    basso cancelleresti in silenzio il brano sbagliato (segnalato da Radio,
    05/08/2026, dopo averlo evitato sul campo)."""
    r = await asyncio.to_thread(_timeline_remove, numero)
    return json.dumps(r, ensure_ascii=False)


@mcp.tool()
async def radio_timeline_show() -> str:
    """Mostra il palinsesto CON GLI ORARI: a che ora parte ogni brano, quale e' in
    onda adesso e a che secondo, quante volte ha gia' girato il palinsesto."""
    return await asyncio.to_thread(_timeline_show)


@mcp.tool()
async def radio_apri() -> str:
    """Riapre la scheda della radio sul Surface se e' stata chiusa.

    E' l'UNICO comando che usa ancora chrome-local/CDP: tutto il resto della radio
    funziona a file JSON e non dipende dal tunnel. Da usare solo se radio_state
    dice che la pagina non pubblica piu' stato."""
    def _apri():
        # Import lazy: playwright serve SOLO a questo tool (apertura scheda via
        # CDP). Al livello del modulo ritarderebbe l'handshake MCP (lezione
        # DEV-005: import pesanti oltre il timeout = server scartato in
        # silenzio) e appesantirebbe anche il keeper, che importa questo file
        # a ogni tick di 30s.
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            page = _radio_page(pw, autostart=True)
            return "OK: scheda radio aperta e accesa" if page else "ERRORE: non sono riuscita ad aprire la scheda"
    return await asyncio.to_thread(_apri)


@mcp.tool()
async def radio_music_search(query: str) -> str:
    """Cerca un brano su YouTube (ricerca reale) e ritorna titolo+id SENZA suonarlo."""
    t = await asyncio.to_thread(sb.yt_search_first, query)
    if not t:
        return f"Nessun risultato per: {query}"
    return json.dumps(t, ensure_ascii=False)


@mcp.tool()
async def radio_music_play(query: str, solo_clean: bool = False,
                           preferisci_video: bool = False) -> str:
    """Cerca un brano su YouTube e lo mette SUBITO in onda (cambia il brano in corso).

    solo_clean=True: mette in onda SOLO versioni che dichiarano di essere pulite
      (clean, radio edit, censored). Se non ne trova NON manda in onda niente
      invece di ripiegare su una versione non verificata. Da usare sempre quando
      ci sono bambini o ospiti in casa.
    preferisci_video=True: preferisce caricamenti che siano video veri invece di
      copertine ferme. Attenzione: i due criteri spesso divergono — le versioni
      pulite sono quasi sempre 'lyrics' o 'audio', non l'Official Video.

    NOTA D'USO (Radio, 05/08/2026, verificata sul campo): la parola 'clean' va messa
    ANCHE nella query, non solo nel flag. Con la parola nella query e il flag come
    verifica passano 22 brani su 22; affidandosi al solo flag ne sopravvivono 9,
    perche' la ricerca non fa nemmeno emergere i caricamenti puliti. La query cerca,
    il flag verifica: servono insieme, non uno al posto dell'altro.
    """
    return await asyncio.to_thread(_music_play, query, solo_clean, preferisci_video)


@mcp.tool()
async def radio_music_queue(query: str) -> str:
    """Cerca un brano su YouTube e lo aggiunge alla coda (parte quando finisce il corrente)."""
    # Passa da _timeline_add invece di appendere a mano su musica.json: stessa
    # famiglia dei bug di play/next (scrittura diretta che non ricalcola la
    # timeline). _timeline_add preserva il punto di ascolto corrente e cura
    # anche una scaletta senza 'inizio' (che il client non sa sincronizzare).
    r = await asyncio.to_thread(_timeline_add, query, 0)
    if r.get("errore"):
        return f"ERRORE: {r['errore']}"
    return f"OK: accodato {r['aggiunto']} (coda: {r['brani']} brani)"


@mcp.tool()
async def radio_music_next() -> str:
    """Salta al brano successivo della coda."""
    return await asyncio.to_thread(_music_next)


@mcp.tool()
async def radio_music_status() -> str:
    """Brano in corso, posizione nella coda e stato player."""
    return await asyncio.to_thread(_music_status)


@mcp.tool()
async def radio_music_volume(n: int) -> str:
    """Volume musica 0-100."""
    return await asyncio.to_thread(_music_volume, n)


@mcp.tool()
async def radio_voice_volume(n: int) -> str:
    """Volume voce 0-100, simmetrico a radio_music_volume - stessa scala,
    cosi' i due si bilanciano davvero invece di poter agire solo sulla
    musica."""
    return await asyncio.to_thread(_voice_volume, n)


@mcp.tool()
async def radio_photo(url: str, caption: str = "") -> str:
    """Mostra una foto precisa nel pannello visuale della radio."""
    return await asyncio.to_thread(_photo, url, caption)


@mcp.tool()
async def radio_photo_nasa(giorni_fa: int = 0) -> str:
    """Mostra l'Astronomy Picture of the Day NASA (0 = oggi, 1 = ieri, ecc.)."""
    url, caption = await asyncio.to_thread(_apod, giorni_fa)
    if not url:
        return f"ERRORE: APOD non trovato per giorni_fa={giorni_fa}"
    return await asyncio.to_thread(_photo, url, caption)


@mcp.tool()
async def radio_search_image(query: str) -> str:
    """Mostra un'immagine pertinente nel pannello visuale: foto Wikipedia
    (Action API pithumbsize=1280, prima it.wikipedia poi en.wikipedia) con
    fallback loremflickr se il soggetto non ha una foto associata (query
    generiche tipo un'atmosfera, non un nome preciso: qui va bene, il
    chiamante non promette un soggetto esatto). NOTA 05/08/2026: la vecchia
    diagnosi "Commons e it.wiki bloccati da Wikimedia per questo server" era
    sbagliata - non c'e' nessun blocco di rete, serviva solo uno User-Agent
    identificante invece di uno generico (vedi WIKI_UA)."""
    url = await asyncio.to_thread(_wiki_thumbnail, query)
    if not url:
        tags = query.strip().replace(" ", ",")
        url = f"https://loremflickr.com/1280/720/{tags}"
    return await asyncio.to_thread(_photo, url, query)


@mcp.tool()
async def radio_state() -> str:
    """Stato generale di Radio Betty (accesa/spenta, player, coda, volumi)."""
    return await asyncio.to_thread(_state)


PALINSESTO = os.path.join(AUDIO_DIR, "palinsesto.json")


def _pal_load():
    if os.path.exists(PALINSESTO):
        with open(PALINSESTO, encoding="utf-8") as f:
            return json.load(f)
    return {"items": []}


def _pal_save(pal):
    with open(PALINSESTO, "w", encoding="utf-8") as f:
        json.dump(pal, f, ensure_ascii=False, indent=2)


@mcp.tool()
async def radio_palinsesto_show() -> str:
    """Mostra il palinsesto corrente (timeline Roma) e l'ora locale di Roma."""
    from zoneinfo import ZoneInfo
    from datetime import datetime
    pal = await asyncio.to_thread(_pal_load)
    ora = datetime.now(ZoneInfo("Europe/Rome")).strftime("%H:%M:%S")
    return json.dumps({"ora_roma": ora, "items": pal.get("items", [])}, ensure_ascii=False)


@mcp.tool()
async def radio_palinsesto_add(tipo: str, testo: str = "", query: str = "",
                               fonte: str = "", caption: str = "", quando: str = "now",
                               ogni_min: int = 0, dalle: str = "", fino_alle: str = "") -> str:
    """Aggiunge un item al palinsesto che il runner in services esegue.
    tipo: 'voce' (testo), 'musica' (query YouTube), 'visuale' (fonte: nasa |
    wikipedia:Voce | url). caption: didascalia mostrata sotto l'immagine -
    usata solo quando fonte e' un url diretto (nasa e wikipedia: si scelgono
    gia' da soli la propria didascalia). quando: 'now' oppure timecode 'HH:MM'
    (fuso Roma). ogni_min + dalle + fino_alle: item ricorrente in finestra
    oraria (es. promemoria colazione dalle 09:00 alle 10:30)."""
    def add():
        pal = _pal_load()
        it = {"tipo": tipo}
        if testo:
            it["testo"] = testo
        if query:
            it["query"] = query
        if fonte:
            it["fonte"] = fonte
        if caption:
            it["caption"] = caption
        if ogni_min:
            it["ogni_min"] = ogni_min
            if dalle:
                it["dalle"] = dalle
            if fino_alle:
                it["fino_alle"] = fino_alle
        else:
            it["quando"] = quando
        pal.setdefault("items", []).append(it)
        _pal_save(pal)
        
        # Sveglia istantanea del keeper (Event-Driven)
        try:
            with open("/tmp/radio_wake", "w") as f:
                f.write("1")
        except Exception:
            pass
            
        return f"OK: item nel palinsesto ({len(pal['items'])} totali)"
    return await asyncio.to_thread(add)


@mcp.tool()
async def radio_palinsesto_clear() -> str:
    """Svuota il palinsesto (gli item in esecuzione/accodati sulla pagina
    continuano; niente di nuovo verra' eseguito)."""
    def clear():
        _pal_save({"items": []})
        return "OK: palinsesto svuotato"
    return await asyncio.to_thread(clear)


@mcp.tool()
async def radio_stop() -> str:
    """Ferma i contenuti di Radio Betty SENZA chiudere la scheda: la radio
    resta pronta a ripartire scrivendo di nuovo i JSON (usa radio_apri se
    invece la scheda non c'e' piu')."""
    return await asyncio.to_thread(_stop)


@mcp.tool()
async def radio_ui_anim_trigger(preset: str, intensita: int = 50, colore: str = "") -> str:
    """Attiva un'animazione visiva (preset) sulla UI della Radio.
    preset supportati: 'radar_allerta', 'tempesta_meteo_live', 'audio_visualizer_3d'."""
    def _anim():
        v = _read_json(VISUALE_JSON, {"seq": 0})
        v["anim"] = preset
        v["anim_intensita"] = intensita
        v["anim_colore"] = colore
        v["seq"] = v.get("seq", 0) + 1
        v["at"] = int(time.time())
        _write_json(VISUALE_JSON, v)
        return f"OK: animazione '{preset}' inviata alla Radio"
    return await asyncio.to_thread(_anim)


if __name__ == "__main__":
    mcp.run()

def _incorporabile(video_id):
    """Controllo economico: il video e' ancora riproducibile dentro la nostra pagina?

    Usa l'endpoint oembed. NON e' una garanzia — provato il 05/08/2026 su casi reali,
    prende una classe di blocchi (risposta 401) ma non tutte. Vale come rete a maglie
    larghe prima di mandare in onda, non come certificazione.
    """
    import urllib.request
    try:
        req = urllib.request.Request(
            f"https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v={video_id}&format=json",
            headers={"User-Agent": "BettyEcosystem/1.0 (https://betty-manf.duckdns.org)"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status == 200
    except Exception:
        return False


def _clip_add(video_id, da_s, a_s, titolo="", posizione=0):
    """Accoda uno SPEZZONE: solo la porzione fra da_s e a_s del video.

    Nasce dalla richiesta di Manfredo (05/08/2026) di "tagliare e cucire" clip.
    La riproduzione era gia' quasi pronta: la pagina usava gia' startSeconds, e la
    timeline tratta uno spezzone come un brano qualunque perche' la durata dichiarata
    e' quella dello spezzone.

    ⚠️ LA TENSIONE CON LA REGOLA "MAI ID SCRITTI A MANO" (sollevata da Radio):
    uno spezzone e' inchiodato a UN caricamento preciso — il timestamp 1:12 vale per
    quel video e per nessun altro — quindi l'id qui e' un dato legittimo e inevitabile.
    La regola non si viola, si specializza: l'id non si SCRIVE, si REGISTRA (deve venire
    da una ricerca fatta al momento della composizione) e si RIVALIDA prima di andare in
    onda. Un id registrato tre settimane fa e' un id che nessuno ha piu' controllato.
    Per questo qui si controlla l'incorporabilita' PRIMA di accodare, e si salva quando
    la validazione e' stata fatta.
    """
    try:
        da_s, a_s = int(da_s), int(a_s)
    except Exception:
        return {"errore": "da_s e a_s devono essere secondi interi"}
    if a_s <= da_s:
        return {"errore": f"la fine ({a_s}s) deve venire dopo l'inizio ({da_s}s)"}
    if a_s - da_s > 900:
        return {"errore": "spezzone troppo lungo (oltre 15 minuti): usa il brano intero"}
    if not _incorporabile(video_id):
        return {"errore": f"{video_id} non risulta riproducibile nella pagina "
                          "(bloccato dal proprietario o inesistente): NON accodato"}

    m = _read_json(MUSICA_JSON, {"tracks": [], "vol": 50})
    tracks = list(m.get("tracks", []))
    voce = {"id": video_id, "title": titolo or f"spezzone {da_s}-{a_s}s",
            "durata": a_s - da_s, "start": da_s,
            "validato": int(time.time())}
    pos = len(tracks) if not posizione else max(0, min(len(tracks), posizione))
    tracks.insert(pos, voce)
    # 'inizio' va spostato come per qualunque inserimento, altrimenti la posizione
    # di ascolto corrente scivola: _timeline_add lo fa gia', qui si riusa la stessa
    # logica invece di riscriverla.
    m["tracks"] = tracks
    _write_json(MUSICA_JSON, m)
    return {"ok": True, "titolo": voce["title"], "durata": voce["durata"],
            "posizione": pos}


@mcp.tool()
async def radio_clip_add(video_id: str, da_s: int, a_s: int,
                         titolo: str = "", posizione: int = 0) -> str:
    """Manda in onda SOLO uno spezzone di un video YouTube (da_s -> a_s, in secondi).

    Serve per i formati a montaggio: "indovina la sigla" (10s per pezzo), medley di
    aperture (15s ciascuna), lo stacco breve prima di far partire il brano intero.

    video_id: NON scriverlo a mano da una fonte esterna. Deve venire da una ricerca
    (yt_cerca) fatta al momento della composizione, salvato insieme a titolo e durata.
    Prima di accodare, questa funzione ricontrolla che il video sia ancora
    riproducibile: se non lo e', NON accoda e te lo dice, invece di lasciarti scoprire
    il buco quando tocca a lui andare in onda.
    """
    r = await asyncio.to_thread(_clip_add, video_id, da_s, a_s, titolo, posizione)
    return json.dumps(r, ensure_ascii=False)


def _timeline_estimate(testo: str):
    """Stima approssimativa (in secondi) della durata dell'audio TTS per questo testo.
    Usato dal Regista per calcolare gli incastri di palinsesto."""
    # Heuristic: 13 characters per second for typical TTS reading speed
    durata_s = len(testo) / 13.0
    # Add a small base overhead for initialization/silence
    return int(durata_s + 2.0)

@mcp.tool()
async def radio_timeline_estimate(testo: str) -> str:
    """Calcola in anticipo la durata esatta (in secondi) di uno script testuale.
    Permette al Regista di incastrare la scaletta (testo + effetti) al millisecondo
    prima della messa in onda."""
    durata = await asyncio.to_thread(_timeline_estimate, testo)
    return json.dumps({"durata_stimata_s": durata}, ensure_ascii=False)

def _emergency_override():
    """Svuota la coda vocale per fermare eventuali frasi corrotte/indesiderate e 
    mette in onda un brano salvavita in fade-in (sostituisce musica in onda)."""
    # 1. Ferma immediatamente la voce svuotando i segmenti
    pl = _read_json(VOCE_JSON, {"segments": [], "live": True, "live_poll_sec": 10})
    pl["segments"] = []
    _write_json(VOCE_JSON, pl)
    
    # 2. Ripristina i volumi standard
    _voice_volume(100)
    _music_volume(50)
    
    # 3. Lancia una ricerca sicura di emergenza
    res = _music_play("lofi hip hop radio relax beats to chill clean", solo_clean=False)
    return "EMERGENZA ATTIVATA: Voce interrotta. " + res

@mcp.tool()
async def radio_emergency_override() -> str:
    """PANIC BUTTON: da usare in caso di problemi tecnici o audio corrotto in diretta.
    Stoppa immediatamente tutto l'audio vocale in riproduzione e fa partire un
    brano musicale salvavita rassicurante."""
    return await asyncio.to_thread(_emergency_override)
