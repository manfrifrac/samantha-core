# Database State, Knowledge Vault & Integration Architecture

> **Scope**: Dual-tier storage architecture, PostgreSQL runtime state management, Markdown Knowledge Vault, and indexing standards.

---

## 💾 Dual-Tier Storage Architecture

The Samantha Ecosystem enforces clean separation between runtime transactional state and long-term analytical intelligence:

```
+--------------------------------------------------------------------+
|                   DUAL-TIER DATA ARCHITECTURE                      |
+------------------------------------+-------------------------------+
|       TRANSACTIONAL STATE          |     ANALYTICAL KNOWLEDGE      |
|       (PostgreSQL Database)        |    (Markdown Obsidian Vault)  |
+------------------------------------+-------------------------------+
| - Table 'agents' (JSONB Registry)  | - Long-term project memory    |
| - Table 'a2a_silent_logs' (Ledger) | - System skill documentation  |
| - Table 'studi' (Domain Workspaces)| - Structured deliverables     |
| - High-throughput IPC transactions | - Human-readable audit trails |
+------------------------------------+-------------------------------+
```

---

## 🗄️ PostgreSQL Runtime Conventions

1. **JSONB Evolution & Upserts**:
   - The `agents` table schema utilizes a primary key `agent_id` with a JSONB `data` column.
   - Updates must use the `||` merge operator with explicit removal keys (`data - 'key'`) when deleting fields to prevent zombie flags.
2. **Deterministic Timestamp Ordering**:
   - Never sort date fields stored as text lexicographically. Always use numeric epoch columns or structured ISO `TIMESTAMP WITH TIME ZONE` (`ORDER BY timestamp DESC`).
3. **Database Portability**:
   - All connection factories must resolve credentials dynamically via `DATABASE_URL` or `core/secret_env.py` rather than hardcoding credentials.

---

## 🏛️ Network Architecture & Single HTTPS Ingress

1. **Unified HTTPS Reverse Proxy**:
   - Web applications, APIs, and media streams are routed exclusively through Nginx with SSL termination.
   - Third-party ephemeral tunnels (e.g. ngrok, public SaaS tunnels) are strictly prohibited on production endpoints.
