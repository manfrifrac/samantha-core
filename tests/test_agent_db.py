#!/usr/bin/env python3
"""
Unit tests for agent persistence, JSONB merge protection, and key deletion.
"""

import unittest
import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "core")))

class TestAgentDB(unittest.TestCase):
    def test_resolve_agent_id(self):
        """Verify fallback resolution of agent identifiers."""
        import agent_db
        
        # Test fallback
        resolved = agent_db.resolve_agent_id(None)
        self.assertIn(resolved, ("samantha", "betty"))

    @patch("psycopg2.connect")
    def test_jsonb_merge_and_remove_keys(self, mock_connect):
        """Verify that remove_keys generates explicit SQL key deletion queries."""
        import agent_db
        
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_connect.return_value = mock_conn
        mock_conn.cursor.return_value = mock_cur

        agent_db._AGENTS_DB = {
            "test_agent": {
                "name": "Test Agent",
                "temporary_flag": "to_remove"
            }
        }

        # Save with key removal
        agent_db.save_db(remove_keys={"test_agent": ["temporary_flag"]})

        # Verify SQL statements executed
        calls = [str(c) for c in mock_cur.execute.mock_calls]
        self.assertTrue(any("UPDATE agents SET data = data - %s" in c for c in calls))
        self.assertNotIn("temporary_flag", agent_db._AGENTS_DB.get("test_agent", {}))

if __name__ == "__main__":
    unittest.main()
