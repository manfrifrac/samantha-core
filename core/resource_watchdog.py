"""
Watchdog risorse di sistema (RAM/load) per l'intero ecosistema.

Nato dal 04/08/2026: Aurelio aveva segnalato via A2A un carico critico (RAM
sotto 1GB, load 17+) scoperto solo perche' l'ha notato lui stesso mentre
lavorava. Non esisteva nessun monitor automatico delle risorse aggregate —
ecosystem_health_cron.py controlla solo che le sessioni tmux siano vive
(self-healing da crash), non quanto pesano insieme. Manfredo ha chiesto
esplicitamente una gestione ottimale del carico affidata a Betty (non un
nuovo agente separato): questo script si limita a MISURARE e AVVISARE Betty
direttamente nella sua finestra tmux — RAM bassa = allerta di EMERGENZA,
carico alto SOSTENUTO (media 5 min) = avviso informativo (vedi soglie sotto);
la decisione su cosa mettere in pausa/riattivare resta a lei, non qui.
"""
import os
import subprocess
import sys
import time
import re

# DEV-069 (08/08/2026): alert_betty usava tmux load-buffer/paste-buffer/
# send-keys diretto, senza nessuna verifica che il paste fosse davvero
# arrivato — esattamente il bug del paste-non-sottomesso gia' documentato in
# AGENTS.md. Prova concreta: ~10 allerte "inviate" in una notte, zero
# arrivate davvero nella conversazione di Betty (log del servizio diceva
# sempre successo). Fix: stesso meccanismo verificato/con retry del motore
# Telegram, importato direttamente (stessa cartella, nessun subprocess).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from send_a2a import send_a2a

CHECK_INTERVAL = 90       # secondi tra un controllo e il successivo
RAM_FREE_MB_THRESHOLD = 1200   # sotto questa soglia di RAM disponibile -> allerta (emergenza)
# Il carico si allerta solo se SOSTENUTO (media 5 minuti), non sul picco a 1
# minuto: su questa macchina (6 core, 15-20 agenti attivi) load 12-15 e' il
# regime NORMALE anche a riposo. Il 05/08/2026 il watchdog ha mandato 17
# allerte quasi tutte per picchi 1-min tra 12 e 16 rientrati da soli in pochi
# minuti — un'allerta che suona sempre smette di informare e quelle VERE si
# perdono in mezzo. Soglia tarata sui dati di quel giorno: rumore fino a ~16,
# burst reali a 22-38 (taratura di Betty, implementazione Elisa).
# ALZATA di nuovo lo stesso giorno (Betty): con ~23 agenti attivi il regime di
# esercizio normale sta gia' fra 20 e 33 - 18 e' scattato 4 volte in un'ora e
# mezza, ogni volta senza nulla di rotto (verificato a mano ogni volta:
# consegne Telegram ok, radio viva, 23 agenti sani, I/O all'1,7%). MA alzare
# il numero da solo non basta (vedi rileva_danno_osservabile sotto): il
# carico da solo, su questa macchina, non e' un'informazione azionabile.
LOAD5_THRESHOLD = 30
# Pressione I/O (Linux PSI, /proc/pressure/io): % di tempo in cui almeno un
# processo era bloccato in attesa di I/O, media 10s - un segnale diretto di
# "lo swap sta facendo davvero male", molto piu' preciso dello swap usato in
# se' (che puo' restare alto e stabile senza causare nessuno stallo reale).
IO_PRESSURE_THRESHOLD = 10
ALERT_COOLDOWN = 900           # non ri-allertare per la stessa condizione prima di 15 min
BETTY_WINDOW = "samantha"


def get_stats():
    free_out = subprocess.getoutput("free -m")
    avail_match = re.search(r"Mem:\s+\d+\s+\d+\s+\d+\s+\d+\s+\d+\s+(\d+)", free_out)
    avail_mb = int(avail_match.group(1)) if avail_match else None

    uptime_out = subprocess.getoutput("uptime")
    # 1-min E 5-min: l'allerta carico usa la media 5 minuti (carico sostenuto,
    # vedi LOAD5_THRESHOLD), il picco 1-min resta solo per il messaggio.
    load_match = re.search(r"load average:\s*([\d.]+),\s*([\d.]+)", uptime_out)
    load1 = float(load_match.group(1)) if load_match else None
    load5 = float(load_match.group(2)) if load_match else None

    return avail_mb, load1, load5


def top_sessions_by_mem():
    """Elenco sessioni tmux ordinate per RAM aggregata dei loro processi, per
    dare a Betty un punto di partenza su cosa mettere in pausa senza doverlo
    ricalcolare lei stessa ogni volta."""
    try:
        panes = subprocess.getoutput(
            "tmux list-panes -a -F '#{session_name} #{pane_pid}'"
        ).splitlines()
        session_mem = {}
        for line in panes:
            parts = line.split()
            if len(parts) != 2:
                continue
            session, pid = parts
            rss_out = subprocess.getoutput(
                f"ps --ppid {pid} -o rss= 2>/dev/null; ps -o rss= -p {pid} 2>/dev/null"
            )
            total_kb = sum(int(x) for x in rss_out.split() if x.strip().isdigit())
            session_mem[session] = session_mem.get(session, 0) + total_kb
        ranked = sorted(session_mem.items(), key=lambda kv: -kv[1])[:5]
        return ", ".join(f"{s} (~{mb // 1024}MB)" for s, mb in ranked if mb > 0)
    except Exception:
        return "non disponibile"


def rileva_danno_osservabile():
    """Proposta di Betty (05/08/2026): il carico da solo non e' azionabile su
    questa macchina, va accompagnato da un danno VERO per giustificare
    un'allerta. Due segnali controllati qui, entrambi gia' disponibili senza
    inventare nulla di nuovo:
    - agenti genuinamente bloccati (non solo spenti) secondo check_agenti_vivi.py;
    - pressione I/O (PSI) sopra soglia, il segnale diretto di swap in sofferenza.
    Le consegne Telegram fallite andrebbero incluse (terzo segnale proposto da
    Betty) ma non esiste oggi un log/metrica pulita da leggere per quello
    (telegram.log e' vuoto, non e' uno storico degli invii) - lasciato fuori
    deliberatamente invece di inventare un proxy debole che sembra un segnale
    vero e non lo e'.
    Ritorna una lista di stringhe (motivi trovati) - vuota se nessun danno."""
    motivi = []
    try:
        out = subprocess.run(
            ["/root/ecosistema_agenti/core/venv/bin/python3",
             "/root/ecosistema_agenti/core/check_agenti_vivi.py"],
            capture_output=True, text=True, timeout=30,
        ).stdout
        m = re.search(r"ANOMALI:\s*(\d+)", out)
        if m and int(m.group(1)) > 0:
            motivi.append(f"{m.group(1)} agenti anomali secondo check_agenti_vivi.py")
    except Exception:
        # Il controllo stesso non e' riuscito a girare: NON e' una prova di
        # danno, quindi non entra in 'motivi' (altrimenti ogni errore dello
        # script diventerebbe un'allerta falsa) - resta muto per questo giro.
        pass
    try:
        with open("/proc/pressure/io", encoding="utf-8") as f:
            riga = f.readline()
        m = re.search(r"some avg10=([\d.]+)", riga)
        if m and float(m.group(1)) > IO_PRESSURE_THRESHOLD:
            motivi.append(f"pressione I/O al {m.group(1)}% (soglia {IO_PRESSURE_THRESHOLD}%) — swap probabilmente in sofferenza")
    except Exception:
        pass
    return motivi


def alert_betty(avail_mb, load1, load5, kind, danno=None):
    top = top_sessions_by_mem()
    if kind == "ram":
        # Emergenza vera: tono urgente, azione richiesta.
        msg = (
            f"🚨 [RAM CRITICA - resource_watchdog] RAM disponibile {avail_mb}MB, sotto la soglia "
            f"di {RAM_FREE_MB_THRESHOLD}MB (load 1-min {load1}).\n"
            f"Sessioni tmux che pesano di piu' ora: {top}\n"
            f"EMERGENZA: valuta SUBITO di mettere in pausa uno studio non urgente."
        )
    else:
        # Non piu' solo "carico alto": arriva qui SOLO se accompagnato da un
        # danno osservabile confermato (vedi rileva_danno_osservabile) - il
        # carico da solo su questa macchina non e' azionabile (Betty,
        # 05/08/2026, dopo 4 allerte innocue in un'ora e mezza). Tono
        # aggiornato di conseguenza: non e' piu' "magari niente", e' "carico
        # alto CON un motivo vero per guardare".
        motivi_str = "; ".join(danno) if danno else "motivo non specificato"
        msg = (
            f"⚠️ [CARICO ALTO CON DANNO OSSERVABILE - resource_watchdog] load a 5-min {load5} "
            f"sopra la soglia {LOAD5_THRESHOLD} (1-min: {load1}), RAM {avail_mb}MB.\n"
            f"Motivo: {motivi_str}\n"
            f"Sessioni tmux che pesano di piu' ora: {top}\n"
            f"A differenza di prima, questa allerta e' gia' correlata a un danno vero - vale la pena guardare."
        )
    # send_a2a() gestisce da sola nomi univoci di buffer/file, verifica che
    # la generazione sia partita davvero e riprova fino a 4 volte (mai un
    # C-m isolato) — sostituisce l'intera sequenza manuale load-buffer/
    # paste-buffer/send-keys di prima, che non verificava nulla.
    ok = send_a2a(BETTY_WINDOW, msg)
    label = "RAM CRITICA" if kind == "ram" else "carico alto con danno osservabile"
    if ok:
        print(f"[Watchdog Risorse] Allerta CONFERMATA a Betty ({label}: RAM {avail_mb}MB, load1 {load1}, load5 {load5})", flush=True)
    else:
        print(f"[Watchdog Risorse] ⚠️ Allerta NON confermata dopo i tentativi ({label}: RAM {avail_mb}MB, load1 {load1}, load5 {load5}) — testo comunque incollato nell'input box di Betty, verificare a mano", flush=True)


def main():
    # Cooldown separati per tipo: un'allerta di carico non deve mai
    # silenziare un'emergenza RAM arrivata poco dopo (prima il timestamp unico
    # le metteva in competizione).
    last_ram_alert = 0.0
    last_load_alert = 0.0
    while True:
        avail_mb, load1, load5 = get_stats()
        now = time.time()
        if avail_mb is not None and avail_mb < RAM_FREE_MB_THRESHOLD \
                and (now - last_ram_alert) > ALERT_COOLDOWN:
            alert_betty(avail_mb, load1, load5, "ram")
            last_ram_alert = now
        if load5 is not None and load5 > LOAD5_THRESHOLD \
                and (now - last_load_alert) > ALERT_COOLDOWN:
            # Il carico da solo non basta piu' (proposta di Betty, 05/08/2026):
            # controlla anche un danno osservabile prima di allertare. Se non
            # ne trova nessuno, il giro passa in silenzio - il prossimo check
            # (90s dopo) puo' ancora trovarne uno se il carico persiste.
            danno = rileva_danno_osservabile()
            if danno:
                alert_betty(avail_mb, load1, load5, "load", danno)
                last_load_alert = now
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
