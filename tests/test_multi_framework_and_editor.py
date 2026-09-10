# SPDX-License-Identifier: AGPL-3.0-or-later
import asyncio
import os
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock

from adapters.discord_shim import Context, bot as discord_bot, inject_discord_shim
from adapters.errbot_shim import BotPlugin, arg_botcmd, botcmd, inject_errbot_shim
from adapters.ircbot_shim import Event, SingleServerIRCBot, inject_ircbot_shim
from core.command_editor import CommandEditor
from core.sideload import SideloadEngine
from minibots.adapters.script_adapter import ScriptAdapter
from plugins.toolbot import cipher_codec, compute_hash, explain_cron, validate_json_text


class TestMultiFrameworkAndEditor(unittest.TestCase):
    def setUp(self):
        inject_errbot_shim()
        inject_discord_shim()
        inject_ircbot_shim()
        self.tmp_dir = tempfile.mkdtemp()
        self.cmd_editor = CommandEditor(db_path=os.path.join(self.tmp_dir, "test_cmds.sqlite3"))
        self.mock_bot = MagicMock()
        self.mock_bot.nick = "testbot"

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_errbot_shim(self):
        class SampleErrbot(BotPlugin):
            @botcmd
            def ping(self, msg, args):
                return "pong"

        plugin = SampleErrbot(bot=self.mock_bot)
        self.assertTrue(hasattr(SampleErrbot.ping, "_errbot_commands"))
        self.assertEqual(SampleErrbot.ping._errbot_commands[0]["name"], "ping")
        self.assertEqual(plugin.ping("!ping", ""), "pong")

    def test_discord_shim(self):
        @discord_bot.command(name="echo")
        async def sample_echo(ctx, *args):
            await ctx.reply(" ".join(args))

        self.assertTrue(hasattr(sample_echo, "_discord_commands"))
        self.assertIn("echo", sample_echo._discord_commands)

        ctx = Context(self.mock_bot, "#channel", "alice", "!echo hello world")
        asyncio.run(sample_echo(ctx, "hello", "world"))
        self.mock_bot.send_msg.assert_called_with("#channel", "alice: hello world")

    def test_ircbot_shim(self):
        class SampleIRCBot(SingleServerIRCBot):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self.received = None

            def on_pubmsg(self, connection, event):
                self.received = event.arguments[0]

        bot_inst = SampleIRCBot()
        bot_inst.on_pubmsg(bot_inst.connection, Event("pubmsg", "bob", "#chan", ["hello"]))
        self.assertEqual(bot_inst.received, "hello")

    def test_command_editor_template(self):
        self.cmd_editor.add_template("greet", "Hello {0}, welcome to {channel}! - {nick}", author="admin")
        res = self.cmd_editor.execute_command("greet", ["alice"], sender="admin", target="#dev")
        self.assertEqual(res, "Hello alice, welcome to #dev! - admin")

        info = self.cmd_editor.get_command_info("greet")
        self.assertEqual(info["call_count"], 1)

    def test_command_editor_python_and_ast_guard(self):
        # 1. Valid python snippet
        py_code = "return f'Sum is {int(args[0]) + int(args[1])}'"
        ok, msg = self.cmd_editor.add_python("add", py_code, author="admin")
        self.assertTrue(ok)
        res = self.cmd_editor.execute_command("add", ["10", "25"])
        self.assertEqual(res, "Sum is 35")

        # 2. Blocked forbidden import
        unsafe_code = "import subprocess\nsubprocess.run(['ls'])"
        ok, msg = self.cmd_editor.add_python("bad", unsafe_code)
        self.assertFalse(ok)
        self.assertIn("Forbidden import", msg)

    def test_command_editor_multiline_session(self):
        self.cmd_editor.begin_session("alice", "multiline_test")
        self.cmd_editor.append_session_line("alice", "x = 5")
        self.cmd_editor.append_session_line("alice", "return x * 2")
        ok, msg = self.cmd_editor.save_session("alice")
        self.assertTrue(ok)

        res = self.cmd_editor.execute_command("multiline_test", [])
        self.assertEqual(res, "10")

    def test_toolbot_expanded_tools(self):
        h = compute_hash("md5", "hello")
        self.assertIn("5d41402abc4b2a76b9719d911017c592", h)

        j = validate_json_text('{"key": "value"}')
        self.assertIn("Valid JSON", j)

        c = explain_cron("*/5 * * * *")
        self.assertIn("Minute: */5", c)

        rot = cipher_codec("rot13", "enc", "hello")
        self.assertEqual(rot, "uryyb")

        b64 = cipher_codec("b64", "enc", "test")
        self.assertEqual(b64, "dGVzdA==")
        dec = cipher_codec("b64", "dec", "dGVzdA==")
        self.assertEqual(dec, "test")

    def test_sideload_scaffold(self):
        engine = SideloadEngine(sideload_dir=os.path.join(self.tmp_dir, "sideload"))
        ok, msg = engine.scaffold_tool("errbot", "scaffold_err")
        self.assertTrue(ok)
        self.assertTrue(os.path.exists(os.path.join(self.tmp_dir, "sideload", "scaffold_err.py")))

        ok2, msg2 = engine.scaffold_tool("discord", "scaffold_disc")
        self.assertTrue(ok2)
        self.assertTrue(os.path.exists(os.path.join(self.tmp_dir, "sideload", "scaffold_disc.py")))


if __name__ == "__main__":
    unittest.main()
