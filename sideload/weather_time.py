# SPDX-License-Identifier: AGPL-3.0-or-later
"""
weather_time.py — Live Weather & World Clock Module for Dystopi Minibots.
Supports wttr.in weather forecasts and UTC/timezone lookups.
"""

import datetime
import logging
import urllib.parse
import urllib.request
from typing import Optional

from sopel import module

logger = logging.getLogger("WeatherTime")

# SSRF Guard
try:
    from core.guard import build_safe_opener
except ImportError:
    def build_safe_opener():
        return urllib.request.build_opener()


@module.commands("w", "weather")
def weather_query(bot, trigger):
    """Fetches live weather conditions from wttr.in."""
    location = trigger.group(2)
    loc_query = urllib.parse.quote(location.strip()) if location else ""
    url = f"https://wttr.in/{loc_query}?format=3"

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "curl/8.0.0"})
        with urllib.request.urlopen(req, timeout=4.0) as resp:
            data = resp.read().decode("utf-8").strip()
            if data and not data.startswith("<"):
                bot.say(f"⛅ [Weather] {data}")
            else:
                bot.reply("❌ Weather data currently unavailable.")
    except Exception as e:
        logger.debug(f"wttr.in query failed: {e}")
        bot.reply("❌ Failed to fetch weather report.")


@module.commands("time", "tz")
def time_query(bot, trigger):
    """Displays current UTC time and offset."""
    loc = (trigger.group(2) or "").strip().lower()
    now_utc = datetime.datetime.now(datetime.timezone.utc)

    if not loc or loc == "utc":
        bot.say(f"🕒 [UTC Time] {now_utc.strftime('%Y-%m-%d %H:%M:%S UTC')}")
        return

    # Basic common timezone offsets
    tz_offsets = {
        "est": -5, "edt": -4,
        "cst": -6, "cdt": -5,
        "mst": -7, "mdt": -6,
        "pst": -8, "pdt": -7,
        "gmt": 0, "bst": 1,
        "cet": 1, "cest": 2,
        "eet": 2, "eest": 3,
        "msk": 3, "jst": 9,
        "aest": 10, "aedt": 11,
        "nzst": 12, "nzdt": 13
    }

    if loc in tz_offsets:
        offset_hours = tz_offsets[loc]
        target_tz = datetime.timezone(datetime.timedelta(hours=offset_hours))
        target_time = now_utc.astimezone(target_tz)
        bot.say(f"🕒 [{loc.upper()} Time] {target_time.strftime('%Y-%m-%d %H:%M:%S')} (UTC{offset_hours:+d})")
        return

    bot.say(f"🕒 [UTC Time] {now_utc.strftime('%Y-%m-%d %H:%M:%S UTC')} (Specify timezone e.g. !time est, !time cet, !time jst)")
