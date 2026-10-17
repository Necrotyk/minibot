# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

"""
telemetry.py — SQLite WAL Telemetry & Token Usage Tracking for Minibots.
"""

import logging
import os
import sqlite3
import time
from functools import wraps
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("Telemetry")


def sqlite_retry(max_retries: int = 5, initial_backoff: float = 0.05):
    """Exponential backoff decorator for SQLite database concurrency."""
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            backoff = initial_backoff
            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except sqlite3.OperationalError as e:
                    if "locked" in str(e).lower() or "busy" in str(e).lower():
                        if attempt == max_retries - 1:
                            raise
                        time.sleep(backoff)
                        backoff *= 2
                    else:
                        raise
        return wrapper
    return decorator


class TelemetryEngine:
    """Records LLM token counts, request durations, and quota consumption with automatic rolling retention."""
    def __init__(self, db_path: Optional[str] = None):
        if not db_path:
            data_dir = Path(__file__).resolve().parents[1] / "data"
            data_dir.mkdir(parents=True, exist_ok=True)
            db_path = str(data_dir / "telemetry.db")

        self.db_path = db_path
        self._init_db()
        try:
            self.prune_older_than(days=30)
        except Exception:
            pass

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA busy_timeout = 30000;")
        conn.execute("PRAGMA synchronous = NORMAL;")
        conn.row_factory = sqlite3.Row
        return conn

    @sqlite_retry()
    def _init_db(self) -> None:
        conn = self._get_conn()
        try:
            with conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS token_usage (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        timestamp REAL NOT NULL,
                        source_bot TEXT NOT NULL,
                        model TEXT NOT NULL,
                        provider TEXT NOT NULL,
                        tokens_in INTEGER DEFAULT 0,
                        tokens_out INTEGER DEFAULT 0,
                        latency_ms REAL DEFAULT 0.0,
                        channel TEXT,
                        user_nick TEXT
                    );
                """)
        finally:
            conn.close()

    @sqlite_retry()
    def record_usage(
        self,
        source_bot: str,
        model: str,
        provider: str,
        tokens_in: int,
        tokens_out: int,
        latency_ms: float,
        channel: Optional[str] = None,
        user_nick: Optional[str] = None
    ) -> None:
        """Appends a token consumption record."""
        conn = self._get_conn()
        try:
            with conn:
                conn.execute("""
                    INSERT INTO token_usage (
                        timestamp, source_bot, model, provider, tokens_in, tokens_out, latency_ms, channel, user_nick
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
                """, (time.time(), source_bot, model, provider, tokens_in, tokens_out, latency_ms, channel, user_nick))
        except Exception as e:
            logger.error(f"Failed to record token telemetry: {e}")
        finally:
            conn.close()

    @sqlite_retry()
    def get_summary(self, hours: int = 24) -> Dict[str, Any]:
        """Retrieves aggregated token metrics over the specified time window."""
        cutoff = time.time() - (hours * 3600)
        conn = self._get_conn()
        try:
            cur = conn.execute("""
                SELECT 
                    COUNT(*) as total_requests,
                    SUM(tokens_in) as total_in,
                    SUM(tokens_out) as total_out,
                    AVG(latency_ms) as avg_latency
                FROM token_usage
                WHERE timestamp >= ?;
            """, (cutoff,))
            row = cur.fetchone()
            if row:
                return {
                    "requests": row["total_requests"] or 0,
                    "tokens_in": row["total_in"] or 0,
                    "tokens_out": row["total_out"] or 0,
                    "avg_latency_ms": round(row["avg_latency"] or 0.0, 2)
                }
            return {"requests": 0, "tokens_in": 0, "tokens_out": 0, "avg_latency_ms": 0.0}
        finally:
            conn.close()

    @sqlite_retry()
    def prune_older_than(self, days: int = 30) -> int:
        """Deletes telemetry records older than specified number of days."""
        cutoff = time.time() - (days * 86400)
        conn = self._get_conn()
        try:
            with conn:
                cur = conn.execute("DELETE FROM token_usage WHERE timestamp < ?;", (cutoff,))
                deleted = cur.rowcount
                if deleted > 0:
                    logger.info(f"Pruned {deleted} telemetry records older than {days} days.")
                return deleted
        finally:
            conn.close()
