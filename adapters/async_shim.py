# SPDX-License-Identifier: AGPL-3.0-or-later
"""
async_shim.py — Native Async Framework Interface for Dystopi Minibots.
"""

from typing import Any, Dict, Optional

class AsyncPlugin:
    """Native async plugin interface with event lifecycle hooks."""
    def __init__(self, bot: Any, db_path: Optional[str] = None):
        self.bot = bot
        self.db_path = db_path

    async def on_load(self) -> None:
        pass

    async def on_unload(self) -> None:
        pass

    async def handle_privmsg(self, nick: str, host: str, channel: str, message: str) -> None:
        pass

    async def handle_join(self, nick: str, channel: str) -> None:
        pass

    async def handle_tick(self) -> None:
        pass
