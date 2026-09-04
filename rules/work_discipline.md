# Operational Discipline, Empirical Verification & Dashboards

> **Scope**: Empirical verification standards, disk-first intelligence retrieval, task planning, and persistent topic dashboards.

---

## 🔍 Empirical Verification Standards

1. **Never Assume Success Without Proof**:
   - Executing a command or dispatching an A2A message is not proof of successful execution.
   - Always verify concrete outcomes: check process exit codes, inspect generated file contents, query network HTTP status codes, or verify database mutations.
2. **Disk-First Intelligence**:
   - Before waking or querying an idle agent via A2A, check whether the requested information is already persisted on disk (e.g. state files, delivery reports, transcripts, system logs).
   - Reading disk files costs zero LLM context tokens for the target agent; waking an idle coordinator incurs full context reload overhead.

---

## 📌 Coordinator Dashboards & Topic Task Lists

1. **Persistent Task Lists**:
   - Studio Coordinators maintain an active task dashboard synchronized with their ground-truth Obsidian project trackers.
   - Pinned to the top of the coordinator's Telegram topic thread using the `[PIN_MESSAGE]` tag.
2. **Real-Time Progress State**:
   - Dashboard items must reflect granular progress states (e.g., `[IN_PROGRESS 60%]`, `[BLOCKED]`, `[VERIFIED]`), rather than static labels.
   - Consulted at every incoming user message to maintain strategic alignment.
