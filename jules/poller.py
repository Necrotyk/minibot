# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

"""
poller.py — Real-Time Activity Poller & Event Streaming for Jules.
"""

import logging
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Set

from jules.client import JulesClient

logger = logging.getLogger("JulesPoller")


class JulesPoller:
    """Streams activity logs and state changes from Google Jules to IRC."""
    def __init__(self, client: JulesClient, poll_interval: float = 10.0):
        self.client = client
        self.poll_interval = poll_interval
        self.active_sessions: Dict[str, Dict[str, Any]] = {}
        self.seen_activities: Dict[str, Set[str]] = {}
        self.callbacks: List[Callable[[str, str, Dict[str, Any]], None]] = []
        self._running = False
        self._thread: Optional[threading.Thread] = None

    def add_callback(self, cb: Callable[[str, str, Dict[str, Any]], None]) -> None:
        self.callbacks.append(cb)

    def track_session(self, session_id: str, label: str, target_chan: str) -> None:
        self.active_sessions[session_id] = {
            "label": label,
            "channel": target_chan,
            "status": "RUNNING",
            "start_time": time.time()
        }
        self.seen_activities[session_id] = set()

    def untrack_session(self, session_id: str) -> None:
        self.active_sessions.pop(session_id, None)
        self.seen_activities.pop(session_id, None)

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._poll_loop, daemon=True, name="JulesPollerThread")
        self._thread.start()

    def stop(self) -> None:
        self._running = False

    def _poll_loop(self) -> None:
        while self._running:
            for session_id, meta in list(self.active_sessions.items()):
                try:
                    activities = self.client.list_activities(session_id)
                    seen = self.seen_activities.setdefault(session_id, set())

                    for act in reversed(activities):
                        act_id = act.get("id", str(act))
                        if act_id not in seen:
                            seen.add(act_id)
                            self._emit(session_id, meta, act)

                    # Check overall session status
                    sess = self.client.get_session(session_id)
                    if sess:
                        state = sess.get("state")
                        if state in ["COMPLETED", "FAILED", "CANCELLED"]:
                            self._emit_final(session_id, meta, sess)
                            self.untrack_session(session_id)
                except Exception as e:
                    logger.debug(f"Error polling Jules session {session_id}: {e}")

            time.sleep(self.poll_interval)

    def _emit(self, session_id: str, meta: Dict[str, Any], activity: Dict[str, Any]) -> None:
        msg = activity.get("description") or activity.get("title") or "Activity update"
        for cb in self.callbacks:
            cb(session_id, meta["channel"], {"type": "activity", "label": meta["label"], "text": msg})

    def _emit_final(self, session_id: str, meta: Dict[str, Any], session_data: Dict[str, Any]) -> None:
        state = session_data.get("state", "COMPLETED")
        pr_url = session_data.get("pullRequest", {}).get("url", "")
        msg = f"[Jules] Session {meta['label']} {state}."
        if pr_url:
            msg += f" PR: {pr_url}"
        for cb in self.callbacks:
            cb(session_id, meta["channel"], {"type": "complete", "label": meta["label"], "text": msg, "pr_url": pr_url})
