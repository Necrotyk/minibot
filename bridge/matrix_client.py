# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

"""
matrix_client.py — Optional Lightweight Matrix Synapse REST API Client.
"""

import json
import logging
import urllib.request
from typing import Any, Dict, Optional

logger = logging.getLogger("MatrixClient")


class MatrixClient:
    """Provides REST event dispatching to Matrix Synapse."""
    def __init__(self, homeserver_url: str, user_id: str, access_token: str):
        self.homeserver = homeserver_url.rstrip("/")
        self.user_id = user_id
        self.access_token = access_token

    def send_room_message(self, room_id: str, body: str) -> bool:
        """Sends a text message to target Matrix room."""
        if not self.access_token or not self.homeserver:
            return False

        txn_id = int(time.time() * 1000)
        url = f"{self.homeserver}/_matrix/client/v3/rooms/{room_id}/send/m.room.message/{txn_id}"
        payload = {
            "msgtype": "m.text",
            "body": body
        }
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json"
        }

        try:
            req = urllib.request.Request(
                url,
                data=json.dumps(payload).encode("utf-8"),
                headers=headers,
                method="PUT"
            )
            with urllib.request.urlopen(req, timeout=5.0) as resp:
                return resp.status == 200
        except Exception as e:
            logger.debug(f"Matrix send failed: {e}")
            return False
