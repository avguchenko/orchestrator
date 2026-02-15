"""Refinery: merges completed task branches. SDK query() for conflict resolution."""

from __future__ import annotations

import logging
import subprocess

from claude_agent_sdk import ClaudeAgentOptions, query

from .config import ProjectConfig
from .models import Task
from .prompts import load_prompt

logger = logging.getLogger(__name__)


def _git(cwd: str, *args: str) -> subprocess.CompletedProcess:
    """Run a git command and return the result."""
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _get_default_branch(cwd: str) -> str:
    """Detect the default branch."""
    try:
        result = _git(cwd, "symbolic-ref", "refs/remotes/origin/HEAD", "--short")
        if result.returncode == 0:
            return result.stdout.strip().split("/")[-1]
    except Exception:
        pass
    result = _git(cwd, "rev-parse", "--verify", "main")
    return "main" if result.returncode == 0 else "master"


def merge_branch(task: Task, project: ProjectConfig) -> tuple[bool, str]:
    """Merge a task branch into the default branch.

    When the merge succeeds without conflicts, the refinery shall fast-forward
    or create a merge commit. When conflicts occur, the refinery shall abort
    and return the conflict details.

    Returns (success, message).
    """
    cwd = str(project.abs_path)
    default = _get_default_branch(cwd)

    # Checkout default branch
    result = _git(cwd, "checkout", default)
    if result.returncode != 0:
        return False, f"Failed to checkout {default}: {result.stderr}"

    # Attempt merge
    result = _git(cwd, "merge", task.branch, "--no-ff", "-m", f"Merge {task.branch}: {task.title}")
    if result.returncode == 0:
        logger.info("Merged %s into %s", task.branch, default)
        return True, f"Merged {task.branch}"

    # Check for conflicts
    if "CONFLICT" in result.stdout or "CONFLICT" in result.stderr:
        conflict_output = result.stdout + result.stderr
        # Abort the merge
        _git(cwd, "merge", "--abort")
        return False, conflict_output

    return False, f"Merge failed: {result.stderr}"


async def resolve_conflicts(
    task: Task,
    project: ProjectConfig,
    conflict_details: str,
) -> bool:
    """When merge conflicts occur, the refinery shall use AI to resolve them.

    Returns True if resolution succeeded.
    """
    cwd = str(project.abs_path)
    default = _get_default_branch(cwd)

    # Re-attempt merge to get conflict markers
    _git(cwd, "checkout", default)
    result = _git(cwd, "merge", task.branch)
    if result.returncode == 0:
        return True  # No conflicts this time

    # Get conflicted files
    status = _git(cwd, "status", "--porcelain")
    conflicted = [
        line[3:] for line in status.stdout.splitlines()
        if line.startswith("UU ") or line.startswith("AA ")
    ]

    if not conflicted:
        _git(cwd, "merge", "--abort")
        return False

    system = load_prompt("refinery_system.md")
    prompt = f"""# Merge Conflict Resolution

Branch: {task.branch}
Target: {default}
Task: {task.title}

## Conflict Details
{conflict_details[:2000]}

## Conflicted Files
{', '.join(conflicted)}

Resolve the merge conflicts in the conflicted files. When resolving, the system shall
preserve the intent of both the task branch changes and the target branch changes.
After resolving, stage the files with git add.
"""

    options = ClaudeAgentOptions(
        model=project.model,
        cwd=cwd,
        system_prompt=system,
        allowed_tools=["Read", "Edit", "Write", "Bash", "Grep", "Glob"],
        permission_mode="bypassPermissions",
        max_budget_usd=project.max_budget_per_task,
    )

    try:
        async for msg in query(prompt=prompt, options=options):
            pass  # Agent resolves conflicts via tools
    except Exception as e:
        logger.error("AI conflict resolution failed: %s", e)
        _git(cwd, "merge", "--abort")
        return False

    # Check if conflicts are resolved
    status = _git(cwd, "status", "--porcelain")
    still_conflicted = [
        line for line in status.stdout.splitlines()
        if line.startswith("UU ") or line.startswith("AA ")
    ]

    if still_conflicted:
        _git(cwd, "merge", "--abort")
        return False

    # Commit the resolution
    result = _git(cwd, "commit", "--no-edit")
    if result.returncode != 0:
        _git(cwd, "merge", "--abort")
        return False

    logger.info("Resolved conflicts and merged %s", task.branch)
    return True


def cleanup_branch(task: Task, project: ProjectConfig) -> None:
    """After successful merge, the refinery shall delete the task branch."""
    cwd = str(project.abs_path)
    _git(cwd, "branch", "-d", task.branch)
