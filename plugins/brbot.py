# SPDX-License-Identifier: AGPL-3.0-or-later
"""
brbot.py — Quiet Matrix-to-IRC Bridge Relay Plugin.
"""

import logging
from typing import Any, Optional

logger = logging.getLogger("BridgeRelay")


class BridgePlugin:
    """Relays messages between Matrix room and IRC channel when enabled."""
    def __init__(self, minibot: Any, matrix_room_id: Optional[str] = None):
        self.minibot = minibot
        self.matrix_room_id = matrix_room_id

    def on_privmsg(self, target: str, sender_nick: str, message: str) -> None:
        # Ignore bot commands to avoid command echoes
        if message.startswith("!") or message.startswith("."):
            return
        if not self.matrix_room_id:
            return
        # Relay logic hook for matrix bridge client
        pass
