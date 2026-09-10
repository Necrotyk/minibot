#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
run_bot.py — Standalone CLI Launcher for Dystopi Minibots.
Spawns and runs a standalone bot directly in terminal/tmux with zero background daemons required.

Usage:
    python3 run_bot.py <nick> <network> <#channel> [mod1,mod2,...]

Examples:
    python3 run_bot.py infobot libera #lobby sideload_infobot
    python3 run_bot.py researchbot local #lobby sideload_infobot,sideload_web_search
"""

import asyncio
import logging
import re
import signal
import ssl
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

from adapters.script_adapter import ScriptAdapter
from adapters.sopel_shim import inject_sopel_shim
inject_sopel_shim()

from core.config import ConfigManager
from core.guard import TokenBucket
from core.logger import setup_logger
from core.sideload import SideloadEngine
from minibots.base import MiniBot

logger = setup_logger("RunBot", logging.INFO)


class StandaloneRunner:
    def __init__(self, nick: str, network: str, channel: str, modules: list):
        self.nick = nick
        self.network = network.lower()
        self.channel = channel if channel.startswith("#") else f"#{channel}"
        self.module_names = [m.strip() for m in modules if m.strip()]
        if not self.module_names:
            self.module_names = ["sideload_infobot"]

        self.cfg_mgr = ConfigManager()
        self.servers = self.cfg_mgr.servers
        srv = self.servers.get(self.network, {})

        self.host = srv.get("host", "127.0.0.1")
        self.port = srv.get("port", 6697 if srv.get("ssl", False) else 6667)
        self.use_ssl = srv.get("ssl", False)

        self.sideload_engine = SideloadEngine()
        self.bot = MiniBot(
            nick=self.nick,
            network=self.network,
            channels=[self.channel],
            raw_send_callback=self._raw_send
        )

        self._send_queue: asyncio.Queue = asyncio.Queue()
        self._writer = None
        self._running = True

        # Attach modules
        self._attach_modules()

    def _raw_send(self, bot_nick: str, line: str) -> None:
        asyncio.create_task(self._send_queue.put(line))

    def _attach_modules(self) -> None:
        for mod_name in self.module_names:
            script_path = mod_name.replace("sideload:", "").replace("sideload_", "")
            if not script_path.endswith(".py"):
                script_path = f"{script_path}.py"
            mod_obj = self.sideload_engine.load_script(script_path)
            if mod_obj:
                adapter = ScriptAdapter(mod_obj, self.bot)
                self.bot.enable_module(f"sideload_{Path(script_path).stem}", adapter)
                logger.info(f"Attached module '{mod_name}' to {self.nick}")
            else:
                logger.error(f"Failed to load module '{mod_name}'")

    async def _send_loop(self):
        while self._running:
            line = await self._send_queue.get()
            if not line:
                break
            clean = line.strip("\r\n")
            logger.info(f"[{self.nick} OUT] {clean}")
            if self._writer and not self._writer.is_closing():
                self._writer.write(f"{clean}\r\n".encode("utf-8"))
                await self._writer.drain()
            await asyncio.sleep(0.3)  # Anti-flood rate limit

    async def _recv_loop(self, reader: asyncio.StreamReader):
        registered = False
        while self._running:
            line_bytes = await reader.readline()
            if not line_bytes:
                logger.warning("Connection closed by remote server.")
                break
            raw_line = line_bytes.decode("utf-8", errors="ignore").strip("\r\n")

            if raw_line.startswith("PING"):
                token = raw_line.split()[1]
                await self._send_queue.put(f"PONG {token}")
                continue

            parts = raw_line.split()
            if len(parts) > 1:
                cmd = parts[1]
                # Registered
                if cmd in ("001", "376", "422") and not registered:
                    registered = True
                    logger.info(f"Registered with {self.network}. Joining {self.channel}...")
                    await self._send_queue.put(f"JOIN {self.channel}")

                # Incoming PRIVMSG
                elif cmd == "PRIVMSG" and len(parts) >= 4:
                    prefix = parts[0].lstrip(":")
                    sender_nick = prefix.split("!")[0]
                    target = parts[2]
                    # Join remainder for message text
                    m_idx = raw_line.find(" :", raw_line.find("PRIVMSG"))
                    if m_idx != -1:
                        msg_text = raw_line[m_idx + 2:]
                    else:
                        msg_text = " ".join(parts[3:]).lstrip(":")

                    # Route through MiniBot event loop
                    self.bot.handle_privmsg(target, sender_nick, msg_text)

    async def run(self):
        ssl_ctx = ssl.create_default_context() if self.use_ssl else None
        if ssl_ctx:
            ssl_ctx.check_hostname = False
            ssl_ctx.verify_mode = ssl.CERT_NONE

        logger.info(f"Connecting to {self.host}:{self.port} (SSL: {self.use_ssl})...")
        reader, writer = await asyncio.open_connection(self.host, self.port, ssl=ssl_ctx)
        self._writer = writer
        logger.info(f"Connected to {self.host}! Registering as {self.nick}...")

        # Handshake
        await self._send_queue.put(f"NICK {self.nick}")
        await self._send_queue.put(f"USER {self.nick} 0 * :Dystopi Minibot")

        send_task = asyncio.create_task(self._send_loop())
        recv_task = asyncio.create_task(self._recv_loop(reader))

        try:
            await asyncio.gather(send_task, recv_task)
        except asyncio.CancelledError:
            pass
        finally:
            if self._writer:
                try:
                    self._writer.write(b"QUIT :Terminated by operator\r\n")
                    await self._writer.drain()
                    self._writer.close()
                except Exception:
                    pass


def main():
    if len(sys.argv) < 4:
        print("Usage: python3 run_bot.py <nick> <network> <#channel> [module1,module2,...]")
        print("Example: python3 run_bot.py infobot libera #lobby sideload_infobot")
        sys.exit(1)

    nick = sys.argv[1]
    network = sys.argv[2]
    channel = sys.argv[3]
    modules = sys.argv[4].split(",") if len(sys.argv) > 4 else ["sideload_infobot"]

    runner = StandaloneRunner(nick, network, channel, modules)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def _sig_handler():
        logger.info("Received termination signal, shutting down...")
        runner._running = False
        for t in asyncio.all_tasks(loop):
            t.cancel()

    for s in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(s, _sig_handler)

    try:
        loop.run_until_complete(runner.run())
    finally:
        loop.close()


if __name__ == "__main__":
    main()
