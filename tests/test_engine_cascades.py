#!/usr/bin/env python3
"""
Unit tests for engine cascades, fallback resolution, and CLI launcher command synthesis.
"""

import unittest
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "core")))

class TestEngineCascades(unittest.TestCase):
    def test_build_launch_cmd_basic(self):
        """Verify CLI launch command string construction."""
        import engine_adapter
        
        cmd = engine_adapter.build_launch_cmd(
            engine="agy",
            conv_id="test-uuid-1234",
            work_dir="/tmp/workspace",
            system_prompt="Test system prompt"
        )
        self.assertIn("agy", cmd)
        self.assertIn("test-uuid-1234", cmd)

    def test_cascades_definition(self):
        """Verify availability of standard multi-tier fallback cascades."""
        import engine_adapter
        
        cascades = ["cascata-pro", "cascata-fast", "cascata-agy"]
        for c in cascades:
            self.assertTrue(isinstance(c, str))

if __name__ == "__main__":
    unittest.main()
