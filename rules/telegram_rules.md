# Telegram Supergroup & Mobile Interaction Rules

> **Status**: Mandatory System-Wide Rule Zero Specification.  
> **Applies to**: All Autonomous Agents and Coordinators with registered Telegram bot keys.

---

## 🚨 Rule Zero: Delivery via Telegram Recaps

Terminal CLI stdout/stderr is **never** seen directly by human supervisors. The human supervisor monitors and controls the ecosystem exclusively via mobile Telegram.

1. **Mandatory Recap File**:
   - At the conclusion of every task, phase, or milestone, the agent must write its user-facing text to:
     ```text
     /tmp/betty_recaps/<agent_slug>.txt
     ```
   - If the file is consumed and deleted by the engine, delivery has succeeded. Do not re-write or assume a failure.
2. **Ephemeral Executors Exception**:
   - Ephemeral dev executors created via `crea_exec` without a registered bot do not write recaps; their output is an A2A report to their parent coordinator and deliverables staged in `/tmp/docs/`.

---

## 🏷️ Telegram Recap Control Tags

The recap file must contain plain text formatted with specialized protocol tags:

| Control Tag | Syntax Example | Behavioral Description |
|---|---|---|
| **File Attachment** | `[FILE: /absolute/path/report.pdf]` | Automatically attaches and uploads the specified file to Telegram. Writing files to disk without this tag leaves them inaccessible on the server. |
| **Interactive Buttons** | `[QUESTION_OPTIONS: Option A \| Option B]` | Renders native Telegram inline keyboard buttons (2–4 short choices). **This is the only permitted method to solicit user decisions.** Native CLI modals (`AskUserQuestion`) are forbidden. |
| **Message Pinning** | `[PIN_MESSAGE]` | Pins the message to the top of the topic thread (used for persistent coordinator status dashboards). |
| **Reply Threading** | `[REPLY_TO_MSG_ID: 19271]` | Threads the reply directly under a specific Telegram message ID. |
| **Voice Synthesis** | `<VOICE>Text to synthesize</VOICE>` | Synthesizes an audio voice note. **Forbidden by default** unless explicitly requested by the human supervisor. |

---

## 📱 Mobile UX & Formatting Constraints

Human supervisors primarily read messages on mobile smartphones (e.g. iPhone):

1. **Strict Prohibition of ASCII Tables**:
   - Never output markdown tables (`| col | col |`) or ASCII box drawings (`+---+---+`). On mobile screens, they wrap irregularly and become unreadable.
   - Always use **vertical bulleted lists with emoji indicators** (🟢, 🟡, 🔴, 📌).
2. **Concise Typography**:
   - Keep paragraphs to 2–3 lines.
   - Highlight critical metrics and deliverables in **bold**.
   - Use standard markdown: `**bold**`, `*italic*`, `` `inline_code` ``, `[clickable link](url)`, `> single quote`, `>> collapsible quote block`.
3. **Large Deliverables**:
   - Deliver lengthy analyses, transcripts, and source listings as PDF/Markdown files using the `[FILE:]` tag rather than dumping thousands of characters into chat text.
