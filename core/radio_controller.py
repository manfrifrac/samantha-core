#!/usr/bin/env python3
"""Radio Controller Daemon:
- Gestione atomica di radio_state.json e radio_cmd.json.
- Rotazione a coda aperta DINAMICA: una traccia riprodotta VIENE CONSUMATA e MAI ri-accodata in loop.
- Sincronizzazione atomica con incremento di seq ad ogni cambio stato.
- Server HTTP interno per /api/next e notifiche.
"""

import os
import sys
import time
import json
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
AUDIO_DIR = os.path.join(BASE_DIR, "sveglia_audio")
STATE_PATH = os.path.join(AUDIO_DIR, "radio_state.json")
CMD_PATH = os.path.join(AUDIO_DIR, "radio_cmd.json")
LOG_PATH = os.path.join(AUDIO_DIR, "radio_controller.log")

DEFAULT_FALLBACK = {
    "tipo": "video",
    "src": "video_bento/soul_funk/01_sir_duke.mp4",
    "titolo": "Sir Duke",
    "artista": "Stevie Wonder",
    "durata": 238,
    "seek": 0
}

# Pool locale di riserva: Soul & Funk Classics
FALLBACK_CODA = [
    {"tipo": "video", "src": "video_bento/soul_funk/01_sir_duke.mp4", "titolo": "Sir Duke", "artista": "Stevie Wonder", "durata": 238, "seek": 0},
    {"tipo": "video", "src": "video_bento/soul_funk/02_isnt_she_lovely.mp4", "titolo": "Isn't She Lovely", "artista": "Stevie Wonder", "durata": 199, "seek": 0},
    {"tipo": "video", "src": "video_bento/soul_funk/03_signed_sealed_delivered.mp4", "titolo": "Signed Sealed Delivered", "artista": "Stevie Wonder", "durata": 161, "seek": 0},
    {"tipo": "video", "src": "video_bento/soul_funk/04_lets_get_it_on.mp4", "titolo": "Let's Get It On", "artista": "Marvin Gaye", "durata": 294, "seek": 0},
    {"tipo": "video", "src": "video_bento/soul_funk/05_whats_going_on.mp4", "titolo": "What's Going On", "artista": "Marvin Gaye", "durata": 232, "seek": 0},
    {"tipo": "video", "src": "video_bento/soul_funk/06_aint_no_mountain.mp4", "titolo": "Ain't No Mountain High Enough", "artista": "Marvin Gaye & Tammi Terrell", "durata": 163, "seek": 0},
    {"tipo": "video", "src": "video_bento/soul_funk/07_respect.mp4", "titolo": "Respect", "artista": "Aretha Franklin", "durata": 150, "seek": 0},
    {"tipo": "video", "src": "video_bento/soul_funk/08_think.mp4", "titolo": "Think", "artista": "Aretha Franklin", "durata": 139, "seek": 0},
    {"tipo": "video", "src": "video_bento/soul_funk/09_i_say_a_little_prayer.mp4", "titolo": "I Say a Little Prayer", "artista": "Aretha Franklin", "durata": 218, "seek": 0},
    {"tipo": "video", "src": "video_bento/soul_funk/10_dock_of_the_bay.mp4", "titolo": "(Sittin' On) The Dock of the Bay", "artista": "Otis Redding", "durata": 162, "seek": 0},
    {"tipo": "video", "src": "video_bento/soul_funk/11_try_a_little_tenderness.mp4", "titolo": "Try a Little Tenderness", "artista": "Otis Redding", "durata": 228, "seek": 0},
    {"tipo": "video", "src": "video_bento/soul_funk/12_september.mp4", "titolo": "September", "artista": "Earth Wind & Fire", "durata": 215, "seek": 0},
    {"tipo": "video", "src": "video_bento/soul_funk/13_boogie_wonderland.mp4", "titolo": "Boogie Wonderland", "artista": "Earth Wind & Fire", "durata": 293, "seek": 0},
    {"tipo": "video", "src": "video_bento/soul_funk/14_lets_groove.mp4", "titolo": "Let's Groove", "artista": "Earth Wind & Fire", "durata": 235, "seek": 0},
    {"tipo": "video", "src": "video_bento/soul_funk/15_i_got_you_feel_good.mp4", "titolo": "I Got You (I Feel Good)", "artista": "James Brown", "durata": 166, "seek": 0},
    {"tipo": "video", "src": "video_bento/soul_funk/16_mans_world.mp4", "titolo": "It's a Man's Man's Man's World", "artista": "James Brown", "durata": 165, "seek": 0},
    {"tipo": "video", "src": "video_bento/soul_funk/17_papas_brand_new_bag.mp4", "titolo": "Papa's Got a Brand New Bag", "artista": "James Brown", "durata": 257, "seek": 0},
    {"tipo": "video", "src": "video_bento/soul_funk/18_lets_stay_together.mp4", "titolo": "Let's Stay Together", "artista": "Al Green", "durata": 200, "seek": 0},
    {"tipo": "video", "src": "video_bento/soul_funk/19_change_is_gonna_come.mp4", "titolo": "A Change Is Gonna Come", "artista": "Sam Cooke", "durata": 191, "seek": 0},
    {"tipo": "video", "src": "video_bento/soul_funk/20_hit_the_road_jack.mp4", "titolo": "Hit the Road Jack", "artista": "Ray Charles", "durata": 122, "seek": 0}
]

def riempi_coda_se_vuota():
    """Se la coda e' vuota, cerca file video mp4 pronti in cache per garantire continuita' assoluta."""
    global state
    if not state["coda"]:
        cache_dir = os.path.join(AUDIO_DIR, "video_bento", "cache")
        if os.path.exists(cache_dir):
            curr_src = state.get("attuale", {}).get("src", "")
            for fname in os.listdir(cache_dir):
                if fname.endswith(".mp4"):
                    rel_path = f"video_bento/cache/{fname}"
                    if rel_path != curr_src:
                        state["coda"].append({
                            "tipo": "video",
                            "src": rel_path,
                            "titolo": "Loved Track",
                            "artista": "Last.fm Loved",
                            "durata": 240,
                            "seek": 0
                        })
                        if len(state["coda"]) >= 2:
                            break

state = {
    "stato": "playing",
    "attuale": dict(DEFAULT_FALLBACK),
    "coda": [],
    "inframmezzo": None,
    "volume": 100,
    "started_at": time.time(),
    "ts": time.time(),
    "seq": 1
}

lock = threading.Lock()
track_started_at = time.time()
interlude_expire_at = 0.0
last_next_time = 0.0

def log(msg):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    riga = f"[{ts}] {msg}\n"
    print(riga, end="", flush=True)
    try:
        with open(LOG_PATH, "a") as f:
            f.write(riga)
    except Exception:
        pass

def salva_stato(inc_seq=False):
    global state, track_started_at
    now = time.time()
    state["ts"] = now
    state["started_at"] = track_started_at
    if state.get("attuale"):
        state["attuale"]["seek"] = max(0, int(now - track_started_at))
    if inc_seq:
        state["seq"] += 1
    tmp_path = STATE_PATH + ".tmp"
    try:
        with open(tmp_path, "w") as f:
            json.dump(state, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, STATE_PATH)
    except Exception as e:
        log(f"Errore salvataggio stato: {e}")

def prosegui_coda():
    global state, track_started_at, last_next_time
    with lock:
        now = time.time()
        if now - last_next_time < 3.0:
            log("next ignorato: rate-limit di sicurezza (meno di 3s dal precedente cambio).")
            return
        last_next_time = now

        # Stream HLS live: 'next' NON si onora.
        if state["attuale"].get("tipo") == "hls":
            log("next ignorato: stream HLS live in onda (fine gestita dal timer).")
            return
        riempi_coda_se_vuota()
        if state["coda"]:
            # Consuma la traccia dalla coda e NON riaccodarla (zero loop)
            next_item = state["coda"].pop(0)
            state["attuale"] = dict(next_item)
            track_started_at = time.time()
            log(f"Traccia successiva: {next_item.get('titolo')} ({next_item.get('artista')})")
        salva_stato(inc_seq=True)

def timer_worker():
    global track_started_at, interlude_expire_at, state
    while True:
        time.sleep(1.0)
        try:
            now = time.time()
            salva_necessario = False
            inc_seq_flag = False

            with lock:
                if state["inframmezzo"]:
                    if now >= interlude_expire_at:
                        log("Inframmezzo scaduto -> rimosso.")
                        state["inframmezzo"] = None
                        salva_necessario = True
                        inc_seq_flag = True
                    else:
                        # Congela l'avanzamento della traccia musicale durante il parlato del TG/inframmezzo
                        track_started_at += 1.0

                # Controllo integrita' file locale
                if state.get("attuale") and state["attuale"].get("tipo") in ["video", "locale"]:
                    src = state["attuale"].get("src", "")
                    if not src.startswith("http://") and not src.startswith("https://"):
                        full_path = os.path.join(AUDIO_DIR, src) if not os.path.isabs(src) else src
                        if not os.path.exists(full_path):
                            log(f"File traccia inesistente su disco ({src}) -> salto automatico alla successiva.")
                            riempi_coda_se_vuota()
                            if state["coda"]:
                                next_item = state["coda"].pop(0)
                                state["attuale"] = dict(next_item)
                                track_started_at = now
                                salva_necessario = True
                                inc_seq_flag = True

                durata = state["attuale"].get("durata", 300) if state.get("attuale") else 300
                elapsed = now - track_started_at
                if elapsed >= durata and not state.get("inframmezzo"):
                    log(f"Traccia completata ({int(elapsed)}s >= {durata}s) -> avanzo.")
                    riempi_coda_se_vuota()
                    if state["coda"]:
                        next_item = state["coda"].pop(0)
                        state["attuale"] = dict(next_item)
                        track_started_at = now
                        salva_necessario = True
                        inc_seq_flag = True

            if salva_necessario:
                salva_stato(inc_seq=inc_seq_flag)
        except Exception as e:
            log(f"Errore timer_worker: {e}")

def inserisci_inframmezzo(badge, testo, autore="Radio Betty", durata_s=12, audio_url=None):
    global state, interlude_expire_at
    with lock:
        interlude_id = f"int_{int(time.time()*1000)}"
        state["inframmezzo"] = {
            "id": interlude_id,
            "badge": badge,
            "testo": testo,
            "autore": autore,
            "durata_s": durata_s,
            "audio_url": audio_url
        }
        # Buffer extra di 20s per garantire la conclusione naturale del TTS prima del timeout server
        interlude_expire_at = time.time() + durata_s + 20
        log(f"Inframmezzo: [{badge}] {testo} ({autore}) - Durata: {durata_s}s (+20s buffer)")
        salva_stato(inc_seq=True)

def consuma_comandi():
    global track_started_at
    if not os.path.exists(CMD_PATH):
        return
    try:
        with open(CMD_PATH) as f:
            cmd = json.load(f)
        os.remove(CMD_PATH)
    except Exception as e:
        log(f"Errore lettura cmd.json: {e}")
        return

    azione = cmd.get("azione")
    if azione == "next":
        prosegui_coda()
    elif azione == "volume":
        with lock:
            state["volume"] = max(0, min(100, int(cmd.get("valore", 75))))
            log(f"Volume: {state['volume']}%")
            salva_stato()
    elif azione == "queue":
        if "item" in cmd:
            with lock:
                state["coda"].append(dict(cmd["item"]))
                log(f"Nuovo brano in coda: {cmd['item'].get('titolo')}")
                salva_stato(inc_seq=True)
    elif azione == "play_now":
        if "item" in cmd:
            with lock:
                state["attuale"] = dict(cmd["item"])
                seek = int(cmd.get("seek", cmd["item"].get("seek", 0)))
                track_started_at = time.time() - seek
                log(f"In onda subito: {cmd['item'].get('titolo')} (seek={seek}s)")
                salva_stato(inc_seq=True)
    elif azione == "set_playlist":
        if "attuale" in cmd and "coda" in cmd:
            with lock:
                curr_src = state.get("attuale", {}).get("src", "")
                new_src = cmd["attuale"].get("src", "")
                same_track = bool(curr_src and curr_src == new_src)
                
                state["attuale"] = dict(cmd["attuale"])
                state["coda"] = [dict(x) for x in cmd["coda"]]
                
                if "seek" in cmd or ("seek" in cmd["attuale"] and cmd["attuale"].get("seek", 0) > 0):
                    seek = int(cmd.get("seek", cmd["attuale"].get("seek", 0)))
                    track_started_at = time.time() - seek
                elif not same_track:
                    track_started_at = time.time()
                
                log(f"Nuova playlist impostata! In onda: {cmd['attuale'].get('titolo')} (same_track={same_track})")
                salva_stato(inc_seq=not same_track)
    elif azione == "seek":
        with lock:
            seek = int(cmd.get("seek", cmd.get("valore", 0)))
            track_started_at = time.time() - seek
            log(f"Seek applicato a {seek}s su {state.get('attuale', {}).get('titolo')}")
            salva_stato(inc_seq=True)
    elif azione == "interlude":
        inserisci_inframmezzo(
            badge=cmd.get("badge", "📢 Notifica"),
            testo=cmd.get("testo", ""),
            autore=cmd.get("autore", "Radio Betty"),
            durata_s=cmd.get("durata_s", 12),
            audio_url=cmd.get("audio_url")
        )

class RadioHTTPHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path == "/api/next":
            prosegui_coda()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status":"ok","azione":"next"}')
        else:
            self.send_response(404)
            self.end_headers()

    def do_GET(self):
        if self.path.startswith("/api/state"):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            with lock:
                self.wfile.write(json.dumps(state).encode())
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass

def start_server():
    server = HTTPServer(("127.0.0.1", 5058), RadioHTTPHandler)
    log("Server HTTP Radio Controller in ascolto su 127.0.0.1:5058")
    server.serve_forever()

if __name__ == "__main__":
    log("=== Radio Controller Daemon Avviato (Coda Aperta a Consumo) ===")
    
    # Carica stato se esistente
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH) as f:
                saved = json.load(f)
                state.update(saved)
                state["inframmezzo"] = None
        except Exception:
            pass

    now = time.time()
    durata = state.get("attuale", {}).get("durata", 240) if state.get("attuale") else 240
    if (now - state.get("started_at", 0)) > durata:
        log("Stato salvato obsoleto/scaduto -> riparto da capo a seek 0.")
        track_started_at = now
        state["started_at"] = now
        if state.get("attuale"):
            state["attuale"]["seek"] = 0
        salva_stato(inc_seq=True)
    else:
        track_started_at = state.get("started_at", now)

    threading.Thread(target=timer_worker, daemon=True).start()
    threading.Thread(target=start_server, daemon=True).start()

    while True:
        try:
            consuma_comandi()
            time.sleep(0.3)
        except Exception as e:
            log(f"Errore ciclo comandi: {e}")
            time.sleep(1.0)
