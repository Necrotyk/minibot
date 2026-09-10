# SPDX-License-Identifier: AGPL-3.0-or-later
"""
test_adapters.py — Unit Tests for Sopel & Limnoria Shims and Script Adapters.
"""

import sys
import unittest
from unittest.mock import MagicMock

from minibots.adapters.limnoria_shim import LimnoriaIrcWrapper, inject_limnoria_shim
from minibots.adapters.script_adapter import ScriptAdapter
from minibots.adapters.sopel_shim import SopelBotWrapper, SopelTrigger, inject_sopel_shim


class TestAdapters(unittest.TestCase):
    def setUp(self):
        inject_sopel_shim()
        inject_limnoria_shim()
        self.mock_bot = MagicMock()
        self.mock_bot.nick = "shim_bot"

    def test_sopel_shim_injection(self):
        self.assertIn("sopel", sys.modules)
        self.assertIn("sopel.module", sys.modules)

        import sopel.module as sopel_mod

        @sopel_mod.commands("hello", "hi")
        def sample_cmd(bot, trigger):
            pass

        self.assertTrue(hasattr(sample_cmd, "_sopel_commands"))
        self.assertEqual(sample_cmd._sopel_commands, ["hello", "hi"])

    def test_sopel_wrapper_say_reply(self):
        wrapper = SopelBotWrapper(self.mock_bot, "#channel", "alice")
        wrapper.say("hello world")
        self.mock_bot.send_msg.assert_called_with("#channel", "hello world")

        wrapper.reply("welcome")
        self.mock_bot.send_msg.assert_called_with("#channel", "alice: welcome")

    def test_limnoria_wrapper_reply(self):
        wrapper = LimnoriaIrcWrapper(self.mock_bot, "#channel", "bob")
        wrapper.reply("greetings", prefixNick=True)
        self.mock_bot.send_msg.assert_called_with("#channel", "bob: greetings")

    def test_script_adapter(self):
        mock_module = MagicMock()
        adapter = ScriptAdapter(mock_module, self.mock_bot)
        mock_module.setup.assert_called_once_with(self.mock_bot)

        adapter.on_privmsg("#chan", "alice", "ping")
        mock_module.handle_message.assert_called_once_with(self.mock_bot, "#chan", "alice", "ping")


    def test_hot_reload_active_bot(self):
        import tempfile
        import os
        import time
        from minibots.manager import MinibotManager

        with tempfile.TemporaryDirectory() as tmpdir:
            sideload_dir = os.path.join(tmpdir, "sideload")
            os.makedirs(sideload_dir, exist_ok=True)
            db_path = os.path.join(tmpdir, "test.db")

            script_file = os.path.join(sideload_dir, "dynbot.py")
            with open(script_file, "w") as f:
                f.write("""
from sopel import module

@module.commands("version")
def version_cmd(bot, trigger):
    bot.say("Version 1")
""")

            manager = MinibotManager(config={}, db_path=db_path)
            manager.sideload_engine.sideload_dir = type(manager.sideload_engine.sideload_dir)(sideload_dir)

            sent_msgs = []
            def _raw_send(nick, line, net):
                sent_msgs.append(line)

            manager.raw_send_callback = _raw_send
            bot = manager.spawn_bot("hotbot", "wireguard", ["#test"], ["sideload:dynbot"], creator_nick="admin")

            # 1. Trigger version on bot
            bot.on_privmsg("#test", "alice", ".version")
            self.assertTrue(any("Version 1" in m for m in sent_msgs))

            # 2. Modify script to Version 2
            time.sleep(0.05)
            with open(script_file, "w") as f:
                f.write("""
from sopel import module

@module.commands("version")
def version_cmd(bot, trigger):
    bot.say("Version 2")
""")
            # Force mtime update forward
            os.utime(script_file, (time.time() + 10, time.time() + 10))

            # 3. Trigger reload
            reloaded = manager.reload_all_modules()
            self.assertIn("sideload_dynbot", reloaded)

            # 4. Trigger version on bot again
            sent_msgs.clear()
            bot.on_privmsg("#test", "alice", ".version")
            self.assertTrue(any("Version 2" in m for m in sent_msgs))
            self.assertFalse(any("Version 1" in m for m in sent_msgs))


if __name__ == "__main__":
    unittest.main()
