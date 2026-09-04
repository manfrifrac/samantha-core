"""
Loader di segreti da ambiente o da core/.env (DEV-016, 05/08/2026).

Fonte unica delle credenziali: variabile d'ambiente, con fallback ai file
segreti di core/. Ordine di ricerca: env -> core/.env -> core/litellm_prod.env
(22/08/2026, exec_adapter_abbonamenti: LITELLM_MASTER_KEY e i token di
servizio dell'adapter vivono in litellm_prod.env, file 0600 root-only, e
devono essere visibili a secret_env prima che il provider litellm_proxy venga
attivato — vedi engine_adapter.CLAUDE_PROVIDERS). NESSUN segreto deve essere
hardcoded nei sorgenti. Uso:

    from secret_env import load_secret
    key = load_secret("GROQ_API_KEY")

Ritorna il valore (strippato) o None se non trovato. Non stampa mai il valore.
"""
import os

_CORE_DIR = os.path.dirname(os.path.abspath(__file__))

# File segreti secondari, letti in ordine se il nome non e' trovato prima.
# litellm_prod.env e' 0600 root-only (chiavi LITELLM_MASTER_KEY e token
# adapter): NON deve mai finire in .env, che ha ACL leggibile da dc-simone.
_SECRET_FILES = (".env", "litellm_prod.env")


def _leggi_da_file(path, name):
    """Ritorna il valore di `name` dal file `path` (righe VAR=valore) o None."""
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith(f"{name}="):
                    return line.split("=", 1)[1].strip()
    except Exception:
        pass
    return None


def load_secret(name):
    """Ritorna il valore del segreto `name` dall'ambiente o dai file segreti
    di core (vedi _SECRET_FILES), o None. Non stampa mai il valore."""
    val = os.environ.get(name)
    if val:
        return val.strip()
    for fname in _SECRET_FILES:
        val = _leggi_da_file(os.path.join(_CORE_DIR, fname), name)
        if val:
            return val
    return None
