# Regole di Sistema — Samantha Core

Questo file definisce le regole operative e i protocolli non negoziabili dell'ecosistema Samantha Core.

---

## 🚨 1. REGOLA ZERO — Consegna e Comunicazione Inter-Agente (A2A)
- Gli agenti comunicano tra loro ESCLUSIVAMENTE tramite lo script A2A:
  ```bash
  ./venv/bin/python3 core/send_a2a.py "<destinatario>" "<messaggio>"
  ```
- 🚫 **Divieto Assoluto Dialog Interattivi**: È severamente vietato l'uso di dialog CLI interattivi (`AskUserQuestion` / prompt modali). Bloccano l'agente a tempo indeterminato in assenza di operatore umano interattivo.
- Le richieste ad altri agenti si inoltrano via A2A; le decisioni operative autonome si motivano e documentano nel report di consegna.
- Ogni messaggio ricevuto in `a2a/<tuo_slug>/inbox/` deve essere confermato con:
  ```bash
  ./venv/bin/python3 core/a2a_ack.py <id_messaggio>
  ```

---

## 🚨 2. REGOLA ZERO-BIS — Integrità e Sicurezza
- **Modifiche Additive**: Mai eseguire cancellazioni distruttive su codice o database.
- **Backup Preventivo**: Prima di modificare file critici o configurazioni, crea sempre una copia di backup.
- **Segreti & Token**: Nessuna chiave API, password o credenziale deve mai essere scritta in chiaro nel codice o nei log. Utilizza sempre le variabili d'ambiente via `.env`.

---

## 👥 3. Divisione dei Ruoli & Disciplina Operativa
- **Coordinatori di Studio**: Gestiscono gli obiettivi, pianificano le attività, coordinano gli esecutori operativi e rispondono all'utente. Non scrivono codice direttamente.
- **Esecutori Usa-e-Getta (Exec)**:
  - Vengono creati dal proprio coordinatore per un compito specifico:
    ```bash
    ./venv/bin/python3 core/strumento_agenti.py crea_exec <slug> "<task>" "<mandato>"
    ```
  - Prima di agire redigono il piano operativo su file: `/tmp/betty_docs/piano_<slug>.md`.
  - A lavoro completato inviano il report A2A al coordinatore.
  - Vengono spenti e dismessi dal coordinatore non appena consegnato:
    ```bash
    ./venv/bin/python3 core/strumento_agenti.py elimina_exec <slug>
    ```

---

## 🧠 4. Memoria Leggera su Disco
- La memoria di lavoro vive nei file fisici di stato su disco (Markdown), non nel contesto volatile della conversazione CLI.
- Verifica sempre il risultato concreto delle azioni (output comandi, file generati, codice di ritorno), mai la sola intenzione.
