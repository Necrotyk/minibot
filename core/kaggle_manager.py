# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

"""
kaggle_manager.py — Autonomous Kaggle GPU Burst Worker & Multi-Account Lifecycle Controller.

Features:
1. Hybrid Multi-Account Management:
   - Primary instance auto-failover on 30h quota exhaustion.
   - Concurrent named instances (e.g. primary vs worker-dev) with independent endpoints.
2. Dual-Mode Cloudflare Tunnels:
   - Named Tunnel with static tokens and domain endpoints.
   - Quick Tunnel (trycloudflare.com) auto-discovery via 120s container log polling.
3. Hermetic Staging Directory (/tmp/kaggle_stage_<slug>):
   - Stages _runner.py and kernel-metadata.json in tmpfs before push (Master Rule §10).
   - Zero repo files dirtied, untracked, or lingering on disk.
4. Live In-Notebook Hot-Swapping & Process Management:
   - Dispatches POST /admin/swap_model to running workers without dropping tunnels.
5. Role-Based Routing & Multi-Instance Registry (data/kaggle_state.json):
   - Routes uncensored vs coding queries to matching active instances.
   - Atomic state persistence.
6. 20-minute idle watchdog & 11h rollover supervisor.
"""

import asyncio
import datetime
import hashlib
import hmac
import json
import logging
import os
import re
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import keystore
from core.hf_model_search import sanitize_model_identifier

logger = logging.getLogger("KaggleManager")

DEFAULT_IDLE_TIMEOUT_SECONDS = 1200  # 20 minutes
MAX_SESSION_SECONDS = 39600          # 11 hours (rollover before 12h Kaggle hard kill)
FIXED_TUNNEL_ENDPOINT = os.environ.get("KAGGLE_TUNNEL_ENDPOINT", "")

BLUE_SLUG = "dystopi-minibot-worker-blue"
GREEN_SLUG = "dystopi-minibot-worker-green"

DEFAULT_MODELS = [
    "gemma4_uncensored",
    "gemma4_e4b_uncensored",
    "dolphin-2.9.2-qwen2-7b",
    "qwen2.5-coder-7b-instruct",
    "hermes-3-llama-3.1-8b",
    "deepseek-coder-6.7b-instruct"
]


class KaggleKernelManager:
    """Orchestrates Kaggle GPU notebooks, multi-account rollover, and hybrid concurrency."""

    def __init__(self, config: Optional[Dict[str, Any]] = None, broadcast_callback: Optional[Any] = None):
        self.config = config or {}
        self.broadcast_callback = broadcast_callback
        self.active_slug = BLUE_SLUG
        self.kernel_status = "STOPPED"
        self.current_model = "gemma4_uncensored"
        self.worker_url = self.config.get("KAGGLE_GPU_URL", FIXED_TUNNEL_ENDPOINT)
        self.worker_secret = keystore.get_secret("KAGGLE_WORKER_SECRET", "minibot-kaggle-sec-token")
        self.cf_token = keystore.get_secret("CLOUDFLARE_TUNNEL_TOKEN", "")
        self.hf_token = keystore.get_secret("HF_TOKEN", "")

        self.last_activity_ts = time.time()
        self.boot_timestamp: Optional[float] = None
        self.active_generations = 0

        # Multi-Account Pool
        self.accounts: List[Dict[str, Any]] = self._load_accounts()
        self.active_account_idx = 0

        # Multi-Instance Registry: instance_id -> dict
        self.instances: Dict[str, Dict[str, Any]] = {
            "primary": {
                "slug": BLUE_SLUG,
                "status": "STOPPED",
                "model": self.current_model,
                "account": self.accounts[0].get("username", "") if self.accounts else "",
                "worker_url": self.worker_url,
                "tunnel_type": "named" if self.cf_token else "quick",
                "role": "uncensored",
                "boot_timestamp": None,
                "last_activity_ts": time.time()
            }
        }

        # State persistence file
        self.state_file = self._resolve_state_file()
        self._load_state()

        self._watchdog_task: Optional[asyncio.Task] = None
        self._rollover_task: Optional[asyncio.Task] = None
        self._drain_lock = asyncio.Lock()
        self._polling_tasks: Dict[str, asyncio.Task] = {}

    def _resolve_state_file(self) -> Path:
        base_dir = Path(__file__).resolve().parent.parent / "data"
        base_dir.mkdir(parents=True, exist_ok=True)
        return base_dir / "kaggle_state.json"

    def _load_accounts(self) -> List[Dict[str, Any]]:
        """Loads Kaggle credentials pool from keystore or config."""
        raw = keystore.get_secret("KAGGLE_ACCOUNTS", "")
        if raw:
            try:
                accs = json.loads(raw)
                if isinstance(accs, list) and accs:
                    return accs
            except Exception:
                pass

        # Fallback to single account
        user = keystore.get_secret("KAGGLE_USERNAME", "")
        key = keystore.get_secret("KAGGLE_KEY", "")
        if user and key:
            return [{"username": user, "key": key, "name": "primary"}]
        return []

    def _save_state(self) -> None:
        """Persists runtime kernel state with atomic tmp-file swap."""
        try:
            state = {
                "active_slug": self.active_slug,
                "kernel_status": self.kernel_status,
                "current_model": self.current_model,
                "active_account_idx": self.active_account_idx,
                "boot_timestamp": self.boot_timestamp,
                "last_activity_ts": self.last_activity_ts,
                "instances": self.instances
            }
            tmp_path = self.state_file.with_suffix(".tmp")
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2)
            os.replace(tmp_path, self.state_file)
        except Exception as e:
            logger.warning(f"[KaggleManager] Failed to persist state: {e}")

    def _load_state(self) -> None:
        """Hydrates previous state on startup."""
        if self.state_file.exists():
            try:
                with open(self.state_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self.active_slug = data.get("active_slug", BLUE_SLUG)
                self.active_account_idx = data.get("active_account_idx", 0)
                self.boot_timestamp = data.get("boot_timestamp")
                self.current_model = data.get("current_model", self.current_model)
                if "instances" in data and isinstance(data["instances"], dict):
                    self.instances = data["instances"]
                    if "primary" in self.instances:
                        p = self.instances["primary"]
                        self.kernel_status = p.get("status", self.kernel_status)
                        self.worker_url = p.get("worker_url", self.worker_url)
            except Exception as e:
                logger.debug(f"[KaggleManager] Could not read previous state: {e}")

    def record_activity(self, instance_id: str = "primary") -> None:
        """Resets idle timer upon processing prompt or chatops command."""
        self.last_activity_ts = time.time()
        if instance_id in self.instances:
            self.instances[instance_id]["last_activity_ts"] = self.last_activity_ts
        self._save_state()

    def start_watchdog(self) -> None:
        """Launches background 20m idle watchdog and 11h rollover supervisor."""
        if self._watchdog_task is None or self._watchdog_task.done():
            self._watchdog_task = asyncio.create_task(self._idle_watchdog_loop())
        if self._rollover_task is None or self._rollover_task.done():
            self._rollover_task = asyncio.create_task(self._rollover_supervisor_loop())

    async def _idle_watchdog_loop(self) -> None:
        """Halts GPU kernel after 20 minutes of complete inactivity."""
        logger.info("[KaggleManager] Autonomous 20m idle watchdog started.")
        while True:
            await asyncio.sleep(30)
            if self.kernel_status in ("RUNNING", "READY", "STARTING"):
                idle_seconds = time.time() - self.last_activity_ts
                if idle_seconds >= DEFAULT_IDLE_TIMEOUT_SECONDS and self.active_generations == 0:
                    logger.warning(f"[KaggleManager] Idle timeout reached ({idle_seconds:.0f}s). Stopping kernel to conserve quota.")
                    success, msg = await self.stop_kernel()
                    if self.broadcast_callback:
                        try:
                            res = self.broadcast_callback(
                                f"[KAGGLE-WATCHDOG] ⏱️ GPU kernel auto-stopped after 20m idle to conserve quota. Status: {msg}"
                            )
                            if asyncio.iscoroutine(res) or hasattr(res, "__await__"):
                                asyncio.create_task(res)
                        except Exception:
                            pass

    async def _rollover_supervisor_loop(self) -> None:
        """Proactively rolls over Blue/Green kernel at 11h before Kaggle's 12h hard kill."""
        while True:
            await asyncio.sleep(60)
            if self.kernel_status in ("RUNNING", "READY") and self.boot_timestamp:
                uptime = time.time() - self.boot_timestamp
                # 10.5 Hours: Warning
                if 37800 <= uptime < 37860:
                    if self.broadcast_callback:
                        self.broadcast_callback("[KAGGLE] ⚠️ Active kernel reached 10.5h uptime. Graceful rollover scheduled in 30 mins.")
                # 11.0 Hours: Execute Rollover
                elif uptime >= MAX_SESSION_SECONDS:
                    logger.info(f"[KaggleManager] Kernel reached 11h uptime ({uptime:.0f}s). Initiating Blue/Green rollover...")
                    asyncio.create_task(self._execute_rollover())
                    break

    async def _execute_rollover(self) -> None:
        """Spins up alternate Blue/Green slug, achieves 200 OK, and terminates old container."""
        retired_slug = self.active_slug
        next_slug = GREEN_SLUG if self.active_slug == BLUE_SLUG else BLUE_SLUG

        logger.info(f"[KaggleManager] Rolling over from '{retired_slug}' -> '{next_slug}'...")
        self.active_slug = next_slug

        success, msg = await self.start_kernel(model=self.current_model)
        if success:
            healthy = False
            for _ in range(24):  # 120 seconds
                await asyncio.sleep(5)
                if await self.probe_health():
                    healthy = True
                    break

            if healthy:
                await self._stop_slug(retired_slug)
                logger.info(f"[KaggleManager] ✅ Rollover complete: '{next_slug}' active, '{retired_slug}' stopped.")
                if self.broadcast_callback:
                    self.broadcast_callback(f"[KAGGLE] ✅ Zero-downtime rollover complete. Switched to '{next_slug}'.")
            else:
                logger.error(f"[KaggleManager] Rollover kernel '{next_slug}' failed health check.")

    def _prepare_ephemeral_runner(
        self,
        target_slug: str,
        account_username: str,
        cf_token: Optional[str] = None
    ) -> Tuple[str, str]:
        """
        Hermetic Kernel Push Preparation:
        Assembles _runner.py and kernel-metadata.json in an isolated tmpfs directory
        (/tmp/kaggle_stage_<slug>), keeping minibots/ git tree 100% clean (Master Rule §10).
        """
        stage_dir = Path(f"/tmp/kaggle_stage_{target_slug}")
        stage_dir.mkdir(parents=True, exist_ok=True)

        kernel_dir = Path(__file__).resolve().parent.parent / "kaggle"
        template_file = kernel_dir / "kaggle_worker_template.py"
        runner_file = stage_dir / "_runner.py"
        meta_file = stage_dir / "kernel-metadata.json"

        with open(template_file, "r", encoding="utf-8") as f:
            content = f.read()

        effective_cf = cf_token if cf_token is not None else self.cf_token
        secret_header = (
            f"# Ephemeral Injected Header (Auto-Generated — Do Not Commit)\n"
            f"import os\n"
            f"os.environ['KAGGLE_WORKER_SECRET'] = {json.dumps(self.worker_secret)}\n"
            f"os.environ['CLOUDFLARE_TUNNEL_TOKEN'] = {json.dumps(effective_cf or '')}\n"
        )
        if self.hf_token:
            secret_header += f"os.environ['HF_TOKEN'] = {json.dumps(self.hf_token)}\n"
        secret_header += "\n"

        with open(runner_file, "w", encoding="utf-8") as f:
            f.write(secret_header + content)
        try:
            os.chmod(runner_file, 0o600)
        except Exception:
            pass

        meta_data = {
            "id": f"{account_username}/{target_slug}",
            "title": f"Dystopi Minibot GPU Worker ({target_slug})",
            "code_file": "_runner.py",
            "language": "python",
            "kernel_type": "script",
            "is_private": "true",
            "enable_gpu": "true",
            "enable_tpu": "false",
            "enable_internet": "true",
            "dataset_sources": [f"{account_username}/dystopi-llm-models"],
            "competition_sources": [],
            "kernel_sources": []
        }
        with open(meta_file, "w", encoding="utf-8") as f:
            json.dump(meta_data, f, indent=2)

        return str(stage_dir), "_runner.py"

    async def start_kernel(
        self,
        model: Optional[str] = None,
        account_name: Optional[str] = None,
        instance_id: str = "primary",
        role: str = "uncensored"
    ) -> Tuple[bool, str]:
        """Pushes and starts Kaggle GPU kernel via CLI with multi-account rotation and hermetic staging."""
        self.record_activity(instance_id)
        if model:
            valid, clean_m, err = sanitize_model_identifier(model)
            if not valid:
                return False, f"Invalid model identifier: {err}"
            self.current_model = clean_m
            if instance_id in self.instances:
                self.instances[instance_id]["model"] = clean_m

        if not self.accounts:
            return False, "No Kaggle accounts configured in keystore (KAGGLE_ACCOUNTS or KAGGLE_USERNAME/KEY)."

        target_slug = f"dystopi-minibot-{instance_id}" if instance_id != "primary" else self.active_slug
        attempts = 0
        total_accounts = len(self.accounts)

        # Match requested account or use active rotation index
        acc_idx = self.active_account_idx
        if account_name:
            for idx, acc in enumerate(self.accounts):
                if acc.get("name", "").lower() == account_name.lower() or acc.get("username", "").lower() == account_name.lower():
                    acc_idx = idx
                    break

        while attempts < total_accounts:
            acc = self.accounts[acc_idx]
            username = acc.get("username", "")
            key = acc.get("key", "")
            acc_token = acc.get("tunnel_token", self.cf_token)

            env = os.environ.copy()
            env["KAGGLE_USERNAME"] = username
            env["KAGGLE_KEY"] = key

            stage_dir, _ = self._prepare_ephemeral_runner(target_slug, username, cf_token=acc_token)

            def _push():
                try:
                    return subprocess.run(
                        ["kaggle", "kernels", "push", "-p", stage_dir],
                        capture_output=True,
                        text=True,
                        env=env,
                        timeout=30
                    )
                finally:
                    # Clean staging directory immediately after execution
                    shutil.rmtree(stage_dir, ignore_errors=True)

            try:
                loop = asyncio.get_running_loop()
                res = await loop.run_in_executor(None, _push)
                if res.returncode == 0:
                    self.kernel_status = "STARTING"
                    self.boot_timestamp = time.time()
                    self.instances[instance_id] = {
                        "slug": target_slug,
                        "status": "STARTING",
                        "model": self.current_model,
                        "account": username,
                        "worker_url": self.worker_url if acc_token else "",
                        "tunnel_type": "named" if acc_token else "quick",
                        "role": role,
                        "boot_timestamp": self.boot_timestamp,
                        "last_activity_ts": time.time()
                    }
                    self._save_state()
                    self.start_watchdog()

                    # If Quick Tunnel, launch log polling discovery
                    if not acc_token:
                        self._start_quick_tunnel_poller(username, target_slug, instance_id, env)

                    return True, f"Kaggle GPU kernel pushed ({username}/{target_slug}). Booting container..."
                else:
                    err_msg = (res.stderr or res.stdout).strip()
                    if "quota" in err_msg.lower() or "limit" in err_msg.lower():
                        logger.warning(f"[KaggleManager] Quota exhausted on account '{username}'. Rotating to next account...")
                        acc_idx = (acc_idx + 1) % total_accounts
                        self.active_account_idx = acc_idx
                        attempts += 1
                        continue

                    # Handle missing dataset gracefully by retrying without dataset_sources
                    if "dataset" in err_msg.lower() and "not exist" in err_msg.lower():
                        logger.info(f"[KaggleManager] Dataset not found on '{username}'. Retrying with direct HF stream...")
                        stage_dir2, _ = self._prepare_ephemeral_runner(target_slug, username, cf_token=acc_token)
                        meta_file = Path(stage_dir2) / "kernel-metadata.json"
                        with open(meta_file, "r") as mf:
                            mdata = json.load(mf)
                        mdata["dataset_sources"] = []
                        with open(meta_file, "w") as mf:
                            json.dump(mdata, mf, indent=2)

                        def _push2():
                            try:
                                return subprocess.run(
                                    ["kaggle", "kernels", "push", "-p", stage_dir2],
                                    capture_output=True, text=True, env=env, timeout=30
                                )
                            finally:
                                shutil.rmtree(stage_dir2, ignore_errors=True)

                        res = await loop.run_in_executor(None, _push2)
                        if res.returncode == 0:
                            self.kernel_status = "STARTING"
                            self.boot_timestamp = time.time()
                            self.instances[instance_id] = {
                                "slug": target_slug,
                                "status": "STARTING",
                                "model": self.current_model,
                                "account": username,
                                "worker_url": self.worker_url if acc_token else "",
                                "tunnel_type": "named" if acc_token else "quick",
                                "role": role,
                                "boot_timestamp": self.boot_timestamp,
                                "last_activity_ts": time.time()
                            }
                            self._save_state()
                            self.start_watchdog()
                            if not acc_token:
                                self._start_quick_tunnel_poller(username, target_slug, instance_id, env)
                            return True, f"Kaggle GPU kernel pushed with direct HF streaming ({username}/{target_slug})."

                    return False, f"Kaggle push failed on '{username}': {err_msg}"
            except Exception as e:
                return False, f"Error launching kernel: {e}"

        return False, "All Kaggle accounts in the rotation pool have exhausted their weekly GPU quota."

    def _start_quick_tunnel_poller(self, username: str, slug: str, instance_id: str, env: Dict[str, str]) -> None:
        """Starts background 120s polling loop (10s intervals) to discover ephemeral trycloudflare URL."""
        task_key = f"{username}/{slug}"
        if task_key in self._polling_tasks and not self._polling_tasks[task_key].done():
            self._polling_tasks[task_key].cancel()
        self._polling_tasks[task_key] = asyncio.create_task(
            self._poll_quick_tunnel_logs(username, slug, instance_id, env)
        )

    async def _poll_quick_tunnel_logs(self, username: str, slug: str, instance_id: str, env: Dict[str, str]) -> None:
        """Polls container output logs every 10s up to 120s to parse [CLOUDFLARE_URL]."""
        logger.info(f"[KaggleManager] Beginning 120s quick tunnel discovery for {username}/{slug}...")
        log_dir = Path(f"/tmp/kg_{slug}_logs")
        log_dir.mkdir(parents=True, exist_ok=True)
        url_pattern = re.compile(r'\[CLOUDFLARE_URL\]\s+(https://[a-zA-Z0-9-]+\.trycloudflare\.com)')

        def _fetch_output():
            return subprocess.run(
                ["kaggle", "kernels", "output", f"{username}/{slug}", "-p", str(log_dir)],
                capture_output=True, text=True, env=env, timeout=15
            )

        start_t = time.time()
        loop = asyncio.get_running_loop()

        while time.time() - start_t < 120:
            await asyncio.sleep(10)
            try:
                await loop.run_in_executor(None, _fetch_output)
                # Inspect all downloaded log files in log_dir
                for f in log_dir.glob("*"):
                    if f.is_file():
                        try:
                            content = f.read_text(encoding="utf-8", errors="replace")
                            m = url_pattern.search(content)
                            if m:
                                discovered_url = m.group(1).strip()
                                logger.info(f"[KaggleManager] 🌐 Discovered Quick Tunnel for {instance_id}: {discovered_url}")
                                if instance_id in self.instances:
                                    self.instances[instance_id]["worker_url"] = discovered_url
                                if instance_id == "primary":
                                    self.worker_url = discovered_url

                                # Validate /health
                                if await self.probe_health(instance_id=instance_id):
                                    self.kernel_status = "READY"
                                    if instance_id in self.instances:
                                        self.instances[instance_id]["status"] = "READY"
                                    self._save_state()
                                    if self.broadcast_callback:
                                        self.broadcast_callback(
                                            f"[KAGGLE] 🌐 Quick tunnel connected for '{instance_id}': {discovered_url} (READY)"
                                        )
                                    shutil.rmtree(log_dir, ignore_errors=True)
                                    return
                        except Exception:
                            pass
            except Exception as e:
                logger.debug(f"[KaggleManager] Log poll iteration failed: {e}")

        logger.warning(f"[KaggleManager] Quick tunnel polling timed out after 120s for {username}/{slug}.")
        shutil.rmtree(log_dir, ignore_errors=True)

    async def _stop_slug(self, slug: str, account_username: Optional[str] = None) -> bool:
        """Stops a specific kernel slug."""
        if not self.accounts:
            return False
        username = account_username or self.accounts[self.active_account_idx].get("username", "")
        # Find matching key
        key = ""
        for acc in self.accounts:
            if acc.get("username") == username:
                key = acc.get("key", "")
                break
        if not key:
            key = self.accounts[self.active_account_idx].get("key", "")

        env = os.environ.copy()
        env["KAGGLE_USERNAME"] = username
        env["KAGGLE_KEY"] = key

        def _stop():
            return subprocess.run(
                ["kaggle", "kernels", "stop", f"{username}/{slug}"],
                capture_output=True,
                text=True,
                env=env,
                timeout=20
            )

        try:
            loop = asyncio.get_running_loop()
            res = await loop.run_in_executor(None, _stop)
            return res.returncode == 0
        except Exception:
            return False

    async def stop_kernel(self, instance_id: str = "primary") -> Tuple[bool, str]:
        """Stops target Kaggle GPU kernel to halt quota burning."""
        self.record_activity(instance_id)
        inst = self.instances.get(instance_id, {})
        target_slug = inst.get("slug", self.active_slug)
        account = inst.get("account")
        logger.info(f"[KaggleManager] Halting GPU kernel '{target_slug}' (instance: {instance_id})...")

        # In-Notebook Graceful Shutdown signal if worker is reachable
        try:
            await self._call_worker_admin("/admin/shutdown", instance_id=instance_id, timeout=4.0)
        except Exception:
            pass

        stopped = await self._stop_slug(target_slug, account_username=account)
        if instance_id == "primary":
            self.kernel_status = "STOPPED"
            self.boot_timestamp = None
        if instance_id in self.instances:
            self.instances[instance_id]["status"] = "STOPPED"
            self.instances[instance_id]["boot_timestamp"] = None
        self._save_state()
        return True, f"Kaggle GPU kernel '{target_slug}' stopped. Quota burning halted."

    async def probe_health(self, instance_id: str = "primary") -> bool:
        """Probes health endpoint on active tunnel for specified instance."""
        inst = self.instances.get(instance_id, {})
        target_url = inst.get("worker_url") or self.worker_url
        if not target_url:
            return False

        url = f"{target_url.rstrip('/')}/health"
        req = urllib.request.Request(url, headers={"User-Agent": "Dystopi-Minibots/2.0"})
        loop = asyncio.get_running_loop()

        def _probe():
            try:
                with urllib.request.urlopen(req, timeout=4.0) as resp:
                    if resp.status == 200:
                        data = json.loads(resp.read().decode("utf-8"))
                        return data.get("status") == "HEALTHY"
            except Exception:
                return False
            return False

        is_ok = await loop.run_in_executor(None, _probe)
        if is_ok:
            if instance_id == "primary":
                self.kernel_status = "READY"
            if instance_id in self.instances:
                self.instances[instance_id]["status"] = "READY"
        return is_ok

    async def swap_model(self, model_name: str, slot: int = 0, instance_id: str = "primary") -> Tuple[bool, str]:
        """Live-swaps model on specified GPU slot (0=uncensored, 1=coding)."""
        self.record_activity(instance_id)
        valid, clean_m, err = sanitize_model_identifier(model_name)
        if not valid:
            return False, f"Invalid model identifier: {err}"

        inst = self.instances.get(instance_id, {})
        inst_status = inst.get("status", self.kernel_status)
        if inst_status not in ("RUNNING", "READY"):
            self.current_model = clean_m
            if instance_id in self.instances:
                self.instances[instance_id]["model"] = clean_m
            return False, f"GPU worker not connected. Target model set to '{clean_m}'. It will boot on next launch."

        async with self._drain_lock:
            drain_start = time.time()
            while self.active_generations > 0 and (time.time() - drain_start < 10.0):
                await asyncio.sleep(0.5)

            try:
                payload = {"model": clean_m, "slot": slot}
                res = await self._call_worker_admin("/admin/swap_model", payload=payload, instance_id=instance_id, timeout=35.0)
                if res.get("status") == "OK":
                    self.current_model = clean_m
                    if instance_id in self.instances:
                        self.instances[instance_id]["model"] = clean_m
                    self._save_state()
                    return True, f"Slot {slot} swapped to '{clean_m}' in {res.get('duration_s', 0):.1f}s."
                return False, f"Model swap rejected: {res.get('error', 'unknown')}"
            except Exception as e:
                return False, f"Model swap failed: {e}"

    def get_worker_url_for_role(self, role: str = "uncensored") -> str:
        """Resolves active worker URL matching role, falling back to primary."""
        clean_role = role.lower()
        for inst_id, inst in self.instances.items():
            if inst.get("status") == "READY" and inst.get("role") == clean_role:
                url = inst.get("worker_url")
                if url:
                    return url
        return self.worker_url

    async def _call_worker_admin(
        self,
        path: str,
        payload: Optional[Dict[str, Any]] = None,
        instance_id: str = "primary",
        timeout: float = 10.0
    ) -> Dict[str, Any]:
        inst = self.instances.get(instance_id, {})
        base_url = inst.get("worker_url") or self.worker_url
        url = f"{base_url.rstrip('/')}{path}"

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.worker_secret}",
            "X-Kaggle-Secret": self.worker_secret
        }
        data_bytes = json.dumps(payload).encode() if payload else None
        req = urllib.request.Request(url, data=data_bytes, headers=headers, method="POST" if data_bytes else "GET")
        loop = asyncio.get_running_loop()

        def _fetch():
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode())

        return await loop.run_in_executor(None, _fetch)

    def get_status(self, instance_id: Optional[str] = None) -> Dict[str, Any]:
        idle_s = time.time() - self.last_activity_ts
        uptime_s = int(time.time() - self.boot_timestamp) if self.boot_timestamp else 0

        st = {
            "status": self.kernel_status,
            "active_slug": self.active_slug,
            "active_account": self.accounts[self.active_account_idx].get("username") if self.accounts else "none",
            "model": self.current_model,
            "worker_url": self.worker_url,
            "idle_seconds": int(idle_s),
            "uptime_seconds": uptime_s,
            "active_generations": self.active_generations,
            "supported_models": DEFAULT_MODELS,
            "total_accounts": len(self.accounts),
            "instances": self.instances
        }
        if instance_id and instance_id in self.instances:
            st["selected_instance"] = self.instances[instance_id]
        return st
