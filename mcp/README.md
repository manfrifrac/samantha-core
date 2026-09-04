# Model Context Protocol (MCP) Servers

This directory contains production-ready **Model Context Protocol (MCP)** server implementations that equip Samantha Core agents with specialized external tools, real-time web browsing, semantic search, and media control.

---

## 🛠️ Available MCP Servers

| Server Script | Protocol / Transport | Capabilities & Tools |
| :--- | :--- | :--- |
| `mcp_chrome_contabo.py` | STDIO / CDP | **Browser Automation**: Full Chrome DevTools Protocol control (tab navigation, screenshots, text fill, clicks, PDF printing, CAPTCHA detection). |
| `mcp_web_search.py` | STDIO | **Real-time Web Search**: Multi-provider search engine integration for factual grounding. |
| `mcp_messages_search.py` | STDIO | **Communication History**: Search indexed email threads, WhatsApp messages, and conversation archives. |
| `mcp_gdrive.py` | STDIO / Google API | **Cloud Storage**: Google Drive file search, folder creation, document download, upload, and metadata sync. |
| `mcp_arturo.py` | STDIO / Vector DB | **Knowledge Base**: Semantic vector search, Obsidian Markdown note search, reading, and indexing. |
| `mcp_regista.py` | STDIO | **Media Director**: Timeline sequencing, audio ducking, live cues, and broadcast state management. |
| `mcp_radio_betty.py` | STDIO | **Audio Playback**: Music queue, speech synthesis control, NASA imagery, and volume ducking. |
| `mcp_news.py` | STDIO | **News Fetcher**: RSS feed parsing and breaking news monitoring. |
| `mcp_photos.py` | STDIO | **Asset Library**: Image library search, Flatlay indexing, and luxury visual asset management. |
| `mcp_tts.py` | STDIO | **Speech Synthesis**: Multi-engine TTS voice synthesis (ElevenLabs, Fish Audio, Edge TTS). |
| `mcp_youtube.py` | STDIO | **Video Search**: YouTube video metadata extraction and stream transcript search. |

---

## ⚙️ Configuration

### 1. Standalone Integration (`mcp.json`)
Copy the example configuration to your working directory or engine configuration path:
```bash
cp mcp/mcp.example.json .mcp.json
```

### 2. Antigravity & Claude Code Integration
To register these MCP servers with Antigravity (`agy`) or Claude Code (`claude`), reference the script in your agent workspace settings:
```json
{
  "mcpServers": {
    "chrome-browser": {
      "command": "python3",
      "args": ["/absolute/path/to/samantha-core/mcp/mcp_chrome_contabo.py"],
      "env": {
        "CDP_HOST": "127.0.0.1",
        "CDP_PORT": "9224"
      }
    }
  }
}
```

---

## 🔒 Security Best Practices
- Never hardcode API keys directly inside server scripts.
- Pass environment credentials through the `.env` file or the `"env"` object in `.mcp.json`.
- All browser interactions avoid modal popups and operate safely across background panes.
