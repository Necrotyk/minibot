# SPDX-License-Identifier: AGPL-3.0-or-later
"""
kaggle_worker_template.py — Standalone Dual-GPU In-Notebook Supervisor with Cloudflare Named/Quick Tunnel.

Features:
1. GPU Hardware Topology Auto-Detection (1x T4/P100 vs 2x T4):
   - Single-GPU (16GB): Slot 0 runs flagship Gemma 4 Uncensored with full VRAM & 8k-16k context window.
   - Dual-GPU (32GB): Slot 0 runs Gemma 4 Uncensored (:8081); Slot 1 runs Qwen 2.5 Coder (:8082).
2. Dynamic Model Resolution & LRU Disk Cache:
   - Supports Gemma 4 Uncensored (12B Q4 / E4B), Dolphin 7B, Qwen Coder, Hermes 8B, and arbitrary Hugging Face GGUFs.
   - Inspects /kaggle/input/ datasets first (0s instant mount); streams to /kaggle/working/models/ with resume (-c).
   - Enforces LRU model pruning to maintain >= 6GB free disk on Kaggle's 20GB disk limit.
3. In-Notebook Admin API & Process Watchdog:
   - POST /admin/swap_model: Hot-swaps slot models live without dropping the Cloudflare tunnel.
   - POST /admin/shutdown: Gracefully terminates child processes.
   - Autonomous background supervisor: Auto-restarts crashed llama-server instances within 5 seconds.
4. Dual-Mode Cloudflare Tunnel:
   - Named Tunnel: When CLOUDFLARE_TUNNEL_TOKEN is set.
   - Quick Tunnel (trycloudflare.com): Captured via unbuffered stderr pipe, emitting [CLOUDFLARE_URL] to stdout.
5. Zero-Trust Security Hardening:
   - Subprocesses executed with shell=False argument vectors (SEC-2026-003).
   - Bearer token / X-Kaggle-Secret authentication on all non-health proxy endpoints.
"""

import http.server
import json
import logging
import os
import re
import shutil
import socketserver
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

# Enforce unbuffered stdout immediately for real-time Kaggle log polling
try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:
    pass
os.environ["PYTHONUNBUFFERED"] = "1"

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [KaggleWorker] %(message)s",
    datefmt="%H:%M:%S"
)
logger = logging.getLogger("KaggleWorker")

PROXY_PORT = 8080
SLOT0_PORT = 8081  # Slot 0: Uncensored Persona / Primary
SLOT1_PORT = 8082  # Slot 1: Coding / Task Runner (Dual-GPU only)

DATASET_DIR = "/kaggle/input/dystopi-llm-models"
SCRATCH_DIR = "/kaggle/working"
MODELS_DIR = os.path.join(SCRATCH_DIR, "models")
TUNNEL_URL_FILE = os.path.join(SCRATCH_DIR, "tunnel_url.txt")

WORKER_SECRET = os.environ.get("KAGGLE_WORKER_SECRET", "dystopi-kaggle-sec-2026")
CLOUDFLARE_TOKEN = os.environ.get("CLOUDFLARE_TUNNEL_TOKEN", "")
HF_TOKEN = os.environ.get("HF_TOKEN", "")

# Curated Presets
MODEL_PRESETS: Dict[str, Dict[str, Any]] = {
    "gemma4_uncensored": {
        "repo": "mradermacher/gemma-4-12B-it-abliterated-uncensored-GGUF",
        "file": "gemma-4-12B-it-abliterated-uncensored.Q4_K_M.gguf",
        "fallback_url": "https://huggingface.co/mradermacher/gemma-4-12B-it-abliterated-uncensored-GGUF/resolve/main/gemma-4-12B-it-abliterated-uncensored.Q4_K_M.gguf",
        "role": "uncensored"
    },
    "gemma4_e4b_uncensored": {
        "repo": "HauhauCS/Gemma-4-E4B-Uncensored",
        "file": "Gemma-4-E4B-Uncensored-Aggressive.Q4_K_M.gguf",
        "fallback_url": "https://huggingface.co/HauhauCS/Gemma-4-E4B-Uncensored/resolve/main/Gemma-4-E4B-Uncensored-Aggressive.Q4_K_M.gguf",
        "role": "uncensored"
    },
    "dolphin-7b": {
        "repo": "cognitivecomputations/dolphin-2.9.2-qwen2-7b-GGUF",
        "file": "dolphin-2.9.2-qwen2-7b-Q4_K_M.gguf",
        "fallback_url": "https://huggingface.co/cognitivecomputations/dolphin-2.9.2-qwen2-7b-GGUF/resolve/main/dolphin-2.9.2-qwen2-7b-Q4_K_M.gguf",
        "role": "uncensored"
    },
    "qwen-coder": {
        "repo": "Qwen/Qwen2.5-Coder-7B-Instruct-GGUF",
        "file": "qwen2.5-coder-7b-instruct-q4_k_m.gguf",
        "fallback_url": "https://huggingface.co/Qwen/Qwen2.5-Coder-7B-Instruct-GGUF/resolve/main/qwen2.5-coder-7b-instruct-q4_k_m.gguf",
        "role": "coding"
    },
    "hermes-8b": {
        "repo": "NousResearch/Hermes-3-Llama-3.1-8B-GGUF",
        "file": "Hermes-3-Llama-3.1-8B.Q4_K_M.gguf",
        "fallback_url": "https://huggingface.co/NousResearch/Hermes-3-Llama-3.1-8B-GGUF/resolve/main/Hermes-3-Llama-3.1-8B.Q4_K_M.gguf",
        "role": "uncensored"
    }
}

# Runtime Global State
ACTIVE_GPU_COUNT = 1
PROC_SLOTS: Dict[int, Optional[subprocess.Popen]] = {0: None, 1: None}
ACTIVE_MODELS: Dict[int, str] = {0: "", 1: ""}
SLOT_STATES: Dict[int, str] = {0: "STOPPED", 1: "STOPPED"}
ACTIVE_REQUESTS: Dict[int, int] = {0: 0, 1: 0}
STATE_LOCK = threading.RLock()
PROC_CLOUDFLARED: Optional[subprocess.Popen] = None
SUPERVISOR_RUNNING = True


def detect_gpu_topology() -> Tuple[int, str]:
    """Detects available NVIDIA CUDA GPUs and returns (count, device_name)."""
    try:
        res = subprocess.run(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
            capture_output=True, text=True, check=True
        )
        lines = [l.strip() for l in res.stdout.strip().splitlines() if l.strip()]
        if lines:
            count = len(lines)
            logger.info(f"Detected {count} NVIDIA GPU(s): {', '.join(lines)}")
            return count, lines[0]
    except Exception:
        pass

    # Fallback to PyTorch CUDA detection if available
    try:
        import torch
        if torch.cuda.is_available():
            count = torch.cuda.device_count()
            name = torch.cuda.get_device_name(0)
            logger.info(f"Torch detected {count} GPU(s): {name}")
            return count, name
    except Exception:
        pass

    logger.warning("No NVIDIA GPUs detected via nvidia-smi/torch. Defaulting to 1 GPU.")
    return 1, "Unknown GPU"


def resolve_llama_server_binary() -> str:
    """Finds CUDA-enabled llama-server binary in dataset, scratch, or downloads."""
    candidates = [
        os.path.join(DATASET_DIR, "llama-server"),
        os.path.join(SCRATCH_DIR, "llama-server"),
        "/usr/local/bin/llama-server",
        "/tmp/llama-server"
    ]
    for c in candidates:
        if os.path.exists(c) and os.access(c, os.X_OK):
            logger.info(f"Using existing llama-server binary: {c}")
            return c

    # Download precompiled CUDA 12 binary from official llama.cpp release
    target = "/tmp/llama-server"
    logger.info("Downloading precompiled CUDA llama-server binary...")
    url = "https://github.com/ggerganov/llama.cpp/releases/download/b4800/llama-b4800-bin-ubuntu-x64.zip"
    try:
        subprocess.run(["wget", "-q", "-c", url, "-O", "/tmp/llama.zip"], check=True)
        subprocess.run(["unzip", "-q", "-o", "/tmp/llama.zip", "llama-server", "-d", "/tmp"], check=True)
        subprocess.run(["chmod", "+x", target], check=True)
        return target
    except Exception as e:
        logger.warning(f"Failed to fetch official binary bundle: {e}. Checking fallback...")
        return target


def prune_disk_cache_if_needed(min_free_gb: float = 6.0) -> None:
    """Removes oldest unmounted .gguf models if free disk drops below threshold."""
    os.makedirs(MODELS_DIR, exist_ok=True)
    try:
        usage = shutil.disk_usage(SCRATCH_DIR)
        free_gb = usage.free / (1024 ** 3)
        if free_gb >= min_free_gb:
            return

        logger.warning(f"Disk space low ({free_gb:.1f}GB free < {min_free_gb}GB). Pruning LRU model cache...")
        active_paths = set(ACTIVE_MODELS.values())
        cached_files: List[Tuple[str, float]] = []

        for f in os.listdir(MODELS_DIR):
            if f.endswith(".gguf"):
                full_path = os.path.join(MODELS_DIR, f)
                if full_path not in active_paths:
                    cached_files.append((full_path, os.path.getatime(full_path)))

        # Sort by access time ascending (oldest first)
        cached_files.sort(key=lambda x: x[1])
        for fpath, _ in cached_files:
            try:
                sz_gb = os.path.getsize(fpath) / (1024 ** 3)
                os.remove(fpath)
                logger.info(f"Pruned cached model: {os.path.basename(fpath)} ({sz_gb:.2f}GB)")
                new_free = shutil.disk_usage(SCRATCH_DIR).free / (1024 ** 3)
                if new_free >= min_free_gb:
                    break
            except Exception as e:
                logger.warning(f"Failed to prune {fpath}: {e}")
    except Exception as e:
        logger.warning(f"Disk check error: {e}")


def resolve_model_path(model_identifier: str, role: str = "uncensored") -> str:
    """
    Locates or downloads target model:
    1. Check pre-mounted Kaggle datasets (/kaggle/input/)
    2. Check local scratch cache (/kaggle/working/models/)
    3. Stream from Hugging Face with resume
    """
    os.makedirs(MODELS_DIR, exist_ok=True)
    clean_id = (model_identifier or "").strip().lower()

    # 1. Resolve Presets
    preset_data = MODEL_PRESETS.get(clean_id)
    if not preset_data:
        # Check alias keys
        if "gemma" in clean_id:
            preset_data = MODEL_PRESETS["gemma4_e4b_uncensored"] if "e4b" in clean_id else MODEL_PRESETS["gemma4_uncensored"]
        elif "dolphin" in clean_id:
            preset_data = MODEL_PRESETS["dolphin-7b"]
        elif "qwen" in clean_id or "code" in clean_id:
            preset_data = MODEL_PRESETS["qwen-coder"]
        elif "hermes" in clean_id:
            preset_data = MODEL_PRESETS["hermes-8b"]

    filename = ""
    download_url = ""

    if preset_data:
        filename = preset_data["file"]
        download_url = preset_data["fallback_url"]
    elif "/" in clean_id and ":" in clean_id:
        # HuggingFace user/repo:quant format
        repo, quant = clean_id.split(":", 1)
        filename = f"{repo.replace('/', '_')}_{quant}.gguf"
        download_url = f"https://huggingface.co/{repo}/resolve/main/{filename}"
    elif "/" in clean_id:
        # HuggingFace user/repo/filename format
        parts = clean_id.split("/")
        filename = parts[-1] if parts[-1].endswith(".gguf") else f"{parts[-1]}.gguf"
        download_url = f"https://huggingface.co/{parts[0]}/{parts[1]}/resolve/main/{filename}"
    else:
        # Default fallback by role
        preset_key = "gemma4_uncensored" if role == "uncensored" else "qwen-coder"
        preset_data = MODEL_PRESETS[preset_key]
        filename = preset_data["file"]
        download_url = preset_data["fallback_url"]

    # 2. Check pre-mounted Kaggle dataset directories
    dataset_candidates = [
        os.path.join(DATASET_DIR, filename),
        os.path.join("/kaggle/input", filename)
    ]
    # Scan subdirectories of /kaggle/input
    if os.path.exists("/kaggle/input"):
        for d in os.listdir("/kaggle/input"):
            full_d = os.path.join("/kaggle/input", d)
            if os.path.isdir(full_d):
                cand = os.path.join(full_d, filename)
                if os.path.exists(cand):
                    logger.info(f"Using pre-mounted dataset model: {cand}")
                    return cand

    for dc in dataset_candidates:
        if os.path.exists(dc):
            logger.info(f"Using pre-mounted dataset model: {dc}")
            return dc

    # 3. Check scratch models cache
    scratch_target = os.path.join(MODELS_DIR, filename)
    if os.path.exists(scratch_target) and os.path.getsize(scratch_target) > 50 * 1024 * 1024:
        logger.info(f"Using cached scratch model: {scratch_target}")
        return scratch_target

    # 4. Stream from Hugging Face Hub with resume
    prune_disk_cache_if_needed(min_free_gb=6.0)
    logger.info(f"Downloading model '{filename}' from {download_url}...")
    cmd = ["curl", "-C", "-", "-L", "-s", download_url, "-o", scratch_target]
    if HF_TOKEN:
        cmd.extend(["-H", f"Authorization: Bearer {HF_TOKEN}"])

    res = subprocess.run(cmd)
    if res.returncode != 0 or not os.path.exists(scratch_target) or os.path.getsize(scratch_target) < 10 * 1024 * 1024:
        logger.warning(f"curl download failed or partial file. Retrying with wget...")
        wget_cmd = ["wget", "-q", "-c", download_url, "-O", scratch_target]
        if HF_TOKEN:
            wget_cmd.extend(["--header", f"Authorization: Bearer {HF_TOKEN}"])
        subprocess.run(wget_cmd, check=True)

    logger.info(f"Successfully staged model: {scratch_target} ({os.path.getsize(scratch_target) / (1024**3):.2f}GB)")
    return scratch_target


def start_llama_slot(
    slot: int,
    gpu_id: int,
    port: int,
    model_path: str,
    context_len: int = 8192,
    parallel: int = 2
) -> subprocess.Popen:
    """Launches isolated llama-server instance pinned to specific GPU with Flash Attention."""
    bin_path = resolve_llama_server_binary()
    env = os.environ.copy()
    env["CUDA_VISIBLE_DEVICES"] = str(gpu_id)
    env["LD_LIBRARY_PATH"] = f"/usr/local/cuda/lib64:{env.get('LD_LIBRARY_PATH', '')}"

    cmd = [
        bin_path,
        "-m", model_path,
        "--host", "127.0.0.1",
        "--port", str(port),
        "-c", str(context_len),
        "-np", str(parallel),
        "-ngl", "99",  # Offload all layers to GPU
        "-fa",         # Flash Attention optimization
        "--api-key", WORKER_SECRET
    ]

    logger.info(f"Booting Slot {slot} on GPU {gpu_id} (Port {port}, Context: {context_len}, FA enabled)...")
    proc = subprocess.Popen(cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    with STATE_LOCK:
        PROC_SLOTS[slot] = proc
        ACTIVE_MODELS[slot] = model_path
        SLOT_STATES[slot] = "STARTING"
    return proc


def is_backend_healthy(port: int) -> bool:
    """Checks if local llama-server on port is responding with 200 OK."""
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/health")
        with urllib.request.urlopen(req, timeout=2.0) as resp:
            return resp.status == 200
    except Exception:
        return False


def supervisor_watchdog_loop():
    """Background supervisor detecting process exits and auto-relaunching slots within 5s."""
    logger.info("Autonomous in-notebook supervisor watchdog active.")
    while SUPERVISOR_RUNNING:
        time.sleep(3)
        with STATE_LOCK:
            for slot, proc in list(PROC_SLOTS.items()):
                if proc is not None:
                    ret = proc.poll()
                    if ret is not None:
                        # Process died unexpectedly
                        m_path = ACTIVE_MODELS.get(slot, "")
                        logger.error(f"[SUPERVISOR] Slot {slot} process died (exit code {ret}). Auto-recovering...")
                        SLOT_STATES[slot] = "RECOVERING"
                        port = SLOT0_PORT if slot == 0 else SLOT1_PORT
                        gpu = 0 if slot == 0 else min(1, ACTIVE_GPU_COUNT - 1)
                        ctx = 4096 if ret != 0 else 8192  # Clamp context on crash
                        try:
                            start_llama_slot(slot=slot, gpu_id=gpu, port=port, model_path=m_path, context_len=ctx)
                        except Exception as e:
                            logger.error(f"[SUPERVISOR] Recovery failed for slot {slot}: {e}")


class ProxyHandler(http.server.BaseHTTPRequestHandler):
    """
    In-Notebook Reverse Proxy & Dynamic Slot Multiplexer.
    Exposes /health, /admin/status, /admin/swap_model, /admin/shutdown, and OpenAI completions.
    """

    def _check_auth(self) -> bool:
        auth_header = self.headers.get("Authorization", "")
        if auth_header == f"Bearer {WORKER_SECRET}":
            return True
        x_secret = self.headers.get("X-Kaggle-Secret", "")
        if x_secret == WORKER_SECRET:
            return True
        return False

    def do_GET(self):
        # 1. Zero-502 Health Probe (Unauthenticated)
        if self.path in ("/health", "/healthz"):
            h0 = is_backend_healthy(SLOT0_PORT)
            h1 = is_backend_healthy(SLOT1_PORT) if ACTIVE_GPU_COUNT > 1 else False
            ready = h0 or (h1 and ACTIVE_GPU_COUNT > 1)
            status = 200 if ready else 503

            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({
                "status": "HEALTHY" if ready else "INITIALIZING",
                "gpu_count": ACTIVE_GPU_COUNT,
                "slot0_uncensored_ready": h0,
                "slot1_coding_ready": h1,
                "slot0_model": os.path.basename(ACTIVE_MODELS.get(0, "")),
                "slot1_model": os.path.basename(ACTIVE_MODELS.get(1, ""))
            }).encode())
            return

        if not self._check_auth():
            self.send_response(401)
            self.end_headers()
            self.wfile.write(b'{"error": "Unauthorized"}')
            return

        # 2. Admin Status Inspection
        if self.path.startswith("/admin/status"):
            usage = shutil.disk_usage(SCRATCH_DIR)
            st = {
                "status": "READY",
                "gpu_count": ACTIVE_GPU_COUNT,
                "slots": {
                    "0": {
                        "port": SLOT0_PORT,
                        "healthy": is_backend_healthy(SLOT0_PORT),
                        "model": ACTIVE_MODELS.get(0, ""),
                        "state": SLOT_STATES.get(0, "UNKNOWN"),
                        "active_requests": ACTIVE_REQUESTS.get(0, 0)
                    },
                    "1": {
                        "port": SLOT1_PORT,
                        "healthy": is_backend_healthy(SLOT1_PORT) if ACTIVE_GPU_COUNT > 1 else False,
                        "model": ACTIVE_MODELS.get(1, ""),
                        "state": SLOT_STATES.get(1, "UNKNOWN"),
                        "active_requests": ACTIVE_REQUESTS.get(1, 0)
                    }
                },
                "disk": {
                    "total_gb": round(usage.total / (1024 ** 3), 2),
                    "free_gb": round(usage.free / (1024 ** 3), 2)
                }
            }
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(st).encode())
            return

        # Default GET: Proxy to Slot 0
        self._proxy_request(SLOT0_PORT)

    def do_POST(self):
        if not self._check_auth():
            self.send_response(401)
            self.end_headers()
            self.wfile.write(b'{"error": "Unauthorized"}')
            return

        content_len = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_len) if content_len > 0 else b"{}"

        # 1. Live Hot-Swap Endpoint (/admin/swap_model or /admin/swap)
        if self.path in ("/admin/swap_model", "/admin/swap"):
            self._handle_swap_model(body)
            return

        # 2. Graceful Shutdown Endpoint (/admin/shutdown)
        if self.path == "/admin/shutdown":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status": "SHUTTING_DOWN"}')
            threading.Thread(target=self._execute_shutdown, daemon=True).start()
            return

        # 3. Standard OpenAI-Compatible Completions Routing
        target_port = SLOT0_PORT
        target_slot = 0
        try:
            parsed = json.loads(body.decode("utf-8"))
            model_str = str(parsed.get("model", "")).lower()
            role_hdr = self.headers.get("X-Model-Role", "").lower()

            if ACTIVE_GPU_COUNT > 1 and ("code" in model_str or role_hdr in ("coding", "task")):
                target_port = SLOT1_PORT
                target_slot = 1
        except Exception:
            pass

        self._proxy_request(target_port, body, slot=target_slot)

    def _handle_swap_model(self, body_bytes: bytes):
        """Live-swaps model on specified slot with request draining."""
        try:
            payload = json.loads(body_bytes.decode("utf-8"))
            slot = int(payload.get("slot", 0))
            new_model = str(payload.get("model", "")).strip()
            if not new_model:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b'{"error": "Missing model identifier."}')
                return

            if slot == 1 and ACTIVE_GPU_COUNT < 2:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b'{"error": "Slot 1 unavailable on single-GPU topology."}')
                return

            start_t = time.time()
            logger.info(f"Initiating live hot-swap on Slot {slot} -> '{new_model}'...")

            with STATE_LOCK:
                SLOT_STATES[slot] = "DRAINING"

            # Drain active requests (wait up to 6 seconds)
            drain_wait = time.time()
            while ACTIVE_REQUESTS.get(slot, 0) > 0 and (time.time() - drain_wait < 6.0):
                time.sleep(0.3)

            # Terminate old process
            with STATE_LOCK:
                old_proc = PROC_SLOTS.get(slot)
                if old_proc and old_proc.poll() is None:
                    old_proc.terminate()
                    try:
                        old_proc.wait(timeout=3.0)
                    except Exception:
                        old_proc.kill()
                PROC_SLOTS[slot] = None

            # Resolve/Download new model
            role = "coding" if slot == 1 else "uncensored"
            m_path = resolve_model_path(new_model, role=role)

            # Launch new llama-server
            port = SLOT0_PORT if slot == 0 else SLOT1_PORT
            gpu = 0 if slot == 0 else min(1, ACTIVE_GPU_COUNT - 1)
            ctx = 8192 if ACTIVE_GPU_COUNT > 1 else 16384
            new_proc = start_llama_slot(slot=slot, gpu_id=gpu, port=port, model_path=m_path, context_len=ctx)

            # Wait for 200 OK on /health
            healthy = False
            for _ in range(25):  # 50s wait
                time.sleep(2)
                if is_backend_healthy(port):
                    healthy = True
                    break

            duration = time.time() - start_t
            if healthy:
                with STATE_LOCK:
                    SLOT_STATES[slot] = "READY"
                logger.info(f"✅ Slot {slot} successfully swapped to '{new_model}' in {duration:.1f}s.")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({
                    "status": "OK",
                    "slot": slot,
                    "model": new_model,
                    "duration_s": round(duration, 2)
                }).encode())
            else:
                with STATE_LOCK:
                    SLOT_STATES[slot] = "FAILED"
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({
                    "status": "ERROR",
                    "error": f"Slot {slot} failed health probe after hot-swap."
                }).encode())

        except Exception as e:
            logger.error(f"Model swap error: {e}")
            self.send_response(500)
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode())

    def _execute_shutdown(self):
        global SUPERVISOR_RUNNING
        logger.info("Executing graceful worker shutdown...")
        SUPERVISOR_RUNNING = False
        time.sleep(1)
        for proc in PROC_SLOTS.values():
            if proc and proc.poll() is None:
                proc.terminate()
        if PROC_CLOUDFLARED and PROC_CLOUDFLARED.poll() is None:
            PROC_CLOUDFLARED.terminate()
        os._exit(0)

    def _proxy_request(self, target_port: int, body: Optional[bytes] = None, slot: int = 0):
        target_url = f"http://127.0.0.1:{target_port}{self.path}"
        headers = {k: v for k, v in self.headers.items() if k.lower() != "host"}
        headers["Host"] = f"127.0.0.1:{target_port}"

        req = urllib.request.Request(target_url, data=body, headers=headers, method=self.command)
        with STATE_LOCK:
            ACTIVE_REQUESTS[slot] = ACTIVE_REQUESTS.get(slot, 0) + 1

        try:
            # Scaled 180s proxy timeout for heavy code generation tasks
            with urllib.request.urlopen(req, timeout=180.0) as resp:
                self.send_response(resp.status)
                for k, v in resp.getheaders():
                    if k.lower() not in ("transfer-encoding", "content-length"):
                        self.send_header(k, v)
                resp_body = resp.read()
                self.send_header("Content-Length", str(len(resp_body)))
                self.end_headers()
                self.wfile.write(resp_body)
        except urllib.error.HTTPError as he:
            self.send_response(he.code)
            self.end_headers()
            self.wfile.write(he.read())
        except Exception as e:
            self.send_response(502)
            self.end_headers()
            self.wfile.write(json.dumps({"error": f"Backend gateway error: {e}"}).encode())
        finally:
            with STATE_LOCK:
                ACTIVE_REQUESTS[slot] = max(0, ACTIVE_REQUESTS.get(slot, 1) - 1)


def start_proxy_server():
    server = socketserver.ThreadingTCPServer(("0.0.0.0", PROXY_PORT), ProxyHandler)
    logger.info(f"In-Notebook Proxy active on port {PROXY_PORT}...")
    server.serve_forever()


def start_cloudflared_tunnel():
    """Starts Cloudflare Named Tunnel or Quick Tunnel with unbuffered stderr discovery."""
    global PROC_CLOUDFLARED

    logger.info("Ensuring cloudflared binary...")
    cf_bin = "/tmp/cloudflared"
    if not (os.path.exists(cf_bin) and os.access(cf_bin, os.X_OK)):
        subprocess.run(
            ["wget", "-q", "-c", "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64", "-O", cf_bin],
            check=True
        )
        subprocess.run(["chmod", "+x", cf_bin], check=True)

    if CLOUDFLARE_TOKEN:
        logger.info("Launching Cloudflare Named Tunnel...")
        cmd = [cf_bin, "tunnel", "run", "--token", CLOUDFLARE_TOKEN]
        PROC_CLOUDFLARED = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        logger.info("Launching Cloudflare Quick Tunnel (trycloudflare.com)...")
        cmd = [cf_bin, "tunnel", "--url", f"http://127.0.0.1:{PROXY_PORT}"]
        PROC_CLOUDFLARED = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1
        )

        def _read_quick_tunnel_url():
            url_pattern = re.compile(r'https://[a-zA-Z0-9-]+\.trycloudflare\.com')
            for line in iter(PROC_CLOUDFLARED.stderr.readline, ''):
                m = url_pattern.search(line)
                if m:
                    tunnel_url = m.group(0)
                    print(f"\n[CLOUDFLARE_URL] {tunnel_url}\n", flush=True)
                    logger.info(f"🌐 Cloudflare Quick Tunnel live: {tunnel_url}")
                    try:
                        with open(TUNNEL_URL_FILE, "w") as f:
                            f.write(tunnel_url)
                    except Exception:
                        pass
                    break

        threading.Thread(target=_read_quick_tunnel_url, daemon=True).start()


def main():
    global ACTIVE_GPU_COUNT
    logger.info("🚀 Booting Dystopi Minibots Autonomous GPU Worker...")

    # 1. Hardware Topology Detection
    ACTIVE_GPU_COUNT, gpu_name = detect_gpu_topology()
    logger.info(f"Hardware Topology: {ACTIVE_GPU_COUNT}x GPU ({gpu_name})")

    # 2. Model Resolution
    # Flagship: Gemma 4 Uncensored (Slot 0)
    m_slot0 = resolve_model_path("gemma4_uncensored", role="uncensored")
    m_slot1 = resolve_model_path("qwen-coder", role="coding") if ACTIVE_GPU_COUNT > 1 else ""

    # 3. Launch Slot 0 (GPU 0)
    ctx_slot0 = 8192 if ACTIVE_GPU_COUNT > 1 else 16384
    start_llama_slot(slot=0, gpu_id=0, port=SLOT0_PORT, model_path=m_slot0, context_len=ctx_slot0, parallel=2)

    # 4. Launch Slot 1 (GPU 1) only if Dual-GPU is available
    if ACTIVE_GPU_COUNT > 1:
        start_llama_slot(slot=1, gpu_id=1, port=SLOT1_PORT, model_path=m_slot1, context_len=8192, parallel=1)

    # 5. Start Background Supervisor Watchdog
    watchdog_t = threading.Thread(target=supervisor_watchdog_loop, daemon=True)
    watchdog_t.start()

    # 6. Start In-Notebook HTTP Proxy on :8080
    proxy_t = threading.Thread(target=start_proxy_server, daemon=True)
    proxy_t.start()

    # 7. Zero-502 Health Gate: Wait until required backends are healthy
    logger.info("Waiting for local GPU models to achieve 200 OK...")
    start_wait = time.time()
    while time.time() - start_wait < 90:
        h0 = is_backend_healthy(SLOT0_PORT)
        h1 = is_backend_healthy(SLOT1_PORT) if ACTIVE_GPU_COUNT > 1 else True
        if h0 and h1:
            logger.info("✅ Active GPU model(s) HEALTHY (200 OK).")
            with STATE_LOCK:
                SLOT_STATES[0] = "READY"
                if ACTIVE_GPU_COUNT > 1:
                    SLOT_STATES[1] = "READY"
            break
        time.sleep(2)

    # 8. Start Cloudflare Tunnel
    start_cloudflared_tunnel()

    logger.info("Worker fully operational and serving requests.")
    while SUPERVISOR_RUNNING:
        time.sleep(30)


if __name__ == "__main__":
    main()
