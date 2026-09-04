# Changelog — Samantha Core

All notable changes to the Samantha Core Autonomous Fleet Infrastructure will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [v0.3.0] — 2026-09-04

### 🚀 Added
- **Global Skills Catalog (`skills/`)**:
  - Six comprehensive domain skill repositories (`dev_engineering.md`, `browser_automation.md`, `infrastructure_ops.md`, `communication.md`, `web_research.md`, `multimedia_production.md`) with a model routing matrix in `skills/README.md`.
- **System Governance & Security Rules (`rules/`)**:
  - Ten codified operational policies: `roles_and_boundaries.md`, `email_payments_policy.md` (Zero-Trust Rule Zero-Bis), `telegram_rules.md` (Rule Zero), `a2a_communication.md`, `agent_lifecycle.md`, `memory_management.md`, `work_discipline.md`, `engines_and_mcp.md`, `standard_profiles.md`, and `browser_automation_policy.md`.
- **Multimodal Studio Workspaces (`studios/`)**:
  - Four specialized studio workspace templates: `studio_demo/` (starter), `research_studio/` (deep web scraping & public administration intelligence), `dev_studio/` (software engineering & infrastructure), and `creative_studio/` (3D audio-reactive WebGL mapping & video editing).
  - Dynamic agent and studio generative factory (`core/create_new_agent_or_studio.py`).
- **Automated Test Suite & Verification (`tests/` & `run_tests.sh`)**:
  - Seven isolated unit test suites covering A2A protocol, PostgreSQL persistence, credential redaction, self-healing state transitions, engine fallback routing, Telegram formatting, and MCP lazy initialization.
  - Root single-command test runner (`./run_tests.sh`) achieving 100% test pass rate.
- **MIT Open Source License**: Standardized open source licensing (`LICENSE`).

### 🔄 Changed
- **Universal Operating Rules (`AGENTS.md`)**: Full English localization with cross-module links to `rules/` and `skills/`.
- **Core Database Abstraction (`core/agent_db.py`)**: Added native `DATABASE_URL` environment variable support and safe secret resolution.
- **Main Documentation (`README.md`)**: Comprehensive architecture diagram, feature badges, and extended Quickstart and Customization guides.

---

## [v0.2.0] — 2026-09-04

### 🚀 Added
- **Integrated MCP Tool Servers (`mcp/`)**:
  - Eleven production-ready MCP tool servers: Chrome CDP automation, web search, conversation search, Google Drive, Obsidian vector search (`mcp_arturo`), director & audio ducking (`mcp_regista`), radio mixer (`mcp_radio_betty`), news aggregator, photo indexer, multi-engine speech synthesis, and YouTube transcript search.
  - Configuration template `mcp/mcp.example.json` and guide `mcp/README.md`.
- **Telegram Supergroup Forum Gateway (`telegram/`)**:
  - Bidirectional bridge connecting Telegram Forum Supergroup topics with active agent Tmux terminal panes (`telegram/unified_telegram_engine.py`).
  - Mobile MarkdownV2 formatter (`telegram/telegram_format.py`) and Topic administration tools (`telegram/telegram_topic_admin.py`).
  - Mobile recap protocol tags: `[FILE:]`, `[QUESTION_OPTIONS:]`, `[PIN_MESSAGE]`, `<VOICE>`.

---

## [v0.1.0] — 2026-09-04

### 🚀 Added
- Initial standalone release of the Samantha Core daemon infrastructure.
- Multi-engine CLI launcher (`core/engine_adapter.py`) with native Tmux integration for Google Antigravity, Anthropic Claude Code, and Qwen.
- Deterministic filesystem-based A2A messaging (`core/send_a2a.py`, `core/a2a_ack.py`, `core/a2a_inbox.py`).
- 5-Tier hierarchical self-healing supervisor (`core/self_healing.py`, `core/self_healing_core.py`, `core/self_healing_stato.py`).
- Ephemeral task executor lifecycle management (`core/strumento_agenti.py`).
- Automated installation and bootstrap scripts (`setup.sh`, `quickstart.sh`, `schema_bootstrap.sql`).
