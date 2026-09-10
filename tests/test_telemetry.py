# SPDX-License-Identifier: AGPL-3.0-or-later
"""
test_telemetry.py — Unit Tests for SQLite WAL Telemetry & Token Recording.
"""

import tempfile
import unittest
from pathlib import Path

from core.telemetry import TelemetryEngine


class TestTelemetry(unittest.TestCase):
    def setUp(self):
        self.temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.temp_db.close()
        self.telemetry = TelemetryEngine(self.temp_db.name)

    def tearDown(self):
        Path(self.temp_db.name).unlink(missing_ok=True)

    def test_record_and_summary(self):
        self.telemetry.record_usage(
            source_bot="testbot",
            model="gemini-2.5-flash",
            provider="google",
            tokens_in=100,
            tokens_out=50,
            latency_ms=250.0,
            channel="#test",
            user_nick="alice"
        )
        self.telemetry.record_usage(
            source_bot="testbot",
            model="gemini-2.5-flash",
            provider="google",
            tokens_in=200,
            tokens_out=150,
            latency_ms=350.0,
            channel="#test",
            user_nick="bob"
        )

        summary = self.telemetry.get_summary(hours=1)
        self.assertEqual(summary["requests"], 2)
        self.assertEqual(summary["tokens_in"], 300)
        self.assertEqual(summary["tokens_out"], 200)
        self.assertEqual(summary["avg_latency_ms"], 300.0)


if __name__ == "__main__":
    unittest.main()
