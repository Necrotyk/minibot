# SPDX-License-Identifier: AGPL-3.0-or-later
"""
test_minibot_lite.py — Unit Tests for Minibot Lite & Sopel Compatibility.
Tests URL/media previewing (gzip/deflate, OpenGraph fallback, punctuation stripping),
Sopel shim (module/plugin imports, decorators, memory), and StandaloneRunner multi-channel CLI.
"""

import gzip
import io
import os
import unittest
import urllib.request
import zlib
from unittest.mock import MagicMock, patch

from adapters.script_adapter import ScriptAdapter
from adapters.sopel_shim import SopelBotWrapper, SopelTrigger, inject_sopel_shim
from run_bot import StandaloneRunner, parse_cli_args
from sideload.infobot import (
    BARE_SEARCH_DOMAINS,
    get_web_title,
    strip_url_punctuation,
    Plugin as InfobotPlugin
)


class TestURLPreviewHarden(unittest.TestCase):
    def test_strip_url_punctuation(self):
        # Balanced parentheses preserved (Wikipedia syntax)
        self.assertEqual(
            strip_url_punctuation("https://en.wikipedia.org/wiki/Python_(programming_language)"),
            "https://en.wikipedia.org/wiki/Python_(programming_language)"
        )
        # Unbalanced trailing brackets/punctuation stripped
        self.assertEqual(
            strip_url_punctuation("https://example.com/test)"),
            "https://example.com/test"
        )
        self.assertEqual(
            strip_url_punctuation("https://example.com/test."),
            "https://example.com/test"
        )
        self.assertEqual(
            strip_url_punctuation("https://example.com/test,"),
            "https://example.com/test"
        )
        self.assertEqual(
            strip_url_punctuation("https://example.com/test>"),
            "https://example.com/test"
        )
        self.assertEqual(
            strip_url_punctuation("https://example.com/test'"),
            "https://example.com/test"
        )

    @patch("sideload.infobot.is_safe_url")
    @patch("sideload.infobot.build_safe_opener")
    def test_gzip_decompression_and_no_host_header(self, mock_build_opener, mock_is_safe):
        mock_is_safe.return_value = (True, "93.184.216.34")

        html_body = "<html><head><title>Gzip Compressed Title</title></head><body>Hello</body></html>"
        compressed = gzip.compress(html_body.encode("utf-8"))

        mock_resp = MagicMock()
        mock_resp.headers = {
            "Content-Type": "text/html; charset=utf-8",
            "Content-Encoding": "gzip"
        }
        mock_resp.read.return_value = compressed
        mock_opener = MagicMock()
        mock_opener.open.return_value.__enter__.return_value = mock_resp
        mock_build_opener.return_value = mock_opener

        title = get_web_title("https://example.com/")
        self.assertEqual(title, "Gzip Compressed Title")

        # Verify no manual "Host" header was passed to Request (avoids 301/302 redirect 400 bug)
        req_obj = mock_opener.open.call_args[0][0]
        self.assertNotIn("Host", req_obj.headers)
        self.assertIn("Accept-encoding", [h.capitalize() for h in req_obj.headers.keys()])

    @patch("sideload.infobot.is_safe_url")
    @patch("sideload.infobot.build_safe_opener")
    def test_opengraph_fallback_on_generic_title(self, mock_build_opener, mock_is_safe):
        mock_is_safe.return_value = (True, "93.184.216.34")

        # Page with Cloudflare generic title and rich OpenGraph og:title
        html_body = (
            '<html><head><title>Just a moment...</title>'
            '<meta property="og:title" content="Real Article Title on Protected Site">'
            '</head><body>Verifying...</body></html>'
        )

        mock_resp = MagicMock()
        mock_resp.headers = {"Content-Type": "text/html"}
        mock_resp.read.return_value = html_body.encode("utf-8")
        mock_opener = MagicMock()
        mock_opener.open.return_value.__enter__.return_value = mock_resp
        mock_build_opener.return_value = mock_opener

        title = get_web_title("https://news.ycombinator.com/item?id=12345")
        self.assertEqual(title, "Real Article Title on Protected Site")

    @patch("sideload.infobot.is_safe_url")
    @patch("sideload.infobot.build_safe_opener")
    def test_non_html_media_file_preview(self, mock_build_opener, mock_is_safe):
        mock_is_safe.return_value = (True, "93.184.216.34")

        mock_resp = MagicMock()
        mock_resp.headers = {
            "Content-Type": "application/pdf",
            "Content-Length": "2621440"  # 2.5 MB
        }
        mock_opener = MagicMock()
        mock_opener.open.return_value.__enter__.return_value = mock_resp
        mock_build_opener.return_value = mock_opener

        title = get_web_title("https://example.com/document.pdf")
        self.assertEqual(title, "[File] application/pdf (2.5 MB)")

    @patch("sideload.infobot.get_web_title")
    def test_multi_url_and_root_domain_handling(self, mock_get_title):
        mock_get_title.side_effect = lambda u: f"Title for {u}"

        mock_bot = MagicMock()
        plugin = InfobotPlugin(mock_bot)

        # Message with 2 URLs (one root domain, one subpath) and 1 trailing paren
        msg = "Check (https://kernel.org/) and https://example.com/foo."
        plugin.on_privmsg("#lobby", "alice", msg)

        # Both URLs should be previewed (up to 2)
        self.assertEqual(mock_bot.send_msg.call_count, 2)
        calls = [c[0][1] for c in mock_bot.send_msg.call_args_list]
        self.assertTrue(any("https://kernel.org/" in c for c in calls))
        self.assertTrue(any("https://example.com/foo" in c for c in calls))

        # Bare search engine root domains must be skipped
        mock_bot.send_msg.reset_mock()
        mock_get_title.reset_mock()
        plugin.on_privmsg("#lobby", "bob", "Go to https://www.google.com/ or https://youtube.com")
        mock_get_title.assert_not_called()
        mock_bot.send_msg.assert_not_called()


class TestSopelShimModernCompatibility(unittest.TestCase):
    def setUp(self):
        inject_sopel_shim()

    def test_sopel_plugin_and_module_imports(self):
        import sopel
        import sopel.module
        import sopel.plugin
        from sopel import module, plugin
        from sopel.tools import SopelMemory

        self.assertIs(sopel.plugin, sopel.module)
        self.assertIs(plugin, module)
        self.assertEqual(SopelMemory, dict)

    def test_sopel_decorators_and_execution(self):
        from sopel import plugin

        @plugin.command("greet")
        @plugin.require_chanmsg
        def greet_cmd(bot, trigger):
            bot.reply(f"Hello, {trigger.nick}! args={trigger.args}")

        @plugin.command("adminonly")
        @plugin.require_admin
        def admin_cmd(bot, trigger):
            bot.say("Secret admin data")

        # Mock module container
        class MockModule:
            __name__ = "test_sopel_mod"

        mod = MockModule()
        mod.greet_cmd = greet_cmd
        mod.admin_cmd = admin_cmd

        mock_minibot = MagicMock()
        mock_minibot.nick = "minibot"
        mock_minibot.creator_nick = "superadmin"
        mock_minibot.config = {"ADMIN_NICKS": ["superadmin"]}
        mock_minibot._sopel_memory = {}

        adapter = ScriptAdapter(mod, mock_minibot)

        # 1. Normal user in channel
        adapter.on_privmsg("#chat", "alice", "!greet one two")
        mock_minibot.send_msg.assert_called_with("#chat", "alice: Hello, alice! args=['one', 'two']")

        # 2. Normal user in private message -> blocked by require_chanmsg
        mock_minibot.send_msg.reset_mock()
        adapter.on_privmsg("minibot", "alice", "!greet one")
        mock_minibot.send_msg.assert_not_called()

        # 3. Normal user calling admin command -> blocked
        mock_minibot.send_msg.reset_mock()
        adapter.on_privmsg("#chat", "alice", "!adminonly")
        mock_minibot.send_msg.assert_called_with("#chat", "alice: Admin privileges required.")

        # 4. Admin calling admin command -> allowed
        mock_minibot.send_msg.reset_mock()
        adapter.on_privmsg("#chat", "superadmin", "!adminonly")
        mock_minibot.send_msg.assert_called_with("#chat", "Secret admin data")

    def test_sopel_bot_wrapper_memory_and_actions(self):
        mock_bot = MagicMock()
        mock_bot._sopel_memory = {}
        wrapper = SopelBotWrapper(mock_bot, "#chan", "alice")

        wrapper.memory["counter"] = 42
        self.assertEqual(mock_bot._sopel_memory["counter"], 42)

        wrapper.action("dances")
        mock_bot.raw_send.assert_called_with("PRIVMSG #chan :\x01ACTION dances\x01")

        wrapper.notice("secret notice")
        mock_bot.raw_send.assert_called_with("NOTICE alice :secret notice")


class TestStandaloneRunnerMultiChannel(unittest.TestCase):
    def test_cli_parsing_options(self):
        parser, nick, net, chan, mods = parse_cli_args([
            "--nick", "litebot",
            "--network", "libera",
            "--channels", "#c1,#c2,#c3",
            "--host", "127.0.0.1",
            "--port", "6697",
            "--ssl",
            "--insecure"
        ])

        self.assertEqual(nick, "litebot")
        self.assertEqual(net, "libera")
        self.assertEqual(chan, "#c1,#c2,#c3")
        args = parser.parsed_args
        self.assertEqual(args.host, "127.0.0.1")
        self.assertEqual(args.port, 6697)
        self.assertTrue(args.ssl)
        self.assertTrue(args.insecure)

    def test_runner_multi_channel_normalization(self):
        runner = StandaloneRunner(
            nick="litebot",
            network="local",
            channels="c1, #c2 , #c3",
            modules=["sideload_infobot"],
            host="127.0.0.1",
            port=6667,
            use_ssl=False
        )

        self.assertEqual(runner.channels, ["#c1", "#c2", "#c3"])
        self.assertEqual(runner.channel, "#c1")
        self.assertIn("#c1", runner.bot.channels)
        self.assertIn("#c2", runner.bot.channels)
        self.assertIn("#c3", runner.bot.channels)


if __name__ == "__main__":
    unittest.main()
