# SPDX-License-Identifier: AGPL-3.0-or-later
"""
test_infobot_grokpedia.py — Fail-Red Repro Test for Grokipedia / Grokpedia Source and Branding.
"""

import unittest
from unittest.mock import MagicMock, patch

from sideload.infobot import search_grokipedia, Plugin as InfobotPlugin


class TestGrokpediaSourceAndBranding(unittest.TestCase):
    """Verifies that Grokipedia queries fetch from grokipedia.com, never return Wikipedia branding or URLs."""

    def test_grokipedia_search_extracts_grokipedia_content(self):
        """Verify search_grokipedia parses grokipedia.com search results with Grokipedia branding and link."""
        fake_html = """
        <!DOCTYPE html><html><body>
        Search for <span class="font-mono text-fg-secondary">"linux"</span> &mdash; 1,000 results
        <div class="relative flex min-h-[432px] flex-col gap-0.5">
          <a href="/page/Linux" class="group block" data-slug="Linux" data-search-result-link="true"
             data-search-snippet="Linux is a free and open-source Unix-like operating system kernel originally authored by Linus Torvalds.">
            <div>
              <span class="truncate text-sm font-medium text-fg-primary"> Linux </span>
              <p class="mt-1 line-clamp-2 text-sm"> Linux is a free and open-source Unix-like operating system kernel originally authored by Linus Torvalds. </p>
            </div>
          </a>
        </div>
        </body></html>
        """
        mock_resp = MagicMock()
        mock_resp.read.return_value = fake_html.encode("utf-8")
        mock_resp.__enter__.return_value = mock_resp

        with patch("urllib.request.urlopen", return_value=mock_resp) as mock_urlopen:
            result = search_grokipedia("linux")
            # Verify request went to grokipedia.com
            req = mock_urlopen.call_args[0][0]
            url = req.full_url if hasattr(req, "full_url") else str(req)
            self.assertIn("grokipedia.com", url)
            self.assertNotIn("duckduckgo.com", url)
            self.assertNotIn("wikipedia.org", url)

            # Verify badge and source link
            self.assertIn("[Grokipedia]", result)
            self.assertNotIn("[Wikipedia]", result)
            self.assertIn("https://grokipedia.com/page/Linux", result)
            self.assertNotIn("wikipedia.org", result)
            self.assertIn("Linus Torvalds", result)

    def test_grokipedia_no_article_found(self):
        """Verify proper [Grokipedia] not found messaging instead of falling back to Wikipedia."""
        fake_html = """
        <!DOCTYPE html><html><body>
        Search for <span class="font-mono text-fg-secondary">"nonexistenttopic123"</span> &mdash; 0 results
        </body></html>
        """
        mock_resp = MagicMock()
        mock_resp.read.return_value = fake_html.encode("utf-8")
        mock_resp.__enter__.return_value = mock_resp

        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = search_grokipedia("nonexistenttopic123")
            self.assertIn("[Grokipedia]", result)
            self.assertNotIn("[Wikipedia]", result)
            self.assertNotIn("wikipedia.org", result)
            self.assertIn("No article found for 'nonexistenttopic123'", result)

    def test_empty_query_usage(self):
        """Verify empty or whitespace query returns usage instructions."""
        self.assertEqual(search_grokipedia(""), "Usage: !grok <topic>")
        self.assertEqual(search_grokipedia("   "), "Usage: !grok <topic>")

    def test_network_error_handling(self):
        """Verify network errors return a clean error badge without falling back to Wikipedia."""
        with patch("urllib.request.urlopen", side_effect=Exception("Connection timed out")):
            result = search_grokipedia("linux")
            self.assertIn("[Grokipedia Error]", result)
            self.assertNotIn("[Wikipedia]", result)
            self.assertNotIn("wikipedia.org", result)

    def test_exact_match_selection(self):
        """Verify exact title/slug match is prioritized over partial matches."""
        fake_html = """
        <!DOCTYPE html><html><body>
        <div class="relative flex flex-col gap-0.5">
          <a href="/page/Grok_Axeoth" class="group block" data-slug="Grok_Axeoth"
             data-search-snippet="Grok is a hero character in Heroes of Might and Magic IV.">
            <div><span class="truncate"> Grok (Axeoth) </span></div>
          </a>
          <a href="/page/Grok" class="group block" data-slug="Grok"
             data-search-snippet="xAI was founded in July 2023 by Elon Musk. Grok was the first AI model.">
            <div><span class="truncate"> Grok </span></div>
          </a>
        </div>
        </body></html>
        """
        mock_resp = MagicMock()
        mock_resp.read.return_value = fake_html.encode("utf-8")
        mock_resp.__enter__.return_value = mock_resp

        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = search_grokipedia("grok")
            self.assertIn("https://grokipedia.com/page/Grok", result)
            self.assertIn("xAI was founded", result)


    def test_plugin_command_triggers(self):
        """Verify !grok, !grokipedia, and !grokpedia trigger search_grokipedia."""
        sent_messages = []
        bot = MagicMock()
        bot.nick = "toolz"
        bot.send_msg = lambda target, msg: sent_messages.append((target, msg))
        plugin = InfobotPlugin(bot)

        with patch("sideload.infobot.search_grokipedia", return_value="🧠 [Grokipedia] Test Article | https://grokipedia.com/page/Test") as mock_grok:
            plugin.on_privmsg("#channel", "alice", "!grok test")
            mock_grok.assert_called_with("test")
            self.assertEqual(len(sent_messages), 1)
            self.assertIn("[Grokipedia]", sent_messages[0][1])

            plugin.on_privmsg("#channel", "alice", "!grokipedia test2")
            mock_grok.assert_called_with("test2")

            plugin.on_privmsg("#channel", "alice", "!grokpedia test3")
            mock_grok.assert_called_with("test3")

    def test_infobot_sopel_module_commands_and_script_adapter(self):
        """Verify infobot defines Sopel command functions discovered by ScriptAdapter and main.py."""
        import sideload.infobot as infobot_mod
        from adapters.script_adapter import ScriptAdapter

        # Check for Sopel commands on infobot functions
        found_cmds = []
        for attr_name in dir(infobot_mod):
            f = getattr(infobot_mod, attr_name)
            if callable(f) and hasattr(f, "_sopel_commands"):
                found_cmds.extend(f._sopel_commands)

        self.assertIn("grok", found_cmds, "infobot must expose 'grok' via @module.commands")
        self.assertIn("wiki", found_cmds, "infobot must expose 'wiki' via @module.commands")

        # Check ScriptAdapter registers these commands
        bot = MagicMock()
        bot.nick = "infobot"
        adapter = ScriptAdapter(infobot_mod, bot)
        cmds = adapter.get_commands()
        self.assertIn("grok", cmds)
        self.assertIn("wiki", cmds)


if __name__ == "__main__":
    unittest.main()

