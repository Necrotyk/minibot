# SPDX-License-Identifier: AGPL-3.0-or-later
"""
test_dai_pipeline.py — Unit Test Suite for DAI Search, Context Memory & Reasoning Pipeline.
Verifies memory isolation, stopword filtering, SSRF defense, URL sanitization, and IRC output formatting.
"""

import time
import unittest

try:
    from adapters.sopel_shim import inject_sopel_shim
except ImportError:
    from minibots.adapters.sopel_shim import inject_sopel_shim
inject_sopel_shim()

from core.memory_graph import EphemeralChatMemory
from mcp_tools.ddg_fast_api import sanitize_url
from mcp_tools.fetch_content import FetchContentScraper, SSRFException
from mcp_tools.sequential_thinking_pi import SequentialThinkingPi
from sideload.dai import (
    MODEL_PRESETS,
    PRESET_LOOKUP,
    chunk_for_irc,
    clean_irc_output,
    cmd_ai,
    cmd_dai,
    cmd_model,
    detect_cultural_trope,
    detect_self_identity_query,
    format_dai_response,
    format_model_list,
    get_auto_switch,
    is_admin_authorized,
    is_bot_admin,
    is_citation_relevant,
    is_reasoning_requested,
    load_dai_state,
    run_pipeline,
    save_dai_state,
    set_auto_switch,
)


class TestDAIPipeline(unittest.TestCase):
    """Test suite covering DAI pipeline components and safety constraints."""

    def test_clean_irc_output(self) -> None:
        """Verifies stripping of markdown code fences, headers, bullets, bold, italics, and backticks."""
        raw_markdown = "### Header\n```python\nprint('hello world')\n```\n* Bullet item\nHere is **bold**, *italic*, and `code` text."
        cleaned = clean_irc_output(raw_markdown)
        self.assertNotIn("```", cleaned)
        self.assertNotIn("###", cleaned)
        self.assertNotIn("* Bullet", cleaned)
        self.assertNotIn("**", cleaned)
        self.assertNotIn("`", cleaned)
        self.assertNotIn("\n", cleaned)
        self.assertIn("Bullet item Here is bold, italic, and code text.", cleaned)

    def test_clean_irc_output_think_stripping(self) -> None:
        """Verifies stripping of think tags and leaked internal monologues before </think>."""
        leaked = "The user greeted me. I should respond politely.</think> Hello! How can I assist?"
        cleaned = clean_irc_output(leaked)
        self.assertNotIn("</think>", cleaned)
        self.assertNotIn("The user greeted me", cleaned)
        self.assertEqual(cleaned, "Hello! How can I assist?")

        with_opening = "<think>Inner thought</think> Actual answer."
        cleaned2 = clean_irc_output(with_opening)
        self.assertEqual(cleaned2, "Actual answer.")

    def test_format_dai_response_standard(self) -> None:
        """Verifies cyan/teal color theme, badge, 1-2 line cap, and inline citation."""
        ans = "WireGuard is an extremely simple yet fast and modern VPN that utilizes state-of-the-art cryptography."
        url = "https://www.wireguard.com"
        lines = format_dai_response(
            nick="alice",
            raw_answer=ans,
            top_url=url,
            should_reason=False,
            prefix_type="normal",
            is_pm=False,
        )
        self.assertLessEqual(len(lines), 2)
        # Verify teal badge \x0310[DAI]
        self.assertIn("\x0310\x02[DAI]\x0f", lines[0])
        # Verify nick mention in channel
        self.assertIn("alice:", lines[0])
        # Verify cyan text \x0311
        self.assertIn("\x0311", lines[0])
        # Verify grey inline citation \x0314— url
        self.assertIn("\x0314— https://www.wireguard.com\x0f", lines[-1])

    def test_format_dai_response_reasoning(self) -> None:
        """Verifies [DAI·Reason] badge and max 3 lines for deep synthesis."""
        long_ans = (
            "WireGuard operates inside Linux kernel space with UDP. "
            "It eliminates handshake round-trips via Noise Protocol. "
            "TCP-over-TCP causes catastrophic meltdown under packet loss. "
            "Therefore UDP provides high throughput without deadlock."
        )
        lines = format_dai_response(
            nick="bob",
            raw_answer=long_ans,
            top_url="https://example.com/wireguard",
            should_reason=True,
            prefix_type="normal",
            is_pm=False,
        )
        self.assertLessEqual(len(lines), 3)
        self.assertIn("\x0310\x02[DAI·Reason]\x0f", lines[0])
        self.assertIn("\x0314— https://example.com/wireguard\x0f", lines[-1])

    def test_format_dai_response_pm(self) -> None:
        """Verifies PM responses omit the nick prefix."""
        lines = format_dai_response(
            nick="alice",
            raw_answer="Direct answer in PM.",
            top_url="",
            should_reason=False,
            prefix_type="normal",
            is_pm=True,
        )
        self.assertEqual(len(lines), 1)
        self.assertNotIn("alice:", lines[0])
        self.assertIn("\x0310\x02[DAI]\x0f", lines[0])

    def test_chunk_for_irc_limits(self) -> None:
        """Verifies RFC 2812 line limits and strict max 4 lines ceiling."""
        long_text = (
            "Sentence one discusses microcomputers. Sentence two explains ARM64 architectures and performance. "
            "Sentence three details containerized workloads with cgroup limits. "
            "Sentence four reviews secure WireGuard networking topologies. "
            "Sentence five should be dropped completely because the hard cap is four lines."
        )
        lines = chunk_for_irc(long_text, max_bytes=80, max_lines=4)
        self.assertLessEqual(len(lines), 4)
        for line in lines:
            self.assertLessEqual(len(line.encode("utf-8")), 80)

    def test_ephemeral_memory_cross_referencing(self) -> None:
        """Verifies multi-user topic linking across conversations in the same scope."""
        mem = EphemeralChatMemory(max_user_history=6, ttl_seconds=60)
        scope = "oftc:#chatterbox"

        # User A asks about Docker on RPi
        mem.record_interaction(scope, "alice", "Can I install docker on rpi?", ["docker", "rpi"])

        # User B asks about memory constraints
        mem.record_interaction(scope, "bob", "How do I limit memory in container?", ["docker", "memory"])

        # Bob requests merged context for "docker"
        context = mem.get_merged_context(scope, "bob", ["docker"], max_tokens=100)

        # Context must contain Alice's query and exclude Bob's own messages
        self.assertTrue(any("alice:" in line and "docker" in line for line in context))
        self.assertFalse(any("bob:" in line for line in context))

    def test_ephemeral_memory_network_and_pm_isolation(self) -> None:
        """Verifies PMs and different networks never leak context into public channels."""
        mem = EphemeralChatMemory(max_user_history=6, ttl_seconds=60)

        # Alice chats in PM on oftc
        pm_scope = mem.make_scope("oftc", "alice", "alice")
        self.assertEqual(pm_scope, "oftc:pm:alice")
        mem.record_interaction(pm_scope, "alice", "Secret key in PM: 12345", ["secret", "docker"])

        # Channel scope on oftc
        chan_scope = mem.make_scope("oftc", "#chat", "bob")
        self.assertEqual(chan_scope, "oftc:#chat")
        context = mem.get_merged_context(chan_scope, "bob", ["docker"])
        self.assertEqual(context, [], "PM context must never leak into channel scope")

        # Libera channel scope
        libera_scope = mem.make_scope("libera", "#chat", "charlie")
        mem.record_interaction(libera_scope, "charlie", "Libera message", ["docker"])
        context_oftc = mem.get_merged_context(chan_scope, "bob", ["docker"])
        self.assertEqual(context_oftc, [], "Network contexts must be strictly isolated")

    def test_ephemeral_memory_stopword_filtering(self) -> None:
        """Verifies common English stopwords are discarded from entity linking."""
        mem = EphemeralChatMemory()
        self.assertIsNone(mem._normalize_entity("this"))
        self.assertIsNone(mem._normalize_entity("what"))
        self.assertIsNone(mem._normalize_entity("how"))
        self.assertIsNone(mem._normalize_entity("a"))
        self.assertEqual(mem._normalize_entity("wireguard"), "wireguard")
        self.assertEqual(mem._normalize_entity("docker"), "docker")

    def test_ephemeral_memory_ttl_pruning(self) -> None:
        """Verifies entries expire and are purged after TTL window."""
        mem = EphemeralChatMemory(ttl_seconds=1)
        scope = "oftc:#test"
        mem.record_interaction(scope, "alice", "Expiring message", ["testtopic"])
        self.assertIn("alice", mem.last_seen[scope])

        # Fast forward monotonic timestamp
        mem.last_seen[scope]["alice"] = time.monotonic() - 10
        mem.prune_stale(scope)
        self.assertNotIn(scope, mem.last_seen)

    def test_ssrf_safety_guard_and_redirects(self) -> None:
        """Verifies SSRF defense against loopback, WireGuard, RFC1918, and metadata endpoints."""
        scraper = FetchContentScraper()
        with self.assertRaises(SSRFException):
            scraper._assert_safe_url("http://127.0.0.1:8080/admin")
        with self.assertRaises(SSRFException):
            scraper._assert_safe_url("http://169.254.169.254/latest/meta-data/")
        with self.assertRaises(SSRFException):
            scraper._assert_safe_url("http://10.0.0.12/internal")
        with self.assertRaises(SSRFException):
            scraper._assert_safe_url("http://192.168.1.1/router")
        with self.assertRaises(SSRFException):
            scraper._assert_safe_url("ftp://example.com/file")

    def test_ddg_sanitize_url(self) -> None:
        """Verifies stripping of tracking and marketing parameters from URLs."""
        url_with_tracking = "https://example.com/guide?utm_source=duckduckgo&utm_medium=web&ref=banner&fbclid=abc123xyz#section"
        clean = sanitize_url(url_with_tracking)
        self.assertNotIn("utm_source", clean)
        self.assertNotIn("utm_medium", clean)
        self.assertNotIn("ref=", clean)
        self.assertNotIn("fbclid", clean)
        self.assertTrue(clean.startswith("https://example.com/guide"))

    def test_reasoning_detection(self) -> None:
        """Verifies explicit flags, shortcut commands, and smart heuristics."""
        # Shortcut command dair
        req, q = is_reasoning_requested("dair", "Why does WireGuard use UDP?")
        self.assertTrue(req)
        self.assertEqual(q, "Why does WireGuard use UDP?")

        # Shortcut command diar (typo alias)
        req, q = is_reasoning_requested("diar", "how do you milk a cat")
        self.assertTrue(req)
        self.assertEqual(q, "how do you milk a cat")

        # Explicit flag
        req, q = is_reasoning_requested("dai", "-r Why does WireGuard use UDP?")
        self.assertTrue(req)
        self.assertEqual(q, "Why does WireGuard use UDP?")

        # Short simple query without flag should NOT trigger reasoning
        req, q = is_reasoning_requested("dai", "how are you?")
        self.assertFalse(req)

        # Deep comparison query > 35 chars
        req, q = is_reasoning_requested("dai", "What is the difference between Docker and Podman?")
        self.assertTrue(req)

    def test_cultural_trope_detection(self) -> None:
        """Verifies detection of Meet the Parents, Monty Python, Hitchhiker's Guide, and other tropes."""
        # Meet the Parents
        t1 = detect_cultural_trope("how do you milk a cat")
        self.assertIsNotNone(t1)
        self.assertEqual(t1["reference"], "Meet the Parents (2000)")
        self.assertIn("nipples", t1["quote_hint"])

        t1_variant = detect_cultural_trope("can you milk me?")
        self.assertIsNotNone(t1_variant)
        self.assertEqual(t1_variant["reference"], "Meet the Parents (2000)")

        # Monty Python
        t2 = detect_cultural_trope("what is the air speed velocity of an unladen swallow?")
        self.assertIsNotNone(t2)
        self.assertIn("Monty Python", t2["reference"])

        # Hitchhiker's Guide
        t3 = detect_cultural_trope("what is the answer to life the universe and everything")
        self.assertIsNotNone(t3)
        self.assertIn("Hitchhiker", t3["reference"])

        # Office Space
        t4 = detect_cultural_trope("what does pc load letter mean")
        self.assertIsNotNone(t4)
        self.assertIn("Office Space", t4["reference"])

        # It Runs Doom
        t5 = detect_cultural_trope("can a smart fridge run doom?")
        self.assertIsNotNone(t5)
        self.assertIn("Doom", t5["reference"])

        # Regular technical query should NOT trigger trope detection
        t6 = detect_cultural_trope("how do I configure wireguard on debian")
        self.assertIsNone(t6)

    def test_citation_relevance(self) -> None:
        """Verifies filtering of root homepages (openai.com), ads, and irrelevant links."""
        # Generic root domain (openai.com) should be rejected
        self.assertFalse(
            is_citation_relevant("https://openai.com/", "what is a freaxeh", "A freaxeh seems to be a nickname.", "OpenAI is an AI research company.")
        )
        self.assertFalse(
            is_citation_relevant("https://openai.com/", "what model are you operating as", "I am an AI assistant.", "OpenAI homepage.")
        )

        # Ad redirect (duckduckgo.com/y.js) should be rejected
        self.assertFalse(
            is_citation_relevant("https://duckduckgo.com/y.js?ad_domain=newegg.com", "freaxeh has 512gb nvme", "NVMe storage can be formatted.", "Ad snippet")
        )

        # Legitimate technical documentation matching query/answer should be accepted
        self.assertTrue(
            is_citation_relevant("https://en.wikipedia.org/wiki/UserDatagramProtocol", "Why does WireGuard use UDP?", "WireGuard uses UDP to avoid TCP meltdown.", "User Datagram Protocol (UDP) is a transport protocol.")
        )
        self.assertTrue(
            is_citation_relevant("https://www.geeksforgeeks.org/devops/podman-vs-docker/", "compare docker and podman", "Podman is daemonless and rootless.", "Docker vs Podman comparison.")
        )

        # Explicit user-provided URL in query should be accepted
        self.assertTrue(
            is_citation_relevant("http://www.example.com/", "summarize https://example.com", "Example.com is an illustrative domain.", "Example Domain")
        )

    def test_self_identity_detection(self) -> None:
        """Verifies detection of model/identity queries to bypass DDG search."""
        self.assertTrue(detect_self_identity_query("what model are you operating as"))
        self.assertTrue(detect_self_identity_query("what model are you"))
        self.assertTrue(detect_self_identity_query("which model are you using"))
        self.assertTrue(detect_self_identity_query("who are you"))
        self.assertTrue(detect_self_identity_query("what are you"))
        self.assertTrue(detect_self_identity_query("what is dai"))

        self.assertFalse(detect_self_identity_query("what is a freaxeh"))
        self.assertFalse(detect_self_identity_query("how do you milk a cat"))
        self.assertFalse(detect_self_identity_query("compare docker and podman"))

    def test_model_presets_and_lookup(self) -> None:
        """Verifies model preset definitions, roles, and alias resolutions."""
        expected_presets = {"llama", "lfm", "granite", "granite-8b", "gemma", "roci"}
        self.assertEqual(set(MODEL_PRESETS.keys()), expected_presets)

        for key, p in MODEL_PRESETS.items():
            self.assertIn("name", p)
            self.assertIn("path", p)
            self.assertIn("alias", p)
            self.assertIn("desc", p)
            self.assertIn("role", p)
            self.assertTrue(p["path"].endswith(".gguf"))

        # Test alias resolutions
        self.assertEqual(PRESET_LOOKUP.get("llama"), "llama")
        self.assertEqual(PRESET_LOOKUP.get("llama-3.2-1b"), "llama")
        self.assertEqual(PRESET_LOOKUP.get("fast"), "llama")
        self.assertEqual(PRESET_LOOKUP.get("1b"), "llama")

        self.assertEqual(PRESET_LOOKUP.get("lfm"), "lfm")
        self.assertEqual(PRESET_LOOKUP.get("lfm2.5"), "lfm")
        self.assertEqual(PRESET_LOOKUP.get("thinking"), "lfm")
        self.assertEqual(PRESET_LOOKUP.get("reason"), "lfm")

        self.assertEqual(PRESET_LOOKUP.get("granite"), "granite")
        self.assertEqual(PRESET_LOOKUP.get("3b"), "granite")
        self.assertEqual(PRESET_LOOKUP.get("granite-8b"), "granite-8b")
        self.assertEqual(PRESET_LOOKUP.get("8b"), "granite-8b")

        self.assertEqual(PRESET_LOOKUP.get("gemma"), "gemma")
        self.assertEqual(PRESET_LOOKUP.get("coder"), "gemma")

        self.assertEqual(PRESET_LOOKUP.get("roci"), "roci")
        self.assertEqual(PRESET_LOOKUP.get("12b"), "roci")

    def test_admin_auth(self) -> None:
        """Verifies bot admin permission gating across dispatchers and fallback lists."""
        import types

        mock_bot = types.SimpleNamespace(
            _minibot=types.SimpleNamespace(
                dispatcher=types.SimpleNamespace(
                    is_admin=lambda n: n.split("!")[0].strip().lower() in ("admin", "operator")
                )
            )
        )

        self.assertTrue(is_bot_admin(mock_bot, "admin"))
        self.assertTrue(is_bot_admin(mock_bot, "admin!user@host"))
        self.assertTrue(is_bot_admin(mock_bot, "operator"))
        self.assertFalse(is_bot_admin(mock_bot, "bob_7702"))
        self.assertFalse(is_bot_admin(mock_bot, "admin_attacker"))
        self.assertFalse(is_bot_admin(mock_bot, ""))

    def test_auto_switch_state(self) -> None:
        """Verifies auto-switch state toggle and persistence."""
        prev = get_auto_switch()
        try:
            set_auto_switch(True)
            self.assertTrue(get_auto_switch())
            set_auto_switch(False)
            self.assertFalse(get_auto_switch())
        finally:
            set_auto_switch(prev)

    def test_format_model_list(self) -> None:
        """Verifies formatting of model list in channel mode and PM mode."""
        lines_chan = format_model_list("lfm", is_pm=False)
        self.assertLessEqual(len(lines_chan), 3)
        self.assertIn("[DAI·Models]", lines_chan[0])
        self.assertIn("Auto-Switch:", lines_chan[0])
        self.assertIn("[ACTIVE] lfm", lines_chan[1])
        self.assertIn("llama", lines_chan[1])

        lines_pm = format_model_list("llama", is_pm=True)
        self.assertGreaterEqual(len(lines_pm), 6)
        self.assertIn("[ACTIVE]", lines_pm[1])
        self.assertIn("llama", lines_pm[1])

    def test_is_admin_authorized(self) -> None:
        """Verifies Option 2 security policy: WireGuard network and admin channel perimeter gating."""
        import types

        mock_disp = types.SimpleNamespace(is_admin=lambda n: n.split("!")[0].strip().lower() in ("admin", "operator"))
        bot_public = types.SimpleNamespace(network="libera", _minibot=types.SimpleNamespace(dispatcher=mock_disp))
        bot_wireguard = types.SimpleNamespace(network="wireguard", _minibot=types.SimpleNamespace(dispatcher=mock_disp))

        t_unauth_wg = types.SimpleNamespace(nick="bob_7702", sender="#minibots")
        ok, msg = is_admin_authorized(bot_wireguard, t_unauth_wg)
        self.assertFalse(ok)
        self.assertIn("restricted to bot admins", msg)

        # Attacker spoofing nick 'admin' on public network #test-channel
        t_spoof_pub = types.SimpleNamespace(nick="admin", sender="#test-channel")
        ok, msg = is_admin_authorized(bot_public, t_spoof_pub)
        self.assertFalse(ok)
        self.assertIn("restricted to the internal WireGuard network or admin channels", msg)

        # Admin on WireGuard in #minibots
        t_admin_wg = types.SimpleNamespace(nick="admin", sender="#minibots")
        ok, msg = is_admin_authorized(bot_wireguard, t_admin_wg)
        self.assertTrue(ok)
        self.assertEqual(msg, "OK")

        # Admin in designated admin channel #minibots on external network
        t_admin_chan = types.SimpleNamespace(nick="admin", sender="#minibots")
        ok, msg = is_admin_authorized(bot_public, t_admin_chan)
        self.assertTrue(ok)

    def test_cmd_model_permissions(self) -> None:
        """Verifies permission gating on IRC .model commands across public channels and WireGuard."""
        import types

        class MockBot:
            def __init__(self, network="wireguard"):
                self.network = network
                self.messages = []
                self._minibot = types.SimpleNamespace(
                    network=network,
                    dispatcher=types.SimpleNamespace(
                        is_admin=lambda n: n.split("!")[0].strip().lower() in ("admin", "operator")
                    )
                )

            def say(self, msg, recipient):
                self.messages.append((recipient, msg))

        class MockTrigger:
            def __init__(self, nick, cmd, args="", sender="#minibots"):
                self.nick = nick
                self.sender = sender
                self.is_privmsg = False
                self._cmd = cmd
                self._args = args

            def group(self, n):
                if n == 1:
                    return self._cmd
                elif n == 2:
                    return self._args
                return ""

        # Test on WireGuard admin channel
        bot_wg = MockBot(network="wireguard")

        # Non-admin attempting to switch model
        t_unauth = MockTrigger("bob_7702", "model", "llama", sender="#minibots")
        cmd_model(bot_wg, t_unauth)
        self.assertTrue(any("Permission denied" in m[1] for m in bot_wg.messages))

        # Attacker spoofing admin nick in public channel #test-channel on libera
        bot_pub = MockBot(network="libera")
        t_spoofed_pub = MockTrigger("admin", "model", "llama", sender="#test-channel")
        cmd_model(bot_pub, t_spoofed_pub)
        self.assertTrue(any("restricted to the internal WireGuard network or admin channels" in m[1] for m in bot_pub.messages))

        # Admin toggling auto-switch in #minibots on WireGuard
        bot_wg.messages.clear()
        t_admin_auto = MockTrigger("admin", "model", "auto on", sender="#minibots")
        cmd_model(bot_wg, t_admin_auto)
        self.assertTrue(any("Auto-switch enabled" in m[1] for m in bot_wg.messages))
        self.assertTrue(get_auto_switch())

        # Admin disabling auto-switch in #minibots on WireGuard
        bot_wg.messages.clear()
        t_admin_auto_off = MockTrigger("admin", "model", "auto off", sender="#minibots")
        cmd_model(bot_wg, t_admin_auto_off)
        self.assertTrue(any("Auto-switch disabled" in m[1] for m in bot_wg.messages))
        self.assertFalse(get_auto_switch())

        # Anyone checking auto-switch status in public channel
        bot_pub.messages.clear()
        t_status = MockTrigger("bob_7702", "model", "auto", sender="#test-channel")
        cmd_model(bot_pub, t_status)
        self.assertTrue(any("Auto-switch is currently" in m[1] for m in bot_pub.messages))

    def test_format_ai_response(self) -> None:
        """Verifies [AI] and [AI·Reason] badges and zero citations for pure AI chat."""
        # Standard chat reply
        lines = format_dai_response(
            nick="alice",
            raw_answer="Linux is a family of open-source Unix-like operating systems based on the Linux kernel.",
            top_url="",
            should_reason=False,
            prefix_type="normal",
            is_pm=False,
            badge_label="AI",
        )
        self.assertLessEqual(len(lines), 2)
        self.assertIn("\x0310\x02[AI]\x0f", lines[0])
        self.assertIn("alice:", lines[0])
        self.assertNotIn("— http", lines[-1])

        # Deep reasoning reply
        lines_r = format_dai_response(
            nick="bob",
            raw_answer="Line 1 analysis.\nLine 2 analysis.\nLine 3 conclusion.",
            top_url="",
            should_reason=True,
            prefix_type="normal",
            is_pm=False,
            badge_label="AI",
        )
        self.assertLessEqual(len(lines_r), 3)
        self.assertIn("\x0310\x02[AI·Reason]\x0f", lines_r[0])
        self.assertNotIn("— http", lines_r[-1])

        # Error response
        lines_err = format_dai_response(
            nick="charlie",
            raw_answer="Worker unreachable.",
            top_url="",
            should_reason=False,
            prefix_type="error",
            is_pm=False,
            badge_label="AI",
        )
        self.assertIn("\x0304\x02[AI Error]\x0f", lines_err[0])

    def test_ai_reasoning_requested(self) -> None:
        """Verifies reasoning detection rules for .ai vs .dai."""
        # Pure AI chat does not trigger heavy heuristic reasoning on long comparison questions
        req, q = is_reasoning_requested("ai", "What is the difference between Docker and Podman?")
        self.assertFalse(req)
        self.assertEqual(q, "What is the difference between Docker and Podman?")

        # Pure AI chat DOES trigger reasoning when explicit -r or --think is passed
        req, q = is_reasoning_requested("ai", "-r What is the difference between Docker and Podman?")
        self.assertTrue(req)
        self.assertEqual(q, "What is the difference between Docker and Podman?")

        req, q = is_reasoning_requested("ask", "--think Explain quantum entanglement")
        self.assertTrue(req)
        self.assertEqual(q, "Explain quantum entanglement")

        # Shortcut .air forces reasoning
        req, q = is_reasoning_requested("air", "Explain quantum entanglement")
        self.assertTrue(req)
        self.assertEqual(q, "Explain quantum entanglement")

    def test_cmd_ai_usage(self) -> None:
        """Verifies usage hints for .ai and .air when called without query."""
        import types

        class MockBot:
            def __init__(self):
                self.messages = []
            def say(self, msg, recipient):
                self.messages.append((recipient, msg))

        class MockTrigger:
            def __init__(self, nick, cmd, args="", sender="#test-channel"):
                self.nick = nick
                self.sender = sender
                self._cmd = cmd
                self._args = args
            def group(self, n):
                if n == 1:
                    return self._cmd
                elif n == 2:
                    return self._args
                return ""

        bot = MockBot()

        # .ai without query
        cmd_ai(bot, MockTrigger("alice", "ai", "", sender="#test-channel"))
        self.assertTrue(any("[AI]" in m[1] and "Usage: .ai [-r] <prompt>" in m[1] for m in bot.messages))

        # .air without query
        bot.messages.clear()
        cmd_ai(bot, MockTrigger("bob", "air", "", sender="#test-channel"))
        self.assertTrue(any("[AI·Reason]" in m[1] and "Usage: .air [-r] <prompt>" in m[1] for m in bot.messages))

    def test_ai_pipeline_bypasses_web_search(self) -> None:
        """Verifies that run_pipeline with enable_web_search=False never touches DDG or fetch scrapers."""
        import asyncio
        from unittest.mock import AsyncMock, patch

        class MockBot:
            def __init__(self):
                self.network = "wireguard"
                self.messages = []
            def say(self, msg, recipient):
                self.messages.append((recipient, msg))

        bot = MockBot()
        fake_llm_response = {
            "choices": [{"message": {"content": "The Linux kernel manages hardware resources directly."}}]
        }

        with patch("sideload.dai._call_pi_with_retry", new=AsyncMock(return_value=fake_llm_response)) as mock_call, \
             patch("sideload.dai.DDG_CLIENT.search", new=AsyncMock(return_value=[])) as mock_ddg, \
             patch("sideload.dai.FETCH_CLIENT.fetch", new=AsyncMock(return_value="")) as mock_fetch:

            asyncio.run(
                run_pipeline(
                    bot,
                    target="#test-channel",
                    nick="alice",
                    raw_query="explain what the linux kernel does",
                    force_reasoning=False,
                    enable_web_search=False,
                )
            )

            # Web search and scraper MUST NOT be called
            mock_ddg.assert_not_called()
            mock_fetch.assert_not_called()

            # LLM should be called only once (Pass 3 generation, skipping Pass 1 extraction)
            self.assertEqual(mock_call.call_count, 1)

            # Response formatting
            self.assertTrue(len(bot.messages) >= 1)
            first_msg = bot.messages[0][1]
            self.assertIn("\x0310\x02[AI]\x0f", first_msg)
            self.assertIn("alice:", first_msg)
            self.assertIn("Linux kernel manages hardware resources", first_msg)
            # No citations
            for _, msg in bot.messages:
                self.assertNotIn("— http", msg)

    def test_cmd_model_authorized_switch_no_name_error(self) -> None:
        """Verifies that an authorized admin invoking .model does not crash with NameError for 'preset'."""
        import asyncio
        import types
        from unittest.mock import AsyncMock, patch

        class MockBot:
            def __init__(self, network="wireguard"):
                self.network = network
                self.messages = []
                self._minibot = types.SimpleNamespace(
                    network=network,
                    dispatcher=types.SimpleNamespace(
                        is_admin=lambda n: n.split("!")[0].strip().lower() in ("admin", "operator")
                    )
                )

            def say(self, msg, recipient):
                self.messages.append((recipient, msg))

        class MockTrigger:
            def __init__(self, nick, cmd, args="", sender="#minibots"):
                self.nick = nick
                self.sender = sender
                self.is_privmsg = False
                self._cmd = cmd
                self._args = args

            def group(self, n):
                if n == 1:
                    return self._cmd
                elif n == 2:
                    return self._args
                return ""

        bot = MockBot(network="wireguard")
        trigger = MockTrigger("admin", "model", "llama", sender="#minibots")

        with patch("sideload.dai.get_active_model_info", new=AsyncMock(return_value=("lfm", "LFM2.5-1.2B-Thinking"))), \
             patch("sideload.dai.switch_pi_model", new=AsyncMock(return_value=(True, "Successfully switched to llama-3.2-1b (llama) in 5s."))):

            async def _run():
                cmd_model(bot, trigger)
                await asyncio.sleep(0.05)

            asyncio.run(_run())

            self.assertTrue(any("Switching Pi 5 worker to llama-3.2-1b" in m[1] for m in bot.messages))
            self.assertTrue(any("Successfully switched" in m[1] for m in bot.messages))

    def test_short_tech_token_extraction(self) -> None:
        """Verifies that 1-2 char tech terms (C, Go, IP, OS, VM, JS, TS) and C++ are not stripped."""
        from sideload.dai import extract_fast_path_keywords

        # C++ query
        kw1 = extract_fast_path_keywords("how to use c++ lambda syntax")
        self.assertIn("c++", [k.lower() for k in kw1])

        # Go query
        kw2 = extract_fast_path_keywords("go channels vs mutex performance")
        self.assertIn("go", [k.lower() for k in kw2])

        # IP query
        kw3 = extract_fast_path_keywords("ip route add default gateway")
        self.assertIn("ip", [k.lower() for k in kw3])

        # VM / OS query
        kw4 = extract_fast_path_keywords("kvm vm setup on debian os")
        self.assertIn("vm", [k.lower() for k in kw4])
        self.assertIn("os", [k.lower() for k in kw4])

    def test_shared_one_turn_conversational_memory(self) -> None:
        """Verifies 1-turn memory storage with 90s TTL, per-user isolation, and error filtering."""
        mem = EphemeralChatMemory(ttl_seconds=90)
        scope = "libera:#test-channel"

        # Alice asks a valid question and gets an answer
        mem.record_interaction(scope, "alice", "What is Debian 12?", ["debian"])
        mem.record_assistant_reply(scope, "alice", "Debian 12 Bookworm is the current stable release.")

        turn_alice = mem.get_user_last_turn(scope, "alice", max_age=90.0)
        self.assertIsNotNone(turn_alice)
        self.assertEqual(turn_alice["query"], "What is Debian 12?")
        self.assertIn("Debian 12 Bookworm", turn_alice["reply"])

        # Bob asks in the same channel — should have an independent clean slate
        turn_bob = mem.get_user_last_turn(scope, "bob", max_age=90.0)
        self.assertIsNone(turn_bob, "Turns must be isolated per-user")

        # An error response must NOT be saved to turn memory
        mem.record_interaction(scope, "alice", "bad query", [])
        mem.record_assistant_reply(scope, "alice", "Worker unreachable. Unable to generate response.", is_error=True)

        # Last valid turn should be preserved or cleared, not polluted with error text
        turn_alice_after_err = mem.get_user_last_turn(scope, "alice", max_age=90.0)
        if turn_alice_after_err:
            self.assertNotIn("Worker unreachable", turn_alice_after_err["reply"])

    def test_pi_inference_scheduler_priority_and_aging(self) -> None:
        """Verifies PiInferenceScheduler enforces single execution, priority ordering (P0 before P2), and aging."""
        import asyncio
        from sideload.dai import PiInferenceScheduler

        scheduler = PiInferenceScheduler(max_queue_depth=4)
        execution_order = []

        async def task(task_id: str, prio: int, sleep_time: float = 0.05):
            async with scheduler.acquire(prio, nick=task_id):
                execution_order.append(f"start_{task_id}")
                await asyncio.sleep(sleep_time)
                execution_order.append(f"end_{task_id}")

        async def _run():
            # Start a long running task at P1
            t1 = asyncio.create_task(task("long_p1", prio=1, sleep_time=0.10))
            await asyncio.sleep(0.01)

            # While t1 runs, enqueue a P2 task (reasoning) and then a P0 task (fast AI)
            t2 = asyncio.create_task(task("reason_p2", prio=2, sleep_time=0.02))
            await asyncio.sleep(0.01)
            t0 = asyncio.create_task(task("fast_p0", prio=0, sleep_time=0.02))

            await asyncio.gather(t1, t2, t0)

        asyncio.run(_run())

        # Assert fast_p0 started before reason_p2 even though reason_p2 was queued first
        self.assertIn("start_fast_p0", execution_order)
        self.assertIn("start_reason_p2", execution_order)
        idx_fast = execution_order.index("start_fast_p0")
        idx_reason = execution_order.index("start_reason_p2")
        self.assertLess(idx_fast, idx_reason, "P0 fast AI must execute before P2 reasoning")

    def test_grok_citation_boundary_in_dai(self) -> None:
        """Verifies that .dai can utilize Grokipedia context, but NEVER cites grokipedia.com in the final output."""
        from sideload.dai import is_citation_relevant

        # In .dai mode, grokipedia.com must NOT be accepted as a citation link unless cmd is .grok
        self.assertFalse(
            is_citation_relevant("https://grokipedia.com/page/Linux", "what is linux", "Linux is a Unix-like kernel.", cmd_name="dai"),
            "In .dai mode, grokipedia.com must not be cited"
        )
        # In .grok mode, grokipedia.com IS valid
        self.assertTrue(
            is_citation_relevant("https://grokipedia.com/page/Linux", "what is linux", "Linux is a Unix-like kernel.", cmd_name="grok"),
            "In .grok mode, grokipedia.com should be cited"
        )


if __name__ == "__main__":
    unittest.main()
