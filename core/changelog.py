# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

"""
changelog.py — Git commit changelog delta extractor and IRC release banner formatter for Minibots.
"""

import os
import subprocess
from typing import Tuple, List, Optional


def extract_git_changelog_delta(
    repo_dir: Optional[str] = None,
    last_commit: Optional[str] = None,
    max_entries: int = 4,
) -> Tuple[str, str, List[str]]:
    """
    Extracts git commit messages between last_commit and HEAD.
    Falls back cleanly to /opt/minibots/commit_info.txt if .git is not present.
    Returns: (current_commit_hash, branch_name, list_of_commit_lines)
    """
    if not repo_dir:
        repo_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    current_hash = ""
    branch_name = "main"
    commits: List[str] = []

    has_git = os.path.exists(os.path.join(repo_dir, ".git"))
    if has_git:
        try:
            res_hash = subprocess.run(
                ["git", "-C", repo_dir, "rev-parse", "--short", "HEAD"],
                capture_output=True,
                text=True,
                timeout=3,
            )
            if res_hash.returncode == 0:
                current_hash = res_hash.stdout.strip()

            res_branch = subprocess.run(
                ["git", "-C", repo_dir, "rev-parse", "--abbrev-ref", "HEAD"],
                capture_output=True,
                text=True,
                timeout=3,
            )
            if res_branch.returncode == 0:
                branch_name = res_branch.stdout.strip()
        except Exception:
            pass

    # Read commit_info.txt fallback
    commit_info_path = os.path.join(repo_dir, "commit_info.txt")
    if not current_hash and os.path.exists(commit_info_path):
        try:
            with open(commit_info_path, "r", encoding="utf-8", errors="ignore") as f:
                lines = [l.strip() for l in f if l.strip()]
                if lines:
                    current_hash = lines[0].split()[0]
        except Exception:
            pass

    if not current_hash:
        current_hash = "prod"

    # Query commits
    if has_git:
        try:
            git_range = f"{last_commit}..HEAD" if (last_commit and last_commit != current_hash) else f"-n {max_entries}"
            cmd = ["git", "-C", repo_dir, "log", "--pretty=format:%h - %s (%ci)"]
            if ".." in git_range:
                cmd.append(git_range)
            else:
                cmd.extend(["-n", str(max_entries)])
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=3)
            if res.returncode == 0 and res.stdout.strip():
                commits = [l.strip() for l in res.stdout.splitlines() if l.strip()][:max_entries]
        except Exception:
            pass

    if not commits and os.path.exists(commit_info_path):
        try:
            with open(commit_info_path, "r", encoding="utf-8", errors="ignore") as f:
                commits = [l.strip() for l in f if l.strip()][:max_entries]
        except Exception:
            pass

    return current_hash, branch_name, commits


def format_startup_changelog_banner(
    repo_dir: Optional[str] = None,
    last_commit: Optional[str] = None,
    max_entries: int = 4,
) -> List[str]:
    """
    Renders formatted IRC announcement lines for release broadcasts.
    """
    current_hash, branch_name, commits = extract_git_changelog_delta(repo_dir, last_commit, max_entries)
    if not commits:
        return []

    lines = [
        f"[\x02\x0303MINIBOTS::RELEASE\x0f] New Build Deployed (\x02{current_hash}\x0f | {len(commits)} improvement{'s' if len(commits) != 1 else ''}) [\x0310{branch_name}\x0f]:"
    ]
    for c in commits:
        lines.append(f"  \x0303•\x0f {c}")
    return lines
