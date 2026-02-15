"""Patrol: detects stuck tasks and repeated failures. Pure Python, no SDK."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from .config import ProjectConfig
from .models import TaskStatus
from .state import StateStore

logger = logging.getLogger(__name__)


def check_stuck_tasks(
    project: ProjectConfig,
    store: StateStore,
) -> list[str]:
    """Check for tasks that have exceeded their timeout.

    When a task has been in_progress longer than the configured timeout,
    the patrol shall mark it as failed. Where a task has remaining retries,
    the patrol shall re-queue it instead.

    Returns list of task IDs that were handled.
    """
    handled = []
    in_progress = store.list_tasks(project=project.name, status=TaskStatus.IN_PROGRESS)
    now = datetime.now(timezone.utc)

    for task in in_progress:
        if task.started_at is None:
            continue
        elapsed = (now - task.started_at).total_seconds()
        if elapsed > project.worker_timeout_seconds:
            logger.warning(
                "Task %s stuck for %.0fs (timeout: %ds)",
                task.id, elapsed, project.worker_timeout_seconds,
            )
            if task.retry_count < task.max_retries:
                store.increment_retry(task.id)
                logger.info("Re-queued stuck task %s (retry %d)", task.id, task.retry_count + 1)
            else:
                store.update_task_status(task.id, TaskStatus.FAILED)
                logger.info("Failed stuck task %s (no retries left)", task.id)
            handled.append(task.id)

    return handled


def check_repeated_failures(
    project: ProjectConfig,
    store: StateStore,
    max_consecutive_failures: int = 5,
) -> bool:
    """When a project has too many consecutive failures, the patrol shall pause it.

    Returns True if the project was paused.
    """
    failed = store.list_tasks(project=project.name, status=TaskStatus.FAILED)
    if len(failed) < max_consecutive_failures:
        return False

    # Check if the last N tasks all failed
    all_tasks = store.list_tasks(project=project.name)
    if len(all_tasks) < max_consecutive_failures:
        return False

    recent = sorted(all_tasks, key=lambda t: t.created_at or datetime.min, reverse=True)
    recent_statuses = [t.status for t in recent[:max_consecutive_failures]]

    if all(s == TaskStatus.FAILED for s in recent_statuses):
        logger.warning(
            "Project %s has %d consecutive failures, pausing",
            project.name, max_consecutive_failures,
        )
        store.set_paused(project.name, True)
        return True

    return False


def run_patrol(project: ProjectConfig, store: StateStore) -> dict:
    """Run all patrol checks for a project.

    The patrol shall check for stuck tasks and repeated failures.
    """
    stuck = check_stuck_tasks(project, store)
    paused = check_repeated_failures(project, store)
    return {
        "stuck_tasks": stuck,
        "project_paused": paused,
    }
