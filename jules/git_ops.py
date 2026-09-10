# SPDX-License-Identifier: AGPL-3.0-or-later
"""
git_ops.py — Automated Git Branching & Remote Provisioning for Jules.
"""

import logging
import subprocess
from typing import Tuple

logger = logging.getLogger("JulesGitOps")


def check_remote_branch(repo: str, branch: str) -> bool:
    """Checks if a branch already exists on GitHub remote."""
    url = f"git@github.com:{repo}.git"
    try:
        res = subprocess.run(
            ["git", "ls-remote", "--heads", url, branch],
            capture_output=True,
            text=True,
            timeout=10
        )
        return branch in res.stdout
    except Exception as e:
        logger.error(f"Failed to check remote branch {branch} on {repo}: {e}")
        return False


def create_remote_branch(repo: str, branch: str, base_branch: str = "main") -> Tuple[bool, str]:
    """Pushes a new remote branch from base branch."""
    url = f"git@github.com:{repo}.git"
    try:
        res = subprocess.run(
            ["git", "push", url, f"refs/heads/{base_branch}:refs/heads/{branch}"],
            capture_output=True,
            text=True,
            timeout=15
        )
        if res.returncode == 0:
            return True, f"Created remote branch {branch}"
        return False, res.stderr
    except Exception as e:
        return False, str(e)
