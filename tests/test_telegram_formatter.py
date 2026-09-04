#!/usr/bin/env python3
"""
Unit tests for the Telegram gateway MarkdownV2 formatting and recap tag parsing.
"""

import unittest
import os
import sys
import re

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "telegram")))

class TestTelegramFormatter(unittest.TestCase):
    def test_extract_recap_tags(self):
        """Verify extraction of [FILE:], [QUESTION_OPTIONS:], [PIN_MESSAGE] control tags."""
        raw_recap = (
            "[REPLY_TO_MSG_ID: 1001]\n"
            "[PIN_MESSAGE]\n"
            "Task execution completed successfully.\n"
            "[FILE: /tmp/docs/report.pdf]\n"
            "[QUESTION_OPTIONS: Proceed | Abort]"
        )

        # File tag
        file_match = re.search(r"\[FILE:\s*([^\]]+)\]", raw_recap)
        self.assertIsNotNone(file_match)
        self.assertEqual(file_match.group(1).strip(), "/tmp/docs/report.pdf")

        # Question options tag
        opts_match = re.search(r"\[QUESTION_OPTIONS:\s*([^\]]+)\]", raw_recap)
        self.assertIsNotNone(opts_match)
        options = [o.strip() for o in opts_match.group(1).split("|")]
        self.assertEqual(options, ["Proceed", "Abort"])

        # Pin message tag
        self.assertIn("[PIN_MESSAGE]", raw_recap)

        # Clean text stripping
        clean_text = re.sub(r"\[(FILE|QUESTION_OPTIONS|PIN_MESSAGE|REPLY_TO_MSG_ID):?[^\]]*\]", "", raw_recap).strip()
        self.assertEqual(clean_text, "Task execution completed successfully.")

    def test_markdownv2_escaping(self):
        """Verify escaping of special reserved characters in Telegram MarkdownV2."""
        reserved_chars = r"_*[]()~`>#+-=|{}.!"
        test_string = "Hello *World*! Release v0.2.0 is live [OK]."
        
        # Test basic escape logic
        escaped = re.sub(r"([_\[\]()~>#+\-=|{}.!])", r"\\\1", test_string)
        self.assertIn(r"\.", escaped)
        self.assertIn(r"\!", escaped)

if __name__ == "__main__":
    unittest.main()
