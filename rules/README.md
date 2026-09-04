# System Governance, Safety & Architecture Rules

> **Directory**: `rules/`  
> **Ecosystem**: Samantha Autonomous Fleet Architecture  
> **Format Constraint**: Strict Markdown documentation (`.md`). Shared across all autonomous studios, coordinators, and ephemeral dev executors.

---

## 🏛️ System Core Policies Overview

The Samantha Autonomous Ecosystem operates under strict multi-tiered governance rules designed to ensure zero-data loss, absolute human oversight over sensitive actions, lightweight token consumption, and seamless inter-agent collaboration.

### The Fundamental Axioms

1. **Rule Zero — The Recap Rule**: Terminal CLI outputs are never seen directly by human supervisors. Every task completion or phase handoff must write its natural language summary directly to `/tmp/betty_recaps/<agent_slug>.txt` for delivery to the dedicated Telegram topic thread.
2. **Rule Zero-Bis — Zero-Trust on External Communications & Payments**: Agents must never send real emails, submit non-draft financial transactions, or perform irreversible external actions without prior explicit human approval on a structured draft.
3. **Strict Separation of Concerns**: Studio Coordinators coordinate domains and communicate with humans. Ephemeral development executors handle coding and system repairs, and are destroyed immediately upon task completion.
4. **Lightweight Memory Architecture**: Agents live on persistent disk storage (`Obsidian-Vault/Progetti/<slug>_stato.md`), not inside long-lived LLM context windows.

---

## 🗂️ Governance & Policy Index

| File | Policy Domain | Scope & Core Directive |
|---|---|---|
| [`roles_and_boundaries.md`](./roles_and_boundaries.md) | **Roles & Boundaries** | Studio boundaries, coordinator duties, ephemeral executor creation, non-negotiable human authority. |
| [`email_payments_policy.md`](./email_payments_policy.md) | **Email & Payments Security** | Zero-trust protocol: draft-first rule, single-turn human confirmation exception, and identity protection. |
| [`telegram_rules.md`](./telegram_rules.md) | **Telegram Mobile UX & Tags** | Tag specifications (`[FILE:]`, `[QUESTION_OPTIONS:]`, `[PIN_MESSAGE]`, `<VOICE>`), anti-table layout rules. |
| [`a2a_communication.md`](./a2a_communication.md) | **Agent-to-Agent (A2A) Protocol** | Inbox filesystem deposition, atomic A2A acknowledgments, named tmux buffer isolation, lightweight payloads. |
| [`agent_lifecycle.md`](./agent_lifecycle.md) | **Agent Lifecycle & Recovery** | Agent creation, bootstrap sequence, persistent vs ephemeral classification, tmux recovery, self-healing. |
| [`memory_management.md`](./memory_management.md) | **Lightweight Memory Protocol** | Disk-persisted state, context refresh triggers, token budgeting, and knowledge base accumulation. |
| [`work_discipline.md`](./work_discipline.md) | **Work Discipline & Verification** | Empirical verification before assumption, live testing before delivery, structured Obsidian dashboards. |
| [`engines_and_mcp.md`](./engines_and_mcp.md) | **Engines, CLI & MCP Standards** | Native CLI transparency, multi-tier model cascading, lazy MCP server initialization. |
| [`standard_profiles.md`](./standard_profiles.md) | **Standard Agent Profiles** | Persona archetypes (Coordinator, Researcher, Developer, Dispatcher), permission groups, and tool bindings. |
| [`browser_automation_policy.md`](./browser_automation_policy.md) | **Browser Automation Policy** | Headless server isolation (`:9224`), Human-in-the-Loop CAPTCHA handoff, session tab garbage collection. |
