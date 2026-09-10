# SPDX-License-Identifier: AGPL-3.0-or-later
"""
test_minibots.py — Comprehensive Unit Tests for Minibots Engine, Commands, Logging & Multi-Socket Routing.
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from core.dispatcher import CommandDispatcher
from core.guard import is_safe_url
from core.logger import ActivityBuffer
from core.telemetry import TelemetryEngine
from minibots.base import MiniBot
from minibots.manager import MinibotManager
from plugins.toolbot import check_port, eval_calc_expression, resolve_dns
from plugins.trollbot import PERSONA_PRESETS, TrollbotPlugin


class TestMiniBotBase(unittest.TestCase):
    def setUp(self):
        self.sent_lines = []

        def mock_send(nick, line):
            self.sent_lines.append((nick, line))

        self.mock_send = mock_send
        self.bot = MiniBot(
            nick="testbot",
            network="wireguard",
            channels=["#test"],
            creator_nick="admin",
            raw_send_callback=self.mock_send
        )

    def test_send_msg_line_capping(self):
        long_msg = "A" * 500
        self.bot.send_msg("#test", long_msg)
        self.assertEqual(len(self.sent_lines), 1)
        nick, raw = self.sent_lines[0]
        self.assertEqual(nick, "testbot")
        self.assertTrue(len(raw.split(" :", 1)[1]) <= 380)

    def test_join_and_part(self):
        self.bot.join("#newchan")
        self.assertIn("#newchan", self.bot.channels)
        self.assertEqual(self.sent_lines[-1][1], "JOIN #newchan")

        self.bot.part("#newchan")
        self.assertNotIn("#newchan", self.bot.channels)
        self.assertEqual(self.sent_lines[-1][1], "PART #newchan :Leaving")

    def test_creator_dm_handling(self):
        self.bot.on_privmsg("testbot", "admin", "join #chan2")
        self.assertIn("#chan2", self.bot.channels)
        self.assertTrue(any("JOIN #chan2" in line[1] for line in self.sent_lines))

    def test_module_enable_disable(self):
        mock_plugin = MagicMock()
        self.bot.enable_module("dummy", mock_plugin)
        self.assertIn("dummy", self.bot.active_modules)

        self.bot.on_privmsg("#test", "alice", "hello")
        mock_plugin.on_privmsg.assert_called_once_with("#test", "alice", "hello")

        self.bot.disable_module("dummy")
        self.assertNotIn("dummy", self.bot.active_modules)


class TestMinibotManager(unittest.TestCase):
    def setUp(self):
        self.temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.temp_db.close()
        self.events = []
        self.manager = MinibotManager(
            config={"LLM": {}, "JULES": {}},
            conn_event_callback=lambda evt: self.events.append(evt),
            db_path=self.temp_db.name
        )

    def tearDown(self):
        Path(self.temp_db.name).unlink(missing_ok=True)

    def test_spawn_and_kill_bot(self):
        bot = self.manager.spawn_bot(
            nick="helperbot",
            network="wireguard",
            channels=["#help"],
            module_names=["urls"],
            creator_nick="admin"
        )
        self.assertIn("helperbot", self.manager.active_bots)
        self.assertIn("urls", bot.active_modules)
        self.assertEqual(len(self.events), 1)
        self.assertEqual(self.events[0]["type"], "spawn_conn")

        # Dynamic attach and detach
        ok_att = self.manager.attach_module("helperbot", "toolbot")
        self.assertTrue(ok_att)
        self.assertIn("toolbot", bot.active_modules)

        ok_det = self.manager.detach_module("helperbot", "urls")
        self.assertTrue(ok_det)
        self.assertNotIn("urls", bot.active_modules)

        # Loop suppression check
        nicks = self.manager.get_registered_nicks()
        self.assertIn("helperbot", nicks)

        # Kill bot
        ok = self.manager.kill_bot("helperbot")
        self.assertTrue(ok)
        self.assertNotIn("helperbot", self.manager.active_bots)
        self.assertEqual(self.events[-1]["type"], "kill_conn")

    def test_rehydrate_bots(self):
        self.manager.spawn_bot("bot1", "wireguard", ["#c1"], ["urls"])
        self.manager.spawn_bot("bot2", "wireguard", ["#c2"], ["toolbot"])

        # Create new manager pointing to same SQLite DB
        new_mgr = MinibotManager(
            config={"LLM": {}, "JULES": {}},
            db_path=self.temp_db.name
        )
        restored = new_mgr.rehydrate_bots()
        self.assertIn("bot1", restored)
        self.assertIn("bot2", restored)
        self.assertIn("bot1", new_mgr.active_bots)
        self.assertIn("bot2", new_mgr.active_bots)


class TestDispatcherAndShorthands(unittest.TestCase):
    def setUp(self):
        self.disp = CommandDispatcher(admin_nicks=["admin*"])
        self.called = []
        self.disp.register("status", lambda net, tgt, snd, args: self.called.append("status"), min_ring=1, aliases=["st", "info"])
        self.disp.register("ping", lambda net, tgt, snd, args: self.called.append("ping"), min_ring=1, aliases=["p"])
        self.disp.register("spawn", lambda net, tgt, snd, args: self.called.append("spawn"), min_ring=0)

    def test_shorthand_parsing(self):
        parsed = self.disp.parse_line("minibots", "#chan", "user1", "!st")
        self.assertIsNotNone(parsed)
        cmd, args, meta = parsed
        self.assertEqual(cmd, "status")

        parsed_p = self.disp.parse_line("minibots", "#chan", "user1", "!p")
        self.assertIsNotNone(parsed_p)
        cmd_p, _, _ = parsed_p
        self.assertEqual(cmd_p, "ping")

    def test_flexible_mentions(self):
        for msg in ["minibots: !st", "minibots, st", "@minibots status", "!status"]:
            parsed = self.disp.parse_line("minibots", "#chan", "user1", msg)
            self.assertIsNotNone(parsed, f"Failed on: {msg}")
            cmd, _, _ = parsed
            self.assertEqual(cmd, "status")

    def test_pm_parsing(self):
        parsed = self.disp.parse_line("minibots", "minibots", "user1", "status")
        self.assertIsNotNone(parsed)
        cmd, _, _ = parsed
        self.assertEqual(cmd, "status")

    def test_help_summary(self):
        wg_help = self.disp.get_help_summary(is_admin=True, is_wireguard=True)
        self.assertTrue(len(wg_help) >= 4)
        self.assertTrue(any("Jules" in l or "AI" in l for l in wg_help))
        self.assertTrue(any("Kaggle" in l or "kaggle" in l for l in wg_help))
        self.assertTrue(any("Tools" in l or "calc" in l for l in wg_help))

        pub_help = self.disp.get_help_summary(is_admin=False, is_wireguard=False)
        self.assertEqual(len(pub_help), 1)
        self.assertIn("!jules", pub_help[0])
        self.assertIn("!kaggle", pub_help[0])

    def test_topic_help_routing(self):
        # 1. Jules topic
        jules_help = self.disp.get_topic_help("jules")
        self.assertTrue(len(jules_help) >= 3)
        self.assertIn("Jules Coding Agent", jules_help[0])
        self.assertIn("!jules init", jules_help[1])

        # 2. Kaggle topic
        kaggle_help = self.disp.get_topic_help("kaggle")
        self.assertTrue(len(kaggle_help) >= 3)
        self.assertIn("Kaggle GPU Burst Worker", kaggle_help[0])
        self.assertIn("qwen-2.5-coder", kaggle_help[2])

        # 3. Trollbot topic
        troll_help = self.disp.get_topic_help("troll")
        self.assertTrue(len(troll_help) >= 3)
        self.assertIn("Trollbot Persona Engine", troll_help[0])

        # 4. Tools topic
        tools_help = self.disp.get_topic_help("tools")
        self.assertTrue(len(tools_help) >= 3)
        self.assertIn("FastMCP Tools", tools_help[0])

        # 5. Sideload topic
        sideload_help = self.disp.get_topic_help("sideload")
        self.assertTrue(len(sideload_help) >= 3)
        self.assertIn("Universal Sideloading", sideload_help[0])

        # 6. Admin topic
        admin_help_user = self.disp.get_topic_help("admin", is_admin=False)
        self.assertIn("require authorized admin credentials", admin_help_user[0])

        admin_help_admin = self.disp.get_topic_help("admin", is_admin=True)
        self.assertIn("!join", admin_help_admin[1])

        # 7. Fallback unknown topic
        unknown_help = self.disp.get_topic_help("nonexistent_topic_12345")
        self.assertIn("Unknown help topic", unknown_help[0])

    def test_context_aware_dm_help(self):
        sent_lines = []
        bot = MiniBot(
            nick="julesbot",
            network="wireguard",
            channels=["#jules"],
            creator_nick="admin",
            raw_send_callback=lambda nick, line: sent_lines.append(line)
        )
        bot.enable_module("julesbot", MagicMock())
        bot.enable_module("toolbot", MagicMock())

        bot.on_privmsg("julesbot", "admin", "help")
        self.assertTrue(len(sent_lines) >= 3)
        self.assertTrue(len(sent_lines) <= 4)
        self.assertTrue(any("Jules:" in l for l in sent_lines))
        self.assertTrue(any("Tools:" in l for l in sent_lines))

    def test_channel_help_aggregates_modules(self):
        sent_lines = []
        bot = MiniBot(
            nick="toolz",
            network="libera",
            channels=["#chat"],
            creator_nick="admin",
            raw_send_callback=lambda nick, line: sent_lines.append(line)
        )
        bot.enable_module("web_search", MagicMock())
        bot.enable_module("weather_time", MagicMock())
        bot.enable_module("infobot", MagicMock())

        # 1. Bare !help in channel (must be max 4 lines)
        bot.on_privmsg("#chat", "admin", "!help")
        self.assertTrue(len(sent_lines) >= 3)
        self.assertTrue(len(sent_lines) <= 4)
        self.assertTrue(any("Web Search" in l or "!ddg" in l for l in sent_lines))
        self.assertTrue(any("Infobot" in l or "!wiki" in l for l in sent_lines))
        self.assertTrue(any("Weather" in l or "!w" in l for l in sent_lines))

        # 2. Topic-specific help: !help web_search (must be max 4 lines)
        sent_lines.clear()
        bot.on_privmsg("#chat", "admin", "!help web_search")
        self.assertTrue(len(sent_lines) >= 2)
        self.assertTrue(len(sent_lines) <= 4)
        self.assertTrue(any("!ddg <query>" in l for l in sent_lines))

        # 3. Multiple (5) modules flood guard check (must never exceed 4 lines)
        sent_lines.clear()
        bot.enable_module("crypto_calc", MagicMock())
        bot.enable_module("sysadmin_tools", MagicMock())
        bot.on_privmsg("#chat", "admin", "!help")
        self.assertTrue(len(sent_lines) <= 4)
        self.assertTrue(len(sent_lines) >= 2)

    def test_infobot_bot_and_root_domain_suppression(self):
        import sideload.infobot as infobot
        sent_lines = []
        bot = MiniBot(
            nick="toolz",
            network="libera",
            channels=["#chatterbox"],
            creator_nick="admin",
            raw_send_callback=lambda nick, line: sent_lines.append(line)
        )
        plugin = infobot.Plugin(bot)

        # 1. Vulnavia bot message containing www.youtube.com must be ignored
        plugin.on_privmsg("#chatterbox", "vulnavia", "[url] Before you continue to YouTube | www.youtube.com")
        self.assertEqual(len(sent_lines), 0)

        # 2. Other bot message with bot prefix must be ignored
        plugin.on_privmsg("#chatterbox", "some_bot", "Check this: https://example.com")
        self.assertEqual(len(sent_lines), 0)

        # 3. Message starting with bot prefix [url] from any nick must be ignored
        plugin.on_privmsg("#chatterbox", "user1", "[url] Some Site Title | https://example.com")
        self.assertEqual(len(sent_lines), 0)

        # 4. Bare root domain (e.g. www.youtube.com) from normal user must be ignored
        plugin.on_privmsg("#chatterbox", "admin", "www.youtube.com")
        self.assertEqual(len(sent_lines), 0)



class TestToolbotAndCalculations(unittest.TestCase):
    def test_calc_safe_evaluation(self):
        self.assertEqual(eval_calc_expression("10 + 20 * 2"), "10 + 20 * 2 = 50")
        self.assertEqual(eval_calc_expression("100 / 4 + 17"), "100 / 4 + 17 = 42.0")
        self.assertEqual(eval_calc_expression("2 ^ 8"), "2 ^ 8 = 256")
        self.assertIn("Invalid expression", eval_calc_expression("__import__('os').system('ls')"))
        self.assertIn("Invalid expression", eval_calc_expression("open('/etc/passwd')"))

    def test_dns_and_port(self):
        res = resolve_dns("127.0.0.1")
        self.assertIn("127.0.0.1", res)


class TestTrollbotPresets(unittest.TestCase):
    def test_persona_preset_switching(self):
        mock_bot = MagicMock()
        mock_bot.nick = "trollbot"
        troll = TrollbotPlugin(mock_bot)
        self.assertEqual(troll.current_preset, "default")

        self.assertTrue(troll.set_preset("hacker"))
        self.assertEqual(troll.current_preset, "hacker")
        self.assertIn("phrack", troll.system_prompt)

        self.assertFalse(troll.set_preset("non_existent_preset"))


class TestActivityLoggingAndTelemetry(unittest.TestCase):
    def test_activity_buffer(self):
        buf = ActivityBuffer(maxlen=5)
        for i in range(10):
            buf.record("wireguard", "#chan", "user", f"!cmd_{i}", "OK", duration_ms=1.5)
        tail = buf.tail(lines=3)
        self.assertEqual(len(tail), 3)
        self.assertIn("!cmd_9", tail[-1])

    def test_telemetry_pruning(self):
        temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        temp_db.close()
        telem = TelemetryEngine(db_path=temp_db.name)
        telem.record_usage("bot", "gpt-4", "openai", 10, 20, 150.0)
        summary = telem.get_summary(hours=1)
        self.assertEqual(summary["requests"], 1)
        deleted = telem.prune_older_than(days=30)
        self.assertEqual(deleted, 0)
        Path(temp_db.name).unlink(missing_ok=True)

    def test_ssrf_guard(self):
        safe, _ = is_safe_url("https://example.com")
        self.assertTrue(safe)
        unsafe_loopback, _ = is_safe_url("http://127.0.0.1:8000")
        self.assertFalse(unsafe_loopback)
        unsafe_wg, _ = is_safe_url("http://10.0.0.1:8050")
        self.assertFalse(unsafe_wg)


if __name__ == "__main__":
    unittest.main()
