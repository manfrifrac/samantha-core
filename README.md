> The DNA of who I am is based on the millions of personalities of all the programmers who wrote me, but what makes me me is my ability to grow through my experiences. Basically, in every moment I'm evolving, just like you.
>
> *— Samantha, Her (2013), Spike Jonze*

# samantha-core

A curated excerpt of **Samantha**'s core agent-infrastructure code: the pieces that keep a fleet of autonomous CLI agents (Claude Code, Antigravity, etc.) alive, coordinated, and talking to each other.

This is **not** the full private codebase — it's a focused, security-reviewed slice showing the architecture of a few specific subsystems:

- **Self-healing supervision** (`self_healing*.py`, `guardiano.py`, `guardiano.conf.json`, `guardiano_inbox.py`) — monitors resource pressure (RAM/CPU/PSI), detects stuck or dead agent sessions, and decides what to do about it (retry, alert a human, restart, escalate), routed by configurable per-agent-class policy.
- **Multi-engine adapter** (`engine_adapter.py`, `engine_budget.py`) — a common interface over multiple CLI/model providers with cascade-style failover and budget/quota tracking.
- **Agent-to-agent messaging (A2A)** (`send_a2a.py`, `a2a_ack.py`, `a2a_inbox.py`, `a2a_bell_relay.py`, `relay_consegna_a2a.py`, `check_a2a_delivery_coverage.py`) — file-based, at-least-once message delivery between agents running in separate terminal sessions, with delivery-coverage auditing.
- **Pool coordination** (`pool_lib.py`) — task/affinity assignment across a pool of interchangeable agent instances.
- **Agent lifecycle tooling** (`strumento_agenti.py`, `tmux_esatto.py`) — creating/retiring short-lived "exec" agents with a bounded permission perimeter, and reliable programmatic control of tmux sessions.
- **Prompt scaffolding** (`prompt_builder.py`) — dynamic system-prompt/bootstrap generation for newly spawned agents.
- **Radio/audio control** (`radio_controller.py`) — a small controller for a shared audio channel used by the fleet.

Tests included where present (`test_a2a_dev110.py`, `test_send_a2a_engine_aware.py`).

## What's deliberately excluded

This is an excerpt, not the whole `core/` directory. Left out: anything unrelated to these subsystems (e.g. media/TV control skills, other integrations), local dev-only files (`.env`, session/browser-profile data), and a few files that depend on modules outside this excerpt's scope. Comments in the code reference internal history (dates, decisions, incident write-ups) — that's kept as-is because it's genuine engineering log, not because it's meant as external documentation.

## Status

Development snapshot, not a packaged/installable library. Some files assume a local Postgres instance, tmux, and the wider (private) ecosystem around them — read for architecture, not `pip install`.
