# SPDX-License-Identifier: AGPL-3.0-or-later
"""
test_tool_hub_and_craft.py — Unit Tests for ToolCatalog, ToolCrafter & Dynamic Remote Pulling.
"""

import ast
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from core.sideload import SideloadEngine
from core.tool_catalog import ToolCatalog
from core.tool_craft import ToolCrafter


class TestToolHubAndCraft(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.sideload_dir = Path(self.temp_dir) / "sideload"
        self.sideload_dir.mkdir(parents=True, exist_ok=True)
        self.sideload_engine = SideloadEngine(sideload_dir=str(self.sideload_dir))
        self.catalog = ToolCatalog(sideload_dir=self.sideload_dir)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_catalog_listing_and_filtering(self):
        tools = self.catalog.list_tools()
        self.assertGreaterEqual(len(tools), 6)
        tool_names = [t["name"] for t in tools]
        self.assertIn("infobot", tool_names)
        self.assertIn("web_search", tool_names)
        self.assertIn("sysadmin_tools", tool_names)
        self.assertIn("crypto_calc", tool_names)
        self.assertIn("weather_time", tool_names)
        self.assertIn("ai_assistant", tool_names)

        # Filter by category
        info_tools = self.catalog.list_tools(category="info")
        self.assertEqual(len(info_tools), 1)
        self.assertEqual(info_tools[0]["name"], "infobot")

    def test_catalog_search(self):
        # Search for youtube
        yt_results = self.catalog.search_tools("youtube")
        self.assertTrue(any(t["name"] == "infobot" for t in yt_results))

        # Search for dns
        dns_results = self.catalog.search_tools("dns")
        self.assertTrue(any(t["name"] == "sysadmin_tools" for t in dns_results))

        # Search for hash
        hash_results = self.catalog.search_tools("hash")
        self.assertTrue(any(t["name"] == "crypto_calc" for t in hash_results))

    def test_catalog_info_and_install(self):
        info = self.catalog.get_tool_info("web_search")
        self.assertIsNotNone(info)
        self.assertEqual(info["category"], "search")
        self.assertIn("ddg", info["commands"])

        # Install into temp sideload dir
        ok, msg = self.catalog.install_tool("web_search")
        self.assertTrue(ok)
        self.assertTrue((self.sideload_dir / "web_search.py").is_file())
        self.assertTrue(self.catalog.is_installed("web_search"))

    def test_sideload_url_normalization(self):
        # GitHub blob
        blob_url = "https://github.com/sopel-irc/sopel/blob/master/sopel/modules/ping.py"
        raw_url = self.sideload_engine.normalize_remote_url(blob_url)
        self.assertEqual(raw_url, "https://raw.githubusercontent.com/sopel-irc/sopel/master/sopel/modules/ping.py")

        # GitHub Gist
        gist_url = "https://gist.github.com/torvalds/1234567890abcdef"
        gist_raw = self.sideload_engine.normalize_remote_url(gist_url)
        self.assertEqual(gist_raw, "https://gist.githubusercontent.com/torvalds/1234567890abcdef/raw")

        # Regular URL
        norm_url = "https://example.com/script.py"
        self.assertEqual(self.sideload_engine.normalize_remote_url(norm_url), norm_url)

    def test_tool_crafter_ast_safety_and_fences(self):
        mock_router = MagicMock()
        crafter = ToolCrafter(mock_router, sideload_dir=self.sideload_dir)

        # 1. Fence stripping
        code_with_fences = "```python\nfrom sopel import module\n@module.commands('hi')\ndef hi(bot, tr):\n    bot.say('hi')\n```"
        stripped = crafter._strip_markdown_fences(code_with_fences)
        self.assertNotIn("```", stripped)
        self.assertIn("@module.commands", stripped)

        # 2. AST security rejects dangerous modules
        safe, reason = crafter.check_ast_safety("import subprocess\nsubprocess.run(['ls'])")
        self.assertFalse(safe)
        self.assertIn("subprocess", reason)

        safe_ctypes, reason_ctypes = crafter.check_ast_safety("import ctypes")
        self.assertFalse(safe_ctypes)
        self.assertIn("ctypes", reason_ctypes)

        safe_eval, reason_eval = crafter.check_ast_safety("x = eval('2+2')")
        self.assertFalse(safe_eval)
        self.assertIn("eval", reason_eval)

        # 3. Valid Sopel script passes safety
        safe_sopel, _ = crafter.check_ast_safety("from sopel import module\n@module.commands('roll')\ndef roll(b, t): b.say('4')")
        self.assertTrue(safe_sopel)

    def test_tool_crafter_synthesis_flow(self):
        mock_router = MagicMock()
        mock_router.generate.return_value = (
            "```python\n"
            "from sopel import module\n\n"
            "@module.commands('echo')\n"
            "def echo_cmd(bot, trigger):\n"
            "    args = trigger.group(2) or ''\n"
            "    bot.say(f'Echo: {args}')\n"
            "```"
        )
        crafter = ToolCrafter(mock_router, sideload_dir=self.sideload_dir)
        ok, msg = crafter.craft_tool("echo", "Echo back whatever the user types")
        self.assertTrue(ok)
        self.assertIn("Successfully crafted", msg)
        self.assertIn("echo", msg)
        self.assertTrue((self.sideload_dir / "echo.py").is_file())


if __name__ == "__main__":
    unittest.main()
