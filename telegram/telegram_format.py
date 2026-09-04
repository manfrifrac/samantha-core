"""
telegram_format.py — piccolo helper condiviso per i cruscotti Telegram (DEV-017,
DEV-024, DEV-026 e successivi). Un solo posto per i pattern che altrimenti si
ridupplicano identici in ogni script:

1. PIN_MESSAGE+troncamento — bug reale già trovato una volta (DEV-017: contare i
   caratteri PRIMA di appendere [PIN_MESSAGE] fa sopravvivere il tag solo per
   margine, non per costruzione) e da non dover ritrovare una terza volta altrove.
2. Blocchi citazione espandibili (`>> `, AGENTS.md mobile-first) — per contenuto
   lungo che non deve essere tagliato ("mostra di più" invece di un "…" secco che
   perde informazione).
3. Tetto fisso per-campo (`cap_field`) — richiesta esplicita di Manfredo/Simone
   (07/08, via Leo) su DEV-026: il troncamento del MESSAGGIO INTERO perde sempre
   in silenzio gli ultimi item quando scatta, cambia solo quando. Cappare ogni
   nota/blocker a un tetto fisso PRIMA di comporre il messaggio rende il totale
   prevedibile per costruzione (somma di righe già cappate) invece che scoperto
   a runtime, e garantisce che OGNI item compaia sempre almeno con una riga
   breve — vedi `finalize_for_telegram` sotto per perché resta comunque un
   ultimo taglio di sicurezza, non alternativo a questo ma sempre più raro.
"""

PIN_TAG = "[PIN_MESSAGE]"
# NON è più il limite di un singolo messaggio Telegram (era 3900, margine sotto i
# 4096 reali) — scoperta fatta il 07/08 leggendo unified_telegram_engine.py: il
# motore spezza GIÀ da solo un recap troppo lungo in più messaggi consecutivi
# (`split_telegram_text`, per paragrafi) e applica il pin (edit-in-place se già
# pinnato, altrimenti send+pin) SOLO all'ultimo blocco. Troncare qui a 3900
# perdeva quindi informazione in silenzio per un limite che il motore sa già
# aggirare da solo — non c'è più motivo di farlo anche qui. Resta solo un
# tetto di buon senso, a difesa da un bug che generasse un payload patologico
# (non un limite Telegram reale).
TELEGRAM_SANITY_CEILING = 20000
FIELD_CAP_DEFAULT = 180


def cap_field(testo, max_len=FIELD_CAP_DEFAULT, fonte=None):
    """Taglia `testo` a `max_len` caratteri — MAI il messaggio intero, un
    singolo campo (nota/blocker), applicato PRIMA di comporre il messaggio
    finale così il totale è prevedibile per costruzione. Se tagliato e
    `fonte` è dato (es. il file sorgente di quell'item), il rimando sostituisce
    l'ultima parte del testo invece di limitarsi a un "…" cieco — chi vuole il
    dettaglio completo sa dove guardarlo."""
    testo = (testo or "").strip()
    if len(testo) <= max_len:
        return testo
    rimando = f"…vedi {fonte}" if fonte else "…"
    taglio = max_len - len(rimando)
    if taglio < 0:
        taglio = 0
    return testo[:taglio].rstrip() + rimando


def expandable_quote(testo):
    """Avvolge `testo` in un blocco citazione espandibile Telegram (`>> ` su ogni
    riga — l'engine lo converte poi nella sintassi nativa MarkdownV2 collassata,
    vedi unified_telegram_engine.to_telegram_markdown). Multi-riga: ogni riga del
    testo sorgente diventa una riga `>> ` propria, cosà da poter poi troncare in
    modo sicuro riga per riga (vedi finalize_for_telegram) senza spezzare una riga
    a metà e rompere il blocco. Tenuto per contenuti che si vogliono mostrare
    integrali su richiesta (non per il caso "nota lunga in una lista di item",
    ora coperto da `cap_field` — vedi sopra il perché del cambio)."""
    righe = testo.strip().splitlines() or [""]
    return "\n".join(f">> {r}" for r in righe)


_EMOJI_RANGES = (
    (0x1F300, 0x1FAFF),  # pittogrammi/emoticon/trasporti/simboli supplementari
    (0x2600, 0x27BF),    # simboli/dingbat storici (es. ✅ ❌ ⭐ prima di un eventuale FE0F)
    (0x2300, 0x23FF),    # miscellaneous technical: ⏸ ⏯ ⏹ ⏰ ⌚ — trovato mancante
                          # testando su enrico.md (⏸️ finiva bucketato in ⚪)
    (0x2B00, 0x2BFF),    # miscellaneous symbols/arrows: ⭐ ⬆️ ecc.
    (0xFE00, 0xFE0F),    # variation selector (es. ⏸️ = U+23F8 + U+FE0F)
    (0x200D, 0x200D),    # zero-width joiner (emoji composti)
)


def _e_emoji(token):
    """True se TUTTI i caratteri di `token` sono in un range Unicode emoji/
    simboli — distingue un vero status-emoji (🔴/🟡/⏸️/...) da un valore
    testuale libero nella cella Status (es. 'In corso', 'Proposto', '2/3').
    Bug reale trovato testando su >1 tracker (DEV-074): leo.md ha celle
    Status testuali, non solo emoji — senza questo controllo ognuna
    diventava una categoria a se' nel riepilogo ('In7', 'Proposto1', ecc.),
    enrico.md invece usa solo emoji quindi da solo non l'avrebbe rivelato."""
    if not token:
        return False
    return all(any(lo <= ord(ch) <= hi for lo, hi in _EMOJI_RANGES) for ch in token)


# DEV-074 (08/08/2026, design Manfredo/Simone via Leo): con 8+ studi x task
# ognuno il pin (aggregato di Simone, e — scope esteso lo stesso giorno — il
# pin del singolo coordinatore, es. Leo a 8540 caratteri) torna a essere un
# report invece di un indice, lo stesso problema che DEV-033 aveva gia'
# risolto per riga-singola ma non per NUMERO di righe. Riepilogo compatto (1
# riga per sezione, conteggio per emoji) + solo i 🔴 elencati per esteso sopra
# la piega + il dettaglio completo (tutte le righe, non solo le critiche)
# dentro un blocco `>> ` collassato sotto la piega — comprime, non taglia:
# nessuna riga sparisce, cambia solo quanto e' visibile senza toccare nulla.
STATUS_PRIORITY = ["🔴", "🟠", "🟡", "🟢", "🔵", "⚪"]


def build_index_section(nome_sezione, righe):
    """righe: lista di dict {'emoji': str, 'riga': str già formattata}.
    Ritorna la sezione completa (riepilogo + critiche + blocco collassato).
    Nessuna riga viene mai scartata: quelle non-critiche restano nel blocco
    `>> `, solo non ripetute per esteso sopra la piega. Il conteggio nel
    riepilogo usa SOLO emoji veri (_e_emoji) come categoria — un valore
    Status testuale libero (es. 'In corso', 'Proposto') finisce nel
    fallback ⚪ invece di diventare una categoria a se' stante (vedi
    _e_emoji per il bug reale che ha reso necessario questo controllo)."""
    if not righe:
        return f"*{nome_sezione}*: (nessun task aperto)"
    conteggio = {}
    for r in righe:
        categoria = r["emoji"] if _e_emoji(r["emoji"]) else "⚪"
        conteggio[categoria] = conteggio.get(categoria, 0) + 1
    ordine = sorted(conteggio, key=lambda e: STATUS_PRIORITY.index(e)
                    if e in STATUS_PRIORITY else len(STATUS_PRIORITY))
    riepilogo = " ".join(f"{e}{conteggio[e]}" for e in ordine)
    lines = [f"*{nome_sezione}*: {riepilogo}"]
    critici = [r["riga"] for r in righe if r["emoji"] == "🔴"]
    lines.extend(critici)
    dettaglio = "\n".join(r["riga"] for r in righe)
    lines.append(expandable_quote(dettaglio))
    return "\n".join(lines)


# F2 modello coordinatori (18/08/2026): sinonimo 'Stato' -> colonna Status
# (il tracker di romolo usa 'Stato' ed è per questo invisibile al parser).
# TEMPO 1: gate SPENTO — comportamento di produzione invariato finché Simone
# non conferma '9/9 parsabili' (~gio 20/08). In tempo 2 basta mettere True:
# è il punto unico che controlla il parser condiviso.
# TEMPO 2 ATTIVATO (18/08/2026, exec_deploy_guardiano): Simone ha dato VIA
# LIBERA — 10/10 tracker parsabili verificati uno per uno col parser. Il gate
# estende il controllo formato-unico a tutti i 10 coordinatori (alias 'Stato'
# + tolleranza colonne extra). Deploy verificato: 10/10 parse reale.
PARSER_ESTESO = True


def match_tracker_header(header_cells, richieste=("task", "status", "dipende da", "note")):
    """Riconosce un header di tracker per PRESENZA dei nomi colonna richiesti,
    non per ordine/posizione fissa — bug reale trovato mentre si lavorava a
    DEV-074: leo.md ha le colonne nell'ordine 'Task|Status|Dipende da|
    Telegram|Note' (Telegram PRIMA di Note), ma sia sync_gantt.py che
    update_dashboard.py si aspettavano un ordine fisso con Telegram DOPO Note
    — il tracker di Leo non veniva riconosciuto da NESSUNO dei due (0 righe
    aggregate, pin caduto sul flatten generico non-indice), causa diretta
    dell'8540 caratteri che ha fatto scoprire il problema. Ritorna un dict
    nome_colonna_lower -> indice se tutte le `richieste` sono presenti
    (case-insensitive), None altrimenti. 'id'/'telegram' non sono richieste
    ma vengono comunque mappate se presenti, in qualunque posizione."""
    lower = [c.strip().lower() for c in header_cells]
    if PARSER_ESTESO:
        # Sinonimo 'Stato' -> colonna Status (caso romolo). Solo col gate acceso:
        # a gate spento un header 'Stato' NON parsa, comportamento invariato.
        lower = ["status" if c == "stato" else c for c in lower]
    if not set(richieste).issubset(set(lower)):
        return None
    mapping = {}
    for nome in list(richieste) + ["id", "telegram"]:
        if nome in lower:
            mapping[nome] = lower.index(nome)
    return mapping


def finalize_for_telegram(content, limit=TELEGRAM_SANITY_CEILING):
    """Aggiunge [PIN_MESSAGE] in coda. Il motore Telegram spezza già da solo un
    recap più lungo di un singolo messaggio (vedi commento su
    TELEGRAM_SANITY_CEILING) — questa funzione non tronca più per il limite
    reale di Telegram, solo come rete di sicurezza contro un bug che generasse
    un payload patologicamente lungo (tetto molto più alto di qualunque
    contenuto legittimo atteso). Se anche quel tetto scattasse, il taglio
    avviene sull'ULTIMA RIGA INTERA che ci sta — mai a metà riga/carattere, per
    non spezzare a metà un blocco `>> ` lasciandolo incompleto."""
    content = content.strip() + f"\n\n{PIN_TAG}"
    if len(content) <= limit:
        return content

    trunc_msg = f"\n\n[... Contenuto anomalo, troncato per sicurezza ...]\n\n{PIN_TAG}"
    budget = limit - len(trunc_msg)
    righe = content.splitlines()
    tenute = []
    tot = 0
    for r in righe:
        costo = len(r) + 1  # +1 per il newline che la separa dalla precedente
        if tot + costo > budget:
            break
        tenute.append(r)
        tot += costo
    return "\n".join(tenute) + trunc_msg
