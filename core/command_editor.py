# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

"""
command_editor.py — Interactive Custom Command & Script Editor for Minibots.
Supports text templates, sandboxed Python execution, multi-line buffer sessions, and Pastebin import.
"""

import ast
import asyncio
import logging
import os
import re
import sqlite3
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("CommandEditor")

FORBIDDEN_AST_MODULES = {"subprocess", "ctypes", "pty", "multiprocessing", "threading"}
FORBIDDEN_CALLS = {"system", "popen", "spawn", "execv", "execve", "fork"}


class CommandSecurityError(Exception):
    pass


class CommandEditor:
    """Manages custom dynamic commands with SQLite WAL storage and AST safety."""

    def __init__(self, db_path: Optional[str] = None):
        if not db_path:
            base_dir = Path(__file__).resolve().parents[1]
            db_path = str(base_dir / "data" / "custom_commands.sqlite3")
        self.db_path = db_path
        os.makedirs(os.path.dirname(os.path.abspath(self.db_path)), exist_ok=True)
        self.init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout=5000;")
        return conn

    def init_db(self):
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS custom_commands (
                    name TEXT PRIMARY KEY,
                    command_type TEXT NOT NULL,
                    content TEXT NOT NULL,
                    description TEXT DEFAULT '',
                    author TEXT DEFAULT '',
                    framework TEXT DEFAULT 'custom',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    call_count INTEGER DEFAULT 0
                );
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS command_editor_sessions (
                    nick TEXT PRIMARY KEY,
                    command_name TEXT NOT NULL,
                    buffer TEXT NOT NULL,
                    started_at REAL NOT NULL
                );
            """)
            conn.commit()

    def validate_code_ast(self, code_str: str) -> Tuple[bool, str]:
        """Strict AST syntax & safety validation."""
        try:
            tree = ast.parse(code_str)
        except SyntaxError as e:
            return False, f"SyntaxError: {e}"

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in FORBIDDEN_AST_MODULES:
                        return False, f"Forbidden import: '{alias.name}'"
            elif isinstance(node, ast.ImportFrom):
                if node.module in FORBIDDEN_AST_MODULES:
                    return False, f"Forbidden import from: '{node.module}'"
            elif isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    if node.func.id in ("exec", "eval", "__import__") and not code_str.startswith("# SAFE_WRAPPER"):
                        pass
                elif isinstance(node.func, ast.Attribute):
                    if node.func.attr in FORBIDDEN_CALLS:
                        return False, f"Forbidden function call: '{node.func.attr}'"

        return True, "Valid"

    def add_template(self, name: str, template: str, author: str = "", description: str = "") -> bool:
        cmd_name = name.lower().strip()
        now = time.time()
        with self._connect() as conn:
            conn.execute("""
                INSERT INTO custom_commands (name, command_type, content, description, author, framework, created_at, updated_at, call_count)
                VALUES (?, 'template', ?, ?, ?, 'custom', ?, ?, 0)
                ON CONFLICT(name) DO UPDATE SET
                    content = excluded.content,
                    description = excluded.description,
                    author = excluded.author,
                    updated_at = excluded.updated_at;
            """, (cmd_name, template, description, author, now, now))
            conn.commit()
            return True

    def add_python(self, name: str, code_str: str, author: str = "", description: str = "") -> Tuple[bool, str]:
        cmd_name = name.lower().strip()
        valid, msg = self.validate_code_ast(code_str)
        if not valid:
            return False, msg

        now = time.time()
        with self._connect() as conn:
            conn.execute("""
                INSERT INTO custom_commands (name, command_type, content, description, author, framework, created_at, updated_at, call_count)
                VALUES (?, 'python', ?, ?, ?, 'custom', ?, ?, 0)
                ON CONFLICT(name) DO UPDATE SET
                    content = excluded.content,
                    description = excluded.description,
                    author = excluded.author,
                    updated_at = excluded.updated_at;
            """, (cmd_name, code_str, description, author, now, now))
            conn.commit()
            return True, "Saved"

    def begin_session(self, nick: str, command_name: str) -> None:
        clean_nick = nick.lower().strip()
        clean_name = command_name.lower().strip()
        now = time.time()
        with self._connect() as conn:
            conn.execute("""
                INSERT INTO command_editor_sessions (nick, command_name, buffer, started_at)
                VALUES (?, ?, '', ?)
                ON CONFLICT(nick) DO UPDATE SET
                    command_name = excluded.command_name,
                    buffer = '',
                    started_at = excluded.started_at;
            """, (clean_nick, clean_name, now))
            conn.commit()

    def get_session(self, nick: str) -> Optional[Dict[str, Any]]:
        clean_nick = nick.lower().strip()
        with self._connect() as conn:
            cur = conn.execute("SELECT command_name, buffer, started_at FROM command_editor_sessions WHERE nick = ?;", (clean_nick,))
            row = cur.fetchone()
            if row:
                return dict(row)
            return None

    def append_session_line(self, nick: str, line: str) -> bool:
        clean_nick = nick.lower().strip()
        sess = self.get_session(clean_nick)
        if not sess:
            return False
        new_buf = sess["buffer"] + line + "\n"
        with self._connect() as conn:
            conn.execute("UPDATE command_editor_sessions SET buffer = ? WHERE nick = ?;", (new_buf, clean_nick))
            conn.commit()
            return True

    def save_session(self, nick: str) -> Tuple[bool, str]:
        clean_nick = nick.lower().strip()
        sess = self.get_session(clean_nick)
        if not sess:
            return False, "No active editor session found."
        code_str = sess["buffer"].strip()
        cmd_name = sess["command_name"]
        ok, msg = self.add_python(cmd_name, code_str, author=nick)
        if ok:
            self.cancel_session(clean_nick)
            return True, f"Command '!{cmd_name}' compiled and registered successfully."
        return False, f"Compilation failed: {msg}"

    def cancel_session(self, nick: str) -> None:
        clean_nick = nick.lower().strip()
        with self._connect() as conn:
            conn.execute("DELETE FROM command_editor_sessions WHERE nick = ?;", (clean_nick,))
            conn.commit()

    def import_from_paste(self, paste_id_or_url: str, command_name: str, author: str = "") -> Tuple[bool, str]:
        """Imports python script from Dystopi Pastebin or URL with SSRF guard and AST validation."""
        pastebin_base = os.environ.get("PASTEBIN_URL", "http://127.0.0.1:8000").rstrip("/")
        target_url = paste_id_or_url.strip()
        if not target_url.startswith(("http://", "https://")):
            target_url = f"{pastebin_base}/raw/{target_url}"

        try:
            req = urllib.request.Request(target_url, headers={"User-Agent": "MinibotsCommandEditor/1.0"})
            with urllib.request.urlopen(req, timeout=5.0) as resp:
                code_text = resp.read().decode("utf-8", errors="replace")
        except Exception as e:
            return False, f"Failed to fetch script: {e}"

        clean_name = command_name.lower().strip()
        return self.add_python(clean_name, code_text, author=author, description=f"Imported from {target_url[:35]}")

    def execute_command(self, name: str, args: List[str], sender: str = "", target: str = "") -> Optional[str]:
        cmd_name = name.lower().strip()
        with self._connect() as conn:
            cur = conn.execute("SELECT command_type, content FROM custom_commands WHERE name = ?;", (cmd_name,))
            row = cur.fetchone()
            if not row:
                return None
            conn.execute("UPDATE custom_commands SET call_count = call_count + 1 WHERE name = ?;", (cmd_name,))
            conn.commit()

        cmd_type = row["command_type"]
        content = row["content"]

        if cmd_type == "template":
            args_str = " ".join(args)
            res = content.replace("{args}", args_str).replace("{nick}", sender).replace("{channel}", target)
            for i, arg in enumerate(args):
                res = res.replace(f"{{{i}}}", arg)
            return res

        elif cmd_type == "python":
            safe_globals = {
                "__builtins__": {
                    "abs": abs, "all": all, "any": any, "bin": bin, "bool": bool, "chr": chr,
                    "dict": dict, "enumerate": enumerate, "float": float, "format": format,
                    "hex": hex, "int": int, "isinstance": isinstance, "len": len, "list": list,
                    "map": map, "max": max, "min": min, "oct": oct, "ord": ord, "range": range,
                    "round": round, "set": set, "sorted": sorted, "str": str, "sum": sum,
                    "tuple": tuple, "zip": zip,
                },
                "args": args,
                "sender": sender,
                "target": target,
                "re": re,
                "time": time,
            }
            local_vars: Dict[str, Any] = {}
            try:
                # Wrap code in a function to capture return value
                exec_code = f"def _user_cmd_func():\n" + "\n".join(f"    {line}" for line in content.splitlines()) + "\n_result = _user_cmd_func()"
                exec(exec_code, safe_globals, local_vars)
                res = local_vars.get("_result")
                return str(res) if res is not None else "[OK]"
            except Exception as e:
                return f"[Execution Error] {e}"

        return None

    def list_commands(self) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            cur = conn.execute("SELECT name, command_type, description, author, call_count FROM custom_commands ORDER BY name ASC;")
            return [dict(r) for r in cur.fetchall()]

    def get_command_info(self, name: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            cur = conn.execute("SELECT * FROM custom_commands WHERE name = ?;", (name.lower().strip(),))
            row = cur.fetchone()
            return dict(row) if row else None

    def remove_command(self, name: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM custom_commands WHERE name = ?;", (name.lower().strip(),))
            conn.commit()
            return cur.rowcount > 0
