# Cross-Engine Agent Migration Checklist

> **Scope**: Safe agent engine migration protocol (`claude` ↔ `agy` ↔ `qwen`) preventing silent state erasure, configuration loss, and tool disconnects.

---

## 🔑 Core Principle: Engine Differences are Silent

Different AI CLI harnesses have non-obvious, silent behavioral differences. Configuration errors rarely throw immediate fatal exceptions; instead, they result in an agent that appears running but has silently lost its memory history or tool bindings.

| Property | `claude` (Claude Code) | `agy` (Antigravity) | `qwen` |
|---|---|---|---|
| **Studio `.mcp.json`** | ✅ **Parsed** — True studio isolation | 🚨 **Ignored** — Global config only (`settings.json`) | ✅ Parsed |
| **Postgres `model` field** | Active via adapter | ✅ **Active** (`--model=<val>`) | Inactive |
| **Non-existent `conv_id`** | `--resume` falls back gracefully | 🚨 **Creates a new ID** silently dropping conversation context | Varies |

---

## 📋 Pre-Migration Checklist (Before Touching State)

1. **Verify State Persistence (Do Not Rely on Memory)**:
   - Command the agent to save its current memory to `Obsidian-Vault/Progetti/<slug>_stato.md` and read it back.
   - Verify the file's `mtime` on disk. An engine migration resets the active context; unpersisted data will be lost.
2. **Postgres `work_dir` == Process `cwd`**:
   - Ensure the database `work_dir` matches the exact physical directory.
3. **Do Not Mutate `tmux_window`**:
   - The Telegram topic routing engine resolves destination windows by window identifier. Changing window names breaks message delivery.
4. **Audit MCP Tool Access**:
   - When migrating an agent to `claude`, verify that all required MCP tools are declared in that studio's local `.mcp.json`.

---

## ⚙️ Migration Execution

5. **Generate a Fresh `conv_id`**:
   - Never reuse a conversation UUID from a different engine.
   - Archive the previous `conv_id` in a timestamped backup key (`conv_id_pre_migration_YYYYMMDD`).
6. **Pre-Create Studio `.mcp.json`**:
   - MCP configurations are frozen at CLI startup; runtime additions have no effect on active sessions.

---

## 🔍 Post-Migration Verification (Empirical Proof)

7. **Verify Real Engine Binary (Not Shell)**:
   - Check `ps -o comm= -p <PID>` to verify the process is executing the true CLI binary, not an idle bash prompt.
8. **Verify MCP Subprocesses**:
   - Inspect child processes (`ps --ppid <engine_pid>`) to confirm standard I/O MCP servers spawned successfully.
9. **Verify Log Confirmation**:
   - Check CLI log streams to ensure `--conversation` UUID was recognized without fallback instantiation.
10. **Verify Transcript Growth**:
    - Take two snapshot reads of the conversation transcript file to confirm continuous active telemetry.
