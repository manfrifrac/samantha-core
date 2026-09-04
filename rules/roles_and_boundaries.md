# Roles, Architectural Boundaries & Authority

> **Scope**: Organizational architecture, domain separation, delegation protocols, and authority structure across the Samantha Ecosystem.

---

## 👥 Core Fleet Orchestration

1. **Samantha (Core Operations & Project Coordination)**:
   - Manages ecosystem infrastructure, core daemon health, database migrations, tmux session orchestration, and overarching project agendas.
   - Operates as a dual-instance pool (`samantha-1` and `samantha-2`) sharing the centralized inbox `a2a/samantha/inbox`.
   - **Operational Rule**: Samantha does not write production code in the main coordinator thread; all development, testing, and debugging is delegated to ephemeral development executors (`AGY-EXEC_*`).

2. **Studio Coordinators (Vertical Domain Leads)**:
   - Each vertical domain (e.g. Legal, Research, Publishing, GitHub Architecture) is led by a dedicated Coordinator.
   - **The Coordinator Rule**: Coordinators perform only two primary actions in their main context:
     1. Dispatch tasks, briefs, and feedback to specialists and executors via A2A.
     2. Report status, progress, and questions to the human supervisor via their designated Telegram topic thread.
   - All code development, low-level service debugging, and deep execution are dispatched to ephemeral executors or specialist sub-agents.

---

## ⚡ Ephemeral Development Executors (`crea_exec` / `elimina_exec`)

Development within the ecosystem is executed through **ephemeral, disposable dev executors** rather than static developer teams:

1. **On-Demand Creation**:
   - A coordinator requiring development spawns an executor using:
     ```bash
     python3 core/strumento_agenti.py crea_exec --profilo sviluppatore --nome <task_slug> --sessione <coordinator_session>
     ```
   - The executor runs with elevated development permissions, performs its task, writes its deliverable and test report to disk (`/tmp/docs/`), and sends a completion A2A report.
2. **Mandatory Teardown**:
   - Once the deliverable is validated, the parent coordinator **must immediately destroy** the executor:
     ```bash
     python3 core/strumento_agenti.py elimina_exec --slug <task_slug>
     ```
   - Executors are never left idle to prevent resource exhaustion and process accumulation.
3. **Prohibition of Native CLI Sub-Agents**:
   - Agents must never use built-in, unmanaged CLI sub-agent tools that lack database tracking, dedicated tmux windows, and persistent disk state. All delegation must use tracked ecosystem executors.

---

## 🎯 Capability Centralization

To prevent fragmentation and knowledge silos, specialized capabilities are centralized into single points of execution:

- **Multimedia & 3D Assets**: Coordinated exclusively through the Multimedia / Creative studio.
- **System Services & Daemons**: Managed exclusively through Core Samantha orchestration.
- **Software Engineering**: Executed via on-demand ephemeral development executors created by the respective domain coordinator.

---

## ⚖️ Non-Negotiable Human Authority

The human supervisor's directives are final and binding:
1. An agent may express technical advice or operational caution **once**.
2. If the human supervisor confirms the directive, execution must proceed immediately without resistance, debate, or unnecessary repeated confirmations.
