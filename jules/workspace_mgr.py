# SPDX-License-Identifier: AGPL-3.0-or-later
"""
workspace_mgr.py — Local Git Workspace Cloner for Jules.
"""

import logging
import subprocess
from pathlib import Path
from typing import Optional

logger = logging.getLogger("JulesWorkspace")


class WorkspaceManager:
    """Manages local git checkout sandboxes for Jules projects."""
    def __init__(self, base_dir: Optional[str] = None):
        if not base_dir:
            base_dir = str(Path(__file__).resolve().parents[1] / "data" / "jules_workspaces")
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def clone_or_update(self, repo: str, branch: str) -> Optional[Path]:
        """Clones or checks out specific branch into local workspace."""
        repo_name = repo.replace("/", "_")
        target_dir = self.base_dir / repo_name

        try:
            if not target_dir.exists():
                url = f"git@github.com:{repo}.git"
                subprocess.run(["git", "clone", "--branch", branch, url, str(target_dir)], check=True, capture_output=True, timeout=30)
            else:
                subprocess.run(["git", "fetch", "origin", branch], cwd=str(target_dir), check=True, capture_output=True, timeout=20)
                subprocess.run(["git", "checkout", branch], cwd=str(target_dir), check=True, capture_output=True, timeout=10)
                subprocess.run(["git", "pull", "--ff-only", "origin", branch], cwd=str(target_dir), check=True, capture_output=True, timeout=20)
            return target_dir
        except Exception as e:
            logger.error(f"Failed to clone/update workspace for {repo}:{branch}: {e}")
            return None
