# Lightweight Memory Architecture & Context Optimization

> **Scope**: Token conservation, context window refresh policies, disk-first state persistence, and procedural skill accumulation.

---

## 🧠 The Disk-First Memory Architecture

In long-running autonomous systems, the conversational context window is an ephemeral, consumable resource. Accumulating hundreds of turns bloats prompt tokens on every turn.

**The Golden Axiom**: *The agent lives on disk, not in the conversational context window.*

1. **Persistent State Storage**:
   - Every agent maintains its ground truth state in its dedicated markdown tracking file:
     ```text
     Obsidian-Vault/Progetti/<agent_slug>_stato.md
     ```
2. **Wake-Up Sequence**:
   - Upon session start or wake-up, the agent's very first action is to **read its own state file from disk**. Never assume an empty or clean slate.
3. **Mandatory Refresh Checkpoint Sequence**:
   When token thresholds are reached or at major task boundaries, execute this non-negotiable three-step sequence:
   1. **Write**: Update the state file on disk with current progress, decisions, and pending tasks.
   2. **Verify**: Read the file back from disk to guarantee write integrity.
   3. **Refresh**: Accept or trigger a clean conversation context refresh (`/refresh` or session resume).

---

## 📚 Procedural Skill & Technique Accumulation

When an agent discovers a reusable workaround, an anti-bot technique, a stable parser pattern, or an API edge-case:
- It must **never** leave the knowledge solely within the ephemeral chat history.
- Write the discovered pattern into the appropriate category in `skills/` or the studio's skill repository.
- This guarantees collective intelligence persists permanently across session compactions and restarts.
