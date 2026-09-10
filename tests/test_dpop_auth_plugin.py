# SPDX-License-Identifier: AGPL-3.0-or-later
"""
test_dpop_auth_plugin.py — Comprehensive Unit Tests for Minibots DPoP Sanctuary Auth Plugin.
Verifies polite channel receipts, PM-only token delivery, cloak-aware anti-abuse,
5-device warning flows, review queues, and Ring 0 admin controls (!dpop).
"""

import time
import unittest
from unittest.mock import MagicMock, patch

from plugins.dpop_auth import DPoPAuthPlugin


class TestDPoPAuthPlugin(unittest.TestCase):
    def setUp(self):
        self.sent_messages = []
        self.mock_nickserv_code = 3

        def mock_send_msg(target, msg):
            self.sent_messages.append((target, msg))
            if target.lower() == "nickserv" and msg.startswith("STATUS "):
                parts = msg.split()
                if len(parts) >= 2:
                    queried_nick = parts[1]
                    code = getattr(self, "mock_nickserv_code", 3)
                    self.plugin.on_notice("NickServ", "authbot", f"STATUS {queried_nick} {code} account")

        self.bot = MagicMock()
        self.bot.nick = "authbot"
        self.bot.network = "oftc"
        self.bot.send_msg = mock_send_msg

        self.plugin = DPoPAuthPlugin(self.bot, config={"UTILS_URL": "http://127.0.0.1:8000"})

    @patch.object(DPoPAuthPlugin, "get_active_devices")
    @patch.object(DPoPAuthPlugin, "create_knock_invite")
    def test_auto_mode_pass_dispatch(self, mock_create, mock_devices):
        """In auto mode, !auth immediately creates knock link, dispatches polite channel receipt and private PM."""
        self.plugin.global_mode = "auto"
        mock_devices.return_value = (1, [{"device_label": "Browser 1"}])
        mock_create.return_value = (True, {
            "master_token": "lz_knock_test123",
            "full_knock_url": "https://util.dystopi.cc/k/lz_knock_test123"
        })

        # Run in channel
        self.plugin.on_privmsg("#dystopi", "alice", "!auth", hostmask="alice!user@oftc/user/alice")

        # 1 message to channel (polite receipt)
        chan_msgs = [m for m in self.sent_messages if m[0] == "#dystopi"]
        self.assertEqual(len(chan_msgs), 1)
        self.assertIn("Pass dispatched to your PM, alice", chan_msgs[0][1])

        # 6 messages to alice's PM (claim link)
        pm_msgs = [m for m in self.sent_messages if m[0] == "alice"]
        self.assertEqual(len(pm_msgs), 6)
        self.assertTrue(any("https://util.dystopi.cc/k/lz_knock_test123" in m[1] for m in pm_msgs))

    @patch.object(DPoPAuthPlugin, "get_active_devices")
    def test_five_device_warning_flow(self, mock_devices):
        """When user already has 5 active devices, !auth prompts for confirmation before evicting."""
        self.plugin.global_mode = "auto"
        mock_devices.return_value = (5, [{"device_label": f"Device {i}"} for i in range(5)])

        self.plugin.on_privmsg("#dystopi", "bob", "!pass", hostmask="bob!user@oftc/user/bob")

        # Bob receives warning in channel
        chan_msgs = [m for m in self.sent_messages if m[0] == "#dystopi"]
        self.assertEqual(len(chan_msgs), 1)
        self.assertIn("5/5 active devices", chan_msgs[0][1])
        self.assertIn("!auth confirm", chan_msgs[0][1])

    @patch.object(DPoPAuthPlugin, "get_active_devices")
    @patch.object(DPoPAuthPlugin, "create_knock_invite")
    def test_moderated_mode_ticket_and_approval(self, mock_create, mock_devices):
        """In moderated mode, !auth queues a ticket which admin can approve via !dpop approve <id>."""
        self.plugin.global_mode = "moderated"
        mock_devices.return_value = (0, [])
        mock_create.return_value = (True, {
            "master_token": "lz_knock_mod123",
            "full_knock_url": "https://util.dystopi.cc/k/lz_knock_mod123"
        })

        # Charlie requests pass in moderated mode
        self.plugin.on_privmsg("#dystopi", "charlie", "!auth", hostmask="charlie!ident@vpn.node")

        chan_msgs = [m for m in self.sent_messages if m[0] == "#dystopi"]
        self.assertEqual(len(chan_msgs), 1)
        self.assertIn("submitted for moderation", chan_msgs[0][1])

        # Operator checks queue via handle_admin_cmd
        res_list = self.plugin.handle_admin_cmd("oftc", "#minibots", "admin", ["list"])
        self.assertIn("charlie", res_list)

        # Operator approves ticket 1
        res_approve = self.plugin.handle_admin_cmd("oftc", "#minibots", "admin", ["approve", "1"])
        self.assertIn("Approved request #1", res_approve)

        # Ticket approved: Charlie receives PM with pass
        pm_msgs = [m for m in self.sent_messages if m[0] == "charlie"]
        self.assertEqual(len(pm_msgs), 6)
        self.assertTrue(any("https://util.dystopi.cc/k/lz_knock_mod123" in m[1] for m in pm_msgs))

    @patch.object(DPoPAuthPlugin, "get_active_devices")
    def test_cloak_rate_limiting_and_rapid_drop(self, mock_devices):
        """Users requesting within 60s are silently dropped; requests within 24h are polite-rejected."""
        self.plugin.global_mode = "auto"
        mock_devices.return_value = (0, [])

        cloak = "eve!user@oftc/user/eve"
        # First request sets history
        self.plugin.hostmask_history["oftc/user/eve"] = time.time() - 30  # 30s ago

        # Eve requests again <60s -> quiet drop (0 messages sent)
        self.plugin.on_privmsg("#dystopi", "eve", "!auth", hostmask=cloak)
        self.assertEqual(len(self.sent_messages), 0)

        # Eve requests again after 2 hours (>60s but <24h) -> cooldown message sent to channel
        self.plugin.hostmask_history["oftc/user/eve"] = time.time() - 7200  # 2 hours ago
        self.plugin.on_privmsg("#dystopi", "eve", "!auth", hostmask=cloak)
        chan_msgs = [m for m in self.sent_messages if m[0] == "#dystopi"]
        self.assertEqual(len(chan_msgs), 1)
        self.assertIn("within the last 24h", chan_msgs[0][1])

    @patch.object(DPoPAuthPlugin, "get_active_devices")
    @patch.object(DPoPAuthPlugin, "create_knock_invite")
    def test_nickserv_status_3_allows_auth(self, mock_create, mock_devices):
        """User identified with NickServ (status 3) is allowed to authenticate."""
        self.plugin.global_mode = "auto"
        self.mock_nickserv_code = 3
        mock_devices.return_value = (0, [])
        mock_create.return_value = (True, {
            "master_token": "lz_knock_ns3",
            "full_knock_url": "https://util.dystopi.cc/k/lz_knock_ns3"
        })

        self.plugin.on_privmsg("#dystopi", "frank", "!auth", hostmask="frank!ident@uncloaked.host")
        pm_msgs = [m for m in self.sent_messages if m[0] == "frank"]
        self.assertTrue(any("https://util.dystopi.cc/k/lz_knock_ns3" in m[1] for m in pm_msgs))

    def test_nickserv_status_2_rejects_auth(self):
        """User registered but NOT identified with NickServ (status 2) is blocked from authenticating."""
        self.plugin.global_mode = "auto"
        self.mock_nickserv_code = 2

        self.plugin.on_privmsg("#dystopi", "mallory", "!auth", hostmask="mallory!ident@uncloaked.host")
        chan_msgs = [m for m in self.sent_messages if m[0] == "#dystopi"]
        self.assertEqual(len(chan_msgs), 1)
        self.assertIn("NickServ authentication required", chan_msgs[0][1])

    @patch.object(DPoPAuthPlugin, "get_user_files")
    def test_files_chatops_command(self, mock_files):
        """!files lists user's uploads with status and links, plus polite channel receipt."""
        mock_files.return_value = (True, {
            "count": 2,
            "items": [
                {
                    "id": "item1",
                    "type": "paste",
                    "display_name": "Config Snippet",
                    "expires_at": "2026-10-01 00:00:00",
                    "is_permanent": False,
                    "share_links": [{"status": "active"}]
                },
                {
                    "id": "item2",
                    "type": "file",
                    "display_name": "firmware.bin",
                    "expires_at": None,
                    "is_permanent": True,
                    "share_links": []
                }
            ]
        })

        self.plugin.on_privmsg("#dystopi", "alice", "!files", hostmask="alice!user@oftc/user/alice")

        # Channel receipt
        chan_msgs = [m for m in self.sent_messages if m[0] == "#dystopi"]
        self.assertEqual(len(chan_msgs), 1)
        self.assertIn("You have 2 items in your vault", chan_msgs[0][1])

        # Detailed PM
        pm_msgs = [m for m in self.sent_messages if m[0] == "alice"]
        self.assertTrue(any("Config Snippet" in m[1] for m in pm_msgs))
        self.assertTrue(any("firmware.bin" in m[1] for m in pm_msgs))
        self.assertTrue(any("Permanent" in m[1] for m in pm_msgs))

    @patch.object(DPoPAuthPlugin, "create_user_share")
    @patch.object(DPoPAuthPlugin, "revoke_user_share")
    def test_share_create_and_revoke_command(self, mock_revoke, mock_share):
        """!share generates a new knock link and !share revoke cancels it."""
        mock_share.return_value = (True, {
            "knock_url": "/k/lz_knock_share123",
            "full_knock_url": "https://util.dystopi.cc/k/lz_knock_share123"
        })
        mock_revoke.return_value = (True, {"status": "revoked"})

        # 1. Create share link
        self.plugin.on_privmsg("#dystopi", "alice", "!share item1 48 multi", hostmask="alice!user@oftc/user/alice")
        mock_share.assert_called_once_with(
            network="oftc",
            nick="alice",
            item_id="item1",
            ttl_hours=48,
            max_claims=-1,
            notify_irc=True
        )
        chan_msgs = [m for m in self.sent_messages if m[0] == "#dystopi"]
        self.assertIn("Knock share link created for #item1", chan_msgs[0][1])
        pm_msgs = [m for m in self.sent_messages if m[0] == "alice"]
        self.assertTrue(any("https://util.dystopi.cc/k/lz_knock_share123" in m[1] for m in pm_msgs))

        # 2. Revoke share link
        self.sent_messages.clear()
        self.plugin.on_privmsg("#dystopi", "alice", "!share revoke abc123hash", hostmask="alice!user@oftc/user/alice")
        mock_revoke.assert_called_once_with("oftc", "alice", "abc123hash")
        chan_msgs2 = [m for m in self.sent_messages if m[0] == "#dystopi"]
        self.assertIn("revoked", chan_msgs2[0][1])

    @patch.object(DPoPAuthPlugin, "get_user_inactivity")
    @patch.object(DPoPAuthPlugin, "get_active_devices")
    @patch.object(DPoPAuthPlugin, "create_knock_invite")
    def test_inactivity_warning_flow(self, mock_create, mock_devices, mock_inact):
        """When user is within 7 days of inactivity threshold, a warning notice is sent."""
        self.plugin.global_mode = "auto"
        mock_devices.return_value = (0, [])
        mock_create.return_value = (True, {"master_token": "tok"})
        mock_inact.return_value = (True, {
            "is_warning": True,
            "days_remaining": 3.5,
            "retention_policy": "4_months"
        })

        self.plugin.on_privmsg("#dystopi", "alice", "!auth", hostmask="alice!user@oftc/user/alice")
        pm_msgs = [m for m in self.sent_messages if m[0] == "alice"]
        self.assertTrue(any("INACTIVITY NOTICE" in m[1] and "3.5 days" in m[1] for m in pm_msgs))


if __name__ == "__main__":
    unittest.main()

