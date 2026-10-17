# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

"""
crypto_calc.py — Cryptographic, Base Conversion & Calculation Module for Dystopi Minibots.
Supports Base64, Hex, Hashes (MD5/SHA1/SHA256), JWT inspection, UUID, and unit conversion.
"""

import base64
import hashlib
import json
import logging
import uuid
from typing import Optional

from sopel import module

logger = logging.getLogger("CryptoCalc")


@module.commands("hex")
def to_hex(bot, trigger):
    """Converts plain text to hex representation."""
    text = trigger.group(2)
    if not text:
        bot.reply("Usage: !hex <text>")
        return
    bot.say(f"🔢 [HEX] {text.encode('utf-8').hex()}")


@module.commands("unhex")
def from_hex(bot, trigger):
    """Converts hex back to plain text."""
    hex_str = (trigger.group(2) or "").replace(" ", "").replace("0x", "")
    if not hex_str:
        bot.reply("Usage: !unhex <hex_string>")
        return
    try:
        raw = bytes.fromhex(hex_str).decode("utf-8", errors="replace")
        bot.say(f"🔤 [UNHEX] {raw}")
    except Exception as e:
        bot.reply(f"❌ [UNHEX] Invalid hex string: {e}")


@module.commands("b64", "base64")
def to_base64(bot, trigger):
    """Encodes plain text to Base64."""
    text = trigger.group(2)
    if not text:
        bot.reply("Usage: !b64 <text>")
        return
    encoded = base64.b64encode(text.encode("utf-8")).decode("ascii")
    bot.say(f"📦 [B64] {encoded}")


@module.commands("unb64", "unbase64")
def from_base64(bot, trigger):
    """Decodes Base64 to plain text."""
    b64_str = (trigger.group(2) or "").strip()
    if not b64_str:
        bot.reply("Usage: !unb64 <base64_string>")
        return
    try:
        # Pad if missing
        missing_padding = len(b64_str) % 4
        if missing_padding:
            b64_str += '=' * (4 - missing_padding)
        decoded = base64.b64decode(b64_str).decode("utf-8", errors="replace")
        bot.say(f"🔓 [UNB64] {decoded}")
    except Exception as e:
        bot.reply(f"❌ [UNB64] Invalid Base64: {e}")


@module.commands("hash")
def compute_hash(bot, trigger):
    """Computes cryptographic hash (md5, sha1, sha256, sha512)."""
    args = (trigger.group(2) or "").split(maxsplit=1)
    if len(args) < 2:
        bot.reply("Usage: !hash <md5|sha1|sha256|sha512> <text>")
        return
    algo, text = args[0].lower().strip(), args[1]
    if algo not in hashlib.algorithms_available:
        bot.reply(f"❌ Unsupported hash algorithm '{algo}'.")
        return
    try:
        h = hashlib.new(algo, text.encode("utf-8")).hexdigest()
        bot.say(f"🔑 [{algo.upper()}] {h}")
    except Exception as e:
        bot.reply(f"❌ Error computing hash: {e}")


@module.commands("jwt")
def inspect_jwt(bot, trigger):
    """Decodes header and payload claims from a JSON Web Token."""
    token = (trigger.group(2) or "").strip()
    if not token or token.count(".") < 2:
        bot.reply("Usage: !jwt <header.payload.signature>")
        return

    parts = token.split(".")
    try:
        def b64_url_decode(s):
            s += '=' * (-len(s) % 4)
            return json.loads(base64.urlsafe_b64decode(s).decode("utf-8"))

        header = b64_url_decode(parts[0])
        payload = b64_url_decode(parts[1])
        bot.say(f"🎫 [JWT Header] {json.dumps(header, separators=(',', ':'))}"[:380])
        bot.say(f"🎫 [JWT Claims] {json.dumps(payload, separators=(',', ':'))}"[:380])
    except Exception as e:
        bot.reply(f"❌ [JWT] Failed to decode token: {e}")


@module.commands("uuid")
def generate_uuid(bot, trigger):
    """Generates a random UUIDv4."""
    u = uuid.uuid4()
    bot.say(f"🆔 [UUIDv4] {u}")


@module.commands("convert")
def unit_convert(bot, trigger):
    """Converts units (e.g. !convert 100 c f, !convert 10 gb mb)."""
    args = (trigger.group(2) or "").split()
    if len(args) < 3:
        bot.reply("Usage: !convert <amount> <from_unit> <to_unit> (e.g. !convert 100 c f, !convert 10 gb mb)")
        return

    try:
        val = float(args[0])
    except ValueError:
        bot.reply("❌ Invalid amount.")
        return

    u_from = args[1].lower().strip()
    u_to = args[2].lower().strip()

    # Temperature
    if u_from in ("c", "celsius") and u_to in ("f", "fahrenheit"):
        res = (val * 9 / 5) + 32
        bot.say(f"🌡️ [TEMP] {val}°C = {res:.1f}°F")
        return
    elif u_from in ("f", "fahrenheit") and u_to in ("c", "celsius"):
        res = (val - 32) * 5 / 9
        bot.say(f"🌡️ [TEMP] {val}°F = {res:.1f}°C")
        return

    # Data sizes (bytes)
    multipliers = {
        "b": 1, "byte": 1, "bytes": 1,
        "kb": 1024, "kib": 1024,
        "mb": 1024**2, "mib": 1024**2,
        "gb": 1024**3, "gib": 1024**3,
        "tb": 1024**4, "tib": 1024**4
    }
    if u_from in multipliers and u_to in multipliers:
        in_bytes = val * multipliers[u_from]
        converted = in_bytes / multipliers[u_to]
        bot.say(f"💾 [DATA] {val} {u_from.upper()} = {converted:,.2f} {u_to.upper()}")
        return

    bot.reply(f"❌ Conversion from '{u_from}' to '{u_to}' not supported.")
