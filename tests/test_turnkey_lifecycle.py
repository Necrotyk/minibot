# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

import asyncio
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from run_bot import (
    StandaloneRunner,
    import_sopel_config,
    persist_channel_change,
)


class TestTurnkeyLifecycle(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.test_dir, ignore_errors=True)

    def test_import_sopel_config_success(self):
        """Tests parsing a Sopel default.cfg and generating a valid Minibots config.json."""
        sopel_cfg_path = os.path.join(self.test_dir, "default.cfg")
        out_json_path = os.path.join(self.test_dir, "config.json")
        plugins_dir = os.path.join(self.test_dir, "plugins")
        sideload_dir = os.path.join(self.test_dir, "sideload")
        os.makedirs(plugins_dir, exist_ok=True)
        os.makedirs(sideload_dir, exist_ok=True)

        # Write dummy custom plugin
        custom_py = os.path.join(plugins_dir, "custom_sopel_test.py")
        with open(custom_py, "w", encoding="utf-8") as f:
            f.write("# dummy plugin\n")

        # Write sample Sopel cfg
        with open(sopel_cfg_path, "w", encoding="utf-8") as f:
            f.write(
                "[core]\n"
                "host = irc.example.net\n"
                "port = 6697\n"
                "use_ssl = true\n"
                "verify_ssl = false\n"
                "nick = TestSopelBot\n"
                "owner = SuperAdmin\n"
                "channels = #general, #sopel-room\n"
                "auth_password = SecretNickPass\n"
            )

        res = import_sopel_config(
            sopel_cfg_path=sopel_cfg_path,
            out_config_json=out_json_path,
            sideload_dir=sideload_dir,
        )

        self.assertTrue(os.path.isfile(out_json_path))
        with open(out_json_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        self.assertEqual(data["PRIMARY_NETWORK"], "primary")
        self.assertIn("SuperAdmin*", data["ADMIN_NICKS"])
        self.assertIn("#general", data["ADMIN_CHANNELS"])
        self.assertIn("#sopel-room", data["ADMIN_CHANNELS"])

        srv = data["SERVERS"]["primary"]
        self.assertEqual(srv["host"], "irc.example.net")
        self.assertEqual(srv["port"], 6697)
        self.assertTrue(srv["ssl"])
        self.assertFalse(srv["tls_verify"])
        self.assertEqual(srv["nick"], "TestSopelBot")
        self.assertEqual(srv["nickserv_password"], "SecretNickPass")

        # Verify plugin copied
        copied = os.path.join(sideload_dir, "custom_sopel_test.py")
        self.assertTrue(os.path.isfile(copied))
        self.assertIn("custom_sopel_test.py", res["copied_plugins"])

    def test_persist_channel_change_add_and_remove(self):
        """Tests adding and removing channels to/from config.json with persist_channel_change."""
        config_path = os.path.join(self.test_dir, "config.json")
        initial_data = {
            "PRIMARY_NETWORK": "primary",
            "ADMIN_CHANNELS": ["#start"],
            "SERVERS": {
                "primary": {
                    "host": "127.0.0.1",
                    "channels": ["#start"]
                }
            }
        }
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(initial_data, f)

        # 1. Add #newchan
        ok = persist_channel_change(config_path, "primary", "#newchan", "add")
        self.assertTrue(ok)
        with open(config_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.assertIn("#newchan", data["SERVERS"]["primary"]["channels"])
        self.assertIn("#newchan", data["ADMIN_CHANNELS"])

        # 2. Add channel without leading hash
        ok = persist_channel_change(config_path, "primary", "lobby", "add")
        self.assertTrue(ok)
        with open(config_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.assertIn("#lobby", data["SERVERS"]["primary"]["channels"])

        # 3. Remove #start
        ok = persist_channel_change(config_path, "primary", "#start", "remove")
        self.assertTrue(ok)
        with open(config_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.assertNotIn("#start", data["SERVERS"]["primary"]["channels"])
        self.assertNotIn("#start", data["ADMIN_CHANNELS"])

    def test_rotating_file_handler_properties(self):
        """Tests that RotatingFileHandler configures 5MB maxBytes and 2 backups."""
        log_path = os.path.join(self.test_dir, "minibot.log")
        handler = RotatingFileHandler(
            log_path,
            maxBytes=5 * 1024 * 1024,
            backupCount=2,
            encoding="utf-8"
        )
        self.assertEqual(handler.maxBytes, 5 * 1024 * 1024)
        self.assertEqual(handler.backupCount, 2)
        handler.close()

    def test_recv_loop_433_collision_and_ghost_reclaim(self):
        """Tests 433 nickname collision handling and subsequent NickServ GHOST reclamation."""
        runner = StandaloneRunner(
            nick="testbot",
            network="local",
            channels=["#lobby"],
            modules=[],
            nickserv_password="secretpassword",
        )
        runner._send_queue = asyncio.Queue()

        async def run_scenario():
            # Mock reader with 433 followed by 001
            lines = [
                b":server 433 * testbot :Nickname is already in use.\r\n",
                b":server 001 testbot_ :Welcome to the IRC Network testbot_!user@host\r\n",
                b":server NICK :testbot\r\n",
                b""
            ]
            reader = AsyncMock()
            reader.readline.side_effect = lines

            # Run recv loop until EOF
            await runner._recv_loop(reader)

        asyncio.run(run_scenario())

        # Check queue commands
        emitted = []
        while not runner._send_queue.empty():
            emitted.append(runner._send_queue.get_nowait())

        # Should have sent NICK testbot_ on 433
        self.assertIn("NICK testbot_", emitted)

        # Should have sent NickServ GHOST and reclaim on 001
        ghost_cmds = [c for c in emitted if "GHOST testbot secretpassword" in c]
        self.assertTrue(len(ghost_cmds) > 0)
        self.assertIn("NICK testbot", emitted)
        self.assertIn("PRIVMSG NickServ :IDENTIFY secretpassword", emitted)
        self.assertIn("JOIN #lobby", emitted)

    def test_admin_commands_join_part_reload_botstatus(self):
        """Tests in-chat admin commands (!join, !part, !reload, !botstatus)."""
        config_path = os.path.join(self.test_dir, "config.json")
        cfg_data = {
            "PRIMARY_NETWORK": "local",
            "ADMIN_NICKS": ["bossman*"],
            "ADMIN_CHANNELS": ["#lobby"],
            "SERVERS": {
                "local": {
                    "host": "127.0.0.1",
                    "channels": ["#lobby"]
                }
            }
        }
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(cfg_data, f)

        runner = StandaloneRunner(
            nick="minibot",
            network="local",
            channels=["#lobby"],
            modules=[],
            config_path=config_path,
        )
        runner._send_queue = asyncio.Queue()
        sent_messages = []
        runner.bot.send_msg = lambda target, msg: sent_messages.append((target, msg))

        async def run_scenario():
            lines = [
                # 1. Admin !join
                b":bossman!user@host PRIVMSG #lobby :!join #secret\r\n",
                # 2. Admin !part
                b":bossman!user@host PRIVMSG #lobby :!part #lobby\r\n",
                # 3. Admin !botstatus
                b":bossman!user@host PRIVMSG #lobby :!botstatus\r\n",
                # 4. Admin !reload
                b":bossman!user@host PRIVMSG #lobby :!reload\r\n",
                # 5. Non-admin !join (ignored by admin handler)
                b":randomuser!user@host PRIVMSG #lobby :!join #hacked\r\n",
                b""
            ]
            reader = AsyncMock()
            reader.readline.side_effect = lines
            await runner._recv_loop(reader)

        asyncio.run(run_scenario())

        # Verify #secret joined
        self.assertIn("#secret", runner.channels)
        self.assertNotIn("#lobby", runner.channels)

        # Check queue had JOIN #secret and PART #lobby
        emitted = []
        while not runner._send_queue.empty():
            emitted.append(runner._send_queue.get_nowait())
        self.assertIn("JOIN #secret", emitted)
        self.assertIn("PART #lobby :Parted by admin", emitted)
        self.assertNotIn("JOIN #hacked", emitted)

        # Check sent bot confirmations
        replies = [msg for _, msg in sent_messages]
        self.assertTrue(any("Joined and persisted #secret" in r for r in replies))
        self.assertTrue(any("Parted and removed #lobby" in r for r in replies))
        self.assertTrue(any("Uptime:" in r and "Python:" in r for r in replies))
        self.assertTrue(any("Hot-Reload" in r for r in replies))

    def test_minibot_sh_help_and_status(self):
        """Tests that ./minibot.sh help and status execute cleanly."""
        sh_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "minibot.sh")
        self.assertTrue(os.path.isfile(sh_path))
        self.assertTrue(os.access(sh_path, os.X_OK))

        import subprocess
        # Test help
        res = subprocess.run([sh_path, "help"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.assertEqual(res.returncode, 0)
        self.assertIn("Usage: ./minibot.sh", res.stdout)
        self.assertIn("watchdog", res.stdout)
        self.assertIn("enable", res.stdout)

        # Test status (when not running)
        res_stat = subprocess.run([sh_path, "status"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.assertEqual(res_stat.returncode, 0)
        self.assertIn("Status:", res_stat.stdout)


if __name__ == "__main__":
    unittest.main()
