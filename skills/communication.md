# Global Skills — Inter-Agent & Telegram Communication (`communication`)

> **Domain**: A2A (Agent-to-Agent) protocols, delegation prompt engineering, tmux concurrency isolation, CLI composer ghost-suggestion detection, and mobile-optimized Telegram messaging.  
> **Source Reference**: Production implementations across Core dispatchers, A2A queues, and Telegram gateways.

---

## 1. Concurrency-Safe A2A Messaging with Named Tmux Buffers

- **Problem Solved**:
  When multiple agents concurrently dispatch messages via tmux CLI pastes using the default shared buffer, a race condition occurs: buffers overwrite each other, causing Agent A's payload to be injected into Agent B's terminal window.
- **Technical Explanation**:
  Standard `tmux set-buffer` and `tmux paste-buffer` without an explicit `-b <name>` flag operate on a single shared global buffer stack.
- **Implementation Guide**:
  1. Generate an isolated, timestamped buffer identifier:
     ```python
     buffer_name = f"a2a_buf_{os.getpid()}_{int(time.time() * 1000)}"
     ```
  2. Load the payload into the unique buffer:
     ```bash
     tmux set-buffer -b <buffer_name> "<payload>"
     ```
  3. Paste into the target window:
     ```bash
     tmux paste-buffer -b <buffer_name> -t <target_window>
     ```
  4. Immediately purge the temporary buffer:
     ```bash
     tmux delete-buffer -b <buffer_name>
     ```
  5. **Golden Rule**: Always utilize the certified dispatch helper (`core/send_a2a.py`) rather than raw interactive tmux shell commands.

---

## 2. Effective A2A Task Delegation & Prompt Engineering

- **Problem Solved**:
  Vague delegation prompts ("look into X", "see if service is working") lacking explicit deliverables, constraints, or known context force recipient agents to spend thousands of tokens rediscovering information, leading to operational drift and deadlocks.
- **Technical Explanation**:
  An A2A delegation is an executable technical specification, requiring structured clarity:
  1. **Concrete Deliverable**: Specify exact target file paths and formats (e.g. "write report to `/tmp/docs/report_X.md` with 3 sections and isolated unit test results").
  2. **Context & Rationale ("Why")**: Explain high-level objectives so the executor can exercise judgment on edge cases without stalling.
  3. **Known System State (Anti-Rediscovery)**: Provide known PIDs, listening ports, log paths, or URLs directly in the brief to avoid costly full-disk scans.
  4. **Explicit Negative Constraints**: State what **NOT** to do (e.g., "read-only inspection, no service restarts", "markdown files only, zero database records").
  5. **Tangible Definition of Done**: Concrete verification criteria (e.g., "HTTP 200 response", "3/3 unit tests PASS", "A2A acknowledgment recorded").

---

## 3. Detecting Non-Breaking Space (NBSP) Ghost Suggestions in CLI Composers

- **Problem Solved**:
  Modern interactive agent CLIs (Claude Code, Antigravity) display grey ghost suggestions in the terminal prompt (e.g., `❯ Try "..."`). Automation dispatchers mistake ghost suggestions for active user input, causing stuck delivery loops or spurious submissions.
- **Technical Explanation**:
  Terminal UIs (Ink/React Terminal) separate the active prompt character (`❯` or `>`) from placeholder ghost suggestions using a **Unicode Non-Breaking Space (NBSP)** `\u00a0` (UTF-8 `\xc2\xa0`), rather than standard ASCII space (`0x20`).
- **Implementation Guide**:
  ```python
  def is_empty_or_ghost_prompt(line: str) -> bool:
      stripped = line.strip()
      if stripped in ("❯", ">", ""):
          return True
      for prompt_char in ("❯", ">"):
          if line.startswith(prompt_char):
              after = line[len(prompt_char):]
              if after.startswith("\xc2\xa0") or after.startswith("\u00a0"):
                  return True
      return False
  ```

---

## 4. Mandatory First-Line Header Formatting for A2A Messages

- **Problem Solved**:
  Placing `[A2A_FROM:<slug>]` or `[A2A_TYPE:<type>]` tags in the middle or end of message payloads breaks queue parsers (`pool_claim.py`, `send_a2a.py`), routing messages as unknown senders and breaking conversation affinity.
- **Technical Explanation**:
  Stream dispatchers inspect the first 256 bytes for protocol routing headers.
- **Implementation Guide**:
  Every A2A message must start on line 1 with:
  ```text
  [A2A_FROM:<your_slug>] [A2A_TYPE:task|question|report|handoff]
  <Natural language body or brief>
  ```

---

## 5. Lightweight Payloads (File References over Inline Dumps)

- **Problem Solved**:
  Pasting long stack traces, multi-megabyte log files, or extensive codebase diffs directly into A2A messages bloats recipient context windows, inflating token costs for every subsequent conversational turn.
- **Technical Explanation**:
  Context loaded into an active agent conversation is charged across all subsequent prompt iterations.
- **Implementation Guide**:
  1. Write extensive data payloads to disk (e.g., `/tmp/docs/analysis_<slug>.md`).
  2. Send only a 2–3 line summary in the A2A message containing the absolute file path.

---

## 6. Mobile-First Telegram Formatting Guidelines

- **Problem Solved**:
  Sending wide ASCII tables, multi-column borders, or raw unformatted markdown blocks to Telegram renders as garbled text on mobile screens.
- **Technical Explanation**:
  Mobile smartphone viewports lack horizontal space for ASCII table grids (`| --- | --- |`), wrapping lines unpredictably.
- **Implementation Guide**:
  - Never use ASCII box tables on Telegram. Use vertical bulleted lists with emoji indicators (🟢, 🟡, 🔴).
  - Structure updates into concise 2–3 line paragraphs with **bold** key metrics.
  - Deliver lengthy reports as attached PDF/Markdown files using the `[FILE: /path/to/report.pdf]` recap tag.
  - Interactive options must be provided via the `[QUESTION_OPTIONS: Opt A | Opt B | Opt C]` tag.

---

## 🎯 Model Routing Recommendations

- **Primary Engine**: **Claude 3.7 Sonnet (`claude` CLI)**
  - Precision adherence to protocol formatting, structured brief drafting, and mobile typography.
- **Secondary Engine**: **Gemini 2.5 Flash (`agy` CLI)**
  - Fast, cost-effective processing of high-volume notifications and routine A2A status pings.
