# LLM Engines, Native CLI Transparency & MCP Standards

> **Scope**: Multi-tier engine routing, native CLI terminal visibility, fallback cascades, and MCP server standards.

---

## 🔀 Native CLI Transparency & Tmux Execution

Agents run directly inside **native CLI wrappers** (`claude` for Claude Code, `agy` for Antigravity) attached to dedicated tmux windows:
1. **Live Visibility**: The human supervisor can inspect terminal windows at any moment to observe real-time tool calls, shell executions, and model reasoning without mock spinners.
2. **Identity & Auth Isolation**: Native CLIs maintain direct OAuth credentials and working directory bindings without intermediary proxy bottlenecks.

---

## ⚡ Multi-Tier Fallback Cascades

Engine dispatchers (`core/engine_adapter.py`) manage seamless fallback sequences across model tiers during upstream provider rate limits or service outages:

| Cascade Profile | Tier 1 (Primary) | Tier 2 (Fallback) | Tier 3 (Emergency) |
|---|---|---|---|
| **`cascata-pro`** | Claude 3.7 Opus | Claude 3.7 Sonnet | Claude 3.5 Haiku |
| **`cascata-fast`** | Claude 3.7 Sonnet | Claude 3.5 Haiku | — |
| **`cascata-agy`** | Gemini 2.5 Pro | Gemini 2.5 Flash | Gemini 2.5 Flash Lite |

### Provider Reliability Notice
- **Prohibition of OpenRouter for Interactive CLIs**: Third-party proxies frequently return empty or malformed HTTP 200 responses during interactive CLI token streaming. Always bind CLIs to direct native provider endpoints.

---

## 🔌 Model Context Protocol (MCP) Server Architecture

1. **Lazy Loading Dependency Rule**:
   - Heavy dependencies (PyTorch, database drivers, large ML models) must be imported inside tool function bodies or behind lazy singletons, never at the module root, to avoid JSON-RPC handshake timeouts.
2. **Process Scope**:
   - MCP servers communicate over standard I/O (JSON-RPC) and run within the agent's permission envelope.
