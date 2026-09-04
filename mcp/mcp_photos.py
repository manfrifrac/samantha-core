import os
import requests
from mcp.server.fastmcp import FastMCP
from google_auth import get_google_credentials

# Initialize FastMCP Server using REST API (direct Google Photos Library API calls)
mcp = FastMCP("Google Photos Controller")

@mcp.tool()
def photos_list_media_items(limit: int = 15) -> str:
    """
    Elenca le foto e i video presenti su Google Photos creati da questa applicazione (restrizione API post-2025).
    :param limit: Numero massimo di elementi da restituire (default 15).
    """
    try:
        creds = get_google_credentials()
        headers = {
            "Authorization": f"Bearer {creds.token}",
            "Content-Type": "application/json"
        }
        # Nota: dopo il 31 marzo 2025, questo endpoint restituisce SOLO i media caricati da questa specifica app
        url = f"https://photoslibrary.googleapis.com/v1/mediaItems?pageSize={limit}"
        response = requests.get(url, headers=headers)
        if response.status_code != 200:
            return f"Errore API Google Photos (Status {response.status_code}): {response.text}"
            
        data = response.json()
        items = data.get("mediaItems", [])
        if not items:
            return "Nessun elemento trovato (o nessun elemento creato da questa applicazione)."
            
        output = "Foto/Video trovati su Google Photos (creati da questa app):\n\n"
        for item in items:
            desc = item.get("description", "Nessuna descrizione")
            output += f"- ID: {item['id']}\n  Nome File: {item['filename']}\n  Descrizione: {desc}\n  Tipo: {item['mimeType']}\n  Base URL: {item['baseUrl']}\n"
            output += "-"*40 + "\n"
        return output
    except Exception as e:
        return f"Errore durante la lettura delle foto: {str(e)}"

@mcp.tool()
def photos_upload_media_item(file_path: str) -> str:
    """
    Carica una nuova foto o video locale su Google Photos tramite API REST.
    :param file_path: Percorso assoluto del file locale (es: C:\\Users\\annag\\foto.jpg).
    """
    if not os.path.exists(file_path):
        return f"Errore: il file locale '{file_path}' non esiste."
        
    try:
        creds = get_google_credentials()
        filename = os.path.basename(file_path)
        
        # 1. Carica i byte grezzi per ottenere un uploadToken
        upload_url = "https://photoslibrary.googleapis.com/v1/uploads"
        headers = {
            "Authorization": f"Bearer {creds.token}",
            "Content-type": "application/octet-stream",
            "X-Goog-Upload-Content-Length": str(os.path.getsize(file_path)),
            "X-Goog-Upload-Protocol": "raw"
        }
        
        print("Uploading bytes...")
        with open(file_path, 'rb') as f:
            img_data = f.read()
            
        response = requests.post(upload_url, headers=headers, data=img_data)
        if response.status_code != 200:
            return f"Errore durante il caricamento dei byte: {response.text}"
            
        upload_token = response.text
        
        # 2. Crea il media item nella libreria usando l'uploadToken
        create_url = "https://photoslibrary.googleapis.com/v1/mediaItems:batchCreate"
        create_headers = {
            "Authorization": f"Bearer {creds.token}",
            "Content-Type": "application/json"
        }
        create_body = {
            "newMediaItems": [
                {
                    "description": f"Caricato da Betty Assistant: {filename}",
                    "simpleMediaItem": {
                        "uploadToken": upload_token
                    }
                }
            ]
        }
        
        create_response = requests.post(create_url, headers=create_headers, json=create_body)
        if create_response.status_code != 200:
            return f"Errore durante la creazione del mediaItem: {create_response.text}"
            
        res_json = create_response.json()
        creation_results = res_json.get("newMediaItemResults", [])
        if creation_results and creation_results[0].get("status", {}).get("message") == "Success":
            media_item = creation_results[0].get("mediaItem", {})
            return f"Foto '{filename}' caricata con successo su Google Foto! ID: {media_item.get('id')}"
        else:
            return f"Errore nella creazione del media item: {create_response.text}"
            
    except Exception as e:
        return f"Errore durante l'upload della foto: {str(e)}"

@mcp.tool()
def photos_download_media_item(media_item_id: str, dest_path: str) -> str:
    """
    Scarica una foto da Google Photos (se creata da questa applicazione) salvandola localmente.
    :param media_item_id: ID dell'elemento multimediale.
    :param dest_path: Percorso assoluto locale in cui salvare il file.
    """
    try:
        creds = get_google_credentials()
        headers = {
            "Authorization": f"Bearer {creds.token}"
        }
        
        # Recupera dettagli
        url = f"https://photoslibrary.googleapis.com/v1/mediaItems/{media_item_id}"
        response = requests.get(url, headers=headers)
        if response.status_code != 200:
            return f"Errore nel recupero dei dettagli: {response.text}"
            
        item_data = response.json()
        base_url = item_data.get("baseUrl")
        filename = item_data.get("filename", "downloaded_media.jpg")
        
        if not base_url:
            return "Errore: baseUrl non trovato per questo elemento (forse non creato da questa app)."
            
        if os.path.isdir(dest_path):
            final_path = os.path.join(dest_path, filename)
        else:
            final_path = dest_path
            
        os.makedirs(os.path.dirname(final_path), exist_ok=True)
        
        # Scarica usando =d
        download_url = f"{base_url}=d"
        media_response = requests.get(download_url)
        if media_response.status_code != 200:
            return f"Errore durante lo scaricamento: {media_response.status_code}"
            
        with open(final_path, 'wb') as f:
            f.write(media_response.content)
            
        return f"Elemento '{filename}' scaricato con successo in '{final_path}' ({len(media_response.content)} byte)!"
    except Exception as e:
        return f"Errore durante il download: {str(e)}"

if __name__ == "__main__":
    mcp.run()
