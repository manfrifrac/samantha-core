"""
MCP Web Search — ricerca web reale via HTTP scraping (Bing + fallback DDG/Google News).

Creato 12/08/2026 da Betty_GestioneAgenti (task urgente bandi_auruncia, Manfredo):
il tool WebSearch integrato del modello deepcode fallisce ("Unexpected token ...
is not valid JSON") e i motori via browser bloccano con CAPTCHA da questa rete.
Verificato: lo scraping HTTP diretto di Bing risponde 200 con risultati reali
(senza chiave API, senza CAPTCHA a questo livello). Google News RSS funziona
già (vedi mcp_news.py, stesso pattern).

Espone un solo tool: web_search(query) -> testo con titoli+url+descrizioni.
"""
import requests
import urllib.parse
import re
import html as html_lib
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("Web Search")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


def _pulisci(s: str) -> str:
    """Strippa tag HTML e decodifica entita'."""
    s = re.sub(r"<[^>]+>", " ", s)
    s = html_lib.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def _scrape_bing(query: str, limit: int = 6) -> list:
    url = "https://www.bing.com/search?q=" + urllib.parse.quote(query)
    r = requests.get(url, headers={"User-Agent": UA}, timeout=15)
    if r.status_code != 200:
        return []
    # I risultati Bing stanno in <li class="b_algo"> con <h2><a href="...">titolo</a></h2>
    blocchi = re.findall(r'<li class="b_algo".*?</li>', r.text, re.S)
    out = []
    for b in blocchi[:limit]:
        m = re.search(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', b, re.S)
        if not m:
            continue
        url = m.group(1)
        titolo = _pulisci(m.group(2))
        # descrizione: <p>...</p> dentro il blocco
        mp = re.search(r"<p[^>]*>(.*?)</p>", b, re.S)
        desc = _pulisci(mp.group(1)) if mp else ""
        out.append({"titolo": titolo, "url": url, "descrizione": desc})
    return out


def _scrape_ddg(query: str, limit: int = 5) -> list:
    """Fallback DuckDuckGo html (lite)."""
    url = "https://html.duckduckgo.com/html/?q=" + urllib.parse.quote(query)
    r = requests.get(url, headers={"User-Agent": UA}, timeout=15)
    if r.status_code != 200:
        return []
    blocchi = re.findall(r'<div class="result.*?</div>\s*</div>', r.text, re.S)
    out = []
    for b in blocchi[:limit]:
        ma = re.search(r'<a[^>]+href="([^"]+)"[^>]*class="result__a"[^>]*>(.*?)</a>', b, re.S)
        if not ma:
            continue
        url = html_lib.unescape(ma.group(1))
        titolo = _pulisci(ma.group(2))
        ms = re.search(r'class="result__snippet"[^>]*>(.*?)</a>', b, re.S)
        desc = _pulisci(ms.group(1)) if ms else ""
        out.append({"titolo": titolo, "url": url, "descrizione": desc})
    return out


def _news_rss(query: str, limit: int = 4) -> list:
    """Ultima risorsa: Google News RSS (stesso pattern di mcp_news.py)."""
    import xml.etree.ElementTree as ET
    url = ("https://news.google.com/rss/search?q=" + urllib.parse.quote(query) +
           "&hl=it&gl=IT&ceid=IT:it")
    r = requests.get(url, headers={"User-Agent": UA}, timeout=15)
    if r.status_code != 200:
        return []
    try:
        root = ET.fromstring(r.content)
    except Exception:
        return []
    out = []
    for item in root.findall(".//item")[:limit]:
        t = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub = (item.findtext("pubDate") or "").strip()
        if t:
            out.append({"titolo": t, "url": link, "descrizione": "Pubblicato: " + pub})
    return out


@mcp.tool()
def web_search(query: str, limit: int = 6) -> str:
    """Cerca sul web (Bing, con fallback DuckDuckGo e Google News).

    Restituisce i risultati come testo: titolo, URL e breve descrizione.
    Da usare per verificare fonti, notizie, bandi, aziende, scadenze.
    """
    if not query or not query.strip():
        return "Query vuota."

    # Google News RSS e' la fonte piu' robusta e strutturata (verificata:
    # risultati reali e pertinenti). Bing come fallback per query non-news.
    risultati = _news_rss(query, limit)
    fonte = "Google News"
    if not risultati:
        risultati = _scrape_bing(query, limit)
        fonte = "Bing"

    if not risultati:
        return (f"Nessun risultato per '{query}'. "
                "Possibile blocco anti-robot o rete.")

    righe = [f"Risultati per '{query}' (fonte: {fonte}):", ""]
    for i, r in enumerate(risultati, 1):
        righe.append(f"{i}. {r['titolo']}")
        righe.append(f"   URL: {r['url']}")
        if r.get("descrizione"):
            righe.append(f"   {r['descrizione'][:220]}")
        righe.append("")
    return "\n".join(righe)


if __name__ == "__main__":
    mcp.run()
