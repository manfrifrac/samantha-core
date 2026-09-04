# Studio Architecture & Multimodal Workspaces

> **Directory**: `studios/`  
> **Architecture**: Samantha Autonomous Fleet Domain Workspaces  
> **Governance Constraint**: Every studio coordinator enforces [`rules/roles_and_boundaries.md`](../rules/roles_and_boundaries.md) and delegates execution to ephemeral dev executors.

---

## 🏛️ What is a Studio?

In the Samantha Core architecture, a **Studio** is an isolated vertical operational workspace dedicated to a specific functional domain (e.g., Deep Web Research, Software Engineering, Multimedia Production, Legal Intelligence).

Each Studio consists of:
1. **Dedicated Working Directory**: An isolated folder (`studios/<studio_name>/`) containing domain assets, scripts, scratch outputs, and local tool configs.
2. **Dedicated Tmux Session**: A dedicated POSIX terminal session named `<studio_name>` hosting the Studio Coordinator and child executor panes.
3. **Studio Coordinator**: A persistent agent (`always_on=true`) registered in PostgreSQL that manages domain objectives, maintains the studio task tracker, communicates with the operator on Telegram, and spawns ephemeral executors.
4. **Symlinked Rules Baseline**: A symlink `CLAUDE.md -> ../../AGENTS.md` ensuring all agents within the studio inherit universal operating policies and Rule Zero without duplication.
5. **Local MCP Tool Registry (`.mcp.json`)**: Domain-specific Model Context Protocol tools activated specifically for that studio's workload.

---

## 🗂️ Standard Studio Templates Included

| Studio Template | Domain | Primary Engine | Preconfigured MCP Tools | Key Artifacts |
|---|---|---|---|---|
| [`studio_demo/`](./studio_demo/) | **General Purpose / Starter** | `cascata-fast` (Sonnet / Flash) | `web-search`, `messages-search` | `coord_demo.md`, `tracker_demo.md` |
| [`research_studio/`](./research_studio/) | **Deep Web Scraping & PA Intelligence** | `cascata-agy` (Gemini Pro / Flash) | `chrome-cdp`, `web-search`, `gdrive` | `coordinator_research.md`, `scouting_tracker.md` |
| [`dev_studio/`](./dev_studio/) | **Software Engineering & Infra** | `cascata-pro` (Claude Opus / Sonnet) | `terminal-tools`, `git-tools`, `arturo-db` | `lead_architect.md`, `backlog_tracker.md` |
| [`creative_studio/`](./creative_studio/) | **Media, Audio & Visual Production** | `cascata-agy` (Gemini Multimodal) | `tts`, `radio-betty`, `photos`, `news` | `creative_director.md`, `production_tracker.md` |

---

## 🚀 Creating a New Custom Studio

### Method 1: Generative Script (`create_new_agent_or_studio.py`)
Run the automated factory script to generate the folder, symlinks, Obsidian profile, PostgreSQL record, and start the Tmux session:

```bash
python3 core/create_new_agent_or_studio.py \
    <coordinator_slug> \
    "<Coordinator Name>" \
    <studio_key> \
    "<System Prompt / Domain Mandate>" \
    studios/<studio_key> \
    agy \
    VPS
```

### Method 2: Manual Registration & Scaffolding
1. **Create Studio Directory & Symlink Rules**:
   ```bash
   mkdir -p studios/legal_studio
   ln -s ../../AGENTS.md studios/legal_studio/CLAUDE.md
   ```
2. **Configure Studio `.mcp.json`**:
   ```json
   {
     "mcpServers": {
       "web-search": {
         "command": "python3",
         "args": ["../../mcp/mcp_web_search.py"]
       }
     }
   }
   ```
3. **Register Coordinator in PostgreSQL**:
   ```sql
   INSERT INTO agents (agent_id, data) VALUES (
       'justitia',
       jsonb_build_object(
           'name', 'Justitia (Legal Coordinator)',
           'studio', 'legal_studio',
           'role', 'Lead Legal Analyst',
           'engine', 'claude',
           'model', 'cascata-pro',
           'always_on', true,
           'tmux_window', 'legal_studio:AGY-JUSTITIA',
           'work_dir', 'studios/legal_studio',
           'coordinatore', 'justitia',
           'dismesso', false
       )
   );
   ```
4. **Boot Coordinator Daemon**:
   ```bash
   python3 core/strumento_agenti.py accendi_agente justitia
   ```

---

## 🔄 Studio Operational Lifecycle

```
[Operator / Telegram Gateway]
              │
              ▼ (Dispatches Domain Goal)
[Studio Coordinator in Tmux Session]
              │
              ├─► Updates 'tracker.md' on disk
              ├─► Formulates step-by-step task mandate
              │
              ▼ (Spawns Ephemeral Exec)
[core/strumento_agenti.py crea_exec]
              │
              ▼ (Executes in Temporary Tmux Window)
[Disposable Exec Agent]
              │
              ├─► Maintains '/tmp/betty_docs/piano_<id>.md'
              ├─► Executes MCP tools & CLI commands
              ├─► Tests deliverables empirically
              │
              ▼ (Sends A2A Completion Report)
[send_a2a.py back to Coordinator]
              │
              ▼ (Coordinator verifies & reaps Exec)
[core/strumento_agenti.py elimina_exec]
              │
              ▼ (Pushes Mobile Summary)
[/tmp/betty_recaps/coordinator.txt] ───► Telegram Topic
```

---

## 🔒 Security & Workspace Boundaries

1. **Perimeter Confinement**: Coordinators can only create, monitor, and destroy executors within their own assigned studio working directory and Tmux session.
2. **Zero Plaintext Secrets**: Local workspace scripts must never store hardcoded API keys. All credentials must be dynamically resolved via `secret_env.py`.
3. **Non-Destructive Decommissioning**: When an ephemeral exec is deleted via `elimina_exec`, the database record is flagged as `dismesso=true` to preserve historical audit logs while terminating the active Tmux process.
