# SPDX-License-Identifier: AGPL-3.0-or-later
"""
client.py — Google Jules REST API Client (v1alpha) with Multi-Key Pool Rotation.
"""

import json
import logging
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Union

logger = logging.getLogger("JulesClient")

BASE_URL = "https://jules.googleapis.com/v1alpha"


class JulesClient:
    """REST Client for Google Jules Agentic Coding API with multi-key pool rotation."""
    def __init__(self, api_keys: Optional[Union[str, List[str]]] = None, api_key: Optional[str] = None):
        target = api_keys if api_keys is not None else api_key
        if isinstance(target, str):
            self.api_keys = [k.strip() for k in target.split(",") if k.strip()]
        elif isinstance(target, (list, tuple)):
            self.api_keys = [str(k).strip() for k in target if k and str(k).strip()]
        else:
            self.api_keys = []
        self._key_index = 0

    @property
    def api_key(self) -> str:
        if not self.api_keys:
            return ""
        return self.api_keys[self._key_index % len(self.api_keys)]

    def rotate_key(self) -> str:
        if self.api_keys:
            self._key_index = (self._key_index + 1) % len(self.api_keys)
            logger.info(f"Rotated Jules API key to index {self._key_index % len(self.api_keys)}")
            return self.api_key
        return ""

    def _request(self, method: str, path: str, payload: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        if not self.api_keys:
            logger.error("Jules API request failed: No API keys configured")
            return None

        attempts = len(self.api_keys)
        for attempt in range(attempts):
            curr_key = self.api_key
            url = f"{BASE_URL}/{path.lstrip('/')}"
            headers = {
                "Content-Type": "application/json",
                "X-Goog-Api-Key": curr_key
            }
            data = json.dumps(payload).encode("utf-8") if payload else None
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            try:
                with urllib.request.urlopen(req, timeout=20.0) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                # If rate limited (429) or quota exceeded (403), rotate to next key in pool
                if e.code in (429, 403) and attempts > 1:
                    logger.warning(f"Jules API returned HTTP {e.code} on key index {self._key_index % len(self.api_keys)}. Rotating key...")
                    self.rotate_key()
                    continue
                logger.error(f"Jules API request {method} {path} failed (HTTP {e.code}): {e}")
                return None
            except Exception as e:
                logger.error(f"Jules API request {method} {path} failed: {e}")
                return None
        return None

    def create_session(self, prompt: str, repo: str, branch: str, title: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Creates a new Jules autonomous coding session."""
        owner, name = repo.split("/")
        payload = {
            "prompt": prompt,
            "title": title or prompt[:50],
            "sourceContext": {
                "githubRepo": {
                    "owner": owner,
                    "repo": name,
                    "branch": branch
                }
            }
        }
        return self._request("POST", "sessions", payload)

    def get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Retrieves session state."""
        return self._request("GET", f"sessions/{session_id}")

    def list_activities(self, session_id: str, page_size: int = 50) -> List[Dict[str, Any]]:
        """Lists activities and step events for a session."""
        res = self._request("GET", f"sessions/{session_id}/activities?pageSize={page_size}")
        if res:
            return res.get("activities", [])
        return []

    def approve_plan(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Approves generated execution plan."""
        return self._request("POST", f"sessions/{session_id}:approvePlan", {})

    def send_message(self, session_id: str, message: str) -> Optional[Dict[str, Any]]:
        """Sends continuation message to the session."""
        return self._request("POST", f"sessions/{session_id}:sendMessage", {"message": message})
