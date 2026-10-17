# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

"""
hf_model_search.py — Hugging Face Hub Model Search & VRAM Feasibility Engine.

Provides:
1. REST API search over Hugging Face Hub for GGUF models.
2. VRAM footprint calculator for 16GB (P100 / 1x T4) and 32GB (2x T4).
3. Security input sanitization against path traversal (..) and CLI argument injection (--).
4. Curated role presets for Uncensored persona bots and Coding agents.
"""

import json
import logging
import re
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("HFModelSearch")

# Safe model name regex: alphanumeric, dash, underscore, dot, slash, optional colon for quant
SAFE_MODEL_PATTERN = re.compile(r'^[a-zA-Z0-9_\-\.]+(?:/[a-zA-Z0-9_\-\.]+)*(?::[a-zA-Z0-9_\-\.]+)?(?:\.gguf)?$', re.IGNORECASE)

# Curated Presets
CURATED_PRESETS: Dict[str, Dict[str, Any]] = {
    "gemma4_uncensored": {
        "repo_id": "mradermacher/gemma-4-12B-it-abliterated-uncensored-GGUF",
        "file": "gemma-4-12B-it-abliterated-uncensored.Q4_K_M.gguf",
        "params": "12B",
        "quant": "Q4_K_M",
        "estimated_vram_gb": 7.8,
        "description": "Flagship Gemma 4 abliterated/uncensored 12B model with unrestricted conversational & technical depth.",
        "recommended_slot": 0,
        "temperature": 0.85,
        "max_tokens": 150,
    },
    "gemma4_e4b_uncensored": {
        "repo_id": "HauhauCS/Gemma-4-E4B-Uncensored",
        "file": "Gemma-4-E4B-Uncensored-Aggressive.Q4_K_M.gguf",
        "params": "4B",
        "quant": "Q4_K_M",
        "estimated_vram_gb": 3.2,
        "description": "Ultra-fast Gemma 4 4B uncensored banter and creative persona chatter model.",
        "recommended_slot": 0,
        "temperature": 0.85,
        "max_tokens": 150,
    },
    "uncensored": {
        "repo_id": "mradermacher/gemma-4-12B-it-abliterated-uncensored-GGUF",
        "file": "gemma-4-12B-it-abliterated-uncensored.Q4_K_M.gguf",
        "params": "12B",
        "quant": "Q4_K_M",
        "estimated_vram_gb": 7.8,
        "description": "Default uncensored flagship model (Gemma 4 12B Abliterated).",
        "recommended_slot": 0,
        "temperature": 0.85,
        "max_tokens": 150,
    },
    "dolphin": {
        "repo_id": "cognitivecomputations/dolphin-2.9.2-qwen2-7b-GGUF",
        "file": "dolphin-2.9.2-qwen2-7b-Q4_K_M.gguf",
        "params": "7B",
        "quant": "Q4_K_M",
        "estimated_vram_gb": 5.2,
        "description": "Uncensored, high-wit banter LLM for IRC trollbots and creative persona chatter.",
        "recommended_slot": 0,
        "temperature": 0.85,
        "max_tokens": 150,
    },
    "coding": {
        "repo_id": "Qwen/Qwen2.5-Coder-7B-Instruct-GGUF",
        "file": "qwen2.5-coder-7b-instruct-q4_k_m.gguf",
        "params": "7B",
        "quant": "Q4_K_M",
        "estimated_vram_gb": 5.4,
        "description": "State-of-the-art 7B code generation, patch synthesis, and AST refactoring model.",
        "recommended_slot": 1,
        "temperature": 0.2,
        "max_tokens": 1024,
    },
    "deepseek_coder": {
        "repo_id": "deepseek-ai/deepseek-coder-6.7b-instruct-GGUF",
        "file": "deepseek-coder-6.7b-instruct.Q4_K_M.gguf",
        "params": "6.7B",
        "quant": "Q4_K_M",
        "estimated_vram_gb": 4.9,
        "description": "Precision software engineering model for unit tests and bug reproduction.",
        "recommended_slot": 1,
        "temperature": 0.2,
        "max_tokens": 1024,
    },
    "hermes": {
        "repo_id": "NousResearch/Hermes-3-Llama-3.1-8B-GGUF",
        "file": "Hermes-3-Llama-3.1-8B.Q4_K_M.gguf",
        "params": "8B",
        "quant": "Q4_K_M",
        "estimated_vram_gb": 5.8,
        "description": "Sharp roleplay and instruction-following persona chatter model.",
        "recommended_slot": 0,
        "temperature": 0.8,
        "max_tokens": 200,
    }
}


def sanitize_model_identifier(raw: str) -> Tuple[bool, str, str]:
    """
    Validates model identifier string against path traversal and flag injection.
    Returns: (is_valid, sanitized_string, error_message)
    """
    cleaned = (raw or "").strip()
    if not cleaned:
        return False, "", "Empty model identifier."

    # Prevent CLI argument injection (SEC-2026-004 alignment)
    if cleaned.startswith("-"):
        return False, cleaned, "Model identifier cannot begin with a hyphen (flag injection risk)."

    # Prevent Path Traversal
    if ".." in cleaned or cleaned.startswith("/") or "\\" in cleaned:
        return False, cleaned, "Path traversal sequence detected ('..' or leading slash)."

    if not SAFE_MODEL_PATTERN.match(cleaned):
        return False, cleaned, f"Invalid characters in model string: '{cleaned}'. Allowed: [a-zA-Z0-9_.-/]."

    return True, cleaned, ""


def estimate_vram_requirements(param_count_b: float, quant_bits: float = 4.5, context_len: int = 4096) -> float:
    """
    Estimates required GPU VRAM in GB:
    Weight memory + 20% runtime buffer + KV cache memory.
    """
    weight_gb = (param_count_b * quant_bits) / 8.0
    buffer_gb = weight_gb * 0.20
    # Approx 0.5MB per 1024 tokens of context for 7B-14B models
    kv_cache_gb = (context_len / 1024.0) * (param_count_b / 7.0) * 0.5
    total_gb = weight_gb + buffer_gb + kv_cache_gb
    return round(total_gb, 2)


def parse_model_params(model_id: str) -> Tuple[float, float]:
    """Extracts approximate parameter count (Billions) and quantization bits from model string."""
    m_lower = model_id.lower()

    # Parameter count
    param_match = re.search(r'(\d+(?:\.\d+)?)\s*b\b', m_lower)
    params = float(param_match.group(1)) if param_match else 7.0

    # Quantization bits
    if "q8" in m_lower or "q8_0" in m_lower:
        bits = 8.5
    elif "q6" in m_lower or "q6_k" in m_lower:
        bits = 6.5
    elif "q5" in m_lower:
        bits = 5.5
    elif "q4" in m_lower:
        bits = 4.5
    elif "q3" in m_lower:
        bits = 3.5
    elif "q2" in m_lower:
        bits = 2.8
    elif "fp16" in m_lower or "f16" in m_lower:
        bits = 16.0
    else:
        bits = 4.5  # Default assumption for GGUF

    return params, bits


def is_hardware_feasible(model_name: str, target_gpu: str = "2x_t4") -> Tuple[bool, float, str]:
    """
    Checks if model fits within target Kaggle GPU tier:
    - '1x_t4_or_p100': Max 15.0 GB usable VRAM
    - '2x_t4': Max 30.0 GB usable VRAM (or 15.0 GB per GPU in dual-port split)
    """
    params, bits = parse_model_params(model_name)
    vram_req = estimate_vram_requirements(params, bits)

    max_limit = 15.0 if target_gpu == "1x_t4_or_p100" else 30.0
    if vram_req > max_limit:
        return False, vram_req, f"Model requires ~{vram_req}GB VRAM, exceeding hardware ceiling of {max_limit}GB."

    return True, vram_req, f"Feasible: requires ~{vram_req}GB VRAM (within {max_limit}GB limit)."


def search_hf_models(query: str, limit: int = 5) -> List[Dict[str, Any]]:
    """
    Queries Hugging Face Hub REST API for GGUF models matching query.
    Pure Python standard library (zero external dependencies).
    """
    is_valid, clean_query, err = sanitize_model_identifier(query)
    if not is_valid:
        logger.warning(f"[HFModelSearch] Search rejected: {err}")
        return []

    encoded = urllib.parse.quote(clean_query)
    url = f"https://huggingface.co/api/models?search={encoded}&filter=gguf&pipeline_tag=text-generation&sort=downloads&direction=-1&limit={limit}"

    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Dystopi-Minibots/2.0 (GNU GPL-3.0+)"}
    )

    try:
        with urllib.request.urlopen(req, timeout=8.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            results = []
            for item in data:
                m_id = item.get("id", "")
                params, bits = parse_model_params(m_id)
                vram = estimate_vram_requirements(params, bits)
                feasible, _, reason = is_hardware_feasible(m_id, "1x_t4_or_p100")
                results.append({
                    "id": m_id,
                    "downloads": item.get("downloads", 0),
                    "likes": item.get("likes", 0),
                    "estimated_vram_gb": vram,
                    "fits_single_gpu": feasible,
                    "feasibility_note": reason
                })
            return results
    except Exception as e:
        logger.error(f"[HFModelSearch] Failed to query Hugging Face API: {e}")
        return []
