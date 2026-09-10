# SPDX-License-Identifier: AGPL-3.0-or-later
"""
test_llm_router.py — Unit Tests for LLM Provider Routing & Think Suppression.
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from core.llm_router import LLMRouter, strip_think_tags
from core.telemetry import TelemetryEngine


class TestLLMRouter(unittest.TestCase):
    def setUp(self):
        self.temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.temp_db.close()
        self.telemetry = TelemetryEngine(self.temp_db.name)
        self.router = LLMRouter(
            config={
                "GEMINI_API_KEY": "test_gemini_key",
                "LOCAL_LLM_URL": "",
                "KAGGLE_GPU_URL": ""
            },
            telemetry=self.telemetry
        )

    def tearDown(self):
        Path(self.temp_db.name).unlink(missing_ok=True)

    def test_strip_think_tags(self):
        raw = "<think>Let me reason deeply about this question...</think>Here is the actual response."
        cleaned = strip_think_tags(raw)
        self.assertEqual(cleaned, "Here is the actual response.")

    @patch("core.llm_router.urllib.request.urlopen")
    def test_gemini_api_routing(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"candidates": [{"content": {"parts": [{"text": "<think>planning</think>Hello from Gemini!"}]}}]}'
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        reply = self.router.generate("Say hello", source_bot="testbot", channel="#test")
        self.assertEqual(reply, "Hello from Gemini!")

        # Verify telemetry recording
        stats = self.telemetry.get_summary(hours=1)
        self.assertEqual(stats["requests"], 1)


if __name__ == "__main__":
    unittest.main()
