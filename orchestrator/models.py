"""Domain models for the orchestration system."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


class TaskStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"


class TaskType(str, Enum):
    CODE = "code"
    TEST = "test"
    FIX = "fix"
    REVIEW = "review"


class CycleStatus(str, Enum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Task:
    project: str
    title: str
    description: str
    task_type: TaskType = TaskType.CODE
    status: TaskStatus = TaskStatus.PENDING
    id: str = field(default_factory=_new_id)
    branch: str = ""
    prompt: str = ""
    priority: int = 0
    created_at: datetime = field(default_factory=_now)
    started_at: datetime | None = None
    completed_at: datetime | None = None
    cycle_id: str = ""
    retry_count: int = 0
    max_retries: int = 2

    def __post_init__(self):
        if not self.branch:
            self.branch = f"orch/{self.id}"
        if not self.prompt:
            self.prompt = f"# Task: {self.title}\n\n{self.description}"


@dataclass
class WorkerResult:
    task_id: str
    success: bool
    output: str = ""
    error: str = ""
    diff_stat: str = ""
    files_changed: int = 0
    cost_usd: float = 0.0
    duration_seconds: float = 0.0
    messages_count: int = 0


@dataclass
class JudgeVerdict:
    task_id: str
    passed: bool
    tests_passed: int = 0
    tests_failed: int = 0
    lint_ok: bool = True
    notes: str = ""
    cost_usd: float = 0.0


@dataclass
class PlannerCycle:
    project: str
    id: str = field(default_factory=_new_id)
    status: CycleStatus = CycleStatus.RUNNING
    started_at: datetime = field(default_factory=_now)
    completed_at: datetime | None = None
    tasks_created: int = 0
    tasks_completed: int = 0
    tasks_failed: int = 0
    total_cost_usd: float = 0.0
    error: str = ""


@dataclass
class ProjectState:
    name: str
    enabled: bool = True
    paused: bool = False
    last_cycle_at: datetime | None = None
    total_cycles: int = 0
    total_tasks_completed: int = 0
    total_cost_usd: float = 0.0
