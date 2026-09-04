# Automated Test Suite & Toolchain

> **Directory**: `tests/`  
> **Framework**: Python `unittest` standard library with isolated mock runners  
> **Execution Constraint**: Zero side-effects on production databases or live Tmux sessions.

---

## 🧪 Test Suite Overview

The Samantha Core test suite provides regression verification, security audits, and protocol compliance checks for all core infrastructure components.

| Test File | Component Under Test | Scope & Verification Criteria |
|---|---|---|
| [`test_a2a_protocol.py`](./test_a2a_protocol.py) | `core/send_a2a.py`, `core/a2a_ack.py` | A2A headers (`[A2A_FROM:]`), atomic mailbox file deposition, named buffer concurrency, ack state shifts (`inbox/` -> `read/`). |
| [`test_agent_db.py`](./test_agent_db.py) | `core/agent_db.py` | Database persistence, JSONB merge protection, key deletion via `remove_keys`, fallback handling. |
| [`test_secret_masking.py`](./test_secret_masking.py) | `core/secret_patterns.py`, `core/secret_env.py` | Regex pattern redaction for API keys, OAuth tokens, passwords, and safe environment loading. |
| [`test_self_healing_evaluator.py`](./test_self_healing_evaluator.py) | `core/self_healing_stato.py` | 5-Tier hierarchical health evaluation (Green, Yellow, Orange, Red, Emergency recovery triggers). |
| [`test_engine_cascades.py`](./test_engine_cascades.py) | `core/engine_adapter.py` | Multi-engine CLI command building, cascade resolution (`cascata-pro`, `cascata-fast`, `cascata-agy`), prompt injection. |
| [`test_telegram_formatter.py`](./test_telegram_formatter.py) | `telegram/telegram_format.py` | Mobile MarkdownV2 escaping, tag parsing (`[FILE:]`, `[QUESTION_OPTIONS:]`, `[PIN_MESSAGE]`), mobile table suppression. |
| [`test_mcp_lazy_init.py`](./test_mcp_lazy_init.py) | `mcp/*.py` | Lazy module dependency loading to guarantee < 1.0s JSON-RPC handshake times. |

---

## 🚀 Running Tests

### 1. Run All Tests via Root Test Runner
From the root of the repository:
```bash
chmod +x run_tests.sh
./run_tests.sh
```

### 2. Run Individual Test Suites
```bash
python3 -m unittest tests/test_a2a_protocol.py
python3 -m unittest tests/test_secret_masking.py
python3 -m unittest tests/test_telegram_formatter.py
```

---

## 📐 Testing Best Practices & Rules

1. **Strict Mocking**: All interactions with Tmux, external HTTP services, or live system daemons must be mocked using `unittest.mock`.
2. **Never Call `sys.exit()` in Test Modules**: As documented in [`skills/dev_engineering.md`](../skills/dev_engineering.md), invoking `sys.exit()` in test files terminates the parent runner prematurely. Always use standard `self.assertEqual`, `self.assertTrue`, or raise `AssertionError`.
3. **Pre-Commit Requirement**: Every core code modification must pass the full test suite before committing.
