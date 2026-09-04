#!/usr/bin/env bash
# ==============================================================================
# Samantha Core — Automated Test Suite Runner
# ==============================================================================
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "======================================================================"
echo "🧪 Running Samantha Core Automated Test Suite"
echo "======================================================================"

TOTAL_TESTS=0
FAILED_TESTS=0

# Ensure python environment is activated if venv exists
if [ -d "venv" ]; then
    PYTHON_BIN="venv/bin/python3"
elif [ -d "/root/ecosistema_agenti/core/venv" ]; then
    PYTHON_BIN="/root/ecosistema_agenti/core/venv/bin/python3"
else
    PYTHON_BIN="python3"
fi

echo "🐍 Python Interpreter: $PYTHON_BIN"
echo ""

for test_file in tests/test_*.py; do
    if [ -f "$test_file" ]; then
        echo "▶️  Executing $test_file..."
        if $PYTHON_BIN -m unittest "$test_file"; then
            echo "   ✅ PASS: $test_file"
        else
            echo "   ❌ FAIL: $test_file"
            FAILED_TESTS=$((FAILED_TESTS + 1))
        fi
        TOTAL_TESTS=$((TOTAL_TESTS + 1))
        echo "----------------------------------------------------------------------"
    fi
done

echo ""
if [ $FAILED_TESTS -eq 0 ]; then
    echo "🎉 ALL $TOTAL_TESTS TEST SUITES PASSED SUCCESSFULLY (100% GREEN)!"
    exit 0
else
    echo "⚠️ $FAILED_TESTS / $TOTAL_TESTS TEST SUITES FAILED!"
    exit 1
fi
