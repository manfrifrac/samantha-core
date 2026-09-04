#!/bin/bash
# WRAPPER PERIODICO per check_a2a_delivery_coverage.py (task Leo, 10/08/2026,
# "AFFIDABILITA' DELLA CONSEGNA MESSAGGI SU DEEP CODE" — dominio di Elisa).
# Stesso schema di check_agenti_stuck.sh: flock single-instance, dedup su
# stato per non ri-allertare la STESSA anomalia ad ogni giro del cron
# (ecosystem_health_cron.py lo richiama ogni ciclo del loop principale).
#
# 17/08/2026 (Samantha): l'alert andava a ELISA dello Studio Leo — dismesso
# l'11/08 insieme a tutti i suoi agenti. Elisa non esiste piu' in Postgres,
# quindi send_a2a rifiutava ("non corrisponde a nessun agente registrato") e
# ecosystem_health_cron.py ci ribatteva addosso ad OGNI ciclo: 22 tentativi
# falliti nel solo pane visibile, log inquinati e CPU sprecata (27% su un
# core). Trovato dall'exec presidio_stato. Destinatario spostato su SAMANTHA,
# che ha ereditato il dominio infrastruttura dopo la dismissione dello Studio.
# 🔑 Quando si dismette uno studio, i destinatari HARDCODED negli script di
# allerta non si aggiornano da soli: vanno cercati (grep dello slug) e
# reindirizzati, altrimenti restano a bussare a una porta murata.

LOCKFILE="/root/ecosistema_agenti/core/check_a2a_delivery_alert.lock"
exec 9>"$LOCKFILE"
if ! flock -n 9; then exit 0; fi

CORE=/root/ecosistema_agenti/core
STATE=/tmp/check_a2a_delivery_alert_state

OUT=$("$CORE/venv/bin/python3" "$CORE/check_a2a_delivery_coverage.py" 2>&1)
RC=$?

if [ "$RC" -eq 0 ]; then
  # sano: nessun alert, e si resetta lo stato cosi' una FUTURA anomalia
  # (anche identica a una gia' vista in passato) viene ri-segnalata.
  rm -f "$STATE"
  exit 0
fi

# 17/08/2026 sera (Samantha): la firma era l'md5 dell'OUTPUT INTERO, che
# contiene il contatore "N agenti censiti" e l'elenco completo degli slug del
# relay — entrambi cambiano ad OGNI movimento a DB (un exec creato, un record
# dismesso) anche quando le anomalie vere sono IDENTICHE. Risultato: 4 alert
# uguali in una sera di lavoro sul DB, un turno di Samantha bruciato l'uno.
# Ora si firmano SOLO le righe di anomalia, esclusa quella della lista slug
# (dichiarata "race fra le due letture" dallo script stesso: rumore by design).
FIRMA=$(echo "$OUT" | grep "⚠️" | grep -v "_dc_slug_set() del relay include" | md5sum | cut -d' ' -f1)
if [ -f "$STATE" ] && grep -qxF "$FIRMA" "$STATE"; then
  exit 0  # stessa identica anomalia gia' segnalata, non ripetere ad ogni giro
fi
echo "$FIRMA" > "$STATE"

cd "$CORE" || exit 1
./venv/bin/python3 send_a2a.py "betty:agy-Samantha" "[A2A_FROM:check_a2a_delivery] [A2A_TYPE:report] Controllo periodico copertura consegna A2A — trovate anomalie:

${OUT}"
exit 0
