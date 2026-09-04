#!/usr/bin/env python3
"""
mcp_regista.py — MCP DEL REGISTA (mandato Manfredo 06/08 via Betty/Leo).
Un tool per funzione dell'inventario del Regista
(core/redazione/regista_inventario_funzioni_mcp.md). Il Regista definisce le
funzioni, Dario le trasforma in tool. Guardie riusate: single-writer+flock,
atomico, VIA di Enrico, blacklist embed condivisa (watchlist_embed.json),
regola una-musica (musica->musica solo crossfade esplicito).

REGOLE: handshake lazy (import mcp_radio_debug dentro le funzioni), stdout
PURO per JSON-RPC, niente credenziali nei log. I tool che compongono
ritornano show-run/scene SENZA pubblicare; la pubblicazione passa solo da
publish_seq/attiva_fallback_b con le loro guardie.
"""
import builtins
import fcntl
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from datetime import datetime

from mcp.server.fastmcp import FastMCP

_real_print = builtins.print


def _err_print(*a, **k):
    k.setdefault("file", sys.stderr)
    _real_print(*a, **k)


builtins.print = _err_print

mcp = FastMCP("Regista")

CORE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "core")
RED_DIR = os.path.join(CORE, "redazione")  # fix 06/08: era dirname(CORE) -> percorso inesistente
CONSEGNE = os.path.join(RED_DIR, "consegne")
AUDIO = os.path.join(CORE, "sveglia_audio")
BENTO_CMD = os.path.join(AUDIO, "bento_cmd.json")
BENTO_CMD_LOCK = os.path.join(AUDIO, "bento_cmd.lock")
BENTO_ENABLED = os.path.join(AUDIO, "bento_enabled")
BENTO_LIVE = os.path.join(AUDIO, "bento_live.json")
REGISTA_LOG = os.path.join(AUDIO, "regista_bento.log")
POSIZIONE_LIVE = os.path.join(CONSEGNE, "posizione_live.json")
WATCHLIST_FILE = os.path.join(AUDIO, "watchlist_embed.json")
TRACKER = os.path.join(RED_DIR, "tracker_shows.json")
SEEK_REQ = os.path.join(CONSEGNE, "richieste_seek_montatore.jsonl")
FALLBACK_B = os.path.join(CONSEGNE, "dario_arco_accanto_pilota_2100.json")

POOL_LEGGERO = {
    "Quum8qoG7v0": "Arnalds nyepi", "jjeKH5EolM8": "Jobim RSI",
    "CTGJUeDupRg": "Caetano", "-kWkpoh8tU0": "Desmond Emily",
    "U6RdpQADHxc": "Django Nuages", "Npmq79Z0FUo": "Joao VEVO",
}
TEXTURE = {"Y-D81SnXFgo": "Calvino bambini", "aczmkY6rMMI": "Rodari fiaba"}
FIRME = {
    "mattina": ("STATICA:https://images.metmuseum.org/CRDImages/ep/original/DP120415.jpg",
                "Colazione olandese, 1635 — buona mattina"),
    "pomeriggio": ("STATICA:https://images.metmuseum.org/CRDImages/dp/original/DP813769.jpg",
                   "Pausa gentile"),
    "sera": ("STATICA:https://images.metmuseum.org/CRDImages/dp/original/DP813769.jpg",
             "Whistler — la sera si accomoda"),
    "notte": ("STATICA:https://images.metmuseum.org/CRDImages/dp/original/DP813769.jpg",
              "Notturno — buonanotte"),
}


def _rd():
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import mcp_radio_debug
    return mcp_radio_debug


def _scrivi_atomico(path, dati):
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path))
    os.close(fd)
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(dati, f, ensure_ascii=False, indent=1)
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


def _lock_bento_cmd():
    lock = open(BENTO_CMD_LOCK, "a+")
    fcntl.flock(lock, fcntl.LOCK_EX)
    return lock


def _a2a(dest, msg):
    try:
        subprocess.Popen(
            [os.path.join(CORE, "venv", "bin", "python3"),
             os.path.join(CORE, "send_a2a.py"), dest, msg],
            cwd=CORE, start_new_session=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception:
        return False


def _tracker():
    try:
        return json.load(open(TRACKER, encoding="utf-8"))
    except Exception:
        return {}


def _tracker_scrivi(t):
    _scrivi_atomico(TRACKER, t)


# ---------------------------------------------------------------------------
# TOOL
# ---------------------------------------------------------------------------


@mcp.tool()
def publish_seq(file_sorgente: str, seq_num: int, via_ref: str = "") -> dict:
    """Pubblica uno show-run su bento_cmd.json (atomico+single-writer). GUARDIA
    VIA: via_ref vuoto = rifiuto, il VIA di Enrico va registrato prima."""
    if not via_ref or not via_ref.strip():
        return {"ok": False, "errore": "VIA di Enrico non registrato: rifiuto pubblicazione",
                "azione_richiesta": "registrare il VIA (param via_ref) e riprovare"}
    path = file_sorgente if os.path.isabs(file_sorgente) else os.path.join(CONSEGNE, file_sorgente)
    try:
        dati = json.load(open(path, encoding="utf-8"))
    except Exception as e:
        return {"ok": False, "errore": f"file non leggibile: {e}"}
    if dati.get("seq") != int(seq_num):
        return {"ok": False, "errore": f"seq nel file ({dati.get('seq')}) != seq_num ({seq_num})"}
    res = _rd().pubblica_seq(path)
    if res.get("ok"):
        t = _tracker()
        t[str(seq_num)] = {"pubblicato": datetime.now().isoformat(timespec="seconds"),
                           "via_ref": via_ref, "file": os.path.basename(path)}
        _tracker_scrivi(t)
    return res


@mcp.tool()
def gate_radio(programma: str, durata_s: int, note: str = "") -> dict:
    """Gate pre-onda: duck mute (vol 0) + bento_enabled true col programma e la
    durata dichiarati. Protocollo pull: la pagina/radio agganciano al poll."""
    azioni = []
    try:
        os.close(os.open(BENTO_ENABLED, os.O_CREAT | os.O_WRONLY, 0o644))
        azioni.append("bento_enabled=true")
    except Exception as e:
        return {"ok": False, "errore": f"enabled: {e}"}
    r = _rd().cue_live("duck", json.dumps({"vol_pct": 0, "attacco_ms": 1000}))
    azioni.append(f"duck cue seq={r.get('seq')}")
    t = _tracker()
    t["gate_corrente"] = {"programma": programma, "durata_s": int(durata_s),
                          "note": note, "ts": datetime.now().isoformat(timespec="seconds")}
    _tracker_scrivi(t)
    return {"ok": True, "azioni": azioni,
            "ack_radio": "canale pull: la radio aggancia al prossimo poll (~3s)"}


@mcp.tool()
def mostra_finita(programma: str, esito: str = "ok", anomalia_detail: str = "") -> dict:
    """Chiude lo show: ripristino vol 50, switch spento (persistente), esito a
    Enrico/Simone/Leo, tracker aggiornato."""
    azioni = []
    try:
        if os.path.exists(BENTO_ENABLED):
            os.remove(BENTO_ENABLED)
        azioni.append("bento_enabled rimosso")
    except Exception as e:
        azioni.append(f"enabled: {e}")
    r = _rd().cue_live("ripristino", json.dumps({"vol_pct": 50, "attacco_ms": 1000}))
    azioni.append(f"ripristino cue seq={r.get('seq')}")
    msg = (f"[A2A_FROM:regista-mcp] [A2A_TYPE:report] MOSTRA FINITA: {programma}, "
           f"esito={esito}" + (f", anomalia: {anomalia_detail}" if anomalia_detail else ""))
    for dest in ("comunicazione:AGY-ENRICO", "betty:agy-Simone", "leo_studio:AGY-LEO"):
        azioni.append(f"esito-> {dest}: {_a2a(dest, msg)}")
    t = _tracker()
    t["ultima_mostra"] = {"programma": programma, "esito": esito,
                          "anomalia": anomalia_detail,
                          "chiusa": datetime.now().isoformat(timespec="seconds")}
    t.pop("gate_corrente", None)
    _tracker_scrivi(t)
    return {"ok": True, "azioni": azioni}


@mcp.tool()
def cue_live_posizione(ts: float, label: str, ttl_s: int = 120,
                       fermo: bool = False, nota: str = "") -> dict:
    """Consuma un evento posizione e produce le scene finestra viaggio (testo
    neon + panorama PD kenburns, mute). FRESCO (ora-ts<=ttl) -> scene pronte da
    inserire nello show-run; SCADUTO -> nessuna scena, resto al base."""
    eta = time.time() - float(ts)
    if eta > float(ttl_s):
        return {"fresco": False, "eta_s": round(eta), "scene": [],
                "nota": "evento scaduto: nessuna finestra viaggio, resto al programma base"}
    scene = [
        {"media": "testo", "testo": f"Stai attraversando {label}", "stile": "neon",
         "tile": "main", "geo": {"preset": "col"}, "mossa": "cut", "in": "fade",
         "out": "cut", "show": True, "ruolo": "primo_piano", "tipo_audio": "muto",
         "dwell_s": 40, "label": f"VIAGGIO {label}"},
        {"video": "STATICA:https://images.metmuseum.org/CRDImages/dp/original/DP876190.jpg",
         "tile": "main", "geo": {"preset": "hero_centro"}, "mossa": "kenburns",
         "in": "fade", "out": "fade", "show": True, "ruolo": "firma_visiva",
         "tipo_audio": "muto", "dwell_s": 60, "label": f"PANORAMA {label}"},
    ]
    if fermo:
        scene[0]["testo"] = f"Sosta a {label}"
    return {"fresco": True, "eta_s": round(eta), "scene": scene,
            "nota": "finestre mute; audio del programma invariato; rientro al base al cue stantio" + (f"; {nota}" if nota else "")}


@mcp.tool()
def watchlist_embed(op: str, id: str = "", motivo: str = "") -> dict:
    """Watchlist/blacklist embed condivisa (VEVO/no-playing): add marca un id
    con motivo, check dice se e' escluso, list mostra tutto."""
    try:
        wl = json.load(open(WATCHLIST_FILE, encoding="utf-8"))
    except Exception:
        wl = _rd().blacklist_embed()
    if op == "add":
        if not id or not motivo:
            return {"ok": False, "errore": "add richiede id e motivo"}
        wl[id] = motivo
        _scrivi_atomico(WATCHLIST_FILE, wl)
        return {"ok": True, "watchlist": len(wl)}
    if op == "check":
        return {"id": id, "escluso": id in wl, "motivo": wl.get(id)}
    return {"watchlist": wl}


@mcp.tool()
def richiedi_seek_montatore(url_yt: str, tipo: str, durata_target_s: int,
                            vincoli: str, priorita: str = "") -> dict:
    """Chiede al Montatore un taglio pulito (chiusura pulita, niente applausi,
    niente neri): registra la richiesta e lo avvisa via A2A."""
    req = {"ts": datetime.now().isoformat(timespec="seconds"), "url_yt": url_yt,
           "tipo": tipo, "durata_target_s": int(durata_target_s), "vincoli": vincoli,
           "priorita": priorita}
    with open(SEEK_REQ, "a", encoding="utf-8") as f:
        f.write(json.dumps(req, ensure_ascii=False) + "\n")
    ok = _a2a("comunicazione:AGY-MONTATORE",
              f"[A2A_FROM:regista-mcp] [A2A_TYPE:task] SEEK richiesto: {url_yt} "
              f"({tipo}, {durata_target_s}s) vincoli: {vincoli}" + (f" priorita: {priorita}" if priorita else ""))
    return {"ok": True, "registrata": True, "a2a_montatore": ok}


@mcp.tool()
def componi_programma_per_momento(momento: str, durata_target_s: int,
                                  apertura_esclusa: str = "", firme: str = "") -> dict:
    """Compone una bozza show-run per un momento della giornata dal pool
    verificato, con regole audio Manfredo (una musica alla volta, crossfade
    esplicito tra musica->musica). NON pubblica: ritorna il JSON."""
    pool = [v for v in POOL_LEGGERO if v != apertura_esclusa and v in
            ("Quum8qoG7v0", "jjeKH5EolM8", "CTGJUeDupRg", "-kWkpoh8tU0", "U6RdpQADHxc")]
    if len(pool) < 3:
        return {"ok": False, "errore": "pool insufficiente dopo esclusione"}
    tot = int(durata_target_s)
    scene = []
    # apertura musica (finestra ~1/3)
    d1 = min(180, max(120, tot // 3))
    scene.append({"video": pool[0], "seek": 0, "tile": "main", "geo": {"preset": "hero_centro"},
                  "mossa": "kenburns", "in": "fade", "out": "fade", "show": True,
                  "ruolo": "primo_piano", "volume": 80, "dwell_s": d1,
                  "label": f"APERTURA {POOL_LEGGERO[pool[0]]}", "tipo_audio": "musica"})
    # texture voce al centro (60-90s)
    tk = list(TEXTURE)[0] if momento in ("mattina", "pomeriggio") else list(TEXTURE)[1]
    scene.append({"video": tk, "seek": 0, "tile": "main", "geo": {"preset": "col"},
                  "mossa": "cut", "in": "cut", "out": "cut", "show": True,
                  "ruolo": "primo_piano", "volume": 70, "dwell_s": 75,
                  "label": f"TEXTURE {TEXTURE[tk]}", "tipo_audio": "parlato"})
    # centro musica (crossfade in/out espliciti)
    d2 = max(120, tot - d1 - 75 - 60)
    scene.append({"video": pool[1], "seek": 0, "tile": "main", "geo": {"preset": "hero_centro"},
                  "mossa": "kenburns", "in": "fade", "out": "fade", "show": True,
                  "ruolo": "primo_piano", "volume": 80, "dwell_s": d2,
                  "label": f"CENTRO {POOL_LEGGERO[pool[1]]}", "tipo_audio": "musica"})
    # congedo musica + firma
    d3 = max(90, tot - d1 - 75 - d2 - 50)
    scene.append({"video": pool[2], "seek": 0, "tile": "main", "geo": {"preset": "hero_centro"},
                  "mossa": "cut", "in": "fade", "out": "fade", "show": True,
                  "ruolo": "primo_piano", "volume": 80, "dwell_s": d3,
                  "label": f"CONGEDO {POOL_LEGGERO[pool[2]]}", "tipo_audio": "musica"})
    url, dida = FIRME.get(momento, FIRME["sera"])
    scene.append({"video": url, "seek": 0, "tile": "main", "geo": {"preset": "full"},
                  "mossa": "none", "in": "fade", "out": "fade", "show": True,
                  "ruolo": "firma_visiva", "audio": False, "dwell_s": 50,
                  "label": f"FIRMA {momento}", "tipo_audio": "muto"})
    return {"ok": True, "momento": momento,
            "totale_s": sum(s["dwell_s"] for s in scene), "scene": scene,
            "nota": "bozza NON pubblicata: publish_seq con VIA per l'onda"}


@mcp.tool()
def componi_scena_brano_in_onda(geo_preset: str = "hero_centro", offset_s: int = 0) -> dict:
    """Compone una scena per mostrare il brano attualmente in onda (MAIN_TRACK).
    Calcola automaticamente i secondi residui del brano (dwell_s) leggendo in_onda.json.
    Non ha il campo 'video', usa 'media': 'main_track'. Offset_s permette di accorciare/allungare il dwell."""
    dwell = 180  # fallback
    titolo = "Brano in onda"
    in_onda_path = os.path.join(AUDIO, "in_onda.json")
    if os.path.exists(in_onda_path):
        try:
            with open(in_onda_path, encoding="utf-8") as f:
                dati = json.load(f)
            residuo = dati.get("residuo_s", 0)
            titolo = dati.get("titolo", titolo)
            misurato = dati.get("misurato", "")
            if misurato:
                # 'misurato' format is 'HH:MM:SS'
                # Compute seconds elapsed since 'misurato' today
                now = datetime.now()
                h, m, s = map(int, misurato.split(":"))
                misurato_dt = now.replace(hour=h, minute=m, second=s, microsecond=0)
                if now < misurato_dt:
                    misurato_dt = misurato_dt.replace(day=now.day - 1)
                elapsed = (now - misurato_dt).total_seconds()
                dwell = max(10, int(residuo - elapsed))
            else:
                dwell = max(10, int(residuo))
        except Exception:
            pass
    
    dwell += int(offset_s)
    if dwell < 10: dwell = 10
    
    scena = {
        "media": "main_track",
        "tile": "main",
        "geo": {"preset": geo_preset},
        "mossa": "kenburns",
        "in": "fade",
        "out": "fade",
        "show": True,
        "ruolo": "primo_piano",
        "dwell_s": dwell,
        "label": f"IN ONDA: {titolo}",
        "tipo_audio": "musica"
    }
    return {"ok": True, "scena": scena, "dwell_calcolato_s": dwell, "titolo": titolo}


@mcp.tool()
def almanacco_del_giorno(data: str, n_fatti: int = 3) -> dict:
    """Tile almanacco: 2-3 fatti leggeri da Wikipedia 'on this day' (UA
    identificante, regola ecosistema). Ritorna testo neon pronto."""
    try:
        m, d = (int(x) for x in data.split("-")[1:])
    except Exception:
        return {"ok": False, "errore": "data attesa YYYY-MM-DD"}
    url = f"https://it.wikipedia.org/api/rest_v1/feed/onthisday/events/{m}/{d}"
    req = urllib.request.Request(url, headers={"User-Agent":
        "BettyEcosystem/1.0 (https://betty-manf.duckdns.org; radio-bento almanacco)"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            eventi = json.load(r).get("events", [])
    except Exception as e:
        return {"ok": False, "errore": f"wikipedia: {e}"}
    leggeri = [e for e in eventi if e.get("text") and len(e["text"]) < 180][: int(n_fatti)]
    if not leggeri:
        return {"ok": False, "errore": "nessun fatto leggero trovato"}
    righe = "\n".join(f"• {data.split('-')[0]} fa o oggi: {e['text']}" for e in leggeri)
    return {"ok": True, "testo_neon": f"ALMANACCO {data}\n{righe}", "n_fatti": len(leggeri)}


@mcp.tool()
def firma_per_momento(momento: str, alternativa: str = "") -> dict:
    """Immagine firma (STATICA PD) per momento: mattina=colazione Heda,
    sera/notte=Whistler. Ritorna url+didascalia."""
    url, dida = FIRME.get(momento, FIRME["sera"])
    if alternativa:
        dida = alternativa
    return {"momento": momento, "url": url, "didascalia": dida}


@mcp.tool()
def stato_palcoscenico() -> dict:
    """Cosa c'e' in onda ORA: seq attiva, scena in main, t trascorso, prossimi
    cambi — per intervenire a ragion veduta."""
    import glob
    out = {}
    
    bento_cmd_path = os.environ.get("BENTO_CMD", BENTO_CMD)
    bento_live_path = os.environ.get("BENTO_OUT", BENTO_LIVE)
    
    out["file_bersaglio"] = {
        "bento_cmd": bento_cmd_path,
        "bento_live": bento_live_path,
        "modalita": "TEST" if "test" in bento_cmd_path else "LIVE"
    }

    cmd_seq = None
    try:
        cmd = json.load(open(bento_cmd_path, encoding="utf-8"))
        cmd_seq = cmd.get("seq")
        out["seq_pubblicata"] = cmd_seq
        scene = cmd.get("scene", [])
    except Exception as e:
        return {"errore": f"lettura bento_cmd: {e}"}
        
    t0 = None
    try:
        with open(REGISTA_LOG, encoding="utf-8", errors="replace") as f:
            for riga in f.readlines()[::-1]:
                if "MONTAGGIO" in riga and "partito" in riga:
                    t0 = datetime.fromisoformat(riga[:19])
                    break
    except Exception:
        pass
    t_trascorso = round((datetime.now() - t0).total_seconds()) if t0 else None
    out["t_trascorso_s"] = t_trascorso
    out["scene"] = []
    acc = 0
    for s in scene:
        dw = s.get("dwell_s", s.get("dwell", 0))
        stato = "in_onda" if (t_trascorso is not None and acc <= t_trascorso < acc + dw) else \
                ("passata" if t_trascorso is not None and t_trascorso >= acc + dw else "in_coda")
        out["scene"].append({"label": s.get("label"), "t": f"{acc}-{acc+dw}", "stato": stato})
        acc += dw
        
    live_seq = None
    try:
        live = json.load(open(bento_live_path, encoding="utf-8"))
        live_seq = live.get("seq")
        out["seq_live"] = live_seq
        
        tiles = live.get("tiles", {})
        out["main_ora"] = tiles.get("main", {}).get("label")
        out["tile_visibili"] = [k for k, v in tiles.items() if v.get("show", True)]
        out["tile_con_audio"] = [k for k, v in tiles.items() if v.get("volume", 0) > 0]
    except Exception as e:
        out["errore_live"] = str(e)
        
    if cmd_seq is not None and live_seq is not None:
        if cmd_seq == live_seq:
            out["stato_pubblicazione"] = "Il tuo ultimo publish E' ATTERRATO."
        else:
            dt = 0
            try:
                cmd_time = os.path.getmtime(bento_cmd_path)
                dt = round(time.time() - cmd_time)
            except: pass
            out["stato_pubblicazione"] = f"Il tuo ultimo publish NON E' ANCORA ATTERRATO (da {dt} secondi)."

    fresco = None
    fresco_time = 0
    stato_dir = os.path.join(CORE, "sveglia_stato")
    for st_file in glob.glob(os.path.join(stato_dir, "stato*.json")):
        try:
            mt = os.path.getmtime(st_file)
            if mt > fresco_time:
                fresco_time = mt
                fresco = st_file
        except: pass
        
    if fresco:
        try:
            st = json.load(open(fresco, encoding="utf-8"))
            out["stato_client"] = {
                "id": os.path.basename(fresco),
                "started": st.get("started"),
                "yt": st.get("yt"),
                "cur": st.get("cur"),
                "ts": st.get("ts")
            }
        except: pass
        
    return out


@mcp.tool()
def attiva_fallback_b(motivo: str, via_ref: str = "", onda_reale: bool = False) -> dict:
    """Attiva il fallback B (arco ACCANTO di riserva) ripubblicandolo su
    bento_cmd. Se onda_reale serve il VIA di Enrico; per test basta il motivo."""
    if onda_reale and not via_ref.strip():
        return {"ok": False, "errore": "onda vera senza VIA di Enrico: rifiuto"}
    res = _rd().pubblica_seq(FALLBACK_B)
    if res.get("ok"):
        t = _tracker()
        t["fallback_b"] = {"motivo": motivo, "via_ref": via_ref or None,
                           "attivato": datetime.now().isoformat(timespec="seconds")}
        _tracker_scrivi(t)
    return res


@mcp.tool()
def leggi_posizione() -> dict:
    """Ultimo evento posizione_live.json + freschezza (ttl 120s). Solo lettura."""
    try:
        dati = json.load(open(POSIZIONE_LIVE, encoding="utf-8"))
    except Exception as e:
        return {"ok": False, "errore": f"posizione_live: {e}"}
    eta = time.time() - float(dati.get("ts", 0))
    return {"ok": True, "evento": dati, "eta_s": round(eta),
            "fresco": eta <= 120, "stato": "fresco" if eta <= 120 else "scaduto"}


@mcp.tool()
def cue_transizione(scena_entrante_label: str, durata_crossfade_s: int = 3,
                    file_sorgente: str = "") -> dict:
    """Marca la transizione musica->musica come crossfade esplicito sulla
    scena entrante (regola Manfredo): in='fade' + nota durata. Riscrive il
    file show-run (default bento_cmd.json) atomico+flock."""
    path = file_sorgente or BENTO_CMD
    if not os.path.isabs(path):
        path = os.path.join(CONSEGNE, path)
    try:
        dati = json.load(open(path, encoding="utf-8"))
    except Exception as e:
        return {"ok": False, "errore": f"file: {e}"}
    trovate = [s for s in dati.get("scene", []) if scena_entrante_label in (s.get("label") or "")]
    if not trovate:
        return {"ok": False, "errore": f"nessuna scena con label '{scena_entrante_label}'"}
    for s in trovate:
        s["in"] = "fade"
        s["nota_transizione"] = f"crossfade esplicito {int(durata_crossfade_s)}s (regola una-musica)"
    lock = _lock_bento_cmd()
    try:
        _scrivi_atomico(path, dati)
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()
    return {"ok": True, "scene_marcatе": len(trovate), "file": os.path.basename(path)}


if __name__ == "__main__":
    mcp.run()
