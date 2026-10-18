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
            remaining_in_other = False
            for s_v in servers.values():
                if any(c.lower() == clean_chan.lower() for c in s_v.get("channels", [])):
                    remaining_in_other = True
                    break
            if not remaining_in_other:
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


def persist_server_change(
    config_path: Optional[str],
    network: str,
    server_data: Optional[Dict[str, Any]],
    action: str
) -> bool:
    """
    Persists adding or removing an IRC server entry in config.json.
    action: 'add' or 'remove'
    """
    cfg_file = Path(config_path) if config_path else (BASE_DIR / "config.json")
    if not cfg_file.exists():
        return False

    try:
        with open(cfg_file, "r", encoding="utf-8") as f:
            data = json.load(f)

        servers = data.setdefault("SERVERS", {})
        net_key = network.lower()

        if action == "add" and server_data is not None:
            servers[net_key] = server_data
            admin_nets = data.setdefault("ADMIN_NETWORKS", [])
            if net_key not in admin_nets:
                admin_nets.append(net_key)
            if not data.get("PRIMARY_NETWORK"):
                data["PRIMARY_NETWORK"] = net_key
            admin_chans = data.setdefault("ADMIN_CHANNELS", [])
            for c in server_data.get("channels", []):
                if c not in admin_chans:
                    admin_chans.append(c)

        elif action in ("remove", "del"):
            if net_key in servers:
                del servers[net_key]
            admin_nets = data.get("ADMIN_NETWORKS", [])
            if net_key in admin_nets:
                data["ADMIN_NETWORKS"] = [n for n in admin_nets if n != net_key]
            if data.get("PRIMARY_NETWORK") == net_key:
                data["PRIMARY_NETWORK"] = next(iter(servers.keys())) if servers else "primary"

        tmp_file = cfg_file.with_suffix(".tmp")
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        tmp_file.replace(cfg_file)
        os.chmod(cfg_file, 0o600)
        return True
    except Exception as e:
        logger.error(f"Failed to persist server change to {cfg_file}: {e}")
        return False


def is_private_or_loopback_host(host: str) -> bool:
    """Checks if a hostname/IP is loopback, localhost, or private RFC1918."""
    if not host:
        return False
    h = host.strip().lower()
    if h in ("127.0.0.1", "localhost", "::1", "0.0.0.0"):
        return True
    if h.startswith("10.") or h.startswith("192.168.") or h.startswith("127."):
        return True
    if h.startswith("172."):
        parts = h.split(".")
        if len(parts) >= 2 and parts[1].isdigit():
            val = int(parts[1])
            if 16 <= val <= 31:
                return True
    return False


def import_sopel_config(
    sopel_cfg_path: Optional[str] = None,
    out_config_json: Optional[str] = None,
    sideload_dir: Optional[str] = None
) -> Dict[str, Any]:
    """
    Imports configuration from existing Sopel installation(s) (.cfg files)
    and converts them into Minibots config.json format using only the Python standard library.
    Supports single file, directory of .cfg files, or auto-discovery across ~/.sopel and ~/.config/sopel.
    Optionally copies custom .py plugins from Sopel plugins/modules directory to sideload/.
    """
    cfg_files: List[str] = []
    sopel_dirs = [
        Path.home() / ".sopel",
        Path.home() / ".config" / "sopel",
    ]

    if sopel_cfg_path:
        p = Path(sopel_cfg_path)
        if p.is_file():
            cfg_files.append(str(p))
        elif p.is_dir():
            cfg_files.extend(sorted(str(f) for f in p.glob("*.cfg")))
    else:
        for sdir in sopel_dirs:
            if sdir.is_dir():
                found = sorted(str(f) for f in sdir.glob("*.cfg"))
                cfg_files.extend(found)
        if not cfg_files:
            for sdir in sopel_dirs:
                def_cfg = sdir / "default.cfg"
                if def_cfg.is_file():
                    cfg_files.append(str(def_cfg))

    if not cfg_files:
        searched = sopel_cfg_path or [str(d) for d in sopel_dirs]
        raise FileNotFoundError(f"No Sopel configuration found. Checked: {searched}")

    config_data: Dict[str, Any] = {
        "PRIMARY_NETWORK": "",
        "ADMIN_NETWORKS": [],
        "ADMIN_CHANNELS": [],
        "ADMIN_NICKS": [],
        "SERVERS": {}
    }

    target_sideload = Path(sideload_dir) if sideload_dir else (BASE_DIR / "sideload")
    target_sideload.mkdir(parents=True, exist_ok=True)

    plugin_dirs: List[Path] = [
        Path.home() / ".sopel" / "plugins",
        Path.home() / ".sopel" / "modules",
        Path.home() / ".config" / "sopel" / "plugins",
    ]

    for cfg_file in cfg_files:
        cp = configparser.ConfigParser(interpolation=None)
        cp.read(cfg_file, encoding="utf-8")

        core = cp["core"] if "core" in cp else {}
        host = core.get("host", "irc.libera.chat").strip()
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
        if owner and owner not in config_data["ADMIN_NICKS"]:
            config_data["ADMIN_NICKS"].append(owner)

        for c in channels:
            if c not in config_data["ADMIN_CHANNELS"]:
                config_data["ADMIN_CHANNELS"].append(c)

        is_trusted = is_private_or_loopback_host(host)

        # Derive network key
        stem = Path(cfg_file).stem.lower()
        if stem.startswith("test"):
            net_key = "primary"
        elif stem in ("default", "config"):
            if len(cfg_files) > 1:
                h_lower = host.lower()
                for known in ("libera", "oftc", "rizon", "efnet", "undernet", "dalnet", "esper"):
                    if known in h_lower:
                        net_key = known
                        break
                else:
                    net_key = "primary"
            else:
                net_key = "primary"
        else:
            net_key = stem

        base_net_key = net_key
        counter = 2
        while net_key in config_data["SERVERS"]:
            net_key = f"{base_net_key}_{counter}"
            counter += 1

        config_data["SERVERS"][net_key] = {
            "host": host,
            "port": port,
            "nick": nick,
            "channels": channels,
            "ssl": use_ssl,
            "trusted": is_trusted,
            "tls_verify": verify_ssl,
            "nickserv_password": auth_pass
        }
        if net_key not in config_data["ADMIN_NETWORKS"]:
            config_data["ADMIN_NETWORKS"].append(net_key)

        if not config_data["PRIMARY_NETWORK"]:
            config_data["PRIMARY_NETWORK"] = net_key

        sopel_dir = Path(cfg_file).parent
        for sub in ("plugins", "modules"):
            candidate_pdir = sopel_dir / sub
            if candidate_pdir.is_dir() and candidate_pdir not in plugin_dirs:
                plugin_dirs.append(candidate_pdir)

    if not config_data["ADMIN_NICKS"]:
        config_data["ADMIN_NICKS"] = ["admin"]

    out_path = Path(out_config_json) if out_config_json else (BASE_DIR / "config.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(config_data, f, indent=2)
    os.chmod(out_path, 0o600)

    copied = []
    for pdir in plugin_dirs:
        if pdir.is_dir():
            for py_file in pdir.glob("*.py"):
                dest = target_sideload / py_file.name
                if not dest.exists():
                    shutil.copy2(py_file, dest)
                    copied.append(py_file.name)

    return {
        "config": config_data,
        "file": cfg_files[0],
        "files": cfg_files,
        "copied_plugins": copied
    }


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
        sideload_engine: Optional[SideloadEngine] = None,
        server_manager: Optional[Any] = None,
    ):
        self.initial_nick = nick
        self.current_nick = nick
        self.nick = nick
        self.network = (network or "local").lower()
        self.status_str = "INITIALIZING"
        self.server_manager = server_manager

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

        self.sideload_engine = sideload_engine or SideloadEngine()
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

    def stop(self) -> None:
        """Signals the runner to terminate and closes the active socket connection."""
        self._running = False
        self.status_str = "STOPPED"
        if self._writer:
            try:
                self._writer.close()
            except Exception:
                pass

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
                logger.info(f"[{self.network}:{self.current_nick}] Attached module '{mod_name}'")
            else:
                logger.error(f"[{self.network}:{self.current_nick}] Failed to load module '{mod_name}'")

    def _reload_modules(self) -> None:
        """In-process hot-reload of all attached sideload plugins without dropping IRC socket."""
        logger.info(f"[{self.network}:{self.current_nick}] Hot-reloading modules...")
        if self.server_manager is None:
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

            logger.info(f"[{self.network}:{self.current_nick} OUT] {clean}")
            if self._writer and not self._writer.is_closing():
                try:
                    self._writer.write(f"{clean}\r\n".encode("utf-8"))
                    await self._writer.drain()
                except Exception as e:
                    logger.warning(f"[{self.network}:{self.current_nick}] Error writing to socket: {e}")
                    break

    async def _recv_loop(self, reader: asyncio.StreamReader):
        registered = False
        while self._running:
            line_bytes = await reader.readline()
            if not line_bytes:
                logger.warning(f"[{self.network}:{self.current_nick}] Connection closed by remote server.")
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
                    logger.warning(f"[{self.network}:{self.current_nick}] Nickname collision: retrying registration as {self.current_nick}...")
                    await self._send_queue.put(f"NICK {self.current_nick}")
                    continue

                # NICK change tracking
                if cmd == "NICK":
                    prefix = parts[0].lstrip(":")
                    sender_nick = prefix.split("!")[0]
                    if sender_nick.lower() == self.current_nick.lower():
                        new_nick = parts[2].lstrip(":") if len(parts) > 2 else parts[-1].lstrip(":")
                        logger.info(f"[{self.network}:{self.current_nick}] Nickname changed from {self.current_nick} to {new_nick}")
                        self.current_nick = new_nick
                        self.bot.nick = new_nick
                        if new_nick.lower() == self.initial_nick.lower():
                            self._reclaim_needed = False
                    continue

                # Registered on 001, 376, 422
                if cmd in ("001", "376", "422") and not registered:
                    registered = True
                    self.status_str = "CONNECTED"
                    logger.info(f"[{self.network}:{self.current_nick}] Registered with {self.network} as {self.current_nick}!")

                    if self._reclaim_needed and self.nickserv_password:
                        logger.info(f"[{self.network}:{self.current_nick}] Reclaiming primary nickname {self.initial_nick} via NickServ GHOST...")
                        await self._send_queue.put(f"PRIVMSG NickServ :GHOST {self.initial_nick} {self.nickserv_password}")
                        await asyncio.sleep(1.5)
                        await self._send_queue.put(f"NICK {self.initial_nick}")
                        await asyncio.sleep(1.0)
                        await self._send_queue.put(f"PRIVMSG NickServ :IDENTIFY {self.nickserv_password}")
                        await asyncio.sleep(1.0)
                    elif self.nickserv_password:
                        logger.info(f"[{self.network}:{self.current_nick}] Authenticating with NickServ...")
                        await self._send_queue.put(f"PRIVMSG NickServ :IDENTIFY {self.nickserv_password}")
                        await asyncio.sleep(1.0)

                    for chan in self.channels:
                        logger.info(f"[{self.network}:{self.current_nick}] Joining {chan}...")
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

                        # 1. !server / !servers
                        if cmd_name in ("!server", "!servers"):
                            subcmd = parts_msg[1].lower() if len(parts_msg) > 1 else "list"
                            if subcmd == "list":
                                if self.server_manager:
                                    lines = []
                                    for net_k, r in self.server_manager.runners.items():
                                        st = getattr(r, "status_str", "UNKNOWN")
                                        ch_str = ",".join(r.channels)
                                        lines.append(f"{net_k} ({r.host}:{r.port}) [{st}] nick={r.current_nick} chans={ch_str}")
                                    self.bot.send_msg(reply_target, f"🌐 Networks ({len(lines)}): " + " | ".join(lines))
                                else:
                                    ch_str = ",".join(self.channels)
                                    self.bot.send_msg(reply_target, f"🌐 Network: {self.network} ({self.host}:{self.port}) [{self.status_str}] nick={self.current_nick} chans={ch_str}")
                                continue

                            elif subcmd == "add":
                                # !server add <net_name> <host[:port]> [ssl|plain] [#chan1,#chan2] [nick]
                                if len(parts_msg) < 4:
                                    self.bot.send_msg(reply_target, "Usage: !server add <net_name> <host[:port]> [ssl|plain] [#chan1,#chan2] [nick]")
                                    continue
                                net_name = parts_msg[2].lower()
                                host_port = parts_msg[3]
                                if ":" in host_port:
                                    h_part, p_part = host_port.split(":", 1)
                                    try:
                                        p_val = int(p_part)
                                    except ValueError:
                                        p_val = None
                                else:
                                    h_part = host_port
                                    p_val = None

                                ssl_mode = "ssl"
                                if len(parts_msg) > 4:
                                    opt = parts_msg[4].lower()
                                    if opt in ("plain", "plaintext", "no", "false"):
                                        ssl_mode = "plain"
                                    elif opt in ("ssl", "tls", "yes", "true"):
                                        ssl_mode = "ssl"
                                use_ssl_val = (ssl_mode == "ssl")
                                if p_val is None:
                                    p_val = 6697 if use_ssl_val else 6667

                                raw_chans = parts_msg[5] if len(parts_msg) > 5 else "#minibot"
                                chans_val = [c if c.startswith("#") else f"#{c}" for c in raw_chans.split(",") if c]
                                if not chans_val:
                                    chans_val = ["#minibot"]

                                nick_val = parts_msg[6] if len(parts_msg) > 6 else self.initial_nick
                                is_trusted = is_private_or_loopback_host(h_part)
                                srv_entry = {
                                    "host": h_part,
                                    "port": p_val,
                                    "nick": nick_val,
                                    "channels": chans_val,
                                    "ssl": use_ssl_val,
                                    "trusted": is_trusted,
                                    "tls_verify": True,
                                    "nickserv_password": ""
                                }
                                ok = persist_server_change(self.config_path, net_name, srv_entry, "add")
                                if not ok:
                                    self.bot.send_msg(reply_target, f"❌ Failed to persist server '{net_name}' to config.json.")
                                    continue

                                if self.server_manager:
                                    await self.server_manager.add_and_start_server(net_name, srv_entry)
                                    self.bot.send_msg(reply_target, f"✅ Added and connected to '{net_name}' ({h_part}:{p_val}, ssl={use_ssl_val}, chans={','.join(chans_val)})")
                                else:
                                    self.bot.send_msg(reply_target, f"✅ Persisted server '{net_name}' ({h_part}:{p_val}). Restart required for single-server mode.")
                                continue

                            elif subcmd in ("del", "remove"):
                                if len(parts_msg) < 3:
                                    self.bot.send_msg(reply_target, "Usage: !server del <net_name>")
                                    continue
                                net_name = parts_msg[2].lower()
                                persist_server_change(self.config_path, net_name, None, "remove")
                                if self.server_manager:
                                    await self.server_manager.stop_and_remove_server(net_name)
                                    self.bot.send_msg(reply_target, f"✅ Stopped and removed server '{net_name}'.")
                                else:
                                    self.bot.send_msg(reply_target, f"✅ Removed server '{net_name}' from config.")
                                continue

                        # 2. !join
                        elif cmd_name == "!join" and len(parts_msg) > 1:
                            arg1 = parts_msg[1]
                            if len(parts_msg) > 2 or (not arg1.startswith("#") and self.server_manager and arg1.lower() in self.server_manager.runners):
                                target_net = arg1.lower()
                                chan_to_join = parts_msg[2]
                            else:
                                target_net = self.network
                                chan_to_join = arg1

                            if not chan_to_join.startswith("#"):
                                chan_to_join = f"#{chan_to_join}"

                            if target_net == self.network:
                                await self._send_queue.put(f"JOIN {chan_to_join}")
                                if chan_to_join not in self.channels:
                                    self.channels.append(chan_to_join)
                                self.bot.channels = self.channels
                                persist_channel_change(self.config_path, self.network, chan_to_join, "add")
                                self.bot.send_msg(reply_target, f"✅ Joined and persisted {chan_to_join}")
                            elif self.server_manager and target_net in self.server_manager.runners:
                                r = self.server_manager.runners[target_net]
                                await r._send_queue.put(f"JOIN {chan_to_join}")
                                if chan_to_join not in r.channels:
                                    r.channels.append(chan_to_join)
                                r.bot.channels = r.channels
                                persist_channel_change(self.config_path, target_net, chan_to_join, "add")
                                self.bot.send_msg(reply_target, f"✅ Joined and persisted {chan_to_join} on {target_net}")
                            else:
                                self.bot.send_msg(reply_target, f"❌ Network '{target_net}' is not active or recognized.")
                            continue

                        # 3. !part
                        elif cmd_name == "!part":
                            target_net = self.network
                            chan_to_part = ""
                            if len(parts_msg) >= 3:
                                target_net = parts_msg[1].lower()
                                chan_to_part = parts_msg[2]
                            elif len(parts_msg) == 2:
                                arg1 = parts_msg[1]
                                if not arg1.startswith("#") and self.server_manager and arg1.lower() in self.server_manager.runners:
                                    target_net = arg1.lower()
                                    chan_to_part = ""
                                else:
                                    chan_to_part = arg1
                            else:
                                chan_to_part = target if target.startswith("#") else ""

                            if chan_to_part and not chan_to_part.startswith("#"):
                                chan_to_part = f"#{chan_to_part}"

                            if not chan_to_part:
                                self.bot.send_msg(reply_target, "Usage: !part [#channel] or !part <network> <#channel>")
                                continue

                            if target_net == self.network:
                                await self._send_queue.put(f"PART {chan_to_part} :Parted by admin")
                                self.channels = [c for c in self.channels if c.lower() != chan_to_part.lower()]
                                self.bot.channels = self.channels
                                persist_channel_change(self.config_path, self.network, chan_to_part, "remove")
                                self.bot.send_msg(reply_target, f"✅ Parted and removed {chan_to_part}")
                            elif self.server_manager and target_net in self.server_manager.runners:
                                r = self.server_manager.runners[target_net]
                                await r._send_queue.put(f"PART {chan_to_part} :Parted by admin")
                                r.channels = [c for c in r.channels if c.lower() != chan_to_part.lower()]
                                r.bot.channels = r.channels
                                persist_channel_change(self.config_path, target_net, chan_to_part, "remove")
                                self.bot.send_msg(reply_target, f"✅ Parted and removed {chan_to_part} from {target_net}")
                            else:
                                self.bot.send_msg(reply_target, f"❌ Network '{target_net}' is not active or recognized.")
                            continue

                        # 4. !reload
                        elif clean_msg.lower() in ("!reload", "!minibot reload"):
                            if self.server_manager:
                                self.server_manager.reload_all()
                                count = len(self._attached_adapters)
                                self.bot.send_msg(reply_target, f"✅ [Hot-Reload] Reloaded configuration and {count} sideload plugins across all networks.")
                            else:
                                if hasattr(self.cfg_mgr, "reload"):
                                    self.cfg_mgr.reload()
                                else:
                                    self.cfg_mgr = ConfigManager(self.config_path) if self.config_path else ConfigManager()
                                self.bot.config = self.cfg_mgr
                                self._reload_modules()
                                self.bot.send_msg(reply_target, f"✅ [Hot-Reload] Reloaded configuration and {len(self._attached_adapters)} sideload plugins.")
                            continue

                        # 5. !botstatus
                        elif cmd_name == "!botstatus":
                            uptime_sec = int(time.time() - self._start_time)
                            hrs, rem = divmod(uptime_sec, 3600)
                            mins, secs = divmod(rem, 60)
                            uptime_str = f"{hrs}h {mins}m {secs}s" if hrs > 0 else f"{mins}m {secs}s"
                            py_ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
                            ssl_mode = "SSL (Verified)" if (self.use_ssl and self.tls_verify) else ("SSL (Insecure)" if self.use_ssl else "Plaintext")
                            chans_str = ", ".join(self.channels)
                            mods_count = len(self._attached_adapters)
                            if self.server_manager:
                                net_names = list(self.server_manager.runners.keys())
                                net_str = f"Networks: {len(net_names)} ({', '.join(net_names)}) | "
                            else:
                                net_str = f"Network: {self.network} | "
                            status_reply = f"🤖 [Status] Uptime: {uptime_str} | {net_str}Python: {py_ver} | TLS: {ssl_mode} | Channels: {chans_str} | Plugins: {mods_count}"
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
                logger.warning(f"[{self.network}:{self.current_nick}] Socket inactive for {idle_sec:.0f}s (exceeded 180s threshold). Forcing reconnect...")
                if self._writer:
                    try:
                        self._writer.close()
                    except Exception:
                        pass
                break
            elif idle_sec > 120.0:
                logger.debug(f"[{self.network}:{self.current_nick}] Socket idle for {idle_sec:.0f}s. Sending keepalive PING...")
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
                        logger.info(f"[{self.network}:{self.current_nick}] Detected modification in {p.name}. Hot-reloading...")
                        changed = True
                    script_mtimes[p.name] = mtime
                if changed:
                    self._reload_modules()
            except Exception as e:
                logger.debug(f"Error in mtime watcher: {e}")

    async def _connect_and_run(self):
        # Reset nickname, state, and queue for clean connection session
        self.current_nick = self.initial_nick
        self.bot.nick = self.initial_nick
        self._reclaim_needed = False
        self._send_queue = asyncio.Queue()
        self.status_str = "CONNECTING"

        ssl_ctx = ssl.create_default_context() if self.use_ssl else None
        if ssl_ctx and not self.tls_verify:
            ssl_ctx.check_hostname = False
            ssl_ctx.verify_mode = ssl.CERT_NONE

        logger.info(f"[{self.network}:{self.current_nick}] Connecting to {self.host}:{self.port} (SSL: {self.use_ssl})...")
        reader, writer = await asyncio.open_connection(self.host, self.port, ssl=ssl_ctx)
        self._writer = writer
        self._last_data_time = time.time()
        logger.info(f"[{self.network}:{self.current_nick}] Connected to {self.host}! Registering as {self.current_nick}...")

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
        watcher_task = None
        if self.server_manager is None:
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
                    logger.error(f"[{self.network}:{self.current_nick}] Connection session error: {e}")

                if not self._running:
                    break

                self.status_str = "RECONNECTING"
                jitter = random.uniform(0.7, 1.3)
                sleep_time = min(60.0, backoff * jitter)
                logger.info(f"[{self.network}:{self.current_nick}] Reconnecting in {sleep_time:.1f}s (backoff={backoff:.1f}s)...")
                await asyncio.sleep(sleep_time)
                backoff = min(60.0, backoff * 2.0)
        finally:
            self.status_str = "STOPPED"
            if watcher_task and not watcher_task.cancelled():
                watcher_task.cancel()


class MultiServerManager:
    """Manages concurrent StandaloneRunner instances multiplexed across multiple IRC networks."""

    def __init__(
        self,
        config_path: Optional[str] = None,
        modules: Optional[List[str]] = None,
        default_nick: Optional[str] = None,
        default_channels: Optional[Union[str, List[str]]] = None,
    ):
        self.config_path = config_path
        self.modules = list(modules) if modules else list(DEFAULT_MODULES)
        self.default_nick = default_nick
        self.default_channels = default_channels
        self.cfg_mgr = ConfigManager(config_path) if config_path else ConfigManager()
        self.sideload_engine = SideloadEngine()
        self.runners: Dict[str, StandaloneRunner] = {}
        self.tasks: Dict[str, asyncio.Task] = {}
        self._running = True
        self._mtime_task: Optional[asyncio.Task] = None

    def reload_all(self) -> None:
        """Hot-reloads sideload plugins across all connected IRC networks simultaneously."""
        logger.info("Hot-reloading modules across all active network runners...")
        self.sideload_engine.loaded_modules.clear()
        if hasattr(self.cfg_mgr, "reload"):
            self.cfg_mgr.reload()
        else:
            self.cfg_mgr = ConfigManager(self.config_path) if self.config_path else ConfigManager()

        for runner in self.runners.values():
            runner.cfg_mgr = self.cfg_mgr
            runner.bot.config = self.cfg_mgr
            runner._reload_modules()

    async def _mtime_watcher_loop(self, interval: float = 3.0) -> None:
        """Watches sideload/ directory for script mtime changes and automatically reloads all runners."""
        script_mtimes = {}
        sideload_dir = BASE_DIR / "sideload"
        while self._running:
            await asyncio.sleep(interval)
            try:
                changed = False
                for p in sideload_dir.glob("*.py"):
                    mtime = p.stat().st_mtime
                    if p.name in script_mtimes and script_mtimes[p.name] != mtime:
                        logger.info(f"Detected modification in {p.name}. Hot-reloading all networks...")
                        changed = True
                    script_mtimes[p.name] = mtime
                if changed:
                    self.reload_all()
            except Exception as e:
                logger.debug(f"Error in shared mtime watcher: {e}")

    def create_runner(self, net_name: str, srv_data: Optional[Dict[str, Any]] = None) -> StandaloneRunner:
        if srv_data is None:
            srv_data = self.cfg_mgr.servers.get(net_name, {})

        nick = self.default_nick or srv_data.get("nick") or os.environ.get("IRC_NICK") or "minibot"
        channels = self.default_channels or srv_data.get("channels") or ["#minibot"]
        host = srv_data.get("host", "127.0.0.1")
        default_ssl = srv_data.get("ssl", False)
        default_port = 6697 if default_ssl else 6667
        port = srv_data.get("port", default_port)
        use_ssl = default_ssl
        tls_verify = srv_data.get("tls_verify", True)
        nickserv_password = srv_data.get("nickserv_password")

        runner = StandaloneRunner(
            nick=nick,
            network=net_name,
            channels=channels,
            modules=self.modules,
            host=host,
            port=port,
            use_ssl=use_ssl,
            tls_verify=tls_verify,
            nickserv_password=nickserv_password,
            config_path=self.config_path,
            sideload_engine=self.sideload_engine,
            server_manager=self,
        )
        return runner

    async def add_and_start_server(self, net_name: str, srv_data: Dict[str, Any]) -> StandaloneRunner:
        """Dynamically adds and connects a new network runner to the active event loop."""
        if net_name in self.runners:
            await self.stop_and_remove_server(net_name)

        runner = self.create_runner(net_name, srv_data)
        self.runners[net_name] = runner
        task = asyncio.create_task(runner.run())
        self.tasks[net_name] = task
        logger.info(f"Dynamically started runner for network '{net_name}'")
        return runner

    async def stop_and_remove_server(self, net_name: str) -> bool:
        """Gracefully disconnects and removes a running network runner."""
        runner = self.runners.pop(net_name, None)
        task = self.tasks.pop(net_name, None)
        if runner:
            runner.stop()
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        return runner is not None

    async def run(self, networks: Optional[List[str]] = None) -> None:
        """Spawns StandaloneRunner tasks for all specified (or configured) networks and runs until stopped."""
        if hasattr(self.cfg_mgr, "reload"):
            self.cfg_mgr.reload()
        else:
            self.cfg_mgr = ConfigManager(self.config_path) if self.config_path else ConfigManager()

        if not networks:
            target_nets = [k for k, v in self.cfg_mgr.servers.items() if v.get("enabled", True) is not False]
            if not target_nets:
                target_nets = [self.cfg_mgr.primary_network or "primary"]
        else:
            target_nets = networks

        for net in target_nets:
            srv_data = self.cfg_mgr.servers.get(net, {})
            runner = self.create_runner(net, srv_data)
            self.runners[net] = runner
            self.tasks[net] = asyncio.create_task(runner.run())

        self._mtime_task = asyncio.create_task(self._mtime_watcher_loop())

        try:
            while self._running:
                if not self.tasks:
                    await asyncio.sleep(0.5)
                    continue
                done, _ = await asyncio.wait(
                    list(self.tasks.values()),
                    return_when=asyncio.FIRST_COMPLETED
                )
                for d in done:
                    for net_k, t in list(self.tasks.items()):
                        if t == d:
                            del self.tasks[net_k]
                            if not d.cancelled():
                                exc = d.exception()
                                if exc:
                                    logger.error(f"Runner task for network '{net_k}' exited with error: {exc}")
        except asyncio.CancelledError:
            pass
        finally:
            self.stop()
            if self._mtime_task and not self._mtime_task.cancelled():
                self._mtime_task.cancel()

    def stop(self) -> None:
        """Stops all running runners and background watchers."""
        self._running = False
        for runner in list(self.runners.values()):
            runner.stop()
        for task in list(self.tasks.values()):
            if not task.cancelled():
                task.cancel()


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
            file_count = len(res.get("files", [res.get("file")]))
            print(f"✅ Successfully imported Sopel configuration ({file_count} file(s)) into config.json.")
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

        # Attach to root logger so all modules/plugins log to data/minibot.log
        root_logger = logging.getLogger()
        root_logger.setLevel(logging.INFO)
        for h in list(root_logger.handlers):
            if isinstance(h, logging.StreamHandler) and not isinstance(h, RotatingFileHandler):
                root_logger.removeHandler(h)
        root_logger.addHandler(file_handler)

        # Route logger handlers
        for h in list(logger.handlers):
            if isinstance(h, logging.StreamHandler) and not isinstance(h, RotatingFileHandler):
                logger.removeHandler(h)
        if file_handler not in logger.handlers:
            logger.addHandler(file_handler)

        def handle_uncaught_exception(exc_type, exc_value, exc_traceback):
            if issubclass(exc_type, KeyboardInterrupt):
                sys.__excepthook__(exc_type, exc_value, exc_traceback)
                return
            logger.critical("Uncaught exception in daemon:", exc_info=(exc_type, exc_value, exc_traceback))

        sys.excepthook = handle_uncaught_exception

    config_path = getattr(args, "config", None) if args else None
    cfg = ConfigManager(config_path) if config_path else ConfigManager()

    if not raw_modules:
        modules = list(DEFAULT_MODULES)
    else:
        modules = [m.strip() for m in raw_modules.split(",") if m.strip()]

    host = getattr(args, "host", None) if args else None
    port = getattr(args, "port", None) if args else None
    use_ssl = getattr(args, "ssl", None) if args else None
    tls_verify = False if (args and getattr(args, "insecure", False)) else None

    # Set up event loop before runner instantiation for Python 3.7-3.9 compatibility
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    if host is not None:
        network = network or cfg.primary_network or "local"
        srv = cfg.servers.get(network, {})
        nick = nick or srv.get("nick") or os.environ.get("IRC_NICK") or "minibot"
        if not channel:
            cfg_chans = srv.get("channels") or cfg.admin_channels
            channel = ",".join(cfg_chans) if cfg_chans else "#minibot"

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

        def _sig_handler():
            logger.info("Received termination signal, shutting down...")
            runner.stop()
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
    else:
        if network:
            if network.strip().lower() == "all":
                target_networks = [k for k, v in cfg.servers.items() if v.get("enabled", True) is not False]
            else:
                target_networks = [n.strip().lower() for n in network.split(",") if n.strip()]
        else:
            target_networks = [k for k, v in cfg.servers.items() if v.get("enabled", True) is not False]
            if not target_networks:
                target_networks = [cfg.primary_network or "primary"]

        cli_nick = nick if (args and (args.opt_nick or args.pos_nick)) else None
        cli_channel = channel if (args and (args.opt_channel or args.pos_channel)) else None

        manager = MultiServerManager(
            config_path=config_path,
            modules=modules,
            default_nick=cli_nick,
            default_channels=cli_channel,
        )

        def _sig_handler():
            logger.info("Received termination signal, shutting down...")
            manager.stop()
            for t in asyncio.all_tasks(loop):
                t.cancel()

        def _sighup_handler():
            logger.info("Received SIGHUP, reloading all sideload plugins live...")
            manager.reload_all()

        for s in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(s, _sig_handler)

        if hasattr(signal, "SIGHUP"):
            loop.add_signal_handler(signal.SIGHUP, _sighup_handler)

        try:
            loop.run_until_complete(manager.run(target_networks))
        finally:
            loop.close()


if __name__ == "__main__":
    main()

