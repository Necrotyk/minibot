#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

"""
conn_daemon.py — Multi-Network TCP/SSL Socket Broker & Zero-Disconnect Multiplexer.
Manages raw IRC connections for primary bots and all spawned minibots with zero socket drops on logic reloads.
"""

import asyncio
import json
import logging
import os
import random
import signal
import socket
import ssl
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Path resolution
BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

import keystore
from core.config import ConfigManager
from core.guard import TokenBucket
from core.logger import setup_logger

logger = setup_logger("ConnDaemon", logging.INFO)

SOCKET_IPC_PATH = os.environ.get("MINIBOTS_IPC_SOCK", str(BASE_DIR / "data" / "minibots_ipc.sock"))


class IRCSocketConnection:
    """Manages an individual raw TCP/SSL connection to an IRC network."""
    def __init__(
        self,
        network_name: str,
        host: str,
        port: int,
        nick: str,
        channels: List[str],
        use_ssl: bool = False,
        nickserv_password: Optional[str] = None,
        on_line_received: Optional[Any] = None,
        on_nick_updated: Optional[Any] = None,
        rate_limiter: Optional[TokenBucket] = None,
        trusted: bool = False,
        tls_verify: bool = True
    ):
        self.network = network_name
        self.host = host
        self.port = port
        self.desired_nick = nick
        self.current_nick = nick
        self.channels = channels.copy()
        self.use_ssl = use_ssl
        self.nickserv_password = nickserv_password
        self.on_line_received = on_line_received
        self.on_nick_updated = on_nick_updated
        self.rate_limiter = rate_limiter or TokenBucket(capacity=4.0, refill_rate=0.5)
        self.trusted = trusted
        self.tls_verify = tls_verify

        self.reader: Optional[asyncio.StreamReader] = None
        self.writer: Optional[asyncio.StreamWriter] = None
        self.is_connected = False
        self.is_registered = False
        self._running = True
        self._send_queue: asyncio.Queue[str] = asyncio.Queue()
        self._tasks: List[asyncio.Task] = []
        self._fail_count = 0

    async def connect_and_run(self) -> None:
        """Connects to the IRC server and runs send/receive loops with exponential backoff."""
        while self._running:
            try:
                logger.info(f"[{self.network}:{self.current_nick}] Connecting to {self.host}:{self.port} (SSL: {self.use_ssl})...")
                ssl_ctx = ssl.create_default_context() if self.use_ssl else None
                if ssl_ctx and not self.tls_verify:
                    ssl_ctx.check_hostname = False
                    ssl_ctx.verify_mode = ssl.CERT_NONE

                self.reader, self.writer = await asyncio.open_connection(
                    self.host, self.port, ssl=ssl_ctx
                )
                self.is_connected = True
                self._fail_count = 0
                logger.info(f"[{self.network}:{self.current_nick}] TCP connected to {self.host}:{self.port}")

                # Start registration
                await self.send_raw(f"NICK {self.current_nick}")
                await self.send_raw(f"USER {self.current_nick} 0 * :Minibots Daemon")

                self._tasks = [
                    asyncio.create_task(self._recv_loop()),
                    asyncio.create_task(self._send_loop())
                ]
                await asyncio.gather(*self._tasks)

            except (asyncio.CancelledError, KeyboardInterrupt):
                break
            except Exception as e:
                self._fail_count += 1
                backoff = min(30.0, 1.0 * (2 ** min(self._fail_count, 5))) + random.uniform(0.0, 2.0)
                logger.error(f"[{self.network}:{self.current_nick}] Connection error: {e}. Reconnecting in {backoff:.1f}s (fail #{self._fail_count})...")
                self.is_connected = False
                self.is_registered = False
                await asyncio.sleep(backoff)

    async def _send_loop(self) -> None:
        """Throttled send queue processor."""
        while self.is_connected and self._running:
            line = await self._send_queue.get()
            if not self.writer or self.writer.is_closing():
                break

            # Rate limiting for non-trusted networks
            if not self.trusted:
                wait = self.rate_limiter.wait_time(1.0)
                if wait > 0:
                    await asyncio.sleep(wait)
                self.rate_limiter.consume(1.0)
            else:
                # Fast burst on trusted networks
                await asyncio.sleep(0.01)

            try:
                self.writer.write(f"{line}\r\n".encode("utf-8"))
                await self.writer.drain()
            except Exception as e:
                logger.error(f"[{self.network}:{self.current_nick}] Send failure: {e}")
                break

    async def _recv_loop(self) -> None:
        """Parses raw lines from IRC socket."""
        while self.is_connected and self._running and self.reader:
            try:
                raw_bytes = await self.reader.readline()
                if not raw_bytes:
                    break
                line = raw_bytes.decode("utf-8", errors="replace").strip()
                if not line:
                    continue

                await self._handle_irc_line(line)
            except Exception as e:
                logger.error(f"[{self.network}:{self.current_nick}] Recv error: {e}")
                break

    async def _handle_irc_line(self, line: str) -> None:
        """Handles basic PING, registration (001), nick collision (433), and passes to logic IPC."""
        if line.startswith("PING "):
            token = line.split(" ", 1)[1]
            await self.send_raw(f"PONG {token}")
            return

        parts = line.split()
        if len(parts) >= 2:
            cmd = parts[1]

            # 001 RPL_WELCOME
            if cmd == "001":
                self.is_registered = True
                self.current_nick = parts[2]
                logger.info(f"[{self.network}:{self.current_nick}] Registered successfully as {self.current_nick}")

                # Identify if password configured
                if self.nickserv_password:
                    await self.send_raw(f"PRIVMSG NickServ :IDENTIFY {self.nickserv_password}")

                # Join configured channels
                for ch in self.channels:
                    await self.send_raw(f"JOIN {ch}")

            # 433 ERR_NICKNAMEINUSE
            elif cmd == "433":
                taken_nick = parts[2] if len(parts) > 2 else self.current_nick
                mutated = f"{taken_nick}_"
                old_nick = self.current_nick
                logger.warning(f"[{self.network}] Nick '{taken_nick}' in use. Mutating to '{mutated}'")
                self.current_nick = mutated
                await self.send_raw(f"NICK {mutated}")
                if self.on_nick_updated:
                    await self.on_nick_updated(self.network, old_nick, mutated)

        # Pass event to logic daemon
        if self.on_line_received:
            await self.on_line_received(self.network, self.current_nick, line)

    async def send_raw(self, line: str) -> None:
        """Enqueues raw line for delivery."""
        await self._send_queue.put(line)

    async def close(self, reason: str = "Dystopi Minibots Daemon shutting down") -> None:
        """Gracefully closes socket."""
        self._running = False
        self.is_connected = False
        for t in self._tasks:
            t.cancel()
        if self.writer:
            try:
                self.writer.write(f"QUIT :{reason}\r\n".encode("utf-8"))
                await asyncio.wait_for(self.writer.drain(), timeout=1.0)
            except Exception:
                pass
            try:
                self.writer.close()
                await asyncio.wait_for(self.writer.wait_closed(), timeout=1.0)
            except Exception:
                pass


class ConnectionBroker:
    """Coordinates multi-network IRC connections and UNIX IPC domain socket."""
    def __init__(self, config: ConfigManager):
        self.config = config
        self.connections: Dict[str, IRCSocketConnection] = {}
        self.ipc_clients: List[asyncio.StreamWriter] = []
        self.ipc_server: Optional[asyncio.Server] = None
        self._running = True

    async def start(self) -> None:
        """Initializes IRC socket connections and starts UNIX domain IPC socket."""
        # 1. Start IPC domain socket server
        sock_path = Path(SOCKET_IPC_PATH)
        sock_path.parent.mkdir(parents=True, exist_ok=True)
        if sock_path.exists():
            sock_path.unlink()

        # Set umask for 0660 socket permissions
        old_umask = os.umask(0o117)
        try:
            self.ipc_server = await asyncio.start_unix_server(
                self._handle_ipc_client, path=str(sock_path)
            )
            sock_path.chmod(0o660)
            logger.info(f"UNIX IPC server listening at {sock_path} (mode 0660)")
        finally:
            os.umask(old_umask)

        # 2. Spin up default IRC socket connections
        servers = self.config.servers
        for net_name, srv in servers.items():
            is_trusted = srv.get("trusted", False) or net_name.startswith("wireguard") or net_name.startswith("local")
            tls_verify = srv.get("tls_verify", True)
            rate_limiter = TokenBucket(capacity=20.0, refill_rate=10.0) if is_trusted else TokenBucket(capacity=4.0, refill_rate=0.5)
            conn = IRCSocketConnection(
                network_name=net_name,
                host=srv.get("host", "127.0.0.1"),
                port=srv.get("port", 6667),
                nick=srv.get("nick", "minibots"),
                channels=srv.get("channels", ["#minibots"]),
                use_ssl=srv.get("ssl", False),
                nickserv_password=srv.get("nickserv_password"),
                on_line_received=self._broadcast_to_ipc,
                on_nick_updated=self._broadcast_nick_update,
                rate_limiter=rate_limiter,
                trusted=is_trusted,
                tls_verify=tls_verify
            )
            key = f"{net_name}:{srv.get('nick', 'minibots').lower()}"
            self.connections[key] = conn
            self.connections[net_name] = conn  # Fallback default
            asyncio.create_task(conn.connect_and_run())

    async def _handle_ipc_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Handles incoming commands and subscriptions from logic daemon (main.py)."""
        logger.info("Logic daemon connected to IPC socket.")
        self.ipc_clients.append(writer)

        try:
            while self._running:
                data = await reader.readline()
                if not data:
                    break
                try:
                    payload = json.loads(data.decode("utf-8").strip())
                    await self._process_ipc_payload(payload)
                except Exception as e:
                    logger.error(f"Failed to process IPC payload: {e}")
        except (asyncio.CancelledError, ConnectionResetError):
            pass
        finally:
            if writer in self.ipc_clients:
                self.ipc_clients.remove(writer)
            logger.info("Logic daemon disconnected from IPC socket.")

    async def _process_ipc_payload(self, payload: Dict[str, Any]) -> None:
        """Processes outgoing instructions from logic daemon."""
        msg_type = payload.get("type")
        default_net = getattr(self.config, "primary_network", "local")
        network = payload.get("network", default_net)
        target_nick = payload.get("nick", "")

        if msg_type == "raw":
            raw_line = payload.get("line", "")
            key = f"{network}:{target_nick.lower()}" if target_nick else network
            conn = self.connections.get(key) or self.connections.get(network)
            if conn:
                await conn.send_raw(raw_line)
            else:
                logger.warning(f"No active connection found for {key}")

        elif msg_type == "spawn_conn":
            bot_nick = payload.get("nick")
            channels = payload.get("channels", ["#minibots"])
            if not bot_nick:
                return

            key = f"{network}:{bot_nick.lower()}"
            if key not in self.connections:
                srv = self.config.servers.get(network, {})
                is_trusted = srv.get("trusted", False) or network.startswith("wireguard") or network.startswith("local")
                tls_verify = srv.get("tls_verify", True)
                rate_limiter = TokenBucket(capacity=20.0, refill_rate=10.0) if is_trusted else TokenBucket(capacity=4.0, refill_rate=0.5)
                conn = IRCSocketConnection(
                    network_name=network,
                    host=srv.get("host", "127.0.0.1"),
                    port=srv.get("port", 6667),
                    nick=bot_nick,
                    channels=channels,
                    use_ssl=srv.get("ssl", False),
                    nickserv_password=srv.get("nickserv_password"),
                    on_line_received=self._broadcast_to_ipc,
                    on_nick_updated=self._broadcast_nick_update,
                    rate_limiter=rate_limiter,
                    trusted=is_trusted,
                    tls_verify=tls_verify
                )
                self.connections[key] = conn
                logger.info(f"Spawning discrete socket connection for {key} in {channels}")
                asyncio.create_task(conn.connect_and_run())

        elif msg_type == "kill_conn":
            bot_nick = payload.get("nick")
            creator = payload.get("creator", "manager")
            if not bot_nick:
                return

            key = f"{network}:{bot_nick.lower()}"
            conn = self.connections.pop(key, None)
            if conn:
                logger.info(f"Terminating discrete socket connection for {key}")
                await conn.close(f"Minibot terminated by {creator}")

        elif msg_type == "add_server":
            net_name = payload.get("network")
            host = payload.get("host")
            port = payload.get("port", 6667)
            use_ssl = payload.get("ssl", False)
            if net_name and host:
                self.config.servers[net_name] = {
                    "host": host,
                    "port": port,
                    "ssl": use_ssl
                }
                logger.info(f"Dynamically registered IRC network '{net_name}': {host}:{port} (ssl={use_ssl})")

    async def _broadcast_to_ipc(self, network: str, bot_nick: str, line: str) -> None:
        """Broadcasts received IRC line to connected logic daemon clients."""
        payload = json.dumps({"type": "irc_line", "network": network, "bot_nick": bot_nick, "line": line}) + "\n"
        data = payload.encode("utf-8")
        for client in list(self.ipc_clients):
            try:
                client.write(data)
                await client.drain()
            except Exception:
                if client in self.ipc_clients:
                    self.ipc_clients.remove(client)

    async def _broadcast_nick_update(self, network: str, old_nick: str, new_nick: str) -> None:
        """Broadcasts nick mutation event to logic daemon clients."""
        payload = json.dumps({"type": "nick_update", "network": network, "old_nick": old_nick, "new_nick": new_nick}) + "\n"
        data = payload.encode("utf-8")
        for client in list(self.ipc_clients):
            try:
                client.write(data)
                await client.drain()
            except Exception:
                if client in self.ipc_clients:
                    self.ipc_clients.remove(client)

    async def shutdown(self) -> None:
        """Gracefully tears down all IRC connections and closes IPC socket."""
        self._running = False
        logger.info("Shutting down connection daemon...")
        for conn in list(self.connections.values()):
            await conn.close()
        if self.ipc_server:
            self.ipc_server.close()
            await self.ipc_server.wait_closed()
        sock_path = Path(SOCKET_IPC_PATH)
        if sock_path.exists():
            sock_path.unlink()
        logger.info("Connection daemon stopped cleanly.")


async def main():
    config = ConfigManager()
    broker = ConnectionBroker(config)

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _sig_handler():
        logger.info("Received SIGINT/SIGTERM. Initiating shutdown...")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _sig_handler)

    await broker.start()
    await stop_event.wait()
    await broker.shutdown()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
