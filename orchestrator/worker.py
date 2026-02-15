"""Worker: async wrapper around claude_agent_sdk.query() with git worktree isolation."""

from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
import time
from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions, query

from .config import ProjectConfig
from .models import Task, WorkerResult
from .prompts import load_prompt

logger = logging.getLogger(__name__)


def _git(cwd: str, *args: str) -> str:
    """Run a git command and return stdout."""
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def _get_default_branch(cwd: str) -> str:
    """Detect the default branch (main or master)."""
    try:
        return _git(cwd, "symbolic-ref", "refs/remotes/origin/HEAD", "--short").split("/")[-1]
    except RuntimeError:
        # Fallback: check if main exists, else master
        try:
            _git(cwd, "rev-parse", "--verify", "main")
            return "main"
        except RuntimeError:
            return "master"


def _create_worktree(cwd: str, branch: str, task_id: str) -> str:
    """Create an isolated git worktree for a worker.

    Each worker gets its own directory so parallel workers don't conflict.
    Returns the path to the worktree directory.
    """
    worktree_dir = str(Path(cwd) / ".orch" / "worktrees" / task_id)
    default = _get_default_branch(cwd)

    # Clean up existing worktree if retrying
    try:
        _git(cwd, "worktree", "remove", "--force", worktree_dir)
    except RuntimeError:
        pass
    # Also clean up stale directory
    if Path(worktree_dir).exists():
        shutil.rmtree(worktree_dir)
    # Delete existing branch if retrying
    try:
        _git(cwd, "branch", "-D", branch)
    except RuntimeError:
        pass

    # Create worktree with a new branch off default
    Path(worktree_dir).parent.mkdir(parents=True, exist_ok=True)
    _git(cwd, "worktree", "add", "-b", branch, worktree_dir, default)
    logger.info("Created worktree for %s at %s", branch, worktree_dir)
    return worktree_dir


def _remove_worktree(cwd: str, worktree_dir: str) -> None:
    """Remove a git worktree after the worker is done."""
    try:
        _git(cwd, "worktree", "remove", "--force", worktree_dir)
    except RuntimeError:
        # Force cleanup if git worktree remove fails
        if Path(worktree_dir).exists():
            shutil.rmtree(worktree_dir)
        try:
            _git(cwd, "worktree", "prune")
        except RuntimeError:
            pass


def _capture_diff(cwd: str) -> tuple[str, int]:
    """Capture git diff stat and count of changed files."""
    try:
        stat = _git(cwd, "diff", "--stat", "HEAD~1")
        numstat = _git(cwd, "diff", "--numstat", "HEAD~1")
        files_changed = len([l for l in numstat.splitlines() if l.strip()])
        return stat, files_changed
    except RuntimeError:
        # No commits yet on branch
        return "", 0


def _prompt_for_task_type(task: Task) -> str:
    """Load the appropriate prompt template for the task type."""
    prompt_map = {
        "code": "worker_code.md",
        "test": "worker_test.md",
        "fix": "worker_fix.md",
        "review": "worker_review.md",
    }
    template_name = prompt_map.get(task.task_type.value, "worker_code.md")
    return load_prompt(template_name)


def _write_worker_log(
    project: ProjectConfig, task: Task,
    output: str, diff_stat: str, cost: float, duration: float,
) -> None:
    """Write worker output to .orch/workers/{task_id}.md for audit."""
    log_dir = project.abs_path / ".orch" / "workers"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{task.id}.md"
    log_path.write_text(
        f"# Worker Log: {task.title}\n\n"
        f"- **Task ID**: {task.id}\n"
        f"- **Branch**: {task.branch}\n"
        f"- **Type**: {task.task_type.value}\n"
        f"- **Cost**: ${cost:.4f}\n"
        f"- **Duration**: {duration:.1f}s\n\n"
        f"## Diff\n```\n{diff_stat}\n```\n\n"
        f"## Output\n{output[:5000]}\n"
    )


async def run_worker(task: Task, project: ProjectConfig) -> WorkerResult:
    """Execute a task via claude_agent_sdk.query() in an isolated git worktree."""
    repo_dir = str(project.abs_path)
    start = time.monotonic()

    # Create isolated worktree — each worker gets its own directory
    try:
        worktree_dir = _create_worktree(repo_dir, task.branch, task.id)
    except RuntimeError as e:
        return WorkerResult(
            task_id=task.id,
            success=False,
            error=f"Worktree creation failed: {e}",
        )

    # Build SDK options — cwd points to the isolated worktree
    system_prompt = _prompt_for_task_type(task)
    options = ClaudeAgentOptions(
        model=project.model,
        cwd=worktree_dir,
        system_prompt=system_prompt,
        allowed_tools=["Read", "Edit", "Write", "Bash", "Grep", "Glob"],
        permission_mode="bypassPermissions",
        max_budget_usd=project.max_budget_per_task,
    )

    # Run the agent
    messages = []
    output_text = ""
    cost = 0.0
    try:
        async for msg in query(prompt=task.prompt, options=options):
            messages.append(msg)
            # ResultMessage is the final message with cost/result
            if hasattr(msg, "total_cost_usd") and msg.total_cost_usd is not None:
                cost = msg.total_cost_usd
            if hasattr(msg, "result") and msg.result:
                output_text = msg.result
            # AssistantMessage has content blocks
            elif hasattr(msg, "content") and isinstance(msg.content, list):
                for block in msg.content:
                    if hasattr(block, "text"):
                        output_text += block.text
    except Exception as e:
        duration = time.monotonic() - start
        _remove_worktree(repo_dir, worktree_dir)
        return WorkerResult(
            task_id=task.id,
            success=False,
            error=str(e),
            duration_seconds=duration,
            messages_count=len(messages),
            cost_usd=cost,
        )

    # Commit any changes in the worktree
    try:
        _git(worktree_dir, "add", "-A")
        _git(worktree_dir, "commit", "-m", f"orch: {task.title}\n\nTask: {task.id}")
    except RuntimeError:
        pass  # No changes to commit is fine

    # Capture diff from the worktree
    diff_stat, files_changed = _capture_diff(worktree_dir)
    duration = time.monotonic() - start

    # Write worker output to disk (in the main repo's .orch dir)
    _write_worker_log(project, task, output_text, diff_stat, cost, duration)

    # Clean up worktree — branch and commits remain in the main repo
    _remove_worktree(repo_dir, worktree_dir)

    return WorkerResult(
        task_id=task.id,
        success=True,
        output=output_text[:5000],
        diff_stat=diff_stat,
        files_changed=files_changed,
        cost_usd=cost,
        duration_seconds=duration,
        messages_count=len(messages),
    )


async def run_workers_parallel(
    tasks: list[Task],
    project: ProjectConfig,
) -> list[WorkerResult]:
    """Run multiple workers in parallel, each in its own git worktree."""
    return list(await asyncio.gather(
        *(run_worker(t, project) for t in tasks)
    ))
