# SPDX-License-Identifier: AGPL-3.0-or-later
"""
tool_craft.py — Autonomous Tool & Plugin Synthesizer for Dystopi Minibots.
Leverages LLMRouter to generate, validate, and hot-load Python bot tools on the fly.
"""

import ast
import inspect
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from core.llm_router import LLMRouter, strip_think_tags

logger = logging.getLogger("ToolCrafter")

# Forbidden AST modules/calls for safety guard
DANGEROUS_MODULES = {"ctypes", "pty", "subprocess"}
DANGEROUS_BUILTINS = {"exec", "eval", "__import__"}


class ToolCrafter:
    """Autonomous AI tool generator producing validated Minibot scripts."""

    def __init__(self, llm_router: LLMRouter, sideload_dir: Optional[Path] = None):
        self.router = llm_router
        base_dir = Path(__file__).resolve().parents[1]
        self.sideload_dir = sideload_dir or (base_dir / "sideload")
        self.sideload_dir.mkdir(parents=True, exist_ok=True)

    def _strip_markdown_fences(self, text: str) -> str:
        """Strips markdown code blocks, backticks, and extraneous preamble."""
        cleaned = strip_think_tags(text).strip()

        # Match ```python ... ``` or ``` ... ```
        pattern = r'^```(?:python)?\s*\n(.*?)\n```\s*$'
        match = re.search(pattern, cleaned, flags=re.DOTALL)
        if match:
            return match.group(1).strip()

        # Check if fences start somewhere inside
        lines = cleaned.splitlines()
        code_lines = []
        in_fence = False
        fence_found = False

        for line in lines:
            if line.strip().startswith("```"):
                in_fence = not in_fence
                fence_found = True
                continue
            if in_fence or not fence_found:
                code_lines.append(line)

        res = "\n".join(code_lines).strip()
        return res if res else cleaned

    def check_ast_safety(self, code_text: str) -> Tuple[bool, Optional[str]]:
        """Inspects AST nodes to block forbidden modules or dangerous built-in execution."""
        try:
            tree = ast.parse(code_text)
        except SyntaxError as e:
            return False, f"Syntax error: {e}"

        for node in ast.walk(tree):
            # Check imports
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root_mod = alias.name.split(".")[0]
                    if root_mod in DANGEROUS_MODULES:
                        return False, f"Security violation: Import of forbidden module '{alias.name}'"
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    root_mod = node.module.split(".")[0]
                    if root_mod in DANGEROUS_MODULES:
                        return False, f"Security violation: Import from forbidden module '{node.module}'"

            # Check calls to eval / exec / __import__
            elif isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    if node.func.id in DANGEROUS_BUILTINS:
                        return False, f"Security violation: Call to forbidden builtin '{node.func.id}()'"

        return True, None

    def craft_tool(
        self,
        tool_name: str,
        instructions: str,
        framework: str = "sopel",
        target_bot: Optional[str] = None
    ) -> Tuple[bool, str]:
        """Synthesizes a tool script using LLM, validates AST, and writes to sideload/."""
        clean_name = re.sub(r'[^a-zA-Z0-9_]', '', tool_name.lower().strip())
        if not clean_name:
            return False, "Invalid tool name. Use alphanumeric characters and underscores only."

        target_file = self.sideload_dir / f"{clean_name}.py"

        system_prompt = (
            "You are an expert IRC bot tool synthesizer for Dystopi Minibots. "
            "Write a clean, production-grade, self-contained Python plugin using the Sopel framework. "
            "Rules:\n"
            "1. Must use 'from sopel import module' and '@module.commands(\"<command_name>\")'.\n"
            "2. Signature must be 'def <cmd_name>(bot, trigger):'.\n"
            "3. Use 'bot.say(\"<message>\")' or 'bot.reply(\"<message>\")' for output.\n"
            "4. Extract arguments with 'trigger.group(2)'.\n"
            "5. Use ONLY Python standard library modules (e.g. urllib, json, re, random, math, datetime, html, hashlib). "
            "Never import external third-party packages.\n"
            "6. Wrap network or parsing logic in try/except blocks to prevent crashes.\n"
            "7. Output ONLY the raw Python code. Do not include introductory text, commentary, or conversational remarks.\n"
        )

        user_prompt = (
            f"Generate a Sopel plugin named '{clean_name}'.\n"
            f"Requirements:\n{instructions.strip()}\n"
            f"Export command name(s) matching '{clean_name}' or appropriate aliases."
        )

        try:
            raw_response = self.router.generate(
                prompt=user_prompt,
                system_prompt=system_prompt,
                source_bot="tool_crafter",
                role="coding",
                max_tokens=800,
                temperature=0.3,
                timeout=120
            )
        except Exception as e:
            logger.error(f"LLMRouter generation failed for tool '{clean_name}': {e}")
            return False, f"LLM generation failed: {e}"

        code_text = self._strip_markdown_fences(raw_response)
        if not code_text:
            return False, "LLM returned empty or unparseable code."

        # AST Syntax Check
        try:
            tree = ast.parse(code_text)
        except SyntaxError as e:
            logger.warning(f"Crafted code failed AST syntax check: {e}")
            return False, f"Crafted script failed syntax validation: {e}"

        # AST Security Gate
        safe, sec_reason = self.check_ast_safety(code_text)
        if not safe:
            logger.warning(f"Crafted code failed AST security check: {sec_reason}")
            return False, f"Crafted script rejected by security policy: {sec_reason}"

        # Extract exported commands from AST
        exported_cmds = []
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                for dec in node.decorator_list:
                    if isinstance(dec, ast.Call) and getattr(dec.func, "attr", None) == "commands":
                        for arg in dec.args:
                            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                                exported_cmds.append(arg.value)

        # Write to sideload directory
        try:
            with open(target_file, "w", encoding="utf-8") as f:
                f.write(f"# SPDX-License-Identifier: AGPL-3.0-or-later\n")
                f.write(f"# Autogenerated by ToolCrafter on behalf of operator\n\n")
                f.write(code_text)
                f.write("\n")
        except Exception as e:
            return False, f"Failed to save script to sideload/: {e}"

        cmd_list_str = ", ".join(exported_cmds) if exported_cmds else clean_name
        return True, f"Successfully crafted 'sideload/{clean_name}.py' (Commands: {cmd_list_str}). Hot-reloader will pick it up automatically."
