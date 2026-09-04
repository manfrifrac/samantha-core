# Browser Automation & Human-in-the-Loop Policy

> **Scope**: Chromium CDP protocol standards, server isolation (`:9224`), residential proxy fallbacks, and Human-in-the-Loop CAPTCHA handoff protocols.

---

## 🌐 Dual-Browser Environment Architecture

1. **Primary Headless Server Instance (`chrome-contabo`, Port `:9224`)**:
   - The default browser for all scraping, form filling, document extraction, and portal interaction.
   - Operates on a dedicated headless X11 virtual display (`Xvfb :99`) with noVNC human intervention support.
2. **Local Surface Bridge (`chrome-local`, Port `:9222`) — Transition Fallback**:
   - Reserved strictly as an emergency fallback for persistent authenticated sessions or high-friction Cloudflare Turnstile barriers requiring residential ASN IP routing.

---

## 🛡️ Operational Directives for Browser Automation

1. **Background Tab Safety (No Window Focusing)**:
   - Agents must never call `bringToFront()` or `window.focus()` on active browser tabs. Always interact with tabs in the background. Screenshots and CDP commands function reliably without window focus.
2. **Prohibition of OS Download/Upload Dialogs**:
   - Native OS modal dialogs block headless browser threads.
   - Use CDP `DOM.setFileInputFiles` for file uploads and `Page.printToPDF` for document exports.
3. **Tab Lifecycle & Garbage Collection**:
   - Always terminate tabs upon workflow completion. Scraping tasks must never leave orphaned tabs open.

---

## 🧩 Human-in-the-Loop (HITL) CAPTCHA Handoff Protocol

When encountering complex interactive challenges (e.g. 2FA, biometric checks, complex visual CAPTCHAs):

```text
[Bot/Agent Automation]
       │
       ▼ (Detects Hard Barrier)
[chrome_contabo_rileva_captcha]
       │
       ▼ (Leaves tab open + captures screenshot)
[chrome_contabo_ask_human] ───► Telegram Alert to Human Supervisor with noVNC Link
                                       │
                                       ▼ (Human resolves challenge via noVNC)
[chrome_contabo_human_stato]  ◄── Human sends "Done" on Telegram
       │
       ▼ (Validates session unlock)
[chrome_contabo_human_risolvi] ───► Automation Resumes Seamlessly
```

1. **Detection**: Run `chrome_contabo_rileva_captcha` to verify barrier status.
2. **Escalation**: Trigger `chrome_contabo_ask_human`, capturing an annotated screenshot and dispatching an alert to the supervisor's Telegram topic.
3. **Verification & Resumption**: When the human resolves the challenge via noVNC and confirms on Telegram, run `chrome_contabo_human_stato` followed by `chrome_contabo_human_risolvi` to resume automation.
