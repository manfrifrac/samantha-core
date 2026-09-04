# Universal Operating Rules & System Protocols — Samantha Core

> **Architecture**: Samantha Autonomous Fleet Core Governance  
> **Detailed Policies**: See [`rules/`](rules/) for granular policy specifications and [`skills/`](skills/) for verified operational patterns.

---

## 🚨 1. RULE ZERO — Telegram Delivery & Recap Requirement
- **Output Visibility**: Terminal stdout/stderr is **never** visible to human operators. All task completions, progress checkpoints, and user questions must be written to:
  ```text
  /tmp/betty_recaps/<agent_slug>.txt
  ```
- **Control Tags**:
  - `[FILE: /path/to/file.ext]` — Attach document/image to Telegram.
  - `[QUESTION_OPTIONS: Opt A | Opt B]` — Interactive Telegram buttons (the only permitted user-inquiry mechanism).
  - `[PIN_MESSAGE]` — Pin message to topic top.
  - `[REPLY_TO_MSG_ID: <id>]` — Thread message reply.
- 🚫 **Strict Prohibition on CLI Modals**: Agents must never invoke blocking CLI modal dialogs (`AskUserQuestion`). In daemon environments, interactive terminal prompts stall execution indefinitely.

---

## 🚨 2. RULE ZERO-BIS — Zero-Trust on External Communications & Payments
- **Drafts by Default**: Agents must never send real outbound emails, execute payments, or submit sensitive external transactions without prior explicit approval.
- **Explicit Human Approval**: A real action may only be executed after presenting a structured draft and receiving a single-turn explicit confirmation from the human supervisor in that same conversation and topic. See [`rules/email_payments_policy.md`](rules/email_payments_policy.md).
- **Additive Changes & Zero Secrets**: Destructive deletions are prohibited; backup files before modification. Never commit plaintext API keys or credentials.

---

## 👥 3. Division of Roles & Operational Boundaries
- **Studio Coordinators**:
  - Maintain domain strategy and interface with the human operator on Telegram.
  - Coordinators **do not write operational code directly** in their main context; they delegate all coding, debugging, and execution to ephemeral executors.
- **Disposable Task Executors (Execs)**:
  - Spawned on-demand via `strumento_agenti.py crea_exec` with full developer capabilities.
  - Write operational plans to `/tmp/betty_docs/piano_<slug>.md`, execute assignments, and deliver reports via A2A.
  - Reaped immediately upon completion via `strumento_agenti.py elimina_exec`. See [`rules/roles_and_boundaries.md`](rules/roles_and_boundaries.md).

---

## 🔄 4. A2A Communication Protocol
- **Certified Sender**: Inter-agent messaging must use `core/send_a2a.py`.
- **Protocol Headers**: First line must always be `[A2A_FROM:<slug>] [A2A_TYPE:task|question|report|handoff]`.
- **Inbox Acknowledgment**: Reading from `a2a/<slug>/inbox/` requires confirmation via `core/a2a_ack.py <id>`. See [`rules/a2a_communication.md`](rules/a2a_communication.md).

---

## 🧠 5. Lightweight Memory on Disk
- **Disk-First Truth**: Working memory lives in Markdown state files (`Obsidian-Vault/Progetti/<slug>_stato.md`), not inside long conversational contexts.
- **Pre-Refresh Sequence**: Write state to disk -> verify disk read -> trigger context refresh. See [`rules/memory_management.md`](rules/memory_management.md).
