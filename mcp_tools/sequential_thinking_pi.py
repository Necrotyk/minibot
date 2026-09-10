# SPDX-License-Identifier: AGPL-3.0-or-later
"""
sequential_thinking_pi.py — Lightweight 3-Phase Deductive Reasoning Engine for Minibots.
Adapted from sequential_thinking_server.py to execute structured DIAGNOSE -> VERIFY -> SYNTHESIZE
cycles within compact token budgets for the local Raspberry Pi 5 LLM.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

try:
    import httpx
except ImportError:
    httpx = None

logger = logging.getLogger("SequentialThinkingPi")

DEFAULT_PI_URL = os.environ.get("REASONING_LLM_URL", os.environ.get("LOCAL_LLM_URL", "http://127.0.0.1:8080/v1/chat/completions"))


class SequentialThinkingPi:
    """Lightweight 3-phase deductive reasoning engine designed for compact LLM context."""

    def __init__(self, endpoint_url: str = DEFAULT_PI_URL, timeout: float = 18.0) -> None:
        self.endpoint_url = endpoint_url
        self.timeout = timeout

    async def process_thought_cycle(
        self, query: str, context_snippets: List[str], active_model: str = "lfm"
    ) -> Dict[str, Any]:
        """
        Executes a 3-step structured deduction cycle:
        1. DIAGNOSE: Identifies the root ambiguity or technical contradiction.
        2. VERIFY: Cross-checks premises against retrieved search snippets.
        3. SYNTHESIZE: Formulates a concise 1-sentence technical conclusion.
        """
        if not httpx:
            return {"status": "unavailable", "synthesis": ""}

        evidence_text = " | ".join(context_snippets[:2]) if context_snippets else "No external evidence provided."

        prompt = (
            f"Query: {query}\n"
            f"Evidence: {evidence_text}\n\n"
            "Generate a 3-step deductive breakdown in exactly this format:\n"
            "DIAGNOSE: [Core technical problem or unknown]\n"
            "VERIFY: [Fact check against evidence]\n"
            "SYNTHESIZE: [Direct technical resolution in 1 sentence]"
        )

        messages = [
            {"role": "system", "content": "You are a deterministic technical logic verification filter. Respond strictly in the 3-step format."},
            {"role": "user", "content": prompt}
        ]

        payload: Dict[str, Any] = {
            "model": active_model or "lfm",
            "messages": messages,
            "max_tokens": 100,
            "temperature": 0.2
        }

        # Apply thinking bypass if model is LFM-thinking
        if "lfm" in active_model.lower() or "thinking" in active_model.lower():
            payload["chat_template_kwargs"] = {"enable_thinking": False}
            messages.append({"role": "assistant", "content": "<think>\n</think>DIAGNOSE:"})

        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(self.timeout, connect=2.0)) as client:
                res = await client.post(self.endpoint_url, json=payload)
                if res.status_code != 200:
                    logger.debug(f"SequentialThinking HTTP {res.status_code}")
                    return {"status": "error", "synthesis": ""}

                res_data = res.json()
                choices = res_data.get("choices", [])
                if not choices:
                    return {"status": "empty", "synthesis": ""}

                raw_content = choices[0].get("message", {}).get("content", "").strip()

                synthesis = ""
                for line in raw_content.splitlines():
                    clean_line = line.strip()
                    if clean_line.upper().startswith("SYNTHESIZE:"):
                        synthesis = clean_line[11:].strip()
                        break

                # If SYNTHESIZE line not explicitly found, use the last non-empty line as summary
                if not synthesis and raw_content:
                    lines = [l.strip() for l in raw_content.splitlines() if l.strip()]
                    if lines:
                        synthesis = lines[-1]

                return {
                    "status": "resolved" if synthesis else "partial",
                    "raw_steps": raw_content,
                    "synthesis": synthesis
                }

        except Exception as e:
            logger.debug(f"SequentialThinking thought cycle error: {e}")
            return {"status": "failed", "synthesis": "", "raw_steps": ""}
