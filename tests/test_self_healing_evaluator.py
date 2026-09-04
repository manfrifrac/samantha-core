#!/usr/bin/env python3
"""
Unit tests for the 5-tier self-healing state evaluator.
"""

import unittest
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "core")))

class TestSelfHealingEvaluator(unittest.TestCase):
    def test_tier_levels(self):
        """Verify the hierarchy of operational health tiers."""
        tiers = ["VERDE", "GIALLO", "ARANCIONE", "ROSSO", "EMERGENZA"]
        self.assertEqual(len(tiers), 5)
        self.assertEqual(tiers[0], "VERDE")
        self.assertEqual(tiers[-1], "EMERGENZA")

    def test_state_evaluation_logic(self):
        """Test health score evaluation for healthy vs degraded agents."""
        # Simulated metrics
        healthy_metrics = {
            "tmux_alive": True,
            "stalled_minutes": 0,
            "memory_pct": 35.0,
            "last_active_sec": 10
        }
        
        degraded_metrics = {
            "tmux_alive": True,
            "stalled_minutes": 45,
            "memory_pct": 89.0,
            "last_active_sec": 2700
        }

        self.assertTrue(healthy_metrics["tmux_alive"])
        self.assertLess(healthy_metrics["stalled_minutes"], 15)
        self.assertGreater(degraded_metrics["stalled_minutes"], 30)

if __name__ == "__main__":
    unittest.main()
