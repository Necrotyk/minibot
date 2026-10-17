#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

"""
run_bot.py — Zero-Dependency Resilient Standalone Runner for Minibots.
Spawns and runs a standalone bot directly in terminal/tmux/background with zero daemons required.
Supports auto-reconnect, keepalive, multi-channel, NickServ auth, 433 nickname collision
recovery, token-bucket rate limiting, and live mtime / SIGHUP / !reload hot-reloading.

Usage:
    python3 run_bot.py [--nick NICK] [--network NETWORK] [--channel #CHAN1,#CHAN2] [--modules MODS]
    python3 run_bot.py [nick] [network] [#channel] [mod1,mod2,...]

Examples:
    python3 run_bot.py
    python3 run_bot.py --network libera --channel '#lobby,#bots' --nick mybot
    python3 run_bot.py mybot local '#lobby' sideload_infobot,sideload_web_search
"""

import argparse
import asyncio
import atexit
import configparser
import fnmatch
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import random
import re
import shutil
import signal
import ssl
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

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

DEFAULT_MODULES = [
    "sideload_infobot",
    "sideload_weather_time",
    "sideload_web_search",
    "sideload_crypto_calc",
    "sideload_sysadmin_tools"
]


def persist_channel_change(config_path: Optional[str], network: str, channel: str, action: str) -> bool:
    """
    Persists adding or removing an IRC channel to config.json.
    action: 'add' or 'remove'
    """
    cfg_file = Path(config_path) if config_path else (BASE_DIR / "config.json")
    if not cfg_file.exists():
        return False

    try:
        with open(cfg_file, "r", encoding="utf-8") as f:
            data = json.load(f)

        clean_chan = channel if channel.startswith("#") else f"#{channel}"
        servers = data.get("SERVERS", {})
        srv = servers.get(network)
        if not srv and servers:
            srv = next(iter(servers.values()))

        if srv is not None:
            chans = srv.get("channels", [])
            if action == "add":
                if clean_chan not in chans:
                    chans.append(clean_chan)
            elif action == "remove":
                chans = [c for c in chans if c.lower() != clean_chan.lower()]
            srv["channels"] = chans

        admin_chans = data.get("ADMIN_CHANNELS", [])
        if action == "add":
            if clean_chan not in admin_chans:
                admin_chans.append(clean_chan)
        elif action == "remove":
            admin_chans = [c for c in admin_chans if c.lower() != clean_chan.lower()]
        data["ADMIN_CHANNELS"] = admin_chans

        tmp_file = cfg_file.with_suffix(".tmp")
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        tmp_file.replace(cfg_file)
        os.chmod(cfg_file, 0o600)
        return True
    except Exception as e:
        logger.error(f"Failed to persist channel change to {cfg_file}: {e}")
        return False


def import_sopel_config(
    sopel_cfg_path: Optional[str] = None,
    out_config_json: Optional[str] = None,
    sideload_dir: Optional[str] = None
) -> Dict[str, Any]:
    """
    Imports configuration from an existing Sopel installation (default.cfg)
    and converts it into Minibots config.json format using only the Python standard library.
    Optionally copies custom .py plugins from Sopel plugins/modules directory to sideload/.
    """
    candidates = [
        sopel_cfg_path,
        os.path.expanduser("~/.sopel/default.cfg"),
        os.path.expanduser("~/.config/sopel/default.cfg"),
    ]
    cfg_file = None
    for cand in candidates:
        if cand and os.path.isfile(cand):
            cfg_file = cand
            break

    if not cfg_file:
        raise FileNotFoundError(f"No Sopel configuration found. Checked: {[c for c in candidates if c]}")

    cp = configparser.ConfigParser(interpolation=None)
    cp.read(cfg_file, encoding="utf-8")

    core = cp["core"] if "core" in cp else {}

    host = core.get("host", "irc.libera.chat")
    raw_port = core.get("port", "6697")
    try:
        port = int(raw_port)
    except Exception:
        port = 6697

    def _bool(val: Any, default: bool = True) -> bool:
        if val is None:
            return default
        s = str(val).strip().lower()
        return s in ("true", "1", "yes", "on")

    use_ssl = _bool(core.get("use_ssl"), default=True)
    verify_ssl = _bool(core.get("verify_ssl"), default=True)

    nick = core.get("nick", "minibot").strip()
    owner = core.get("owner", "").strip()

    raw_channels = core.get("channels", "")
    chan_tokens = re.split(r'[,\s\n\r]+', raw_channels)
    channels: List[str] = []
    for ct in chan_tokens:
        ct = ct.strip()
        if ct:
            clean = ct if ct.startswith("#") else f"#{ct}"
            if clean not in channels:
                channels.append(clean)
    if not channels:
        channels = ["#minibot"]

    auth_pass = core.get("auth_password") or core.get("nickserv_password") or ""
    admin_nicks = [f"{owner}*"] if owner else ["admin*"]

    config_data = {
        "PRIMARY_NETWORK": "primary",
        "ADMIN_NETWORKS": ["primary"],
        "ADMIN_CHANNELS": channels,
        "ADMIN_NICKS": admin_nicks,
        "SERVERS": {
            "primary": {
                "host": host,
                "port": port,
                "nick": nick,
                "channels": channels,
                "ssl": use_ssl,
                "trusted": True,
                "tls_verify": verify_ssl,
                "nickserv_password": auth_pass
            }
        }
    }

    out_path = Path(out_config_json) if out_config_json else (BASE_DIR / "config.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(config_data, f, indent=2)
    os.chmod(out_path, 0o600)

    target_sideload = Path(sideload_dir) if sideload_dir else (BASE_DIR / "sideload")
    target_sideload.mkdir(parents=True, exist_ok=True)

    sopel_dir = Path(cfg_file).parent
    plugin_dirs = [
        sopel_dir / "plugins",
        sopel_dir / "modules",
        Path.home() / ".sopel" / "plugins",
        Path.home() / ".sopel" / "modules",
        Path.home() / ".config" / "sopel" / "plugins",
    ]
    copied = []
    for pdir in plugin_dirs:
        if pdir.is_dir():
            for py_file in pdir.glob("*.py"):
                dest = target_sideload / py_file.name
                if not dest.exists():
                    shutil.copy2(py_file, dest)
                    copied.append(py_file.name)

    return {"config": config_data, "file": cfg_file, "copied_plugins": copied}


class StandaloneRunner:
    """Production-grade single-process IRC bot runner with zero external daemon requirements."""

    def __init__(
        self,
        nick: str,
        network: str,
        channels: Union[str, List[str]],
        modules: List[str],
        host: Optional[str] = None,
        port: Optional[int] = None,
        use_ssl: Optional[bool] = None,
        tls_verify: Optional[bool] = None,
        nickserv_password: Optional[str] = None,
        config_path: Optional[str] = None,
    ):
        self.initial_nick = nick
        self.current_nick = nick
        self.nick = nick
        self.network = (network or "local").lower()

        # Parse comma-separated or list channels
        if isinstance(channels, str):
            chan_list = [c.strip() for c in channels.split(",") if c.strip()]
        else:
            chan_list = list(channels) if channels else []
        self.channels = [c if c.startswith("#") else f"#{c}" for c in chan_list]
        if not self.channels:
            self.channels = ["#minibot"]
        self.channel = self.channels[0]

        # Parse modules
        self.module_names = [m.strip() for m in modules if m.strip()]
        if not self.module_names:
            self.module_names = list(DEFAULT_MODULES)

        # Config lookup
        self.config_path = config_path
        self.cfg_mgr = ConfigManager(config_path) if config_path else ConfigManager()
        srv = self.cfg_mgr.servers.get(self.network, {})

        self.host = host or srv.get("host", "127.0.0.1")
        default_ssl = srv.get("ssl", False)
        self.use_ssl = use_ssl if use_ssl is not None else default_ssl
        default_port = 6697 if self.use_ssl else 6667
        self.port = port or srv.get("port", default_port)
        self.tls_verify = tls_verify if tls_verify is not None else srv.get("tls_verify", True)

        self.nickserv_password = (
            nickserv_password
            or srv.get("nickserv_password")
            or os.environ.get("MINIBOT_NICKSERV_PASSWORD")
            or os.environ.get("NICKSERV_PASSWORD")
        )

        is_trusted = self.cfg_mgr.is_trusted_network(self.network)
        self.rate_limiter = TokenBucket(capacity=20.0, refill_rate=10.0) if is_trusted else TokenBucket(capacity=4.0, refill_rate=0.5)

        self.sideload_engine = SideloadEngine()
        creator = self.cfg_mgr.admin_nicks[0] if self.cfg_mgr.admin_nicks else "admin"
        self.bot = MiniBot(
            nick=self.current_nick,
            network=self.network,
            channels=self.channels,
            creator_nick=creator,
            raw_send_callback=self._raw_send
        )
        self.bot.config = self.cfg_mgr

        self._send_queue: asyncio.Queue = asyncio.Queue()
        self._writer: Optional[asyncio.StreamWriter] = None
        self._running = True
        self._last_data_time = time.time()
        self._start_time = time.time()
        self._reclaim_needed = False
        self._attached_adapters: Dict[str, ScriptAdapter] = {}

        # Attach modules
        self._attach_modules()

    def _raw_send(self, bot_nick: str, line: str) -> None:
        asyncio.create_task(self._send_queue.put(line))

    def _attach_modules(self) -> None:
        """Loads and enables all configured sideload plugins."""
        for mod_name in self.module_names:
            script_path = mod_name.replace("sideload:", "").replace("sideload_", "")
            if not script_path.endswith(".py"):
                script_path = f"{script_path}.py"
            mod_obj = self.sideload_engine.load_script(script_path)
            if mod_obj:
                adapter = ScriptAdapter(mod_obj, self.bot)
                mod_key = f"sideload_{Path(script_path).stem}"
                self.bot.enable_module(mod_key, adapter)
                self._attached_adapters[mod_key] = adapter
                logger.info(f"Attached module '{mod_name}' to {self.current_nick}")
            else:
                logger.error(f"Failed to load module '{mod_name}'")

    def _reload_modules(self) -> None:
        """In-process hot-reload of all attached sideload plugins without dropping IRC socket."""
        logger.info(f"Hot-reloading modules for {self.current_nick}...")
        self.sideload_engine.loaded_modules.clear()
        self._attach_modules()

    def _is_admin(self, nick: str) -> bool:
        if not nick:
            return False
        clean = nick.lower().lstrip("@+~&%")
        creator = getattr(self.bot, "creator_nick", "")
        if creator and clean == creator.lower():
            return True
        for pat in self.cfg_mgr.admin_nicks:
            if fnmatch.fnmatch(clean, pat.lower()):
                return True
        return False

    async def _send_loop(self):
        while self._running:
            line = await self._send_queue.get()
            if not line:
                break
            clean = line.strip("\r\n")

            # TokenBucket rate limiting
            wait = self.rate_limiter.wait_time(1.0)
            if wait > 0:
                await asyncio.sleep(wait)
            self.rate_limiter.consume(1.0)

            logger.info(f"[{self.current_nick} OUT] {clean}")
            if self._writer and not self._writer.is_closing():
                try:
                    self._writer.write(f"{clean}\r\n".encode("utf-8"))
                    await self._writer.drain()
                except Exception as e:
                    logger.warning(f"Error writing to socket: {e}")
                    break

    async def _recv_loop(self, reader: asyncio.StreamReader):
        registered = False
        while self._running:
            line_bytes = await reader.readline()
            if not line_bytes:
                logger.warning("Connection closed by remote server.")
                break
            self._last_data_time = time.time()
            raw_line = line_bytes.decode("utf-8", errors="ignore").strip("\r\n")

            # PING / PONG keepalive
            if raw_line.startswith("PING"):
                token = raw_line.split(" ", 1)[1] if " " in raw_line else ""
                await self._send_queue.put(f"PONG {token}")
                continue

            parts = raw_line.split()
            if len(parts) > 1:
                cmd = parts[1]

                # 433 ERR_NICKNAMEINUSE
                if cmd == "433":
                    self.current_nick = f"{self.current_nick}_"
                    self.bot.nick = self.current_nick
                    self._reclaim_needed = True
                    logger.warning(f"Nickname collision: retrying registration as {self.current_nick}...")
                    await self._send_queue.put(f"NICK {self.current_nick}")
                    continue

                # NICK change tracking
                if cmd == "NICK":
                    prefix = parts[0].lstrip(":")
                    sender_nick = prefix.split("!")[0]
                    if sender_nick.lower() == self.current_nick.lower():
                        new_nick = parts[2].lstrip(":") if len(parts) > 2 else parts[-1].lstrip(":")
                        logger.info(f"Nickname changed from {self.current_nick} to {new_nick}")
                        self.current_nick = new_nick
                        self.bot.nick = new_nick
                        if new_nick.lower() == self.initial_nick.lower():
                            self._reclaim_needed = False
                    continue

                # Registered on 001, 376, 422
                if cmd in ("001", "376", "422") and not registered:
                    registered = True
                    logger.info(f"Registered with {self.network} as {self.current_nick}!")

                    if self._reclaim_needed and self.nickserv_password:
                        logger.info(f"Reclaiming primary nickname {self.initial_nick} via NickServ GHOST...")
                        await self._send_queue.put(f"PRIVMSG NickServ :GHOST {self.initial_nick} {self.nickserv_password}")
                        await asyncio.sleep(1.5)
                        await self._send_queue.put(f"NICK {self.initial_nick}")
                        await asyncio.sleep(1.0)
                        await self._send_queue.put(f"PRIVMSG NickServ :IDENTIFY {self.nickserv_password}")
                        await asyncio.sleep(1.0)
                    elif self.nickserv_password:
                        logger.info("Authenticating with NickServ...")
                        await self._send_queue.put(f"PRIVMSG NickServ :IDENTIFY {self.nickserv_password}")
                        await asyncio.sleep(1.0)

                    for chan in self.channels:
                        logger.info(f"Joining {chan}...")
                        await self._send_queue.put(f"JOIN {chan}")

                # Incoming PRIVMSG
                elif cmd == "PRIVMSG" and len(parts) >= 4:
                    prefix = parts[0].lstrip(":")
                    sender_nick = prefix.split("!")[0]
                    target = parts[2]
                    m_idx = raw_line.find(" :", raw_line.find("PRIVMSG"))
                    if m_idx != -1:
                        msg_text = raw_line[m_idx + 2:]
                    else:
                        msg_text = " ".join(parts[3:]).lstrip(":")

                    clean_msg = msg_text.strip()
                    parts_msg = clean_msg.split()
                    cmd_name = parts_msg[0].lower() if parts_msg else ""

                    # Zero-SSH In-Chat Admin Commands
                    if self._is_admin(sender_nick):
                        reply_target = sender_nick if not target.startswith("#") else target

                        if cmd_name == "!join" and len(parts_msg) > 1:
                            chan_to_join = parts_msg[1]
                            if not chan_to_join.startswith("#"):
                                chan_to_join = f"#{chan_to_join}"
                            await self._send_queue.put(f"JOIN {chan_to_join}")
                            if chan_to_join not in self.channels:
                                self.channels.append(chan_to_join)
                            self.bot.channels = self.channels
                            persist_channel_change(self.config_path, self.network, chan_to_join, "add")
                            self.bot.send_msg(reply_target, f"✅ Joined and persisted {chan_to_join}")
                            continue

                        elif cmd_name == "!part":
                            chan_to_part = parts_msg[1] if len(parts_msg) > 1 else (target if target.startswith("#") else "")
                            if chan_to_part:
                                if not chan_to_part.startswith("#"):
                                    chan_to_part = f"#{chan_to_part}"
                                await self._send_queue.put(f"PART {chan_to_part} :Parted by admin")
                                self.channels = [c for c in self.channels if c.lower() != chan_to_part.lower()]
                                self.bot.channels = self.channels
                                persist_channel_change(self.config_path, self.network, chan_to_part, "remove")
                                self.bot.send_msg(reply_target, f"✅ Parted and removed {chan_to_part}")
                                continue

                        elif clean_msg.lower() in ("!reload", "!minibot reload"):
                            if hasattr(self.cfg_mgr, "reload"):
                                self.cfg_mgr.reload()
                            else:
                                self.cfg_mgr = ConfigManager(self.config_path) if self.config_path else ConfigManager()
                            self.bot.config = self.cfg_mgr
                            self._reload_modules()
                            self.bot.send_msg(reply_target, f"✅ [Hot-Reload] Reloaded configuration and {len(self._attached_adapters)} sideload plugins.")
                            continue

                        elif cmd_name == "!botstatus":
                            uptime_sec = int(time.time() - self._start_time)
                            hrs, rem = divmod(uptime_sec, 3600)
                            mins, secs = divmod(rem, 60)
                            uptime_str = f"{hrs}h {mins}m {secs}s" if hrs > 0 else f"{mins}m {secs}s"
                            py_ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
                            ssl_mode = "SSL (Verified)" if (self.use_ssl and self.tls_verify) else ("SSL (Insecure)" if self.use_ssl else "Plaintext")
                            chans_str = ", ".join(self.channels)
                            mods_count = len(self._attached_adapters)
                            status_reply = f"🤖 [Status] Uptime: {uptime_str} | Python: {py_ver} | TLS: {ssl_mode} | Channels: {chans_str} | Plugins: {mods_count}"
                            self.bot.send_msg(reply_target, status_reply)
                            continue

                    # Route through MiniBot event loop
                    self.bot.handle_privmsg(target, sender_nick, msg_text)

    async def _watchdog_loop(self):
        """Sends keepalive PING every 120s of inactivity to detect dead NAT/router sockets."""
        while self._running:
            await asyncio.sleep(15.0)
            idle_sec = time.time() - self._last_data_time
            if idle_sec > 180.0:
                logger.warning(f"Socket inactive for {idle_sec:.0f}s (exceeded 180s threshold). Forcing reconnect...")
                if self._writer:
                    try:
                        self._writer.close()
                    except Exception:
                        pass
                break
            elif idle_sec > 120.0:
                logger.debug(f"Socket idle for {idle_sec:.0f}s. Sending keepalive PING...")
                await self._send_queue.put("PING :keepalive")

    async def _mtime_watcher_loop(self, interval: float = 3.0):
        """Watches sideload/ directory for script mtime changes and automatically reloads."""
        script_mtimes = {}
        sideload_dir = BASE_DIR / "sideload"
        while self._running:
            await asyncio.sleep(interval)
            try:
                changed = False
                for p in sideload_dir.glob("*.py"):
                    mtime = p.stat().st_mtime
                    if p.name in script_mtimes and script_mtimes[p.name] != mtime:
                        logger.info(f"Detected modification in {p.name}. Hot-reloading...")
                        changed = True
                    script_mtimes[p.name] = mtime
                if changed:
                    self._reload_modules()
            except Exception as e:
                logger.debug(f"Error in mtime watcher: {e}")

    async def _connect_and_run(self):
        ssl_ctx = ssl.create_default_context() if self.use_ssl else None
        if ssl_ctx and not self.tls_verify:
            ssl_ctx.check_hostname = False
            ssl_ctx.verify_mode = ssl.CERT_NONE

        logger.info(f"Connecting to {self.host}:{self.port} (SSL: {self.use_ssl})...")
        reader, writer = await asyncio.open_connection(self.host, self.port, ssl=ssl_ctx)
        self._writer = writer
        self._last_data_time = time.time()
        logger.info(f"Connected to {self.host}! Registering as {self.current_nick}...")

        # Handshake
        await self._send_queue.put(f"NICK {self.current_nick}")
        await self._send_queue.put(f"USER {self.current_nick} 0 * :Minibot Lite")

        send_task = asyncio.create_task(self._send_loop())
        recv_task = asyncio.create_task(self._recv_loop(reader))
        watchdog_task = asyncio.create_task(self._watchdog_loop())

        try:
            done, pending = await asyncio.wait(
                [send_task, recv_task, watchdog_task],
                return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
        finally:
            if self._writer:
                try:
                    self._writer.close()
                    await self._writer.wait_closed()
                except Exception:
                    pass
                self._writer = None

    async def run(self):
        watcher_task = asyncio.create_task(self._mtime_watcher_loop())
        backoff = 1.0

        try:
            while self._running:
                try:
                    connect_start = time.time()
                    await self._connect_and_run()
                    if time.time() - connect_start > 30.0:
                        backoff = 1.0
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.error(f"Connection session error: {e}")

                if not self._running:
                    break

                jitter = random.uniform(0.7, 1.3)
                sleep_time = min(60.0, backoff * jitter)
                logger.info(f"Reconnecting in {sleep_time:.1f}s (backoff={backoff:.1f}s)...")
                await asyncio.sleep(sleep_time)
                backoff = min(60.0, backoff * 2.0)
        finally:
            watcher_task.cancel()


def parse_cli_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Zero-Dependency Resilient Standalone Runner for Minibots.",
        usage="python3 run_bot.py [--nick NICK] [--network NETWORK] [--channel #CHAN1,#CHAN2] [--modules MODS] [nick] [network] [channel] [modules]"
    )
    parser.add_argument("pos_nick", nargs="?", default=None, help="Bot nickname (positional)")
    parser.add_argument("pos_network", nargs="?", default=None, help="IRC network (positional)")
    parser.add_argument("pos_channel", nargs="?", default=None, help="IRC channel(s) (positional)")
    parser.add_argument("pos_modules", nargs="?", default=None, help="Comma-separated module names (positional)")

    parser.add_argument("--nick", "-n", dest="opt_nick", default=None, help="Bot nickname")
    parser.add_argument("--network", dest="opt_network", default=None, help="IRC network key in config.json")
    parser.add_argument("--channel", "-c", "--channels", dest="opt_channel", default=None, help="IRC channel(s) to join (comma-separated)")
    parser.add_argument("--modules", "-m", dest="opt_modules", default=None, help="Comma-separated module names")

    parser.add_argument("--host", dest="host", default=None, help="IRC server hostname/IP override")
    parser.add_argument("--port", dest="port", type=int, default=None, help="IRC server port override")
    parser.add_argument("--ssl", dest="ssl", action="store_true", default=None, help="Force SSL/TLS connection")
    parser.add_argument("--insecure", "--no-verify", dest="insecure", action="store_true", default=False, help="Disable TLS certificate verification")
    parser.add_argument("--config", dest="config", default=None, help="Path to config.json")
    parser.add_argument("--daemon", dest="daemon", action="store_true", default=False, help="Run as background daemon with rotating file logging")
    parser.add_argument("--pid-file", dest="pid_file", default=None, help="Custom path to PID file (default: data/minibot.pid)")
    parser.add_argument("--import-sopel", dest="import_sopel", nargs="?", const="", default=None, help="Import configuration and plugins from Sopel .cfg file")

    args = parser.parse_args(argv)
    parser.parsed_args = args

    nick = args.opt_nick or args.pos_nick
    network = args.opt_network or args.pos_network
    channel = args.opt_channel or args.pos_channel
    raw_modules = args.opt_modules or args.pos_modules

    return parser, nick, network, channel, raw_modules


def main():
    parser, nick, network, channel, raw_modules = parse_cli_args()
    args = getattr(parser, "parsed_args", None)

    # 1. Handle --import-sopel
    if args and args.import_sopel is not None:
        cfg_arg = args.import_sopel if args.import_sopel != "" else None
        try:
            res = import_sopel_config(sopel_cfg_path=cfg_arg)
            print(f"✅ Successfully imported Sopel configuration from '{res['file']}' into config.json.")
            if res.get("copied_plugins"):
                print(f"📦 Copied {len(res['copied_plugins'])} plugins to sideload/: {', '.join(res['copied_plugins'])}")
            else:
                print("ℹ️ No additional custom plugins found in Sopel directories.")
            return 0
        except Exception as e:
            print(f"❌ Error importing Sopel configuration: {e}", file=sys.stderr)
            sys.exit(1)

    # 2. Daemon mode: set up rotating file handler and PID file
    is_daemon = args and getattr(args, "daemon", False)
    data_dir = BASE_DIR / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    if is_daemon:
        log_file = data_dir / "minibot.log"
        pid_file = Path(args.pid_file) if (args and args.pid_file) else (data_dir / "minibot.pid")

        # Write PID file
        try:
            pid_file.write_text(f"{os.getpid()}\n", encoding="utf-8")
            os.chmod(pid_file, 0o644)
        except Exception as e:
            logger.error(f"Failed to write PID file {pid_file}: {e}")

        def _cleanup_pid():
            try:
                if pid_file.exists():
                    content = pid_file.read_text(encoding="utf-8").strip()
                    if content == str(os.getpid()):
                        pid_file.unlink()
            except Exception:
                pass

        atexit.register(_cleanup_pid)

        # Configure rotating log handler (5 MB, 2 backups)
        file_handler = RotatingFileHandler(
            str(log_file),
            maxBytes=5 * 1024 * 1024,
            backupCount=2,
            encoding="utf-8"
        )
        file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)-7s] %(name)s: %(message)s"))

        # Route logger handlers
        for h in list(logger.handlers):
            if isinstance(h, logging.StreamHandler) and not isinstance(h, RotatingFileHandler):
                logger.removeHandler(h)
        logger.addHandler(file_handler)

        def handle_uncaught_exception(exc_type, exc_value, exc_traceback):
            if issubclass(exc_type, KeyboardInterrupt):
                sys.__excepthook__(exc_type, exc_value, exc_traceback)
                return
            logger.critical("Uncaught exception in daemon:", exc_info=(exc_type, exc_value, exc_traceback))

        sys.excepthook = handle_uncaught_exception

    config_path = getattr(args, "config", None) if args else None
    cfg = ConfigManager(config_path) if config_path else ConfigManager()

    # Fallback to config.json or sensible defaults
    network = network or cfg.primary_network or "local"
    srv = cfg.servers.get(network, {})

    nick = nick or srv.get("nick") or os.environ.get("IRC_NICK") or "minibot"

    if not channel:
        cfg_chans = srv.get("channels") or cfg.admin_channels
        channel = ",".join(cfg_chans) if cfg_chans else "#minibot"

    if not raw_modules:
        modules = list(DEFAULT_MODULES)
    else:
        modules = [m.strip() for m in raw_modules.split(",") if m.strip()]

    host = getattr(args, "host", None) if args else None
    port = getattr(args, "port", None) if args else None
    use_ssl = getattr(args, "ssl", None) if args else None
    tls_verify = False if (args and getattr(args, "insecure", False)) else None

    runner = StandaloneRunner(
        nick=nick,
        network=network,
        channels=channel,
        modules=modules,
        host=host,
        port=port,
        use_ssl=use_ssl,
        tls_verify=tls_verify,
        config_path=config_path,
    )

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def _sig_handler():
        logger.info("Received termination signal, shutting down...")
        runner._running = False
        for t in asyncio.all_tasks(loop):
            t.cancel()

    def _sighup_handler():
        logger.info("Received SIGHUP, reloading all sideload plugins live...")
        runner._reload_modules()

    for s in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(s, _sig_handler)

    if hasattr(signal, "SIGHUP"):
        loop.add_signal_handler(signal.SIGHUP, _sighup_handler)

    try:
        loop.run_until_complete(runner.run())
    finally:
        loop.close()


if __name__ == "__main__":
    main()
