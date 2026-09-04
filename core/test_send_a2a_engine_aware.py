"""Collaudo FIX 10/08/2026 — bug reale trovato da Betty a mano su Regista
(Deep Code): _notifica_tmux in send_a2a.py usava un pattern testuale hardcoded
sulla UI di Claude Code ("esc to interrupt" / "bypass permissions on"). Su
qualunque altro motore la seconda stringa non compare mai, quindi la
condizione OR era vera SEMPRE al primo C-m — falso positivo "notificato" anche
quando il testo restava solo accumulato nel campo di input, mai sottomesso
(3 messaggi impilati su "Type your message...", osservato dal vivo da Betty).

Riproduce lo scenario ESATTO: pane deepcode che, dopo il C-m, mostra ancora
il placeholder idle "Type your message..." (mai "Thinking..."), come quando
tmux consegna il tasto ma la TUI non lo interpreta come submit. Con il fix,
_notifica_tmux deve riconoscerlo come NON generato e ritentare/fallire onesto,
non dichiarare successo al primo giro."""
import sys
import time
import types

sys.path.append("/root/ecosistema_agenti/core")
import send_a2a

ok = True


def check(label, cond, extra=""):
    global ok
    status = "PASS" if cond else "FAIL"
    if not cond:
        ok = False
    print(f"[{status}] {label} {extra}")


# La nuova fase wait_idle (_attendi_pannello_libero) e' testata per conto suo
# più sotto, con timeout brevi e sintetici. Qui viene stubbata a "già libero,
# procedi" per non far dipendere QUESTI test (che verificano il comportamento
# a valle: paste/dedup/notifica) dal poll reale, che con pannelli sempre
# "occupati" nei fake_run sotto girerebbe fino al timeout vero (90s) prima di
# ogni singolo controllo.
_attendi_vero = send_a2a._attendi_pannello_libero
send_a2a._attendi_pannello_libero = lambda *a, **k: True


def _fake_run_factory(pane_sequence):
    """Ogni chiamata a tmux capture-pane consuma il prossimo testo della
    sequenza; le altre chiamate (load-buffer, paste-buffer, send-keys,
    delete-buffer) ritornano un successo neutro senza toccare tmux davvero."""
    state = {"i": 0}

    def _fake_run(cmd):
        result = types.SimpleNamespace(returncode=0, stdout="", stderr="")
        if "capture-pane" in cmd:
            idx = min(state["i"], len(pane_sequence) - 1)
            result.stdout = pane_sequence[idx]
            state["i"] += 1
        return result

    return _fake_run


# --- Scenario reale: deepcode, testo mai sottomesso (bug osservato) ---------
PANE_IDLE_DEEPCODE = "qualche riga di contesto\n> Type your message...\n"

orig_run = send_a2a._run
send_a2a._run = _fake_run_factory([PANE_IDLE_DEEPCODE] * 10)
try:
    esito = send_a2a._notifica_tmux(
        "fake:WIN", "[A2A_FROM:x] [A2A_TYPE:report] [A2A_ID:test]\ncorpo", max_attempts=2, engine="deepcode"
    )
finally:
    send_a2a._run = orig_run

check(
    "deepcode, pane resta su 'Type your message...' dopo il C-m: NON dichiara notificato (bug chiuso)",
    esito is False,
)

# --- Controprova: deepcode, generazione REALMENTE partita -------------------
PANE_GENERATING_DEEPCODE = "qualche riga\nThinking... (2s) · ↓ 40 tokens\n"

send_a2a._run = _fake_run_factory([PANE_GENERATING_DEEPCODE] * 10)
try:
    esito = send_a2a._notifica_tmux(
        "fake:WIN", "[A2A_FROM:x] [A2A_TYPE:report] [A2A_ID:test]\ncorpo", max_attempts=2, engine="deepcode"
    )
finally:
    send_a2a._run = orig_run

check(
    "deepcode, pane mostra 'Thinking...' dopo il C-m: dichiara notificato (nessun falso negativo introdotto)",
    esito is True,
)

# --- Scenario Betty (10/08/2026, osservato dal vivo su Regista): pannello ---
# occupato con un tool lungo (comando bash in corso), mai "Thinking..." ne'
# idle puro — is_pane_generating non lo rileva come "sta generando", ma il
# vecchio codice ripassava il PASTE ad ogni tentativo: 4 copie identiche
# impilate nell'input che l'agente avrebbe dovuto leggere/scartare una per
# una. Il fix deve riconoscere che il campanello e' GIA' nel campo di input
# (marcatore visibile) e NON ripeterlo, solo un altro invio.
MARCATORE_BUSY = "[A2A_FROM:x] [A2A_TYPE:report] [A2A_ID:test-busy]"


def _fake_run_pannello_occupato():
    stato = {"incollato": False, "paste_count": 0}

    def _fake_run(cmd):
        result = types.SimpleNamespace(returncode=0, stdout="", stderr="")
        if "paste-buffer" in cmd:
            stato["paste_count"] += 1
            stato["incollato"] = True
        elif "capture-pane" in cmd:
            if stato["incollato"]:
                result.stdout = (
                    f"riga di contesto\n$ comando-bash-lungo-in-corso\n"
                    f"{MARCATORE_BUSY}\ncorpo\n> Type your message...\n"
                )
            else:
                result.stdout = "riga di contesto\n> Type your message...\n"
        return result

    return _fake_run, stato


fake_run, stato_busy = _fake_run_pannello_occupato()
send_a2a._run = fake_run
try:
    esito = send_a2a._notifica_tmux(
        "fake:WIN", f"{MARCATORE_BUSY}\ncorpo", max_attempts=4, engine="deepcode"
    )
finally:
    send_a2a._run = orig_run

check(
    "pannello occupato (bash lungo, mai rilevato come 'sta generando'): il paste avviene UNA sola volta "
    "su 4 tentativi, niente spam impilato (bug Betty chiuso)",
    stato_busy["paste_count"] == 1,
    f"paste_count={stato_busy['paste_count']}",
)
check(
    "pannello occupato: esito onesto (non notificato) invece di un falso positivo, il messaggio resta comunque in inbox",
    esito is False,
)

# --- Regressione: claude, comportamento storico invariato -------------------
PANE_GENERATING_CLAUDE = "...\nesc to interrupt\n"

send_a2a._run = _fake_run_factory([PANE_GENERATING_CLAUDE] * 10)
try:
    esito = send_a2a._notifica_tmux(
        "fake:WIN", "[A2A_FROM:x] [A2A_TYPE:report] [A2A_ID:test]\ncorpo", max_attempts=2, engine="claude"
    )
finally:
    send_a2a._run = orig_run

check("claude, 'esc to interrupt' presente: dichiara notificato come prima del fix", esito is True)

PANE_IDLE_CLAUDE = "...\n? for shortcuts   bypass permissions on\n"

send_a2a._run = _fake_run_factory([PANE_IDLE_CLAUDE] * 10)
try:
    esito = send_a2a._notifica_tmux(
        "fake:WIN", "[A2A_FROM:x] [A2A_TYPE:report] [A2A_ID:test]\ncorpo", max_attempts=2, engine="claude"
    )
finally:
    send_a2a._run = orig_run

check("claude, pane resta idle su 'bypass permissions on': NON dichiara notificato", esito is False)

# --- resolve_engine: fallback 'claude' se l'agente non ha motore in Postgres
class _FakeCursor:
    def execute(self, *a, **k):
        pass

    def fetchone(self):
        return None


class _FakeConn:
    def cursor(self):
        return _FakeCursor()

    def close(self):
        pass


import psycopg2  # noqa: E402

orig_connect = psycopg2.connect
psycopg2.connect = lambda *a, **k: _FakeConn()
try:
    motore = send_a2a.resolve_engine("agente-senza-motore-in-postgres")
finally:
    psycopg2.connect = orig_connect

check("resolve_engine: fallback 'claude' se la riga non ha 'engine' impostato", motore == "claude")

# --- _attendi_pannello_libero: la funzione VERA, non lo stub sopra ---------
send_a2a._attendi_pannello_libero = _attendi_vero

t0 = time.time()
send_a2a._run = _fake_run_factory(["qualche riga\n> Type your message...\n"] * 10)
try:
    esito = send_a2a._attendi_pannello_libero("fake:WIN", "deepcode", timeout_s=5.0, poll_interval=0.2)
finally:
    send_a2a._run = orig_run
durata = time.time() - t0
check("pannello già libero: True dopo 2 letture consecutive (non una sola, vedi fix sotto), veloce comunque",
      esito is True and durata < 1.0, f"durata={durata:.2f}s")

# Occupato per le prime 3 letture, poi libero — deve aspettare la transizione
# E le due letture consecutive di conferma
_seq_occupato_poi_libero = (
    ["qualche riga\nThinking... (1s)\n"] * 3
    + ["qualche riga\n> Type your message...\n"] * 10
)
t0 = time.time()
send_a2a._run = _fake_run_factory(_seq_occupato_poi_libero)
try:
    esito = send_a2a._attendi_pannello_libero("fake:WIN", "deepcode", timeout_s=5.0, poll_interval=0.2)
finally:
    send_a2a._run = orig_run
durata = time.time() - t0
check("pannello occupato poi libero: True dopo aver aspettato la transizione (non al primo giro)",
      esito is True and 0.6 <= durata < 5.0, f"durata={durata:.2f}s")

# --- SEGUITO reale (caso Betty, messaggio 669c Radio->Regista): un SOLO
# lampo di "libero" in mezzo a due stati occupati NON deve bastare — solo
# due letture VERE consecutive. Sequenza: occupato, occupato, LIBERO UNA
# VOLTA SOLA (il falso libero transitorio), occupato di nuovo, poi
# finalmente libero per davvero (2 volte).
_seq_falso_libero_transitorio = (
    ["qualche riga\nThinking... (1s)\n"] * 2
    + ["qualche riga\n> Type your message...\n"]        # lampo isolato, 1 sola lettura
    + ["qualche riga\nThinking... (2s)\n"] * 2           # torna occupato subito dopo
    + ["qualche riga\n> Type your message...\n"] * 10    # stavolta stabile
)
t0 = time.time()
send_a2a._run = _fake_run_factory(_seq_falso_libero_transitorio)
try:
    esito = send_a2a._attendi_pannello_libero("fake:WIN", "deepcode", timeout_s=5.0, poll_interval=0.2)
finally:
    send_a2a._run = orig_run
durata = time.time() - t0
check("falso libero transitorio (un solo lampo, come il caso reale Betty/669c): NON basta, aspetta lo stabile",
      esito is True and durata >= 1.0, f"durata={durata:.2f}s (deve superare il lampo isolato)")

# Mai libero entro il tetto: False, non un'attesa infinita
t0 = time.time()
send_a2a._run = _fake_run_factory(["qualche riga\nThinking... (1s)\n"] * 50)
try:
    esito = send_a2a._attendi_pannello_libero("fake:WIN", "deepcode", timeout_s=1.0, poll_interval=0.2)
finally:
    send_a2a._run = orig_run
durata = time.time() - t0
check("pannello mai libero: False dopo il tetto (mai un blocco infinito)",
      esito is False and durata < 2.0, f"durata={durata:.2f}s")

# Finestra inesistente (agente offline, caso reale DEV-110): bail-out
# immediato, MAI l'attesa intera — altrimenti ogni invio a un agente offline
# impiegherebbe fino a timeout_s prima di fallire nel punto giusto.
def _fake_run_finestra_inesistente(cmd):
    return types.SimpleNamespace(returncode=1, stdout="", stderr="can't find session: fake")


t0 = time.time()
send_a2a._run = _fake_run_finestra_inesistente
try:
    esito = send_a2a._attendi_pannello_libero("fake:WIN-INESISTENTE", "deepcode", timeout_s=90.0, poll_interval=0.2)
finally:
    send_a2a._run = orig_run
durata = time.time() - t0
check("finestra inesistente: False immediato, NON i 90s interi del tetto",
      esito is False and durata < 1.0, f"durata={durata:.2f}s")

# --- SEGUITO reale, terza conferma di Manfredo: ESC-poi-messaggio è il
# DEFAULT per deepcode (non solo urgente), claude/qwen/agy restano wait_idle.
# Verifica che il ramo giusto scatti per il motore giusto: ESC inviato SOLO
# per deepcode, MAI per claude — e che _attendi_pannello_libero (il wait) non
# venga proprio chiamato per deepcode.
_attendi_chiamate = []
send_a2a._attendi_pannello_libero = lambda *a, **k: (_attendi_chiamate.append(a) or True)

esc_inviati = []


def _fake_run_traccia_esc(cmd):
    if "Escape" in cmd:
        esc_inviati.append(cmd)
    result = types.SimpleNamespace(returncode=0, stdout="", stderr="")
    if "capture-pane" in cmd:
        result.stdout = "qualche riga\nThinking... (1s)\n"
    return result


send_a2a._run = _fake_run_traccia_esc
try:
    send_a2a._notifica_tmux("fake:WIN", "[A2A_FROM:x] [A2A_TYPE:report] [A2A_ID:t]\ncorpo", max_attempts=1, engine="deepcode")
finally:
    send_a2a._run = orig_run
    send_a2a._attendi_pannello_libero = _attendi_vero

check("deepcode: ESC inviato (comportamento di default, non solo urgente)", len(esc_inviati) == 1, f"esc_inviati={len(esc_inviati)}")
check("deepcode: _attendi_pannello_libero NON chiamato (ESC bypassa il wait)", len(_attendi_chiamate) == 0)

_attendi_chiamate.clear()
esc_inviati.clear()
send_a2a._attendi_pannello_libero = lambda *a, **k: (_attendi_chiamate.append(a) or True)
send_a2a._run = _fake_run_traccia_esc
try:
    send_a2a._notifica_tmux("fake:WIN", "[A2A_FROM:x] [A2A_TYPE:report] [A2A_ID:t]\ncorpo", max_attempts=1, engine="claude")
finally:
    send_a2a._run = orig_run
    send_a2a._attendi_pannello_libero = _attendi_vero

check("claude: NESSUN ESC inviato (resta wait_idle, invariato)", len(esc_inviati) == 0, f"esc_inviati={len(esc_inviati)}")
check("claude: _attendi_pannello_libero chiamato come prima (comportamento invariato)", len(_attendi_chiamate) == 1)

print()
print("RISULTATO: TUTTI I TEST PASSANO" if ok else "RISULTATO: CI SONO FALLIMENTI")
sys.exit(0 if ok else 1)
