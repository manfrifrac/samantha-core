# Agent Lifecycle, Process State & Recovery

> **Scope**: Safe agent creation, persistent vs ephemeral lifecycles, session resumption, self-healing daemons, and state preservation.

---

## ♻️ Agent Creation & Lifecycle Management

All agent lifecycle operations (creation, startup, shutdown, and migration) are managed through standardized tools:

```bash
# Create an ephemeral development executor
python3 core/strumento_agenti.py crea_exec --profilo sviluppatore --nome <slug> --sessione <session>

# Terminate an ephemeral executor
python3 core/strumento_agenti.py elimina_exec --slug <slug>

# Manage persistent studio agents
python3 core/strumento_agenti.py accendi_agente --slug <slug>
python3 core/strumento_agenti.py spegni_agente --slug <slug>
```

---

## 🛡️ Persistent vs Ephemeral Agents

| Property | Persistent Studio Agent | Ephemeral Dev Executor |
|---|---|---|
| **Identity & DB** | Permanent row in PostgreSQL `agents` table | Temporary tracked row with `always_on=false` |
| **Workspace** | Dedicated Studio directory (`studios/<studio>/`) | Isolated coordinator subfolder / scratch workspace |
| **State File** | Persistent Obsidian file (`Progetti/<slug>_stato.md`) | Ephemeral report delivered to `/tmp/docs/` |
| **Telegram Bot** | Registered bot key & dedicated topic thread | No bot key; reports exclusively via A2A |
| **Teardown** | Runs indefinitely (self-healing managed) | Explicitly terminated immediately upon task completion |

---

## 🚨 Silent Memory Loss Prevention

Improper CLI invocations can silently overwrite conversation history without throwing errors:

1. **Explicit Conversation ID (`--resume`)**:
   - Resuming an agent CLI session must always specify the registered `conv_id` stored in the database. Omitting the conversation ID creates a blank conversation isolate while leaving the database out of sync.
2. **The `always_on` Self-Healing Rule**:
   - The self-healing supervisor scans the database every 5 minutes. If a persistent agent marked `always_on=true` is stopped manually without updating the database, the supervisor will automatically relaunch it. Always use `spegni_agente` to update persistence flags cleanly.
