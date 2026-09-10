# SPDX-License-Identifier: AGPL-3.0-or-later
"""
test_jules.py — Unit Tests for Google Jules API Client & Activity Poller.
"""

import unittest
from unittest.mock import MagicMock, patch

from jules.client import JulesClient
from jules.poller import JulesPoller


class TestJulesEngine(unittest.TestCase):
    def setUp(self):
        self.client = JulesClient(api_key="test_key")

    @patch("jules.client.urllib.request.urlopen")
    def test_create_session(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"name": "sessions/sess-123", "title": "test task"}'
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        res = self.client.create_session("Implement health check", "owner/repo", "main")
        self.assertIsNotNone(res)
        self.assertEqual(res.get("name"), "sessions/sess-123")

    @patch("jules.client.urllib.request.urlopen")
    def test_approve_plan(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"name": "sessions/sess-123", "state": "IN_PROGRESS"}'
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        res = self.client.approve_plan("sess-123")
        self.assertIsNotNone(res)
        self.assertEqual(res.get("state"), "IN_PROGRESS")

    def test_poller_tracking(self):
        poller = JulesPoller(self.client)
        poller.track_session("sess-123", "my_task", "#jules")
        self.assertIn("sess-123", poller.active_sessions)
        self.assertEqual(poller.active_sessions["sess-123"]["channel"], "#jules")

        poller.untrack_session("sess-123")
        self.assertNotIn("sess-123", poller.active_sessions)

    @patch("jules.client.urllib.request.urlopen")
    def test_multi_key_rotation_and_429_failover(self, mock_urlopen):
        import urllib.error
        client = JulesClient(api_keys=["key1", "key2", "key3"])
        self.assertEqual(client.api_key, "key1")

        # Mock first call failing with 429, second call succeeding
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"name": "sessions/sess-rot"}'
        mock_urlopen.side_effect = [
            urllib.error.HTTPError("http://test", 429, "Too Many Requests", {}, None),
            MagicMock(__enter__=MagicMock(return_value=mock_resp))
        ]

        res = client.get_session("sess-rot")
        self.assertIsNotNone(res)
        self.assertEqual(res.get("name"), "sessions/sess-rot")
        self.assertEqual(client.api_key, "key2")


class TestJulesBotPlugin(unittest.TestCase):
    def setUp(self):
        self.mock_bot = MagicMock()
        self.mock_bot.nick = "julesbot"
        self.sent_msgs = []
        self.mock_bot.send_msg.side_effect = lambda target, msg: self.sent_msgs.append((target, msg))

    @patch("plugins.julesbot.JulesPoller")
    @patch("plugins.julesbot.JulesClient")
    def test_jules_help_output(self, mock_client, mock_poller):
        from plugins.julesbot import JulesBotPlugin
        plugin = JulesBotPlugin(self.mock_bot, api_key="dummy")

        # Test !help in #jules
        self.sent_msgs.clear()
        plugin.on_privmsg("#jules", "admin", "!help")
        self.assertTrue(len(self.sent_msgs) >= 5)
        self.assertTrue(any("Google Jules" in msg[1] for msg in self.sent_msgs))
        self.assertTrue(any("!init" in msg[1] for msg in self.sent_msgs))

        # Test !jules in #general
        self.sent_msgs.clear()
        plugin.on_privmsg("#general", "admin", "!jules")
        self.assertTrue(len(self.sent_msgs) >= 5)

        # Test repos command
        self.sent_msgs.clear()
        plugin.on_privmsg("#jules", "admin", "!repos")
        self.assertEqual(len(self.sent_msgs), 1)
        self.assertIn("Jules Repos", self.sent_msgs[0][1])


if __name__ == "__main__":
    unittest.main()
