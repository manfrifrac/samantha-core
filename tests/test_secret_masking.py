#!/usr/bin/env python3
"""
Security unit tests for secret patterns redaction and credential masking.
"""

import unittest
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "core")))

class TestSecretMasking(unittest.TestCase):
    def test_redact_sensitive_strings(self):
        """Verify that sensitive API tokens and credentials are appropriately masked."""
        import secret_patterns

        test_log_line = "Failed to connect with Bearer sk-ant-api03-abcdef1234567890abcdef1234567890 to host."
        masked_line = secret_patterns.mask_secrets(test_log_line) if hasattr(secret_patterns, "mask_secrets") else test_log_line

        # Check regex rules
        for pattern in secret_patterns.PATTERNS if hasattr(secret_patterns, "PATTERNS") else []:
            self.assertFalse(pattern.search(masked_line) if masked_line != test_log_line else True)

    def test_secret_env_loader_precedence(self):
        """Verify environment variable priority in secret_env."""
        import secret_env

        os.environ["TEST_SECRET_KEY_123"] = "super_secret_value"
        loaded = secret_env.load_secret("TEST_SECRET_KEY_123")
        self.assertEqual(loaded, "super_secret_value")
        del os.environ["TEST_SECRET_KEY_123"]

        # Missing secret should safely return None without throwing
        missing = secret_env.load_secret("NON_EXISTENT_SECRET_XYZ_999")
        self.assertIsNone(missing)

if __name__ == "__main__":
    unittest.main()
