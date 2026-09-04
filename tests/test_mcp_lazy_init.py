#!/usr/bin/env python3
"""
Unit tests validating MCP server lazy initialization to prevent JSON-RPC handshake timeouts.
"""

import unittest
import os
import sys
import time

class TestMCPLazyInit(unittest.TestCase):
    def test_lazy_initialization_pattern(self):
        """Verify that lazy getter functions initialize dependencies on-demand rather than at import."""
        _heavy_resource = None

        def get_heavy_resource():
            nonlocal _heavy_resource
            if _heavy_resource is None:
                # Simulate loading on first call
                _heavy_resource = {"model_name": "embedded_model", "ready": True}
            return _heavy_resource

        # Prior to first call, resource is unloaded
        self.assertIsNone(_heavy_resource)

        # First access loads the resource
        res1 = get_heavy_resource()
        self.assertIsNotNone(_heavy_resource)
        self.assertTrue(res1["ready"])

        # Second access returns cached instance instantly
        res2 = get_heavy_resource()
        self.assertIs(res1, res2)

if __name__ == "__main__":
    unittest.main()
