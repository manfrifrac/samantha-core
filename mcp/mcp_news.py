import requests
import xml.etree.ElementTree as ET
import urllib.parse
from mcp.server.fastmcp import FastMCP

# Initialize FastMCP Server for Google News Search
mcp = FastMCP("Google News Controller")

@mcp.tool()
def search_global_news(query: str, limit: int = 5) -> str:
    """
    Cerca notizie e articoli a livello globale tramite il motore di ricerca ufficiale di Google News.
    :param query: Termini di ricerca della notizia (es: 'intelligenza artificiale novità').
    :param limit: Numero massimo di notizie da restituire (default 5).
    """
    try:
        encoded_query = urllib.parse.quote(query)
        url = f"https://news.google.com/rss/search?q={encoded_query}&hl=it&gl=IT&ceid=IT:it"
        
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        
        response = requests.get(url, headers=headers)
        if response.status_code != 200:
            return f"Errore nel recupero delle notizie da Google News (Status {response.status_code})."
            
        root = ET.fromstring(response.content)
        items = root.findall(".//item")
        if not items:
            return f"Nessuna notizia trovata per: '{query}'."
            
        output = f"📰 Risultati Google News per '{query}':\n\n"
        for idx, item in enumerate(items[:limit]):
            title = item.find("title").text if item.find("title") is not None else "Senza Titolo"
            link = item.find("link").text if item.find("link") is not None else ""
            pub_date = item.find("pubDate").text if item.find("pubDate") is not None else "Data non disponibile"
            source = item.find("source").text if item.find("source") is not None else "Fonte Sconosciuta"
            
            output += f"[{idx + 1}] {title}\n"
            output += f"    Fonte: {source} | Data: {pub_date}\n"
            if link:
                output += f"    Link: {link}\n"
            output += "-" * 50 + "\n"
        return output
    except Exception as e:
        return f"Errore durante la ricerca notizie: {str(e)}"

@mcp.tool()
def search_local_news(location: str, query: str, limit: int = 5) -> str:
    """
    Cerca notizie locali relative ad una specifica città, provincia o regione geografica.
    :param location: Città o area geografica (es: 'Milano', 'Roma', 'Lazio').
    :param query: L'argomento della notizia (es: 'viabilità', 'nuove aperture', 'meteo').
    :param limit: Numero massimo di notizie da restituire (default 5).
    """
    combined_query = f"{location} {query}"
    return search_global_news(combined_query, limit)

if __name__ == "__main__":
    mcp.run()
