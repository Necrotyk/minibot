# SPDX-License-Identifier: AGPL-3.0-or-later
"""
sideload.py — Universal Sideloading Engine with AST Pre-Flight, Framework Adapters & mtime Watcher.
"""

import ast
import asyncio
import importlib.util
import inspect
import logging
import os
import re
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("Sideloader")


class SideloadEngine:
    """Manages dynamic module importing, AST validation, multi-framework shims, and execution sandboxing."""

    def __init__(
        self,
        sideload_dir: Optional[str] = None,
        data_dir: Optional[str] = None,
        on_reload_callback: Optional[Callable[[List[str]], None]] = None
    ):
        if not sideload_dir:
            sideload_dir = str(Path(__file__).resolve().parents[1] / "sideload")
        if not data_dir:
            data_dir = str(Path(__file__).resolve().parents[1] / "data" / "sideload")

        self.sideload_dir = Path(sideload_dir)
        self.sideload_dir.mkdir(parents=True, exist_ok=True)
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)

        self.on_reload_callback = on_reload_callback
        self.loaded_modules: Dict[str, Any] = {}
        self.module_mtimes: Dict[str, float] = {}
        self.failure_counts: Dict[str, int] = {}
        self.quarantined_modules: Dict[str, str] = {}
        self.worker_pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="SideloadWorker")
        self._watcher_task: Optional[asyncio.Task] = None

    def list_available_scripts(self) -> List[str]:
        """Returns list of python files available in the sideload directory."""
        if not self.sideload_dir.exists():
            return []
        return sorted([f.name for f in self.sideload_dir.glob("*.py") if not f.name.startswith("__")])

    def detect_framework(self, code_text: str) -> str:
        """Inspects AST / source tokens to determine target framework."""
        if "@module.commands" in code_text or "@module.rule" in code_text or "from sopel" in code_text:
            return "sopel"
        if "callbacks.Plugin" in code_text or "from supybot" in code_text or "from limnoria" in code_text:
            return "limnoria"
        if "@botcmd" in code_text or "BotPlugin" in code_text or "from errbot" in code_text:
            return "errbot"
        if "@bot.command" in code_text or "@bot.group" in code_text or "commands.Bot" in code_text or "discord_shim" in code_text:
            return "discord"
        if "SingleServerIRCBot" in code_text or "from irc.bot" in code_text or "ircbot_shim" in code_text:
            return "ircbot"
        if "AsyncPlugin" in code_text or "handle_privmsg" in code_text:
            return "async"
        return "generic"

    def scaffold_tool(self, framework: str, tool_name: str) -> tuple:
        """Generates a boilerplate template in sideload directory for specified framework."""
        fw = framework.lower().strip()
        name = re.sub(r'[^a-zA-Z0-9_]', '', tool_name.lower().strip())
        if not name:
            return False, "Invalid tool name."

        target_file = self.sideload_dir / f"{name}.py"
        if target_file.exists():
            return False, f"File {name}.py already exists in sideload directory."

        if fw in ("sopel", "willie"):
            code = f"""# SPDX-License-Identifier: AGPL-3.0-or-later
from sopel import module

@module.commands("{name}")
def {name}_cmd(bot, trigger):
    args = trigger.group(2) or ""
    bot.reply(f"[{name.upper()}] Arguments: {{args}}")
"""
        elif fw == "errbot":
            code = f"""# SPDX-License-Identifier: AGPL-3.0-or-later
from errbot import BotPlugin, botcmd

class {name.capitalize()}Plugin(BotPlugin):
    @botcmd
    def {name}(self, msg, args):
        return f"[{name.upper()}] Processed: {{args}}"
"""
        elif fw in ("discord", "nextcord"):
            code = f"""# SPDX-License-Identifier: AGPL-3.0-or-later
from discord_shim import bot

@bot.command(name="{name}", help="Executes {name} command with args.")
async def {name}_cmd(ctx, *args):
    await ctx.reply(f"[{name.upper()}] Processed: {{' '.join(args)}}")
"""
        elif fw in ("ircbot", "jaraco"):
            code = f"""# SPDX-License-Identifier: AGPL-3.0-or-later
from ircbot_shim import SingleServerIRCBot

class {name.capitalize()}Bot(SingleServerIRCBot):
    def on_pubmsg(self, connection, event):
        msg = event.arguments[0]
        if msg.startswith("!{name}"):
            connection.privmsg(event.target, f"[{name.upper()}] Handled: {{msg}}")
"""
        else:
            code = f"""# SPDX-License-Identifier: AGPL-3.0-or-later
class Plugin:
    def __init__(self, bot):
        self.bot = bot
    async def handle_privmsg(self, target: str, sender: str, text: str):
        if text.startswith("!{name}"):
            self.bot.send_msg(target, f"[{name.upper()}] Hello {{sender}}!")
"""

        with open(target_file, "w", encoding="utf-8") as f:
            f.write(code)

        valid, det_fw = self.validate_ast(str(target_file))
        if not valid:
            target_file.unlink(missing_ok=True)
            return False, f"Generated template failed AST validation: {det_fw}"
        return True, f"Created {name}.py ({det_fw}) in sideload/ (Ready for !sideload load {name}.py)"

    def normalize_remote_url(self, url: str) -> str:
        """Translates GitHub web blob and Gist URLs to direct raw endpoints."""
        u = url.strip()
        # GitHub blob: https://github.com/user/repo/blob/branch/path.py -> https://raw.githubusercontent.com/user/repo/branch/path.py
        gh_match = re.match(r"^https?://(?:www\.)?github\.com/([^/]+)/([^/]+)/blob/([^/]+)/(.+)$", u)
        if gh_match:
            user, repo, branch, path = gh_match.groups()
            return f"https://raw.githubusercontent.com/{user}/{repo}/{branch}/{path}"

        # GitHub Gist: https://gist.github.com/user/id -> https://gist.githubusercontent.com/user/id/raw
        gist_match = re.match(r"^https?://gist\.github\.com/([^/]+)/([a-f0-9]+)(?:/.*)?$", u, re.IGNORECASE)
        if gist_match and "/raw" not in u:
            user, gist_id = gist_match.groups()
            return f"https://gist.githubusercontent.com/{user}/{gist_id}/raw"

        return u

    def import_plugin(self, url_or_paste: str, custom_name: str = None) -> tuple:
        """Fetches a plugin from URL, GitHub, Gist, or Dystopi pastebin, validates AST, and writes to sideload/."""
        import urllib.request
        from core.guard import is_safe_url

        pastebin_base = os.environ.get("PASTEBIN_URL", "http://127.0.0.1:8000").rstrip("/")
        target_url = url_or_paste.strip()
        if not target_url.startswith(("http://", "https://")):
            target_url = f"{pastebin_base}/raw/{target_url}"
        else:
            target_url = self.normalize_remote_url(target_url)

        # SSRF Check (allow trusted pastebin base if specified)
        if not (pastebin_base and target_url.startswith(pastebin_base)):
            safe, _ = is_safe_url(target_url)
            if not safe:
                return False, "SSRF Guard blocked access to non-public / internal network address."

        try:
            req = urllib.request.Request(target_url, headers={"User-Agent": "MinibotsPluginImporter/1.0"})
            with urllib.request.urlopen(req, timeout=5.0) as resp:
                code_text = resp.read().decode("utf-8", errors="replace")
        except Exception as e:
            return False, f"Failed to fetch plugin: {e}"

        # Strip markdown fences if present
        if "```" in code_text:
            match = re.search(r'```(?:python)?\s*\n(.*?)\n```', code_text, flags=re.DOTALL)
            if match:
                code_text = match.group(1).strip()

        if not custom_name:
            parsed_name = Path(target_url.split("?")[0]).stem
            custom_name = parsed_name if parsed_name and parsed_name not in ("raw", "index") else f"imported_{int(time.time())}"

        clean_name = re.sub(r'[^a-zA-Z0-9_]', '', custom_name.lower().strip())
        if not clean_name:
            clean_name = f"imported_{int(time.time())}"
        target_file = self.sideload_dir / f"{clean_name}.py"

        try:
            tree = ast.parse(code_text)
            fw = self.detect_framework(code_text)
        except Exception as e:
            return False, f"Plugin failed AST syntax validation: {e}"

        # Security check: forbid dangerous modules
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[0] in {"ctypes", "pty", "subprocess"}:
                        return False, f"Plugin rejected: Forbidden module '{alias.name}'"
            elif isinstance(node, ast.ImportFrom):
                if node.module and node.module.split(".")[0] in {"ctypes", "pty", "subprocess"}:
                    return False, f"Plugin rejected: Forbidden module '{node.module}'"

        with open(target_file, "w", encoding="utf-8") as f:
            f.write(code_text)
        return True, f"Successfully imported {clean_name}.py ({fw}) into sideload/"

    def validate_ast(self, file_path: str) -> Tuple[bool, str]:
        """Parses python file into an Abstract Syntax Tree to ensure syntax validity."""
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                code = f.read()
            tree = ast.parse(code, filename=file_path)
            framework = self.detect_framework(code)
            return True, framework
        except Exception as e:
            logger.error(f"[AST GATE] Syntax validation failed for {file_path}: {e}")
            return False, str(e)

    def _get_plugin_db(self, plugin_stem: str) -> str:
        """Creates namespaced storage directory and SQLite database in WAL mode."""
        plugin_data_dir = self.data_dir / plugin_stem
        plugin_data_dir.mkdir(parents=True, exist_ok=True)
        db_path = str(plugin_data_dir / "storage.sqlite3")
        try:
            conn = sqlite3.connect(db_path)
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA busy_timeout=5000;")
            conn.close()
        except Exception as e:
            logger.debug(f"Error initializing WAL on {db_path}: {e}")
        return db_path

    def load_script(self, script_path: str, module_name: Optional[str] = None) -> Optional[Any]:
        """Loads or reloads a Python script after AST verification and sandbox preparation."""
        path_obj = Path(script_path)
        if not path_obj.is_absolute():
            in_sideload = self.sideload_dir / script_path
            if in_sideload.is_file():
                path_obj = in_sideload

        if not path_obj.is_file():
            logger.error(f"Script file not found: {script_path}")
            return None

        abs_path = str(path_obj.resolve())
        is_valid, framework = self.validate_ast(abs_path)
        if not is_valid:
            return None

        if not module_name:
            module_name = f"sideload_{path_obj.stem}"

        # Check circuit breaker
        if self.failure_counts.get(module_name, 0) >= 5:
            logger.warning(f"[CIRCUIT-BREAKER] Module '{module_name}' is quarantined ({self.failure_counts[module_name]} failures).")
            self.quarantined_modules[module_name] = "Quarantined due to repeated runtime exceptions."
            return None

        try:
            mtime = os.path.getmtime(abs_path)
            spec = importlib.util.spec_from_file_location(module_name, abs_path)
            if not spec or not spec.loader:
                logger.error(f"Could not construct module spec for {abs_path}")
                return None

            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module

            # Inject namespaced DB path and framework helpers
            db_path = self._get_plugin_db(path_obj.stem)
            setattr(module, "_DYSTOPI_DB_PATH", db_path)
            setattr(module, "_DYSTOPI_FRAMEWORK", framework)

            # Pre-inject all framework shims into sys.modules before executing code
            try:
                try:
                    from adapters.sopel_shim import inject_sopel_shim
                    from adapters.limnoria_shim import inject_limnoria_shim
                    from adapters.errbot_shim import inject_errbot_shim
                    from adapters.discord_shim import inject_discord_shim
                    from adapters.ircbot_shim import inject_ircbot_shim
                except ImportError:
                    from minibots.adapters.sopel_shim import inject_sopel_shim
                    from minibots.adapters.limnoria_shim import inject_limnoria_shim
                    from minibots.adapters.errbot_shim import inject_errbot_shim
                    from minibots.adapters.discord_shim import inject_discord_shim
                    from minibots.adapters.ircbot_shim import inject_ircbot_shim

                inject_sopel_shim()
                inject_limnoria_shim()
                inject_errbot_shim()
                inject_discord_shim()
                inject_ircbot_shim()
            except Exception as _e:
                logger.warning(f"Notice injecting framework shims: {_e}")

            spec.loader.exec_module(module)

            self.loaded_modules[module_name] = module
            self.module_mtimes[module_name] = mtime
            self.failure_counts[module_name] = 0
            self.quarantined_modules.pop(module_name, None)
            logger.info(f"Successfully sideloaded module '{module_name}' ({framework}) from {abs_path}")
            return module
        except Exception as e:
            self.failure_counts[module_name] = self.failure_counts.get(module_name, 0) + 1
            logger.error(f"Failed to execute sideloaded module {module_name}: {e}")
            return None

    def check_and_reload_modified(self) -> List[str]:
        """Checks mtime of all loaded modules and reloads any that have changed."""
        reloaded = []
        for name, mod in list(self.loaded_modules.items()):
            file_path = getattr(mod, "__file__", None)
            if file_path and os.path.isfile(file_path):
                cur_mtime = os.path.getmtime(file_path)
                if cur_mtime > self.module_mtimes.get(name, 0.0):
                    logger.info(f"Detected modification in {name}, hot-reloading...")
                    if self.load_script(file_path, name):
                        reloaded.append(name)
        if reloaded and self.on_reload_callback:
            try:
                self.on_reload_callback(reloaded)
            except Exception as e:
                logger.error(f"[Sideloader] Error in on_reload_callback: {e}")
        return reloaded

    def start_watcher(self, interval: float = 2.0) -> None:
        """Starts background async loop scanning mtimes every 2s for zero-disconnect hot reloading."""
        if self._watcher_task is None or self._watcher_task.done():
            self._watcher_task = asyncio.create_task(self._mtime_watcher_loop(interval))

    async def _mtime_watcher_loop(self, interval: float) -> None:
        while True:
            await asyncio.sleep(interval)
            try:
                reloaded = self.check_and_reload_modified()
                if reloaded:
                    logger.info(f"[Sideloader] Auto-reloaded {len(reloaded)} module(s): {', '.join(reloaded)}")
            except Exception as e:
                logger.debug(f"[Sideloader] Error in mtime loop: {e}")

    def run_with_timeout(self, func: Callable, args: tuple = (), kwargs: dict = None, timeout: float = 10.0, module_name: Optional[str] = None) -> Any:
        """Executes a sideloaded function within a 10s timeout guard and circuit breaker."""
        if kwargs is None:
            kwargs = {}
        future = self.worker_pool.submit(func, *args, **kwargs)
        try:
            res = future.result(timeout=timeout)
            if module_name and module_name in self.failure_counts:
                self.failure_counts[module_name] = 0
            return res
        except TimeoutError:
            logger.warning(f"Sideloaded callback execution timed out after {timeout}s")
            if module_name:
                self.failure_counts[module_name] = self.failure_counts.get(module_name, 0) + 1
            return None
        except Exception as e:
            logger.error(f"Unhandled exception in sideloaded callback: {e}")
            if module_name:
                self.failure_counts[module_name] = self.failure_counts.get(module_name, 0) + 1
            return None

    def get_module_help(self, module_name: str) -> List[str]:
        """Inspects docstrings and exported commands to generate standard 380-byte !help lines."""
        mod = self.loaded_modules.get(module_name)
        if not mod:
            return [f"Module '{module_name}' is not loaded."]

        lines = []
        doc = getattr(mod, "__doc__", "").strip().splitlines()
        header = doc[0] if doc else f"Sideloaded plugin '{module_name}'"
        lines.append(f"[10PLUGIN] {module_name} — {header[:300]}")

        commands = []
        for name, obj in inspect.getmembers(mod):
            if hasattr(obj, "_sopel_commands"):
                commands.extend(obj._sopel_commands)
            elif callable(obj) and getattr(obj, "__doc__", None):
                if name.startswith("cmd_") or name.startswith("do_"):
                    commands.append(name.split("_", 1)[1])

        if commands:
            cmd_str = ", ".join(sorted(set(commands)))
            lines.append(f"  11Commands: {cmd_str[:320]}")

        return lines
