# Global Skills & Capabilities Catalog

> **Directory**: `skills/`  
> **Architecture**: Samantha Autonomous Fleet Operational Knowledge Base  
> **Format Constraint**: Strict Markdown documentation (`.md`). Never stored in database blobs or ephemeral runtime memory.

---

## 📖 Purpose & Vision

The **Global Skills Catalog** is the canonical repository of verified engineering techniques, operational patterns, automation workarounds, and architectural solutions developed by specialized agents and ephemeral development executors across the Samantha Autonomous Ecosystem.

In complex autonomous systems, operational intelligence discovered in production (e.g. anti-bot bypass strategies, memory leak remedies, CDP click mechanics, or token conservation tactics) often risks being lost when task sessions refresh. This catalog acts as the **Single Source of Truth** for reusable collective intelligence.

---

## 🗂️ Skill Categories

The skills catalog is organized into 6 core vertical domains:

| Category File | Domain | Key Topics & Patterns | Primary Recommended Model |
|---|---|---|---|
| [`dev_engineering.md`](./dev_engineering.md) | **Development & Debugging** | Isolated unit testing with mocks, regression prevention, lazy MCP loading, ANSI/Unicode terminal formats, targeted process restarts, 1M context window workflows, textual date columns, unittest exit traps. | Claude 3.7 Sonnet / Opus |
| [`browser_automation.md`](./browser_automation.md) | **Browser & CDP Automation** | Hardware-level OS Trusted Click (`xdotool`), PeopleSoft / enterprise portal navigation, iframe isolation, Turnstile bypass via residential IP, atomic tab closure, React/Ashby form auto-fill, base64 file uploads, anti-bot pacing. | Gemini 2.5 Pro/Flash or Claude Sonnet |
| [`infrastructure_ops.md`](./infrastructure_ops.md) | **Infrastructure & Services** | Declarative single supervisor (`supervisore_unico.py`), atomic file locking (`flock`), OOM-killer tuning (`oom_score_adj`), pre-commit sanitization, flat-rate quota probes, garbage collection, tmux cascading restarts, real-interval CPU monitoring. | Claude Sonnet / Opus |
| [`communication.md`](./communication.md) | **A2A & Telegram Protocol** | Unique tmux buffers preventing race conditions, non-breaking space (NBSP) ghost prevention in CLI composers, mobile-optimized Telegram formatting, atomic inbox depositions, message deduplication, mtime jitter tolerance. | Claude Sonnet / Gemini Flash |
| [`web_research.md`](./web_research.md) | **Web Scraping & PA Portals** | Multi-query search patterns, direct REST queries for public portals, browserless HTTP POST form submission, 403 User-Agent diagnosis, public procurement and grant scoring pipelines. | Gemini Flash / Claude Sonnet |
| [`multimedia_production.md`](./multimedia_production.md) | **Media, Graphics & 3D** | Audio-reactive WebGL/Three.js 3D video mapping, FFmpeg H.264/AAC mobile transcoding, Wikimedia Commons media sourcing, computer vision HUD timelapse rendering, scalable SVG vector design systems. | Gemini 2.5 Multimodal & Claude |

---

## 🎯 Model Routing by Domain

To maximize reasoning efficiency and cost-effectiveness, agents select models according to their technical domain:

| Domain | Primary Engine | Secondary Engine | Architectural Rationale |
|---|---|---|---|
| `dev` | **Claude Sonnet / Opus** (`claude`) | **Gemini 2.5 Pro** (`agy`) | Superior code reasoning and anti-regression verification; Gemini for massive (1M+ token) refactors. |
| `web_research` | **Gemini Flash** (`agy`) | **Claude Sonnet** (`claude`) | High throughput and minimal latency for bulk data scraping; Sonnet for nuanced scoring and synthesis. |
| `browser_automation` | **Gemini Pro/Flash** or **Claude Sonnet** | **Claude Opus** | High precision in tool calling and visual element verification via screenshots. |
| `infrastructure` | **Claude Sonnet / Opus** (`claude`) | **Gemini 2.5 Pro** (`agy`) | Zero tolerance for system-level errors, service configuration drift, or security boundary breaches. |
| `communication` | **Claude Sonnet** (`claude`) | **Gemini Flash** (`agy`) | Structured adherence to protocol headers, mobile screen UX formatting, and delegation prompts. |
| `multimedia` | **Gemini 2.5 Multimodal** (`agy`) | **Claude Opus / Sonnet** (`claude`) | Native visual understanding and image generation tools; Claude for mathematical GLSL/Three.js scripts. |

---

## 📐 Standard Pattern Template

Every entry in the category catalogs strictly follows this schema:

```markdown
## N. Clear Title of Technique or Pattern

- **Problem Solved**: Exact context of symptom, failure mode, or technical constraint.
- **Technical Explanation**: Root cause analysis and theoretical / architectural explanation.
- **Implementation Guide**: Step-by-step procedure, copy-pasteable code snippet, shell commands, or mathematical formula.
- **Author & Date**: Agent/executor identifier, date, and task reference.
- **References**: Relative links to source implementations or documentation.
```

---

## 🛠️ Contribution & Maintenance Guidelines

1. **Who Updates**:
   - Ephemeral dev executors upon completing R&D or debugging tasks that discover generalizable patterns.
   - Studio Coordinators and Core maintainers during periodic state consolidation.
2. **What NOT to Include**:
   - ❌ Ephemeral task logs or chronologies (those belong in state trackers).
   - ❌ Unverified or hypothetical techniques. If a pattern is experimental, it must be explicitly marked as such.
3. **Execution Rule**:
   - Before implementing complex automation or debugging procedures, agents must review the relevant skill files to reuse battle-tested patterns.
