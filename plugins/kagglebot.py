# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

"""
kagglebot.py — ChatOps Administrative Plugin for Kaggle Burst GPU Cluster.

Exposes:
!kaggle status [instance] | launch [model] [--account <name>] [--instance <id>] | stop [instance] | search <query> | attach | detach | swap <slot> <model> [--instance <id>] | models | accounts
"""

import asyncio
import logging
from typing import Any, List, Optional

from core.hf_model_search import search_hf_models, CURATED_PRESETS, sanitize_model_identifier
from core.kaggle_bot_binder import KaggleBotBinder

logger = logging.getLogger("KaggleBotPlugin")


class KaggleBotPlugin:
    """ChatOps administration handler for Kaggle GPU instances and bot attachments."""

    def __init__(self, minibot: Any, kaggle_mgr: Any, bot_binder: Optional[KaggleBotBinder] = None):
        self.minibot = minibot
        self.kaggle_mgr = kaggle_mgr
        self.bot_binder = bot_binder or KaggleBotBinder()

    async def handle_command(self, target: str, sender: str, args: List[str]) -> None:
        """Dispatches !kaggle ChatOps commands."""
        if not args:
            await self.cmd_status(target, sender, [])
            return

        subcmd = args[0].lower().strip()
        sub_args = args[1:]

        if subcmd in ("status", "st", "info"):
            await self.cmd_status(target, sender, sub_args)
        elif subcmd in ("launch", "start", "boot", "up"):
            await self.cmd_launch(target, sender, sub_args)
        elif subcmd in ("stop", "halt", "down", "kill"):
            await self.cmd_stop(target, sender, sub_args)
        elif subcmd in ("search", "find"):
            await self.cmd_search(target, sender, sub_args)
        elif subcmd in ("attach", "bind"):
            await self.cmd_attach(target, sender, sub_args)
        elif subcmd in ("detach", "unbind"):
            await self.cmd_detach(target, sender, sub_args)
        elif subcmd in ("swap", "switch"):
            await self.cmd_swap(target, sender, sub_args)
        elif subcmd in ("models", "presets", "list"):
            await self.cmd_models(target, sender)
        elif subcmd in ("accounts", "pool"):
            await self.cmd_accounts(target, sender)
        else:
            self._reply(target, f"Unknown subcmd '{subcmd}'. Usage: !kaggle status [id] | launch [model] [--account <name>] [--instance <id>] | stop [id] | swap <slot> <model> | accounts | models")

    def _reply(self, target: str, message: str) -> None:
        net = getattr(self.minibot, "network", "wireguard")
        if hasattr(self.minibot, "send_privmsg"):
            self.minibot.send_privmsg(net, target, message)
        elif hasattr(self.minibot, "send_msg"):
            try:
                self.minibot.send_msg(net, target, message)
            except TypeError:
                self.minibot.send_msg(target, message)
        elif hasattr(self.minibot, "reply"):
            self.minibot.reply(message)

    async def cmd_status(self, target: str, sender: str, args: List[str]) -> None:
        instance_id = args[0] if args else None
        st = self.kaggle_mgr.get_status(instance_id=instance_id)

        target_inst = st.get("selected_instance") or st.get("instances", {}).get("primary", {})
        inst_name = instance_id or "primary"
        status_val = target_inst.get("status", st.get("status", "STOPPED"))
        status_color = "🟢" if status_val == "READY" else ("🟡" if status_val == "STARTING" else "🔴")
        uptime_m = st.get("uptime_seconds", 0) // 60
        idle_m = st.get("idle_seconds", 0) // 60
        slug = target_inst.get("slug", st.get("active_slug", "unknown"))
        acc = target_inst.get("account") or st.get("active_account", "none")
        model = target_inst.get("model", st.get("model", "unknown"))
        url = target_inst.get("worker_url") or st.get("worker_url", "none")
        ttype = target_inst.get("tunnel_type", "quick")

        self._reply(
            target,
            f"[KAG-GPU] {status_color} [{inst_name}] Status: {status_val} | Slug: {slug} (Acc: {acc}) | "
            f"Model: {model} | Up: {uptime_m}m | Idle: {idle_m}m/20m | Tunnel: {ttype} ({url}) | Gens: {st.get('active_generations', 0)}"
        )

        all_insts = [f"{k}:{v.get('status','STOPPED')}" for k, v in st.get("instances", {}).items() if k != inst_name]
        if all_insts:
            self._reply(target, f"[KAG-GPU] Other Instances: {', '.join(all_insts)}")

    async def cmd_launch(self, target: str, sender: str, args: List[str]) -> None:
        model = None
        account_name = None
        instance_id = "primary"
        role = "uncensored"

        i = 0
        plain_args = []
        while i < len(args):
            arg = args[i]
            if arg in ("--account", "-a") and i + 1 < len(args):
                account_name = args[i + 1]
                i += 2
            elif arg in ("--instance", "-i") and i + 1 < len(args):
                instance_id = args[i + 1]
                i += 2
            elif arg in ("--role", "-r") and i + 1 < len(args):
                role = args[i + 1]
                i += 2
            else:
                plain_args.append(arg)
                i += 1

        if plain_args:
            model = plain_args[0]

        target_m = model or self.kaggle_mgr.current_model
        self._reply(
            target,
            f"[KAG-GPU] 🚀 Initiating push for '{target_m}' (Instance: {instance_id}, Role: {role}, Account: {account_name or 'auto-failover'})..."
        )
        ok, msg = await self.kaggle_mgr.start_kernel(
            model=model,
            account_name=account_name,
            instance_id=instance_id,
            role=role
        )
        self._reply(target, f"[KAG-GPU] {'✅' if ok else '❌'} {msg}")

    async def cmd_stop(self, target: str, sender: str, args: List[str]) -> None:
        instance_id = args[0] if args else "primary"
        self._reply(target, f"[KAG-GPU] 🛑 Halting Kaggle GPU container '{instance_id}'...")
        ok, msg = await self.kaggle_mgr.stop_kernel(instance_id=instance_id)
        self._reply(target, f"[KAG-GPU] {'✅' if ok else '❌'} {msg}")

    async def cmd_search(self, target: str, sender: str, args: List[str]) -> None:
        if not args:
            self._reply(target, "Usage: !kaggle search <keyword> (e.g. !kaggle search uncensored)")
            return
        query = " ".join(args)
        self._reply(target, f"[KAG-GPU] 🔍 Querying Hugging Face Hub for GGUF models matching '{query}'...")
        loop = asyncio.get_running_loop()
        results = await loop.run_in_executor(None, search_hf_models, query, 3)
        if not results:
            self._reply(target, "[KAG-GPU] No matching GGUF models found or API rate-limited.")
            return

        for r in results:
            feas = "✅ Feasible (16GB)" if r["fits_single_gpu"] else "⚠️ Requires 2x T4 (32GB)"
            self._reply(target, f"• {r['id']} | ~{r['estimated_vram_gb']}GB VRAM | {r['downloads']} dl | {feas}")

    async def cmd_attach(self, target: str, sender: str, args: List[str]) -> None:
        if len(args) < 2:
            self._reply(target, "Usage: !kaggle attach <bot_nick> <uncensored|coding> [model_id]")
            return
        bot_nick = args[0]
        role = args[1].lower()
        model_id = args[2] if len(args) > 2 else None

        valid_m = None
        if model_id:
            ok, clean_m, err = sanitize_model_identifier(model_id)
            if not ok:
                self._reply(target, f"[KAG-GPU] ❌ Model name error: {err}")
                return
            valid_m = clean_m

        binding = self.bot_binder.bind_bot(bot_nick, role, model_id=valid_m)
        self._reply(
            target,
            f"[KAG-GPU] ✅ Bound bot '{bot_nick}' to role '{binding['role']}' on Slot {binding['slot']} (Model: {binding['model']})"
        )

    async def cmd_detach(self, target: str, sender: str, args: List[str]) -> None:
        if not args:
            self._reply(target, "Usage: !kaggle detach <bot_nick>")
            return
        bot_nick = args[0]
        if self.bot_binder.unbind_bot(bot_nick):
            self._reply(target, f"[KAG-GPU] ✅ Detached bot '{bot_nick}'. Reverted to standard router fallback.")
        else:
            self._reply(target, f"[KAG-GPU] Bot '{bot_nick}' was not bound to Kaggle.")

    async def cmd_swap(self, target: str, sender: str, args: List[str]) -> None:
        if not args:
            self._reply(target, "Usage: !kaggle swap <slot_0_or_1> <model_name> [--instance <id>]")
            return

        instance_id = "primary"
        plain_args = []
        i = 0
        while i < len(args):
            arg = args[i]
            if arg in ("--instance", "-i") and i + 1 < len(args):
                instance_id = args[i + 1]
                i += 2
            else:
                plain_args.append(arg)
                i += 1

        if not plain_args:
            self._reply(target, "Usage: !kaggle swap <slot_0_or_1> <model_name> [--instance <id>]")
            return

        try:
            slot = int(plain_args[0])
            model_name = plain_args[1]
        except (ValueError, IndexError):
            slot = 0
            model_name = plain_args[0]

        self._reply(target, f"[KAG-GPU] 🔄 Requesting live model swap on Slot {slot} to '{model_name}' (Instance: {instance_id})...")
        ok, msg = await self.kaggle_mgr.swap_model(model_name, slot=slot, instance_id=instance_id)
        self._reply(target, f"[KAG-GPU] {'✅' if ok else '❌'} {msg}")

    async def cmd_models(self, target: str, sender: str) -> None:
        self._reply(target, "[KAG-GPU] 🧠 Curated Model Presets (0s Dataset Mount / Fast Stream):")
        for key, p in CURATED_PRESETS.items():
            self._reply(target, f"• {key}: {p['file']} ({p['params']}, ~{p['estimated_vram_gb']}GB) — {p['description']}")

    async def cmd_accounts(self, target: str, sender: str) -> None:
        total = len(self.kaggle_mgr.accounts)
        if not total:
            self._reply(target, "[KAG-GPU] 👥 Rotation Pool: No Kaggle accounts registered.")
            return

        active_idx = self.kaggle_mgr.active_account_idx
        acc_summaries = []
        for idx, acc in enumerate(self.kaggle_mgr.accounts):
            uname = acc.get("username", f"acc_{idx}")
            marker = "⭐" if idx == active_idx else "•"
            acc_summaries.append(f"{marker} #{idx + 1}: {uname}")

        self._reply(
            target,
            f"[KAG-GPU] 👥 Rotation Pool ({total} accounts): {' | '.join(acc_summaries)}"
        )
