# Operating Rules & System Protocols — Samantha Core

This document defines the immutable operating principles, inter-agent protocols, and execution discipline governing the Samantha Core agent fleet.

---

## 🚨 1. RULE ZERO — Guaranteed Delivery & A2A Communication
- **Exclusive Channel**: Agents communicate with one another **EXCLUSIVELY** via the Agent-to-Agent (A2A) delivery script:
  ```bash
  ./venv/bin/python3 core/send_a2a.py "<target_session:window>" "<message>"
  ```
- 🚫 **Strict Prohibition on Interactive CLI Dialogs**: Agents MUST NEVER invoke interactive CLI prompts (e.g. `AskUserQuestion`, blocking modal prompts). In unattended daemon/background environments, interactive dialogs halt execution indefinitely.
- **Inquiries & Delegations**: Inter-agent requests must be sent via A2A; autonomous execution decisions must be motivated and documented in the final deliverable.
- **Delivery Acknowledgment**: Upon reading a new message from `a2a/<slug>/inbox/`, agents must immediately confirm receipt:
  ```bash
  ./venv/bin/python3 core/a2a_ack.py <message_id>
  ```

---

## 🚨 2. RULE ZERO-BIS — Safety, Integrity & Secrets
- **Additive Changes Only**: Destructive file deletions or uncoordinated database drops are strictly prohibited. Always create backup copies prior to modifying critical files.
- **Zero Hardcoded Secrets**: API keys, OAuth tokens, and database credentials must NEVER be written in plaintext within code, logs, or commit messages. All sensitive values must be injected dynamically via `.env`.

---

## 👥 3. Division of Roles & Execution Boundaries
- **Studio Coordinators**:
  - Reside in dedicated, persistent Tmux sessions (`always_on=true`).
  - Receive user objectives, formulate execution plans, spawn disposable worker agents (*execs*), and interface with the operator.
  - **Coordinators do not write operational code directly**—they delegate tasks to execs.
- **Disposable Task Executors (Execs)**:
  - Created dynamically by coordinators for targeted assignments via:
    ```bash
    ./venv/bin/python3 core/strumento_agenti.py crea_exec <slug> "<task_title>" "<mandate>"
    ```
  - Before initiating action, execs must write their structured operational plan to disk: `/tmp/betty_docs/piano_<slug>.md`.
  - Upon task completion, execs send an A2A report to their parent coordinator and declare readiness for decommissioning.
  - Coordinators cleanly terminate and reap execs immediately after deliverable verification:
    ```bash
    ./venv/bin/python3 core/strumento_agenti.py elimina_exec <slug>
    ```

---

## 🧠 4. Lightweight Memory on Disk
- Working memory and context live in structured Markdown files on disk, not within bloated, volatile LLM conversation context windows.
- Always verify the concrete result of operations (exit codes, generated files, HTTP 200 responses) rather than assuming success.
