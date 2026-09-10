# SPDX-License-Identifier: AGPL-3.0-or-later
"""
llm_router.py — Multi-Provider LLM Router with Autonomous Failover & <think> Suppression.
"""

import json
import logging
import re
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

import keystore
from core.prober import GPUProber
from core.telemetry import TelemetryEngine

logger = logging.getLogger("LLMRouter")


def strip_think_tags(text: str) -> str:
    """Strips <think>, <thought>, <reasoning> blocks and trailing unclosed tags."""
    if not text:
        return ""
    cleaned = re.sub(r'<(?:think|thought|reasoning)>.*?</(?:think|thought|reasoning)>', '', text, flags=re.DOTALL | re.IGNORECASE)
    cleaned = re.sub(r'<(?:think|thought|reasoning)>.*$', '', cleaned, flags=re.DOTALL | re.IGNORECASE)
    return cleaned.strip()


class LLMRouter:
    """Routes generation requests to Gemini, Local llama-server, Kaggle GPU, or MCP."""
    def __init__(self, config: Dict[str, Any], telemetry: Optional[TelemetryEngine] = None):
        self.config = config
        self.telemetry = telemetry or TelemetryEngine()
        self.gemini_key = self.config.get("GEMINI_API_KEY") or keystore.get_secret("GEMINI_API_KEY", "")
        self.local_url = self.config.get("LOCAL_LLM_URL", "http://127.0.0.1:8080/v1")
        self.kaggle_url = self.config.get("KAGGLE_GPU_URL") or keystore.get_secret("KAGGLE_GPU_URL", "")

        self.worker_secret = self.config.get("KAGGLE_WORKER_SECRET") or keystore.get_secret("KAGGLE_WORKER_SECRET", "dystopi-kaggle-sec-2026")

        self.gpu_prober: Optional[GPUProber] = None
        if self.kaggle_url:
            self.gpu_prober = GPUProber(self.kaggle_url)
            self.gpu_prober.start()

    def generate(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        source_bot: str = "minibot",
        channel: Optional[str] = None,
        user_nick: Optional[str] = None,
        max_tokens: int = 500,
        temperature: float = 0.7,
        role: Optional[str] = None,
        model: Optional[str] = None,
        timeout: Optional[float] = None
    ) -> str:
        """Executes generation with automatic multi-tier fallback and dynamic timeout scaling."""
        start_time = time.perf_counter()

        # Resolve role targeting
        eff_role = role
        if not eff_role:
            if source_bot.lower() in ("taskbot", "tool_crafter", "codebot", "julesbot"):
                eff_role = "coding"
            else:
                eff_role = "uncensored"

        # Dynamic timeout: 35s for chat/personas, 120s for coding tasks
        eff_timeout = timeout or (120.0 if eff_role == "coding" else 35.0)

        # Tier 1: Kaggle GPU Burst Worker (if registered and healthy)
        if self.gpu_prober and self.gpu_prober.is_healthy:
            res = self._call_openai_compat(
                self.kaggle_url, prompt, system_prompt, max_tokens, temperature,
                model=model, role=eff_role, timeout=eff_timeout
            )
            if res:
                latency = (time.perf_counter() - start_time) * 1000.0
                self.telemetry.record_usage(source_bot, "kaggle-gpu", "kaggle", len(prompt) // 4, len(res) // 4, latency, channel, user_nick)
                return strip_think_tags(res)

        # Tier 2: Local llama-server / vLLM (if configured and responding)
        if self.local_url:
            res = self._call_openai_compat(
                self.local_url, prompt, system_prompt, max_tokens, temperature,
                model=model, role=eff_role, timeout=eff_timeout
            )
            if res:
                latency = (time.perf_counter() - start_time) * 1000.0
                self.telemetry.record_usage(source_bot, "local-llama", "local", len(prompt) // 4, len(res) // 4, latency, channel, user_nick)
                return strip_think_tags(res)

        # Tier 3: Google Gemini API (Primary / Reliable Fallback)
        if self.gemini_key:
            res = self._call_gemini_api(prompt, system_prompt, max_tokens, temperature)
            if res:
                latency = (time.perf_counter() - start_time) * 1000.0
                self.telemetry.record_usage(source_bot, "gemini-2.5-flash", "google", len(prompt) // 4, len(res) // 4, latency, channel, user_nick)
                return strip_think_tags(res)

        return "[Error: All LLM providers offline or unconfigured.]"

    def _call_gemini_api(self, prompt: str, system_prompt: Optional[str], max_tokens: int, temperature: float) -> Optional[str]:
        """Calls Google Gemini API via REST endpoint."""
        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={self.gemini_key}"
        payload: Dict[str, Any] = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "maxOutputTokens": max_tokens,
                "temperature": temperature
            }
        }
        if system_prompt:
            payload["systemInstruction"] = {"parts": [{"text": system_prompt}]}

        try:
            req = urllib.request.Request(
                url,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=15.0) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                candidates = data.get("candidates", [])
                if candidates:
                    parts = candidates[0].get("content", {}).get("parts", [])
                    if parts:
                        return parts[0].get("text", "")
        except Exception as e:
            logger.error(f"Gemini API call failed: {e}")
        return None

    def _call_openai_compat(
        self,
        base_url: str,
        prompt: str,
        system_prompt: Optional[str],
        max_tokens: int,
        temperature: float,
        model: Optional[str] = None,
        role: Optional[str] = "uncensored",
        timeout: float = 35.0
    ) -> Optional[str]:
        """Calls OpenAI-compatible /v1/chat/completions endpoint with role targeting and dynamic timeout."""
        endpoint = f"{base_url.rstrip('/')}/chat/completions"
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        effective_model = model or ("qwen-coder" if role == "coding" else "gemma4_uncensored")

        payload = {
            "model": effective_model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature
        }

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.worker_secret}",
            "X-Kaggle-Secret": self.worker_secret,
            "X-Model-Role": role or "uncensored"
        }

        try:
            req = urllib.request.Request(
                endpoint,
                data=json.dumps(payload).encode("utf-8"),
                headers=headers
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                choices = data.get("choices", [])
                if choices:
                    return choices[0].get("message", {}).get("content", "")
        except Exception as e:
            logger.debug(f"OpenAI-compat call to {endpoint} (timeout={timeout}s) failed: {e}")
        return None
