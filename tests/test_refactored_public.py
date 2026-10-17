# SPDX-License-Identifier: AGPL-3.0-or-later
"""
test_refactored_public.py — Unit tests for public release refactoring invariants.
Verifies config-driven topology, run_bot CLI parsing, IPC socket defaults,
and decoupled CTI/DPoP/DAI options.
"""

import os
import unittest
from unittest.mock import MagicMock, patch

from core.config import ConfigManager
from plugins.dpop_auth import DPoPAuthPlugin
from run_bot import parse_cli_args


class TestPublicReleaseRefactoring(unittest.TestCase):
    def test_config_manager_network_topology(self):
        cfg = ConfigManager()
        cfg.data = {
            "PRIMARY_NETWORK": "homelab",
            "ADMIN_NETWORKS": ["homelab", "mgmt"],
            "ADMIN_CHANNELS": ["#control", "#ops"],
            "SERVERS": {
                "homelab": {"trusted": True, "tls_verify": False},
                "public_irc": {"trusted": False, "tls_verify": True},
                "custom_sec": {"trusted": True},
            },
        }

        self.assertEqual(cfg.primary_network, "homelab")
        self.assertEqual(cfg.admin_networks, ["homelab", "mgmt"])
        self.assertEqual(cfg.admin_channels, ["#control", "#ops"])

        # Trusted networks
        self.assertTrue(cfg.is_trusted_network("homelab"))
        self.assertTrue(cfg.is_trusted_network("mgmt"))
        self.assertTrue(cfg.is_trusted_network("custom_sec"))

        # Backward compatibility aliases
        self.assertTrue(cfg.is_trusted_network("wireguard"))
        self.assertTrue(cfg.is_trusted_network("local"))
        self.assertTrue(cfg.is_trusted_network("wg"))

        # Untrusted networks
        self.assertFalse(cfg.is_trusted_network("public_irc"))
        self.assertFalse(cfg.is_trusted_network("libera"))
        self.assertFalse(cfg.is_trusted_network("oftc"))

    def test_run_bot_arg_parsing(self):
        # 1. Optional flags
        _, nick, net, chan, mods = parse_cli_args(
            ["--nick", "mybot", "--network", "libera", "--channel", "#lobby", "--modules", "infobot,urls"]
        )
        self.assertEqual(nick, "mybot")
        self.assertEqual(net, "libera")
        self.assertEqual(chan, "#lobby")
        self.assertEqual(mods, "infobot,urls")

        # 2. Positional arguments
        _, nick, net, chan, mods = parse_cli_args(["bot2", "local", "#dev", "web_search"])
        self.assertEqual(nick, "bot2")
        self.assertEqual(net, "local")
        self.assertEqual(chan, "#dev")
        self.assertEqual(mods, "web_search")

        # 3. Missing arguments
        _, nick, net, chan, mods = parse_cli_args([])
        self.assertIsNone(nick)
        self.assertIsNone(net)
        self.assertIsNone(chan)
        self.assertIsNone(mods)

    def test_dpop_auth_configuration(self):
        bot = MagicMock()
        bot.config = {
            "DPOP_AUTH": {
                "BASE_URL": "https://auth.example.com",
            }
        }
        with patch.dict(os.environ, {"DPOP_AUTH_KEY": "test_auth_key"}):
            plugin = DPoPAuthPlugin(bot)
            self.assertEqual(plugin.utils_url, "https://auth.example.com")
            self.assertEqual(plugin._get_api_auth_key(), "test_auth_key")

    def test_dai_identity_format(self):
        from sideload.dai import detect_self_identity_query
        self.assertTrue(detect_self_identity_query("who are you"))
        self.assertTrue(detect_self_identity_query("what model is this"))
        self.assertFalse(detect_self_identity_query("what is python?"))

    def test_ipc_socket_default_path(self):
        from main import SOCKET_IPC_PATH as MAIN_IPC_PATH
        from conn_daemon import SOCKET_IPC_PATH as CONN_IPC_PATH

        self.assertTrue(str(MAIN_IPC_PATH).endswith("data/minibots_ipc.sock"))
        self.assertTrue(str(CONN_IPC_PATH).endswith("data/minibots_ipc.sock"))


if __name__ == "__main__":
    unittest.main()
