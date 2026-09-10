# SPDX-License-Identifier: AGPL-3.0-or-later
"""
test_lertz_threat_intel.py — Unit Tests for IBM Granite Lertz Threat Intelligence Sideload.
"""

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from sideload.lertz_threat_intel import (
    LertzDBReader,
    build_campaign_context,
    build_ip_context,
    execute_granite_inference,
    format_irc_output,
    handle_granite_command,
)


class TestLertzThreatIntelFormatting(unittest.TestCase):
    """Test suite for IRC cybernetic output formatting and capping."""

    def test_format_irc_output_single_line(self):
        lines = format_irc_output("TEST", "Single line threat summary.", "3B")
        self.assertEqual(len(lines), 1)
        self.assertIn("[TEST·3B]", lines[0])
        self.assertIn("Single line threat summary.", lines[0])

    def test_format_irc_output_multi_line_tree(self):
        text = "Line 1\nLine 2\nLine 3\nLine 4\nLine 5"
        lines = format_irc_output("TEST", text, "8B")
        self.assertEqual(len(lines), 4)  # Capped at max 4 lines
        self.assertIn("[TEST·8B]", lines[0])
        self.assertIn("↳", lines[1])
        self.assertIn("↳", lines[2])
        self.assertIn("↳", lines[3])

    def test_format_irc_output_line_truncation(self):
        long_line = "A" * 500
        lines = format_irc_output("TEST", long_line, "3B")
        self.assertEqual(len(lines), 1)
        self.assertLessEqual(len(lines[0]), 380)

    def test_format_irc_output_empty(self):
        lines = format_irc_output("TEST", "", "3B")
        self.assertEqual(len(lines), 1)
        self.assertIn("No analysis content generated", lines[0])


class TestContextBuilders(unittest.TestCase):
    """Test suite for pre-aggregated context strings."""

    def test_build_ip_context(self):
        timeline = {
            "ip": "45.155.205.233",
            "events_count": 12,
            "country": "RU",
            "asn": "AS44050",
            "threat_score": 8.5,
            "event_types": {"sqli": 5, "probe": 7},
            "targeted_paths": ["/admin", "/wp-login.php"],
            "summaries": ["SQLi attempt on /admin", "Path traversal on /config"],
            "active_ban": {"reason": "crowdsec ban"},
            "reputation": {"threat_score": 9.0, "is_tor": False, "is_vpn": True},
        }
        ctx = build_ip_context(timeline)
        self.assertIn("45.155.205.233", ctx)
        self.assertIn("Location: RU", ctx)
        self.assertIn("AS44050", ctx)
        self.assertIn("sqli:5", ctx)
        self.assertIn("/admin", ctx)
        self.assertIn("ACTIVE", ctx)
        self.assertIn("VPN:True", ctx)

    def test_build_campaign_context(self):
        camp = {
            "hours": 24.0,
            "total_events": 450,
            "unique_ips": 32,
            "active_ban_count": 5,
            "subnet_clusters": {"194.26.29.0/24": 120, "45.155.205.0/24": 80},
            "top_asns": {"AS44050": 150, "AS12345": 90},
            "top_paths": {"/wp-login.php": 85, "/.env": 60},
        }
        ctx = build_campaign_context(camp)
        self.assertIn("Past 24 Hours", ctx)
        self.assertIn("450 events across 32 unique IPs", ctx)
        self.assertIn("Active Bans: 5", ctx)
        self.assertIn("194.26.29.0/24", ctx)
        self.assertIn("/wp-login.php", ctx)


class TestLertzDBReader(unittest.TestCase):
    """Test suite for LertzDBReader database query aggregation."""

    def setUp(self):
        self.reader = LertzDBReader(dsn="dbname=lertz_mock")

    @patch("sideload.lertz_threat_intel.LertzDBReader._get_connection")
    def test_get_ip_timeline(self, mock_get_conn):
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_get_conn.return_value.__enter__.return_value = mock_conn
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur

        # Mock fetchall for ids_events
        mock_cur.fetchall.return_value = [
            (1700000000.0, "nginx_probe", "nginx", "NL", "AS1234", 7.0, "GET /wp-admin", '{"path": "/wp-admin"}')
        ]
        # Mock fetchone for reputation & ban
        mock_cur.fetchone.side_effect = [
            ("NL", "AS1234", False, False, 7.0, "Clean", 1, 1700000000.0),  # reputation
            ("banned for probe", 1700000000.0, 1700086400.0, "fail2ban"),  # active ban
        ]

        tl = self.reader.get_ip_timeline("1.2.3.4")
        self.assertEqual(tl["ip"], "1.2.3.4")
        self.assertEqual(tl["events_count"], 1)
        self.assertEqual(tl["country"], "NL")
        self.assertIn("/wp-admin", tl["targeted_paths"])
        self.assertIsNotNone(tl["active_ban"])
        self.assertIsNotNone(tl["reputation"])

    @patch("sideload.lertz_threat_intel.LertzDBReader._get_connection")
    def test_get_cve_details(self, mock_get_conn):
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_get_conn.return_value.__enter__.return_value = mock_conn
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur

        mock_cur.fetchone.side_effect = [
            ("CVE-2024-3094", "XZ Backdoor", 10.0, 0.95, "tukaani", "xz", "5.6.0-5.6.1", "Backdoor in upstream xz"),
            (3,),  # 3 matching probe events
        ]

        cve = self.reader.get_cve_details("CVE-2024-3094")
        self.assertIsNotNone(cve)
        self.assertEqual(cve["cve_id"], "CVE-2024-3094")
        self.assertEqual(cve["cvss_score"], 10.0)
        self.assertEqual(cve["local_probes_detected"], 3)


class TestGraniteInference(unittest.IsolatedAsyncioTestCase):
    """Test suite for execute_granite_inference."""

    @patch("sideload.lertz_threat_intel.httpx")
    async def test_successful_inference_strips_think_tags(self, mock_httpx):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "choices": [
                {
                    "message": {
                        "content": "<think>Scanning telemetry...</think>Threat Actor: Automated Scanner targeting CMS endpoints."
                    }
                }
            ]
        }
        mock_client = AsyncMock()
        mock_client.post.return_value = mock_resp
        mock_httpx.AsyncClient.return_value.__aenter__.return_value = mock_client

        ok, text = await execute_granite_inference("Analyze target IP", system_role="actor")
        self.assertTrue(ok)
        self.assertNotIn("<think>", text)
        self.assertEqual(text, "Threat Actor: Automated Scanner targeting CMS endpoints.")


class TestHandleGraniteCommand(unittest.IsolatedAsyncioTestCase):
    """Test suite for handle_granite_command routing and output."""

    def setUp(self):
        self.sent_messages = []
        self.send_cb = lambda target, msg: self.sent_messages.append((target, msg))

    async def test_empty_args_shows_usage(self):
        await handle_granite_command(self.send_cb, "#lertz-dystopi", "admin", [], is_admin=True)
        self.assertEqual(len(self.sent_messages), 1)
        self.assertIn("Usage: !gr", self.sent_messages[0][1])

    @patch("sideload.lertz_threat_intel.DB_READER.get_ip_timeline")
    async def test_actor_unknown_ip_returns_clean_notice(self, mock_get_tl):
        mock_get_tl.return_value = {
            "ip": "8.8.8.8",
            "events_count": 0,
            "reputation": {},
            "active_ban": None,
        }
        await handle_granite_command(
            self.send_cb,
            "#lertz-dystopi",
            "admin",
            ["actor", "8.8.8.8"],
            is_admin=True,
        )
        self.assertTrue(len(self.sent_messages) >= 2)
        # First message is analyzing acknowledgment badge
        self.assertIn("Analyzing IP 8.8.8.8", self.sent_messages[0][1])
        # Second message is zero telemetry notice
        self.assertIn("No historical telemetry", self.sent_messages[1][1])

    @patch("sideload.dai.switch_pi_model", new_callable=AsyncMock)
    async def test_switch_8b_calls_granite_8b(self, mock_switch):
        mock_switch.return_value = (True, "Switched successfully")
        await handle_granite_command(
            self.send_cb,
            "#lertz-dystopi",
            "admin",
            ["switch", "8b"],
            is_admin=True,
        )
        mock_switch.assert_called_once_with("granite-8b")


if __name__ == "__main__":
    unittest.main()

