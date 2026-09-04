# Telegram Forum Gateway

The **Telegram Forum Gateway** (`unified_telegram_engine.py`) provides an autonomous bi-directional bridge between Telegram Supergroup Forum Topics and Tmux CLI agent sessions.

---

## 🏛️ Architecture & Communication Flow

```
+-------------------------------------------------------------+
|               TELEGRAM FORUM SUPERGROUP                      |
|  +------------------+  +------------------+  +-----------+  |
|  | Topic: Lead Dev  |  | Topic: Research  |  | Topic: Ops|  |
|  +------------------+  +------------------+  +-----------+  |
+-------------------------------------------------------------+
                               |
                   Unified Telegram Engine
              (Polling Updates & Routing Engine)
                               |
                               v
+-------------------------------------------------------------+
|                     TMUX AGENT FLOCK                        |
|  +------------------+  +------------------+  +-----------+  |
|  | Pane: LEAD_DEV   |  | Pane: RESEARCH   |  | Pane: OPS |  |
|  +------------------+  +------------------+  +-----------+  |
+-------------------------------------------------------------+
                               |
                File Recap Drop (/tmp/betty_recaps/)
                               v
               Unified Telegram Engine Deliverer
       (MarkdownV2 Formatting, Attachments, Buttons & Pin)
```

---

## 🚀 Key Features

1. **Topic-to-Pane Routing**:
   Incoming user messages inside a specific Telegram topic (Thread ID) are routed directly into the corresponding agent's active Tmux window.

2. **File Recap System (`/tmp/betty_recaps/<slug>.txt`)**:
   Agents conclude tasks by dropping a text recap. The engine detects the file, formats it into clean mobile-first MarkdownV2, sends it to the target topic, and deletes the file upon confirmed delivery.

3. **Specialized Recap Tags**:
   - `[FILE: /path/to/document.pdf]`: Automatically uploads and attaches files (PDF, images, logs) to the message.
   - `[QUESTION_OPTIONS: Option A | Option B | Option C]`: Generates Telegram inline keyboard buttons for interactive human-in-the-loop decisions.
   - `[PIN_MESSAGE]`: Pins the delivered dashboard or status recap to the top of the topic.
   - `[REPLY_TO_MSG_ID: 12345]`: Visually chains the agent's response to the original user prompt.
   - `<VOICE>...</VOICE>`: Synthesizes speech and delivers an audible Telegram voice message.

4. **Mobile-First MarkdownV2 Formatter (`telegram_format.py`)**:
   Prevents broken formatting, handles nested formatting entities, renders collapsible expandable quotes (`>> `), and formats long reports cleanly without breaking iPhone layouts.

---

## ⚙️ Configuration & Setup

### 1. Environment Variables (`.env`)
Add your bot credentials and chat configuration:
```bash
TELEGRAM_BOT_TOKEN=123456789:ABCdefGHIjklMNOpqrSTUvwxYZ
TELEGRAM_CHAT_ID=-1001234567890
TELEGRAM_ADMIN_ID=987654321
```

### 2. Launching the Telegram Gateway
```bash
python3 telegram/unified_telegram_engine.py
```
Or start it as a background service via your supervisor / Tmux session.
