# SPDX-License-Identifier: AGPL-3.0-or-later
"""
test_admin_public_channels.py — Unit Tests for Admin Command Restriction in Public Channels.
"""

import os
import unittest
from unittest.mock import MagicMock, patch

from core.config import ConfigManager
from core.guard import is_public_channel
from main import MinibotsLogicDaemon
from minibots.base import MiniBot


class TestAdminPublicChannels(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmpdir = tempfile.TemporaryDirectory()
        self.mock_cfg = MagicMock()
        self.mock_cfg.data = {
            "ADMIN_NICKS": ["admin"],
            "LLM": {},
            "JULES": {},
        }
        self.mock_cfg.admin_nicks = ["admin"]
        temp_db = os.path.join(self.tmpdir.name, "test_minibots.db")
        self.daemon = MinibotsLogicDaemon(self.mock_cfg, db_path=temp_db)
        self.sent_privmsgs = []
        self.daemon.send_privmsg = lambda net, tgt, msg: self.sent_privmsgs.append((net, tgt, msg))

    def tearDown(self):
        try:
            from sideload.dai import set_auto_switch
            set_auto_switch(False)
        except Exception:
            pass
        self.tmpdir.cleanup()

    def test_is_public_channel(self):
        # Direct messages are not public channels
        self.assertFalse(is_public_channel("wireguard", "admin"))
        self.assertFalse(is_public_channel("libera", "admin"))
        self.assertFalse(is_public_channel("libera", "dystopi-minibot"))
        self.assertFalse(is_public_channel("wireguard", ""))

        # Any channel on non-WireGuard networks is a public channel
        self.assertTrue(is_public_channel("libera", "#test-channel"))
        self.assertTrue(is_public_channel("libera", "#chat"))
        self.assertTrue(is_public_channel("libera", "#dystopi-public"))
        self.assertTrue(is_public_channel("oftc", "#test"))

        # On WireGuard, admin planes are private, other channels are public
        self.assertFalse(is_public_channel("wireguard", "#minibots"))
        self.assertFalse(is_public_channel("wireguard", "#minibot"))
        self.assertFalse(is_public_channel("wireguard", "#jules"))
        self.assertTrue(is_public_channel("wireguard", "#public"))
        self.assertTrue(is_public_channel("wireguard", "#random"))

    def test_top_level_admin_commands_blocked_in_public_channel(self):
        # 1. !spawn in #test-channel
        line = ":admin!user@host PRIVMSG #test-channel :!spawn evilbot #test-channel urls"
        self.daemon._handle_irc_event("libera", "dystopi-minibot", line)
        self.assertTrue(any("Admin commands cannot be used in public channels" in m[2] for m in self.sent_privmsgs))
        self.assertNotIn("evilbot", self.daemon.manager.active_bots)

        # 2. !kill in #dystopi-public
        self.sent_privmsgs.clear()
        line = ":admin!user@host PRIVMSG #dystopi-public :!kill somebot"
        self.daemon._handle_irc_event("libera", "dystopi-minibot", line)
        self.assertTrue(any("Admin commands cannot be used in public channels" in m[2] for m in self.sent_privmsgs))

        # 3. !craft in #test-channel
        self.sent_privmsgs.clear()
        line = ":admin!user@host PRIVMSG #test-channel :!craft mytool build something"
        self.daemon._handle_irc_event("libera", "dystopi-minibot", line)
        self.assertTrue(any("Admin commands cannot be used in public channels" in m[2] for m in self.sent_privmsgs))

        # 4. !join in #chat
        self.sent_privmsgs.clear()
        line = ":admin!user@host PRIVMSG #chat :!join #secret"
        self.daemon._handle_irc_event("libera", "dystopi-minibot", line)
        self.assertTrue(any("Admin commands cannot be used in public channels" in m[2] for m in self.sent_privmsgs))

        # 5. !raw in #chat
        self.sent_privmsgs.clear()
        line = ":admin!user@host PRIVMSG #chat :!raw QUIT :pwned"
        self.daemon._handle_irc_event("libera", "dystopi-minibot", line)
        self.assertTrue(any("Admin commands cannot be used in public channels" in m[2] for m in self.sent_privmsgs))

    def test_hub_subcommands_in_public_channel(self):
        # !hub install blocked in public channel
        self.sent_privmsgs.clear()
        line = ":admin!user@host PRIVMSG #test-channel :!hub install crypto_calc"
        self.daemon._handle_irc_event("libera", "dystopi-minibot", line)
        self.assertTrue(any("Admin command 'install' cannot be used in public channels" in m[2] for m in self.sent_privmsgs))

        # !hub pull blocked in public channel
        self.sent_privmsgs.clear()
        line = ":admin!user@host PRIVMSG #test-channel :!hub pull https://github.com/example/tool.py"
        self.daemon._handle_irc_event("libera", "dystopi-minibot", line)
        self.assertTrue(any("Admin command 'pull' cannot be used in public channels" in m[2] for m in self.sent_privmsgs))

        # Safe subcommands like !hub list and !hub search ARE allowed
        self.sent_privmsgs.clear()
        line = ":user1!user@host PRIVMSG #test-channel :!hub list"
        self.daemon._handle_irc_event("libera", "dystopi-minibot", line)
        self.assertTrue(any("Tool Hub Catalog" in m[2] for m in self.sent_privmsgs))

        self.sent_privmsgs.clear()
        line = ":user1!user@host PRIVMSG #test-channel :!hub search crypto"
        self.daemon._handle_irc_event("libera", "dystopi-minibot", line)
        self.assertTrue(any("Hub Search" in m[2] for m in self.sent_privmsgs))

    def test_tool_subcommands_in_public_channel(self):
        # !tool scaffold blocked in public channel
        self.sent_privmsgs.clear()
        line = ":admin!user@host PRIVMSG #test-channel :!tool scaffold sopel mybot"
        self.daemon._handle_irc_event("libera", "dystopi-minibot", line)
        self.assertTrue(any("Admin command 'scaffold' cannot be used in public channels" in m[2] for m in self.sent_privmsgs))

        # !tool import blocked in public channel
        self.sent_privmsgs.clear()
        line = ":admin!user@host PRIVMSG #test-channel :!tool import https://example.com/bot.py"
        self.daemon._handle_irc_event("libera", "dystopi-minibot", line)
        self.assertTrue(any("Admin command 'import' cannot be used in public channels" in m[2] for m in self.sent_privmsgs))

        # Safe subcommands like !tool calc and !tool dns ARE allowed in public channel
        self.sent_privmsgs.clear()
        line = ":user1!user@host PRIVMSG #test-channel :!calc 10 + 5"
        self.daemon._handle_irc_event("libera", "dystopi-minibot", line)
        self.assertTrue(any("15" in m[2] for m in self.sent_privmsgs))

    def test_sideload_subcommands_in_public_channel(self):
        # !sideload load blocked in public channel
        self.sent_privmsgs.clear()
        line = ":admin!user@host PRIVMSG #test-channel :!sideload load infobot"
        self.daemon._handle_irc_event("libera", "dystopi-minibot", line)
        self.assertTrue(any("Admin command 'load' cannot be used in public channels" in m[2] for m in self.sent_privmsgs))

        # !sideload list is ALLOWED in public channel
        self.sent_privmsgs.clear()
        line = ":user1!user@host PRIVMSG #test-channel :!sideload list"
        self.daemon._handle_irc_event("libera", "dystopi-minibot", line)
        self.assertTrue(any("Sideload Scripts" in m[2] for m in self.sent_privmsgs))

    def test_cmd_editor_in_public_channel(self):
        # !cmd add blocked in public channel
        self.sent_privmsgs.clear()
        line = ":admin!user@host PRIVMSG #test-channel :!cmd add test Hello"
        self.daemon._handle_irc_event("libera", "dystopi-minibot", line)
        self.assertTrue(any("Admin command 'add' cannot be used in public channels" in m[2] for m in self.sent_privmsgs))

        # !cmd py blocked in public channel
        self.sent_privmsgs.clear()
        line = ":admin!user@host PRIVMSG #test-channel :!cmd py test 1+1"
        self.daemon._handle_irc_event("libera", "dystopi-minibot", line)
        self.assertTrue(any("Admin command 'py' cannot be used in public channels" in m[2] for m in self.sent_privmsgs))

        # !cmd list is ALLOWED
        self.sent_privmsgs.clear()
        line = ":user1!user@host PRIVMSG #test-channel :!cmd list"
        self.daemon._handle_irc_event("libera", "dystopi-minibot", line)
        self.assertTrue(any("CMD-EDITOR" in m[2] or "CUSTOM COMMANDS" in m[2] for m in self.sent_privmsgs))

    def test_troll_persona_in_public_channel(self):
        # !troll set blocked in public channel
        self.sent_privmsgs.clear()
        line = ":admin!user@host PRIVMSG #test-channel :!troll set pirate"
        self.daemon._handle_irc_event("libera", "dystopi-minibot", line)
        self.assertTrue(any("Admin command 'set' cannot be used in public channels" in m[2] for m in self.sent_privmsgs))

        # !troll list is ALLOWED in public channel
        self.sent_privmsgs.clear()
        line = ":user1!user@host PRIVMSG #test-channel :!troll list"
        self.daemon._handle_irc_event("libera", "dystopi-minibot", line)
        self.assertTrue(any("Trollbot Presets" in m[2] for m in self.sent_privmsgs))

    def test_minibot_creator_commands_in_public_channel(self):
        sent = []
        bot = MiniBot(
            nick="testbot",
            network="libera",
            channels=["#test-channel"],
            creator_nick="admin",
            raw_send_callback=lambda n, l: sent.append(l)
        )

        # 1. Creator attempts 'join' in public channel
        bot.on_privmsg("#test-channel", "admin", "testbot: join #other")
        self.assertTrue(any("Admin commands cannot be used in public channels" in s for s in sent))
        self.assertNotIn("#other", bot.channels)

        # 2. Creator attempts 'say' in public channel
        sent.clear()
        bot.on_privmsg("#test-channel", "admin", "testbot: say #secret hello")
        self.assertTrue(any("Admin commands cannot be used in public channels" in s for s in sent))

        # 3. Creator attempts 'part' in public channel
        sent.clear()
        bot.on_privmsg("#test-channel", "admin", "testbot: part #test-channel")
        self.assertTrue(any("Admin commands cannot be used in public channels" in s for s in sent))
        self.assertIn("#test-channel", bot.channels)

        # 4. Creator uses 'join' in private DM -> ALLOWED
        sent.clear()
        bot.on_privmsg("testbot", "admin", "join #newchan")
        self.assertIn("#newchan", bot.channels)

        # 5. Normal user checks status in public channel -> ALLOWED
        sent.clear()
        bot.on_privmsg("#test-channel", "random_user", "testbot: status")
        self.assertTrue(any("Status: Online" in s for s in sent))

    def test_cmd_join_and_part_for_minibots(self):
        # Spawn a testbot in manager
        bot = self.daemon.manager.spawn_bot("workerbot", "libera", ["#chan1"], ["urls"], creator_nick="admin")
        self.assertIn("#chan1", bot.channels)

        # 1. Admin uses !join workerbot #chan2 in WireGuard admin channel
        self.sent_privmsgs.clear()
        line = ":admin!user@host PRIVMSG #minibots :!join workerbot #chan2"
        self.daemon._handle_irc_event("wireguard", "minibots", line)
        self.assertIn("#chan2", bot.channels)
        self.assertTrue(any("workerbot" in m[2] and "Joining channel #chan2" in m[2] for m in self.sent_privmsgs))

        # 2. Admin uses !part workerbot #chan1 in WireGuard admin channel
        self.sent_privmsgs.clear()
        line = ":admin!user@host PRIVMSG #minibots :!part workerbot #chan1"
        self.daemon._handle_irc_event("wireguard", "minibots", line)
        self.assertNotIn("#chan1", bot.channels)
        self.assertTrue(any("workerbot" in m[2] and "Parted channel #chan1" in m[2] for m in self.sent_privmsgs))

        # 3. Admin uses !minibot join workerbot #chan3
        self.sent_privmsgs.clear()
        line = ":admin!user@host PRIVMSG #minibots :!minibot join workerbot #chan3"
        self.daemon._handle_irc_event("wireguard", "minibots", line)
        self.assertIn("#chan3", bot.channels)
        self.assertTrue(any("workerbot" in m[2] and "Joining channel #chan3" in m[2] for m in self.sent_privmsgs))

        # 4. Admin uses !minibot part workerbot #chan3
        self.sent_privmsgs.clear()
        line = ":admin!user@host PRIVMSG #minibots :!minibot part workerbot #chan3"
        self.daemon._handle_irc_event("wireguard", "minibots", line)
        self.assertNotIn("#chan3", bot.channels)
        self.assertTrue(any("workerbot" in m[2] and "Parted channel #chan3" in m[2] for m in self.sent_privmsgs))

    def test_cmd_list_and_attach(self):
        # Spawn bots on two networks
        self.daemon.manager.spawn_bot("thebot", "libera", ["#chat"], ["urls"], creator_nick="admin")
        self.daemon.manager.spawn_bot("wgbot", "wireguard", ["#minibots"], ["urls"], creator_nick="admin")

        # 1. Unfiltered !list shows network names
        self.sent_privmsgs.clear()
        self.daemon._handle_irc_event("wireguard", "minibots", ":admin!user@host PRIVMSG #minibots :!list")
        self.assertTrue(any("thebot on libera" in m[2] and "wgbot on wireguard" in m[2] for m in self.sent_privmsgs))

        # 2. Filtered !list libera shows only libera
        self.sent_privmsgs.clear()
        self.daemon._handle_irc_event("wireguard", "minibots", ":admin!user@host PRIVMSG #minibots :!list libera")
        self.assertTrue(any("thebot on libera" in m[2] and "wgbot" not in m[2] for m in self.sent_privmsgs))

        # 3. Filtered !list nonexistent network
        self.sent_privmsgs.clear()
        self.daemon._handle_irc_event("wireguard", "minibots", ":admin!user@host PRIVMSG #minibots :!list nonet")
        self.assertTrue(any("No active minibots on network 'nonet'" in m[2] for m in self.sent_privmsgs))

        # 4. Flexible !spawn <nick> <network> <#chan> [mods]
        self.sent_privmsgs.clear()
        self.daemon._handle_irc_event("wireguard", "minibots", ":admin!user@host PRIVMSG #minibots :!spawn dynbot libera #test-channel")
        self.assertIn("dynbot", self.daemon.manager.active_bots)
        self.assertEqual(self.daemon.manager.active_bots["dynbot"].network, "libera")
        self.assertEqual(self.daemon.manager.active_bots["dynbot"].channels, ["#test-channel"])

        # 5. !attach to bot with unknown module reports available scripts
        self.sent_privmsgs.clear()
        self.daemon._handle_irc_event("wireguard", "minibots", ":admin!user@host PRIVMSG #minibots :!attach dynbot totally_bogus_mod")
        self.assertTrue(any("Unknown module or script not found" in m[2] for m in self.sent_privmsgs))

    def test_master_bot_model_and_ai_commands_in_minibot_channel(self):
        """Verifies master bot executes .models, .model, .ai, and delegations in #minibot and #minibots."""
        from unittest.mock import AsyncMock, patch

        # 1. Admin executing .models in #minibot on WireGuard
        self.sent_privmsgs.clear()
        with patch("sideload.dai.get_active_model_info", new=AsyncMock(return_value=("llama", "llama-3.2-1b"))):
            self.daemon._handle_irc_event("wireguard", "minibots", ":admin!user@host PRIVMSG #minibot :.models")
            self.assertTrue(any("[DAI·Models]" in m[2] for m in self.sent_privmsgs))
            self.assertTrue(any("llama-3.2-1b" in m[2] for m in self.sent_privmsgs))

        # 2. Admin toggling auto-switch in #minibot on WireGuard
        self.sent_privmsgs.clear()
        self.daemon._handle_irc_event("wireguard", "minibots", ":admin!user@host PRIVMSG #minibot :.model auto on")
        self.assertTrue(any("Auto-switch enabled" in m[2] for m in self.sent_privmsgs))
        self.sent_privmsgs.clear()
        self.daemon._handle_irc_event("wireguard", "minibots", ":admin!user@host PRIVMSG #minibot :.model auto off")
        self.assertTrue(any("Auto-switch disabled" in m[2] for m in self.sent_privmsgs))

        # 3. Admin switching model via .model llama in #minibot on WireGuard
        self.sent_privmsgs.clear()
        with patch("sideload.dai.get_active_model_info", new=AsyncMock(return_value=("lfm", "LFM2.5-1.2B-Thinking"))), \
             patch("sideload.dai.switch_pi_model", new=AsyncMock(return_value=(True, "Switched to llama-3.2-1b (llama)"))):
            self.daemon._handle_irc_event("wireguard", "minibots", ":admin!user@host PRIVMSG #minibot :.model llama")
            self.assertTrue(any("Switching Pi 5 worker to llama-3.2-1b" in m[2] for m in self.sent_privmsgs))
            self.assertTrue(any("Switched to llama-3.2-1b" in m[2] for m in self.sent_privmsgs))

        # 4. Non-admin attempting to switch model in #minibot
        self.sent_privmsgs.clear()
        self.daemon._handle_irc_event("wireguard", "minibots", ":bob!user@host PRIVMSG #minibot :.model llama")
        self.assertTrue(any("Permission denied" in m[2] for m in self.sent_privmsgs))

        # 5. Admin switching model via umbrella command !minibot model lfm
        self.sent_privmsgs.clear()
        with patch("sideload.dai.get_active_model_info", new=AsyncMock(return_value=("llama", "llama-3.2-1b"))), \
             patch("sideload.dai.switch_pi_model", new=AsyncMock(return_value=(True, "Switched to LFM2.5-1.2B-Thinking (lfm)"))):
            self.daemon._handle_irc_event("wireguard", "minibots", ":admin!user@host PRIVMSG #minibot :!minibot model lfm")
            self.assertTrue(any("Switched to LFM2.5-1.2B-Thinking" in m[2] for m in self.sent_privmsgs))

        # 6. User executing .ai pure model query in #minibot
        self.sent_privmsgs.clear()
        with patch("sideload.dai.run_pipeline", new=AsyncMock()) as mock_pipeline:
            self.daemon._handle_irc_event("wireguard", "minibots", ":admin!user@host PRIVMSG #minibot :.ai write a poem")
            mock_pipeline.assert_called_once()
            call_kwargs = mock_pipeline.call_args[1] if mock_pipeline.call_args[1] else {}
            call_args = mock_pipeline.call_args[0]
            # Verify target #minibot and query
            self.assertEqual(call_args[1], "#minibot")
            self.assertEqual(call_args[2], "admin")
            self.assertEqual(call_args[3], "write a poem")
            self.assertFalse(call_kwargs.get("enable_web_search", True))


if __name__ == "__main__":
    unittest.main()

