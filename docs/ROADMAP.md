# Product Roadmap — Samantha

```
                    SAMANTHA ARCHITECTURAL ROADMAP
  ┌──────────────────┐    ┌──────────────────┐    ┌──────────────────┐
  │   v1.0 (Current) │ ➔  │    v1.1 (Q4 '26) │ ➔  │    v1.2 (Q1 '27) │
  │ Single-Node Core │    │ Distributed Mesh │    │ Web TUI & Visual │
  └──────────────────┘    └──────────────────┘    └──────────────────┘
```

---

## 🟢 v1.0 — Foundation & Single-Node Fleet (Current Release)
- [x] **Unix Primitives Engine**: Native Tmux multi-pane lifecycle orchestration.
- [x] **Deterministic A2A Filesystem IPC**: Atomic inbox queues with `a2a_ack.py` proof-of-delivery.
- [x] **Ephemeral Execs Framework**: On-demand task worker generation (`crea_exec`) and automated cleanup (`elimina_exec`).
- [x] **Multi-Engine CLI Adapter**: Seamless integration for Antigravity, Claude Code, and Qwen CLI harnesses.
- [x] **5-Tier Self-Healing Supervisor**: Background watchdog for resource monitoring and dynamic 429 cascade failover.
- [x] **Telegram Forum Gateway**: Human-in-the-loop multi-studio interface with topic-based routing and inline keyboards.

---

## 🟡 v1.1 — Distributed Fleet & Hardened Sandboxing (Target: Q4 2026)
- [ ] **Multi-Node Mesh & Remote Tmux Orchestration**:
  - Encrypted agent-to-agent routing over WireGuard / SSH mesh tunnels.
  - Transparent delegation across heterogeneous server nodes (e.g. edge GPU nodes + cloud coordinator).
- [ ] **Optional Rootless Sandbox Isolation (Bubblewrap / Docker)**:
  - Plug-and-play ephemeral container sandbox for untrusted code execution.
  - Granular syscall and network namespace filtering per Exec.
- [ ] **Dynamic MCP Tool Hot-Plugging**:
  - Hot-reload MCP server configurations without restarting active coordinator sessions.
  - Centralized MCP capability discovery registry.
- [ ] **Automated GitHub Issue & PR Triage Exec**:
  - Specialized open-source maintainer Exec for automated repository issue labeling, test reproduction, and draft PR generation.

---

## 🔵 v1.2 — Real-Time Fleet Visualization & Web TUI (Target: Q1 2027)
- [ ] **Real-Time Fleet Topology Web Visualizer**:
  - WebSocket-based interactive visual graph displaying active studios, exec lifecycles, and A2A queue throughput.
  - Read-only terminal streaming for human oversight via web browser.
- [ ] **Interactive TUI Dashboard (Rich/Textual)**:
  - High-performance terminal dashboard for system administrators and DevOps engineers.
  - Live resource telemetry (CPU/RAM per agent pane, model token spend, queue latency).
- [ ] **Fine-Grained Role-Based Access Control (RBAC)**:
  - Cryptographically signed A2A tickets for high-security enterprise deployments.

---

## 🟣 v1.3 — Autonomous Tool Synthesis & Self-Optimization (Target: Q2 2027)
- [ ] **Autonomous Skill Compilation**:
  - Automated extraction of recurring task workflows into structured `/skills/` packages.
- [ ] **Heterogeneous Model Routing Optimizer**:
  - ML-driven cost-and-latency optimizer that dynamically assigns sub-tasks to the most cost-effective model (Local LLMs vs Frontier APIs).
- [ ] **Enterprise Audit & Compliance Suite**:
  - Immutable append-only audit trail for regulated industries (finance, healthcare, defense).

---

*Contributions and RFCs are welcome! Join our discussions on [GitHub Issues](https://github.com/manfrifrac/samantha-core/issues).*
