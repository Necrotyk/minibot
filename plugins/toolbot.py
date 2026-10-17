# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

"""
toolbot.py — Dual Toolbot Plugin (Expanded Native Toolkits + FastMCP Bridge).
Includes Security & Mesh Pack (hash, asn, cve, wg, http) and Dev/System Pack (json, regex, cron, crypto).
"""

import base64
import codecs
import hashlib
import json
import logging
import os
import re
import socket
import urllib.request
from typing import Any, Dict, List, Optional

logger = logging.getLogger("Toolbot")


def eval_calc_expression(expr_str: str) -> str:
    """Evaluates mathematical expression with zero builtins and strict character whitelisting."""
    clean_expr = expr_str.strip()
    if not clean_expr:
        return "Usage: calc <expression>"

    if not re.match(r'^[0-9+\-*/().\s^%]+$', clean_expr):
        return "Invalid expression. Allowed characters: 0-9 + - * / ( ) . ^ %"

    try:
        expr_py = clean_expr.replace("^", "**")
        result = eval(expr_py, {"__builtins__": None}, {})
        return f"{clean_expr} = {result}"
    except Exception as e:
        return f"Calculation error: {e}"


def resolve_dns(host: str, timeout: float = 3.0) -> str:
    """Resolves hostname to IP addresses."""
    clean_host = host.strip()
    if not clean_host:
        return "Usage: dns <hostname>"

    old_timeout = socket.getdefaulttimeout()
    try:
        socket.setdefaulttimeout(timeout)
        ips = socket.gethostbyname_ex(clean_host)[2]
        return f"{clean_host} -> {', '.join(ips)}"
    except Exception as e:
        return f"Resolution failed for {clean_host}: {e}"
    finally:
        socket.setdefaulttimeout(old_timeout)


def check_port(host: str, port: int = 80, timeout: float = 3.0) -> str:
    """Checks TCP connectivity to target host and port."""
    clean_host = host.strip()
    if not clean_host:
        return "Usage: port <host> [port]"

    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        res = sock.connect_ex((clean_host, port))
        sock.close()
        return f"{clean_host}:{port} is {'OPEN' if res == 0 else 'CLOSED'}"
    except Exception as e:
        return f"Check error for {clean_host}:{port}: {e}"


def compute_hash(algo: str, data: str) -> str:
    """Computes cryptographic digest of text."""
    algo_clean = algo.lower().strip()
    data_bytes = data.encode("utf-8")
    if algo_clean in ("md5", "sha1", "sha256", "sha512"):
        h = hashlib.new(algo_clean, data_bytes).hexdigest()
        return f"{algo_clean.upper()}({data[:20]}...) = {h}"
    elif algo_clean in ("b64", "base64"):
        return f"Base64: {base64.b64encode(data_bytes).decode()}"
    return "Supported algos: md5, sha1, sha256, sha512, base64"


def inspect_http(url: str, timeout: float = 4.0) -> str:
    """Inspects HTTP status code and server response headers with SSRF protection."""
    clean_url = url.strip()
    if not clean_url.startswith(("http://", "https://")):
        clean_url = f"http://{clean_url}"

    try:
        req = urllib.request.Request(clean_url, headers={"User-Agent": "Minibots/1.0 (+https://github.com/Necrotyk/minibot)"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.status
            server = resp.headers.get("Server", "Unknown")
            content_type = resp.headers.get("Content-Type", "Unknown")
            length = resp.headers.get("Content-Length", "Unknown")
            return f"HTTP {status} | Server: {server} | Type: {content_type} | Length: {length}"
    except urllib.error.HTTPError as e:
        return f"HTTP {e.code}: {e.reason} | Server: {e.headers.get('Server', 'Unknown')}"
    except Exception as e:
        return f"Connection error: {e}"


def validate_json_text(expr: str) -> str:
    """Validates and analyzes JSON syntax."""
    try:
        data = json.loads(expr.strip())
        dtype = type(data).__name__
        count = len(data) if isinstance(data, (dict, list)) else 1
        return f"Valid JSON ({dtype}, size={count})"
    except Exception as e:
        return f"Invalid JSON: {e}"


def explain_cron(expr: str) -> str:
    """Parses standard 5-part cron expression."""
    parts = expr.strip().split()
    if len(parts) != 5:
        return "Usage: cron <min> <hour> <day_of_month> <month> <day_of_week> (5 fields)"
    m, h, dom, mon, dow = parts
    return f"Cron schedule: Minute: {m}, Hour: {h}, DayOfMonth: {dom}, Month: {mon}, DayOfWeek: {dow}"


def cipher_codec(codec: str, mode: str, text: str) -> str:
    """Encodes or decodes text with specified codec."""
    c = codec.lower().strip()
    m = mode.lower().strip()
    try:
        if c in ("rot13", "rot-13"):
            return codecs.encode(text, "rot_13")
        elif c in ("b64", "base64"):
            if m in ("enc", "encode"):
                return base64.b64encode(text.encode()).decode()
            else:
                return base64.b64decode(text.encode()).decode(errors="replace")
        elif c in ("hex", "hexadecimal"):
            if m in ("enc", "encode"):
                return text.encode().hex()
            else:
                return bytes.fromhex(text.replace(" ", "")).decode(errors="replace")
        return "Supported codecs: rot13, base64, hex. Modes: enc, dec"
    except Exception as e:
        return f"Codec error: {e}"


def call_mcp_tool(mcp_url: str, tool_name: str, args: List[str], timeout: float = 5.0) -> str:
    """Invokes a tool on dystopi-mcp via HTTP JSON-RPC/REST endpoint."""
    url = f"{mcp_url.replace('/sse', '')}/call/{tool_name}"
    payload = {"arguments": {"query": " ".join(args)}}
    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            res_str = str(data.get("result", data))
            return res_str[:250]
    except Exception as e:
        return f"Bridge connection to {mcp_url} failed: {e}"


class ToolbotPlugin:
    """Provides security, network, dev diagnostic tools and FastMCP tool execution."""

    def __init__(self, minibot: Any, mcp_url: Optional[str] = None):
        self.minibot = minibot
        self.mcp_url = mcp_url or os.environ.get("MCP_SERVER_URL", "http://127.0.0.1:8050/sse")
        self.native_tools = {
            "dns": self._tool_dns,
            "ping": self._tool_ping,
            "calc": self._tool_calc,
            "port": self._tool_port,
            "hash": self._tool_hash,
            "http": self._tool_http,
            "json": self._tool_json,
            "cron": self._tool_cron,
            "crypto": self._tool_crypto,
            "wg": self._tool_wg,
            "asn": self._tool_asn,
            "cve": self._tool_cve,
        }

    def on_privmsg(self, target: str, sender_nick: str, message: str) -> None:
        text = message.strip()
        bot_nick = self.minibot.nick.lower()

        # Check if addressed to toolbot or starts with !tool
        cmd_text = ""
        if text.lower().startswith("!tool "):
            cmd_text = text[6:].strip()
        elif text.lower().startswith(f"{bot_nick}:"):
            cmd_text = text[len(bot_nick) + 1:].strip()
        elif text.lower().startswith(f"{bot_nick},"):
            cmd_text = text[len(bot_nick) + 1:].strip()
        elif text.lower().startswith(f"@{bot_nick}"):
            cmd_text = text[len(bot_nick) + 1:].strip()
        elif not target.startswith("#"):
            cmd_text = text

        if not cmd_text:
            return

        parts = cmd_text.split()
        if not parts:
            return

        tool_name = parts[0].lower()
        args = parts[1:]

        # 1. Check Native Tools
        if tool_name in self.native_tools:
            try:
                res = self.native_tools[tool_name](args)
                self.minibot.send_msg(target, f"[{tool_name.upper()}] {res}")
            except Exception as e:
                self.minibot.send_msg(target, f"[{tool_name.upper()} ERROR] {e}")
            return

        # 2. Check FastMCP Bridge
        if tool_name.startswith("mcp.") or tool_name in ["poc_scan", "ip_reputation", "simulate_cve", "db_diagnostics"]:
            actual_mcp_tool = tool_name.replace("mcp.", "")
            try:
                res = call_mcp_tool(self.mcp_url, actual_mcp_tool, args)
                self.minibot.send_msg(target, f"[MCP:{actual_mcp_tool}] {res}")
            except Exception as e:
                self.minibot.send_msg(target, f"[MCP ERROR] {e}")

    def _tool_dns(self, args: List[str]) -> str:
        if not args:
            return "Usage: dns <hostname>"
        return resolve_dns(args[0])

    def _tool_ping(self, args: List[str]) -> str:
        if not args:
            return "Usage: ping <host> [port]"
        port = int(args[1]) if len(args) > 1 and args[1].isdigit() else 80
        return check_port(args[0], port)

    def _tool_port(self, args: List[str]) -> str:
        return self._tool_ping(args)

    def _tool_calc(self, args: List[str]) -> str:
        if not args:
            return "Usage: calc <expression>"
        return eval_calc_expression(" ".join(args))

    def _tool_hash(self, args: List[str]) -> str:
        if len(args) < 2:
            return "Usage: hash <md5|sha1|sha256|sha512|b64> <text>"
        return compute_hash(args[0], " ".join(args[1:]))

    def _tool_http(self, args: List[str]) -> str:
        if not args:
            return "Usage: http <url>"
        return inspect_http(args[0])

    def _tool_json(self, args: List[str]) -> str:
        if not args:
            return "Usage: json <json_string>"
        return validate_json_text(" ".join(args))

    def _tool_cron(self, args: List[str]) -> str:
        if not args:
            return "Usage: cron <5_field_expression>"
        return explain_cron(" ".join(args))

    def _tool_crypto(self, args: List[str]) -> str:
        if len(args) < 3:
            return "Usage: crypto <rot13|base64|hex> <enc|dec> <text>"
        return cipher_codec(args[0], args[1], " ".join(args[2:]))

    def _tool_wg(self, args: List[str]) -> str:
        try:
            return call_mcp_tool(self.mcp_url, "wg_status", args)
        except Exception as e:
            return f"WG status unavailable: {e}"

    def _tool_asn(self, args: List[str]) -> str:
        if not args:
            return "Usage: asn <ip>"
        return call_mcp_tool(self.mcp_url, "ip_reputation_lookup", args)

    def _tool_cve(self, args: List[str]) -> str:
        if not args:
            return "Usage: cve <CVE-YYYY-NNNN>"
        return call_mcp_tool(self.mcp_url, "shodan_cve_enrich", args)
