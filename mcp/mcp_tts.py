import os
import asyncio
import edge_tts
from mcp.server.fastmcp import FastMCP

# Initialize FastMCP Server
mcp = FastMCP("TTS Speaker")

# Cartella predefinita per l'output audio. All'import NON si scrive MAI nella
# cartella del codice (core/ o mcp_servers/, 755, non scrivibile da un dc-*
# non privilegiato: prima faceva os.makedirs(<dir>/scratch/audio_output) e un
# dc-* moriva appena caricava questo modulo). Si scrive nella scratch condivisa
# del TTS già esistente (core/tts_scratch_condiviso, setgid dc-agents), coerente
# col fix "HOME o scratch" in sveglia_betty.py (TTS_SCRATCH_CONDIVISO).
if os.name == 'nt':
    OUTPUT_DIR = r"C:\Users\annag\OneDrive\Documenti\manfredo\audio_output"
else:
    OUTPUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                              "core", "tts_scratch_condiviso")
os.makedirs(OUTPUT_DIR, exist_ok=True)

async def synthesize_speech(text: str, voice: str, filepath: str):
    """Esegue la sintesi vocale tramite edge-tts in modo asincrono."""
    communicate = edge_tts.Communicate(text, voice)
    await communicate.save(filepath)

@mcp.tool()
def text_to_speech(text: str, filename: str = "lettura.mp3", voice: str = "it-IT-ElsaNeural") -> str:
    """
    Sintetizza un testo in un file audio MP3 di alta qualita (voce neurale Microsoft).
    :param text: Il testo o l'articolo da leggere a voce alta.
    :param filename: Nome del file MP3 di output (es. 'articolo_scienza.mp3').
    :param voice: La voce neurale da utilizzare. 
                  Voci Italiane ammesse:
                  - 'it-IT-ElsaNeural' (Femminile, molto naturale - default)
                  - 'it-IT-IsabellaNeural' (Femminile, chiara)
                  - 'it-IT-GiuseppeNeural' (Maschile, profonda ed espressiva)
                  - 'it-IT-DiegoNeural' (Maschile, standard)
    """
    try:
        # Sanifica il nome del file
        if not filename.endswith(".mp3"):
            filename += ".mp3"
        
        # Percorso completo del file
        filepath = os.path.join(OUTPUT_DIR, filename)
        
        # Esegue la sintesi asincrona in un loop sincrono
        asyncio.run(synthesize_speech(text, voice, filepath))
        
        # Genera il link assoluto del file per l'utente
        file_url = f"file:///{filepath.replace(os.sep, '/')}"
        
        return (
            f"🔊 Sintesi vocale completata con successo!\n\n"
            f"- **File salvato in**: [Apri file audio]({file_url})\n"
            f"- **Voce utilizzata**: `{voice}`\n"
            f"- **Lunghezza testo**: {len(text)} caratteri\n"
            f"- **Directory di output**: {OUTPUT_DIR}\n"
        )
    except Exception as e:
        return f"Errore durante la generazione dell'audio: {str(e)}"

@mcp.tool()
def list_voices() -> str:
    """
    Restituisce l'elenco delle voci neurali italiane supportate e consigliate per la sintesi vocale.
    """
    return (
        "🗣️ Voci Italiane Supportate (Microsoft Edge Neural):\n\n"
        "1. `it-IT-ElsaNeural` (Femminile - Consigliata per letture narrative, molto fluida ed espressiva)\n"
        "2. `it-IT-GiuseppeNeural` (Maschile - Consigliata per letture informative e documentari, tono caldo e profondo)\n"
        "3. `it-IT-IsabellaNeural` (Femminile - Tono chiaro e istituzionale, ottima per notizie)\n"
        "4. `it-IT-DiegoNeural` (Maschile - Standard, chiaro ed efficiente)\n"
    )

if __name__ == "__main__":
    mcp.run()
