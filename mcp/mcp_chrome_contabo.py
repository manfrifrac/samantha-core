#!/usr/bin/env python3
"""
mcp_chrome_contabo.py — MCP CHROME CONTABO (canale agenti -> Chrome GUI Contabo).

Clonato dal pattern di mcp_chrome_fisso.py (chrome-fisso :9223, mandato
06/08 via Betty/Leo) e PUNTATO alla nuova istanza GUI del Contabo: CDP
127.0.0.1:9224, visibile a Manfredo via noVNC http://100.75.68.16:6080/vnc.html.
Exec: exec_chrome_automation, visione Manfredo msg 10455, 18/08/2026.

Perche' serve: il Chrome del Surface (tunnel :9222) e' instabile; gli agenti
migrano su QUESTO canale per scraping e automazione.

DIFFERENZE VOLUTE dal modello (documentate, NON bug):
  1. PREFISSO TOOL `chrome_contabo_*` (niente collisione con chrome_* di
     chrome-fisso / chrome-local).
  2. _verifica_browser al posto di _ensure_chrome: l'istanza :9224 e' DI
     PROPRIETA' di core/chrome_gui_service.py (Xvfb :99 + Chrome headful +
     x11vnc + websockify, guardia flock). QUESTO SERVER NON SPAWNA MAI un
     secondo Chrome (rischio concorrenza): si limita a verificare e a dire
     di controllare il servizio.
  3. NIENTE fallback `/json/new` in open_url: la regola nucleo vieta il
     foreground (bringToFront). Solo Target.createTarget in background.
  4. Aggiunti: read_content (strutturato), download_pdf (Page.printToPDF,
     niente dialog nativo), log chiamate (regola 10), flusso CAPTCHA /
     intervento umano (rileva_captcha, ask_human, human_stato, human_risolvi,
     human_annulla).

REGOLE APPLICATE (regole/chrome_e_browser.md): tab in background, mai
bringToFront / window.focus() / /json/new, screenshot come unico sguardo,
tab per agente con registro flock, niente pulsanti di download nativi,
muro login/CAPTCHA -> stop + screenshot + avviso a Manfredo.
"""
import builtins
import fcntl
import json
import os
import subprocess  # non usato per Chrome: resta solo per compatibilita' pattern
import sys
import tempfile
import time
import urllib.request
import re
from datetime import datetime

from mcp.server.fastmcp import FastMCP

_real_print = builtins.print


def _err_print(*a, **k):
    k.setdefault("file", sys.stderr)
    _real_print(*a, **k)


builtins.print = _err_print

mcp = FastMCP("Chrome Contabo")

# 31/08/2026 (mandato samantha_1, MVP multi-Chrome isolato): porta parametrica
# via env var, DEFAULT INVARIATO (9224 = comportamento identico a prima di
# oggi). CHROME_CONTABO_PORT=9230 punta questo stesso script alla seconda
# istanza (chrome_gui_service_2.py). I path condivisi in /tmp vengono
# namespaced per porta quando NON e' quella di default, altrimenti due
# processi MCP (uno per istanza) collidono sugli stessi file di
# registro/log/evidenze — invisibile finche' non giri due istanze insieme.
CHROME_PORT = int(os.environ.get("CHROME_CONTABO_PORT", "9224"))
_ISTANZA_DEFAULT = (CHROME_PORT == 9224)
CHROME_BASE = f"http://127.0.0.1:{CHROME_PORT}"
_NOVNC_WEB_PORT = int(os.environ.get(
    "CHROME_CONTABO_NOVNC_PORT", "6080" if _ISTANZA_DEFAULT else "6090"))
NOVNC_URL = f"http://100.75.68.16:{_NOVNC_WEB_PORT}/vnc.html"
_SUFFIX = "" if _ISTANZA_DEFAULT else f"_{CHROME_PORT}"
REGISTRO = os.path.join("/tmp", f"chrome_contabo_runtime{_SUFFIX}",
                        f"chrome_contabo_tabs{_SUFFIX}.json")
EVIDENZE = f"/tmp/chrome_contabo_evidenze{_SUFFIX}"
RUNTIME = f"/tmp/chrome_contabo_runtime{_SUFFIX}"
DRYRUN_RECAP = os.path.join(RUNTIME, "dryrun_recaps")      # collaudo: MAI consegnato
RECAP_DIR = "/tmp/betty_recaps"                             # produzione: motore recap -> Telegram (condiviso, non e' stato di tab)
LOG_CALLS = f"/tmp/chrome_contabo_calls{_SUFFIX}.log"        # regola 10: chi chiama, quando, cosa
CDP_TIMEOUT = 8

CONSOLE_HOOK = """(() => { if (window.__mcp_console) return 'gia';
  window.__mcp_console = [];
  const push = (liv, args) => { try { window.__mcp_console.push({ts: Date.now(), livello: liv, testo: Array.from(args).map(a => { try { return (typeof a === 'string') ? a : JSON.stringify(a); } catch (e) { return String(a); } }).join(' ')}); if (window.__mcp_console.length > 200) window.__mcp_console.shift(); } catch (e) {} };
  ['log','warn','error','info'].forEach(l => { const orig = console[l]; console[l] = (...a) => { push(l, a); orig.apply(console, a); }; });
  window.addEventListener('error', e => push('pageerror', [String(e.message)]));
  return 'ok'; })()"""


# ---------------------------------------------------------------------------
# Stato istanza (NON si spawna: istanza gestita da chrome_gui_service)
# ---------------------------------------------------------------------------


def _browser_vivo():
    try:
        with urllib.request.urlopen(CHROME_BASE + "/json/version", timeout=3) as r:
            return r.status == 200
    except Exception:
        return False


_SERVIZIO_GESTORE = "chrome_gui_service" if _ISTANZA_DEFAULT else "chrome_gui_service_2"


def _verifica_browser():
    """Sola verifica: l'istanza e' gestita dal servizio chrome_gui_service*.py
    (istanza 1 = chrome_gui_service.py :9224, istanza 2 = chrome_gui_service_2.py
    :9230, selezionata da CHROME_CONTABO_PORT). Se e' giu', NON avviarla qui
    (rischio doppio Chrome sulla stessa GUI): segnala di controllare il servizio."""
    if _browser_vivo():
        return {"vivo": True,
                "gestita_da": _SERVIZIO_GESTORE,
                "nota": "Chrome GUI, noVNC " + NOVNC_URL,
                "porta": CHROME_PORT}
    return {"vivo": False,
            "errore": f"Chrome :{CHROME_PORT} non risponde. E' gestito da "
                      f"{_SERVIZIO_GESTORE}: controlla quel servizio (guardia flock + health-check), "
                      "NON avviarlo da qui."}


def _json_cdp(path):
    with urllib.request.urlopen(CHROME_BASE + path, timeout=CDP_TIMEOUT) as r:
        return json.load(r)


def _ws_url_tab(tab_id):
    for t in _json_cdp("/json/list"):
        if t.get("id") == tab_id:
            return t.get("webSocketDebuggerUrl")
    return None


def _ws_call(ws_url, method, params=None, timeout=CDP_TIMEOUT):
    import websocket  # lazy: regola handshake
    ws = websocket.create_connection(ws_url, timeout=timeout, suppress_origin=True)
    try:
        msg_id = int(time.time() * 1000) % 10_000_000
        ws.send(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
        fine = time.time() + timeout
        while time.time() < fine:
            dato = json.loads(ws.recv())
            if dato.get("id") == msg_id:
                if "error" in dato:
                    return {"errore_cdp": dato["error"]}
                return dato.get("result")
        return {"errore_cdp": "timeout risposta"}
    finally:
        try:
            ws.close()
        except Exception:
            pass


def _eval(tab_id, expr):
    ws_url = _ws_url_tab(tab_id)
    if not ws_url:
        return None, f"tab {tab_id} non trovata"
    r = _ws_call(ws_url, "Runtime.evaluate",
                 {"expression": expr, "returnByValue": True})
    if isinstance(r, dict) and "errore_cdp" in r:
        return None, r["errore_cdp"]
    return (r or {}).get("result", {}).get("value"), None


def _tab_or_agente(tab_id, agente):
    """Risolve la tab: tab_id esplicito vince; altrimenti ultima tab del registro
    per l'agente; altrimenti la prima tab non-about:blank."""
    if tab_id:
        return tab_id
    reg = _leggi_registro()
    if agente and reg.get(agente):
        for t in reversed(reg[agente]):
            if any(x.get("id") == t for x in _json_cdp("/json/list")):
                return t
    for t in _json_cdp("/json/list"):
        if t.get("type") == "page" and "about:blank" not in t.get("url", ""):
            return t.get("id")
    return None


# --- registro tab per agente (isolamento) ---

def _leggi_registro():
    try:
        return json.load(open(REGISTRO, encoding="utf-8"))
    except Exception:
        return {}


def _scrivi_registro(reg):
    lock_path = REGISTRO + ".lock"
    try:
        os.chmod(lock_path, 0o666)
    except OSError:
        pass
    with open(lock_path, "a+") as lock:
        try:
            os.chmod(lock_path, 0o666)
        except OSError:
            pass
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            fd, tmp = tempfile.mkstemp(dir=os.path.dirname(REGISTRO))
            os.chmod(tmp, 0o666)
            os.close(fd)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(reg, f, ensure_ascii=False)
            os.replace(tmp, REGISTRO)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


# --- log chiamate (regola 10: MCP condiviso) ---

def _chi_chiama():
    user = "?"
    try:
        import pwd
        user = pwd.getpwuid(os.getuid()).pw_name
    except Exception:
        pass
    return user, os.environ.get("CLAUDE_PROJECT_DIR", "")


def _reg_loga(tool, **kw):
    """Logga chiamante, orario, valore. NIENTE credenziali nei log."""
    try:
        user, proj = _chi_chiama()
        dettagli = " ".join("%s=%s" % (k, v) for k, v in kw.items()
                            if v not in (None, ""))
        linea = ("[%s] user=%s project=%s pid=%d tool=%s %s"
                 % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    user, proj or "-", os.getpid(), tool, dettagli))
        with open(LOG_CALLS, "a", encoding="utf-8") as f:
            f.write(linea.strip() + "\n")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# TOOL — base (pattern chrome-fisso su :9224)
# ---------------------------------------------------------------------------


@mcp.tool()
def chrome_contabo_stato() -> dict:
    """Stato del Chrome GUI Contabo (:9224) + tab aperte + registro agenti.
    L'istanza e' gestita da chrome_gui_service (noVNC per Manfredo)."""
    istanza = _verifica_browser()
    tabs = []
    try:
        tabs = [{"id": t.get("id"), "url": t.get("url"), "title": t.get("title"),
                 "type": t.get("type")} for t in _json_cdp("/json/list")
                if t.get("type") == "page"]
    except Exception as e:
        tabs = [{"errore": str(e)}]
    _reg_loga("chrome_contabo_stato", tab_n=str(len(tabs)))
    return {"istanza": istanza, "noVNC": NOVNC_URL,
            "tab": tabs, "registro": _leggi_registro()}


@mcp.tool()
def chrome_contabo_open_url(url: str, agente: str = "") -> dict:
    """Apre una tab NUOVA in BACKGROUND nel Chrome GUI Contabo (:9224) e la
    registra all'agente (isolamento). MAI in foreground (regola nucleo)."""
    _verifica_browser()
    tabs = _json_cdp("/json/list")
    if not tabs:
        return {"ok": False, "errore": "nessuna tab nel Chrome :9224 (servizio giu'?)"}
    ws_url = _ws_url_tab(tabs[0]["id"])
    r = _ws_call(ws_url, "Target.createTarget", {"url": url})
    if not r or "targetId" not in r:
        return {"ok": False, "errore": str(r)}
    tab_id = r["targetId"]
    if agente and tab_id:
        reg = _leggi_registro()
        reg.setdefault(agente, []).append(tab_id)
        _scrivi_registro(reg)
    if tab_id:
        _eval(tab_id, CONSOLE_HOOK)
    _reg_loga("chrome_contabo_open_url", agente=agente, tab_id=tab_id, url=url)
    return {"ok": True, "tab_id": tab_id, "url": url, "agente": agente or None,
            "nota": "tab aperta in background (regola nucleo: niente foreground)"}


@mcp.tool()
def chrome_contabo_close_tab(tab_id: str = "", agente: str = "") -> dict:
    """Chiude una tab e la toglie dal registro dell'agente."""
    tab_id = _tab_or_agente(tab_id, agente)
    if not tab_id:
        return {"ok": False, "errore": "nessuna tab risolta"}
    ws0 = next((t for t in _json_cdp("/json/list")), None)
    if ws0:
        _ws_call(_ws_url_tab(ws0["id"]), "Target.closeTarget", {"targetId": tab_id})
    reg = _leggi_registro()
    for a in list(reg):
        reg[a] = [t for t in reg[a] if t != tab_id]
        if not reg[a]:
            del reg[a]
    _scrivi_registro(reg)
    _reg_loga("chrome_contabo_close_tab", agente=agente, tab_id=tab_id)
    return {"ok": True, "tab_id": tab_id}


@mcp.tool()
def chrome_contabo_tabs(agente: str = "") -> dict:
    """Elenco tab del Chrome GUI Contabo; con agente filtra quelle del registro."""
    tabs = [{"id": t.get("id"), "url": t.get("url"), "title": t.get("title")}
            for t in _json_cdp("/json/list") if t.get("type") == "page"]
    if agente:
        mie = set(_leggi_registro().get(agente, []))
        tabs = [t for t in tabs if t["id"] in mie]
    _reg_loga("chrome_contabo_tabs", agente=agente)
    return {"tab": tabs}


@mcp.tool()
def chrome_contabo_navigate(url: str, tab_id: str = "", agente: str = "") -> dict:
    """Naviga la tab (esplicita o dell'agente) verso url; reinstalla hook console."""
    _verifica_browser()
    tab_id = _tab_or_agente(tab_id, agente)
    if not tab_id:
        return {"ok": False, "errore": "nessuna tab: aprila con chrome_contabo_open_url"}
    ws_url = _ws_url_tab(tab_id)
    r = _ws_call(ws_url, "Page.navigate", {"url": url})
    _eval(tab_id, CONSOLE_HOOK)
    _reg_loga("chrome_contabo_navigate", agente=agente, tab_id=tab_id, url=url)
    return {"ok": not (isinstance(r, dict) and "errore_cdp" in r), "tab_id": tab_id,
            "url": url, "cdp": r}


@mcp.tool()
def chrome_contabo_click(selector: str, tab_id: str = "", agente: str = "") -> dict:
    """Click su selettore CSS (scrollIntoView + click) nella tab."""
    tab_id = _tab_or_agente(tab_id, agente)
    if not tab_id:
        return {"ok": False, "errore": "nessuna tab"}
    val, err = _eval(tab_id, f"""(() => {{ const el = document.querySelector({json.dumps(selector)});
        if (!el) return 'non trovato'; el.scrollIntoView({{block:'center'}}); el.click(); return 'ok'; }})()""")
    _reg_loga("chrome_contabo_click", agente=agente, tab_id=tab_id, selector=selector)
    return {"ok": val == "ok", "esito": val or err, "tab_id": tab_id}


@mcp.tool()
def chrome_contabo_trusted_click(selector: str = "", xpath: str = "", text: str = "",
                                 tab_id: str = "", agente: str = "",
                                 double_click: bool = False, display: str = ":99") -> dict:
    """Click OS-level (xdotool su X11 :99) con eventi trusted reali (isTrusted: true,
    focus nativo). Risolve il blocco sui campi Smart Prompt / Lookup PeopleSoft (IFAD)."""
    tab_id = _tab_or_agente(tab_id, agente)
    if not tab_id:
        return {"ok": False, "errore": "nessuna tab"}
    
    js_finder = f"""
    (() => {{
        function findInDoc(doc, offX, offY) {{
            if (!doc) return null;
            let el = null;
            if ({json.dumps(selector)}) {{
                el = doc.querySelector({json.dumps(selector)});
            }} else if ({json.dumps(xpath)}) {{
                const res = doc.evaluate({json.dumps(xpath)}, doc, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null);
                el = res.singleNodeValue;
            }} else if ({json.dumps(text)}) {{
                const all = doc.querySelectorAll('a, button, span, div, img, input, label');
                for (let i = 0; i < all.length; i++) {{
                    if (all[i].textContent && all[i].textContent.trim().includes({json.dumps(text)})) {{
                        el = all[i];
                        break;
                    }}
                }}
            }}
            if (el) {{
                el.scrollIntoView({{behavior: 'instant', block: 'center', inline: 'center'}});
                const r = el.getBoundingClientRect();
                return {{
                    clientX: offX + r.left + (r.width / 2.0),
                    clientY: offY + r.top + (r.height / 2.0),
                    rect: {{left: r.left, top: r.top, width: r.width, height: r.height}},
                    tagName: el.tagName, id: el.id || null, className: el.className || null
                }};
            }}
            const iframes = doc.querySelectorAll('iframe, frame');
            for (let i = 0; i < iframes.length; i++) {{
                const ifr = iframes[i];
                const ifrRect = ifr.getBoundingClientRect();
                try {{
                    const subDoc = ifr.contentDocument || ifr.contentWindow.document;
                    const res = findInDoc(subDoc, offX + ifrRect.left, offY + ifrRect.top);
                    if (res) return res;
                }} catch (e) {{}}
            }}
            return null;
        }}
        const found = findInDoc(document, 0, 0);
        if (!found) return {{error: 'Elemento non trovato'}};
        const screenLeft = window.screenLeft ?? window.screenX ?? 0;
        const screenTop = window.screenTop ?? window.screenY ?? 0;
        const topChromeHeight = (window.outerHeight - window.innerHeight);
        const dpr = window.devicePixelRatio || 1;
        const screenX = Math.round(screenLeft + (found.clientX * dpr));
        const screenY = Math.round(screenTop + topChromeHeight + (found.clientY * dpr));
        return {{
            found: true,
            clientX: found.clientX,
            clientY: found.clientY,
            screenX: screenX,
            screenY: screenY,
            rect: found.rect,
            tagName: found.tagName,
            id: found.id,
            className: found.className
        }};
    }})()
    """
    data, err = _eval(tab_id, js_finder)
    if err or not data or data.get("error"):
        _reg_loga("chrome_contabo_trusted_click", agente=agente, tab_id=tab_id, esito="errore_trova")
        return {"ok": False, "errore": err or (data.get("error") if data else "errore valutazione JS"), "tab_id": tab_id}
    
    sx = data["screenX"]
    sy = data["screenY"]
    click_cmd = "click 1" if not double_click else "click --repeat 2 --delay 100 1"
    xdo = f"DISPLAY={display} xdotool mousemove --sync {sx} {sy} {click_cmd}"
    p = subprocess.run(xdo, shell=True, capture_output=True, text=True)
    _reg_loga("chrome_contabo_trusted_click", agente=agente, tab_id=tab_id,
              selector=selector, xpath=xpath, text=text, screenX=sx, screenY=sy,
              esito="ok" if p.returncode == 0 else "xdo_err")
    if p.returncode != 0:
        return {"ok": False, "errore": f"xdotool fallito: {p.stderr.strip()}", "coords": data, "tab_id": tab_id}
    return {"ok": True, "coords": data, "xdotool_cmd": xdo, "tab_id": tab_id}


@mcp.tool()
def chrome_contabo_fill(selector: str, valore: str, tab_id: str = "", agente: str = "") -> dict:
    """Riempie un input (value + eventi input/change) nella tab."""
    tab_id = _tab_or_agente(tab_id, agente)
    if not tab_id:
        return {"ok": False, "errore": "nessuna tab"}
    val, err = _eval(tab_id, f"""(() => {{ const el = document.querySelector({json.dumps(selector)});
        if (!el) return 'non trovato'; el.value = {json.dumps(valore)};
        el.dispatchEvent(new Event('input', {{bubbles:true}}));
        el.dispatchEvent(new Event('change', {{bubbles:true}})); return 'ok'; }})()""")
    _reg_loga("chrome_contabo_fill", agente=agente, tab_id=tab_id,
              selector=selector, valore="<nascosto>" if valore else "")
    return {"ok": val == "ok", "esito": val or err, "tab_id": tab_id}


@mcp.tool()
def chrome_contabo_read(tab_id: str = "", agente: str = "", max_chars: int = 6000) -> str:
    """Testo visibile della tab (troncato a max_chars)."""
    tab_id = _tab_or_agente(tab_id, agente)
    if not tab_id:
        return "nessuna tab"
    val, err = _eval(tab_id, "document.body ? document.body.innerText : ''")
    if err:
        return f"errore: {err}"
    _reg_loga("chrome_contabo_read", agente=agente, tab_id=tab_id, max_chars=int(max_chars))
    return (val or "")[:int(max_chars)]


@mcp.tool()
def chrome_contabo_read_content(tab_id: str = "", agente: str = "",
                                max_chars: int = 6000) -> dict:
    """Lettura completa della tab: url, titolo e testo visibile (innerText).
    E' il tool 'read_content' del mandato."""
    tab_id = _tab_or_agente(tab_id, agente)
    if not tab_id:
        return {"ok": False, "errore": "nessuna tab"}
    testo, err = _eval(tab_id, "document.body ? document.body.innerText : ''")
    if err:
        return {"ok": False, "errore": err}
    titolo, _ = _eval(tab_id, "document.title || ''")
    url, _ = _eval(tab_id, "location.href || ''")
    _reg_loga("chrome_contabo_read_content", agente=agente, tab_id=tab_id,
              max_chars=int(max_chars))
    return {"ok": True, "tab_id": tab_id, "url": url or "",
            "titolo": titolo or "", "testo": (testo or "")[:int(max_chars)]}


@mcp.tool()
def chrome_contabo_console(tab_id: str = "", agente: str = "", n: int = 50) -> dict:
    """Buffer console/pageerror della tab (hook installato da open/navigate)."""
    tab_id = _tab_or_agente(tab_id, agente)
    if not tab_id:
        return {"errore": "nessuna tab"}
    val, err = _eval(tab_id, f"(window.__mcp_console || []).slice(-{int(n)})")
    if err:
        return {"errore": err}
    return {"console": val if val is not None else []}


@mcp.tool()
def chrome_contabo_screenshot(tab_id: str = "", agente: str = "") -> str:
    """Screenshot PNG della tab in /tmp/chrome_contabo_evidenze/<ts>_<tab8>.png.
    L'unico 'sguardo' sulla pagina (regola nucleo)."""
    _verifica_browser()
    tab_id = _tab_or_agente(tab_id, agente)
    if not tab_id:
        return "nessuna tab"
    ws_url = _ws_url_tab(tab_id)
    r = _ws_call(ws_url, "Page.captureScreenshot", {"format": "png"}, timeout=15)
    if not isinstance(r, dict) or "data" not in r:
        return f"screenshot fallito: {r}"
    os.makedirs(EVIDENZE, exist_ok=True)
    nome = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + tab_id[:8] + ".png"
    path = os.path.join(EVIDENZE, nome)
    import base64
    with open(path, "wb") as f:
        f.write(base64.b64decode(r["data"]))
    _reg_loga("chrome_contabo_screenshot", agente=agente, tab_id=tab_id, path=path)
    return path


@mcp.tool()
def chrome_contabo_download_pdf(tab_id: str = "", agente: str = "", url: str = "",
                                dest_dir: str = "", filename: str = "") -> dict:
    """Salva la tab come PDF via Page.printToPDF (niente dialog nativo di
    Windows: regola nucleo). Opzionale url per navigare prima di stampare."""
    _verifica_browser()
    tab_id = _tab_or_agente(tab_id, agente)
    if not tab_id:
        return {"ok": False, "errore": "nessuna tab"}
    ws_url = _ws_url_tab(tab_id)
    if url:
        _ws_call(ws_url, "Page.navigate", {"url": url})
        for _ in range(20):
            st, _ = _eval(tab_id, "document.readyState")
            if st == "complete":
                break
            time.sleep(0.5)
    r = _ws_call(ws_url, "Page.printToPDF", {"printBackground": True}, timeout=40)
    if not isinstance(r, dict) or "data" not in r:
        return {"ok": False, "errore": str(r)}
    dest = dest_dir or EVIDENZE
    os.makedirs(dest, exist_ok=True)
    if not filename:
        nome = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + tab_id[:8] + ".pdf"
    else:
        nome = filename if filename.endswith(".pdf") else filename + ".pdf"
    path = os.path.join(dest, nome)
    import base64
    with open(path, "wb") as f:
        f.write(base64.b64decode(r["data"]))
    _reg_loga("chrome_contabo_download_pdf", agente=agente, tab_id=tab_id,
              path=path, url=url or "")
    return {"ok": True, "path": path, "tab_id": tab_id}


# ---------------------------------------------------------------------------
# TOOL — flusso CAPTCHA / intervento umano (la parte voluta da Manfredo)
# ---------------------------------------------------------------------------

_CAPTCHA_INDIZI = [
    "captcha", "recaptcha", "hcaptcha", "turnstile", "cf-challenge",
    "checking your browser", "verify you are human", "are you human",
    "non sei un robot", "verifica che non sei un robot", "verifica umana",
    "prova che sei umano", "access denied", "accesso negato",
    "two-factor", "2fa", "two factor", "autenticazione a due fattori",
    "verifica in due passaggi", "inserisci il codice", "enter the code",
    "sign in", "log in", "accedi", "login", "password", "e-mail o telefono",
]
_CAPTCHA_IFRAME = ["recaptcha", "hcaptcha", "turnstile", "captcha"]


@mcp.tool()
def chrome_contabo_rileva_captcha(tab_id: str = "", agente: str = "") -> dict:
    """Euristica: la tab presenta un muro CAPTCHA/login/2FA che richiede
    intervento umano? Analizza testo pagina, iframe captcha e selettori."""
    tab_id = _tab_or_agente(tab_id, agente)
    if not tab_id:
        return {"ok": False, "errore": "nessuna tab"}
    testo, err = _eval(tab_id, "document.body ? document.body.innerText : ''")
    if err:
        return {"ok": False, "errore": err}
    testo_basso = (testo or "").lower()
    trovati = [i for i in _CAPTCHA_INDIZI if i in testo_basso]
    # iframe con src che contiene parole captcha + selettori classici
    frame_js = """(() => { const out = [];
      document.querySelectorAll('iframe').forEach(f => {
        const src = (f.getAttribute('src') || '').toLowerCase();
        if (/%s/.test(src)) out.push(src.slice(0, 120));
      });
      const sel = document.querySelectorAll('.g-recaptcha, [data-sitekey], .h-captcha');
      return {iframe: out, selettori: sel.length}; })()""" % "|".join(_CAPTCHA_IFRAME)
    frame_info, ferr = _eval(tab_id, frame_js)
    if ferr:
        frame_info = {"iframe": [], "selettori": 0}
    captcha = bool(trovati) or bool((frame_info or {}).get("iframe")) \
        or ((frame_info or {}).get("selettori") or 0) > 0
    _reg_loga("chrome_contabo_rileva_captcha", agente=agente, tab_id=tab_id,
              captcha=str(captcha))
    return {"ok": True, "captcha": captcha, "indizi": trovati,
            "iframe_captcha": (frame_info or {}).get("iframe", []),
            "selettori_captcha": (frame_info or {}).get("selettori", 0),
            "tab_id": tab_id}


def _slug(agente):
    s = (agente or "").strip() or "anonimo"
    return re.sub(r"[^A-Za-z0-9_.-]", "_", s)


def _stato_path(agente):
    return os.path.join(RUNTIME, f"attesa_humano_{_slug(agente)}.json")


def _leggi_stato_humano(agente):
    try:
        return json.load(open(_stato_path(agente), encoding="utf-8"))
    except Exception:
        return None


def _scrivi_stato_humano(agente, stato):
    os.makedirs(RUNTIME, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=RUNTIME)
    os.close(fd)
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(stato, f, ensure_ascii=False, indent=2)
    os.replace(tmp, _stato_path(agente))
    return stato


def _fai_screenshot(tab_id):
    try:
        return chrome_contabo_screenshot(tab_id=tab_id, agente="")
    except Exception:
        return None


@mcp.tool()
def chrome_contabo_ask_human(message: str, agente: str, tab_id: str = "",
                             screenshot: bool = True, wait_sec: int = 0,
                             timeout_sec: int = 3600, dry_run: bool = False) -> dict:
    """FLUSSO INTERVENTO UMANO. Quando un agente incontra CAPTCHA/login o
    serve un'azione umana: lascia la pagina APERTA, scatta screenshot, avvisa
    Manfredo su Telegram (recap) con l'URL noVNC e l'istruzione, e mette in
    attesa. Poi l'agente termina il turno: Manfredo risolve visivamente via
    noVNC e risponde 'ok' su Telegram. Al ritorno l'agente chiama
    chrome_contabo_human_stato per verificare e chrome_contabo_human_risolvi
    per registrare, poi riprende.
    Con wait_sec>0 polla lo stato (utile in script) fino a timeout_sec.
    dry_run=True scrive l'avviso SOLO in /tmp/chrome_contabo_runtime/
    dryrun_recaps/ (collaudo): MAI consegnato a Manfredo."""
    _verifica_browser()
    tab_id = _tab_or_agente(tab_id, agente)
    if not tab_id:
        return {"ok": False, "errore": "nessuna tab: non ho una pagina da lasciare aperta"}
    shot = None
    if screenshot:
        shot = _fai_screenshot(tab_id)
    testo = ("🛑 **Ho bisogno di te sul browser**\n\n"
             "Apri: `%s`\n\n"
             "Fai: **%s**\n\n"
             "Poi scrivimi: **ok**" % (NOVNC_URL, (message or "").strip()))
    if shot:
        testo += "\n\n[FILE: %s]" % shot
    recap_dir = DRYRUN_RECAP if dry_run else RECAP_DIR
    os.makedirs(recap_dir, exist_ok=True)
    recap_path = os.path.join(recap_dir, _slug(agente) + ".txt")
    with open(recap_path, "w", encoding="utf-8") as f:
        f.write(testo + "\n")
    stato = {
        "agente": _slug(agente), "stato": "in_attesa",
        "messaggio": (message or "").strip(),
        "screenshot": shot, "tab_id": tab_id,
        "noVNC": NOVNC_URL,
        "richiesto_da": _chi_chiama()[0],
        "ts_richiesta": datetime.now().isoformat(timespec="seconds"),
        "ts_risolto": None, "esito": None, "note": None,
        "dry_run": bool(dry_run),
        "timeout_sec": int(timeout_sec),
    }
    _scrivi_stato_humano(agente, stato)
    _reg_loga("chrome_contabo_ask_human", agente=_slug(agente), tab_id=tab_id,
              recap=recap_path, dry_run=str(dry_run))
    if int(wait_sec) > 0:
        scadenza = time.time() + int(wait_sec)
        while time.time() < scadenza:
            cur = _leggi_stato_humano(agente)
            if cur and cur.get("stato") != "in_attesa":
                return {"ok": True, "recap_path": recap_path, "screenshot": shot,
                        "stato": cur, "esito_attesa": "completata"}
            time.sleep(2)
        cur = _leggi_stato_humano(agente) or stato
        if cur.get("stato") == "in_attesa":
            cur["stato"] = "timeout"
            cur["note"] = "attesa scaduta senza risposta"
            _scrivi_stato_humano(agente, cur)
        return {"ok": True, "recap_path": recap_path, "screenshot": shot,
                "stato": cur, "esito_attesa": "timeout"}
    return {"ok": True, "recap_path": recap_path, "screenshot": shot,
            "stato": stato,
            "come_riprendere": "attendi l'ok di Manfredo su Telegram; poi "
                               "chiama chrome_contabo_human_stato e "
                               "chrome_contabo_human_risolvi"}


@mcp.tool()
def chrome_contabo_human_stato(agente: str) -> dict:
    """Stato dell'attesa umana per l'agente: in_attesa / risolto / annullato /
    timeout. Usala al risveglio per verificare se Manfredo ha gia' risolto."""
    cur = _leggi_stato_humano(agente)
    _reg_loga("chrome_contabo_human_stato", agente=_slug(agente),
              stato=(cur or {}).get("stato"))
    if not cur:
        return {"ok": False, "errore": "nessuna attesa in corso per questo agente"}
    return {"ok": True, **cur}


@mcp.tool()
def chrome_contabo_human_risolvi(agente: str, esito: str = "ok",
                                 note: str = "") -> dict:
    """Registra che Manfredo ha risolto (ha scritto 'ok' su Telegram) e chiude
    l'attesa. L'agente la chiama al ritorno prima di riprendere il lavoro."""
    cur = _leggi_stato_humano(agente)
    if not cur:
        return {"ok": False, "errore": "nessuna attesa in corso per questo agente"}
    if cur.get("stato") in ("risolto", "annullato", "timeout"):
        return {"ok": True, "stato": cur, "nota": "attesa gia' chiusa"}
    cur["stato"] = "risolto"
    cur["esito"] = esito
    cur["note"] = note
    cur["ts_risolto"] = datetime.now().isoformat(timespec="seconds")
    _scrivi_stato_humano(agente, cur)
    _reg_loga("chrome_contabo_human_risolvi", agente=_slug(agente), esito=esito)
    return {"ok": True, "stato": cur}


@mcp.tool()
def chrome_contabo_human_annulla(agente: str, note: str = "") -> dict:
    """Annulla l'attesa umana (es. non serve piu' l'intervento)."""
    cur = _leggi_stato_humano(agente)
    if not cur:
        return {"ok": False, "errore": "nessuna attesa in corso per questo agente"}
    cur["stato"] = "annullato"
    cur["note"] = note
    cur["ts_risolto"] = datetime.now().isoformat(timespec="seconds")
    _scrivi_stato_humano(agente, cur)
    _reg_loga("chrome_contabo_human_annulla", agente=_slug(agente))
    return {"ok": True, "stato": cur}


if __name__ == "__main__":
    try:
        os.makedirs(RUNTIME, exist_ok=True)
        os.makedirs(EVIDENZE, exist_ok=True)
        _reg_loga("__server_start", pid=os.getpid())
    except Exception:
        pass
    mcp.run()
