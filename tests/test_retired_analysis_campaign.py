"""Regression tests for the retired summary-based multi-Pi CLI."""

from __future__ import annotations

import argparse
import tempfile
import unittest

from scripts.analysis_campaign import _prepare, _run_agent, _run_round


class RetiredAnalysisCampaignTests(unittest.TestCase):
    """Legacy commands cannot write product analyses or call an Agent."""

    def test_mutating_commands_fail_before_reading_runtime_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            options = argparse.Namespace(runtime_root=temporary)
            for handler in (_prepare, _run_agent, _run_round):
                with self.subTest(handler=handler.__name__):
                    with self.assertRaisesRegex(ValueError, "legacy.*retired"):
                        handler(options)


if __name__ == "__main__":
    unittest.main()
