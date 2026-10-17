#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

"""
dpop_auth.py — Ephemeral DPoP Sanctuary Knock & Authentication Plugin for Minibots.
Dispatches user-tier knock passes into public/private IRC channels via PM.
Enforces 24h unclaimed TTL, 30d session grants, 5-device FIFO eviction warning,
account-cloak aware anti-abuse, channel review queue caps, and Ring 0 admin approval.
"""

import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import keystore

logger = logging.getLogger("DPoPAuthPlugin")

DEFAULT_UTILS_URL = "http://127.0.0.1:8000"


class DPoPAuthPlugin:
    """
    Minibot plugin providing automated and moderated DPoP sanctuary passes.
    """
    def __init__(self, minibot: Any, config: Optional[Dict[str, Any]] = None):
        self.bot = minibot
        self.config = config if config is not None else (getattr(minibot, "config", {}) or {})
        self.lock = threading.RLock()

        # State tracking
        self.pending_requests: Dict[int, Dict[str, Any]] = {}  # ticket_id -> data
        self.pending_confirmations: Dict[str, float] = {}       # nick -> timestamp
        self.hostmask_history: Dict[str, float] = {}            # cloak -> timestamp
        self.channel_modes: Dict[str, str] = {}                 # channel -> auto | moderated
        self.global_mode: str = "moderated"                     # default mode: moderated
        self.ticket_counter: int = 1

        # NickServ verification state
        self.nickserv_cache: Dict[str, Tuple[int, float]] = {}  # nick.lower() -> (status_code, timestamp)
        self.nickserv_waiters: Dict[str, threading.Event] = {}  # nick.lower() -> Event

        dpop_cfg = self.config.get("DPOP_AUTH", {}) if isinstance(self.config.get("DPOP_AUTH"), dict) else {}
        self.utils_url = (
            dpop_cfg.get("BASE_URL")
            or self.config.get("UTILS_URL")
            or os.environ.get("DPOP_AUTH_URL")
            or DEFAULT_UTILS_URL
        ).rstrip("/")

    def _get_api_auth_key(self) -> str:
        """Retrieves internal authorization key for DPoP auth API."""
        key = (
            keystore.get_secret("DPOP_AUTH_KEY")
            or keystore.get_secret("LERTZ_SECRET_KEY")
            or keystore.get_secret("SECRET_KEY")
            or os.environ.get("DPOP_AUTH_KEY")
            or os.environ.get("LERTZ_SECRET_KEY")
            or os.environ.get("SECRET_KEY")
            or os.environ.get("LERTZ_KEY")
            or ""
        )
        return key

    def _extract_cloak(self, sender_nick: str, hostmask: str) -> str:
        """
        Extracts account cloak or hostmask to prevent NAT/Tor collisions.
        Prioritizes IRCv3/NickServ user cloaks (e.g. user/account).
        """
        if not hostmask:
            net = getattr(self.bot, "network", "irc") if self.bot else "irc"
            return f"{sender_nick.lower()}@{net}"

        clean_mask = hostmask.lstrip(":")
        if "@" in clean_mask:
            ident, host = clean_mask.split("@", 1)
            # Check for cloak style with '/'
            if "/" in host:
                return host.lower()
            return f"{ident.lower()}@{host.lower()}"
        return clean_mask.lower()

    def _call_utils_api(self, endpoint: str, method: str = "GET", payload: Optional[Dict[str, Any]] = None) -> Tuple[bool, Any]:
        """Hermetic HTTP client for dystopi-utils internal API."""
        url = f"{self.utils_url}{endpoint}"
        auth_key = self._get_api_auth_key()
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "Minibots-DPoP-Auth/1.0",
        }
        if auth_key:
            headers["X-DPoP-Auth-Key"] = auth_key
            headers["X-Lertz-Key"] = auth_key
            headers["Authorization"] = f"Bearer {auth_key}"

        data_bytes = json.dumps(payload).encode("utf-8") if payload else None
        req = urllib.request.Request(url, data=data_bytes, headers=headers, method=method.upper())

        try:
            with urllib.request.urlopen(req, timeout=5.0) as resp:
                status = resp.status
                body = resp.read().decode("utf-8")
                try:
                    res_json = json.loads(body)
                    return True, res_json
                except Exception:
                    return True, body
        except urllib.error.HTTPError as e:
            try:
                err_body = json.loads(e.read().decode("utf-8"))
            except Exception:
                err_body = {"detail": str(e)}
            logger.warning(f"Utils API HTTPError {e.code} on {endpoint}: {err_body}")
            return False, err_body
        except Exception as e:
            logger.error(f"Utils API request failed on {endpoint}: {e}")
            return False, {"detail": str(e)}

    def on_notice(self, sender: str, target: str, message: str) -> None:
        """Handles IRC NOTICE events, specifically NickServ STATUS responses."""
        if not sender:
            return
        sender_lower = sender.lower().split("!")[0]
        if sender_lower == "nickserv":
            m = re.match(r"^STATUS\s+(\S+)\s+(\d+)", message.strip(), re.IGNORECASE)
            if m:
                nick = m.group(1).lower()
                status_code = int(m.group(2))
                with self.lock:
                    self.nickserv_cache[nick] = (status_code, time.time())
                    waiter = self.nickserv_waiters.get(nick)
                    if waiter:
                        waiter.set()

    def check_nickserv_identified(self, nick: str, hostmask: str = "", timeout: float = 5.0) -> Tuple[bool, str]:
        """
        Verifies whether user is identified with NickServ (STATUS code 3).
        Falls back to IRCv3 account cloak if NickServ does not respond within timeout.
        """
        clean_nick = nick.strip().lower()

        # 1. Fast check: Account cloak in hostmask (e.g. /user/account or @network/user/account)
        if hostmask:
            clean_mask = hostmask.lstrip(":")
            if "/user/" in clean_mask or ("@" in clean_mask and "/" in clean_mask.split("@", 1)[1]):
                return True, "Verified via IRCv3 account cloak"

        # 2. Check in-memory cache (5-minute TTL)
        now = time.time()
        with self.lock:
            if clean_nick in self.nickserv_cache:
                status, cached_time = self.nickserv_cache[clean_nick]
                if now - cached_time < 300:
                    if status == 3:
                        return True, "NickServ status 3 (identified)"
                    elif status in (1, 2):
                        return False, f"NickServ status {status} (not identified)"

        # 3. Query NickServ if bot can send messages
        if self.bot and hasattr(self.bot, "send_msg"):
            waiter = threading.Event()
            with self.lock:
                self.nickserv_waiters[clean_nick] = waiter

            try:
                self.bot.send_msg("NickServ", f"STATUS {nick}")
                signaled = waiter.wait(timeout=timeout)
                if signaled:
                    with self.lock:
                        status, _ = self.nickserv_cache.get(clean_nick, (0, 0))
                    if status == 3:
                        return True, "NickServ status 3 (identified)"
                    return False, f"NickServ status {status} (not identified)"
            except Exception as e:
                logger.warning(f"NickServ query failed: {e}")
            finally:
                with self.lock:
                    self.nickserv_waiters.pop(clean_nick, None)

        # 4. Timeout fallback: Allow if hostmask exists or network has no NickServ
        logger.info(f"NickServ verification timed out for {nick}; falling back to hostmask {hostmask}")
        return True, "Fallback to hostmask provenance (NickServ timeout)"

    def get_user_files(self, network: str, nick: str) -> Tuple[bool, Dict[str, Any]]:
        """Queries dystopi-utils for user uploads and share links."""
        return self._call_utils_api(f"/api/internal/user/{urllib.parse.quote(network)}/{urllib.parse.quote(nick)}/files")

    def create_user_share(
        self,
        network: str,
        nick: str,
        item_id: str,
        ttl_hours: int = 24,
        max_claims: int = 1,
        label: Optional[str] = None,
        passphrase: Optional[str] = None,
        notify_irc: bool = False,
        is_permanent: bool = False
    ) -> Tuple[bool, Dict[str, Any]]:
        """Calls dystopi-utils to create a knock share link on behalf of user."""
        payload = {
            "item_id": item_id,
            "ttl_hours": ttl_hours,
            "max_claims": max_claims,
            "label": label,
            "passphrase": passphrase,
            "notify_irc": notify_irc,
            "is_permanent": is_permanent
        }
        return self._call_utils_api(
            f"/api/internal/user/{urllib.parse.quote(network)}/{urllib.parse.quote(nick)}/share",
            method="POST",
            payload=payload
        )

    def revoke_user_share(self, network: str, nick: str, link_hash: str) -> Tuple[bool, Dict[str, Any]]:
        """Calls dystopi-utils to revoke a user share link."""
        return self._call_utils_api(
            f"/api/internal/user/{urllib.parse.quote(network)}/{urllib.parse.quote(nick)}/revoke",
            method="POST",
            payload={"link_hash": link_hash}
        )

    def get_user_inactivity(self, network: str, nick: str) -> Tuple[bool, Dict[str, Any]]:
        """Audits user activity and inactivity threshold."""
        return self._call_utils_api(f"/api/internal/user/{urllib.parse.quote(network)}/{urllib.parse.quote(nick)}/inactivity")

    def get_active_devices(self, nick: str) -> Tuple[int, List[Dict[str, Any]]]:
        """Queries dystopi-utils for active device count and details."""
        ok, res = self._call_utils_api(f"/api/internal/knock/devices/{urllib.parse.quote(nick)}")
        if ok and isinstance(res, dict):
            return res.get("active_devices_count", 0), res.get("devices", [])
        return 0, []

    def create_knock_invite(
        self,
        nick: str,
        network: str,
        channel: str,
        ttl_hours: int = 24
    ) -> Tuple[bool, Dict[str, Any]]:
        """Calls dystopi-utils internal knock creation API for a 30d user session."""
        payload = {
            "grant_type": "session",
            "item_id": "SESSION",
            "ttl_hours": max(1, min(720, ttl_hours)),
            "requester_nick": nick,
            "requester_network": network,
            "requester_channel": channel,
            "role": "user"
        }
        ok, res = self._call_utils_api("/api/internal/knock/create", method="POST", payload=payload)
        return ok, res if isinstance(res, dict) else {}

    def dispatch_pass_to_user(self, nick: str, res_data: Dict[str, Any], is_cli: bool = False) -> None:
        """Transmits one-time sanctuary pass to user exclusively via Private Message."""
        if not self.bot:
            return

        token = res_data.get("master_token", "")
        knock_url = res_data.get("full_knock_url") or f"{self.utils_url}/k/{token}"
        parsed_target = urllib.parse.urlparse(self.utils_url).netloc or self.utils_url

        if is_cli:
            self.bot.send_msg(nick, f"[DPoP] === HEADLESS CLI SANCTUARY PASS ===")
            self.bot.send_msg(nick, f"[DPoP] Target: {parsed_target} | Role: USER | Session: 30 Days")
            self.bot.send_msg(nick, f"[DPoP] Execute this claim command in your terminal within 24 hours:")
            self.bot.send_msg(nick, f"curl -sS -X POST \"{self.utils_url}/api/k/{token}/claim-cli\"")
            self.bot.send_msg(nick, f"[DPoP] This command will issue your 30-day bearer CLI token (lz_cli_...) and export aliases.")
        else:
            self.bot.send_msg(nick, f"[DPoP] === ZERO-TRUST SANCTUARY PASS ===")
            self.bot.send_msg(nick, f"[DPoP] Target: {parsed_target} | Role: USER | Session: 30 Days")
            self.bot.send_msg(nick, f"[DPoP] One-Time Knock URL (Unclaimed link expires in 24h):")
            self.bot.send_msg(nick, f"{knock_url}")
            self.bot.send_msg(nick, f"[DPoP] Open in your browser to establish hardware-bound WebCrypto ECDSA P-256 session.")
            self.bot.send_msg(nick, f"[DPoP] Includes 100% PoW bypass and supports up to 5 concurrent active devices.")

    def on_privmsg(self, target: str, sender_nick: str, message: str, hostmask: str = "") -> None:
        """Handles in-channel and PM authentication requests."""
        text = message.strip()
        bot_nick = getattr(self.bot, "nick", "minibot").lower()
        is_dm = not target.startswith("#") or (target.lower() == bot_nick)

        # Parse command tokens
        parts = text.split()
        if not parts:
            return

        cmd = parts[0].lower().lstrip("!.")

        # --- PM Commands ---
        if is_dm:
            if cmd in ("auth", "pass", "knock"):
                is_cli = "cli" in [p.lower() for p in parts[1:]]
                is_confirm = "confirm" in [p.lower() for p in parts[1:]]
                self._handle_auth_flow(sender_nick, sender_nick, hostmask, is_cli=is_cli, is_confirm=is_confirm, is_dm=True)
            elif cmd == "devices":
                self._handle_devices_command(sender_nick, sender_nick, is_dm=True)
            elif cmd == "files":
                self._handle_files_command(sender_nick, sender_nick, hostmask, is_dm=True)
            elif cmd == "share":
                self._handle_share_command(sender_nick, sender_nick, hostmask, parts[1:], is_dm=True)
            elif cmd == "status":
                self._handle_status_command(sender_nick, sender_nick, hostmask, is_dm=True)
            elif cmd in ("help", "?"):
                self._handle_help_command(sender_nick, is_dm=True)
            return

        # --- In-Channel Commands ---
        if cmd in ("auth", "pass", "knock"):
            is_cli = "cli" in [p.lower() for p in parts[1:]]
            is_confirm = "confirm" in [p.lower() for p in parts[1:]]
            self._handle_auth_flow(target, sender_nick, hostmask, is_cli=is_cli, is_confirm=is_confirm, is_dm=False)
        elif cmd == "devices":
            self._handle_devices_command(target, sender_nick, is_dm=False)
        elif cmd == "files":
            self._handle_files_command(target, sender_nick, hostmask, is_dm=False)
        elif cmd == "share":
            self._handle_share_command(target, sender_nick, hostmask, parts[1:], is_dm=False)

    def _handle_auth_flow(
        self,
        reply_dest: str,
        sender_nick: str,
        hostmask: str,
        is_cli: bool = False,
        is_confirm: bool = False,
        is_dm: bool = False
    ) -> None:
        """Processes authentication request with anti-abuse, 5-device warnings, and channel receipts."""
        # 0. NickServ verification check
        is_identified, id_reason = self.check_nickserv_identified(sender_nick, hostmask)
        if not is_identified:
            msg = f"⚠️ NickServ authentication required: {id_reason}. Please identify with NickServ (`/msg NickServ IDENTIFY <password>`) before requesting sanctuary access."
            if is_dm:
                self.bot.send_msg(sender_nick, f"[DPoP] {msg}")
            else:
                self.bot.send_msg(reply_dest, f"[DPoP] {sender_nick}: {msg}")
            return

        # Inactivity notice check
        network_name = getattr(self.bot, "network", "irc")
        inact_ok, inact_data = self.get_user_inactivity(network_name, sender_nick)
        if inact_ok and isinstance(inact_data, dict) and inact_data.get("is_warning"):
            rem = inact_data.get("days_remaining", 0)
            policy = inact_data.get("retention_policy", "4_months")
            warn_msg = f"⚠️ INACTIVITY NOTICE: Your vault data was scheduled for auto-purge in {rem} days under '{policy}' policy. Authenticating now has reset your inactivity timer!"
            self.bot.send_msg(sender_nick, f"[DPoP] {warn_msg}")

        cloak = self._extract_cloak(sender_nick, hostmask)
        now = time.time()

        # 1. Anti-abuse: quiet drop rapid repeats (<60s)
        last_req = self.hostmask_history.get(cloak, 0)
        if now - last_req < 60 and not is_confirm:
            logger.debug(f"[DPoP] Dropping rapid repeat auth request from {cloak} (<60s)")
            return

        # 2. Anti-abuse: 1 pass per 24 hours per cloak/hostmask
        if now - last_req < 86400 and not is_confirm:
            remaining_hours = round((86400 - (now - last_req)) / 3600.0, 1)
            msg = f"⚠️ A pass was already requested within the last 24h for your identity/cloak ({cloak}). Next request eligible in {remaining_hours}h."
            if is_dm:
                self.bot.send_msg(sender_nick, f"[DPoP] {msg}")
            else:
                self.bot.send_msg(reply_dest, f"[DPoP] {sender_nick}: {msg}")
            return

        # 3. Multi-device check (5-device ceiling with warning)
        count, _ = self.get_active_devices(sender_nick)
        if count >= 5:
            pending_time = self.pending_confirmations.get(sender_nick, 0)
            if not is_confirm and (now - pending_time > 300):
                self.pending_confirmations[sender_nick] = now
                warn_msg = (
                    f"⚠️ You currently have {count}/5 active devices. Requesting another pass will evict your oldest "
                    f"device session. To proceed, reply with '!auth confirm' or send 'auth confirm' in PM. Use '!devices' to inspect slots."
                )
                if is_dm:
                    self.bot.send_msg(sender_nick, f"[DPoP] {warn_msg}")
                else:
                    self.bot.send_msg(reply_dest, f"[DPoP] {sender_nick}: {warn_msg}")
                return
            else:
                # Confirmed
                self.pending_confirmations.pop(sender_nick, None)

        # 4. Mode evaluation (Auto vs Moderated)
        mode = self.channel_modes.get(reply_dest.lower(), self.global_mode)

        if mode == "auto":
            network_name = getattr(self.bot, "network", "irc")
            ok, res = self.create_knock_invite(sender_nick, network_name, reply_dest)
            if ok and "master_token" in res:
                self.hostmask_history[cloak] = now
                self.dispatch_pass_to_user(sender_nick, res, is_cli=is_cli)
                if not is_dm:
                    self.bot.send_msg(reply_dest, f"[DPoP] Pass dispatched to your PM, {sender_nick}.")
            else:
                err = res.get("detail", "Unknown server error")
                self.bot.send_msg(reply_dest if not is_dm else sender_nick, f"[DPoP] Error generating pass: {err}")
            return

        # 5. Moderated Mode (Ticket creation & Ring 0 notification)
        with self.lock:
            # Check review queue cap (max 10 pending per channel)
            chan_pending = [r for r in self.pending_requests.values() if r["channel"].lower() == reply_dest.lower()]
            if len(chan_pending) >= 10:
                msg = "[DPoP] Channel review queue is full (10 pending requests). Please wait for operators to process."
                self.bot.send_msg(reply_dest if not is_dm else sender_nick, msg)
                return

            req_id = self.ticket_counter
            self.ticket_counter += 1
            network_name = getattr(self.bot, "network", "irc")
            self.pending_requests[req_id] = {
                "id": req_id,
                "nick": sender_nick,
                "network": network_name,
                "channel": reply_dest,
                "hostmask": hostmask or cloak,
                "cloak": cloak,
                "is_cli": is_cli,
                "device_count": count,
                "timestamp": now,
            }

        self.hostmask_history[cloak] = now

        # Polite channel receipt
        if not is_dm:
            self.bot.send_msg(reply_dest, f"[DPoP] Request #{req_id} submitted for moderation. An operator will review your request shortly.")
        else:
            self.bot.send_msg(sender_nick, f"[DPoP] Request #{req_id} submitted for moderation. You will receive your pass via PM once approved.")

        # Dispatch Ring 0 notification to #minibots
        alert = (
            f"🔔 [DPoP REQ #{req_id}] {sender_nick} ({cloak}) on {network_name}:{reply_dest} "
            f"requests access (Device {count}/5). !dpop approve {req_id} [days] | !dpop deny {req_id} [reason]"
        )
        if self.bot and hasattr(self.bot, "raw_send_callback") and self.bot.raw_send_callback:
            try:
                self.bot.raw_send_callback(self.bot.nick, f"PRIVMSG #minibots :{alert}")
            except Exception as e:
                logger.error(f"[DPoP] Failed to alert #minibots: {e}")

    def _handle_devices_command(self, reply_dest: str, sender_nick: str, is_dm: bool = False) -> None:
        """Reports active device grants to user."""
        count, devices = self.get_active_devices(sender_nick)
        dest = sender_nick if is_dm else reply_dest
        prefix = f"[DPoP] " if is_dm else f"[DPoP] {sender_nick}: "

        if count == 0:
            self.bot.send_msg(dest, f"{prefix}You have 0 active devices registered.")
            return

        self.bot.send_msg(dest, f"{prefix}You have {count}/5 active device slots in use.")
        if is_dm:
            for i, dev in enumerate(devices, 1):
                label = dev.get("device_label", "Browser")
                expires = dev.get("expires_at", "N/A")
                net = dev.get("claimant_net", "ROAMING")
                self.bot.send_msg(sender_nick, f"  Slot {i}: {label} | Net: {net} | Expires: {expires}")

    def _handle_status_command(self, reply_dest: str, sender_nick: str, hostmask: str, is_dm: bool = False) -> None:
        """Reports rate limit and session eligibility status."""
        cloak = self._extract_cloak(sender_nick, hostmask)
        last_req = self.hostmask_history.get(cloak, 0)
        now = time.time()
        elapsed = now - last_req

        dest = sender_nick if is_dm else reply_dest
        prefix = f"[DPoP] " if is_dm else f"[DPoP] {sender_nick}: "

        count, _ = self.get_active_devices(sender_nick)
        if elapsed < 86400:
            rem = round((86400 - elapsed) / 3600.0, 1)
            self.bot.send_msg(dest, f"{prefix}Active devices: {count}/5 | Next pass request eligible in {rem}h (Cloak: {cloak})")
        else:
            self.bot.send_msg(dest, f"{prefix}Active devices: {count}/5 | Eligible to request a sanctuary pass now.")

    def _handle_files_command(self, reply_dest: str, sender_nick: str, hostmask: str, is_dm: bool = False) -> None:
        """Handles !files ChatOps command to inspect vault items and links."""
        is_identified, id_reason = self.check_nickserv_identified(sender_nick, hostmask)
        if not is_identified:
            self.bot.send_msg(reply_dest if not is_dm else sender_nick, f"[DPoP] {sender_nick}: NickServ identification required ({id_reason}).")
            return

        network_name = getattr(self.bot, "network", "irc")
        ok, data = self.get_user_files(network_name, sender_nick)
        if not ok or not isinstance(data, dict):
            self.bot.send_msg(reply_dest if not is_dm else sender_nick, f"[DPoP] Error fetching vault items: {data.get('detail', 'Server error')}")
            return

        items = data.get("items", [])
        count = data.get("count", len(items))

        parsed_target = urllib.parse.urlparse(self.utils_url).netloc or self.utils_url
        if not is_dm:
            self.bot.send_msg(reply_dest, f"[DPoP] {sender_nick}: You have {count} items in your vault. Detailed listing dispatched to PM. Web Vault: {self.utils_url}/my-files")

        self.bot.send_msg(sender_nick, f"[DPoP] === YOUR VAULT ITEMS ({count} total) ===")
        self.bot.send_msg(sender_nick, f"[DPoP] CRT Web Vault: {self.utils_url}/my-files")
        if not items:
            self.bot.send_msg(sender_nick, f"[DPoP] No items found. Upload files or create pastes at {parsed_target}.")
            return

        for it in items[:5]:
            item_id = it.get("id")
            name = it.get("display_name") or it.get("original_filename") or item_id
            itype = (it.get("type") or "file").upper()
            exp = "Permanent" if it.get("is_permanent") else (it.get("expires_at") or "Unknown")
            links = it.get("share_links", [])
            active_links = sum(1 for l in links if l.get("status") == "active")
            self.bot.send_msg(sender_nick, f"  #{item_id} [{itype}] {name} | Expiry: {exp} | Active Links: {active_links}")

        if len(items) > 5:
            self.bot.send_msg(sender_nick, f"  ... and {len(items) - 5} more items. View all at {self.utils_url}/my-files")
        self.bot.send_msg(sender_nick, f"[DPoP] Use '!share <item_id> [ttl_hours] [single|multi]' to issue knock links.")

    def _handle_share_command(self, reply_dest: str, sender_nick: str, hostmask: str, args: List[str], is_dm: bool = False) -> None:
        """Handles !share <item_id> [ttl] [single|multi] and !share revoke <link_hash>."""
        if not args:
            self.bot.send_msg(reply_dest if not is_dm else sender_nick, "[DPoP] Usage: !share <item_id> [ttl_hours] [single|multi] | !share revoke <link_hash>")
            return

        is_identified, id_reason = self.check_nickserv_identified(sender_nick, hostmask)
        if not is_identified:
            self.bot.send_msg(reply_dest if not is_dm else sender_nick, f"[DPoP] {sender_nick}: NickServ identification required ({id_reason}).")
            return

        network_name = getattr(self.bot, "network", "irc")

        if args[0].lower() == "revoke":
            if len(args) < 2:
                self.bot.send_msg(reply_dest if not is_dm else sender_nick, "[DPoP] Usage: !share revoke <link_hash>")
                return
            link_hash = args[1]
            ok, res = self.revoke_user_share(network_name, sender_nick, link_hash)
            if ok:
                msg = f"Link {link_hash} revoked. All active viewers kicked."
            else:
                msg = f"Revocation failed: {res.get('detail', 'Not found')}"
            self.bot.send_msg(reply_dest if not is_dm else sender_nick, f"[DPoP] {msg}")
            return

        item_id = args[0]
        ttl_hours = 24
        max_claims = 1

        for extra in args[1:]:
            extra_lower = extra.lower()
            if extra_lower in ("multi", "unlimited"):
                max_claims = -1
            elif extra_lower in ("single", "1"):
                max_claims = 1
            elif extra.isdigit():
                ttl_hours = int(extra)

        ok, res = self.create_user_share(
            network=network_name,
            nick=sender_nick,
            item_id=item_id,
            ttl_hours=ttl_hours,
            max_claims=max_claims,
            notify_irc=True
        )

        if not ok or not isinstance(res, dict) or "knock_url" not in res:
            err = res.get("detail", "Error creating link") if isinstance(res, dict) else "Error"
            self.bot.send_msg(reply_dest if not is_dm else sender_nick, f"[DPoP] Failed to create share link: {err}")
            return

        full_url = res.get("full_knock_url") or f"{self.utils_url}{res['knock_url']}"
        quota_str = "Unlimited" if max_claims == -1 else f"{max_claims} Claim"

        if not is_dm:
            self.bot.send_msg(reply_dest, f"[DPoP] Knock share link created for #{item_id}. Dispatched to your PM, {sender_nick}.")

        self.bot.send_msg(sender_nick, f"[DPoP] === KNOCK SHARE LINK CREATED ===")
        self.bot.send_nick_msg = getattr(self.bot, "send_msg", None)
        self.bot.send_msg(sender_nick, f"[DPoP] Item: #{item_id} | TTL: {ttl_hours}h | Claims: {quota_str}")
        self.bot.send_msg(sender_nick, f"[DPoP] Knock URL: {full_url}")
        self.bot.send_msg(sender_nick, f"[DPoP] Recipients of this link can access #{item_id} and ONLY #{item_id}. All other pages return 444.")

    def _handle_help_command(self, reply_dest: str, is_dm: bool = False) -> None:
        """Sends command documentation to user."""
        dest = reply_dest
        self.bot.send_msg(dest, "[DPoP] === Dystopi Sanctuary Authentication Commands ===")
        self.bot.send_msg(dest, "  !auth           - Request 30-day Web browser sanctuary knock pass")
        self.bot.send_msg(dest, "  !auth cli       - Request 30-day headless terminal CLI token (curl)")
        self.bot.send_msg(dest, "  !auth confirm   - Confirm pass request and evict oldest device if at 5/5 slots")
        self.bot.send_msg(dest, "  !devices        - View active bound device sessions and remaining slots")
        self.bot.send_msg(dest, "  !files          - Inspect your vault uploads and active share links")
        self.bot.send_msg(dest, "  !share <id> [h] [s|m] - Issue knock link for item (e.g. !share abc 24 multi)")
        self.bot.send_msg(dest, "  !share revoke <hash> - Revoke a knock share link instantly")
        self.bot.send_msg(dest, "  !status         - Check cooldown and request eligibility status")

    # --- Ring 0 Admin Command Interface (Called by Logic Daemon dispatcher) ---

    def handle_admin_cmd(self, network: str, target: str, sender: str, args: List[str]) -> str:
        """Handles administrative review commands executed in Ring 0 (#minibots)."""
        if not args:
            return "Usage: !dpop <list|approve|deny|mode|revoke> [args...]"

        sub = args[0].lower()

        if sub in ("list", "ls"):
            with self.lock:
                if not self.pending_requests:
                    return "[DPoP] No pending access requests awaiting review."
                lines = ["[DPoP] Pending Access Requests:"]
                now = time.time()
                for req in self.pending_requests.values():
                    elapsed = int(now - req["timestamp"])
                    cli_tag = " (CLI)" if req.get("is_cli") else ""
                    lines.append(
                        f"  #{req['id']} {req['nick']}{cli_tag} ({req['network']}:{req['channel']}, dev: {req['device_count']}/5, {elapsed}s ago)"
                    )
                return "\n".join(lines)

        elif sub == "approve":
            if len(args) < 2:
                return "Usage: !dpop approve <ticket_id> [days]"
            try:
                ticket_id = int(args[1])
            except ValueError:
                return "❌ Invalid ticket ID."

            days = 30
            if len(args) >= 3:
                try:
                    days = int(args[2])
                except ValueError:
                    days = 30

            with self.lock:
                ticket = self.pending_requests.pop(ticket_id, None)

            if not ticket:
                return f"❌ Request #{ticket_id} not found or already processed."

            nick = ticket["nick"]
            net = ticket["network"]
            chan = ticket["channel"]
            is_cli = ticket.get("is_cli", False)

            ok, res = self.create_knock_invite(nick, net, chan, ttl_hours=24)
            if ok and "master_token" in res:
                self.dispatch_pass_to_user(nick, res, is_cli=is_cli)
                if self.bot and chan.startswith("#"):
                    self.bot.send_msg(chan, f"[DPoP] Access request #{ticket_id} approved. Pass dispatched to your PM, {nick}.")
                return f"✅ [DPoP] Approved request #{ticket_id} for {nick} ({days}d session issued)."
            else:
                err = res.get("detail", "API error")
                return f"❌ [DPoP] Failed to generate knock pass: {err}"

        elif sub == "deny":
            if len(args) < 2:
                return "Usage: !dpop deny <ticket_id> [reason]"
            try:
                ticket_id = int(args[1])
            except ValueError:
                return "❌ Invalid ticket ID."

            reason = " ".join(args[2:]) if len(args) >= 3 else "Request declined by operator."

            with self.lock:
                ticket = self.pending_requests.pop(ticket_id, None)

            if not ticket:
                return f"❌ Request #{ticket_id} not found."

            nick = ticket["nick"]
            if self.bot:
                self.bot.send_msg(nick, f"[DPoP] Your access request #{ticket_id} was denied by an operator. Reason: {reason}")
            return f"🛑 [DPoP] Denied request #{ticket_id} for {nick}."

        elif sub == "mode":
            if len(args) < 2:
                return f"[DPoP] Global mode: {self.global_mode.upper()} | Overrides: {len(self.channel_modes)}"

            if len(args) == 2:
                # Setting global mode
                new_mode = args[1].lower()
                if new_mode in ("auto", "moderated"):
                    self.global_mode = new_mode
                    return f"⚙️ [DPoP] Global dispatch mode set to: {new_mode.upper()}"
                return "Usage: !dpop mode [channel] <auto|moderated>"

            chan = args[1]
            new_mode = args[2].lower()
            if new_mode in ("auto", "moderated"):
                self.channel_modes[chan.lower()] = new_mode
                return f"⚙️ [DPoP] Mode for {chan} set to: {new_mode.upper()}"
            return "Usage: !dpop mode [channel] <auto|moderated>"

        elif sub == "revoke":
            if len(args) < 2:
                return "Usage: !dpop revoke <nick|master_token|bound_jkt>"
            target = args[1]
            ok, res = self._call_utils_api("/api/internal/knock/revoke", method="POST", payload={"target": target})
            if ok:
                return f"🗑️ [DPoP] Revocation issued for '{target}'."
            return f"❌ [DPoP] Revocation failed: {res.get('detail', 'Unknown error')}"

        return "Usage: !dpop <list|approve|deny|mode|revoke> [args...]"
