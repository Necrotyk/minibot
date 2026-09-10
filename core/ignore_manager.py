# SPDX-License-Identifier: AGPL-3.0-or-later
"""
ignore_manager.py — Dynamic URL Ignore Engine & Persistent Filter.
Suppresses duplicate URL title previews from sibling bots, IRC bridges, and automated feeds.
"""

from __future__ import annotations

import fnmatch
import json
import logging
import threading
from pathlib import Path
from typing import List, Optional, Set

logger = logging.getLogger("IgnoreManager")

DEFAULT_KNOWN_BOTS: Set[str] = {
    "vulnavia",
    "sopel",
    "chanserv",
    "nickserv",
    "minibots",
    "toolz",
    "julesbot",
    "lertz",
    "lertz_dystopi",
    "lertz_admin",
    "lertz_bot",
    "gr_dystopi",
    "relay",
    "telebot",
    "matrix",
    "appservice",
}


class IgnoredNicksManager:
    """Thread-safe manager for ignored IRC nicks and wildcard glob patterns."""

    def __init__(self, state_file: Optional[Path | str] = None):
        if state_file:
            self.state_file = Path(state_file)
        else:
            data_dir = Path(__file__).resolve().parents[1] / "data"
            data_dir.mkdir(parents=True, exist_ok=True)
            self.state_file = data_dir / "ignored_url_nicks.json"

        self.lock = threading.RLock()
        self.ignored_patterns: Set[str] = set()
        self._load()

    def _load(self) -> None:
        """Loads ignored patterns from JSON storage."""
        with self.lock:
            if self.state_file.exists():
                try:
                    data = json.loads(self.state_file.read_text(encoding="utf-8"))
                    if isinstance(data, list):
                        self.ignored_patterns = {str(p).strip().lower() for p in data if str(p).strip()}
                        return
                except Exception as e:
                    logger.warning("Failed loading %s: %s. Using defaults.", self.state_file, e)
            self.ignored_patterns = set()

    def _save(self) -> None:
        """Atomically persists ignored patterns to JSON storage."""
        with self.lock:
            try:
                self.state_file.parent.mkdir(parents=True, exist_ok=True)
                tmp_file = self.state_file.with_suffix(".tmp")
                tmp_file.write_text(
                    json.dumps(sorted(list(self.ignored_patterns)), indent=2),
                    encoding="utf-8"
                )
                tmp_file.replace(self.state_file)
            except Exception as e:
                logger.error("Failed persisting ignored patterns to %s: %s", self.state_file, e)

    def add(self, pattern: str) -> bool:
        """Adds a nick or wildcard glob pattern (e.g. 'relay*' or 'bot_*')."""
        clean = pattern.strip().lower()
        if not clean:
            return False
        with self.lock:
            if clean in self.ignored_patterns:
                return False
            self.ignored_patterns.add(clean)
            self._save()
            return True

    def remove(self, pattern: str) -> bool:
        """Removes an ignored nick or wildcard glob pattern."""
        clean = pattern.strip().lower()
        with self.lock:
            if clean not in self.ignored_patterns:
                return False
            self.ignored_patterns.remove(clean)
            self._save()
            return True

    def list_ignored(self) -> List[str]:
        """Returns sorted list of explicitly configured patterns."""
        with self.lock:
            return sorted(list(self.ignored_patterns))

    def is_ignored(self, nick: str) -> bool:
        """Evaluates whether an incoming sender should be suppressed from URL previews."""
        if not nick:
            return False
        clean = nick.split("!")[0].strip().lower().lstrip("@+~&%")
        if clean in DEFAULT_KNOWN_BOTS or clean.endswith("bot") or clean.startswith("lertz"):
            return True

        with self.lock:
            for pat in self.ignored_patterns:
                if clean == pat or fnmatch.fnmatchcase(clean, pat):
                    return True

        return False


# Global singleton instance
_GLOBAL_IGNORE_MANAGER: Optional[IgnoredNicksManager] = None
_LOCK = threading.Lock()


def get_ignore_manager(state_file: Optional[Path | str] = None) -> IgnoredNicksManager:
    """Returns the shared IgnoredNicksManager singleton."""
    global _GLOBAL_IGNORE_MANAGER
    with _LOCK:
        if _GLOBAL_IGNORE_MANAGER is None:
            _GLOBAL_IGNORE_MANAGER = IgnoredNicksManager(state_file=state_file)
        return _GLOBAL_IGNORE_MANAGER
