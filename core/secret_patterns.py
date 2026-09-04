"""
Fonte unica per "che aspetto ha un segreto" (DEV-017, 05/08/2026).

Questo modulo e' l'UNICA fonte delle espressioni regolari che riconoscono le
credenziali. Lo usano:
  - check_plaintext_tokens.py (scanner anti-ricomparsa su file + scrollback tmux)
  - il filtro logging MascheraSegreti (oscura i segreti PRIMA che arrivino al log)

NON duplicare queste espressioni altrove: se cambia "che aspetto ha un segreto",
si cambia SOLO qui.

Perche' un filtro logging e non basta mascherare i singoli punti di log:
l'API Telegram mette il token nell'URL (/bot<TOKEN>/...), quindi QUALUNQUE
eccezione che include l'URL (anche di librerie terze che non controlliamo) lo
stampa. Mascherare un solo punto non impedisce al prossimo di ripetersi; un
filtro sul logging applicato agli handler li copre tutti in un posto solo.

IMPORTANTE (trappola Python logging): un logging.Filter attaccato a un LOGGER
non filtra i record prodotti dai logger figli e propagati verso il root (la
propagazione consegna il record ai HANDLER del padre, saltando i suoi filtri).
Per coprire anche le librerie terze il filtro va aggiunto agli HANDLER del root
logger — vedi install_secret_masking().
"""
import logging
import re

# Lista di (famiglia, regex). Stesse famiglie dello scanner. Ordine irrilevante:
# vengono applicate tutte. Nota: telegram_api_hash (32 hex) e' volutamente larga
# (meglio over-maskare un hash legittimo nei log che lasciare sfuggire un api_hash).
SECRET_PATTERNS = [
    # NIENTE \b prima delle cifre: il token appare anche come /bot<TOKEN>/...
    # negli URL, dove non c'e' word-boundary tra "bot" e le cifre. (Stesso
    # pattern dello scanner, che infatti rilevava i token nei file E negli URL.)
    ("telegram_bot_token",
     re.compile(r"[0-9]{8,10}:[A-Za-z0-9_-]{35}")),
    ("telegram_api_hash",
     re.compile(r"\b[a-f0-9]{32}\b")),
    ("aws_access_key_id",
     re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    # Ancorato a inizio riga (MULTILINE cosi' vale sia nello scanner riga-per-riga
    # sia in mask_secrets che processa l'intero messaggio): una chiave privata vera
    # ha l'header da solo a inizio riga; la frase "-----BEGIN ... PRIVATE KEY-----"
    # scritta DENTRO un testo (es. un report che la cita) NON deve matchare.
    ("private_key_block",
     re.compile(r"^\s*-----BEGIN [A-Z ]*PRIVATE KEY-----", re.MULTILINE)),
    ("service_api_key",
     re.compile(r"\b(?:sk-[A-Za-z0-9_\-]{20,}|sk_[A-Za-z0-9]{20,}|"
                r"gsk_[A-Za-z0-9]{20,}|rpa_[A-Za-z0-9]{20,}|"
                r"AIza[0-9A-Za-z_\-]{35})")),
    # Token OAuth Google (accesso Gmail/GCP): prefisso ya29. + stringa base64url
    # lunga. sync_messaggi maneggia token Gmail: oggi il rischio e' basso (il
    # token passa via header Authorization, non nell'URL) ma coprirlo costa un
    # rigo e non coprirlo costa un account Gmail. Deciso da Betty il 05/08/2026.
    ("gmail_oauth_token",
     re.compile(r"ya29\.[A-Za-z0-9_-]{20,}")),
]


def mask_secrets(text):
    """Sostituisce qualunque cosa somigli a un segreto con <FAMIGLIA>.
    Ritorna il testo mascherato; se non e' una stringa lo ritorna invariato."""
    if not isinstance(text, str):
        return text
    for family, rx in SECRET_PATTERNS:
        text = rx.sub("<%s>" % family.upper(), text)
    return text


class MascheraSegreti(logging.Filter):
    """Filtro logging che oscura i segreti PRIMA che il record venga scritto.

    Strategia robusta: formatta il messaggio completo (msg % args), maschera il
    risultato, e lo rimette in record.msg azzerando record.args — cosi' qualunque
    handler/formatter standard (che usa record.getMessage()) scrive il testo gia'
    mascherato. Copre sia gli f-string (segreto gia' in msg) sia lo stile
    logger.error("...%s", url) (segreto in args)."""

    def filter(self, record):
        try:
            full = record.getMessage()
            masked = mask_secrets(full)
            if masked != full:
                record.msg = masked
                record.args = None
        except Exception:
            # Fallback: maschera almeno msg se getMessage() fallisce.
            try:
                if isinstance(record.msg, str):
                    record.msg = mask_secrets(record.msg)
            except Exception:
                pass
        return True


def install_secret_masking():
    """Attacca MascheraSegreti a tutti gli handler del root logger.

    Va chiamato DOPO logging.basicConfig (o comunque dopo che gli handler sono
    stati creati). Aggiunge il filtro agli HANDLER, non al logger: cosi' vengono
    mascherati anche i record prodotti da librerie terze e propagati al root.
    Idempotente: non aggiunge filtri duplicati se chiamato piu' volte."""
    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig()
    for h in root.handlers:
        if not any(isinstance(f, MascheraSegreti) for f in h.filters):
            h.addFilter(MascheraSegreti())
