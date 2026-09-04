from mcp.server.fastmcp import FastMCP
from google_auth import get_service

# Initialize FastMCP Server
mcp = FastMCP("YouTube Controller")

@mcp.tool()
def youtube_search(query: str, limit: int = 5) -> str:
    """
    Cerca video su YouTube.
    :param query: Termini di ricerca (es: 'canzoni estate 2026').
    :param limit: Numero massimo di video da restituire (default 5).
    """
    try:
        service = get_service('youtube', 'v3')
        request = service.search().list(
            q=query,
            part="snippet",
            type="video",
            maxResults=limit
        )
        response = request.execute()
        
        items = response.get('items', [])
        if not items:
            return f"Nessun video trovato per '{query}'."
            
        output = f"Risultati di ricerca su YouTube per '{query}':\n\n"
        for item in items:
            title = item['snippet']['title']
            video_id = item['id']['videoId']
            channel = item['snippet']['channelTitle']
            output += f"- Video: {title}\n  Canale: {channel}\n  Link: https://www.youtube.com/watch?v={video_id}\n"
            output += "-"*40 + "\n"
        return output
    except Exception as e:
        return f"Errore nella ricerca YouTube: {str(e)}"

@mcp.tool()
def youtube_get_channel_stats(channel_id: str) -> str:
    """
    Recupera le statistiche di un canale YouTube specifico.
    :param channel_id: ID del canale YouTube (es: UC_x5XG1OV2P6uZZ5FSM9Ttw).
    """
    try:
        service = get_service('youtube', 'v3')
        request = service.channels().list(
            part="snippet,statistics",
            id=channel_id
        )
        response = request.execute()
        
        items = response.get('items', [])
        if not items:
            return f"Canale con ID '{channel_id}' non trovato."
            
        channel = items[0]
        title = channel['snippet']['title']
        stats = channel['statistics']
        subs = stats.get('subscriberCount', 'N/A')
        views = stats.get('viewCount', 'N/A')
        vids = stats.get('videoCount', 'N/A')
        
        output = f"Statistiche per il canale '{title}':\n"
        output += f"- Iscritti: {subs}\n"
        output += f"- Visualizzazioni totali: {views}\n"
        output += f"- Numero di video: {vids}\n"
        return output
    except Exception as e:
        return f"Errore nel recupero delle statistiche del canale: {str(e)}"

@mcp.tool()
def youtube_create_playlist(title: str, description: str = None) -> str:
    """
    Crea una nuova playlist sul tuo canale YouTube.
    :param title: Titolo della playlist.
    :param description: Descrizione della playlist.
    """
    try:
        service = get_service('youtube', 'v3')
        body = {
            'snippet': {
                'title': title,
                'description': description or ''
            },
            'status': {
                'privacyStatus': 'private'  # default to private
            }
        }
        request = service.playlists().insert(
            part="snippet,status",
            body=body
        )
        response = request.execute()
        return f"Playlist '{title}' creata con successo! ID: {response.get('id')}"
    except Exception as e:
        return f"Errore nella creazione della playlist: {str(e)}"

@mcp.tool()
def youtube_add_to_playlist(playlist_id: str, video_id: str) -> str:
    """
    Aggiunge un video a una playlist esistente.
    :param playlist_id: ID della playlist.
    :param video_id: ID del video di YouTube da aggiungere (es: dQw4w9WgXcQ).
    """
    try:
        service = get_service('youtube', 'v3')
        body = {
            'snippet': {
                'playlistId': playlist_id,
                'resourceId': {
                    'kind': 'youtube#video',
                    'videoId': video_id
                }
            }
        }
        request = service.playlistItems().insert(
            part="snippet",
            body=body
        )
        request.execute()
        return "Video aggiunto alla playlist con successo!"
    except Exception as e:
        return f"Errore nell'aggiunta del video alla playlist: {str(e)}"

if __name__ == "__main__":
    mcp.run()
