# Development Studio — Active Backlog & Bug Tracker

## 🛠️ Sprint Backlog

| ID | Issue / Feature | Priority | Status | Assigned Exec | Verification Criteria |
|---|---|---|---|---|---|
| #201 | Postgres JSONB key deletion patch | High | 🟢 Resolved | `exec_fix_pg` | Unit test 3/3 PASS |
| #202 | MCP Lazy Initialization Refactor | Medium | 🟢 Resolved | `exec_mcp_lazy` | Handshake < 1.0s |
| #203 | Non-blocking CPU% calculation | Low | 🟢 Resolved | `exec_cpu_fix` | Telemetry loop jitter < 50ms |
| #204 | WebSocket reconnection backoff | Medium | ⚪ Backlog | — | Exponential jitter test |

---

## 📌 Engineering Protocol
- Always create a local backup (`.bak_pre_*`) before editing existing scripts.
- Never deploy code changes to long-running services without an explicit daemon restart.
- Use isolated mock test runners in `/tmp/tests/` before declaring fixes production-ready.
