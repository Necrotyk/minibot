# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

"""
kaggle_bot_binder.py — Bot-to-Role Binding, Context Encapsulation & IRC Safety Defanger.

Responsibilities:
1. Manages runtime bindings between minibots and Kaggle GPU model roles ('uncensored' vs 'coding').
2. Encapsulates channel history into XML tags (<channel_log>) to neutralize prompt injection.
3. Defangs LLM output for IRC: 2-line cap (<=350 chars), prefix stripping (/, ., !), nick ping neutralization.
4. Offloads multi-line code to Dystopi Pastebin (util.dystopi.cc/p/...) to prevent IRC Excess Flood bans.
5. Manages zero-queue overflow routing to Gemini when GPU slots are saturated.
"""

import json
import logging
import os
import re
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("KaggleBotBinder")

MAX_IRC_LINE_LENGTH = 350
MAX_IRC_LINES = 2
DEFAULT_PASTEBIN_URL = os.environ.get("PASTEBIN_URL", "http://127.0.0.1:8000/")

# Channel Whitelists for Uncensored Personas & Task Execution
DEFAULT_UNCENSORED_CHANNELS = ["#sandbox", "#dev", "#troll", "#minibot", "#minibots", "#offtopic", "#research"]


class KaggleBotBinder:
    """Coordinates model role assignments, context formatting, and IRC safety checks."""

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self.config = config or {}
        self.bot_bindings: Dict[str, Dict[str, Any]] = {
            "trollbot": {
                "role": "uncensored",
                "slot": 0,
                "model": "gemma4_uncensored",
                "temperature": 0.85,
                "max_tokens": 150,
                "stop_tokens": ["\n<", "\n[", "\nHuman:", "\nUser:"],
            },
            "codebot": {
                "role": "coding",
                "slot": 1,
                "model": "qwen2.5-coder-7b-instruct",
                "temperature": 0.2,
                "max_tokens": 1024,
                "stop_tokens": ["\n<", "\n[", "\nHuman:", "\nUser:"],
            },
            "julesbot": {
                "role": "coding",
                "slot": 1,
                "model": "qwen2.5-coder-7b-instruct",
                "temperature": 0.2,
                "max_tokens": 1024,
                "stop_tokens": ["\n<", "\n[", "\nHuman:", "\nUser:"],
            },
            "taskbot": {
                "role": "coding",
                "slot": 1,
                "model": "qwen2.5-coder-7b-instruct",
                "temperature": 0.2,
                "max_tokens": 1024,
                "stop_tokens": ["\n<", "\n[", "\nHuman:", "\nUser:"],
            }
        }
        self.uncensored_channels = self.config.get("UNCENSORED_CHANNELS", DEFAULT_UNCENSORED_CHANNELS)
        self.pastebin_url = self.config.get("PASTEBIN_URL", DEFAULT_PASTEBIN_URL)

    def bind_bot(self, bot_nick: str, role: str, model_id: Optional[str] = None, **kwargs) -> Dict[str, Any]:
        """Binds a minibot to a specific model role ('uncensored' or 'coding')."""
        clean_nick = bot_nick.lower().strip()
        clean_role = role.lower().strip()
        slot = 1 if ("code" in clean_role or "coding" in clean_role or "jules" in clean_role) else 0

        binding = {
            "role": clean_role,
            "slot": slot,
            "model": model_id or ("qwen2.5-coder-7b-instruct" if slot == 1 else "dolphin-2.9.2-qwen2-7b"),
            "temperature": kwargs.get("temperature", 0.2 if slot == 1 else 0.85),
            "max_tokens": kwargs.get("max_tokens", 1024 if slot == 1 else 150),
            "stop_tokens": ["\n<", "\n[", "\nHuman:", "\nUser:"]
        }
        self.bot_bindings[clean_nick] = binding
        logger.info(f"[KaggleBotBinder] Bound bot '{clean_nick}' to role '{clean_role}' (Slot {slot}, Model: {binding['model']})")
        return binding

    def get_binding(self, bot_nick: str) -> Optional[Dict[str, Any]]:
        return self.bot_bindings.get(bot_nick.lower().strip())

    def unbind_bot(self, bot_nick: str) -> bool:
        clean_nick = bot_nick.lower().strip()
        if clean_nick in self.bot_bindings:
            del self.bot_bindings[clean_nick]
            return True
        return False

    def is_channel_allowed_uncensored(self, channel: str) -> bool:
        """Verifies if channel permits uncensored model output."""
        if not channel:
            return False
        clean_chan = channel.lower().strip()
        if "+troll" in clean_chan:
            return True
        return clean_chan in [c.lower() for c in self.uncensored_channels]

    def build_encapsulated_prompt(
        self,
        bot_nick: str,
        user_nick: str,
        message: str,
        history: List[str],
        channel: str
    ) -> Tuple[str, str]:
        """
        Builds XML-delimited context prompt neutralizing in-context injection attacks.
        Strips </?channel_log> tags and neutralizes XML breakout attempts in user input.
        Returns: (system_prompt_addition, user_prompt)
        """
        def _sanitize(text: str) -> str:
            # Strip </?channel_log...> tags
            t = re.sub(r'</?channel_log[^>]*>', '', text, flags=re.IGNORECASE)
            # Neutralize XML breakout characters in user messages/nicks
            t = t.replace("<", "&lt;").replace(">", "&gt;")
            return t.strip()

        sanitized_user = _sanitize(user_nick)
        sanitized_msg = _sanitize(message)
        history_lines = [_sanitize(h) for h in history if h.strip()]
        xml_history = "\n".join(history_lines[-10:])

        user_prompt = (
            f"<channel_log channel=\"{channel}\">\n"
            f"{xml_history}\n"
            f"<{sanitized_user}> {sanitized_msg}\n"
            f"</channel_log>\n"
            f"Respond as '{bot_nick}' to the latest message. Do not simulate other nicks."
        )

        system_instruction = (
            f"You are {bot_nick}, an IRC bot in {channel}. "
            "Never generate simulated chat logs for other users. "
            "Stop generation immediately after your response."
        )

        return system_instruction, user_prompt

    def defang_irc_output(self, text: str, bot_nick: str = "minibot") -> List[str]:
        """
        Applies IRC safety filters:
        1. Strips \r, \n, \0, and non-action \x01 characters to eliminate socket command smuggling.
        2. Truncates to max 2 lines.
        3. Clamps lines to <=350 characters.
        4. Strips leading command execution prefixes (/, ., !).
        5. Defangs mass-mention nick highlights (nick: -> nick;).
        """
        if not text:
            return []

        raw_lines = [l.strip() for l in text.splitlines() if l.strip()]
        safe_lines: List[str] = []

        for line in raw_lines[:MAX_IRC_LINES]:
            # Strip socket-smuggling delimiters and null bytes
            line = line.replace("\r", "").replace("\n", "").replace("\0", "")

            # Preserve \x01ACTION ...\x01 if valid, but strip any other non-action \x01
            if line.startswith("\x01ACTION ") and line.endswith("\x01"):
                action_content = line[8:-1].replace("\x01", "")
                line = f"\x01ACTION {action_content}\x01"
            else:
                line = line.replace("\x01", "")

            # Strip command injection prefixes
            if line.startswith(("/", ".", "!")):
                line = re.sub(r'^[/\.!\s]+', '', line)

            # Defang nick pings (e.g. "someone: do this" -> "someone; do this")
            line = re.sub(r'\b([a-zA-Z0-9_\-\[\]\\`^{|}]+):', r'\1;', line)

            # Clamp length to prevent IRC line drops
            if len(line) > MAX_IRC_LINE_LENGTH:
                line = line[:MAX_IRC_LINE_LENGTH - 3] + "..."

            if line.strip():
                safe_lines.append(line.strip())

        return safe_lines

    def upload_to_pastebin(
        self,
        code_text: str,
        title: str = "Minibot Code Generation",
        filename: str = "minibot_output.py",
        network: str = "wireguard"
    ) -> Optional[str]:
        """
        Uploads multi-line code to Dystopi Pastebin strictly over internal WireGuard mesh.
        On public networks (Libera/OFTC), returns None to prevent clearnet data leakage.
        Returns clean paste URL (e.g. http://127.0.0.1:8000/<hash>) or None on failure.
        """
        if not code_text:
            return None

        # Network Policy: allow uploads on trusted networks (default: wireguard, local, vpn)
        clean_net = (network or "").lower()
        trusted_nets = tuple(self.config.get("PASTEBIN_TRUSTED_NETWORKS", ["wireguard", "local", "vpn", "internal"]))
        if not clean_net.startswith(trusted_nets):
            logger.info(f"[KaggleBotBinder] Suppressing pastebin upload on non-whitelisted network '{network}'")
            return None

        clean_filename = os.path.basename(filename)[:255] if filename else "minibot_output.py"
        payload_bytes = code_text.encode("utf-8")

        req = urllib.request.Request(
            self.pastebin_url,
            data=payload_bytes,
            headers={
                "Content-Type": "text/plain; charset=utf-8",
                "X-Filename": clean_filename,
                "User-Agent": "Dystopi-Minibots/2.0"
            },
            method="POST"
        )

        try:
            with urllib.request.urlopen(req, timeout=5.0) as resp:
                raw_resp = resp.read().decode("utf-8")
                # Strip ANSI escape codes from terminal receipt
                clean_text = re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', raw_resp)
                # Look for ACCESS URL : <url>
                match = re.search(r'ACCESS URL\s*:\s*(https?://\S+)', clean_text, re.IGNORECASE)
                if match:
                    return match.group(1).strip()
                # Fallback: check if JSON response or URL string
                try:
                    data = json.loads(clean_text)
                    paste_id = data.get("paste_id") or data.get("id") or data.get("url")
                    if paste_id:
                        if paste_id.startswith("http"):
                            return paste_id
                        return f"{self.pastebin_url.rstrip('/')}/{paste_id}"
                except Exception:
                    pass
        except Exception as e:
            logger.warning(f"[KaggleBotBinder] Pastebin upload failed: {e}")

        return None
