# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

"""
julesbot.py — Google Jules Autonomous AI Coding Agent Plugin.
"""

import logging
import os
import re
import sqlite3
from typing import Any, Dict, List, Optional

import keystore
from core.telemetry import sqlite_retry
from jules.client import JulesClient
from jules.git_ops import check_remote_branch, create_remote_branch
from jules.poller import JulesPoller

logger = logging.getLogger("JulesBot")


class JulesBotPlugin:
    """Plugin providing conversational autonomous coding via Google Jules in #jules."""
    def __init__(self, minibot: Any, api_key: Optional[str] = None, default_repo: Optional[str] = None):
        self.minibot = minibot
        self.default_repo = default_repo or os.environ.get("JULES_DEFAULT_REPO", "owner/repo")
        keys: List[str] = []
        if api_key:
            keys.append(api_key)
        else:
            primary = keystore.get_secret("JULES_API_KEY", "")
            if primary:
                keys.append(primary)
            for env_k, env_v in os.environ.items():
                if ("JULES_API_KEY" in env_k or env_k.endswith("_JULES_KEY")) and env_v and env_v not in keys:
                    keys.append(env_v)

        self.api_keys = keys
        self.api_key = keys[0] if keys else ""
        self.default_repo = default_repo
        self.client = JulesClient(self.api_keys) if self.api_keys else None
        self.poller = JulesPoller(self.client) if self.client else None
        self.active_sessions: Dict[str, str] = {}  # label -> session_id

        if self.poller:
            self.poller.add_callback(self._on_jules_event)
            self.poller.start()

    def _on_jules_event(self, session_id: str, channel: str, event: Dict[str, Any]) -> None:
        label = event.get("label", "task")
        text = event.get("text", "")
        self.minibot.send_msg(channel, f"[Jules:{label}] {text}")

    def on_privmsg(self, target: str, sender_nick: str, message: str) -> None:
        text = message.strip()
        if not text:
            return

        bot_nick = self.minibot.nick.lower()
        clean_bot = bot_nick.lstrip("@")
        in_jules_room = target.lower() == "#jules"
        is_direct_msg = not target.startswith("#")

        # Handle commands starting with !jules or addressed directly in #jules / PM
        cmd_text = ""
        low_text = text.lower()

        if low_text.startswith("!jules"):
            cmd_text = text[6:].strip()
            if not cmd_text:
                cmd_text = "help"
        elif low_text.startswith(f"{clean_bot}:") or low_text.startswith(f"{clean_bot},"):
            cmd_text = text[len(clean_bot) + 1:].strip()
        elif low_text.startswith(f"@{clean_bot}"):
            cmd_text = text[len(clean_bot) + 1:].strip()
        elif low_text.startswith("jules:") or low_text.startswith("jules,"):
            cmd_text = text[6:].strip()
        elif is_direct_msg:
            cmd_text = text.lstrip("!")
        elif in_jules_room:
            # In #jules, allow direct !commands, natural verbs, or help triggers
            if text.startswith("!"):
                cmd_text = text[1:].strip()
            elif low_text in ("help", "h", "jules", "repos", "list", "sessions", "status"):
                cmd_text = low_text
            elif any(low_text.startswith(verb + " ") for verb in ("init", "approve", "status", "cancel", "repos", "help", "list", "sessions", "diff", "logs")):
                cmd_text = text

        if not cmd_text:
            return

        parts = cmd_text.split(maxsplit=2)
        action = parts[0].lower() if parts else "help"

        if action in ("help", "h", "man", "?", "--help", "-h", "jules"):
            self.minibot.send_msg(target, "\x02\x0303━━━ Google Jules Autonomous AI Coding Agent ━━━\x0f")
            self.minibot.send_msg(target, "\x02!init <label> [repo] <prompt>\x0f — Provision branch & spawn coding session")
            self.minibot.send_msg(target, "\x02!approve <label>\x0f — Approve generated plan & begin code execution")
            self.minibot.send_msg(target, "\x02!status <label>\x0f — Check live session state, plan, and progress")
            self.minibot.send_msg(target, "\x02!cancel <label>\x0f — Terminate and clean up an active coding session")
            self.minibot.send_msg(target, "\x02!repos\x0f — List default & supported GitHub repositories")
            self.minibot.send_msg(target, "\x0314Example:\x0f !init refactor-auth owner/repo Add strict token bucket rate limiter")
            return

        if action in ("repos", "repo"):
            self.minibot.send_msg(target, f"[Jules Repos] Default: \x02{self.default_repo}\x0f (Configurable via JULES_DEFAULT_REPO)")
            return

        if action in ("list", "ls", "ps", "sessions"):
            if not self.active_sessions:
                self.minibot.send_msg(target, "[Jules] No active coding sessions.")
            else:
                sessions_str = ", ".join([f"{k} ({v})" for k, v in self.active_sessions.items()])
                self.minibot.send_msg(target, f"[Jules Sessions] {sessions_str}")
            return

        if not self.client:
            self.minibot.send_msg(target, "[Jules Error] Google Jules API key is not configured.")
            return

        if action == "init":
            if len(parts) < 3:
                self.minibot.send_msg(target, "Usage: !init <label> [repo] <prompt>")
                return
            label = parts[1]
            rest = parts[2].split(maxsplit=1)
            repo = rest[0] if len(rest) > 1 and "/" in rest[0] else self.default_repo
            prompt = rest[1] if len(rest) > 1 and "/" in rest[0] else parts[2]

            branch = f"jules-{sender_nick.lower()}-{label}"
            # Ensure branch exists on remote
            if not check_remote_branch(repo, branch):
                ok, res = create_remote_branch(repo, branch)
                if not ok:
                    self.minibot.send_msg(target, f"[Jules] Branch provisioning failed: {res}")
                    return

            sess = self.client.create_session(prompt, repo, branch, title=label)
            if sess and "name" in sess:
                session_id = sess["name"].split("/")[-1]
                self.active_sessions[label] = session_id
                if self.poller:
                    self.poller.track_session(session_id, label, target)
                self.minibot.send_msg(target, f"[Jules] 🚀 Initialized {label} on {repo}:{branch}. Generating plan...")
            else:
                self.minibot.send_msg(target, f"[Jules] Session creation failed.")

        elif action == "approve":
            label = parts[1] if len(parts) > 1 else ""
            session_id = self.active_sessions.get(label)
            if not session_id:
                self.minibot.send_msg(target, f"[Jules] No active session found for {label}")
                return
            res = self.client.approve_plan(session_id)
            if res:
                self.minibot.send_msg(target, f"[Jules] ✅ Plan approved for {label}. Execution started.")
            else:
                self.minibot.send_msg(target, f"[Jules] Plan approval failed.")

        elif action == "status":
            label = parts[1] if len(parts) > 1 else ""
            session_id = self.active_sessions.get(label)
            if not session_id:
                self.minibot.send_msg(target, f"[Jules] No active session found for {label}")
                return
            sess = self.client.get_session(session_id)
            if sess:
                state = sess.get("state", "UNKNOWN")
                self.minibot.send_msg(target, f"[Jules:{label}] State: {state}")
            else:
                self.minibot.send_msg(target, f"[Jules] Could not fetch status for {label}")

        elif action == "cancel":
            label = parts[1] if len(parts) > 1 else ""
            session_id = self.active_sessions.pop(label, None)
            if session_id:
                self.minibot.send_msg(target, f"[Jules] 🛑 Cancelled session {label}")
            else:
                self.minibot.send_msg(target, f"[Jules] No active session found for {label}")
