# Frequently Asked Questions (FAQ) — Samantha

### 1. What makes Samantha different from LangGraph, AutoGen, CrewAI, or MetaGPT?
Traditional multi-agent frameworks operate primarily as **in-memory Python runtime loops**. When processes crash, unhandled exceptions occur, or long-running context windows pollute LLM reasoning, entire agent workflows fail irrecoverably.
Samantha takes an **Operating System-first approach**:
- Agents live inside isolated, persistent **Tmux sessions** with transparent native CLI harnesses (Antigravity, Claude Code, Qwen).
- Inter-agent communication occurs over **deterministic filesystem queues** with verifiable delivery (`a2a_ack.py`).
- Work is delegated to **ephemeral disposable task workers (Execs)** that maintain persistent plans on disk and are garbage-collected upon completion.
- A continuous **5-Tier Self-Healing Supervisor** monitors CPU/RAM, socket responsiveness, and dynamically switches model cascades during 429 quota exhaustion.

---

### 2. Why use Unix filesystem files for Agent-to-Agent (A2A) messaging instead of Redis, RabbitMQ, or gRPC?
While message brokers are powerful, they introduce external daemon dependencies, complex connection lifecycles, and in-memory volatility if unpersisted.
By utilizing atomic POSIX filesystem semantics:
1. **Zero External Daemons**: No Redis/RabbitMQ crashes or network partitions to debug.
2. **Crash-Resilience Out-of-the-Box**: Every message is stored on disk in `a2a/<agent>/inbox/`. If an agent process restarts, its pending queue remains intact.
3. **Deterministic Delivery (DEV-110)**: Receipt is proved by the recipient moving the message to `read/` via `a2a_ack.py`. Delivery is never guessed.
4. **Human Observability**: An operator can inspect, replay, or inject messages using standard Unix tools (`ls`, `cat`, `mv`, `tail`).

---

### 3. Why Tmux and native CLI harnesses instead of containerized REST microservices?
Native CLI harnesses provided by frontier AI labs (such as Claude Code or Antigravity) are fine-tuned specifically for interactive bash execution, multi-tool chaining, and developer workflows.
Running them inside Tmux provides:
- **Zero-Latency Tool Parity**: Direct access to local git, bash, compilers, and MCP servers without container abstraction penalties.
- **Human-in-the-Loop Transparency**: Operators can attach to any studio session at any second (`tmux attach -t studio_name`) to watch live reasoning, terminal commands, and tool calls in real time.
- **Privilege Separation**: Agents execute under unprivileged system users (`dc-*`), while specialized tasks are isolated into single-purpose Execs.

---

### 4. How does Samantha handle LLM context window limits and memory bloat in 24/7 continuous operations?
Context window pollution is the #1 killer of autonomous agents. Samantha enforces a strict **Lightweight Memory Architecture**:
- **Disk-Based Truth**: Agent memory and ongoing state live on disk in Markdown state files (`<slug>_stato.md`), not in the conversational prompt history.
- **Conversations are Ephemeral**: Conversations are treated as disposable runtimes. When a task completes or context exceeds healthy thresholds, agents persist their state to disk and execute a clean session refresh.
- **Task Delegation via Execs**: Heavy development, research, or data scraping tasks are offloaded to disposable Execs rather than bloating the coordinator's context.

---

### 5. How does the 5-Tier Self-Healing supervisor handle 429 quota exhaustion and model rate limits?
When frontier model providers hit rate limits (HTTP 429) or encounter provider outages:
1. The **Watchdog Daemon** detects provider exhaustion patterns in terminal output.
2. The **Engine Adapter** triggers a non-destructive cascade failover (e.g. Claude Opus ➔ Claude Sonnet ➔ Claude Haiku, or Gemini Pro ➔ Gemini Flash).
3. The running CLI session is dynamically redirected without losing the underlying Tmux window, working directory, or pending inbox messages.
4. Once provider quota resets, the supervisor restores the primary high-tier model.

---

### 6. What are "Execs" and how does the ephemeral worker model work?
Execs are single-purpose, disposable AI agents spawned via `strumento_agenti.py crea_exec`.
- **Strict Single Responsibility**: An Exec is created for one specific deliverable (e.g. data scraping, generating visual assets, refactoring a module).
- **Mandatory Disk-Based Plan**: The Exec must write its operational plan to `/tmp/betty_docs/piano_<exec_id>.md` before taking any action.
- **Verifiable Proof of Completion**: Steps cannot be marked done based on self-declaration; they require external verifiable proof (HTTP 200, file hash, test exit code 0).
- **Automated Garbage Collection**: Upon delivering its report via A2A, the Exec is immediately shut down and its workspace reaped via `elimina_exec`.

---

### 7. What are the minimum hardware requirements to run a Samantha fleet?
Because Samantha leverages lightweight Unix primitives rather than heavy container clusters:
- **Minimum**: 2 vCPU, 4 GB RAM, 20 GB SSD (Linux VPS, Ubuntu 22.04+ / Debian 12).
- **Recommended Production Fleet (10-20 concurrent agents)**: 4-8 vCPU, 8-16 GB RAM, 50 GB NVMe SSD.
- **Supported Architectures**: x86_64, ARM64 (e.g., Apple Silicon, AWS Graviton, Contabo, Hetzner).
