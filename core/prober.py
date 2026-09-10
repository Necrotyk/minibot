# SPDX-License-Identifier: AGPL-3.0-or-later
"""
prober.py — Autonomous Kaggle GPU Health Prober & Latency Monitor.
"""

import logging
import threading
import time
from typing import Callable, Optional
import urllib.request
import json

logger = logging.getLogger("GPUProber")


class GPUProber:
    """Monitors Kaggle GPU worker latency over Cloudflare Tunnels."""
    def __init__(self, target_url: str, check_interval: float = 30.0, timeout: float = 1.5):
        self.target_url = target_url.rstrip("/")
        self.check_interval = check_interval
        self.timeout = timeout
        self.is_healthy = False
        self.last_latency_ms = 0.0
        self.last_check = 0.0
        self.on_state_change: Optional[Callable[[bool, str], None]] = None
        self._running = False
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        """Starts background probing loop."""
        if self._running or not self.target_url:
            return
        self._running = True
        self._thread = threading.Thread(target=self._probe_loop, daemon=True, name="GPUProberThread")
        self._thread.start()
        logger.info(f"Started GPU health prober for {self.target_url}")

    def stop(self) -> None:
        """Stops background probing loop."""
        self._running = False

    def probe_now(self) -> bool:
        """Performs a synchronous health probe against the target URL."""
        if not self.target_url:
            self.is_healthy = False
            return False

        probe_endpoint = f"{self.target_url}/health" if "/v1" not in self.target_url else f"{self.target_url.replace('/v1', '')}/health"
        start = time.perf_counter()
        try:
            req = urllib.request.Request(probe_endpoint, headers={"User-Agent": "Minibots-Prober/1.0"})
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                if resp.status == 200:
                    latency = (time.perf_counter() - start) * 1000.0
                    self.last_latency_ms = round(latency, 2)
                    was_healthy = self.is_healthy
                    self.is_healthy = True
                    self.last_check = time.time()
                    if not was_healthy and self.on_state_change:
                        self.on_state_change(True, f"Kaggle GPU online ({self.last_latency_ms}ms)")
                    return True
        except Exception as e:
            was_healthy = self.is_healthy
            self.is_healthy = False
            self.last_check = time.time()
            if was_healthy and self.on_state_change:
                self.on_state_change(False, f"Kaggle GPU offline/unreachable: {e}")

        return False

    def _probe_loop(self) -> None:
        while self._running:
            self.probe_now()
            time.sleep(self.check_interval)
