import os
import io
from mcp.server.fastmcp import FastMCP
from google_auth import get_service
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload

# Initialize FastMCP Server
mcp = FastMCP("Google Drive Controller")

@mcp.tool()
def gdrive_list_files(query: str = None, limit: int = 15) -> str:
    """
    Cerca e elenca i file o le cartelle presenti su Google Drive.
    :param query: Termine di ricerca (es: nome del file). Lascia vuoto per elencare tutto.
    :param limit: Numero massimo di risultati da restituire (default 15).
    """
    try:
        service = get_service('drive', 'v3')
        q_str = "trashed = false"
        if query:
            q_str += f" and name contains '{query}'"
            
        results = service.files().list(
            q=q_str,
            pageSize=limit,
            fields="nextPageToken, files(id, name, mimeType, size)"
        ).execute()
        
        files = results.get('files', [])
        if not files:
            return "Nessun file trovato su Google Drive."
            
        output = "File trovati su Google Drive:\n\n"
        for f in files:
            size_str = f"{int(f['size'])/1024:.1f} KB" if 'size' in f else "N/A"
            is_folder = "Sì" if f['mimeType'] == 'application/vnd.google-apps.folder' else "No"
            output += f"- Nome: {f['name']}\n  ID: {f['id']}\n  Tipo: {f['mimeType']} (Cartella: {is_folder})\n  Dimensione: {size_str}\n"
            output += "-"*40 + "\n"
        return output
    except Exception as e:
        return f"Errore durante l'elenco dei file: {str(e)}"

@mcp.tool()
def gdrive_create_folder(folder_name: str, parent_folder_id: str = None) -> str:
    """
    Crea una nuova cartella su Google Drive.
    :param folder_name: Nome della nuova cartella.
    :param parent_folder_id: (Opzionale) ID della cartella genitore in cui crearla.
    """
    try:
        service = get_service('drive', 'v3')
        file_metadata = {
            'name': folder_name,
            'mimeType': 'application/vnd.google-apps.folder'
        }
        if parent_folder_id:
            file_metadata['parents'] = [parent_folder_id]
            
        folder = service.files().create(body=file_metadata, fields='id').execute()
        return f"Cartella '{folder_name}' creata con successo! ID: {folder.get('id')}"
    except Exception as e:
        return f"Errore durante la creazione della cartella: {str(e)}"

@mcp.tool()
def gdrive_upload_file(file_path: str, parent_folder_id: str = None) -> str:
    """
    Carica un file locale su Google Drive.
    :param file_path: Percorso assoluto del file locale da caricare (es: C:\\Users\\annag\\documento.pdf).
    :param parent_folder_id: (Opzionale) ID della cartella di destinazione su Google Drive.
    """
    if not os.path.exists(file_path):
        return f"Errore: il file locale '{file_path}' non esiste."
        
    try:
        service = get_service('drive', 'v3')
        file_name = os.path.basename(file_path)
        file_metadata = {'name': file_name}
        if parent_folder_id:
            file_metadata['parents'] = [parent_folder_id]
            
        media = MediaFileUpload(file_path, resumable=True)
        file = service.files().create(body=file_metadata, media_body=media, fields='id').execute()
        return f"File '{file_name}' caricato con successo! ID di Google Drive: {file.get('id')}"
    except Exception as e:
        return f"Errore durante il caricamento del file: {str(e)}"

@mcp.tool()
def gdrive_download_file(file_id: str, dest_path: str) -> str:
    """
    Scarica un file da Google Drive salvandolo sul computer locale.
    :param file_id: ID del file di Google Drive da scaricare.
    :param dest_path: Percorso assoluto locale in cui salvare il file (incluso il nome del file, es: C:\\Users\\annag\\scaricato.pdf).
    """
    try:
        service = get_service('drive', 'v3')
        request = service.files().get_media(fileId=file_id)
        
        # Create directories if they do not exist
        os.makedirs(os.path.dirname(dest_path), exist_ok=True)
        
        with io.FileIO(dest_path, 'wb') as fh:
            downloader = MediaIoBaseDownload(fh, request)
            done = False
            while not done:
                status, done = downloader.next_chunk()
        return f"File scaricato con successo e salvato in '{dest_path}'."
    except Exception as e:
        return f"Errore durante il download del file: {str(e)}"

@mcp.tool()
def gdrive_update_file(file_id: str, file_path: str) -> str:
    """
    Aggiorna un file esistente su Google Drive sovrascrivendone il contenuto.
    Mantiene intatto l'ID originale del file, la cronologia delle versioni e i link di condivisione.
    :param file_id: ID del file da aggiornare su Google Drive (es: 1M5plKO8VjnsDu7zpVrMFM7tKqV7GsAlq).
    :param file_path: Percorso assoluto del file locale aggiornato da caricare.
    """
    if not os.path.exists(file_path):
        return f"Errore: il file locale '{file_path}' non esiste."
        
    try:
        service = get_service('drive', 'v3')
        file_name = os.path.basename(file_path)
        
        media = MediaFileUpload(file_path, resumable=True)
        file = service.files().update(
            fileId=file_id,
            media_body=media,
            fields='id, name, mimeType'
        ).execute()
        return f"File '{file_name}' (ID: {file.get('id')}) aggiornato con successo online su Google Drive!"
    except Exception as e:
        return f"Errore durante l'aggiornamento del file: {str(e)}"

if __name__ == "__main__":
    mcp.run()
