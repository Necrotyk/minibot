# SPDX-License-Identifier: AGPL-3.0-or-later
"""
test_toolbot_mcp.py — Unit Tests for Toolbot Native & FastMCP Bridge.
"""

import unittest
from unittest.mock import MagicMock, patch

from plugins.toolbot import ToolbotPlugin


class TestToolbot(unittest.TestCase):
    def setUp(self):
        self.mock_bot = MagicMock()
        self.mock_bot.nick = "toolbot"
        self.toolbot = ToolbotPlugin(self.mock_bot, mcp_url="http://127.0.0.1:8050/sse")

    def test_native_calc(self):
        res = self.toolbot._tool_calc(["(10", "+", "5)", "*", "2"])
        self.assertIn("30", res)

    def test_native_calc_invalid_syntax(self):
        res = self.toolbot._tool_calc(["__import__('os').system('ls')"])
        self.assertIn("Invalid expression", res)

    @patch("plugins.toolbot.urllib.request.urlopen")
    def test_mcp_bridge_call(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"result": "CVE-2024-1086: Linux Kernel Netfilter Privilege Escalation"}'
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        self.toolbot.on_privmsg("#sec", "alice", "!tool poc_scan CVE-2024-1086")
        self.mock_bot.send_msg.assert_called_with(
            "#sec",
            "[MCP:poc_scan] CVE-2024-1086: Linux Kernel Netfilter Privilege Escalation"
        )


if __name__ == "__main__":
    unittest.main()
