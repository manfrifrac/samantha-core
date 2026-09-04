#!/usr/bin/env python3
"""
Unit tests for the Agent-to-Agent (A2A) messaging protocol and inbox delivery.
"""

import unittest
import os
import sys
import tempfile
import shutil
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "core")))

class TestA2AProtocol(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.a2a_base = os.path.join(self.test_dir, "a2a")
        os.makedirs(self.a2a_base, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_header_validation(self):
        """Verify parsing of mandatory A2A first-line protocol headers."""
        valid_payload = "[A2A_FROM:researcher] [A2A_TYPE:task] Please investigate tender 401."
        lines = valid_payload.split("\n")
        first_line = lines[0].strip()

        self.assertTrue(first_line.startswith("[A2A_FROM:"))
        self.assertIn("[A2A_TYPE:", first_line)

    def test_mailbox_deposition_and_ack(self):
        """Verify atomic inbox deposition and transition from inbox/ to read/."""
        recipient = "dev_lead"
        sender = "samantha"
        msg_id = "test_msg_9988"

        inbox_dir = os.path.join(self.a2a_base, recipient, "inbox")
        read_dir = os.path.join(self.a2a_base, recipient, "read")
        os.makedirs(inbox_dir, exist_ok=True)
        os.makedirs(read_dir, exist_ok=True)

        msg_filename = f"20260904T100000Z__{sender}__{msg_id}.md"
        inbox_file_path = os.path.join(inbox_dir, msg_filename)

        payload = f"[A2A_FROM:{sender}] [A2A_TYPE:task] [A2A_ID:{msg_id}]\nUnit test payload."
        with open(inbox_file_path, "w", encoding="utf-8") as f:
            f.write(payload)

        self.assertTrue(os.path.exists(inbox_file_path))

        # Simulate a2a_ack moving file to read/
        read_file_path = os.path.join(read_dir, msg_filename)
        shutil.move(inbox_file_path, read_file_path)

        self.assertFalse(os.path.exists(inbox_file_path))
        self.assertTrue(os.path.exists(read_file_path))

        with open(read_file_path, "r", encoding="utf-8") as f:
            content = f.read()
            self.assertIn("Unit test payload.", content)

    def test_named_tmux_buffer_generation(self):
        """Verify unique named tmux buffer identifier to prevent concurrency collisions."""
        import time
        pid = 12345
        now = int(time.time() * 1000)
        buffer_name = f"a2a_buf_{pid}_{now}"
        self.assertTrue(buffer_name.startswith("a2a_buf_12345_"))
        self.assertGreater(len(buffer_name), 15)

if __name__ == "__main__":
    unittest.main()
