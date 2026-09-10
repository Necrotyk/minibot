# SPDX-License-Identifier: AGPL-3.0-or-later
"""
test_url_ignore.py — Unit Tests for IgnoredNicksManager and URL Preview Suppression.
"""

import json
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from core.ignore_manager import IgnoredNicksManager
from plugins.urls import URLPlugin


class TestIgnoredNicksManager(unittest.TestCase):
    """Test suite for IgnoredNicksManager singleton and persistence."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.storage_file = os.path.join(self.temp_dir.name, "ignored_test.json")

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_add_and_list_nicks(self):
        mgr = IgnoredNicksManager(state_file=self.storage_file)
        self.assertTrue(mgr.add("custom_feed"))
        self.assertFalse(mgr.add("custom_feed"))  # Duplicate
        self.assertIn("custom_feed", mgr.list_ignored())

    def test_remove_nick(self):
        mgr = IgnoredNicksManager(state_file=self.storage_file)
        mgr.add("relay_test")
        self.assertTrue(mgr.remove("relay_test"))
        self.assertFalse(mgr.remove("relay_test"))  # Already removed
        self.assertNotIn("relay_test", mgr.list_ignored())

    def test_is_ignored_case_insensitive(self):
        mgr = IgnoredNicksManager(state_file=self.storage_file)
        mgr.add("CustomFeed")
        self.assertTrue(mgr.is_ignored("customfeed"))
        self.assertTrue(mgr.is_ignored("CUSTOMFEED"))
        self.assertTrue(mgr.is_ignored("CustomFeed"))
        self.assertFalse(mgr.is_ignored("alice"))

    def test_is_ignored_wildcards(self):
        mgr = IgnoredNicksManager(state_file=self.storage_file)
        mgr.add("feed_*")
        self.assertTrue(mgr.is_ignored("feed_news"))
        self.assertTrue(mgr.is_ignored("feed_sec"))
        self.assertFalse(mgr.is_ignored("human_user"))

    def test_is_ignored_default_bots(self):
        mgr = IgnoredNicksManager(state_file=self.storage_file)
        # Bots ending with 'bot' or in DEFAULT_KNOWN_BOTS are automatically ignored
        self.assertTrue(mgr.is_ignored("vulnavia"))
        self.assertTrue(mgr.is_ignored("infobot"))
        self.assertTrue(mgr.is_ignored("some_auto_bot"))

    def test_is_ignored_lertz_bot_variants_and_irc_modes(self):
        mgr = IgnoredNicksManager(state_file=self.storage_file)
        self.assertTrue(mgr.is_ignored("lertz_dystopi"))
        self.assertTrue(mgr.is_ignored("lertz_admin"))
        self.assertTrue(mgr.is_ignored("@lertz_dystopi"))
        self.assertTrue(mgr.is_ignored("+lertz"))
        self.assertTrue(mgr.is_ignored("~lertz_admin"))
        self.assertTrue(mgr.is_ignored("%lertz_bot"))
        self.assertTrue(mgr.is_ignored("lertz_conn"))


    def test_persistence_across_instances(self):
        mgr1 = IgnoredNicksManager(state_file=self.storage_file)
        mgr1.add("feed1")
        mgr1.add("relay_*")

        mgr2 = IgnoredNicksManager(state_file=self.storage_file)
        self.assertTrue(mgr2.is_ignored("feed1"))
        self.assertTrue(mgr2.is_ignored("relay_node"))
        self.assertEqual(sorted(mgr2.list_ignored()), ["feed1", "relay_*"])

    def test_handles_corrupt_file_gracefully(self):
        with open(self.storage_file, "w") as f:
            f.write("{corrupt json content")

        mgr = IgnoredNicksManager(state_file=self.storage_file)
        self.assertEqual(mgr.list_ignored(), [])
        # Should still allow adding new nicks
        self.assertTrue(mgr.add("valid_nick"))
        self.assertTrue(mgr.is_ignored("valid_nick"))


class TestURLPluginIgnoreIntegration(unittest.TestCase):
    """Test suite verifying URLPlugin respects IgnoredNicksManager."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.storage_file = os.path.join(self.temp_dir.name, "ignored_test.json")
        self.ignore_mgr = IgnoredNicksManager(state_file=self.storage_file)
        self.ignore_mgr.add("custom_spammer")
        self.ignore_mgr.add("feed_*")

    def tearDown(self):
        self.temp_dir.cleanup()

    @patch("plugins.urls.get_ignore_manager")
    @patch("plugins.urls.get_url_title")
    def test_drops_urls_from_ignored_nick(self, mock_get_title, mock_get_mgr):
        mock_get_mgr.return_value = self.ignore_mgr
        mock_bot = MagicMock()
        plugin = URLPlugin(mock_bot)

        # Message from ignored nick
        plugin.on_privmsg(
            "#lertz-dystopi",
            "custom_spammer",
            "Check this out: https://example.com/advisory",
        )

        # get_url_title and bot.send_msg should never be called
        mock_get_title.assert_not_called()
        mock_bot.send_msg.assert_not_called()

    @patch("plugins.urls.get_ignore_manager")
    @patch("plugins.urls.get_url_title")
    def test_processes_urls_from_regular_user(self, mock_get_title, mock_get_mgr):
        mock_get_mgr.return_value = self.ignore_mgr
        mock_get_title.return_value = "Example Advisory Title"
        mock_bot = MagicMock()
        plugin = URLPlugin(mock_bot)

        # Message from unignored user
        plugin.on_privmsg(
            "#general",
            "admin",
            "Check this out: https://example.com/advisory",
        )

        mock_get_title.assert_called_once_with("https://example.com/advisory")
        mock_bot.send_msg.assert_called_once()
        self.assertIn("Example Advisory Title", mock_bot.send_msg.call_args[0][1])

    @patch("plugins.urls.get_ignore_manager")
    @patch("plugins.urls.get_url_title")
    def test_drops_youtube_urls(self, mock_get_title, mock_get_mgr):
        mock_get_mgr.return_value = self.ignore_mgr
        mock_bot = MagicMock()
        plugin = URLPlugin(mock_bot)

        # Standard YouTube URL
        plugin.on_privmsg("#lertz", "user1", "https://www.youtube.com/watch?v=dQw4w9WgXcQ")
        mock_get_title.assert_not_called()
        mock_bot.send_msg.assert_not_called()

        # youtu.be short URL
        plugin.on_privmsg("#lertz", "user1", "https://youtu.be/dQw4w9WgXcQ")
        mock_get_title.assert_not_called()
        mock_bot.send_msg.assert_not_called()


class TestInfobotIgnoreIntegration(unittest.TestCase):
    """Test suite verifying InfobotPlugin respects IgnoredNicksManager."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.storage_file = os.path.join(self.temp_dir.name, "ignored_test.json")
        self.ignore_mgr = IgnoredNicksManager(state_file=self.storage_file)
        self.ignore_mgr.add("relay_user")

    def tearDown(self):
        self.temp_dir.cleanup()

    @patch("core.ignore_manager.get_ignore_manager")
    @patch("sideload.infobot.get_web_title")
    def test_infobot_drops_urls_from_ignored_nick(self, mock_get_title, mock_get_mgr):
        from sideload.infobot import Plugin as InfobotPlugin
        mock_get_mgr.return_value = self.ignore_mgr
        mock_bot = MagicMock()
        plugin = InfobotPlugin(mock_bot)

        plugin.on_privmsg("#lertz", "relay_user", "https://example.com/news")

        mock_get_title.assert_not_called()
        mock_bot.send_msg.assert_not_called()


if __name__ == "__main__":
    unittest.main()
