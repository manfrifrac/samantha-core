# Agent-to-Agent (A2A) Messaging Protocol

> **Scope**: High-reliability inter-agent communication, delegation routing, asynchronous inbox workflows, and payload hygiene.

---

## 🔄 The Certified A2A Dispatch Protocol

All inter-agent communication must use the certified dispatcher script rather than manual tmux key injections:

```bash
# Text message payload
python3 core/send_a2a.py "<session:window>" "[A2A_FROM:<slug>] [A2A_TYPE:task] <Message body>"

# File-based message payload (for structured briefs)
python3 core/send_a2a.py "<session:window>" --file /tmp/docs/mandate.txt
```

### Mandatory Protocol Headers

Every A2A payload must begin on line 1 with standard routing tags:

```text
[A2A_FROM:<sender_slug>] [A2A_TYPE:task|question|report|handoff]
<Natural language body, brief, or file pointer>
```

---

## 📬 Inbox Deposition & Acknowledgment (`a2a_ack`)

A2A messaging operates on a file-persisted asynchronous inbox model:

1. **Deposition on Disk**:
   - The dispatcher deposits the message file into the recipient's filesystem mailbox:
     ```text
     a2a/<recipient_slug>/inbox/<timestamp>__<sender>__<id>.md
     ```
2. **Bootstrap Mailbox Check**:
   - Upon session start or wake-up, every agent must first inspect its `a2a/<slug>/inbox/` directory for pending messages.
3. **Explicit Acknowledgment**:
   - Once an agent reads and processes an inbox message, it must acknowledge receipt using:
     ```bash
     python3 core/a2a_ack.py <message_id>
     ```
   - This moves the file from `inbox/` to `read/`, serving as definitive proof of receipt.

---

## 📎 Payload Hygiene & Lightweight Messaging

1. **No Inlined Heavy Dumps**:
   - Stack traces, extensive build logs, raw datasets, and multi-file code listings must never be pasted directly into A2A messages.
   - Stage large outputs to disk (e.g. `/tmp/docs/report_<task>.md`) and pass only the file path with a concise 2–3 line brief.
2. **Shared Bug Escalation**:
   - If an agent discovers a defect affecting shared infrastructure (PostgreSQL schemas, Telegram engine, Chromium CDP bridges, shared MCP servers), it must escalate an A2A report to the **Samantha Pool** (`a2a/samantha/inbox`) for system-level resolution.
