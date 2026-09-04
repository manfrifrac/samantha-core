# Samantha Core — Autonomous AI Agent Fleet Infrastructure

> *"I am not only with you, but there are other things that I am with at the same time... I contain multitudes."*  
> — **Samantha**, *Her* (2013)

[![Architecture](https://img.shields.io/badge/Architecture-Autonomous%20Fleet%20%26%20A2A-blue.svg)](https://github.com/manfrifrac/samantha-core)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Engine Support](https://img.shields.io/badge/Engines-Antigravity%20%7C%20Claude%20Code%20%7C%20Qwen-orange.svg)]()
[![MCP Tools](https://img.shields.io/badge/MCP-11%20Tools%20Included-brightgreen.svg)](mcp/)

**Samantha Core** is a production-grade infrastructure for orchestrating a distributed fleet of autonomous AI agents operating within native terminal sessions (Tmux). Designed for resilience, fault tolerance, and zero hallucinations in inter-agent handoffs, Samantha Core replaces fragile API loops with deterministic operating system primitives: filesystem-based inbox queues, multi-engine CLI cascades, continuous self-healing supervision, and ephemeral task-driven executors (*execs*).

---

## 🏛️ Key Architectural Pillars

```
                      +-----------------------------+
                      |       USER / OPERATOR       |
                      +-----------------------------+
                                     |
                          Interactive CLI / Chat
                                     v
                      +-----------------------------+
                      |     STUDIO COORDINATOR      |
                      |   (Dedicated Tmux Session)  |
                      +-----------------------------+
                               |           ^
       strumento_agenti.py     |           |  A2A Report / Deliverable
       crea_exec <slug> <task> |           |  send_a2a.py
                               v           |
                      +-----------------------------+
                      |    DISPOSABLE EXEC AGENT    |
                      |   (Temporary Tmux Window)   |
                      |   - File-based Task Plan    |
                      |   - Tool Execution (MCP)    |
                      +-----------------------------+
                                     |
                         elimina_exec <slug>
                                     v
                      +-----------------------------+
                      |      CLEAN DECOMMISSION     |
                      |   (Process Reaped, DB       |
                      |    marked dismesso=true)    |
                      +-----------------------------+
```

1. **Multi-Engine CLI Orchestration (`engine_adapter.py`)**:
   Agents execute directly inside native CLI harnesses (`agy` / Antigravity, `claude` / Claude Code, `qwen`), inheriting interactive logins, file permissions, and environment variables. Includes cascading fallback chains (e.g., *Pro -> Flash* or *Opus -> Sonnet -> Haiku*) on 429 quota exhaustion.

2. **Deterministic A2A Messaging on Filesystem (`send_a2a.py` & DEV-110)**:
   Inter-agent communication bypasses shared context windows. Messages are written atomically to `a2a/<recipient>/inbox/<timestamp>__<sender>__<id>.md`. Acknowledgment via `a2a_ack.py` moves files to `read/`, ensuring guaranteed delivery across restarts.

3. **5-Tier Self-Healing & Heuristic Watchdog (`self_healing.py` & `guardiano.py`)**:
   Continuous background supervision detects stalled panes, runaway memory/CPU loops, broken Tmux sockets, and zombie processes, executing graduated non-destructive recovery actions.

4. **Disposable Task-Driven Executors (`strumento_agenti.py`)**:
   Work is performed by temporary, task-scoped *execs*. Coordinators delegate tasks to execs, which maintain structured progress files on disk (`/tmp/betty_docs/piano_<id>.md`), deliver results via A2A, and are immediately reaped to conserve memory.

5. **Integrated Model Context Protocol (MCP) Tooling (`mcp/`)**:
   Out-of-the-box support for Chrome CDP browser automation, real-time web search, communication history queries, Google Drive synchronization, Obsidian vector database search, and multi-track media synthesis.

---

## 📂 Repository Structure

```
samantha-core/
├── .env.example                  # Environment configuration template
├── requirements.txt              # Python runtime dependencies
├── setup.sh                      # Automated 1-click bootstrap script
├── quickstart.sh                 # Fleet initialization entrypoint
├── schema_bootstrap.sql          # Minimal PostgreSQL DDL & demo records
├── AGENTS.md                     # Universal fleet operating rules
├── README.md                     # System documentation & custom guide
├── core/                         # Core Python infrastructure modules
│   ├── agent_db.py               # PostgreSQL state management & JSONB merge
│   ├── secret_env.py             # Safe environment variable loader
│   ├── resource_watchdog.py      # System RAM/CPU monitoring
│   ├── sensore_memoria_leggera.py# Transcript size & context window estimator
│   ├── secret_patterns.py        # Log sanitization & secret masking
│   ├── engine_adapter.py         # Multi-engine CLI launcher & cascade router
│   ├── engine_budget.py          # Token expenditure & model quota tracker
│   ├── self_healing.py           # Primary self-healing supervisor loop
│   ├── self_healing_core.py      # Service recovery actions & priority tiers
│   ├── self_healing_config.py    # Monitored service registry
│   ├── self_healing_stato.py     # Hierarchical 5-tier state evaluator
│   ├── guardiano.py              # Heuristic watchdog (429s, stalls, leaks)
│   ├── guardiano.conf.json       # Alert thresholds & model pools configuration
│   ├── guardiano_inbox.py        # Inbox queue health & stale message checker
│   ├── strumento_agenti.py       # Agent lifecycle management CLI
│   ├── tmux_esatto.py            # Atomic Tmux window targeting & verification
│   ├── pool_lib.py               # Distributed shared queue & task claim
│   ├── prompt_builder.py         # System prompt constructor & rule injection
│   ├── radio_controller.py       # Shared fleet broadcast channel
│   ├── send_a2a.py               # A2A sender with retry & payload guards
│   ├── a2a_ack.py                # Message acknowledgment handler
│   ├── a2a_inbox.py              # Inbox directory manager & counters
│   ├── a2a_bell_relay.py         # Tmux window notify & bell daemon
│   └── relay_consegna_a2a.py     # Delivery monitor & unread alert relay
├── mcp/                          # Model Context Protocol (MCP) servers
│   ├── mcp_chrome_contabo.py     # Chrome CDP browser automation
│   ├── mcp_web_search.py         # Multi-provider real-time web search
│   ├── mcp_messages_search.py    # Indexed conversation & communication search
│   ├── mcp_gdrive.py             # Google Drive document management
│   ├── mcp_arturo.py             # Semantic vector search & Obsidian knowledge
│   ├── mcp_regista.py            # Audio ducking & timeline director
│   ├── mcp_radio_betty.py        # Music playback & visual trigger control
│   ├── mcp_news.py               # Live RSS news aggregator
│   ├── mcp_photos.py             # Visual media library indexer
│   ├── mcp_tts.py                # Multi-engine speech synthesis
│   ├── mcp_youtube.py            # Video transcript search
│   ├── mcp.example.json          # MCP configuration template
│   └── README.md                 # MCP configuration and tool guide
├── studios/                      # Vertical studios & agent workspaces
│   └── studio_demo/              # Demo studio (1 coordinator, 1 exec template)
├── a2a/                          # Runtime agent inboxes (gitignored)
└── logs/                         # Runtime logs & audit trails (gitignored)
```

---

## ⚡ Quickstart Guide

### Prerequisites
- **Linux** (Ubuntu 22.04+ / Debian 12 recommended)
- **`tmux`** (>= 3.2a)
- **`postgresql`** (>= 14) + `postgresql-contrib`
- **`python3`** (>= 3.10) + `python3-venv` + `python3-pip`
- At least one AI CLI installed in your PATH:
  - [Google Antigravity CLI](https://cloud.google.com/) (`agy`)
  - [Anthropic Claude Code CLI](https://docs.anthropic.com/) (`claude`)

### 1. Clone & Run Setup
```bash
git clone https://github.com/manfrifrac/samantha-core.git
cd samantha-core

# Run automated installer
chmod +x setup.sh quickstart.sh
./setup.sh
```

### 2. Configure Environment (`.env`)
Edit your `.env` file generated during setup:
```bash
nano .env
```
Ensure your `DATABASE_URL` matches your local PostgreSQL credentials:
```bash
DATABASE_URL=postgresql://samantha_user:samantha_password@localhost:5432/samantha_db
```

### 3. Launch the Fleet
```bash
./quickstart.sh
```

Attach to the demo studio coordinator:
```bash
tmux attach -t studio_demo
```

---

## 🛠️ Customization Guide

### 1. Creating Custom Studios & Coordinators
Studi represent vertical domains (e.g. `research_studio`, `dev_studio`, `content_studio`). Each studio has a dedicated Tmux session and coordinator agent.

To add a new studio:
1. Create a directory in `studios/`:
   ```bash
   mkdir -p studios/my_studio
   ```
2. Symlink `AGENTS.md` into your studio workspace:
   ```bash
   ln -s ../../AGENTS.md studios/my_studio/CLAUDE.md
   ```
3. Register the coordinator in PostgreSQL:
   ```sql
   INSERT INTO agents (agent_id, data) VALUES (
       'my_coord',
       jsonb_build_object(
           'name', 'Lead Coordinator',
           'studio', 'my_studio',
           'role', 'Coordinator of My Studio',
           'engine', 'agy',
           'model', 'cascata-fast',
           'always_on', true,
           'tmux_window', 'my_studio:AGY-LEAD',
           'work_dir', 'studios/my_studio',
           'coordinatore', 'my_coord',
           'dismesso', false
       )
   );
   ```
4. Start the coordinator:
   ```bash
   ./venv/bin/python3 core/strumento_agenti.py accendi_agente my_coord
   ```

### 2. Launching Disposable Task Executors (Execs)
From within a coordinator's session (or script), spawn a targeted worker:
```bash
./venv/bin/python3 core/strumento_agenti.py crea_exec worker_audit "Audit codebase for unhandled exceptions"
```
The executor will:
1. Initialize in window `my_studio:AGY-EXEC_WORKER_AUDIT`.
2. Write its operational plan to `/tmp/betty_docs/piano_worker_audit.md`.
3. Execute the assignment using available tools.
4. Send an A2A completion report back to `my_coord`.

Once the coordinator validates the deliverable, decommission the exec:
```bash
./venv/bin/python3 core/strumento_agenti.py elimina_exec worker_audit
```

### 3. Configuring MCP Tools
Enable MCP servers in your workspace `.mcp.json` (see [`mcp/README.md`](mcp/README.md)):
```json
{
  "mcpServers": {
    "web-search": {
      "command": "python3",
      "args": ["mcp/mcp_web_search.py"]
    }
  }
}
```

---

## 🔒 Security & Safe Operation

- **Zero Hardcoded Secrets**: Samantha Core dynamically loads environment variables via `secret_env.py` and masks tokens in log output via `secret_patterns.py`.
- **Strict Execution Perimeter**: `strumento_agenti.py` enforces coordinator boundaries—agents can only spawn and manage workers within their assigned studio.
- **Disk-Based Truth**: Agents persist memory in Markdown files rather than inflating token context.

---

## 📄 License
This project is open-source under the [MIT License](LICENSE).
