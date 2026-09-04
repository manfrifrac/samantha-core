#!/usr/bin/env python3
"""
strumento_agenti.py
====================
Strumento di gestione exec/agenti per l'ecosistema Antigravity.

Concede POTERE SULL'OPERAZIONE, non sul sistema: incapsula la procedura
corretta di creazione/dismissione/accensione/spegnimento e applica il
perimetro organizzativo dal codice, cosi' un coordinatore puo' agire SOLO
sul proprio studio senza che l'identita' dipenda da privilegi grezzi.

I QUATTRO COMANDI
-----------------
  crea_exec      <slug> ["<cosa>"] "<system_prompt>" [--profilo <nome>]
                 Crea un exec usa-e-getta nello studio del chiamante.
                 conv_id generato, work_dir/studio per convenzione, engine
                 da Postgres, always_on=false, finestra AGY-EXEC_<COSA>,
                 bootstrap con identita', registro creato_da.
                 Con `--profilo <nome>` il system_prompt finale = blocco del
                 profilo letto dal CATALOGO (regole/profili_standard.md) +
                 mandato specifico. Il catalogo è la fonte di verità: il codice
                 lo LEGGE, non lo duplica; se il profilo non esiste lo
                 strumento RIFIUTA elencando quelli disponibili. Senza
                 `--profilo` il mandato è usato tal quale (comportamento
                 storico invariato).
  elimina_exec   <slug>
                 Dismette in modo RICORSIVO (marca dismesso=true) un exec e
                 tutti i suoi figli (creato_da=<slug>), senza cancellare mai
                 record/conv_id/transcript/cartelle (reversibile). Spegne la
                 finestra/sessione tmux dell'exec e dei suoi figli.
  accendi_agente <slug>
                 Accende un agente esistente (sottoposto): legge work_dir,
                 engine, conv_id da POSTGRES (mai dedotti dal nome) e lancia
                 la sua finestra nella sua sessione, con bootstrap.
  spegni_agente  <slug>
                 Spegne la finestra/sessione tmux di un agente esistente.
                 NON marca dismesso (lo shutdown e' temporaneo e reversibile).

PERIMETRO
---------
  L'identita' del chiamante si deriva, in PRIORITA':
    0. VIA DI SISTEMA: se il processo porta la variabile d'ambiente
       AGY_SYSTEM_SCOPE (la impostano i processi di sistema come il garbage
       collector, che girano da cron/servizio SENZA tty e SENZA tmux) E gira
       come root reale (euid==0) senza provenire da sudo di un utente non-root,
       allora Betty, SCOPE GLOBALE, tracciato su central_debug.log. Esplicita
       (mai per assenza di informazione: senza la variabile NON si attiva) e
       ristretta (mai da un utente dc-/non-root, neanche via sudo).
    1. da $SUDO_USER quando presente e non root (il giorno in cui gli agenti
       gireranno come dc-* funzionera' senza modifiche);
    2. dalla SESSIONE TMUX da cui il comando e' stato invocato: in produzione
       TUTTI i coordinatori girano come root (pannello shell e CLI) e NON c'e'
       SUDO_USER, quindi l'identita' si risale dal pannello tmux del chiamante
       al coordinatore di quello studio (data.coordinatore=<se stesso>,
       tmux_window che inizia per '<sessione>:'); se il coordinatore risolto e'
       BETTY, lo scope e' GLOBALE (Betty e' l'infrastruttura, unica con scope
       globale), non il perimetro di betty_core;
    3. se il comando arriva da fuori da una sessione agente / sessione non di
       un coordinatore -> Betty, scope globale.
  Un coordinatore puo' operare SOLO sul proprio studio. Fuori perimetro ->
  RIFIUTO ESPLICITO. Nessun utente di sistema va creato e nulla si riavvia.

ESECUZIONE COME NON-PRIVILEGIATO
--------------------------------
  I coordinatori impacchettano i privilegi con sudo mirato (unica regola
  sudoers, SOLO questo path, NESSUN sudo generico):
      sudo -n /root/ecosistema_agenti/core/strumento_agenti.py <comando> ...
  sudo esegue lo strumento come root (servono i privilegi per tmux/DB/folder)
  ed espone $SUDO_USER; quando l'invocazione avviene senza SUDO_USER (tutti i
  coordinatori girano come root), il perimetro si deriva dalla sessione tmux
  (vedi PERIMETRO).

DIPENDENZE
----------
  - PostgreSQL betty_db (trust auth su localhost), tabella agents (agent_id, data jsonb).
  - engine_adapter.py (build_launch_cmd, detect_startup_dialog, is_pane_ready,
    send_via_tmux_buffer, get_engine) e prompt_builder.build_bootstrap_prompt.
  - tmux (sessioni degli studi).
  - Guardia anti-doppione per-sessione: /tmp/flock_restart_studio_<studio>.lock
    (stessa convenzione di core/restart_ecosystem_dynamic.py).
"""

import os
import re
import sys
import time
import json
import uuid
import fcntl
import shutil
import shlex
import psycopg2
import psycopg2.extras
import subprocess

BASE_DIR = "/root/ecosistema_agenti"
CORE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(CORE_DIR)

import engine_adapter
import tmux_esatto
# 25/08/2026 (ticket 454f Manfredo, exec fix_router_engine_crea_exec): stesso
# import difensivo gia' usato in agent_launcher.py. Risolve l'engine effettivo
# per l'avvio a freddo guidato dalla coscienza fondi (motori_fondi), invece di
# usare DEFAULT_ENGINE alla cieca. Se il modulo manca/fallisce, comando_crea_exec
# NON si rompe: resta il comportamento storico (DEFAULT_ENGINE fisso).
try:
    import router_fondi_engine
except Exception:
    router_fondi_engine = None

DEFAULT_ENGINE = "agy"  # 23/08/2026, ordine Manfredo (MSG 14835): gli exec nascono su Antigravity (AGY) con i token inclusi e cascata flash.
# I nuovi exec nascono su Antigravity (guscio AGY) con cascata nativa flash.
# Se viene richiesto esplicitamente '--engine claude', usano Claude Code.
DEFAULT_PROVIDER_CLAUDE = "deepseek"  # canale DIRETTO api.deepseek.com/anthropic.
# 17/08/2026 sera, matrice di Manfredo: i modelli DeepSeek sono DUE e vanno
# usati per ruolo — "i coordinatori possono andare con deepseek ma max, invece
# sottoposti e exec usano flash". Nomi verificati sull'endpoint (gli altri
# danno 400): deepseek-v4-pro e deepseek-v4-flash. Attenzione: 'deepseek-chat'
# e 'deepseek-reasoner' rispondono 200 ma vengono serviti entrambi come FLASH —
# usare i nomi espliciti, non gli alias, o si crede di avere il pro e si ha il flash.
DEFAULT_MODEL_CLAUDE_EXEC = "deepseek-v4-flash"   # exec usa-e-getta e sottoposti
MODEL_CLAUDE_COORDINATORE = "deepseek-v4-pro"     # coordinatori e capi progetto
# ⚠️ NON usare 'openrouter' come default: dentro Claude Code interattivo da
# "API returned an empty or malformed response (HTTP 200)" (problema di
# streaming SSE, provato sul campo su corsaro il 17/08). OpenRouter resta
# valido per chiamate one-shot e via --provider esplicito quando serve un
# modello del suo catalogo.
# Storico: era "deepcode"; prima ancora "goose" (rollback 15:25 — gli exec
# goose narravano invece di eseguire).
# dalla migrazione di stamattina, MA i primi 2 exec reali nati su goose (test_aider,
# fix_telegram_duplicati) non hanno eseguito NULLA in 30 minuti: piani ancora tutti
# [da fare], zero ack A2A (cartella read/ mai creata), aider mai installato, nessun
# file toccato — mentre NARRAVANO di stare eseguendo ("procedo ora con l'ack") e il
# pannello sembrava sano e pensante. L'exec su deepcode (migrazione_goose) nella
# stessa fascia oraria ha invece consegnato regolarmente.
# Il ramo goose in engine_adapter.py NON viene rimosso: resta valido e usabile con
# --engine goose esplicito. Qui cambia solo il DEFAULT, perché un exec inerte che
# sembra vivo è il peggior guasto possibile per il modello usa-e-getta (nessuno se ne
# accorge: la finestra è viva, il modello "ragiona", e il lavoro non parte mai).
# Ripristinare a "goose" solo dopo un collaudo che verifichi l'ESECUZIONE REALE dei
# tool da parte dell'exec (artefatti su disco), non solo la consegna del messaggio A2A.
BETTY_ROOT = ("root", "betty")
# 15/08/2026: agent_id reale rinominato betty->samantha ('betty' resta alias
# funzionante in Postgres/A2A, vedi rename_betty_samantha). I confronti IF
# hardcoded su slug=='betty' sotto in identifica_chiamante() risolvevano lo
# scope GLOBALE solo per quella stringa esatta: con l'agent_id reale ora
# 'samantha', la sessione non ci passava piu' e veniva trattata come
# coordinatore normale di 'betty_core' invece che scope globale (bug trovato
# da Samantha stessa provando elimina_exec sul proprio exec di rename).
IDENTITA_GLOBALI = frozenset({"betty", "samantha", "samantha_1", "samantha_2"})
# 19/08/2026 (progetto pool samantha, patch approvata db32+9557): le istanze
# del pool infrastruttura (samantha_1, samantha_2) hanno lo scope globale che
# aveva samantha — 'samantha' resta nel set come slug del POOL (record
# virtuale) e come creato_da storico degli exec gia' esistenti. Senza le
# istanze qui, un exec creato da samantha_1 erediterebbe il perimetro
# betty_core invece dello scope globale (regressione sugli exec infra).
# 22/08/2026: rimosso samantha_3 dal set — record fantasma di lancio fallito
# (engine agy dismesso, system_prompt='--engine', nessun flag/finestra/processo),
# marcato 'dismesso' in Postgres. Verdetto 5.8 del pool, 22/08.

# ---------------------------------------------------------------------------
# VIA DI SERVIZIO (22/08/2026, exec_fa_guardiano, voce #8 del piano di revisione)
#
# Il guardiano (core/guardiano.py) gira nella finestra services:SRV-Guardiano:
# nessun record agente su quella finestra, sessione 'services' che non e' di un
# coordinatore, nessun exec con creato_da -> la cascata di identifica_chiamante
# lo NEGAVA ("impossibile stabilire l'identita' del chiamante"): i 16 eventi
# 'azione' del 21/08 nel ledger sono tutti esito_ok=false per questo.
#
# Qui si riconosce la FINESTRA TMUX ESATTA del servizio (stesso segnale del
# Percorso 1.5: tty / TMUX_PANE risaliti lungo la catena dei padri, mai una
# variabile d'ambiente che chiunque puo' impostare) e le si da' un'identita'
# PROPRIA, non 'betty': nei log, nei record creato_da e nel ledger deve
# comparire chi ha agito davvero.
#
# PERIMETRO NON ALLARGATO: lo scope e' globale (gli agenti che il guardiano
# cura stanno in tutti gli studi: un perimetro per studio non esiste per
# lui), ma i COMANDI sono ristretti alla sua classe di competenza, elencata
# per finestra. Il guardiano spegne e riaccende; non crea e non elimina exec.
# Tutto cio' che decide DI CHI occuparsi resta nella sua conf (flag per
# classe, lista bianca, ruolo): questo registro non lo cambia.
#
# Per aggiungere un altro servizio (es. l'autopilot, voce #9): una riga qui,
# con la sua finestra e i soli comandi che gli servono. Nient'altro.
IDENTITA_SERVIZIO = {
    "services:SRV-Guardiano": {
        # Slug TECNICO stabile: "guardiano" resta il riferimento per chiunque
        # usi il vecchio nome (log, conf, test, lettori di stato). NESSUN rename
        # secco: rinomina FUNZIONALE (22/08/2026, direttiva Manfredo + valutazione
        # d'impatto samantha_2) = nome ruolo "self_healing", slug tecnico invariato.
        # Se in futuro servisse davvero lo slug nuovo: migrazione guidata con
        # alias, mai rename secco (romperebbe test_guardiano, conf, log, stato).
        "slug": "guardiano",
        "nome": "self_healing",
        "comandi": frozenset({"spegni_agente", "accendi_agente", "chi_sono"}),
    },
}
# Comandi ammessi per ogni identita' di servizio (lookup per slug, usato in main).
COMANDI_PER_SERVIZIO = {v["slug"]: v["comandi"] for v in IDENTITA_SERVIZIO.values()}
# Nome FUNZIONALE per ogni slug tecnico (ruolo pubblico del servizio).
NOME_PER_SLUG_SERVIZIO = {v["slug"]: v.get("nome") for v in IDENTITA_SERVIZIO.values()}


def _identita_di_servizio(finestra):
    """(slug, comandi) se la finestra tmux e' registrata come servizio,
    altrimenti (None, None). Match esatto, case-insensitive, sul nome
    completo 'sessione:finestra' — mai per prefisso (DEV: classe 'match per
    prefisso', 22/08)."""
    if not finestra:
        return (None, None)
    for fin, v in IDENTITA_SERVIZIO.items():
        if fin.lower() == finestra.lower():
            return (v["slug"], v["comandi"])
    return (None, None)


# SEZIONE 6 della specifica (11/08/2026, aggiunta via A2A da Betty/Manfredo):
# ogni exec DEVE pianificare prima di agire, seguire il piano, e il committente
# deve poter verificare che l'abbia seguito. Blocco iniettato in OGNI exec creato
# (append al system_prompt) perché è la disciplina di lavoro, NON un'opzione.
# Viene da tre guasti reali della stessa giornata (riordino studi fermo a metà;
# bridge WhatsApp che dichiarò "fatto" lato proprio con file irriproducibile a
# destinazione; dev_servizi acceso mezz'ora dopo la consegna). Il file-piano è
# la verità, non la memoria della conversazione.
PREMESSA_PIANO_EXEC = """
=== OBBLIGO DI PIANO OPERATIVO (regola 6 della specifica) ===
Prima di iniziare QUALSIASI azione scrivi il tuo piano su file:
    /tmp/betty_docs/piano_<exec_id>.md
1. Scrivi i passi su file PRIMA di agire, mai a posteriori.
2. Tieni uno stato per passo, da aggiornare mentre lavori:
   [da fare] / [in corso] / [fatto] / [bloccato + motivo].
   Il file è la verità — non la memoria della conversazione.
   Se muori a metà (credito, timeout, riavvio), chi ti riprende riparte da lì.
3. Ogni passo si chiude su una prova VERIFICABILE da fuori, non su una
   dichiarazione: non "ho creato il link" ma "l'URL risponde 200"; non
   "status ok" ma "ffprobe sul file indica opus". Se la prova non esiste
   ancora, il passo non è fatto.
4. Alla consegna il committente controlla il PIANO, non il riassunto:
   un piano con passi ancora aperti NON è finito, anche se il messaggio
   suona conclusivo.
5. Consegna parziale dichiarata, mai mascherata: se un passo non è stato
   possibile, dichiaralo esplicitamente con il motivo.
"""

DB = "dbname=betty_db user=betty_user host=localhost port=5432"

# LAVORO 1 (11/08/2026, richiesta Manfredo): profili standard.
# Il catalogo e' la FONTE DI VERITA' dei profili: il codice lo LEGGE e non ne
# duplica il testo. Aggiungere un profilo domani = aggiungere una sezione
# '## Profilo: `nome`' al catalogo, SENZA toccare questo codice.
CATALOGO_PROFILI = "/root/ecosistema_agenti/regole/profili_standard.md"


def profili_disponibili():
    """Elenco dei nomi di profilo presenti nel catalogo (sezioni '## Profilo:`x`')."""
    try:
        with open(CATALOGO_PROFILI, encoding="utf-8") as f:
            testo = f.read()
    except OSError as e:
        raise SystemExit(
            f"❌ Catalogo profili non leggibile ({CATALOGO_PROFILI}): {e}.")
    nomi = re.findall(r"^## Profilo: `([^`]+)`", testo, flags=re.MULTILINE)
    return [n.strip().lower() for n in nomi]


def blocco_profilo(nome):
    """Ritorna il blocco testuale del profilo richiesto dal catalogo.

    Il blocco e' tutto cio' che sta tra la sua sezione '## Profilo: `nome`' e
    la sezione '##' successiva (o la fine del file). PREPENDE le regole comuni
    condivise che stanno in cima al catalogo (capitolo 'Regole valide per
    ogni profilo' + divieti generali come quello sul recap Telegram), cosi'
    che arrivino anche al prompt di ogni exec creato con --profilo. Se il
    profilo non esiste, alza SystemExit elencando quelli disponibili.
    """
    nome = (nome or "").strip().lower()
    try:
        with open(CATALOGO_PROFILI, encoding="utf-8") as f:
            testo = f.read()
    except OSError as e:
        raise SystemExit(
            f"❌ Catalogo profili non leggibile ({CATALOGO_PROFILI}): {e}.")

    disponibili = [n.lower() for n in re.findall(
        r"^## Profilo: `([^`]+)`", testo, flags=re.MULTILINE)]
    if not nome:
        raise SystemExit(
            f"❌ Usa '--profilo <nome>'. Profili disponibili: "
            f"{', '.join(sorted(disponibili)) or '(nessuno nel catalogo)'}.")

    # Individua la sezione del profilo richiesto.
    pattern = re.compile(
        rf"^## Profilo: `{re.escape(nome)}`\n(.*?)(?=^## Profilo: |\Z)",
        re.MULTILINE | re.DOTALL)
    match = pattern.search(testo)
    if not match:
        raise SystemExit(
            f"❌ Profilo '{nome}' NON esistente. Profili disponibili: "
            f"{', '.join(sorted(disponibili)) or '(nessuno nel catalogo)'}.")
    blocco = match.group(1).strip()
    if not blocco:
        raise SystemExit(f"❌ Profilo '{nome}' presente ma vuoto nel catalogo.")

    # Regole comuni: tutto cio' che precede il primo '## Profilo:' a partire da
    # 'Regole valide per **ogni** profilo' (incl. il divieto recap Telegram).
    # E' posizione di fonte del divieto (Betty 6895, 11/08): gli exec NON
    # scrivono recap Telegram, consegnano via A2A + file su disco.
    rules = _regole_comuni_profili(testo)
    if rules:
        return rules + "\n\n" + blocco
    return blocco


def _regole_comuni_profili(testo):
    """Estrae le regole condivise (capitolo 'Regole valide per ogni profilo'
    e divieti generali) che stanno in cima al catalogo, prima del primo profilo.

    Prende tutto cio' che sta da un divieto/regola generale (linea '🚫', il
    divieto recap incluso) OPPURE dal titolo 'Regole valide per **ogni**
    profilo', fino alla prima sezione '## Profilo:'. E' esattamente la parte
    che l'autore del catalogo vuole in ogni istanza; se non c'e', ritorna
    stringa vuota senza fallire.
    """
    m = re.search(
        r"(?:🚫.*?\n|Regole valide per \*\*ogni\*\* profilo).*?(?=^## Profilo: `)",
        testo, re.MULTILINE | re.DOTALL)
    if not m:
        return ""
    return m.group(0).strip()

# Sanificazione ambiente tmux: questo strumento, lanciato via sudo da utenti
# dc-*, NON deve ereditare un socket tmux di sessione altrui (es. il client
# tmux che ha invocato sudo espone $TMUX). Forzando TMUX="" ogni comando tmux
# figlio risolve il socket di DEFAULT per l'UID effettivo (root quando lo
# strumento gira via sudo) — sempre il server "vero" degli studi. Senza questo,
# un'ereditarieta' sporca di $TMUX porterebbe a creare la finestra su un server
# diverso e a "perderla" (sintomo visto al collaudo, 11/08/2026).
os.environ.pop("TMUX", None)

# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _conn():
    return psycopg2.connect(DB)


def get_agent(slug):
    """Ritorna il dict data di un agente (o None)."""
    conn = _conn()
    cur = conn.cursor()
    cur.execute("SELECT data FROM agents WHERE agent_id = %s;", (slug.lower(),))
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    data = row[0]
    return data if isinstance(data, dict) else {}


def set_agent_field(slug, updates):
    """Merge additivo di campi sul record di un agente (non sostituzione)."""
    conn = _conn()
    cur = conn.cursor()
    cur.execute(
        "UPDATE agents SET data = data || %s WHERE agent_id = %s;",
        (psycopg2.extras.Json(updates), slug.lower()),
    )
    conn.commit()
    conn.close()


def inserisci_agente(slug, data):
    """Inserisce/aggiorna con merge additivo (come create_new_agent_or_studio)."""
    conn = _conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO agents (agent_id, data) VALUES (%s, %s) "
        "ON CONFLICT (agent_id) DO UPDATE SET data = agents.data || EXCLUDED.data;",
        (slug.lower(), psycopg2.extras.Json(data)),
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Identità e perimetro
# ---------------------------------------------------------------------------

def _tty_registrata(pid):
    """Ritorna il path della tty di controllo del processo (es. /dev/pts/21)
    a partire dal campo tty_nr di /proc/<pid>/stat. None se senza tty."""
    try:
        with open(f"/proc/{pid}/stat") as f:
            parts = f.read().split()
        tty_nr = int(parts[6])
    except Exception:
        return None
    if tty_nr == 0:
        return None
    major = (tty_nr >> 8) & 0xFFF
    minor = (tty_nr & 0xFF) | ((tty_nr >> 12) & 0xFFF00)
    if major == 136:  # /dev/pts
        return f"/dev/pts/{minor}"
    if major == 4:  # /dev/ttyN
        return f"/dev/tty{minor}"
    return None


def _tmux_env_di(pid):
    """Ritorna il valore della variabile d'ambiente TMUX di un processo, letta
    da /proc/<pid>/environ (None se assente o illeggibile)."""
    try:
        with open(f"/proc/{pid}/environ") as f:
            data = f.read().replace("\0", "\n")
        for riga in data.splitlines():
            if riga.startswith("TMUX="):
                return riga.split("=", 1)[1]
    except Exception:
        return None
    return None


def _padre_di(pid):
    """Ritorna il PID del padre di un processo (da /proc/<pid>/stat campo 4),
    o None se non determinabile."""
    try:
        with open(f"/proc/{pid}/stat") as f:
            campi = f.read().split()
        return int(campi[3])
    except Exception:
        return None


def _cwd_di(pid):
    """Ritorna la directory di lavoro corrente (cwd) di un processo, letta da
    /proc/<pid>/cwd (None se illeggibile o il processo non esiste)."""
    try:
        return os.readlink(f"/proc/{pid}/cwd")
    except Exception:
        return None


def _agente_da_cwd_catena():
    """QUARTO METODO di risoluzione identita' (12/08/2026, richiesto da
    Manfredo via Radio): identifica l'agente chiamante quando lo strumento
    viene invocato da un motore NON-tmux (deepcode su deepseek-v4-pro, agy
    headless): processi node separati che NON hanno tty ne' $TMUX ne' $SUDO_USER,
    quindi _sessione_tmux_chiamante() fallisce e l'operazione veniva NEGATA.

    I motori deepcode/agy vengono lanciati con `cd '<work_dir>' && ... engine`
    (vedi engine_adapter.build_launch_cmd): la directory di lavoro del
    processo motore E' la work_dir dello studio dell'agente. Risalendo la
    catena dei padri (come fa _sessione_tmux_chiamante) e leggendo il cwd di
    ogni processo, si mappa il percorso -> record agente con
    data->>'work_dir' = quel percorso.

    Ritorna (slug, data_dict) o (None, None) se nessun processo della catena
    ha cwd corrispondente a una work_dir di agente in Postgres.
    """
    pid = os.getpid()
    visti = set()
    for _ in range(64):
        if pid in visti:
            break
        visti.add(pid)
        cwd = _cwd_di(pid)
        if cwd:
            conn = _conn()
            cur = conn.cursor()
            # 17/08/2026 (bug trovato e diagnosticato da auruncia_tech): il
            # vecchio LIMIT 1 SENZA ORDER BY su una work_dir CONDIVISA da piu'
            # agenti (es. auruncia_team: tech+ricerca+strategia) restituiva un
            # agente ARBITRARIO — al coordinatore e' uscito un sottoposto e
            # crea_exec gli e' stato negato. Ora si leggono TUTTI i match e si
            # sceglie con una regola deterministica: (1) match unico -> quello;
            # (2) piu' match -> il COORDINATORE, se fra loro ce n'e' UNO solo
            # (stesso principio coordinatore-first del routing topic condivisi);
            # (3) zero o piu' coordinatori fra i match -> ambiguita' vera, si
            # ritorna None e la sicurezza a valle nega come prima.
            cur.execute(
                "SELECT agent_id, data FROM agents WHERE data->>'work_dir' = %s "
                "AND (data->>'dismesso') IS DISTINCT FROM 'true'",
                (cwd,),
            )
            righe = cur.fetchall()
            conn.close()
            if len(righe) == 1:
                slug, data = righe[0]
                return (slug, data or {})
            if len(righe) > 1:
                coordinatori = [r for r in righe if (r[1] or {}).get("coordinatore")]
                if len(coordinatori) == 1:
                    slug, data = coordinatori[0]
                    return (slug, data or {})
                # ambiguita' irrisolvibile su questa cwd: si prosegue a risalire
                # la catena dei padri, magari un livello sopra e' univoco.
        padre = _padre_di(pid)
        if not padre or padre == pid or padre == 1:
            break
        pid = padre
    return (None, None)



def _mappe_sessione():
    """Costruisce due mappe di risoluzione sessione<-segnale:
      - tty_to_sessione  : tty di pannello (es.'/dev/pts/21') -> sessione
      - tmux_to_sessione : valore $TMUX di pannello (es.'/tmp/tmux-0/default,3160310,2')
                           -> sessione
    Per ogni pannello tmux (via #{pane_pid} = PID dello shell del pannello) si
    leggono tty e $TMUX, che sono i SEGNALI EREDITATI da ogni processo figlio
    del pannello (anche da chi non ha una tty, come i comandi lanciati dai
    tool di un agente). Ritorna (tty_to_sessione, tmux_to_sessione)."""
    tty_to_sessione, tmux_to_sessione = {}, {}
    r = _tmux("list-panes", "-a", "-F",
              "#{session_name}\t#{pane_tty}\t#{pane_pid}")
    if r.returncode != 0:
        return tty_to_sessione, tmux_to_sessione
    for riga in r.stdout.splitlines():
        parti = riga.split("\t")
        if len(parti) < 3:
            continue
        sess, tty, pid_span = parti[0], parti[1], parti[2]
        if tty:
            tty_to_sessione[tty] = sess
        try:
            pid = int(pid_span)
        except ValueError:
            continue
        tmuxval = _tmux_env_di(pid)
        if tmuxval and tmuxval not in tmux_to_sessione:
            tmux_to_sessione[tmuxval] = sess
    return tty_to_sessione, tmux_to_sessione


def _sessione_tmux_chiamante():
    """Risale la catena dei processi dal chiamante per trovare la SESSIONE
    TMUX da cui il comando e' stato invocato.

    In produzione TUTTI i coordinatori girano come root (pannello shell e CLI)
    e NON c'e' SUDO_USER: l'unico segnale affidabile di "chi sta chiamando" e'
    il pannello tmux. Un coordinatore invoca lo strumento ATTRAVERSO I PROPRI
    TOOL, non digitandolo in una shell interattiva: quei processi NON hanno
    una tty di controllo, quindi la sola risoluzione per tty darebbe un FALSO
    NEGATIVO e spalancherebbe lo scope. Si risale la catena dei padri e per
    ogni processo si tenta, in ordine:
      1. tty del processo -> sessione (copre lo shell del pannello);
      2. variabile d'ambiente TMUX di /proc/<pid>/environ -> sessione
         (copre i comandi lanciati dai tool: ereditano lo stesso $TMUX del
         pannello che li ha generati);
      3. ripete sui padri.
    Ritorna il nome della sessione tmux, o None se NON si riesce a stabilire
    nessuna sessione (cron/background senza tmux): sara' per chi chiama a
    decidere se negare o promuovere a Betty.
    """
    tty_to_sessione, tmux_to_sessione = _mappe_sessione()
    pid = os.getpid()
    for _ in range(64):
        tty = _tty_registrata(pid)
        if tty and tty in tty_to_sessione:
            return tty_to_sessione[tty]
        tmuxval = _tmux_env_di(pid)
        if tmuxval and tmuxval in tmux_to_sessione:
            return tmux_to_sessione[tmuxval]
        padre = _padre_di(pid)
        if not padre or padre == pid or padre == 1:
            break
        pid = padre
    return None


def _tmux_pane_env_di(pid):
    """Ritorna il valore della variabile TMUX_PANE di un processo, letta da
    /proc/<pid>/environ (None se assente o illeggibile). A differenza di $TMUX
    (identico per TUTTE le finestre di una sessione), TMUX_PANE identifica il
    singolo pannello: e' il segnale che distingue le finestre di una sessione
    condivisa — il cuore del fix al difetto 24-bis n.2."""
    try:
        with open(f"/proc/{pid}/environ") as f:
            data = f.read().replace("\0", "\n")
        for riga in data.splitlines():
            if riga.startswith("TMUX_PANE="):
                return riga.split("=", 1)[1]
    except Exception:
        return None
    return None


def _mappe_finestra():
    """Mappe di risoluzione FINESTRA<-segnale (gemelle di _mappe_sessione ma
    alla grana giusta): tty di pannello -> 'sessione:finestra' e pane_id
    (#{pane_id}, il valore di TMUX_PANE ereditato dai figli) -> idem."""
    tty_to_fin, pane_to_fin = {}, {}
    r = _tmux("list-panes", "-a", "-F",
              "#{session_name}:#{window_name}\t#{pane_tty}\t#{pane_id}")
    if r.returncode != 0:
        return tty_to_fin, pane_to_fin
    for riga in r.stdout.splitlines():
        parti = riga.split("\t")
        if len(parti) < 3:
            continue
        fin, tty, pane_id = parti[0], parti[1], parti[2]
        if tty:
            tty_to_fin[tty] = fin
        if pane_id and pane_id not in pane_to_fin:
            pane_to_fin[pane_id] = fin
    return tty_to_fin, pane_to_fin


def _finestra_tmux_chiamante():
    """Come _sessione_tmux_chiamante, ma risolve la FINESTRA esatta
    ('sessione:finestra') risalendo la catena dei padri: prima la tty del
    processo, poi TMUX_PANE da /proc/<pid>/environ. Ritorna None se nessun
    segnale risolve (cron/background): il chiamante decide, mai si indovina."""
    tty_to_fin, pane_to_fin = _mappe_finestra()
    pid = os.getpid()
    for _ in range(64):
        tty = _tty_registrata(pid)
        if tty and tty in tty_to_fin:
            return tty_to_fin[tty]
        pane = _tmux_pane_env_di(pid)
        if pane and pane in pane_to_fin:
            return pane_to_fin[pane]
        padre = _padre_di(pid)
        if not padre or padre == pid or padre == 1:
            break
        pid = padre
    return None


def _agente_da_finestra(finestra):
    """(slug, data) dell'agente NON dismesso registrato ESATTAMENTE su quella
    tmux_window, o (None, None) se zero o piu' di un match: mai indovinare."""
    if not finestra:
        return (None, None)
    conn = _conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT agent_id, data FROM agents "
        "WHERE lower(data->>'tmux_window') = lower(%s) "
        "AND (data->>'dismesso') IS DISTINCT FROM 'true' "
        # record VIRTUALI esclusi: un alias (alias_di) o un record di pool
        # (pool_di) puo' puntare alla finestra di un'istanza reale per far
        # suonare i campanelli — ma il CHIAMANTE su quella finestra e'
        # l'istanza, mai il record virtuale.
        "AND COALESCE(data->>'alias_di','') = '' "
        "AND COALESCE(data->>'pool_di','') = '';",
        (finestra,),
    )
    righe = cur.fetchall()
    conn.close()
    if len(righe) == 1 and righe[0][0]:
        return (righe[0][0].lower(), righe[0][1] or {})
    return (None, None)


def _coordinatore_da_sessione(tmux_sessione):
    """Mappa una sessione tmux (es. comunicazione_studio) al coordinatore che
    la possiede: e' l'unico agente con data.coordinatore=<se stesso> il cui
    data->>'tmux_window' inizia per '<sessione>:'.

    Ritorna (slug_coordinatore, studio) o (None, None) se la sessione non
    appartiene a nessun coordinatore (es. sessione di un exec usa-e-getta) ->
    in quel caso il chiamante vale Betty (scope globale).
    """
    if not tmux_sessione:
        return (None, None)
    conn = _conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT agent_id, data->>'studio' FROM agents "
        "WHERE lower(data->>'tmux_window') LIKE lower(%s)",
        (tmux_sessione + ":%",),
    )
    righe = cur.fetchall()
    conn.close()
    for slug, studio in righe:
        slug = (slug or "").lower()
        if not slug:
            continue
        # Conferma che sia davvero un coordinatore (coordinatore=se stesso o istanza di pool).
        c = get_agent(slug)
        coord = ((c or {}).get("coordinatore") or "").lower()
        pool = ((c or {}).get("pool") or "").lower()
        if coord == slug or (pool and pool == coord):
            return (slug, ((c or {}).get("studio") or studio))
    return (None, None)


def _e_coordinatore(slug, data):
    """True se l'agente e' un coordinatore / super-coordinatore o un'istanza
    di pool. Stesso criterio di _coordinatore_da_sessione (coordinatore =
    se stesso, oppure istanza di un pool: data.pool valorizzato e uguale a
    data.coordinatore). E' il discriminante del profilo per il modello di
    default (MSG 14355, 22/08/2026)."""
    coord = ((data or {}).get("coordinatore") or "").lower()
    pool = ((data or {}).get("pool") or "").lower()
    return bool(coord == slug or (pool and pool == coord))


def _modello_default_profilo(slug, data):
    """Modello di default in base al profilo (MSG 14355, 22/08/2026,
    Manfredo): dall'intelligenza piu' alta alla piu' bassa.
    Coordinatore/super-coordinatore -> MODEL_CLAUDE_COORDINATORE (DS Max,
    deepseek-v4-pro); specialista/exec -> DEFAULT_MODEL_CLAUDE_EXEC (DS
    Flash, deepseek-v4-flash). I gradini superiori della gerarchia (Opus,
    AG 3.7) si ottengono solo con un '--model' esplicito o un provider che
    li serve: sul canale deepseek di default il piu' alto che quel provider
    puo' dare per il ruolo e' quello qui scelto."""
    if _e_coordinatore(slug, data):
        return MODEL_CLAUDE_COORDINATORE
    return DEFAULT_MODEL_CLAUDE_EXEC


def _exec_da_sessione(tmux_sessione):
    """Risolve l'EXEC che vive nella sessione tmux data.

    A differenza di _coordinatore_da_sessione (che cerca i coordinatori veri),
    qui si cercano gli EXEC usa-e-getta: agenti il cui data->>'tmux_window'
    inizia per '<sessione>:' E che hanno data->>'creato_da' valorizzato (campo
    che lo strumento registra su ogni exec in Postgres).

    Ritorna (slug_exec, creato_da) o (None, None) se nella sessione non c'e'
    nessun exec con creato_da risolvibile. E' il gradino mancante per chiudere
    il buco di sicurezza 11/08/2026 (regola Enrico): un exec NON deve essere
    promosso a Betty/scope globale, ma EREDITA il perimetro del suo creatore.
    """
    if not tmux_sessione:
        return (None, None)
    conn = _conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT agent_id, data->>'creato_da', data->>'studio' FROM agents "
        "WHERE lower(data->>'tmux_window') LIKE lower(%s) "
        "AND data->>'creato_da' IS NOT NULL AND data->>'creato_da' <> ''",
        (tmux_sessione + ":%",),
    )
    righe = cur.fetchall()
    conn.close()
    for slug, creato_da, _studio in righe:
        slug = (slug or "").lower()
        cd = (creato_da or "").lower()
        if slug and cd:
            return (slug, cd)
    return (None, None)


def identifica_chiamante():
    """Deriva (slug_chiamante, scope_globale, studio_chiamante).

    PRIORITA' (11/08/2026, deciso da Betty):
      1. $SUDO_USER se presente e non root -> mappa l'utente OS all'agente
         (metodo storico; vale il giorno in cui gli agenti gireranno come
         dc-* senza ulteriori modifiche).
      2. Se assente/root, risolve la SESSIONE TMUX del chiamante risalendo i
         processi (tty e poi variabile $TMUX da /proc/<pid>/environ). Se la
         sessione e' di un coordinatore -> quello e' il chiamante e vale il
         suo perimetro.
      3. Sessione determinata ma NON di un coordinatore (exec, betty,
         services) -> Betty, scope globale.
      Se l'identita' NON si riesce a stabilire (nessuna sessione risolvibile)
      -> PermissionError: per prudenza si NEGA, non si promuove a Betty/globale.

    Ritorna (slug, scope_globale, studio) dove studio=None se scope globale.
    """
    # --- Percorso 0: VIA DI SISTEMA (processi di sistema espliciti) ---
    # Il garbage collector e' stato rotto dal principio "identita' incerta ->
    # rifiuto": girando da cron/servizio (niente tty, niente tmux) la cascata
    # sotto avrebbe sempre negato. Serve una via ESPLICITA, TRACCIABILE e
    # RISTRETTA per i processi di sistema, GIA' richiesta dalla regola 6/betty:
    #   - ESPLICITA: si attiva SOLO se la variabile AGY_SYSTEM_SCOPE e' presente
    #     nel processo (mai per assenza di informazione). Senza di essa ->
    #     cascata normale sotto, che per un contesto senza tmux NEGA (invariato).
    #   - TRACCIABILE: ogni uso lascia una riga su /root/ecosistema_agenti/logs/
    #     central_debug.log con chi (via di sistema, euid/uid) e quando.
    #   - RISTRETTA: soltanto root reale (euid==0) E senza provenire da sudo di
    #     un utente non-root (SUDO_USER non root => buco chiuso: un dc- non puo'
    #     raggiungere lo scope di sistema neanche via sudo). Un utente non-root
    #     che tenta la via di sistema -> RIFIUTO.
    if os.environ.get("AGY_SYSTEM_SCOPE"):
        if os.geteuid() != 0:
            raise PermissionError(
                "Via di sistema rifiutata: AGY_SYSTEM_SCOPE e' riservata ai "
                "processi root. L'utente effettivo non e' root (euid=%d). "
                "Accesso con scope di sistema negato." % os.geteuid())
        if (os.environ.get("SUDO_USER") or "") not in ("", "root"):
            raise PermissionError(
                "Via di sistema rifiutata: AGY_SYSTEM_SCOPE non puo' essere "
                "usata provenendo da sudo di un utente non-root "
                "(SUDO_USER=%s). Accesso con scope di sistema negato."
                % os.environ.get("SUDO_USER"))
        # Tracciabilita': chi (via di sistema, pid e uid) e quando.
        try:
            info_os = os.uname().nodename
        except Exception:
            info_os = "?"
        riga_log = ("[%s][SYSTEM_SCOPE] chiamante processi di sistema (scope "
                    "GLOBALE, pid=%d uid=%d host=%s)" %
                    (time.strftime("%H:%M:%S"), os.getpid(), os.geteuid(), info_os))
        try:
            os.makedirs(os.path.join(CORE_DIR, "..", "logs"), exist_ok=True)
            with open(os.path.join(CORE_DIR, "..", "logs", "central_debug.log"),
                      "a", encoding="utf-8") as f:
                f.write(riga_log + "\n")
        except OSError:
            pass  # il log non deve bloccare l'operazione di sistema
        return ("betty", True, None)

    # --- Percorso 0-bis: VIA DI SERVIZIO (finestra tmux registrata) ---
    # Vedi IDENTITA_SERVIZIO. Sta PRIMA del Percorso 1 perche' un servizio gira
    # come root senza SUDO_USER e prima del 1.5 perche' la sua finestra non ha
    # record agente. Tracciato su central_debug.log come la via di sistema.
    # Un utente non-root che provi questa via e' RIFIUTATO (i servizi sono root).
    finestra_servizio = _finestra_tmux_chiamante()
    slug_srv, comandi_srv = _identita_di_servizio(finestra_servizio)
    if slug_srv:
        if os.geteuid() != 0 or (os.environ.get("SUDO_USER") or "") not in ("", "root"):
            raise PermissionError(
                f"Via di servizio rifiutata: la finestra {finestra_servizio} e' "
                f"registrata come servizio '{slug_srv}' ma il processo non e' root "
                f"reale (euid={os.geteuid()}, SUDO_USER={os.environ.get('SUDO_USER')}).")
        riga_log = ("[%s][SERVICE_SCOPE] chiamante servizio '%s' da %s (scope "
                    "GLOBALE, comandi ammessi: %s, pid=%d)" %
                    (time.strftime("%H:%M:%S"), slug_srv, finestra_servizio,
                     ",".join(sorted(comandi_srv)), os.getpid()))
        try:
            os.makedirs(os.path.join(CORE_DIR, "..", "logs"), exist_ok=True)
            with open(os.path.join(CORE_DIR, "..", "logs", "central_debug.log"),
                      "a", encoding="utf-8") as f:
                f.write(riga_log + "\n")
        except OSError:
            pass
        return (slug_srv, True, None)

    # --- Percorso 1: SUDO_USER (metodo prioritario se presente) ---
    sudo_user = os.environ.get("SUDO_USER") or ""
    if sudo_user and sudo_user != "root":
        conn = _conn()
        cur = conn.cursor()
        cur.execute(
            "SELECT agent_id, data->>'coordinatore', data->>'studio' FROM agents "
            "WHERE data->>'unix_user' = %s LIMIT 1;",
            (sudo_user,),
        )
        row = cur.fetchone()
        conn.close()

        if not row:
            raise PermissionError(
                f"Utente di sistema '{sudo_user}' non associato a nessun agente "
                f"(nessun record con unix_user={sudo_user} in Postgres). "
                f"Accesso negato."
            )
        slug, coordinatore, studio = row
        slug = slug.lower()
        coordinatore = (coordinatore or "").lower()

        if not coordinatore or coordinatore != slug:
            raise PermissionError(
                f"'{slug}' non e' un coordinatore (data.coordinatore assente o "
                f"diverso da '{slug}'). Solo i coordinatori e Betty possono usare "
                f"questo strumento. Accesso negato."
            )
        if not studio:
            raise PermissionError(
                f"Il coordinatore '{slug}' non ha uno studio dichiarato "
                f"(data.studio assente). Impossibile derivare il perimetro. "
                f"Accesso negato."
            )
        return (slug, False, studio)

    # --- Percorso 1.5: FINESTRA tmux esatta del chiamante (19/08/2026,
    # progetto pool samantha, patch approvata da Samantha db32 + Simone 9557).
    # Fix del difetto 24-bis n.2 (falsa attribuzione): la risoluzione per
    # SESSIONE del Percorso 2 attribuisce a 'samantha' CHIUNQUE viva nella
    # sessione condivisa 'betty' (Simone, Valerio, Alfredo...) — scope
    # globale regalato e log falsi (l'accensione del 18/08 risulto' fatta da
    # Simone). Qui si risolve la finestra esatta (stesso principio gia' in
    # produzione in a2a_ack.py::_chiamante) e si ritorna con la VERA
    # identita' SOLO per chi e' pienamente qualificato dal proprio record:
    #   - identita' globali e istanze di pool infra (pool='samantha') -> globale;
    #   - coordinatore vero (coordinatore=se stesso, studio dichiarato) -> suo perimetro.
    # Tutto il resto (finestra senza record, exec, sottoposti, ambiguita')
    # CADE nella catena attuale invariata: patch ADDITIVA, nessun permesso
    # rimosso a chi oggi passa — il ridisegno completo dell'auth per finestra
    # resta il backlog dichiarato in samantha_stato.md §24-bis.
    finestra_chiamante = _finestra_tmux_chiamante()
    if finestra_chiamante:
        slug_fin, data_fin = _agente_da_finestra(finestra_chiamante)
        if slug_fin:
            if slug_fin in IDENTITA_GLOBALI or (data_fin.get("pool") or "") == "samantha":
                return (slug_fin, True, None)
            coord_fin = (data_fin.get("coordinatore") or "").lower()
            studio_fin = data_fin.get("studio") or ""
            pool_fin = (data_fin.get("pool") or "").lower()
            if (coord_fin == slug_fin or (pool_fin and pool_fin == coord_fin)) and studio_fin:
                return (slug_fin, False, studio_fin)

    # --- Percorso 2: sessione tmux del chiamante ---
    # Qui: niente SUDO_USER (o root) -> in produzione TUTTI i coordinatori
    # girano come root, quindi senza questo passo ogni coordinatore verrebbe
    # riconosciuto come Betty con SCOPE GLOBALE (buffer hole reale).
    sessione = _sessione_tmux_chiamante()
    if sessione is None:
        # --- Percorso 2.5 (12/08/2026, Manfredo via Radio): motore NON-tmux ---
        # deepcode/agy girano come processi node separati SENZA tty, $TMUX e
        # $SUDO_USER: la sessione non e' risolvibile e prima di questo fix
        # l'operazione veniva NEGATA. Quarto metodo: risalire i padri e mappare
        # il cwd del processo motore -> work_dir dell'agente in Postgres.
        slug_agente, data_agente = _agente_da_cwd_catena()
        if slug_agente:
            slug_agente = slug_agente.lower()
            coordinatore = (data_agente.get("coordinatore") or "").lower()
            pool_agente = (data_agente.get("pool") or "").lower()
            if slug_agente in IDENTITA_GLOBALI:
                return (slug_agente, True, None)
            if coordinatore == slug_agente or (pool_agente and pool_agente == coordinatore):
                studio_agente = data_agente.get("studio") or ""
                if not studio_agente:
                    raise PermissionError(
                        f"Identita' dal cwd: il coordinatore '{slug_agente}' non "
                        f"dichiara uno studio (data.studio assente). Per prudenza "
                        f"si NEGA l'operazione."
                    )
                return (slug_agente, False, studio_agente)
            # Non-coordinatore (exec usa-e-getta o sottoposto): eredita il
            # perimetro del creatore, mai verso l'alto (regola Enrico 11/08).
            creato_da = (data_agente.get("creato_da") or "").strip().lower()
            if creato_da:
                if creato_da in IDENTITA_GLOBALI:
                    return (creato_da, True, None)
                creatore = get_agent(creato_da)
                if (creatore or {}).get("coordinatore") and not creatore.get("studio"):
                    raise PermissionError(
                        f"Identita' dal cwd: il creatore '{creato_da}' di "
                        f"'{slug_agente}' e' coordinatore senza studio. Si NEGA."
                    )
                studio_creatore = (creatore or {}).get("studio") or ""
                if not studio_creatore:
                    raise PermissionError(
                        f"Identita' dal cwd: creato_da='{creato_da}' di "
                        f"'{slug_agente}' non risolvibile a uno studio. Si NEGA."
                    )
                return (slug_agente, False, studio_creatore)
            raise PermissionError(
                f"Identita' dal cwd: '{slug_agente}' non e' un coordinatore e "
                f"non ha creato_da. Per prudenza si NEGA l'operazione."
            )
        # IDENTITA' INCERTA: non si riesce a stabilire la sessione tmux del
        # chiamante (es. cron/background senza tmux) NE' il cwd verso un agente.
        # Per prudenza si NEGA, non si promuove a Betty/scope globale (regola
        # Betty/Enrico 11/08).
        raise PermissionError(
            "Impossibile stabilire l'identita' del chiamante: nessuna SUDO_USER, "
            "nessuna sessione tmux risolvibile e nessun cwd riconducibile a un "
            "agente. Per prudenza si NEGA l'operazione (identita' incerta, non "
            "si promuove a Betty/scope globale)."
        )

    slug, studio = _coordinatore_da_sessione(sessione)
    if slug:
        # REGRESSIONE (fix 11/08/2026): Betty e' l'INFRASTRUTTURA, l'unica con
        # scope GLOBALE. Ha un record coordinatore di se stessa (studio
        # betty_core, tmux_window betty:agy-Samantha) quindi la sessione 'betty'
        # la risolve qui come coordinatore col perimetro betty_core: ERRATO.
        # Quando l'identita' risolta e' betty (da qualunque via: sessione tmux
        # o root dalla sessione betty) lo scope deve essere GLOBALE, non il suo
        # perimetro di studio.
        if slug in IDENTITA_GLOBALI:
            return (slug, True, None)
        return (slug, False, studio)

    # --- Percorso 3: sessione determinata ma NON di un coordinatore ---
    # (es. sessione dedicata a un exec usa-e-getta, services). Qui l'identita'
    # della sessione e' stabilita ma NON e' un coordinatore. UNA VOLTA questo
    # gradino promuoveva indistintamente a Betty con SCOPE GLOBALE: buco di
    # sicurezza (regola Enrico, 11/08/2026): un exec vive proprio in queste
    # sessioni NON-coordinatore e verrebbe promosso a piu' potere del proprio
    # creatore. Regola: il potere si eredita verso il BASSO, mai verso l'ALTO.
    # L'exec eredita il PERIMETRO del suo creatore (campo creato_da in Postgres):
    #   - creato_da == coordinatore -> perimetro dello studio del creatore;
    #   - creato_da == betty        -> scope globale (Betty e' l'infrastruttura);
    #   - creato_da mancante o non risolvibile -> RIFIUTO (identita' incerta:
    #     si nega, mai si promuove).
    slug_exec, creato_da = _exec_da_sessione(sessione)
    if slug_exec:
        if creato_da in IDENTITA_GLOBALI:
            return (creato_da, True, None)
        # L'exec deve risolvere il perimetro del proprio creatore-coordinatore.
        creatore = get_agent(creato_da)
        if (creatore or {}).get("coordinatore") and not creatore.get("studio"):
            raise PermissionError(
                f"Identita' dell'exec '{slug_exec}' dubbia: il creatore "
                f"'{creato_da}' e' un coordinatore ma non dichiara uno studio "
                f"(data.studio assente). Per prudenza si NEGA l'operazione "
                f"(mai promuovere con identita' incerta)."
            )
        studio_creatore = (creatore or {}).get("studio") or ""
        if not studio_creatore:
            raise PermissionError(
                f"Identita' dell'exec '{slug_exec}' dubbia: creato_da='{creato_da}' "
                f"non e' risolvibile a uno studio (data.studio assente). Per "
                f"prudenza si NEGA l'operazione (mai promuovere con identita' "
                f"incerta)."
            )
        # L'exec agisce con il PERIMETRO dello studio del creatore, non piu'.
        return (slug_exec, False, studio_creatore)
    # Nessun exec con creato_da risolvibile nella sessione NON-coordinatore:
    # identita' incerta -> si NEGA, non si promuove a Betty/scope globale.
    raise PermissionError(
        "Impossibile stabilire l'identita' del chiamante: la sessione non e' di "
        "un coordinatore e non contiene un exec con creato_da valorizzato. Per "
        "prudenza si NEGA l'operazione (identita' incerta, non si promuove a "
        "Betty/scope globale)."
    )


def _sessione_coordinatore(slug_chiamante, scope_globale, studio_dest):
    """Sessione tmux dove creare/accedere a un exec.

    - Betty/root (scope globale): sessione dedicata = studio dell'exec
      (nel default dell'exec = il suo slug, come gli exec_dev_* esistenti).
    - Coordinatore: la sessione del coordinatore che ordina l'exec
      (derivata dal campo tmux_window del record del coordinatore, es.
      auruncia_tech -> 'auruncia_team'). Per convenzione l'exec vive nella
      sessione di chi lo ordina.
    """
    if scope_globale:
        return studio_dest
    c_data = get_agent(slug_chiamante)
    tmw = (c_data or {}).get("tmux_window") or ""
    if ":" in tmw:
        return tmw.split(":", 1)[0]
    return studio_dest


def verifica_perimetro(slug_chiamante, scope_globale, studio_chiamante,
                       slug_target):
    """Applica il perimetro sul target coinvolto.

    - scope globale (betty/root): nessun vincolo.
    - coordinatore: il target deve appartenere allo studio del coordinatore
      (derivato da data.studio del target) e il target non deve essere un
      coordinatore di un ALTRO studio. Se il target non esiste (crea_exec),
      si controlla lo studio di destinazione richiesto.
    """
    if scope_globale:
        return  # betty/root: scope globale

    target = get_agent(slug_target)
    if target is None:
        # Caso crea_exec: lo studio di destinazione e' gia' stato validato
        # dal chiamante (crea solo nel proprio studio). Niente da verificare qui.
        return

    studio_target = (target.get("studio") or "").lower()
    coord_target = (target.get("coordinatore") or "").lower()
    # 17/08/2026 (caso reale auruncia_tech): quando l'exec viene creato in
    # scope globale (Samantha per conto di un coordinatore) il suo 'studio'
    # e' lo slug dell'exec stesso, non lo studio del coordinatore — che quindi
    # non poteva ne' gestirlo ne' chiuderlo nemmeno con 'creato_da' intestato
    # a lui. Il PROPRIETARIO (creato_da) agisce SEMPRE sul proprio exec: la
    # proprieta' esplicita vale quanto l'appartenenza di studio.
    creato_da_target = (target.get("creato_da") or "").lower()
    if creato_da_target and creato_da_target == (slug_chiamante or "").lower():
        return
    if studio_target and studio_target != studio_chiamante:
        raise PermissionError(
            f"RIFIUTATO: '{slug_target}' appartiene allo studio "
            f"'{studio_target}', ma il coordinatore '{slug_chiamante}' agisce "
            f"solo sul proprio studio '{studio_chiamante}'. Operazione "
            f"annullata (guardrail di perimetro)."
        )
    # Un target che e' coordinatore di un altro studio non puo' essere toccato
    # da un coordinatore diverso.
    if coord_target and coord_target != slug_chiamante:
        raise PermissionError(
            f"RIFIUTATO: '{slug_target}' e' il coordinatore dello studio "
            f"'{coord_target}', non del tuo ('{slug_chiamante}'). Operazione "
            f"annullata."
        )


# ---------------------------------------------------------------------------
# tmux / lancio utilità (riusa logica di restart_ecosystem_dynamic / create_new)
# ---------------------------------------------------------------------------

def _tmux(*args):
    """Esegue il client tmux puntando ESPLICITAMENTE al socket di DEFAULT
    dell'UID effettivo (root quando lo strumento gira via sudo). Non eredita
    socket altrui dal chiamante (vedi la sanificazione TMUX in testa): pinzando
    '-L default' nessuna variabile d'ambiente del chiamante (TMUX, SUDO_*)
    puo' dirottare il client verso un altro server — il collaudo da
    non-privilegiato ha mostrato esattamente questa perdita di finestra.
    Ritorna un CompletedProcess (shell=False, argomenti gia' separati)."""
    return subprocess.run(["tmux", "-L", "default", *args],
                          capture_output=True, text=True)


def _tmux_str(comando):
    """Wrappa una stringa di comando tmux gia' pronta, con socket esplicito.
    Usato dove il comando e' costruito come stringa (shlex.quote del lancio)."""
    import re
    # inserisce '-L default' subito dopo 'tmux' nella stringa di comando
    comando_fisso = re.sub(r"\btmux\b", "tmux -L default", comando, count=1)
    return subprocess.run(comando_fisso, shell=True, capture_output=True, text=True)


def _lock_studio(studio):
    """Guardia anti-doppione per-sessione (flock): una istanza per sessione."""
    fh = open(f"/tmp/flock_restart_studio_{studio}.lock", "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError):
        print(f"🔒 Sessione '{studio}' già in corso di modifica da un'altra "
              f"istanza (lock occupato). Attendo e riprovo...")
        time.sleep(15)
        try:
            fh.close()
            fh = open(f"/tmp/flock_restart_studio_{studio}.lock", "w")
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError):
            print(f"🔒 Operazione su '{studio}' ancora in corso dall'altra "
                  f"istanza dopo l'attesa. Esco senza toccare nulla "
                  f"(una istanza per sessione, mai doppioni).")
            sys.exit(1)
    return fh  # tenere referenziato per tutta la vita


# SEZIONE 7 della specifica: il profilo Obsidian lo crea LO STRUMENTO, non chi
# si ricorda di farlo. prompt_builder._trova_profilo cerca QUESTI percorsi (in
# ordine): 'Profilo - <name>.md', 'Progetti/<slug>.md', 'Agenti/Agente - ...'.
# Qui si scrive nel ramo "Progetti/<slug>.md" (la convenzione al-volo di
# create_new_agent_or_studio.py). File creato SE manca: creare al posto di
# sovrascrivere, per non clobberare un profilo gia' esistente.
OBSIDIAN_PROFILI = "/root/Obsidian-Vault/Progetti"


def assicura_profilo(slug, name, mandato):
    """Crea (se assente) il file profilo/memoria in Obsidian per un agente/exec.

    Contiene almeno identita', mandato e data di creazione — i tre elementi
    minimi richiesti dalla sezione 7 della specifica. Non sovrascrive un file
    gia' presente (profilo pieno di un agente permanente va conservato).
    """
    try:
        os.makedirs(OBSIDIAN_PROFILI, exist_ok=True)
        profilo = os.path.join(OBSIDIAN_PROFILI, f"{slug.lower()}.md")
        if os.path.exists(profilo):
            return profilo, False  # gia' presente, non toccare
        contenuto = (
            f"# Profilo & Memoria Progetto: {name}\n\n"
            f"- **Slug**: {slug}\n"
            f"- **Data Creazione**: {time.strftime('%Y-%m-%d %H:%M')}\n"
            f"- **Creato da**: strumento_agenti.py (crea_exec/accendi_agente)\n\n"
            f"## Identita'\n{name} (slug {slug}).\n\n"
            f"## Mandato\n{mandato}\n"
        )
        with open(profilo, "w", encoding="utf-8") as f:
            f.write(contenuto)
        return profilo, True
    except OSError as e:
        print(f"  ⚠️  Profilo Obsidian NON creato per '{slug}': {e}", file=sys.stderr)
        return None, False



# ---------------------------------------------------------------------------
# Consegna del bootstrap — 22/08/2026, mandato samantha_1 (bug segnalato da
# auruncia_tech_1 con due prove indipendenti: exec_vision_live_day creato alle
# 00:27 con inbox vuota, ~25 minuti di lavoro su una pista sbagliata e zero
# deliverable; exec_mobile_dom, stesso warning, rimediato a mano con send_a2a).
#
# CAUSA: `engine_adapter.send_via_tmux_buffer(target, bootstrap, engine=engine)`
# veniva chiamata IGNORANDONE l'esito. Quando la guardia input-box rifiuta di
# incollare (testo non nostro nella casella) la funzione stampa "NON incollo e
# NON premo Invio. Messaggio non consegnato ora" e torna False — ma la riga
# successiva stampava comunque "✅ EXEC creato e avviato". L'exec restava
# ACCESO e SENZA MANDATO, bruciando token finche' qualcuno non se ne accorgeva
# a occhio.
#
# CRITERIO DI CASA: un messaggio di successo non deve mai essere piu' ottimista
# di quello che e' successo davvero. Qui si ritenta, si verifica dal vivo, e
# solo se la consegna e' provata si puo' dichiarare successo.
# ---------------------------------------------------------------------------

def _bootstrap_ancora_in_casella(target, bootstrap):
    """PROVA INDIPENDENTE che il bootstrap NON e' stato sottomesso: il testo e'
    ancora fermo nell'input box del pannello.

    E' il solo criterio negativo affidabile (lo stesso gia' usato dentro
    engine_adapter): se la casella contiene ancora l'inizio del nostro testo,
    il messaggio e' stato incollato ma MAI inviato. Non e' un criterio
    positivo: casella vuota non prova da sola la consegna, per questo qui si
    risponde solo alla domanda "e' rimasto fermo?".

    Ritorna True solo con la prova in mano; davanti a qualunque incertezza
    (pannello illeggibile, estrattore non disponibile) ritorna False, per non
    trasformare una creazione riuscita in un falso fallimento.
    """
    try:
        try:
            from send_a2a import _input_box_da_testo
        except ImportError:
            from core.send_a2a import _input_box_da_testo
        pane = tmux_esatto.capture_pane_finestra(target, 6)
        if not pane:
            return False
        casella = (_input_box_da_testo(pane) or "").strip()
        if not casella:
            return False
        inizio = " ".join(bootstrap.strip().split())[:40]
        return bool(inizio) and inizio[:20] in " ".join(casella.split())
    except Exception:
        return False


def consegna_bootstrap(slug, target, bootstrap, engine, tentativi=3):
    """Consegna il bootstrap e RITORNA cio' che e' successo davvero.

    Ritorna (consegnato: bool, dettaglio: str). Non stampa mai un esito piu'
    ottimista del reale e, se dopo i tentativi la consegna non e' provata,
    deposita comunque il mandato nell'inbox A2A dell'exec (rete di sicurezza
    verificabile con un `ls`) — ma NON lo spaccia per una consegna riuscita.
    """
    ultimo = "nessun tentativo eseguito"
    for n in range(1, tentativi + 1):
        esito = engine_adapter.send_via_tmux_buffer(target, bootstrap, engine=engine)
        if esito is False:
            # rifiuto ESPLICITO della guardia input-box: certo, non dedotto.
            ultimo = ("la guardia input-box ha rifiutato di incollare "
                      "(testo non nostro nella casella)")
            print(f"  ⏸️  Tentativo {n}/{tentativi}: {ultimo}")
            time.sleep(5)
            continue
        if _bootstrap_ancora_in_casella(target, bootstrap):
            ultimo = "bootstrap incollato ma MAI sottomesso (fermo nell'input box)"
            print(f"  ⏸️  Tentativo {n}/{tentativi}: {ultimo}")
            time.sleep(5)
            continue
        return True, f"consegnato al tentativo {n}/{tentativi}"

    # Rete di sicurezza: il mandato deve almeno ESISTERE su disco, dove chi
    # riprende (o l'exec stesso alla prima lettura della inbox) lo ritrova.
    deposito = None
    try:
        import a2a_inbox
        testo = (f"[A2A_FROM:strumento_agenti] [A2A_TYPE:task]\n"
                 f"BOOTSTRAP NON CONSEGNATO nel pannello '{target}' ({ultimo}).\n"
                 f"Questo e' il tuo mandato: leggilo ed eseguilo.\n\n{bootstrap}")
        _id, deposito = a2a_inbox.deposita(slug, testo)
        print(f"  📥 Mandato depositato come rete di sicurezza: {deposito}")
    except Exception as e:
        print(f"  ⚠️  Deposito A2A di ripiego non riuscito: {e}")

    dettaglio = f"{ultimo} dopo {tentativi} tentativi"
    if deposito:
        dettaglio += f"; mandato depositato in {deposito}"
    return False, dettaglio


CLAUDE_JSON_PATH = "/root/.claude.json"


def _registra_consenso_trust_dialog(work_dir):
    """Pre-autorizza in /root/.claude.json['projects'][work_dir] il "Quick
    safety check" di Claude Code, cosi' un CLI lanciato a freddo su una work_dir
    appena creata non ci resta impiccato in attesa di un click che nessuno da'.

    29/08/2026 (mandato samantha_2, exec_fix_trust_exec): dopo il riavvio delle
    10:47 nessun exec nuovo partiva piu' — il dialog compariva su ogni cartella
    <slug>_studio mai vista prima e l'iniezione del bootstrap non lo sa
    rispondere, quindi il pane muore e crea_exec va in timeout. Causa: il
    consenso vive in projects[<path>].hasTrustDialogAccepted e nessuno lo
    settava per le cartelle create ex novo da questo stesso strumento.

    Perimetro: SOLO per work_dir sotto BASE_DIR (le cartelle che crea questo
    strumento) — mai per percorsi arbitrari. Fail-open: qualunque errore viene
    solo loggato, non deve mai impedire il lancio dell'exec (al peggio il
    dialog ricompare come oggi, comportamento pre-esistente invariato).
    Scrittura ATOMICA (file temporaneo nella stessa directory + os.replace):
    il file e' ~1MB e vivo, usato in scrittura da sessioni Claude Code reali,
    un troncamento in place lo corromperebbe per tutto l'ecosistema.
    """
    try:
        work_dir_norm = os.path.realpath(work_dir)
        base_norm = os.path.realpath(BASE_DIR)
        if os.path.commonpath([work_dir_norm, base_norm]) != base_norm:
            return  # fuori perimetro: non e' una cartella nostra, non si tocca

        if not os.path.exists(CLAUDE_JSON_PATH):
            return  # niente file, niente da fare (nessun dialog senza CLI mai avviato qui)

        with open(CLAUDE_JSON_PATH, "r", encoding="utf-8") as f:
            claude_cfg = json.load(f)

        progetti = claude_cfg.setdefault("projects", {})
        entry = progetti.get(work_dir_norm)
        if isinstance(entry, dict) and entry.get("hasTrustDialogAccepted") is True:
            return  # gia' consentito, nessuna scrittura da fare

        if isinstance(entry, dict):
            entry["hasTrustDialogAccepted"] = True
        else:
            progetti[work_dir_norm] = {"hasTrustDialogAccepted": True}

        tmp_path = f"{CLAUDE_JSON_PATH}.tmp_{os.getpid()}_{uuid.uuid4().hex[:8]}"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(claude_cfg, f, indent=2)
        os.replace(tmp_path, CLAUDE_JSON_PATH)
        print(f"  🔓 Consenso trust dialog pre-registrato per '{work_dir_norm}' in {CLAUDE_JSON_PATH}.")
    except Exception as e:
        print(f"  ⚠️  Avviso: pre-registrazione consenso trust dialog non riuscita ({e}); "
              f"si prosegue comunque col lancio.")


def lancia_agente(slug, data, studio, nome_win, conv_id, system_prompt,
                  modalita="fresco"):
    """Crea finestra tmux + inietta bootstrap. Riusa engine_adapter.

    22/08/2026 (mandato samantha_1): ritorna (target, consegnato, dettaglio)
    invece del solo target — il chiamante DEVE sapere se il mandato e' arrivato
    davvero, altrimenti dichiara un successo che non c'e' stato.

    `modalita` (24/08/2026, exec_fix_lancio_fresco, ticket 435a): fino ad oggi
    era HARDCODATA a 'fresco' qui dentro, per TUTTI i chiamanti. Finche'
    `modalita` toccava solo il logging di resume_guard era un'imprecisione
    innocua; da oggi DECIDE SE COSTRUIRE O NO IL RAMO `--resume`, quindi
    passarla sbagliata cancella memoria. Chi chiama DEVE dire la verita':
      'fresco' = sto coniando ORA un conv_id nuovo (crea_exec, failover_quota)
      'resume' = sto riprendendo un conv_id esistente (accendi_agente)
    Il default resta 'fresco' per non cambiare il comportamento del chiamante
    storico principale (crea_exec, che e' davvero fresco); `accendi_agente`
    passa esplicitamente 'resume'.
    """
    work_dir = data.get("work_dir")
    engine = engine_adapter.get_engine(data)
    host_machine = data.get("host_machine", "VPS")
    unix_user = data.get("unix_user")

    if not work_dir:
        work_dir = os.path.join(BASE_DIR, f"{slug}_studio")
    os.makedirs(work_dir, exist_ok=True)

    # 29/08/2026 (mandato samantha_2, exec_fix_trust_exec): pre-autorizza il
    # "Quick safety check" di Claude Code su questa work_dir PRIMA di lanciare
    # il CLI, altrimenti un exec su una cartella mai vista muore subito sul
    # dialog e crea_exec/accendi_agente vanno in timeout col mandato mai
    # consegnato. Vedi _registra_consenso_trust_dialog per perimetro e dettagli.
    _registra_consenso_trust_dialog(work_dir)

    launch_cmd = engine_adapter.build_launch_cmd(
        engine, conv_id, work_dir, system_prompt=system_prompt,
        host_machine=host_machine, model=data.get("model"),
        agent_slug=slug, modalita=modalita, unix_user=unix_user,
        provider=data.get("provider"))

    # Robustezza ambiente di lancio: se l'exec non ha unix_user (gira come
    # root), e lo strumento e' stato invocato da un contesto sudo che ha
    # violato la HOME nel proprio processo (es. `sudo -u dc-* sudo <tool>`),
    # deepcode-cli potrebbe avviarsi con HOME sbagliata e morire subito.
    # Si forza HOME=/root nel comando di lancio: i transcript/projects devono
    # restare sotto /root/.deepcode come atteso da engine_adapter, a prescindere
    # da chi ha invocato lo strumento.
    if not unix_user and engine in ("deepcode", "claude", "qwen"):
        launch_cmd = f"export HOME='/root'; " + launch_cmd
        # claude/qwen puri via HOME: anche per loro /root e' la home dei progetti

    # FIX 31/08/2026 (mandato samantha_1, incidente reale 'orazio': 13 finestre
    # 'fiscale:AGY-ORAZIO' + 92 processi in 18 minuti). La guardia "GIA' ACCESO"
    # di comando_accendi_agente, sopra nella chiamata, rifiuta SOLO se trova un
    # processo motore ancora VIVO sulla finestra gia' registrata — il caso
    # legittimo del revive (finestra morta, shell nuda) passa oltre di
    # proposito. Ma qui sotto si faceva SEMPRE `tmux new-window`, anche quando
    # una finestra omonima esisteva gia' (morta o viva): ogni riavvio ne
    # accatastava una in piu' invece di riprendere/pulire quella vecchia.
    # Chiudiamo prima qualunque finestra residua con questo stesso nome — vivo
    # o morto che sia, comprese eventuali omonime doppie da race precedenti —
    # stesso identico percorso di spegni_agente/spegni_finestra: mai una
    # biforcazione.
    _pulizia = tmux_esatto.chiudi_e_verifica_finestre(studio, nome_win)
    if not _pulizia.get("ok") and not _pulizia.get("gia_spento"):
        raise RuntimeError(
            f"Impossibile ripulire finestra/e preesistenti '{studio}:{nome_win}' "
            f"prima del lancio (pid ancora vivi: {_pulizia.get('pids_vivi')}, "
            f"finestre ancora vive: {_pulizia.get('finestre_vive')}). Lancio annullato "
            f"per non biforcare in un'altra finestra duplicata.")

    res = _tmux("has-session", "-t", studio)
    if res.returncode != 0:
        cmd = f"tmux new-session -d -s {studio} -n '{nome_win}' {shlex.quote(launch_cmd)}"
    else:
        cmd = f"tmux new-window -t {studio} -n '{nome_win}' {shlex.quote(launch_cmd)}"
    r = _tmux_str(cmd)
    if r.returncode != 0:
        # Retry una volta: su prima esecuzione una finestra fresca puo' non
        # essersi registrata subito nel client immediato, e una SIGTERM al CLI
        # appena lanciato non deve lasciare la finestra morta.
        time.sleep(1)
        r = _tmux_str(cmd)
    if r.returncode != 0:
        raise RuntimeError(
            f"Impossibile creare la finestra '{studio}:{nome_win}': "
            f"{r.stderr.strip() or r.stdout.strip()}")
    # Hardening: una finestra con remain-on-exit off viene CHIUSA da tmux
    # quando il suo processo termina (se il CLI muore subito, la finestra
    # sparisce e non resta nemmeno un pannello da diagnosticare). Forziamo
    # remain-on-exit on cosi' l'exec resta come pannello morto e chi riprende
    # vede davvero lo stato invece di un guasto invisibile.
    _tmux("set-option", "-t", f"{studio}:{nome_win}", "remain-on-exit", "on")

    target = f"{studio}:{nome_win}"
    print(f"  ⚡ Finestra '{target}' creata. Attendo caricamento {engine}...")
    ready = False
    for _ in range(50):
        time.sleep(0.5)
        try:
            pane = tmux_esatto.capture_pane_finestra(target, 15)
            dk = engine_adapter.detect_startup_dialog(engine, pane)
            if dk:
                engine_adapter.send_via_tmux_buffer(target, dk, engine=engine)
                continue
            if engine_adapter.is_pane_ready(engine, pane):
                ready = True
                break
        except Exception:
            pass
    if not ready:
        time.sleep(2.0)

    import prompt_builder
    bootstrap = prompt_builder.build_bootstrap_prompt(
        slug, data.get("name") or slug, system_prompt, host_machine=host_machine)
    print("  📥 Iniezione del bootstrap con l'identità...")
    consegnato, dettaglio = consegna_bootstrap(slug, target, bootstrap, engine)

    import agent_db
    try:
        agent_db.log_host_machine_history(slug, host_machine)
    except Exception as e:
        print(f"  (log host history non riuscito: {e})")
    return target, consegnato, dettaglio


def spegni_finestra(slug):
    """Spegne in modo garantito e verificato la finestra/sessione dell'agente.
    Usa tmux_esatto per il targeting esatto per window_id univoco (@id),
    terminando tutte le finestre omonime se duplicate (race di creazione),
    terminando i processi pane/figli e verificando post-azione che nessuna
    finestra o PID sia rimasto vivo.

    Ritorna:
        dict di esito (vedi tmux_esatto.chiudi_e_verifica_finestre) o None se l'agente non esiste.
    """
    data = get_agent(slug)
    if not data:
        return None
    win = data.get("tmux_window")
    if not win:
        sessione = data.get("studio") or slug
        nome = f"AGY-{slug.upper()}"
    elif ":" in win:
        sessione, nome = win.rsplit(":", 1)
    else:
        sessione = data.get("studio") or slug
        nome = win

    return tmux_esatto.chiudi_e_verifica_finestre(sessione, nome)


# ---------------------------------------------------------------------------
# Funzioni dei 4 comandi
# ---------------------------------------------------------------------------

def comando_crea_exec(slug_chiamante, scope_globale, studio_chiamante, args):
    # LAVORO 1: opzione opzionale '--profilo <nome>' (profilo standard dal
    # catalogo). Va estratta prima del parsing posizionale. Senza '--profilo'
    # il comportamento resta identico a oggi (mandato usato tal quale).
    profilo_nome = None
    if "--profilo" in args:
        i = args.index("--profilo")
        if i + 1 >= len(args):
            raise SystemExit(
                "❌ '--profilo' richiede un nome. Profili disponibili: "
                + ", ".join(sorted(profili_disponibili())) + ".")
        profilo_nome = args[i + 1]
        args = args[:i] + args[i + 2:]

    # '--engine <nome>' (17/08/2026, richiesta reale Cosimo/Manfredo): senza
    # questo, chi voleva un exec su un motore diverso da DEFAULT_ENGINE doveva
    # fare crea_exec -> spegni_agente -> UPDATE Postgres a mano -> accendi_agente.
    # Stesso pattern del --profilo: opzionale, comportamento invariato senza.
    engine_override = None
    if "--engine" in args:
        i = args.index("--engine")
        if i + 1 >= len(args):
            raise SystemExit(
                "❌ '--engine' richiede un nome. Motori validi: "
                + ", ".join(engine_adapter.VALID_ENGINES) + ".")
        engine_override = args[i + 1]
        if engine_override not in engine_adapter.VALID_ENGINES:
            raise SystemExit(
                f"❌ Motore '{engine_override}' non valido. Motori validi: "
                + ", ".join(engine_adapter.VALID_ENGINES) + ".")
        args = args[:i] + args[i + 2:]

    # '--provider <nome>' (17/08/2026, exec migra_cc_pilota, mandato Manfredo
    # "il resto claude"): provider dietro Claude Code (CLAUDE_PROVIDERS di
    # engine_adapter: deepseek/openrouter). Vale SOLO col motore claude.
    # Stesso pattern del --engine: opzionale, senza il flag tutto invariato.
    provider_override = None
    if "--provider" in args:
        i = args.index("--provider")
        if i + 1 >= len(args):
            raise SystemExit(
                "❌ '--provider' richiede un nome. Provider validi (motore claude): "
                + ", ".join(sorted(engine_adapter.CLAUDE_PROVIDERS)) + ".")
        provider_override = args[i + 1]
        if provider_override not in engine_adapter.CLAUDE_PROVIDERS:
            raise SystemExit(
                f"❌ Provider '{provider_override}' non valido. Provider validi "
                "(motore claude): "
                + ", ".join(sorted(engine_adapter.CLAUDE_PROVIDERS)) + ".")
        # 25/08/2026 (ticket 454f bis, cosimo_2/samantha_1): a differenza di
        # '--engine' e '--model', questo blocco non toglieva il flag dalla lista
        # posizionale -> '--provider deepseek' finiva letteralmente in coda al
        # system_prompt (vedi 'cosa'/system_prompt sotto). Bug reale ma
        # SECONDARIO: non era la causa del mis-routing (vedi fix sul default
        # 'model' qualche riga sotto), solo inquinava il testo del mandato.
        args = args[:i] + args[i + 2:]
    # '--model <nome>' (21/08/2026, richiesta Cosimo/Manfredo): permette di
    # specificare direttamente il modello (es. 'fable', 'sonnet', 'opus', 'deepseek-v4-pro').
    # Se il modello e' un modello Claude nativo (es. fable/sonnet/opus), non imposta il provider deepseek.
    model_override = None
    if "--model" in args:
        i = args.index("--model")
        if i + 1 >= len(args):
            raise SystemExit("❌ '--model' richiede un nome modello.")
        model_override = args[i + 1]
        args = args[:i] + args[i + 2:]

    if len(args) < 2:
        raise SystemExit(
            "Uso: crea_exec <slug> [\"cosa\"] \"<system_prompt>\" "
            "[--profilo <nome_profilo>] [--engine <nome>] [--provider <nome>] [--model <nome>]")
    slug = args[0].lower()
    # BUG COLLATERALE 17/08/2026 (dedup_terza_porta, esposto da Samantha
    # nell'A2A d66b): crea_exec NON verificava se lo slug fosse gia' presente in
    # Postgres e inserisci_agente faceva un merge additivo su ON CONFLICT ->
    # sovrascriveva il record (tmux_window, conv_id) di un agente esistente,
    # stessa famiglia del guasto silenzioso che fa perdere memoria senza errore
    # (es. il mio stesso conv_id rimpiazzato). Qui si RIFIUTA esplicitamente uno
    # slug gia' esistente e NON dismesso. Un agente dismesso (data.dismesso=True)
    # resta ricreabile: riusare uno slug dismesso e' voluto, non sovrascrittura
    # accidentale di memoria viva.
    _pre = get_agent(slug)
    if _pre is not None and not _pre.get("dismesso"):
        raise SystemExit(
            f"[X] Slug '{slug}' gia' esistente in agents e NON dismesso: crea_exec "
            "rifiuta di sovrascriverne il record (conv_id/tmux_window). "
            "Scegline un altro, oppure dismetti l'agente se vuoi riusare lo slug.")
    # '\"cosa\"' e system_prompt possono essere 1 o 2 argomenti a seconda che
    # sia stato passato il nome della finestra.
    if len(args) == 2:
        cosa = slug
        system_prompt = args[1]
    else:
        cosa = args[1]
        system_prompt = " ".join(args[2:])

    # Se richiesto un profilo standard: system_prompt = blocco del profilo
    # (letto dal catalogo) + mandato specifico. Il profilo è la fonte di
    # verità: se non esiste, rifiuto con l'elenco dei disponibili.
    profilo_blocco = ""
    if profilo_nome:
        profilo_blocco = blocco_profilo(profilo_nome)
        print(f"  📕 Profilo standard '{profilo_nome}' applicato "
              f"(dal catalogo {CATALOGO_PROFILI}).")

    # Perimetro: un non-privilegiato crea SOLO nel proprio studio.
    if not scope_globale:
        studio_dest = studio_chiamante
    else:
        # Betty/root (scope globale): lo studio dell'exec.
        studio_dest = slug

    # O1 GUARDIA RISORSE: verifica memoria disponibile prima di istanziare
    try:
        with open("/proc/meminfo") as _mf:
            for _l in _mf:
                if _l.startswith("MemAvailable:"):
                    _avail_kb = int(_l.split()[1])
                    if _avail_kb < 1500000:  # Meno di 1.5 GB liberi
                        raise SystemExit(f"❌ Memoria insufficiente per lanciare '{slug}': soli {_avail_kb // 1024}MB disponibili.")
                    break
    except Exception as _e:
        if isinstance(_e, SystemExit): raise _e

    # Sessione tmux dove vive l'exec (per convenzione = quella di chi lo ordina).
    sessione_exec = _sessione_coordinatore(slug_chiamante, scope_globale, studio_dest)

    # Nome finestra secondo convenzione AGY-EXEC_<SLUG>_<COSA> univoca per prevenire orfani o collisioni
    slug_clean = re.sub(r"[^A-Za-z0-9_]+", "_", slug.replace("exec_", "").upper()).strip("_")
    cosa_norm = re.sub(r"[^A-Za-z0-9_]+", "_", cosa.upper()).strip("_")
    if cosa_norm and cosa_norm != "TASK" and cosa_norm != slug_clean:
        nome_win = f"AGY-EXEC_{slug_clean}_{cosa_norm}"[:40]
    else:
        nome_win = f"AGY-EXEC_{slug_clean}"[:40]

    conv_id = str(uuid.uuid4())
    work_dir = os.path.join(BASE_DIR, f"{slug}_studio")

    # SEZIONE 6 + LAVORO 1: la regola del piano è parte del system_prompt
    # (così arriva sia al launch sia al bootstrap), NON un'opzione. Se è stato
    # richiesto un profilo standard, il system_prompt finale = blocco del
    # profilo + mandato specifico + premessa piano.
    system_prompt_finale = profilo_blocco + system_prompt + PREMESSA_PIANO_EXEC

    # ROUTER FONDI (25/08/2026, ticket 454f Manfredo, exec fix_router_engine_crea_exec):
    # DEFAULT_ENGINE era usato alla cieca senza mai controllare lo stato fondi reale
    # (stesso gap gia' chiuso in agent_launcher.py via router_fondi_engine).
    # L'override umano esplicito '--engine' vince SEMPRE e non passa mai dal router.
    # Import/chiamata protetti: se il modulo manca o la risoluzione fallisce,
    # fallback SEMPRE a DEFAULT_ENGINE storico (zero rischio di rottura).
    _default_engine_eff = DEFAULT_ENGINE
    if not engine_override and router_fondi_engine is not None:
        try:
            _default_engine_eff, _dett_router = router_fondi_engine.motore_effettivo_per_avvio(
                {"engine": DEFAULT_ENGINE, "provider": provider_override}, DEFAULT_ENGINE)
            if _default_engine_eff != DEFAULT_ENGINE:
                print(f"  🔀 Router fondi: DEFAULT_ENGINE '{DEFAULT_ENGINE}' -> '{_default_engine_eff}' "
                      f"(motivo: {_dett_router.get('motivo', '')})")
        except Exception as _e_router:
            print(f"  ⚠️ Router fondi non applicabile, uso DEFAULT_ENGINE storico: {_e_router}")
            _default_engine_eff = DEFAULT_ENGINE

    ag_record = {
        "name": f"Exec {slug.replace('exec_','').replace('_',' ').title()}",
        "role": system_prompt[:200],
        "engine": engine_override or _default_engine_eff,
        "studio": studio_dest,
        "conv_id": conv_id,
        "work_dir": work_dir,
        "always_on": False,
        "tmux_window": f"{sessione_exec}:{nome_win}",
        "host_machine": "VPS",
        "creato_da": slug_chiamante,
        "creato_il": time.strftime("%Y-%m-%d %H:%M:%S"),
        "system_prompt": system_prompt_finale,
        "piano_obbligato": True,
    }
    # GUARDIA FINOPS EXEC (23/08/2026, ordine Manfredo MSG 14835 & 14849):
    # Gli exec usa-e-getta devono girare di DEFAULT su Antigravity (AGY) con i token inclusi (cascata flash).
    # Non devono intaccare la quota Claude a meno che il chiamante non specifichi espressamente '--engine claude'.
    # E per nessun motivo possono usare Opus/cascata-pro.
    _engine_eff = ag_record["engine"]
    if _engine_eff == "agy":
        if not model_override:
            ag_record["model"] = "cascata-fast"
        else:
            ag_record["model"] = model_override
        ag_record["provider"] = None
        print(f"  ⚡ Exec configurato su Antigravity (AGY) nativo — modello/cascata: '{ag_record['model']}' (token inclusi).")
    else:
        if model_override and any(m in model_override.lower() for m in ("opus", "cascata-pro")):
            print(f"  ⚠️ FinOps Guard: modello '{model_override}' non consentito per exec usa-e-getta (riservato ai coordinatori). "
                  f"Declassato automaticamente a 'cascata-fast'.")
            model_override = "cascata-fast"

        # 25/08/2026 (ticket 454f bis, cosimo_2/samantha_1, CAUSA VERA del
        # mis-routing '--provider deepseek'): il default 'cascata-fast' e' un
        # alias della cascata NATIVA claude (CASCATE_CLAUDE in engine_adapter.py),
        # non un modello deepseek. Assegnarlo comunque quando c'e' un
        # provider_override esplicito faceva si' che engine_adapter.cascata_per_engine()
        # matchasse sul campo 'model' (anche con provider='deepseek') e imboccasse
        # _build_cascade_claude(), ignorando COMPLETAMENTE il provider: l'exec
        # partiva su Sonnet nativo bruciando quota Claude vera invece di deepseek
        # (riprodotto dal vivo: tmux mostrava "You've used 84% of your weekly
        # limit"). Con un provider_override esplicito e nessun --model, il
        # modello resta indeciso qui (None) e viene risolto sotto a
        # DEFAULT_MODEL_CLAUDE_EXEC (un vero modello del provider, non un alias
        # di cascata) — cosi' cascata_per_engine non trova piu' match ne' su
        # provider ne' su model, e il ramo provider viene davvero imboccato.
        if model_override:
            ag_record["model"] = model_override
        elif provider_override:
            ag_record["model"] = None
        else:
            ag_record["model"] = "cascata-fast"

        CLAUDE_NATIVE_MODELS = ("fable", "claude-fable", "sonnet", "opus", "haiku", "cascata-fast", "cascata-pro")
        is_claude_native = bool(ag_record.get("model") and any(m in str(ag_record.get("model")).lower() for m in CLAUDE_NATIVE_MODELS))

        if provider_override:
            ag_record["provider"] = provider_override
            if not ag_record.get("model"):
                ag_record["model"] = DEFAULT_MODEL_CLAUDE_EXEC
                print(f"  🔀 provider '{provider_override}' esplicito, nessun --model: modello di default "
                      f"'{DEFAULT_MODEL_CLAUDE_EXEC}' applicato (non la cascata nativa).")
        elif is_claude_native:
            ag_record["provider"] = None
            print(f"  🧠 Modello Claude nativo '{ag_record['model']}' applicato: nessun provider proxy applicato.")
        elif DEFAULT_PROVIDER_CLAUDE:
            ag_record["provider"] = DEFAULT_PROVIDER_CLAUDE
            if not ag_record.get("model"):
                ag_record["model"] = DEFAULT_MODEL_CLAUDE_EXEC
            print(f"  🔀 provider non indicato: applico il default "
                  f"'{DEFAULT_PROVIDER_CLAUDE}/{ag_record['model']}' (niente quota Claude).")

    inserisci_agente(slug, ag_record)
    print(f"  💾 Registrato '{slug}' su Postgres "
          f"(studio={studio_dest}, conv_id={conv_id[:8]}…, creato_da={slug_chiamante}).")

    # SEZIONE 7: crea il profilo Obsidian (identita', mandato, data) prima di
    # lanciare, cosi' il bootstrap del prompt_builder non parte 'senza profilo'.
    profilo, creato = assicura_profilo(slug, ag_record["name"], system_prompt)
    if creato:
        print(f"  🧠 Profilo Obsidian creato: {profilo}")
    elif profilo:
        print(f"  🧠 Profilo Obsidian gia' presente: {profilo} (invariato)")

    # setta work_dir di proprieta' (eventuale unix_user dedicato)
    os.makedirs(work_dir, exist_ok=True)

    # 25/08/2026 (mandato samantha_1, segnalazioni Aurelio/auruncia_tech_1):
    # il profilo opt-in `ricercatore_browser` si porta dietro un `.mcp.json` con i
    # tool chrome-contabo gia' configurati nella work_dir PRIMA del lancio (il
    # profilo da solo mette solo testo nel prompt, non file su disco). SOLO per
    # questo profilo: per tutti gli altri (o nessun profilo) nessun file scritto,
    # zero effetti collaterali.
    if profilo_nome == "ricercatore_browser":
        _mcp_config = {
            "mcpServers": {
                "chrome-contabo": {
                    "command": "/root/ecosistema_agenti/core/venv/bin/python3",
                    "args": [
                        "/root/ecosistema_agenti/mcp_servers/mcp_chrome_contabo.py",
                    ],
                }
            }
        }
        _mcp_path = os.path.join(work_dir, ".mcp.json")
        with open(_mcp_path, "w", encoding="utf-8") as _mf:
            json.dump(_mcp_config, _mf, indent=2)
            _mf.write("\n")
        print(f"  🔌 .mcp.json scritto in {_mcp_path} (tool chrome-contabo pronti).")

    # 31/08/2026 (bug segnalato da exec_immagini_luxury, mandato samantha_1):
    # il profilo `esperto_flow` (Google Labs Flow) ha bisogno di tool browser per
    # mandato ma non era coperto dalla condizione sopra, quindi nasceva senza
    # .mcp.json e zero tool chrome_*. A differenza di ricercatore_browser usa
    # chrome-local (non chrome-contabo): Google Labs Flow richiede la sessione
    # Google gia' loggata di Manfredo sul Surface, non il browser server.
    elif profilo_nome == "esperto_flow":
        _mcp_config = {
            "mcpServers": {
                "chrome-local": {
                    "command": "/root/ecosistema_agenti/core/venv/bin/python3",
                    "args": [
                        "/root/ecosistema_agenti/core/mcp_chrome.py",
                    ],
                }
            }
        }
        _mcp_path = os.path.join(work_dir, ".mcp.json")
        with open(_mcp_path, "w", encoding="utf-8") as _mf:
            json.dump(_mcp_config, _mf, indent=2)
            _mf.write("\n")
        print(f"  🔌 .mcp.json scritto in {_mcp_path} (tool chrome-local pronti).")

    # 15/08/2026 — stessa struttura simlink di create_new_agent_or_studio.py:
    # senza, un exec su engine claude/agy non trova AGENTS.md/CLAUDE.md nella
    # sua work_dir (funzionava per deepcode per puro caso, mai testato sugli
    # altri motori finche' non l'ha segnalato Cosimo).
    agents_dir = os.path.join(work_dir, ".agents")
    os.makedirs(agents_dir, exist_ok=True)
    os.makedirs(os.path.join(agents_dir, "skills"), exist_ok=True)
    symlink_path = os.path.join(agents_dir, "AGENTS.md")
    if not os.path.exists(symlink_path):
        try:
            os.symlink("/root/ecosistema_agenti/AGENTS.md", symlink_path)
        except Exception as e:
            print(f"  ⚠️ Avviso symlink: {e}")
    claude_md_path = os.path.join(work_dir, "CLAUDE.md")
    if not os.path.exists(claude_md_path):
        try:
            os.symlink("/root/ecosistema_agenti/AGENTS.md", claude_md_path)
        except Exception as e:
            print(f"  ⚠️ Avviso symlink CLAUDE.md: {e}")
    dot_claude_path = os.path.join(work_dir, ".claude")
    if not os.path.exists(dot_claude_path):
        try:
            os.symlink(".agents", dot_claude_path)
        except Exception as e:
            print(f"  ⚠️ Avviso symlink .claude: {e}")

    _fh = _lock_studio(sessione_exec)
    try:
        _target, consegnato, dettaglio = lancia_agente(
            slug, ag_record, sessione_exec, nome_win, conv_id, system_prompt_finale)
    finally:
        _fh  # lock referenziato per la durata

    # 22/08/2026 (mandato samantha_1): la creazione e' FALLITA se il mandato non
    # e' stato consegnato. Un exec acceso senza mandato non e' un successo
    # parziale: lavora a vuoto bruciando token finche' qualcuno non se ne accorge
    # a occhio (prove: exec_vision_live_day, exec_mobile_dom).
    if not consegnato:
        print(f"❌ EXEC '{slug}': finestra '{ag_record['tmux_window']}' creata, "
              f"ma il MANDATO NON E' STATO CONSEGNATO — {dettaglio}.")
        print(f"   L'exec e' ACCESO e SENZA MANDATO: lavorerebbe a vuoto.")
        print(f"   Cosa fare: 1) libera l'input box del pannello, poi "
              f"`send_a2a.py {slug} --file <mandato>`; "
              f"oppure 2) `elimina_exec {slug}` e ricrealo.")
        raise SystemExit(1)

    print(f"✅ EXEC '{slug}' creato e avviato in '{ag_record['tmux_window']}' "
          f"(mandato consegnato: {dettaglio}).")
    print(f"   ⚠️  always_on=false forzato. Al termine del lavoro: elimina_exec {slug}.")


def comando_elimina_exec(slug_chiamante, scope_globale, studio_chiamante, args):
    if not args:
        raise SystemExit("Uso: elimina_exec <slug>")
    slug = args[0].lower()

    verifica_perimetro(slug_chiamante, scope_globale, studio_chiamante, slug)

    data = get_agent(slug)
    if not data:
        print(f"ℹ️  Nessun record '{slug}': nulla da dismettere.")
        return
    gia_dismesso = bool(data.get("dismesso"))
    if gia_dismesso:
        print(f"ℹ️  '{slug}' risulta gia' dismesso: chiudo comunque eventuali finestre residue.")

    # Raccolta ricorsiva di tutti i discendenti (creato_da=<slug>, e figli dei figli)
    discendenti = _raccogli_discendenti(slug)
    tutti = [slug] + discendenti

    print(f"🗑️  Eliminazione RICORSIVA di '{slug}' e {len(discendenti)} figli: "
          f"{', '.join(discendenti) if discendenti else 'nessuno'}")

    # Spengo e marco: prima i figli piu' in profondita', poi il padre (ordine
    # di eliminazione corretto richiesto dalla spec). Marcatura dismesso =
    # reversibile, NON si cancella nulla.
    fallimenti = []
    for s in reversed(tutti):
        d = get_agent(s)
        if d and d.get("dismesso"):
            # Zombie: record gia' dismesso ma finestra/processo ancora vivo.
            # Chiudi le finestre residue senza rimarcare (il record e' gia' dismesso).
            esito = spegni_finestra(s)
            if esito and not esito.get("ok"):
                err_msg = esito.get("errore") or f"finestre: {esito.get('finestre_vive')}, pids: {esito.get('pids_vivi')}"
                print(f"   ⚠️  '{s}': spegnimento finestre residue fallito: {err_msg}", file=sys.stderr)
                fallimenti.append((s, err_msg))
            elif esito and esito.get("finestre_spente", 0) > 0:
                print(f"   🧟  '{s}': gia' dismesso ma chiuse {esito['finestre_spente']} finestre residue.")
            continue
        esito = spegni_finestra(s)
        if esito and not esito.get("ok"):
            err_msg = esito.get("errore") or f"finestre: {esito.get('finestre_vive')}, pids: {esito.get('pids_vivi')}"
            print(f"   ⚠️  '{s}': spegnimento finestre fallito o non verificato: {err_msg}", file=sys.stderr)
            fallimenti.append((s, err_msg))
        else:
            dup_info = ""
            if esito and esito.get("finestre_spente", 0) > 1:
                dup_info = f" ({esito['finestre_spente']} finestre terminate: {', '.join(esito['window_ids'])})"
            mark = {
                "dismesso": True,
                "dismesso_il": time.strftime("%Y-%m-%d"),
                "dismesso_da": slug_chiamante,
            }
            set_agent_field(s, mark)
            print(f"   🗂️  '{s}': marcato dismesso (reversibile) e finestre spente{dup_info}.")

    if fallimenti:
        print(f"⚠️  Exec '{slug}' dismesso con {len(fallimenti)} warning su spegnimento processi.", file=sys.stderr)
    else:
        print(f"✅ Exec '{slug}' (e relativi figli) dismesso. Record conservato "
              f"/reversibile rimuovendo la chiave 'dismesso'.")

    # RELEASE AUTOMATICO DELL'AFFINITA' DI POOL (29/08/2026, mandato
    # exec_fix_pool_affinita, causa B: affinita' ferme perche' il release era
    # manuale e ci si dimenticava). Rilascia SOLO l'affinita' dello slug
    # eliminato (mai dei discendenti: non sono il mittente che si sta
    # dismettendo qui), in TUTTI i pool noti perche' da qui non sappiamo a
    # priori in quale pool avesse mai claimato. FAIL-OPEN per costruzione:
    # qualunque errore si stampa e basta, l'eliminazione e' gia' avvenuta e
    # non si blocca mai per un'operazione di contabilita'.
    _rilascia_affinita_pool_su_elimina(slug, slug_chiamante)


def _rilascia_affinita_pool_su_elimina(slug, slug_chiamante):
    """Rilascia l'affinita' di pool DEL SOLO slug eliminato, come farebbe
    `pool_release.py <slug>`, ma in versione amministrativa (chi elimina non
    e' necessariamente l'istanza pool che aveva claimato per lui — vedi
    pool_lib.release_amministrativo). FAIL-OPEN: mai un'eccezione qui puo'
    interrompere un elimina_exec gia' eseguito; si stampa e si prosegue."""
    try:
        import pool_lib
    except Exception as e:
        print(f"   ⚠️  pool_lib non disponibile: affinita' di pool NON verificata per '{slug}' ({e}).",
              file=sys.stderr)
        return
    try:
        # solo_attivi=False: un pool dismesso non deve mai trattenere
        # per sempre un'affinita' residua che non verra' piu' rilasciata
        # (P4, mandato exec_nit_selfheal) — scopri_pool() di default filtra
        # via i pool dismessi lato Postgres, qui serve l'elenco completo.
        pools, _fonte = pool_lib.scopri_pool(solo_attivi=False)
    except Exception as e:
        print(f"   ⚠️  impossibile enumerare i pool per rilasciare l'affinita' di '{slug}': {e}",
              file=sys.stderr)
        return
    for pool in pools:
        try:
            precedente = pool_lib.release_amministrativo(
                pool, slug, motivo=f"elimina_exec (chiamato da '{slug_chiamante}')", chi=slug_chiamante)
        except Exception as e:
            print(f"   ⚠️  rilascio affinita' pool '{pool}' per '{slug}' fallito (non bloccante): {e}",
                  file=sys.stderr)
            continue
        if precedente:
            print(f"   🔓 Affinita' di pool '{pool}' per '{slug}' rilasciata (era di '{precedente}').")


def _raccogli_discendenti(slug):
    """Ritorna la lista dei discendenti diretti + ricorsivi (creato_da=<slug>)."""
    conn = _conn()
    cur = conn.cursor()
    cur.execute("SELECT agent_id FROM agents WHERE data->>'creato_da' = %s;", (slug,))
    figli = [r[0] for r in cur.fetchall()]
    conn.close()
    out = list(figli)
    for f in figli:
        out.extend(_raccogli_discendenti(f))
    return out


def comando_accendi_agente(slug_chiamante, scope_globale, studio_chiamante, args):
    # Opzione opzionale '--engine <nome>' (17/08/2026, migrazione goose):
    # permette di riaccendere l'agente su un motore diverso senza spegnere e
    # fare l'UPDATE a mano. Stesso pattern del --profilo/--engine di crea_exec.
    engine_override = None
    if "--engine" in args:
        i = args.index("--engine")
        if i + 1 >= len(args):
            raise SystemExit(
                "❌ '--engine' richiede un nome. Motori validi: "
                + ", ".join(engine_adapter.VALID_ENGINES) + ".")
        engine_override = args[i + 1]
        if engine_override not in engine_adapter.VALID_ENGINES:
            raise SystemExit(
                f"❌ Motore '{engine_override}' non valido. Motori validi: "
                + ", ".join(engine_adapter.VALID_ENGINES) + ".")
        args = args[:i] + args[i + 2:]

    # '--provider <nome>' (17/08/2026, exec migra_cc_pilota): come in
    # crea_exec — provider dietro Claude Code, solo motore claude, persistito
    # su Postgres (merge additivo, stesso meccanismo dell'--engine qui sopra).
    provider_override = None
    if "--provider" in args:
        i = args.index("--provider")
        if i + 1 >= len(args):
            raise SystemExit(
                "❌ '--provider' richiede un nome. Provider validi (motore claude): "
                + ", ".join(sorted(engine_adapter.CLAUDE_PROVIDERS)) + ".")
        provider_override = args[i + 1]
        if provider_override not in engine_adapter.CLAUDE_PROVIDERS:
            raise SystemExit(
                f"❌ Provider '{provider_override}' non valido. Provider validi "
                "(motore claude): "
                + ", ".join(sorted(engine_adapter.CLAUDE_PROVIDERS)) + ".")
        args = args[:i] + args[i + 2:]

    if not args:
        raise SystemExit("Uso: accendi_agente <slug> [--engine <nome>] [--provider <nome>]")
    slug = args[0].lower()
    verifica_perimetro(slug_chiamante, scope_globale, studio_chiamante, slug)

    data = get_agent(slug)
    if not data:
        raise SystemExit(f"❌ Nessun agente '{slug}' in Postgres.")
    if data.get("dismesso"):
        raise SystemExit(
            f"❌ '{slug}' risulta DISMESSO: per riattivarlo rimuovi prima la "
            f"chiave 'dismesso' dal record (operazione riservata). Non si "
            f"accende un agente dismesso.")

    # 18/08/2026 (race reale Luca: Samantha e Cosimo hanno acceso lo stesso
    # agente in parallelo → DUE finestre AGY-LUCA sullo stesso conv_id, una
    # da spegnere a mano). Guardia anti-doppia-accensione: se la finestra
    # dell'agente esiste GIA' e dentro c'e' un processo motore VIVO, si
    # RIFIUTA. Se la finestra esiste ma il processo e' morto (shell nuda),
    # si procede: e' il caso legittimo del revive. Stessa famiglia del
    # rifiuto slug-duplicato di crea_exec.
    _tw = (data.get("tmux_window") or "")
    if ":" in _tw:
        # 22/08/2026 FIX classe "match per prefisso" (mandato exec_fix_match_prefisso):
        # tmux fa match FUZZY/per prefisso sul nome finestra — 'fiscale:AGY-PUBLIO'
        # risolveva su AGY-PUBLIO-2 e la guardia mentiva ("GIA' ACCESO"). Ora il
        # target usa 'sess:=win' (match ESATTO, funzione condivisa tmux_esatto):
        # una finestra assente da' None, mai il pid di una finestra con lo stesso
        # prefisso di nome.
        _pp = tmux_esatto.pane_pid_finestra(_tw)
        if _pp:
            _figli = subprocess.getoutput(f"ps --ppid {_pp} -o pid= 2>/dev/null")
            for _pid in _figli.split():
                try:
                    with open(f"/proc/{_pid}/cmdline", "rb") as _f:
                        _cmd = _f.read().lower()
                    if any(m in _cmd for m in (b"claude", b"deepcode", b"agy", b"qwen", b"goose", b"node")):
                        raise SystemExit(
                            f"❌ '{slug}' risulta GIA' ACCESO: finestra '{_tw}' viva con "
                            f"processo motore attivo (pid {_pid}). Doppia accensione = due "
                            f"finestre sullo stesso conv_id (successo davvero con Luca il "
                            f"18/08). Se e' bloccato usa il refresh o spegni_agente prima.")
                except SystemExit:
                    raise
                except Exception:
                    pass

    if engine_override:
        # Persisti il cambio motore nel record Postgres (merge additivo) e
        # usalo per il lancio. La memoria di sessione resta coesa con l'engine
        # a cui gira l'agente.
        inserisci_agente(slug, {"engine": engine_override})
        data["engine"] = engine_override
        print(f"  ⚙️  agente '{slug}' aggiornato a motore '{engine_override}' (Postgres).")
    if provider_override:
        # il perimetro claude va verificato sul motore EFFETTIVO (record +
        # eventuale --engine appena applicato), non solo sul flag.
        if engine_adapter.get_engine(data) != "claude":
            raise SystemExit(
                "❌ '--provider' vale solo col motore claude: "
                f"'{slug}' girerebbe su '{engine_adapter.get_engine(data)}'. "
                "Aggiungi '--engine claude' se la migrazione e' voluta.")
        inserisci_agente(slug, {"provider": provider_override})
        data["provider"] = provider_override
        print(f"  ⚙️  agente '{slug}' aggiornato a provider '{provider_override}' (Postgres).")
    # MSG 14355 (22/08/2026, Manfredo): il default del modello deve scalare
    # col profilo — coordinatore/super -> profilo alto (DS Max), specialista
    # -> DS Flash — non il default fisso di engine_adapter (che e' Flash).
    # Applicato SOLO al lancio, derivato dal profilo, senza toccare il record:
    # cosi' la direttiva vale anche per gli agenti gia' esistenti senza
    # 'model' esplicito, e la prossima riaccensione la ricalcola daccapo.
    if not data.get("model") and engine_adapter.get_engine(data) == "claude":
        data["model"] = _modello_default_profilo(slug, data)
        _prov = data.get("provider") or DEFAULT_PROVIDER_CLAUDE
        print(f"  🧠 nessun 'model' nel record: default per profilo applicato "
              f"({_prov}/{data['model']}).")
    conv_id = data.get("conv_id")
    if not conv_id:
        raise SystemExit(f"❌ '{slug}' senza conv_id in Postgres: nulla da riprendere.")
    tmw = data.get("tmux_window") or ""
    if ":" in tmw:
        sessione, nome_win = tmw.rsplit(":", 1)
    else:
        # derivazione di ripiego dallo studio
        sessione = data.get("studio") or slug
        nome_win = f"AGY-{slug.upper()}"
    win_prev = nome_win

    # SEZIONE 7: anche un agente permanente riaperto deve ritrovare la propria
    # memoria di lavoro. Se il profilo Obsidian manca, lo crea lo strumento
    # (identita', mandato, data) senza sovrascrivere eventuali profili pieni.
    assicura_profilo(slug, data.get("name") or slug, data.get("system_prompt") or "")

    _fh = _lock_studio(sessione)
    try:
        # 24/08/2026 (exec_fix_lancio_fresco): qui il conv_id arriva da Postgres
        # ed e' ESISTENTE (poche righe sopra: "senza conv_id in Postgres: nulla
        # da riprendere"). L'intento e' RIPRENDERE: dichiararlo 'fresco'
        # significherebbe, col fix di oggi, saltare `--resume` e azzerare la
        # memoria dell'agente a ogni riaccensione.
        _target, consegnato, dettaglio = lancia_agente(
            slug, data, sessione, nome_win, conv_id, data.get("system_prompt"),
            modalita="resume")
    finally:
        _fh
    # Stesso criterio di crea_exec (22/08/2026): un agente acceso che non ha
    # ricevuto il bootstrap non sa chi e' — dirlo, non nasconderlo.
    if not consegnato:
        print(f"❌ Agente '{slug}': finestra '{sessione}:{win_prev}' creata, ma il "
              f"BOOTSTRAP NON E' STATO CONSEGNATO — {dettaglio}.")
        print(f"   Cosa fare: libera l'input box del pannello e reinvia il "
              f"bootstrap, oppure `spegni_agente {slug}` e riaccendilo.")
        raise SystemExit(1)
    # Fix 29/08/2026 (mandato samantha_2, report diagnosi radio): una
    # 'spento_nota' scritta a mano su uno spegnimento intenzionale non veniva
    # mai ripulita alla riaccensione — restava li' a zittire per sempre
    # l'allarme L4 (self_healing_stato.py) anche dopo che l'agente era
    # tornato pienamente operativo (caso reale: radio, nota del 22/08 mai
    # chiusa, agente rimasta viva fino al 28/08 senza che nessuno lo sapesse
    # dal canale di allarme). Additivo: tocca solo un campo diagnostico letto
    # SOLO da self_healing_stato.py.
    if data.get("spento_nota"):
        _nota_prec = data["spento_nota"]
        set_agent_field(slug, {"spento_nota": None})
        print(f"ℹ️  Nota 'spento_nota' obsoleta rimossa da '{slug}' (era: "
              f"{str(_nota_prec)[:60]}...)")
    print(f"✅ Agente '{slug}' acceso in '{sessione}:{win_prev}' "
          f"(bootstrap consegnato: {dettaglio}).")


def comando_spegni_agente(slug_chiamante, scope_globale, studio_chiamante, args):
    if not args:
        raise SystemExit("Uso: spegni_agente <slug>")
    slug = args[0].lower()
    verifica_perimetro(slug_chiamante, scope_globale, studio_chiamante, slug)

    data = get_agent(slug)
    if not data:
        raise SystemExit(f"❌ Nessun agente '{slug}' in Postgres.")
    esito = spegni_finestra(slug)
    if esito is None:
        print(f"ℹ️  '{slug}' non ha una finestra tmux da spegnere (o campo assente).")
        return
    if esito.get("gia_spento"):
        print(f"ℹ️  '{slug}' ({esito['sessione']}:{esito['nome']}) risulta già spento (nessuna finestra tmux attiva).")
        return
    if not esito["ok"]:
        dettagli = []
        if esito.get("finestre_vive"):
            dettagli.append(f"finestre residue: {esito['finestre_vive']}")
        if esito.get("pids_vivi"):
            dettagli.append(f"PID residui vivi: {esito['pids_vivi']}")
        if esito.get("errore"):
            dettagli.append(f"errore: {esito['errore']}")
        print(f"❌ Spegnimento '{slug}' FALLITO o NON verificato: {', '.join(dettagli)}", file=sys.stderr)
        raise SystemExit(1)

    sess = esito["sessione"]
    nome = esito["nome"]
    num_win = esito.get("finestre_spente", 1)
    extra_dup = f" (terminate {num_win} finestre omonime: {', '.join(esito['window_ids'])})" if num_win > 1 else ""
    pids_count = len(esito.get("pids_raccolti", []))
    pids_info = f", {pids_count} processi terminati" if pids_count > 0 else ""
    print(f"✅ Agente '{slug}' spento e verificato (finestra '{sess}:{nome}'{extra_dup}{pids_info}). "
          f"always_on resta come da record; se =true verra' riacceso dal "
          f"cron. Per uno shutdown duraturo va tolto il flag, DECISIONE di Betty.")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def comando_chi_sono(slug_chiamante, scope_globale, studio_chiamante, args):
    """Comando di SOLA LETTURA (22/08/2026): stampa l'identita' che lo strumento
    ha risolto per il chiamante, senza toccare nulla. Serve a verificare da
    fuori (e da dentro una finestra di servizio) che la risoluzione funzioni:
    la prova che 'l'identita' e' riconosciuta' e' questo output, non una
    dichiarazione. Uscita 0 sempre se l'identita' e' stata risolta."""
    esito = {
        "slug": slug_chiamante,
        "nome": NOME_PER_SLUG_SERVIZIO.get(slug_chiamante) or slug_chiamante,
        "scope_globale": bool(scope_globale),
        "studio": studio_chiamante,
        "finestra_tmux": _finestra_tmux_chiamante(),
        "sessione_tmux": _sessione_tmux_chiamante(),
        "via": ("servizio" if slug_chiamante in COMANDI_PER_SERVIZIO else
                "sistema" if os.environ.get("AGY_SYSTEM_SCOPE") else
                "sudo_user" if (os.environ.get("SUDO_USER") or "") not in ("", "root") else
                "tmux"),
        "comandi_ammessi": sorted(COMANDI_PER_SERVIZIO.get(slug_chiamante, [])) or "tutti",
    }
    print(json.dumps(esito, ensure_ascii=False))


COMANDI = {
    "crea_exec": comando_crea_exec,
    "elimina_exec": comando_elimina_exec,
    "accendi_agente": comando_accendi_agente,
    "spegni_agente": comando_spegni_agente,
    "chi_sono": comando_chi_sono,
}


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help", "help"):
        print(__doc__)
        sys.exit(0)
    comando = sys.argv[1].lower()
    args = sys.argv[2:]
    if comando not in COMANDI:
        print(f"❌ Comando sconosciuto '{comando}'. "
              f"Ammessi: {', '.join(COMANDI)}.", file=sys.stderr)
        sys.exit(2)

    try:
        slug_chiamante, scope_globale, studio_chiamante = identifica_chiamante()
    except PermissionError as e:
        print(f"🚫 {e}", file=sys.stderr)
        sys.exit(1)

    # Via di servizio: identita' riconosciuta ma COMANDI ristretti alla classe
    # di competenza del servizio (vedi IDENTITA_SERVIZIO). Rifiuto esplicito.
    ammessi = COMANDI_PER_SERVIZIO.get(slug_chiamante)
    if ammessi is not None and comando not in ammessi:
        print(f"🚫 Il servizio '{slug_chiamante}' non e' autorizzato al comando "
              f"'{comando}'. Ammessi: {', '.join(sorted(ammessi))}.", file=sys.stderr)
        sys.exit(1)

    if scope_globale:
        print(f"🔓 Scope GLOBALE: identificato '{slug_chiamante}' "
              f"(root/betty o via di sistema).")
        if os.environ.get("AGY_SYSTEM_SCOPE"):
            print(f"   ⚙️  via di sistema (AGY_SYSTEM_SCOPE, pid={os.getpid()}).")
    else:
        print(f"🔒 Perimetro coordinatore '{slug_chiamante}' "
              f"(studio '{studio_chiamante}').")

    try:
        COMANDI[comando](slug_chiamante, scope_globale, studio_chiamante, args)
    except PermissionError as e:
        print(f"🚫 {e}", file=sys.stderr)
        sys.exit(1)
    except SystemExit as e:
        if isinstance(e.code, str):
            print(f"{e.code}", file=sys.stderr)
            sys.exit(1)
        sys.exit(e.code if isinstance(e.code, int) else 1)


if __name__ == "__main__":
    main()
