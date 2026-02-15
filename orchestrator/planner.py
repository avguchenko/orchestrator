"""Planner: decomposes project work into tasks via SDK query()."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions, query

from .config import ProjectConfig
from .judge import evaluate_result
from .models import (
    CycleStatus,
    PlannerCycle,
    Task,
    TaskStatus,
    TaskType,
    WorkerResult,
)
from .prompts import load_prompt
from .state import StateStore
from .worker import run_workers_parallel

logger = logging.getLogger(__name__)

TASK_LIST_SCHEMA = {
    "type": "object",
    "properties": {
        "tasks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "description": {"type": "string"},
                    "task_type": {"type": "string", "enum": ["code", "test", "fix", "review"]},
                    "priority": {"type": "integer"},
                },
                "required": ["title", "description"],
            },
        },
        "reasoning": {"type": "string"},
    },
    "required": ["tasks"],
}


def _build_planner_prompt(project: ProjectConfig, store: StateStore, cycle_id: str = "") -> str:
    """Build the planner prompt with current state context."""
    # Current tasks
    pending = store.list_tasks(project=project.name, status=TaskStatus.PENDING)
    in_progress = store.list_tasks(project=project.name, status=TaskStatus.IN_PROGRESS)
    recent_done = store.list_tasks(project=project.name, status=TaskStatus.DONE)
    recent_failed = store.list_tasks(project=project.name, status=TaskStatus.FAILED)

    # Read context files
    context_content = ""
    for relpath in project.planner_context_files:
        fpath = project.abs_path / relpath
        if fpath.exists():
            context_content += f"\n## {relpath}\n```\n{fpath.read_text()[:3000]}\n```\n"

    return f"""# Planning Cycle for: {project.name}

## Worker Constraints
- Max workers this cycle: {project.max_workers}
- Worker timeout: {project.worker_timeout_seconds}s
- Worker budget: ${project.max_budget_per_task:.2f} per task
- Worker tools: Read, Edit, Write, Bash, Grep, Glob
- Each worker runs on an isolated git branch — workers cannot see each other's changes

## Current Task State
- Pending (queued, not yet started): {len(pending)}
- In-progress (currently running): {len(in_progress)}
- Recently completed: {len(recent_done)}
- Recently failed: {len(recent_failed)}

### Pending Tasks
{_format_tasks(pending)}

### Recently Completed Tasks
{_format_tasks(recent_done)}

### Recently Failed Tasks (do not re-emit these as-is — decompose or fix prerequisites)
{_format_tasks(recent_failed)}

## Project Context Files
{context_content if context_content else "(no context files found)"}

## Your Job

1. **Explore the codebase** using Glob and Read. Understand what exists — files, modules, tests, config. Do not rely only on the context files above.
2. **Identify the highest-value work** using the priority order: fix broken things > unblock future work > add tests > new features.
3. **Write your plan to `.orch/plans/cycle_{cycle_id}.md`** — a markdown document explaining what you found in the codebase, what work you're proposing, why, and what you deferred. This is the human-readable record of your planning decisions.
4. **Produce {project.max_workers} tasks** (or fewer if less work is needed). Each task must be completable by one worker in ~5 minutes with a ${project.max_budget_per_task:.2f} budget.
5. **Write complete task descriptions.** The worker sees only the description and the codebase — no other context. Include: what to build, which files to create/modify, acceptance criteria, and what to leave alone.

Return the JSON task list.
"""


def _format_tasks(tasks: list[Task]) -> str:
    if not tasks:
        return "(none)"
    return "\n".join(f"- [{t.status.value}] {t.title}: {t.description[:100]}" for t in tasks)


def _ensure_plans_dir(project: ProjectConfig) -> Path:
    """Ensure .orch/plans/ exists in the project directory."""
    plans_dir = project.abs_path / ".orch" / "plans"
    plans_dir.mkdir(parents=True, exist_ok=True)
    return plans_dir


async def plan_tasks(project: ProjectConfig, store: StateStore, cycle_id: str = "") -> list[Task]:
    """Call the planner to generate new tasks for a project."""
    _ensure_plans_dir(project)
    prompt = _build_planner_prompt(project, store, cycle_id=cycle_id)
    system = load_prompt("planner_system.md")

    options = ClaudeAgentOptions(
        model=project.planner_model,
        cwd=str(project.abs_path),
        system_prompt=system,
        allowed_tools=["Read", "Write", "Grep", "Glob"],
        permission_mode="bypassPermissions",
        output_format={"type": "json_schema", "schema": TASK_LIST_SCHEMA},
        setting_sources=["project"],
    )

    result_text = ""
    structured = None
    async for msg in query(prompt=prompt, options=options):
        if hasattr(msg, "structured_output") and msg.structured_output is not None:
            structured = msg.structured_output
        if hasattr(msg, "result") and msg.result:
            result_text = msg.result

    # Parse structured output (prefer structured_output, fall back to result text)
    try:
        if structured and isinstance(structured, dict):
            data = structured
        elif structured and isinstance(structured, str):
            data = json.loads(structured)
        else:
            data = json.loads(result_text)
    except json.JSONDecodeError:
        logger.error("Planner returned invalid JSON: %s", result_text[:200])
        return []

    tasks = []
    for item in data.get("tasks", []):
        task = Task(
            project=project.name,
            title=item["title"],
            description=item["description"],
            task_type=TaskType(item.get("task_type", "code")),
            priority=item.get("priority", 0),
        )
        tasks.append(task)

    return tasks


async def run_cycle(project: ProjectConfig, store: StateStore) -> PlannerCycle:
    """Run one full planner cycle for a project.

    The orchestrator shall:
    1. Check if the project is paused
    2. Plan new tasks if backlog is low
    3. Claim and dispatch pending tasks to workers
    4. Evaluate worker results via judge
    5. Update state and complete the cycle
    """
    # Check pause state
    proj_state = store.get_project_state(project.name)
    if proj_state.paused:
        logger.info("Project %s is paused, skipping cycle", project.name)
        cycle = PlannerCycle(project=project.name, status=CycleStatus.COMPLETED)
        return cycle

    cycle = PlannerCycle(project=project.name)
    store.create_cycle(cycle)
    total_cost = 0.0

    try:
        # Step 1: When backlog is low, the planner shall generate new tasks
        pending_count = store.pending_count(project.name)
        if pending_count < project.max_workers:
            logger.info("Backlog low (%d), planning new tasks for %s", pending_count, project.name)
            new_tasks = await plan_tasks(project, store, cycle_id=cycle.id)
            for t in new_tasks:
                t.cycle_id = cycle.id
                store.create_task(t)
            cycle.tasks_created = len(new_tasks)
            logger.info("Planned %d new tasks for %s", len(new_tasks), project.name)

        # Step 2: Claim pending tasks up to max_workers
        pending = store.list_tasks(project=project.name, status=TaskStatus.PENDING)
        to_run = []
        for task in pending[: project.max_workers]:
            if store.try_claim_task(task.id):
                to_run.append(task)

        if not to_run:
            logger.info("No tasks to run for %s", project.name)
            store.complete_cycle(cycle.id, CycleStatus.COMPLETED)
            return cycle

        # Step 3: Run workers in parallel
        logger.info("Running %d workers for %s", len(to_run), project.name)
        results = await run_workers_parallel(to_run, project)

        # Step 4: Judge each result
        completed = 0
        failed = 0
        task_by_id = {t.id: t for t in to_run}
        for result in results:
            store.save_worker_result(result)
            total_cost += result.cost_usd
            task = task_by_id.get(result.task_id) or store.get_task(result.task_id)

            if result.success and task:
                verdict = await evaluate_result(task, result, project, store)
                store.save_verdict(verdict)
                total_cost += verdict.cost_usd

                if verdict.passed:
                    store.update_task_status(result.task_id, TaskStatus.DONE)
                    completed += 1
                else:
                    # When a task fails judge evaluation and retries remain,
                    # the system shall re-queue the task
                    if task.retry_count < task.max_retries:
                        store.increment_retry(result.task_id)
                    else:
                        store.update_task_status(result.task_id, TaskStatus.FAILED)
                        failed += 1
            else:
                store.update_task_status(result.task_id, TaskStatus.FAILED)
                failed += 1

        # Step 5: Complete the cycle
        store.complete_cycle(
            cycle.id,
            CycleStatus.COMPLETED,
            tasks_completed=completed,
            tasks_failed=failed,
            total_cost=total_cost,
        )

        # Update project state
        proj_state.total_cycles += 1
        proj_state.total_tasks_completed += completed
        proj_state.total_cost_usd += total_cost
        from datetime import datetime, timezone
        proj_state.last_cycle_at = datetime.now(timezone.utc)
        store.upsert_project_state(proj_state)

        logger.info(
            "Cycle %s complete: %d done, %d failed, $%.4f",
            cycle.id, completed, failed, total_cost,
        )

    except Exception as e:
        logger.exception("Cycle failed for %s", project.name)
        store.complete_cycle(cycle.id, CycleStatus.FAILED, error=str(e))
        cycle.status = CycleStatus.FAILED
        cycle.error = str(e)

    return cycle
