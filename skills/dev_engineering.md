# Global Skills — Development & Debugging (`dev`)

> **Domain**: Software engineering patterns, bug isolation, anti-regression practices, process memory management, and MCP server architecture.  
> **Source Reference**: Production post-mortems and verified engineering implementations across the Samantha Ecosystem.

---

## 1. False Positive Elimination (Unconditional `return True` / `return None` Antipattern)

- **Problem Solved**:
  Core runtime functions that encounter fatal errors or deadlocks (e.g. CLI prompt submission timeouts, IPC send failures, pipeline crashes) returning `True` or `None` unconditionally. Downstream dispatchers (such as Telegram engines or A2A orchestrators) interpret the return value as successful delivery, suppressing alerts and retries, creating silent fleet-wide deadlocks.
- **Technical Explanation**:
  When a function encapsulates retry or fallback logic, if error branches omit explicit boolean updates or default optimistically to `True`, failures are swallowed. Furthermore, if a retry is attempted without subsequent verification, the operation is falsely marked as successful.
- **Implementation Guide**:
  1. Explicitly initialize tracking variables: `delivery_success = False`.
  2. On every non-recoverable error branch (`DEADLOCK`, timeout exceeded, caught exception), explicitly set `delivery_success = False`.
  3. When executing retries, **always re-verify** the outcome: if the retry also fails, escalate state to `DEADLOCK` and keep `delivery_success = False`.
  4. Always return the computed outcome variable: `return delivery_success`.
  5. In data fetching pipelines, distinguish between legitimate empty data (`None`) and retrieval exceptions (explicit error bubbling).

---

## 2. Isolated Unit Testing with Mocks Prior to Modifying Core Code

- **Problem Solved**:
  Modifying mission-critical infrastructure scripts (e.g., `unified_telegram_engine.py` or `send_a2a.py`) directly in production without isolated tests risks introducing fleet-wide outages or shipping untested edge cases.
- **Technical Explanation**:
  Systemic bugs must be reproduced by mocking external side effects (subprocess calls to tmux, network I/O, database mutations) in standalone unit test runners before altering production code.
- **Implementation Guide**:
  1. **Write Bug Reproduction Test**: Create `/tmp/tests/test_bug_<name>.py` reproducing the exact failure mode. Verify that it fails on the unfixed codebase.
  2. **Backup Target File**: Create an explicit backup: `cp target.py target.py.bak_pre_<fix>`.
  3. **Apply Minimal Surgical Patch**: Modify only the precise lines required to resolve the issue.
  4. **Write Multi-Scenario Validation Suite**: Create `/tmp/tests/test_validation_<name>.py` covering:
     - Clean baseline execution → expected `True`
     - Retry with successful recovery → expected `True`
     - Retry with persistent failure / Deadlock → expected `False`
  5. **Static Syntax Verification**: Run `python3 -m py_compile target.py`.
  6. Execute the test suite and verify 100% pass rate before restarting daemons.

---

## 3. Lazy Initialization in MCP Servers to Prevent Handshake Timeouts

- **Problem Solved**:
  MCP servers importing heavy dependencies at module load time (e.g., PyTorch, sentence-transformers, remote database drivers) taking 30–60+ seconds to initialize. Agent CLI clients (Claude Code / Antigravity) enforce strict JSON-RPC handshake timeouts, causing the MCP server to be flagged as dead/failed and stripping tools from the agent.
- **Technical Explanation**:
  During startup, the MCP client establishes standard I/O communication over JSON-RPC. Top-level imports block the event loop, preventing standard I/O readiness before all dependencies load.
- **Implementation Guide**:
  1. Defer all heavy imports and model initializations into tool functions or behind a lazy singleton helper.
  2. Import only core lightweight primitives at the global module level (`mcp.server.fastmcp`).
  3. Implementation pattern:
     ```python
     _model = None

     def get_model():
         global _model
         if _model is None:
             from sentence_transformers import SentenceTransformer
             _model = SentenceTransformer("all-MiniLM-L6-v2")
         return _model

     @mcp.tool()
     def vector_search(query: str):
         model = get_model()
         # perform search logic
     ```

---

## 4. ANSI Escape Stripping & Unicode East Asian Width Box Calculations

- **Problem Solved**:
  In CLI dashboards or boxed terminal interfaces (e.g., supervisor dashboards, status panels), emojis (🟢, 🟡, 🔴, 🛡️, 💰) break right vertical border alignments (`│`), causing jagged borders or unwanted line wraps.
- **Technical Explanation**:
  Standard Python `len("🟢") == 1` counts Unicode code points. However, terminal emulators render emojis as East Asian Wide / Emoji Presentation characters occupying **2 terminal columns**. Calculating line padding with `padding = max_width - len(text)` produces misalignments. Furthermore, ANSI color escape sequences (`\033[32m...\033[0m`) consume character length but zero terminal visual width.
- **Implementation Guide**:
  1. Strip ANSI escape sequences before calculating visual width:
     ```python
     import re, unicodedata

     ANSI_REGEX = re.compile(r"\x1b(?:[@-Z\-_]|\[[0-?]*[ -/]*[@-~])")

     def strip_ansi(text: str) -> str:
         return ANSI_REGEX.sub("", text)
     ```
  2. Compute real visual column width:
     ```python
     def visual_len(text: str) -> int:
         plain = strip_ansi(text)
         width = 0
         for ch in plain:
             if unicodedata.east_asian_width(ch) in ("W", "F"):
                 width += 2
             elif ord(ch) in (0x1F7E2, 0x1F7E1, 0x1F534, 0x26AA, 0x1F6E1, 0x1F4B0, 0x23F0, 0x1F310, 0x2728, 0x26A0):
                 width += 2
             else:
                 width += 1
         return width
     ```
  3. Calculate dynamic padding:
     `padding = " " * max(0, total_width - visual_len(line))`

---

## 5. Long-Running Daemon Code Reloading & Restart Policies

- **Problem Solved**:
  Modifications made to long-running Python daemons (e.g., Telegram engines, memory sensors, supervisor scripts) saved to disk while the running process continues executing stale bytecode cached in RAM. Developers falsely assume the fix is active.
- **Technical Explanation**:
  Python loads modules into `sys.modules` at startup and never reloads them unless the process is explicitly restarted or reloaded.
- **Implementation Guide**:
  1. Never declare a production bug resolved without restarting the host daemon.
  2. Perform controlled daemon restarts:
     ```bash
     python3 core/supervisore_unico.py restart <service_name>
     ```
  3. Post-restart verification: inspect `ps aux | grep <script>` to confirm a new PID, and verify error-free startup logs via `supervisore_unico.py logs <service_name> -n 20`.

---

## 6. Unique Immutable IDs vs Positional Indices in Shared Queues

- **Problem Solved**:
  When multiple asynchronous workers or schedulers operate on ordered lists that are modified, pruned, or filtered dynamically, comparing state by positional index (`array[i]`) causes race conditions, phantom updates, and data overwrites.
- **Technical Explanation**:
  If the server removes the first 2 processed items from a queue, the item previously at index 3 shifts to index 1. A client checking index 1 falsely detects a mutation of item 1 rather than a positional shift.
- **Implementation Guide**:
  1. Assign every item an immutable unique identifier (e.g., UUID or SHA-256 content hash).
  2. Synchronize and mutate data structures using ID-indexed dictionaries (`dict[item_id, item]`), never array positions.
  3. In diff algorithms, always match identity via `item.id == server_item.id`.

---

## 7. Postgres JSONB Upsert `||` Merge Prevents Key Deletion

- **Problem Solved**:
  Removing a dictionary key in Python (`d.pop("key", None)`) followed by a PostgreSQL upsert using the JSONB `||` operator (`SET data = table.data || EXCLUDED.data`) fails to delete the key: on subsequent reads, the old key persists silently.
- **Technical Explanation**:
  In PostgreSQL, `jsonb || jsonb` performs a shallow merge where keys present in the right operand overwrite the left operand, but keys missing from the right operand are retained from the left operand.
- **Implementation Guide**:
  1. To delete keys in JSONB, use explicit removal operators: `SET data = data - 'key'` (or `#- '{nested,path}'`).
  2. Expose explicit deletion parameters in persistence abstraction layers (e.g. `save_db(..., remove_keys=["key"])`).
  3. Always verify mutations against a test database record before marking persistence bugs resolved.

---

## 8. Expanding Context Windows to 1M Tokens via `[1m]` Suffix

- **Problem Solved**:
  Flat-rate models capped at default 200k token windows, triggering unnecessary session context refreshes on heavy workloads.
- **Technical Explanation**:
  Extended 1M token windows on Anthropic models require explicit environment variables combined with the `[1m]` model identifier suffix (e.g., `claude-sonnet-5[1m]`).
- **Implementation Guide**:
  1. Configure launcher environment: `CLAUDE_CODE_MAX_CONTEXT_TOKENS=1000000` and `CLAUDE_CODE_AUTO_COMPACT_WINDOW=800000`.
  2. Pass `[1m]` model suffixes for Sonnet and Opus models.
  3. Note: Lightweight models like Haiku remain capped at 200k.
  4. Align context monitoring alarm thresholds (e.g. 750k/900k) to prevent premature context alerts.

---

## 9. Audio Transcoding to Ogg/Opus with `ffprobe` Verification

- **Problem Solved**:
  Voice notes and audio payloads sent to messaging bridges (e.g., WhatsApp Baileys or Telegram Voice) failing playback due to improper codecs, despite HTTP 200 upload confirmations.
- **Technical Explanation**:
  Messaging voice notes require explicit Ogg/Opus encoding (mono, 24kHz). API success indicates network receipt, not codec compatibility.
- **Implementation Guide**:
  1. Inspect source codec:
     ```bash
     ffprobe -v error -show_entries stream=codec_name -of csv=p=0 <input_file>
     ```
  2. Transcode to standard Opus container:
     ```bash
     ffmpeg -i <input_file> -ac 1 -ar 24000 -c:a libopus -b:a 32k <output_file>.ogg
     ```
  3. Transcode to a temporary file, preserving source files.
  4. Perform post-transcode `ffprobe` validation on the output file before dispatch.

---

## 10. Sequential Test Runner Traps with `sys.exit()`

- **Problem Solved**:
  Running multiple test files in a single invocation (`python3 -m unittest test_a.py test_b.py`) falsely reports all tests green, but if `test_a.py` invokes `sys.exit()`, the Python interpreter terminates immediately and `test_b.py` is never executed.
- **Technical Explanation**:
  `sys.exit()` raises `SystemExit`. When test files are written as standalone executable scripts rather than standard `unittest.TestCase` suites, calling `sys.exit()` terminates the parent test runner.
- **Implementation Guide**:
  1. Execute standalone test scripts independently in isolated subprocesses.
  2. Structure all test suites as standard `unittest.TestCase` classes without top-level `sys.exit()` statements.
  3. Verify explicit test counts in execution logs to ensure all test suites ran to completion.

---

## 🎯 Model Routing Recommendations

- **Primary Engine**: **Claude 3.7 Sonnet / Opus (`claude` CLI)**
  - Unmatched logical precision, strict adherence to anti-regression protocols, and robust mock generation.
- **Secondary Engine**: **Gemini 2.5 Pro (`agy` CLI)**
  - Ideal for large-scale multi-file refactors spanning 1M+ token context windows.
