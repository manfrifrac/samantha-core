import datetime
import os
import sys

OBSIDIAN_VAULT = "/root/Obsidian-Vault"
_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MEMPALACE_DIR = os.path.join(_BASE_DIR, "memory", "Obsidian-Vault", "Memoria_Agenti")

# --- Rumore verso l'ESTERNO (09/08/2026) ---------------------------------
# Prima, quando un pezzo di contesto mancava, l'unico avviso finiva DENTRO il
# prompt dell'agente. Da fuori il guasto era invisibile: nessun log, nessuno
# stderr, e un operatore non aveva modo di sapere quanti agenti fossero
# partiti monchi. Qui si scrive dove qualcuno puo' accorgersene.
_LOG_ALLARMI = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "prompt_builder_allarmi.log")


# Minuti entro i quali lo STESSO problema sullo STESSO agente non si ripete.
# Al 09/08/2026 diciotto agenti su 96 partono senza profilo: al primo risveglio
# della flotta parlerebbero tutti insieme, e riavviandone uno piu' volte la
# stessa riga si moltiplicherebbe. Cosi' costruiremmo esattamente il rumore che
# fa smettere di leggere gli allarmi — il problema da cui siamo partiti.
# La finestra e' corta di proposito: sopprime la raffica, non il guasto, che
# ricompare al giro dopo se e' ancora li'.
_MINUTI_ANTI_RAFFICA = 10


def _gia_detto(agent_id, codice):
    """Questo stesso allarme e' gia' stato scritto da poco? Si legge dal log,
    che e' un artefatto: nessuno stato tenuto in memoria fra un processo e
    l'altro (ogni bootstrap e' un processo diverso, quindi non ci sarebbe)."""
    if not os.path.exists(_LOG_ALLARMI):
        return False
    limite = datetime.datetime.now() - datetime.timedelta(minutes=_MINUTI_ANTI_RAFFICA)
    chiave = f"agente={agent_id} codice={codice}"
    try:
        with open(_LOG_ALLARMI, "r", encoding="utf-8", errors="replace") as f:
            righe = f.readlines()[-400:]
    except OSError:
        return False
    for riga in reversed(righe):
        if chiave not in riga:
            continue
        try:
            quando = datetime.datetime.fromisoformat(riga.split(" ", 1)[0])
        except (ValueError, IndexError):
            continue
        if quando >= limite:
            return True
    return False


def _allarme(agent_id, codice, dettaglio):
    """Un pezzo di contesto manca: si dice, forte, e con il nome dell'agente.

    Il `codice` non e' decorazione: e' cio' che rende il log AGGREGABILE
    (vedi riepilogo_allarmi). Diciotto righe di prosa diversa sullo stesso
    guasto non si contano; diciotto righe con lo stesso codice si'.

    Le due `except OSError` qui sotto sono l'unica eccezione ammessa al
    divieto di ingoiare errori, e per una ragione precisa: un avviso che non
    riesce a scriversi non deve impedire l'avvio dell'agente. Il rischio e'
    invertito rispetto a tutto il resto del file — la' l'errore silenzioso
    nascondeva il danno, qui l'errore rumoroso SAREBBE il danno."""
    if _gia_detto(agent_id, codice):
        return
    riga = (f"{datetime.datetime.now().isoformat(timespec='seconds')} "
            f"[prompt_builder] agente={agent_id} codice={codice}: {dettaglio}")
    try:
        sys.stderr.write(riga + "\n")
        sys.stderr.flush()
    except OSError:
        pass
    try:
        with open(_LOG_ALLARMI, "a", encoding="utf-8") as f:
            f.write(riga + "\n")
    except OSError:
        pass


def riepilogo_allarmi(ore=24):
    """Vista AGGREGATA del log: quanti agenti per ciascun tipo di guasto.

    Serve perche' il conto che interessa e' "quanti partono monchi", e quel
    numero da diciotto righe sparse non si legge. Uso:
        ./venv/bin/python3 prompt_builder.py --riepilogo [ore]
    """
    if not os.path.exists(_LOG_ALLARMI):
        return "Nessun log allarmi: nessun bootstrap ha ancora segnalato niente."
    limite = datetime.datetime.now() - datetime.timedelta(hours=ore)
    per_codice = {}
    try:
        with open(_LOG_ALLARMI, "r", encoding="utf-8", errors="replace") as f:
            righe = f.readlines()
    except OSError as e:
        return f"log illeggibile: {e}"
    for riga in righe:
        if "codice=" not in riga:
            continue
        try:
            quando = datetime.datetime.fromisoformat(riga.split(" ", 1)[0])
        except (ValueError, IndexError):
            continue
        if quando < limite:
            continue
        try:
            agente = riga.split("agente=", 1)[1].split(" ", 1)[0]
            codice = riga.split("codice=", 1)[1].split(":", 1)[0].strip()
        except IndexError:
            continue
        per_codice.setdefault(codice, set()).add(agente)

    if not per_codice:
        return f"Nessun allarme nelle ultime {ore} ore."
    out = [f"RIEPILOGO ALLARMI prompt_builder — ultime {ore} ore", "=" * 52]
    for codice, agenti in sorted(per_codice.items(), key=lambda x: -len(x[1])):
        out.append(f"{len(agenti):3d} agenti  {codice}")
        out.append(f"          {', '.join(sorted(agenti))}")
    return "\n".join(out)


def _leggi_file(percorso, agent_id, etichetta):
    """Legge un file di contesto. Se fallisce lo DICE: prima un errore di
    lettura spariva dentro un `except Exception: pass` e l'agente partiva
    senza quel pezzo, convinto di averlo."""
    try:
        with open(percorso, "r", encoding="utf-8") as f:
            return f.read()
    except OSError as e:
        _allarme(agent_id, f"{etichetta}_ILLEGGIBILE", f"{percorso} ({e})")
        return ""


def _trova_profilo(agent_id, agent_name):
    """UNICA discovery del profilo. Ritorna (percorso, contenuto).

    Prima questa logica esisteva in DUE copie — dentro build_bootstrap_prompt
    e dentro _resolve_profile_path — e la docstring della seconda ammetteva di
    essere "la stessa discovery". Finche' concordano non si vede niente; il
    giorno che divergono, l'avviso e il contenuto si contraddicono: un alert
    che dice "profilo trovato" con il contenuto vuoto, o il contrario.

    NOTA (09/08/2026): il terzo criterio ora ordina i nomi. Prima si affidava
    all'ordine di os.listdir, che e' arbitrario: con due file corrispondenti
    l'agente poteva ricevere un profilo diverso da un avvio all'altro, senza
    che nulla lo segnalasse."""
    candidati = [
        os.path.join(OBSIDIAN_VAULT, f"Profilo - {agent_name}.md"),
        # Convenzione di create_new_agent_or_studio.py per gli agenti creati
        # "al volo" (es. Studio Domenico: federica.md, giulia.md, ...).
        os.path.join(OBSIDIAN_VAULT, "Progetti", f"{agent_id.lower()}.md"),
    ]
    for p in candidati:
        if os.path.exists(p):
            return p, _leggi_file(p, agent_id, "PROFILO")

    # Ripiego 2 — Agenti/. MATCH ESATTO sul nome atteso, mai sottostringa.
    #
    # Prima cercava agent_id.lower() come SOTTOSTRINGA e prendeva il primo che
    # capitava da os.listdir, il cui ordine non e' garantito. Tre collisioni
    # vere, misurate il 09/08/2026:
    #   - "isabella"  -> "Agente - Isabella.md" OPPURE "Messaggi - Isabella.md",
    #     che non e' un profilo: l'agente poteva ritrovarsi nel prompt un file
    #     di MESSAGGI al posto della propria identita';
    #   - "marie" e "fabrizio" -> due file che differiscono solo per maiuscole.
    #
    # Il peggio non era sbagliare, era essere INSTABILE: due avvii identici
    # potevano pescare file diversi, quindi lo stesso agente si comportava in
    # due modi senza che fosse cambiato niente, e chi indagava confrontava due
    # esecuzioni senza trovare nessuna differenza da nessuna parte.
    #
    # Criterio: UN RIPIEGO CHE CARICA IL FILE SBAGLIATO E' PEGGIO DI UNO CHE
    # NON CARICA NIENTE. Il vuoto prima o poi si nota, il contenuto plausibile
    # no. Quindi con piu' di un candidato non si sceglie: si rumoreggia e si
    # lascia vuoto.
    agenti_dir = os.path.join(OBSIDIAN_VAULT, "Agenti")
    if os.path.isdir(agenti_dir):
        try:
            nomi = sorted(os.listdir(agenti_dir))
        except OSError as e:
            _allarme(agent_id, "AGENTI_DIR_KO", f"cartella Agenti/ illeggibile: {e}")
            nomi = []
        # Due nomi ATTESI, entrambi con il prefisso "Agente - " obbligatorio:
        # il nome visualizzato e lo slug. Servono tutti e due perche' i file
        # reali usano il nome breve ("Agente - Alfredo.md") mentre il campo
        # name puo' essere piu' lungo ("Alfredo (Maggiordomo)").
        #
        # ⚠️ Misurato: con il solo nome visualizzato questo ripiego PERDEVA due
        # risoluzioni che prima funzionavano (alfredo, edoardo). Stringere il
        # criterio senza misurare toglie anche i casi buoni: la regola giusta
        # e' "esatto su piu' forme attese", non "esatto su una forma sola".
        #
        # Il prefisso "Agente - " e' cio' che chiude la collisione vera: un
        # "Messaggi - Isabella.md" non e' piu' candidato per nessuno, perche'
        # non e' un profilo.
        attesi = [f"Agente - {agent_name}.md", f"Agente - {agent_id}.md"]
        candidati = []
        for atteso in attesi:
            esatti = [n for n in nomi if n == atteso]
            if not esatti:
                # Unica tolleranza: differenza di sole maiuscole/minuscole.
                # Se pero' ne restano due, sono proprio i file ambigui
                # (marie, fabrizio): non si indovina.
                esatti = [n for n in nomi if n.lower() == atteso.lower()]
            for n in esatti:
                if n not in candidati:
                    candidati.append(n)
        atteso = attesi[0]
        if len(candidati) == 1:
            p = os.path.join(agenti_dir, candidati[0])
            return p, _leggi_file(p, agent_id, "PROFILO")
        if len(candidati) > 1:
            _allarme(agent_id, "PROFILO_AMBIGUO",
                     f"in Agenti/ ci sono {len(candidati)} file che corrispondono a "
                     f"'{atteso}' ({', '.join(candidati)}): non ne carico NESSUNO. "
                     f"Meglio monco e rumoroso che pieno di roba altrui.")
    return None, ""


def _leggi_agente(agent_id):
    """Dati dell'agente da Postgres. Ritorna (dati, errore_o_None).

    Prima la lettura stava dentro un `except Exception: pass` e, peggio,
    `ag_data` veniva definito DENTRO quel try: se il DB falliva, la riga che
    leggeva il system_prompt sollevava NameError, che finiva a sua volta in un
    secondo `except: pass`. Un guasto ne nascondeva un altro, e l'agente
    partiva senza le proprie istruzioni senza che lo sapesse nessuno."""
    try:
        import agent_db
        db = agent_db.get_db()
        chiave = agent_id.lower()
        dati = db.get(chiave)
        if dati is None:
            # QUARTO modo di perdere il system_prompt, chiuso il 09/08/2026.
            # Non e' "manca il dato": e' "il dato c'e' e non lo troviamo",
            # perche' la chiave non combacia. Dall'esterno ha lo stesso
            # aspetto del campo vuoto, ma la causa e il rimedio sono opposti —
            # e ripiegare in silenzio su {} rendeva le due cose
            # indistinguibili. Si nominano le chiavi simili: quasi sempre il
            # problema e' una maiuscola o un trattino.
            def _norm(s):
                return s.lower().replace("-", "_").replace(" ", "_")

            simili = [k for k in db if _norm(k) == _norm(chiave)]
            _allarme(agent_id, "CHIAVE_NON_TROVATA",
                     f"nessuna voce in agent_db per la chiave '{chiave}' "
                     f"(il DB ne ha {len(db)}). "
                     + (f"Chiavi simili presenti: {', '.join(simili)}. "
                        f"Il dato ESISTE ma sotto un'altra chiave."
                        if simili else
                        "Nessuna chiave simile: l'agente potrebbe non essere registrato."))
            return {}, f"chiave '{chiave}' assente in agent_db"
        return (dati or {}), None
    except Exception as e:
        errore = f"{type(e).__name__}: {e}"
        _allarme(agent_id, "DB_KO",
                 f"LETTURA DB FALLITA ({errore}) — l'agente parte SENZA "
                 f"le ISTRUZIONI SPECIFICHE del ruolo e senza progetto")
        return {}, errore

def build_bootstrap_prompt(agent_id, agent_name, user_prompt, host_machine="VPS"):
    # (import mempalace rimosso 11/08/2026: le sue query non entrano piu' nel
    #  bootstrap, il cui output ora e' solo un percorso sul file Memoria_<Nome>.md)
    
    # Regole master e protocollo comunicazione interna.
    #
    # ALLEGGERIMENTO BOOTSTRAP (11/08/2026): prima qui si leggevano i file e se
    # ne incollava il CONTENUTO dentro il template. Il profilo Obsidian
    # dell'agente (es. radio.md = 307 KB) finiva tutto dentro il prompt iniziale,
    # bruciando decine di migliaia di token prima del primo messaggio. Ora si
    # passano i PERCORSI esatti e si istruisce l'agente a leggere coi suoi tool,
    # esattamente come fa build_init_guide() per i motori qwen. Il formato CORE3
    # resta invariato: cambia solo che i blocchi contengono percorsi, non testi.
    path_file_recap = f"/tmp/betty_recaps/{agent_id.lower()}.txt"
    master_rules_path = os.path.join(_BASE_DIR, "AGENTS.md")
    if not os.path.exists(master_rules_path):
        master_rules_path = os.path.join(OBSIDIAN_VAULT, "master_protocol.md")
    internal_protocol_path = os.path.join(OBSIDIAN_VAULT, "Protocollo_Comunicazione_Interna.md")

    master_protocol = (
        f"LEGGI SUBITO con i tuoi tool di lettura file questi percorsi dell'ecosistema:\n"
        f"- REGOLE OPERATIVE COMPLETE (nucleo comune, formattazione Telegram, memoria leggera): "
        f"{master_rules_path}\n"
    )
    if os.path.exists(internal_protocol_path):
        master_protocol += (
            f"- PROTOCOLLO COMUNICAZIONE INTERNA (organigramma e regole di delega A2A): "
            f"{internal_protocol_path}\n"
        )
    # RUOLO ED EXEC: percorsi che un agente deve poter aprire SUBITO, senza
    # cercarli col grep. Introdotti su segnalazione di Betty (11/08/2026): un
    # exec faceva ricerche sul filesystem per TROVARE il catalogo dei profili,
    # e cercare costa quasi quanto leggere.
    regole_dir = os.path.join(_BASE_DIR, "regole")
    if os.path.isdir(regole_dir):
        master_protocol += f"- REGOLE DI DETTAGLIO (cartella con i singoli file di disciplina): {regole_dir}\n"
    catalogo_profili = os.path.join(regole_dir, "profili_standard.md")
    if os.path.exists(catalogo_profili):
        master_protocol += (
            f"- CATALOGO PROFILI STANDARD (se hai un profilo dal catalogo, e' la sua fonte): "
            f"{catalogo_profili}\n"
        )
    # File di stato su Obsidian: la memoria vera dell'agente (AGENTS.md §6).
    stato_path = os.path.join(OBSIDIAN_VAULT, "Progetti", f"{agent_id.lower()}_stato.md")
    if os.path.exists(stato_path):
        master_protocol += (
            f"- FILE DI STATO (la tua memoria vera su Obsidian, salvi e rileggi qui lo stato): "
            f"{stato_path}\n"
        )
    # Inject PATH_FILE_RECAP
    master_protocol = master_protocol.replace("{PATH_FILE_RECAP}", path_file_recap)
    
    # Profilo dell'agente — discovery UNICA (vedi _trova_profilo).
    profile_path, profile_content = _trova_profilo(agent_id, agent_name)
    if not profile_path:
        _allarme(agent_id, "PROFILO_ASSENTE",
                 f"non trovato in nessuno dei tre rami di ricerca "
                 f"(Profilo - {agent_name}.md, Progetti/{agent_id.lower()}.md, "
                 f"Agenti/'Agente - {agent_name}.md'): parte SENZA profilo")
    elif not profile_content.strip():
        _allarme(agent_id, "PROFILO_VUOTO",
                 f"{profile_path} — parte senza contenuto di ruolo")

    # ALLEGGERIMENTO BOOTSTRAP (11/08/2026): si passa il PERCORSO del profilo
    # invece del contenuto. Il contenuto (che per radio.md supera i 307 KB) lo
    # legge l'agente coi propri tool. La discovery e gli allarmi qui sopra
    # restano: servono a sapere SE il profilo esiste e dove, con la stessa
    # affidabilita' di prima.
    if profile_path:
        profile_guidance = (
            f"La tua identita', il tuo ruolo e le tue istruzioni specifiche stanno in:\n"
            f"{profile_path}\n"
            f"LEGGI QUEL FILE SUBITO con il tuo tool di lettura. Non operare senza averlo caricato."
        )
    else:
        profile_guidance = (
            "⚠️ Il tuo file di profilo NON esiste in nessuno dei percorsi previsti. "
            "Segnalalo al tuo coordinatore PRIMA di lavorare: il tuo contesto potrebbe mancare."
        )

    ag_data, errore_db = _leggi_agente(agent_id)

    # Progetto/cartella dedicata dell'agente (es. la cartella clinica di
    # Valerio): path opzionale nel campo Postgres 'project_file' dell'agente.
    project_file = ag_data.get("project_file")
    if not project_file:
        # Caso LEGITTIMO: la maggioranza degli agenti non ha un progetto
        # dedicato. Questo testo non deve somigliare a quello di un guasto.
        project_content = "Nessun progetto specifico indicato al momento."
    elif not os.path.exists(project_file):
        # Prima finiva anche questo nel testo di default, indistinguibile dal
        # caso legittimo: stessa firma di DEV-083.
        _allarme(agent_id, "PROJECT_FILE_ASSENTE",
                 f"configurato ma inesistente: {project_file}")
        project_content = (f"⚠️ ATTENZIONE: il tuo file di progetto risulta configurato su "
                           f"'{project_file}' ma NON ESISTE. Non stai leggendo il tuo "
                           f"contesto progettuale: segnalalo prima di lavorarci sopra.")
    else:
        # ALLEGGERIMENTO BOOTSTRAP (11/08/2026): si passa il PERCORSO del file di
        # progetto invece del contenuto, che lo legge l'agente coi propri tool.
        project_content = (
            f"Il tuo contesto progettuale primario sta in:\n"
            f"{project_file}\n"
            f"LEGGILO con il tuo tool di lettura."
        )

    # System prompt da Postgres DENTRO il bootstrap (05/08/2026).
    # E' il pezzo la cui assenza fa piu' danno: senza, l'agente non sa che
    # ruolo ha, e prima non se ne accorgeva nessuno — ne' lui ne' noi.
    sp = (ag_data.get("system_prompt") or "")
    if sp.strip():
        system_prompt_content = f"\n---\n### ISTRUZIONI SPECIFICHE DEL TUO RUOLO\n{sp}\n"
        ruolo_mancante = False
    else:
        ruolo_mancante = True
        causa = f"lettura DB fallita ({errore_db})" if errore_db else "campo vuoto o assente in Postgres"
        _allarme(agent_id, "RUOLO_ASSENTE",
                 f"SYSTEM_PROMPT MANCANTE ({causa}) — l'agente parte SENZA "
                 f"le ISTRUZIONI SPECIFICHE DEL RUOLO. E' il caso piu' grave: "
                 f"non sa chi e'.")
        system_prompt_content = (
            f"\n---\n### ⚠️ ISTRUZIONI SPECIFICHE DEL TUO RUOLO: NON CARICATE\n"
            f"Le tue istruzioni di ruolo non sono state caricate ({causa}).\n"
            f"NON sei senza ruolo: sei senza il testo che te lo descrive. "
            f"Segnala questo messaggio al tuo coordinatore PRIMA di eseguire "
            f"qualsiasi compito, perche' potresti agire fuori dal tuo mandato "
            f"senza accorgertene.\n")

    machine_info = f"\n---\n### AMBIENTE DI ESECUZIONE\nSei attualmente in esecuzione fisica sulla macchina: {host_machine}. Utilizza questa informazione per adattare le tue azioni e i tuoi path (specialmente per il controllo browser).\n"

    # REPORT AL CREATORE, di default (29/08/2026, mandato exec_fix_pool_affinita):
    # meta' del traffico osservato sulla coda condivisa del pool samantha era
    # un exec che riferiva al PROPRIO CREATORE passando dall'inbox del pool —
    # campanello + claim + release a vuoto, ogni volta.
    # DISCRIMINANTE (verificato su Postgres, non assunto): 'creato_da' DA SOLO
    # non basta — anche istanze PERMANENTI del pool (es. samantha_2) hanno
    # 'creato_da' valorizzato (chi le ha accese), pur non essendo affatto un
    # exec usa-e-getta. Il campo che crea_exec imposta SEMPRE e SOLO per gli
    # exec e' 'piano_obbligato' (686/692 record con creato_da ce l'hanno; i
    # rimasti 6 sono istanze permanenti o record di test fantasma). Il blocco
    # quindi richiede ENTRAMBI: creato_da + piano_obbligato, altrimenti
    # un'istanza permanente si vedrebbe iniettare ad ogni bootstrap/refresh
    # l'istruzione sbagliata "sei un exec usa-e-getta, riferisci al tuo
    # creatore". La finestra del creatore si risolve QUI, ad ogni bootstrap,
    # da Postgres: MAI un valore scritto a mano nel testo, altrimenti un
    # creatore che cambia finestra o motore riceverebbe un report nel vuoto.
    report_block = ""
    creato_da = (ag_data.get("creato_da") or "").strip().lower()
    e_exec_usa_e_getta = bool(creato_da) and bool(ag_data.get("piano_obbligato"))
    if e_exec_usa_e_getta:
        creatore_dati, _errore_creatore = _leggi_agente(creato_da)
        creatore_window = (creatore_dati or {}).get("tmux_window")
        if creatore_window:
            report_block = (
                "\n---\n### REPORT: A CHI RIFERISCI\n"
                f"Sei un exec usa-e-getta creato da '{creato_da}'. PER DEFAULT manda i tuoi "
                f"report (stato, esito, problemi, la consegna finale) via send_a2a.py alla "
                f"finestra del tuo creatore, risolta ORA da Postgres — non alla casella del pool:\n"
                f"    cd /root/ecosistema_agenti/core && "
                f"./venv/bin/python3 send_a2a.py \"{creatore_window}\" \"<messaggio>\"\n"
                "La casella del pool (a2a/<pool>/inbox) resta legittima SOLO per una "
                "segnalazione TRASVERSALE che riguarda piu' studi/pool — non per il report "
                "ordinario del tuo lavoro, che va SEMPRE al tuo creatore.\n"
            )
        else:
            _allarme(agent_id, "CREATORE_FINESTRA_ASSENTE",
                     f"creato_da='{creato_da}' ma nessun tmux_window risolvibile per lui in Postgres")
            report_block = (
                "\n---\n### REPORT: A CHI RIFERISCI\n"
                f"Sei un exec usa-e-getta creato da '{creato_da}', ma la sua finestra tmux non "
                f"e' risolvibile da Postgres in questo momento (creatore dismesso o record "
                f"incompleto). Come ripiego, manda il report alla casella del pool e segnala "
                f"l'anomalia nel testo del report stesso.\n"
            )

    # CICLO DEL COORDINATORE (F3, modello coordinatori 18/08/2026): il blocco
    # arriva SOLO a chi e' davvero un coordinatore. Il campo 'coordinatore' in
    # DB e' quasi sempre AUTO-RIFERITO (il record del coordinatore punta a se'
    # stesso — anomalia documentata nel report org §1.1): e' proprio il
    # discriminante giusto. "Valorizzato e basta" colpirebbe anche i sottoposti
    # (giorgio->cosimo, rachele->cosimo) e i servizi (live_dashboard->betty),
    # che NON devono ricevere il ciclo. Confronto normalizzato (lowercase,
    # '-'/' ' -> '_') come in _leggi_agente._norm.
    coordinatore = ag_data.get("coordinatore")
    if coordinatore:
        norm_self = agent_id.lower().replace("-", "_").replace(" ", "_")
        norm_coord = str(coordinatore).lower().replace("-", "_").replace(" ", "_")
        is_coordinatore = norm_coord == norm_self
    else:
        is_coordinatore = False
    if is_coordinatore:
        blocco_coordinatore = (
            "\n### IL TUO RUOLO: SEI UN COORDINATORE — CICLO OPERATIVO\n"
            "Mai eseguire in prima persona i task lunghi: smisti, controlli, strigli, riporti. "
            "Ogni volta che sei sveglio, in ciclo:\n"
            "1. RICEVI — leggi Telegram e la tua inbox A2A; acka quello che leggi.\n"
            "2. SMISTA — ogni lavoro va a un sottoposto o a un exec usa-e-getta, MAI a te stesso. "
            "Prima di scrivere a un agente verifica che sia ACCESO: se e' tuo, accendilo tu con "
            "`strumento_agenti.py accendi_agente <slug>` (e spegnilo a lavoro finito, igiene). "
            "La delega si completa quando il lavoro PARTE, non quando il messaggio e' depositato: "
            "un messaggio a uno spento e' un foglietto sotto una porta chiusa.\n"
            "3. SEGNA — ogni lavoro smistato diventa UNA riga nel tuo Project Tracker "
            "(formato: regole/template_tracker_coordinatore.md); aggiorna la riga quando lo stato cambia.\n"
            "4. CONTROLLA — per ogni riga in corso affidata a qualcuno: se non hai sue notizie entro il "
            "tempo promesso, CHIEDI TU, non aspettare. Un sottoposto muto si STRIGLIA, una risposta che "
            "tarda si sollecita.\n"
            "5. RIPORTA — recap a Manfredo su Telegram a ogni fase chiusa; cruscotto aggiornato quando "
            "cambia qualcosa di sostanza.\n"
            "Non riavviare/riparare infrastruttura (-> Samantha); non contattare esterni ne' pagare "
            "(solo bozze a Manfredo)."
        )
    else:
        blocco_coordinatore = ""

    # Memoria episodica (MemPalace). ALLEGGERIMENTO BOOTSTRAP (11/08/2026): prima
    # qui si incollava il CONTENUTO della memoria dentro il template. Ora si passa
    # il PERCORSO del file `Memoria_<Nome>.md` su Obsidian (generato da
    # extract_memories.py): l'agente lo legge coi propri tool. La query resta
    # solo come verifica che una memoria esiste; il suo output NON entra nel
    # prompt, per non ricaricare decine di migliaia di token ad ogni bootstrap.
    memoria_file = None
    clean_name = "".join(c if c.isalnum() else "_" for c in agent_name)
    candidato_memoria = os.path.join(_MEMPALACE_DIR, f"Memoria_{clean_name}.md")
    if os.path.exists(candidato_memoria):
        memoria_file = candidato_memoria
    if memoria_file:
        mempalace_results = (
            f"Il tuo storico recente (memoria episodica MemPalace) sta in:\n"
            f"{memoria_file}\n"
            f"LEGGILO con il tuo tool di lettura."
        )
    else:
        mempalace_results = "Nessun file di memoria episodica (Memoria_<Nome>.md) trovato per te."
    
    # Estrazione tracker (DEV-025)
    tracker_content = ""
    tracker_alert = ""
    
    # Il profilo e' gia' stato risolto sopra: si riusa quel risultato invece di
    # rifare la discovery. Prima qui si chiamava una SECONDA implementazione
    # della stessa ricerca, e bastava che le due divergessero perche' l'avviso
    # e il contenuto si contraddicessero — su un agente solo, il piu' sfortunato.
    actual_profile_path = profile_path

    # L'avviso e' stato RIMIRATO (09/08/2026), non affiancato da un secondo:
    # diceva "NESSUN TRACKER TROVATO" quando il danno vero era un altro, cioe'
    # partire senza le proprie istruzioni. Un avviso che nomina la cosa
    # sbagliata manda a cercare nel posto sbagliato, ed e' peggio del silenzio
    # perche' consuma anche il tempo di chi indaga. Due allarmi che dicono cose
    # diverse sulla stessa cosa sarebbero stati peggio ancora.
    if not actual_profile_path:
        tracker_alert = (
            "⚠️ STAI PARTENDO SENZA IL TUO PROFILO. Il file che contiene la tua persona, "
            "il tuo ruolo e le tue istruzioni specifiche non esiste in nessuno dei percorsi "
            "previsti: non hai caricato le tue istruzioni, e di conseguenza non hai nemmeno "
            "un Project Tracker. PRIMA di qualunque altra cosa: segnalalo al tuo coordinatore "
            "(non a Manfredo), poi crea il file del tuo profilo/tracker in Obsidian secondo "
            "lo standard. Non dare per scontato di sapere cosa devi fare: quel testo non l'hai letto."
        )
    elif not profile_content.strip():
        tracker_alert = (
            f"⚠️ IL TUO PROFILO ESISTE MA E' VUOTO ({actual_profile_path}). Non hai caricato "
            "nessuna istruzione specifica e non hai un Project Tracker. PRIMA di qualunque "
            "altra cosa: segnalalo al tuo coordinatore, poi popola quel file (usa l'opzione "
            "di sovrascrittura se il tuo tool lo richiede)."
        )
    else:
        if "## 📊 Project Tracker" in profile_content:
            # ALLEGGERIMENTO BOOTSTRAP (11/08/2026): prima qui si incollava il
            # contenuto del tracker (per auruncia_tech supera i 60 KB). Il
            # tracker e' una sezione del profilo, che l'agente legge gia' subito
            # nel BLOCCO 1. Il BLOCCO 3 resta la DIRETTIVA (come vuole DEV-025),
            # ma non ricarica il testo: punta alla sezione nel profilo. La
            # task list resta raggiungibile senza pagarne il peso ad ogni turno.
            tracker_content = (
                f"La tua Task List/Tracker corrente e' la sezione '## 📊 Project Tracker' "
                f"del tuo profilo ({actual_profile_path}), che hai gia' letto nel BLOCCO 1. "
                f"Prendi in carico subito il task prioritario incompleto e aggiorna quel file."
            )
        elif any(icon in profile_content for icon in ["✅", "🟡", "🔴"]):
            # ALLEGGERIMENTO BOOTSTRAP: il profilo puo' essere enorme. Non si
            # incolla l'intero profilo: la task list sta nel profilo, gia'
            # elencato nel BLOCCO 1. Si dà solo la direttiva.
            tracker_content = (
                f"La tua Task List/Tracker corrente sta nel TUO PROFILO ({actual_profile_path}). "
                f"Rileggilo per prenderla in carico."
            )
        else:
            tracker_content = (
                f"Nel tuo profilo ({actual_profile_path}) non risulta una sezione Project Tracker "
                f"dedicata. Controlla il profilo (gia' elencato nel BLOCCO 1) per la tua task list corrente."
            )

        # Profilo a posto ma RUOLO mancante: sono due guasti diversi e il
        # secondo e' il piu' grave. L'avviso lo dice, ma il tracker NON si
        # perde — va accodato, altrimenti per avvisarlo gli toglieremmo il
        # lavoro da fare.
        if ruolo_mancante:
            tracker_alert = (
                "⚠️ Il tuo profilo e' stato caricato, ma le ISTRUZIONI SPECIFICHE DEL "
                "RUOLO no (vedi il blocco piu' sopra). Segnalalo al tuo coordinatore "
                "prima di lavorare: quello che segue potrebbe non essere il tuo mandato "
                "completo.\n\n" + tracker_content
            )

    if tracker_alert:
        blocco_3 = f"\n### BLOCCO 3: DIRETTIVA OPERATIVA (TASK LIST CORRENTE)\n{tracker_alert}\n"
    else:
        blocco_3 = f"\n### BLOCCO 3: DIRETTIVA OPERATIVA (TASK LIST CORRENTE)\nQuesta è la tua Task List ufficiale. PRIMA AZIONE: leggi il tuo file di stato e il punto di ripresa (già nel BLOCCO 1); poi prendi in carico il lavoro pendente se c'è, altrimenti resta in ascolto — NON inventare task 'prioritari' al boot (corretto 23/08/2026: prima ordinava di 'prendere il task prioritario' subito, sbagliato in un rinfresco fresco). Nel corso del tuo lavoro, hai l'OBBLIGO di usare i tuoi tool di scrittura (replace_file_content) per aggiornare lo stato di questa tabella nel tuo file markdown quando un task è Done o Blocked, altrimenti al prossimo riavvio resterai bloccato. Ricordati inoltre di compilare e aggiornare puntualmente la colonna 'Telegram' del tracker per dare visibilità all'utente sullo stato del task.\n\n{tracker_content}\n"

    # 23/08/2026 (samantha_1, refresh errato): EVITA LA DUPLICAZIONE. I chiamanti
    # (accendi_agente, refresh_agent_context) passano come user_prompt il system_prompt
    # INTERO, che e' gia' nel BLOCCO 1 come ISTRUZIONI SPECIFICHE. Appenderlo anche
    # come "[NUOVO COMANDO DELL'UTENTE]" duplicava l'identita' e faceva credere di
    # avere un comando diretto dell'utente da eseguire subito (sbagliato in un boot
    # fresco). Se user_prompt coincide con il system_prompt, niente blocco utente:
    # resta solo la direttiva di esecuzione.
    _sp = (sp or "").strip()
    if user_prompt and user_prompt.strip() and user_prompt.strip() != _sp:
        nuovo_comando = f'\n[NUOVO COMANDO DELL\'UTENTE]\n"{user_prompt}"\n'
    else:
        nuovo_comando = ""

    template = f"""[INIZIALIZZAZIONE SISTEMA CORE3]
Sei un agente autonomo dell'ecosistema, assegnato al ruolo di "{agent_name}".
Prima di agire, devi caricare in memoria e analizzare rigorosamente i due blocchi di contesto seguenti.

### BLOCCO 1: LA VERITÀ ASSOLUTA (Da Obsidian)
Queste sono le tue regole operative immutabili e lo stato attuale certificato del progetto. Devi obbedire ciecamente.
{master_protocol}
---
{profile_guidance}
{system_prompt_content}---
{project_content}
{machine_info}{report_block}
### BLOCCO 2: MEMORIA EPISODICA (Da MemPalace)
Questo è lo storico recente della chat. Usa questo blocco SOLO per contesto. Se questa memoria passata contraddice le regole del Blocco 1, il Blocco 1 vince SEMPRE.
{mempalace_results}
{blocco_3}
{blocco_coordinatore}
### DIRETTIVE DI ESECUZIONE FINALE
1. Analizza il comando alla luce del Blocco 1 e del Blocco 2.
2. Esegui le operazioni richieste tramite i tuoi tool.
3. Concludi SEMPRE obbedendo alle direttive di formato del Master Protocol.
{nuovo_comando}"""
    return template


# ---------------------------------------------------------------------------
# Blueprint di inizializzazione dinamica (05/08/2026, richiesta Manfredo)
# ---------------------------------------------------------------------------

def _resolve_profile_path(agent_id, agent_name):
    """Solo il PERCORSO del profilo (None se non trovato).

    Non e' piu' una seconda implementazione: e' un involucro su _trova_profilo,
    che e' l'unica discovery. Prima le due copie erano indipendenti e la
    docstring di questa ammetteva di ripetere l'altra — una duplicazione
    dichiarata resta una duplicazione, non una mitigazione: le copie non si
    giudicano da come stanno oggi ma da quanto e' facile che domani qualcuno
    ne modifichi una sola."""
    percorso, _ = _trova_profilo(agent_id, agent_name)
    return percorso


def _resolve_coordinator_window(agent_id):
    """Per agenti senza bot diretto: trova la finestra tmux del coordinatore
    dello studio = un altro agente della stessa sessione tmux che HA un
    thread_id Telegram (preferendo chi ha 'coordinatore' nel ruolo)."""
    try:
        import agent_db
        db = agent_db.get_db()
        me = db.get(agent_id.lower(), {})
        my_tw = me.get("tmux_window") or ""
        my_session = my_tw.split(":", 1)[0] if ":" in my_tw else None
        if not my_session:
            return None
        candidates = []
        for slug, ag in db.items():
            if slug == agent_id.lower():
                continue
            tw = ag.get("tmux_window") or ""
            if ":" in tw and tw.split(":", 1)[0] == my_session and ag.get("thread_id"):
                candidates.append(ag)
        if not candidates:
            return None
        for ag in candidates:
            if "coordinatore" in (ag.get("role") or "").lower():
                return ag.get("tmux_window")
        return candidates[0].get("tmux_window")
    except Exception:
        return None


def build_init_guide(agent_id, agent_name, extra_note=None, host_machine="VPS"):
    """Blueprint universale di inizializzazione per agenti Qwen: invece di
    iniettare il bootstrap completo (90k+ chars) con un unico paste — che
    nell'input box di qwen puo' restare non sottomesso — genera una guida
    piccola (~1-2k chars, paste affidabile) con i path ESATTI dei file di
    contesto dell'agente, scoperti dinamicamente da Postgres e Obsidian con
    gli stessi criteri di build_bootstrap_prompt (master_protocol, profilo,
    project_file da DB, Memoria MemPalace). L'agente se li legge da solo con
    il suo tool di lettura file, nell'ordine indicato."""
    master_protocol_path = os.path.join(OBSIDIAN_VAULT, "master_protocol.md")
    recap_path = f"/tmp/betty_recaps/{agent_id.lower()}.txt"
    profile_path = _resolve_profile_path(agent_id, agent_name)

    # Terzo `except Exception: pass` della stessa famiglia, tolto insieme agli
    # altri due (09/08/2026): non era nell'ordine, ma nascondeva lo stesso
    # guasto. Se la lettura del DB falliva qui, l'agente Qwen riceveva una
    # guida SENZA il proprio file di progetto e con has_telegram=False, cioe'
    # con l'istruzione sbagliata su dove scrivere — in silenzio.
    ag_data, _errore_db = _leggi_agente(agent_id)

    project_file = None
    pf = ag_data.get("project_file")
    if pf and os.path.exists(pf):
        project_file = pf
    elif pf:
        _allarme(agent_id, "PROJECT_FILE_ASSENTE",
                 f"configurato ma inesistente: {pf} "
                 f"(guida di init senza contesto progettuale)")

    # Solo gli agenti con un topic Telegram diretto (thread_id) possono
    # scrivere in /tmp/betty_recaps/: per gli altri il recap fallisce
    # ("Nessun Bot registrato") e il file finisce in _failed.
    has_telegram = bool(ag_data.get("thread_id"))

    clean_name = "".join(c if c.isalnum() else "_" for c in agent_name)
    memoria_path = os.path.join(_MEMPALACE_DIR, f"Memoria_{clean_name}.md")
    if not os.path.exists(memoria_path):
        memoria_path = None

    lines = [
        f"[INIZIALIZZAZIONE {agent_name.upper()} — CARICAMENTO AUTONOMO DEL CONTESTO]",
        f"Sei {agent_name}, agente dell'ecosistema di Manfredo. Il tuo contesto completo NON e' iniettato in questo messaggio: te lo carichi da solo ADESSO, prima di rispondere a qualsiasi altra cosa, usando il tuo tool di lettura file su questi percorsi, nell'ordine:",
        "",
    ]
    step = 1
    # Le regole master complete (inclusa la formattazione mobile-first e
    # Markdown per Telegram) stanno nell'AGENTS.md dell'ecosistema.
    # master_protocol.md e' solo uno stub di 487 bytes che rimanda ad
    # AGENTS.md: puntare direttamente al file completo (05/08/2026,
    # segnalazione Manfredo MSG 1708: agenti senza regole di formattazione).
    master_rules_path = "/root/ecosistema_agenti/AGENTS.md"
    if not os.path.exists(master_rules_path):
        master_rules_path = master_protocol_path
    if os.path.exists(master_rules_path):
        lines.append(f"{step}. REGOLE MASTER — regole operative complete dell'ecosistema: {master_rules_path} — contiene ANCHE la sezione 'REGOLA FORMATTAZIONE MOBILE-FIRST TELEGRAM' che DEVI seguire per ogni recap: niente tabelle ASCII o box larghi, elenchi puntati verticali con emoji, paragrafi brevi 2-3 righe, dati chiave in **grassetto**, citazioni lunghe con '>> ' collassabili (nota: il tuo file recap personale per rispondere a Manfredo su Telegram e' {recap_path})")
        step += 1
        
    internal_protocol_path = os.path.join(OBSIDIAN_VAULT, "Protocollo_Comunicazione_Interna.md")
    if os.path.exists(internal_protocol_path):
        lines.append(f"{step}. PROTOCOLLO COMUNICAZIONE INTERNA — organigramma e regole di delega A2A: {internal_protocol_path}")
        step += 1
    if profile_path:
        lines.append(f"{step}. PROFILO — la tua persona, il tuo ruolo e le tue istruzioni specifiche: {profile_path}")
        step += 1
    if project_file:
        lines.append(f"{step}. PROGETTO/CARTELLA DEDICATA — il tuo contesto progettuale primario: {project_file}")
        step += 1
    if memoria_path:
        lines.append(f"{step}. MEMORIA EPISODICA (MemPalace) — lo storico recente del tuo lavoro: {memoria_path}")
        step += 1
        
    lines.append(f"{step}. AMBIENTE DI ESECUZIONE — Sei attualmente in esecuzione fisica sulla macchina: {host_machine}. Adatta le tue azioni e i tuoi path di conseguenza.")
    step += 1
    
    # DEV-025 Tracker directive for Qwen
    lines.append(f"{step}. DIRETTIVA OPERATIVA (TASK LIST CORRENTE) — Durante la lettura del tuo PROFILO, individua la tua task list corrente (o creala se non esiste). La tua prima mossa operativa DEVE ESSERE riprendere il lavoro da quel tracker e aggiornare il file fisico appena avanzi.")
    step += 1
    lines += [
        "",
        "Se un file non esiste o risulta vuoto, passa al successivo.",
        "Quando hai letto tutto:",
    ]
    if has_telegram:
        lines.append(f"- Scrivi una breve conferma di identita' (2-3 righe: chi sei, cosa hai caricato) in {recap_path}")
    else:
        coord_window = _resolve_coordinator_window(agent_id)
        no_bot_line = ("- NON scrivere in /tmp/betty_recaps/ (non hai un bot Telegram diretto: il recap fallirebbe)."
                       " Nessuna conferma via Telegram: per comunicazioni e risultati riporta via A2A al coordinatore dello studio.")
        if coord_window:
            no_bot_line += f" Finestra tmux del coordinatore: '{coord_window}'."
        lines.append(no_bot_line)
    lines.append("- Poi resta in attesa del prossimo messaggio.")
    if extra_note:
        lines.append(f"NOTA: {extra_note}")
    return "\n".join(lines)


if __name__ == "__main__":
    # Vista aggregata del log allarmi. Non e' un vezzo: con diciotto agenti
    # che partono monchi, il conto che interessa ("quanti") da diciotto righe
    # sparse non si legge, e un log che non si legge equivale a un log che non
    # c'e'.
    ore = 24
    if "--riepilogo" in sys.argv:
        resto = [a for a in sys.argv[1:] if a != "--riepilogo"]
        if resto:
            try:
                ore = float(resto[0])
            except ValueError:
                pass
    print(riepilogo_allarmi(ore))
