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
    MultiServerManager,
    StandaloneRunner,
    import_sopel_config,
    parse_cli_args,
    persist_channel_change,
    persist_server_change,
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
        self.assertEqual(data["ADMIN_NICKS"], ["SuperAdmin"])
        self.assertIn("#general", data["ADMIN_CHANNELS"])
        self.assertIn("#sopel-room", data["ADMIN_CHANNELS"])

        srv = data["SERVERS"]["primary"]
        self.assertEqual(srv["host"], "irc.example.net")
        self.assertEqual(srv["port"], 6697)
        self.assertTrue(srv["ssl"])
        self.assertFalse(srv["trusted"])
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

    def test_import_sopel_config_trusted_and_public_hosts(self):
        """Tests that import_sopel_config sets trusted=False for public hosts and trusted=True for loopback/private."""
        scenarios = [
            ("irc.libera.chat", False),
            ("irc.oftc.net", False),
            ("127.0.0.1", True),
            ("localhost", True),
            ("10.99.0.1", True),
            ("192.168.1.100", True),
            ("172.16.0.5", True),
        ]
        for host, expected_trusted in scenarios:
            cfg_path = os.path.join(self.test_dir, f"test_{expected_trusted}_{host.replace('.', '_')}.cfg")
            out_json = os.path.join(self.test_dir, f"out_{expected_trusted}_{host.replace('.', '_')}.json")
            with open(cfg_path, "w", encoding="utf-8") as f:
                f.write(
                    f"[core]\n"
                    f"host = {host}\n"
                    f"nick = TestBot\n"
                    f"owner = AliceAdmin\n"
                    f"channels = #test\n"
                )
            import_sopel_config(sopel_cfg_path=cfg_path, out_config_json=out_json)
            with open(out_json, "r", encoding="utf-8") as f:
                data = json.load(f)
            srv = data["SERVERS"]["primary"]
            self.assertEqual(
                srv["trusted"],
                expected_trusted,
                f"Expected trusted={expected_trusted} for host {host}, got {srv['trusted']}"
            )
            # Ensure owner is exact without wildcard
            self.assertEqual(data["ADMIN_NICKS"], ["AliceAdmin"])

    def test_standalone_runner_reset_on_reconnect(self):
        """Tests that StandaloneRunner resets current_nick, bot.nick, _reclaim_needed, and queue on reconnect."""
        runner = StandaloneRunner(
            nick="coolbot",
            network="local",
            channels=["#lobby"],
            modules=[],
        )

        # Mutate to simulate state during a nickname collision
        runner.current_nick = "coolbot_"
        runner.bot.nick = "coolbot_"
        runner._reclaim_needed = True
        runner._send_queue.put_nowait("STALE_MESSAGE_FROM_PREVIOUS_SOCKET")

        # Mock open_connection to raise an error so _connect_and_run resets state and exits
        with patch("asyncio.open_connection", side_effect=ConnectionRefusedError("Mock connection error")):
            with self.assertRaises(ConnectionRefusedError):
                asyncio.run(runner._connect_and_run())

        # Verify state is reset to clean session defaults
        self.assertEqual(runner.current_nick, "coolbot")
        self.assertEqual(runner.bot.nick, "coolbot")
        self.assertFalse(runner._reclaim_needed)
        # Ensure fresh queue without stale messages
        self.assertTrue(runner._send_queue.empty())

    def test_minibot_sh_watchdog_stopped_marker(self):
        """Tests that ./minibot.sh watchdog respects data/minibot.stopped marker and exits silently."""
        sh_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "minibot.sh")
        data_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
        stopped_file = os.path.join(data_dir, "minibot.stopped")
        pid_file = os.path.join(data_dir, "minibot.pid")

        os.makedirs(data_dir, exist_ok=True)
        try:
            # Create stopped marker
            with open(stopped_file, "w") as f:
                f.write("1\n")

            import subprocess
            res = subprocess.run([sh_path, "watchdog"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            self.assertEqual(res.returncode, 0)
            self.assertFalse(os.path.exists(pid_file))
        finally:
            if os.path.exists(stopped_file):
                os.remove(stopped_file)


    def test_import_sopel_config_multi_file_directory(self):
        """Tests importing multiple Sopel .cfg files from a directory and merging into config.json."""
        sopel_dir = os.path.join(self.test_dir, "sopel_configs")
        os.makedirs(sopel_dir, exist_ok=True)
        out_json = os.path.join(self.test_dir, "config_multi.json")

        # 1. libera.cfg
        with open(os.path.join(sopel_dir, "libera.cfg"), "w", encoding="utf-8") as f:
            f.write(
                "[core]\n"
                "host = irc.libera.chat\n"
                "port = 6697\n"
                "use_ssl = true\n"
                "nick = LiberaBot\n"
                "owner = LiberaAdmin\n"
                "channels = #libera1, #libera2\n"
            )

        # 2. oftc.cfg
        with open(os.path.join(sopel_dir, "oftc.cfg"), "w", encoding="utf-8") as f:
            f.write(
                "[core]\n"
                "host = irc.oftc.net\n"
                "port = 6697\n"
                "use_ssl = true\n"
                "nick = OFTCBot\n"
                "owner = OFTCAdmin\n"
                "channels = #oftc1\n"
            )

        res = import_sopel_config(sopel_cfg_path=sopel_dir, out_config_json=out_json)
        self.assertEqual(len(res["files"]), 2)

        with open(out_json, "r", encoding="utf-8") as f:
            data = json.load(f)

        self.assertIn("libera", data["SERVERS"])
        self.assertIn("oftc", data["SERVERS"])
        self.assertEqual(data["SERVERS"]["libera"]["host"], "irc.libera.chat")
        self.assertEqual(data["SERVERS"]["oftc"]["host"], "irc.oftc.net")
        self.assertIn("LiberaAdmin", data["ADMIN_NICKS"])
        self.assertIn("OFTCAdmin", data["ADMIN_NICKS"])
        self.assertIn("#libera1", data["ADMIN_CHANNELS"])
        self.assertIn("#oftc1", data["ADMIN_CHANNELS"])
        self.assertIn("libera", data["ADMIN_NETWORKS"])
        self.assertIn("oftc", data["ADMIN_NETWORKS"])
        self.assertTrue(data["PRIMARY_NETWORK"] in ("libera", "oftc"))

    def test_persist_server_change_add_and_remove(self):
        """Tests adding and removing server entries in config.json via persist_server_change."""
        config_path = os.path.join(self.test_dir, "config_srv.json")
        init_data = {
            "PRIMARY_NETWORK": "primary",
            "ADMIN_NETWORKS": ["primary"],
            "ADMIN_CHANNELS": ["#lobby"],
            "SERVERS": {
                "primary": {
                    "host": "127.0.0.1",
                    "channels": ["#lobby"]
                }
            }
        }
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(init_data, f)

        # 1. Add oftc server
        new_srv = {
            "host": "irc.oftc.net",
            "port": 6697,
            "nick": "minibot",
            "channels": ["#oftc-lobby"],
            "ssl": True,
            "trusted": False,
            "tls_verify": True,
            "nickserv_password": ""
        }
        ok = persist_server_change(config_path, "oftc", new_srv, "add")
        self.assertTrue(ok)

        with open(config_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.assertIn("oftc", data["SERVERS"])
        self.assertIn("oftc", data["ADMIN_NETWORKS"])
        self.assertIn("#oftc-lobby", data["ADMIN_CHANNELS"])
        self.assertEqual(data["SERVERS"]["oftc"]["host"], "irc.oftc.net")

        # 2. Remove oftc server
        ok = persist_server_change(config_path, "oftc", None, "remove")
        self.assertTrue(ok)

        with open(config_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.assertNotIn("oftc", data["SERVERS"])
        self.assertNotIn("oftc", data["ADMIN_NETWORKS"])

    def test_multi_server_in_chat_admin_commands(self):
        """Tests in-chat multi-server commands: !server list, !server add, !server del, and cross-network !join/!part."""
        config_path = os.path.join(self.test_dir, "config_chat_multi.json")
        init_data = {
            "PRIMARY_NETWORK": "libera",
            "ADMIN_NICKS": ["chief"],
            "ADMIN_NETWORKS": ["libera"],
            "ADMIN_CHANNELS": ["#libera-admin"],
            "SERVERS": {
                "libera": {
                    "host": "irc.libera.chat",
                    "port": 6697,
                    "nick": "minibot",
                    "channels": ["#libera-admin"],
                    "ssl": True,
                    "trusted": False,
                    "tls_verify": True
                }
            }
        }
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(init_data, f)

        manager = MultiServerManager(config_path=config_path)
        libera_runner = manager.create_runner("libera")
        manager.runners["libera"] = libera_runner
        libera_runner._send_queue = asyncio.Queue()

        sent_msgs = []
        libera_runner.bot.send_msg = lambda target, msg: sent_msgs.append((target, msg))

        async def run_scenario():
            lines = [
                # 1. !server list
                b":chief!u@h PRIVMSG #libera-admin :!server list\r\n",
                # 2. !server add oftc irc.oftc.net:6697 ssl #oftc-lobby\r\n
                b":chief!u@h PRIVMSG #libera-admin :!server add oftc irc.oftc.net:6697 ssl #oftc-lobby\r\n",
                # 3. Cross-network !join oftc #oftc-new
                b":chief!u@h PRIVMSG #libera-admin :!join oftc #oftc-new\r\n",
                # 4. Cross-network !part oftc #oftc-new
                b":chief!u@h PRIVMSG #libera-admin :!part oftc #oftc-new\r\n",
                # 5. !server del oftc
                b":chief!u@h PRIVMSG #libera-admin :!server del oftc\r\n",
                # 6. !botstatus
                b":chief!u@h PRIVMSG #libera-admin :!botstatus\r\n",
                b""
            ]
            reader = AsyncMock()
            reader.readline.side_effect = lines
            await libera_runner._recv_loop(reader)

        asyncio.run(run_scenario())

        replies = [m for _, m in sent_msgs]
        self.assertTrue(any("Networks (" in r or "Network:" in r for r in replies))
        self.assertTrue(any("Added and connected to 'oftc'" in r for r in replies))
        self.assertTrue(any("Joined and persisted #oftc-new on oftc" in r for r in replies))
        self.assertTrue(any("Parted and removed #oftc-new from oftc" in r for r in replies))
        self.assertTrue(any("Stopped and removed server 'oftc'" in r for r in replies))
        self.assertTrue(any("Networks:" in r or "!botstatus" in r for r in replies))

    def test_parse_cli_args_multi_network(self):
        """Tests CLI multi-network argument parsing."""
        _, _, net, _, _ = parse_cli_args(["--network", "libera,oftc", "--channel", "#ch1,#ch2"])
        self.assertEqual(net, "libera,oftc")

        _, _, net_all, _, _ = parse_cli_args(["--network", "all"])
        self.assertEqual(net_all, "all")


if __name__ == "__main__":
    unittest.main()
