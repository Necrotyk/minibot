# SPDX-License-Identifier: AGPL-3.0-or-later
"""
logger.py — ANSI Colored Terminal, Systemd Journal & Rotating File Logger with Activity Ring Buffer.
"""

import collections
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import sys
import threading
import time
from typing import List, Optional


class ColoredFormatter(logging.Formatter):
    """ANSI color log formatter for terminal and journalctl debugging."""
    LEVEL_COLORS = {
        logging.DEBUG: "\033[36m",      # Cyan
        logging.INFO: "\033[32m",       # Green
        logging.WARNING: "\033[33m",    # Yellow
        logging.ERROR: "\033[31m",      # Red
        logging.CRITICAL: "\033[1;31m", # Bold Red
    }
    RESET = "\033[0m"
    GREY = "\033[90m"
    PURPLE = "\033[35m"

    def format(self, record):
        level_color = self.LEVEL_COLORS.get(record.levelno, self.RESET)
        time_str = self.formatTime(record, "%Y-%m-%d %H:%M:%S")
        msg = record.getMessage()
        return f"{self.GREY}{time_str}{self.RESET} [{level_color}{record.levelname:<7}{self.RESET}] {self.PURPLE}{record.name}{self.RESET}: {msg}"


class PlainFileFormatter(logging.Formatter):
    """Plain text log formatter for disk storage without ANSI escape sequences."""
    def format(self, record):
        time_str = self.formatTime(record, "%Y-%m-%d %H:%M:%S")
        msg = record.getMessage()
        return f"{time_str} [{record.levelname:<7}] {record.name}: {msg}"


class ActivityBuffer:
    """Thread-safe in-memory ring buffer capturing recent ChatOps interactions."""
    def __init__(self, maxlen: int = 200):
        self.buffer = collections.deque(maxlen=maxlen)
        self.lock = threading.Lock()

    def record(
        self,
        network: str,
        target: str,
        sender: str,
        command: str,
        status: str = "OK",
        duration_ms: Optional[float] = None
    ) -> None:
        """Appends a ChatOps activity event."""
        with self.lock:
            ts = time.strftime("%H:%M:%S")
            dur_str = f" ({duration_ms:.1f}ms)" if duration_ms is not None else ""
            line = f"[{ts}] ({network}/{target}) <{sender}> {command} -> {status}{dur_str}"
            self.buffer.append(line)

    def tail(self, lines: int = 5) -> List[str]:
        """Returns the N most recent activity records."""
        with self.lock:
            count = max(1, min(lines, len(self.buffer)))
            return list(self.buffer)[-count:]

    def clear(self) -> None:
        """Clears all in-memory activity records."""
        with self.lock:
            self.buffer.clear()


# Global activity buffer singleton
activity_buffer = ActivityBuffer(maxlen=200)


def setup_logger(name: str = "Minibots", level: int = logging.INFO) -> logging.Logger:
    """Configures and returns a logger instance with colored console and rotating file output."""
    logger = logging.getLogger(name)
    if not logger.handlers:
        # 1. Console Stream Handler
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(ColoredFormatter())
        logger.addHandler(console_handler)

        # 2. Rotating File Handler (/opt/minibots/data/minibots.log)
        log_file_env = os.environ.get("MINIBOTS_LOG_FILE")
        if not log_file_env:
            base_dir = Path(__file__).resolve().parents[1]
            data_dir = base_dir / "data"
            if not data_dir.exists() and os.path.exists("/opt/minibots/data"):
                data_dir = Path("/opt/minibots/data")
            log_file = data_dir / "minibots.log"
        else:
            log_file = Path(log_file_env)

        try:
            log_file.parent.mkdir(parents=True, exist_ok=True)
            file_handler = RotatingFileHandler(
                str(log_file),
                maxBytes=5 * 1024 * 1024,  # 5 MB
                backupCount=3,
                encoding="utf-8"
            )
            file_handler.setFormatter(PlainFileFormatter())
            logger.addHandler(file_handler)
        except Exception:
            # Fallback cleanly if disk path is not writable in unit tests
            pass

        logger.setLevel(level)
        logger.propagate = False

    return logger
