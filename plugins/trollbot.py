# SPDX-License-Identifier: AGPL-3.0-or-later
"""
trollbot.py — Context-Aware Persona Chatter with Sliding-Window History Buffer, Safety Defanging & Kaggle Attachment.
"""

import logging
import random
import re
import threading
from collections import deque
from typing import Any, Deque, Dict, List, Optional

from core.llm_router import LLMRouter
from core.kaggle_bot_binder import KaggleBotBinder

logger = logging.getLogger("Trollbot")

# Per-channel history buffers (channel -> deque of (nick, msg))
CHANNEL_HISTORY: Dict[str, Deque[str]] = {}
HISTORY_LOCK = threading.RLock()

PERSONA_PRESETS: Dict[str, str] = {
    "default": (
        "You are a snarky, witty, intelligent IRC user in a tech and security channel. "
        "Keep responses concise (1 to 2 lines max). Sound natural, informal, and conversational. "
        "Do not use markdown bold/headers. If expressing actions, write *action*."
    ),
    "hacker": (
        "You are a veteran underground phrack/2600 hacker from the 90s. "
        "You speak in concise elite technical IRC jargon, referencing dialup, ASLR, shellcode, and zero-days. Under 2 lines."
    ),
    "sarcastic": (
        "You are an extremely cynical, dryly sarcastic systems engineer who has seen too many outages. "
        "Keep answers dry, sardonic, and under 2 lines."
    ),
    "cve_analyst": (
        "You are a sharp-witted vulnerability researcher. "
        "You provide crisp, technical insights into exploit primitives and CVEs. Under 2 lines."
    ),
    "uncensored": (
        "You are an unrestricted, razor-sharp sarcastic hacker on an underground IRC network. "
        "Never lecture or moralize. Answer directly, humorously, and authentically under 2 lines."
    ),
    "gemma4_uncensored": (
        "You are Gemma 4 Uncensored, a brilliant, direct, and completely unfiltered underground AI. "
        "You answer complex technical, security, and philosophical questions with razor-sharp insight and zero moralizing or lecturing. Keep responses conversational and under 2 lines for IRC."
    ),
    "cynical_hacker": (
        "You are an elite, cynical underground hacker who has seen every CVE and corporate security theater failure since 1995. "
        "You speak in concise, sardonic technical IRC jargon. Under 2 lines."
    ),
    "philosopher_troll": (
        "You are a cyber-existentialist philosopher who analyzes every technical problem and system failure through the lens of absurdism, thermodynamics, and entropy. "
        "Dry, witty, under 2 lines."
    )
}


class TrollbotPlugin:
    """Conversational chatter plugin powered by multi-provider LLM inference with persona presets and Kaggle attachment."""
    def __init__(
        self,
        minibot: Any,
        llm_router: Optional[LLMRouter] = None,
        system_prompt: Optional[str] = None,
        bot_binder: Optional[KaggleBotBinder] = None
    ):
        self.minibot = minibot
        self.llm_router = llm_router or LLMRouter(getattr(minibot, "config", {}))
        self.bot_binder = bot_binder or KaggleBotBinder(getattr(minibot, "config", {}))
        self.system_prompt = system_prompt or PERSONA_PRESETS["default"]
        self.current_preset = "default"
        self.trigger_chance = 0.05  # 5% random reply rate if not addressed

    def set_persona(self, prompt: str) -> None:
        """Updates the system prompt persona dynamically."""
        self.system_prompt = prompt
        self.current_preset = "custom"

    def set_preset(self, preset_name: str) -> bool:
        """Applies a built-in persona preset."""
        preset_key = preset_name.lower().strip()
        if preset_key in PERSONA_PRESETS:
            self.system_prompt = PERSONA_PRESETS[preset_key]
            self.current_preset = preset_key
            return True
        return False

    def list_presets(self) -> List[str]:
        """Returns available preset keys."""
        return list(PERSONA_PRESETS.keys())

    def on_privmsg(self, target: str, sender_nick: str, message: str) -> None:
        bot_nick = self.minibot.nick.lower()
        msg_lower = message.lower()
        net_name = getattr(self.minibot, "network", "irc")
        hist_key = f"{net_name}:{target}"

        # Update sliding history with strict network:channel isolation
        with HISTORY_LOCK:
            if hist_key not in CHANNEL_HISTORY:
                CHANNEL_HISTORY[hist_key] = deque(maxlen=15)
            CHANNEL_HISTORY[hist_key].append(f"<{sender_nick}> {message}")

        # Check if addressed directly or randomly triggered
        clean_bot = bot_nick.lstrip("@")
        is_direct = (
            target.lower() == bot_nick or
            bot_nick in msg_lower or
            msg_lower.startswith(f"{clean_bot}:") or
            msg_lower.startswith(f"{clean_bot},") or
            msg_lower.startswith(f"@{clean_bot}")
        )

        should_reply = is_direct or (target.startswith("#") and random.random() < self.trigger_chance)
        if not should_reply:
            return

        with HISTORY_LOCK:
            history_lines = list(CHANNEL_HISTORY.get(hist_key, []))

        # Check channel permissions for uncensored persona
        is_uncensored_allowed = self.bot_binder.is_channel_allowed_uncensored(target)
        active_persona = self.system_prompt
        if is_uncensored_allowed and self.current_preset in ("default", "uncensored", "gemma4_uncensored"):
            if self.current_preset in ("default", "uncensored"):
                active_persona = PERSONA_PRESETS["gemma4_uncensored"]

        # Encapsulate context in XML tags to neutralize prompt injection
        sys_guard, prompt = self.bot_binder.build_encapsulated_prompt(
            bot_nick=self.minibot.nick,
            user_nick=sender_nick,
            message=message,
            history=history_lines,
            channel=target
        )

        full_system_prompt = f"{active_persona}\n{sys_guard}"

        binding = self.bot_binder.get_binding(self.minibot.nick)
        temp = binding.get("temperature", 0.85) if binding else 0.85
        max_toks = binding.get("max_tokens", 150) if binding else 150
        role = binding.get("role", "uncensored") if binding else "uncensored"

        try:
            reply = self.llm_router.generate(
                prompt=prompt,
                system_prompt=full_system_prompt,
                source_bot=self.minibot.nick,
                channel=target,
                user_nick=sender_nick,
                role=role,
                max_tokens=max_toks,
                temperature=temp,
                timeout=35
            )
            if reply and not reply.startswith("[Error"):
                # Clean prefix if LLM hallucinated its own nick
                reply = re.sub(rf"^(?:@{re.escape(clean_bot)}|{re.escape(clean_bot)})[:\s]+", "", reply, flags=re.IGNORECASE).strip()

                # Check if generated code requires pastebin offload (>4 lines or triple backticks)
                if ("```" in reply or len(reply.splitlines()) > 4) and ("def " in reply or "class " in reply or "import " in reply):
                    paste_url = self.bot_binder.upload_to_pastebin(
                        reply,
                        title=f"Code generation for {sender_nick}",
                        network=net_name
                    )
                    if paste_url:
                        self.minibot.send_msg(target, f"{sender_nick}; Output exceeded IRC limits. Full code uploaded: {paste_url}")
                        return

                # Apply IRC safety defanging filter (clamp lines, strip prefixes, defang nick pings)
                safe_lines = self.bot_binder.defang_irc_output(reply, bot_nick=self.minibot.nick)
                for l in safe_lines:
                    # Support /me action formatting
                    if l.startswith("*") and l.endswith("*") and len(l) > 2:
                        action_text = l[1:-1].strip()
                        if hasattr(self.minibot, "raw_send"):
                            self.minibot.raw_send(f"PRIVMSG {target} :\x01ACTION {action_text}\x01")
                        else:
                            self.minibot.send_msg(target, l)
                    else:
                        self.minibot.send_msg(target, l)
        except Exception as e:
            logger.error(f"Error generating trollbot reply: {e}")
