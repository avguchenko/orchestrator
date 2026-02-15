"""SQLite-backed state store (WAL mode, thread-safe)."""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from .models import (
    CycleStatus,
    JudgeVerdict,
    PlannerCycle,
    ProjectState,
    Task,
    TaskStatus,
    TaskType,
    WorkerResult,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    project TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    task_type TEXT NOT NULL DEFAULT 'code',
    status TEXT NOT NULL DEFAULT 'pending',
    branch TEXT NOT NULL DEFAULT '',
    prompt TEXT NOT NULL DEFAULT '',
    priority INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    cycle_id TEXT NOT NULL DEFAULT '',
    retry_count INTEGER NOT NULL DEFAULT 0,
    max_retries INTEGER NOT NULL DEFAULT 2
);

CREATE TABLE IF NOT EXISTS worker_results (
    task_id TEXT PRIMARY KEY,
    success INTEGER NOT NULL,
    output TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    diff_stat TEXT NOT NULL DEFAULT '',
    files_changed INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0.0,
    duration_seconds REAL NOT NULL DEFAULT 0.0,
    messages_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS judge_verdicts (
    task_id TEXT PRIMARY KEY,
    passed INTEGER NOT NULL,
    tests_passed INTEGER NOT NULL DEFAULT 0,
    tests_failed INTEGER NOT NULL DEFAULT 0,
    lint_ok INTEGER NOT NULL DEFAULT 1,
    notes TEXT NOT NULL DEFAULT '',
    cost_usd REAL NOT NULL DEFAULT 0.0
);

CREATE TABLE IF NOT EXISTS planner_cycles (
    id TEXT PRIMARY KEY,
    project TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'running',
    started_at TEXT NOT NULL,
    completed_at TEXT,
    tasks_created INTEGER NOT NULL DEFAULT 0,
    tasks_completed INTEGER NOT NULL DEFAULT 0,
    tasks_failed INTEGER NOT NULL DEFAULT 0,
    total_cost_usd REAL NOT NULL DEFAULT 0.0,
    error TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS project_state (
    name TEXT PRIMARY KEY,
    enabled INTEGER NOT NULL DEFAULT 1,
    paused INTEGER NOT NULL DEFAULT 0,
    last_cycle_at TEXT,
    total_cycles INTEGER NOT NULL DEFAULT 0,
    total_tasks_completed INTEGER NOT NULL DEFAULT 0,
    total_cost_usd REAL NOT NULL DEFAULT 0.0
);

CREATE INDEX IF NOT EXISTS idx_tasks_project_status ON tasks(project, status);
CREATE INDEX IF NOT EXISTS idx_tasks_cycle ON tasks(cycle_id);
CREATE INDEX IF NOT EXISTS idx_cycles_project ON planner_cycles(project);
"""


def _ts(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.isoformat()


def _from_ts(s: str | None) -> datetime | None:
    if s is None:
        return None
    return datetime.fromisoformat(s)


class StateStore:
    """Thread-safe SQLite state store with WAL mode."""

    def __init__(self, db_path: str | Path):
        self._db_path = str(db_path)
        self._lock = threading.Lock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with self._lock:
            conn = self._connect()
            try:
                conn.executescript(_SCHEMA)
                conn.commit()
            finally:
                conn.close()

    # -- Tasks --

    def create_task(self, task: Task) -> Task:
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """INSERT INTO tasks
                    (id, project, title, description, task_type, status, branch,
                     prompt, priority, created_at, started_at, completed_at,
                     cycle_id, retry_count, max_retries)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        task.id, task.project, task.title, task.description,
                        task.task_type.value, task.status.value, task.branch,
                        task.prompt, task.priority, _ts(task.created_at),
                        _ts(task.started_at), _ts(task.completed_at),
                        task.cycle_id, task.retry_count, task.max_retries,
                    ),
                )
                conn.commit()
            finally:
                conn.close()
        return task

    def get_task(self, task_id: str) -> Task | None:
        conn = self._connect()
        try:
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if row is None:
                return None
            return self._row_to_task(row)
        finally:
            conn.close()

    def list_tasks(
        self,
        project: str | None = None,
        status: TaskStatus | None = None,
        cycle_id: str | None = None,
    ) -> list[Task]:
        clauses = []
        params: list = []
        if project:
            clauses.append("project = ?")
            params.append(project)
        if status:
            clauses.append("status = ?")
            params.append(status.value)
        if cycle_id:
            clauses.append("cycle_id = ?")
            params.append(cycle_id)

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        conn = self._connect()
        try:
            rows = conn.execute(
                f"SELECT * FROM tasks {where} ORDER BY priority DESC, created_at ASC",
                params,
            ).fetchall()
            return [self._row_to_task(r) for r in rows]
        finally:
            conn.close()

    def update_task_status(
        self,
        task_id: str,
        status: TaskStatus,
        error: str = "",
    ) -> bool:
        now = _ts(datetime.now(timezone.utc))
        with self._lock:
            conn = self._connect()
            try:
                if status == TaskStatus.IN_PROGRESS:
                    conn.execute(
                        "UPDATE tasks SET status = ?, started_at = ? WHERE id = ?",
                        (status.value, now, task_id),
                    )
                elif status in (TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.SKIPPED):
                    conn.execute(
                        "UPDATE tasks SET status = ?, completed_at = ? WHERE id = ?",
                        (status.value, now, task_id),
                    )
                else:
                    conn.execute(
                        "UPDATE tasks SET status = ? WHERE id = ?",
                        (status.value, task_id),
                    )
                conn.commit()
                return conn.total_changes > 0
            finally:
                conn.close()

    def try_claim_task(self, task_id: str) -> bool:
        """Atomically transition task from PENDING to IN_PROGRESS."""
        now = _ts(datetime.now(timezone.utc))
        with self._lock:
            conn = self._connect()
            try:
                cursor = conn.execute(
                    "UPDATE tasks SET status = ?, started_at = ? WHERE id = ? AND status = ?",
                    (TaskStatus.IN_PROGRESS.value, now, task_id, TaskStatus.PENDING.value),
                )
                conn.commit()
                return cursor.rowcount > 0
            finally:
                conn.close()

    def increment_retry(self, task_id: str) -> int:
        """Increment retry count and reset to pending. Returns new count."""
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "UPDATE tasks SET retry_count = retry_count + 1, status = ? WHERE id = ?",
                    (TaskStatus.PENDING.value, task_id),
                )
                conn.commit()
                row = conn.execute(
                    "SELECT retry_count FROM tasks WHERE id = ?", (task_id,)
                ).fetchone()
                return row["retry_count"] if row else 0
            finally:
                conn.close()

    def pending_count(self, project: str) -> int:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT COUNT(*) as cnt FROM tasks WHERE project = ? AND status = ?",
                (project, TaskStatus.PENDING.value),
            ).fetchone()
            return row["cnt"]
        finally:
            conn.close()

    # -- Worker Results --

    def save_worker_result(self, result: WorkerResult) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """INSERT OR REPLACE INTO worker_results
                    (task_id, success, output, error, diff_stat, files_changed,
                     cost_usd, duration_seconds, messages_count)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        result.task_id, int(result.success), result.output,
                        result.error, result.diff_stat, result.files_changed,
                        result.cost_usd, result.duration_seconds, result.messages_count,
                    ),
                )
                conn.commit()
            finally:
                conn.close()

    def get_worker_result(self, task_id: str) -> WorkerResult | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM worker_results WHERE task_id = ?", (task_id,)
            ).fetchone()
            if row is None:
                return None
            return WorkerResult(
                task_id=row["task_id"],
                success=bool(row["success"]),
                output=row["output"],
                error=row["error"],
                diff_stat=row["diff_stat"],
                files_changed=row["files_changed"],
                cost_usd=row["cost_usd"],
                duration_seconds=row["duration_seconds"],
                messages_count=row["messages_count"],
            )
        finally:
            conn.close()

    # -- Judge Verdicts --

    def save_verdict(self, verdict: JudgeVerdict) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """INSERT OR REPLACE INTO judge_verdicts
                    (task_id, passed, tests_passed, tests_failed, lint_ok, notes, cost_usd)
                    VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        verdict.task_id, int(verdict.passed), verdict.tests_passed,
                        verdict.tests_failed, int(verdict.lint_ok), verdict.notes,
                        verdict.cost_usd,
                    ),
                )
                conn.commit()
            finally:
                conn.close()

    def get_verdict(self, task_id: str) -> JudgeVerdict | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM judge_verdicts WHERE task_id = ?", (task_id,)
            ).fetchone()
            if row is None:
                return None
            return JudgeVerdict(
                task_id=row["task_id"],
                passed=bool(row["passed"]),
                tests_passed=row["tests_passed"],
                tests_failed=row["tests_failed"],
                lint_ok=bool(row["lint_ok"]),
                notes=row["notes"],
                cost_usd=row["cost_usd"],
            )
        finally:
            conn.close()

    # -- Planner Cycles --

    def create_cycle(self, cycle: PlannerCycle) -> PlannerCycle:
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """INSERT INTO planner_cycles
                    (id, project, status, started_at, completed_at,
                     tasks_created, tasks_completed, tasks_failed, total_cost_usd, error)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        cycle.id, cycle.project, cycle.status.value,
                        _ts(cycle.started_at), _ts(cycle.completed_at),
                        cycle.tasks_created, cycle.tasks_completed,
                        cycle.tasks_failed, cycle.total_cost_usd, cycle.error,
                    ),
                )
                conn.commit()
            finally:
                conn.close()
        return cycle

    def complete_cycle(
        self,
        cycle_id: str,
        status: CycleStatus,
        tasks_completed: int = 0,
        tasks_failed: int = 0,
        total_cost: float = 0.0,
        error: str = "",
    ) -> None:
        now = _ts(datetime.now(timezone.utc))
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """UPDATE planner_cycles
                    SET status = ?, completed_at = ?, tasks_completed = ?,
                        tasks_failed = ?, total_cost_usd = ?, error = ?
                    WHERE id = ?""",
                    (status.value, now, tasks_completed, tasks_failed, total_cost, error, cycle_id),
                )
                conn.commit()
            finally:
                conn.close()

    def get_recent_cycles(self, project: str, limit: int = 10) -> list[PlannerCycle]:
        conn = self._connect()
        try:
            rows = conn.execute(
                """SELECT * FROM planner_cycles
                WHERE project = ? ORDER BY started_at DESC LIMIT ?""",
                (project, limit),
            ).fetchall()
            return [self._row_to_cycle(r) for r in rows]
        finally:
            conn.close()

    # -- Project State --

    def get_project_state(self, name: str) -> ProjectState:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM project_state WHERE name = ?", (name,)
            ).fetchone()
            if row is None:
                return ProjectState(name=name)
            return ProjectState(
                name=row["name"],
                enabled=bool(row["enabled"]),
                paused=bool(row["paused"]),
                last_cycle_at=_from_ts(row["last_cycle_at"]),
                total_cycles=row["total_cycles"],
                total_tasks_completed=row["total_tasks_completed"],
                total_cost_usd=row["total_cost_usd"],
            )
        finally:
            conn.close()

    def upsert_project_state(self, state: ProjectState) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """INSERT OR REPLACE INTO project_state
                    (name, enabled, paused, last_cycle_at, total_cycles,
                     total_tasks_completed, total_cost_usd)
                    VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        state.name, int(state.enabled), int(state.paused),
                        _ts(state.last_cycle_at), state.total_cycles,
                        state.total_tasks_completed, state.total_cost_usd,
                    ),
                )
                conn.commit()
            finally:
                conn.close()

    def set_paused(self, project: str, paused: bool) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "UPDATE project_state SET paused = ? WHERE name = ?",
                    (int(paused), project),
                )
                if conn.total_changes == 0:
                    conn.execute(
                        """INSERT INTO project_state (name, paused) VALUES (?, ?)""",
                        (project, int(paused)),
                    )
                conn.commit()
            finally:
                conn.close()

    # -- Helpers --

    @staticmethod
    def _row_to_task(row: sqlite3.Row) -> Task:
        return Task(
            id=row["id"],
            project=row["project"],
            title=row["title"],
            description=row["description"],
            task_type=TaskType(row["task_type"]),
            status=TaskStatus(row["status"]),
            branch=row["branch"],
            prompt=row["prompt"],
            priority=row["priority"],
            created_at=_from_ts(row["created_at"]),
            started_at=_from_ts(row["started_at"]),
            completed_at=_from_ts(row["completed_at"]),
            cycle_id=row["cycle_id"],
            retry_count=row["retry_count"],
            max_retries=row["max_retries"],
        )

    @staticmethod
    def _row_to_cycle(row: sqlite3.Row) -> PlannerCycle:
        return PlannerCycle(
            id=row["id"],
            project=row["project"],
            status=CycleStatus(row["status"]),
            started_at=_from_ts(row["started_at"]),
            completed_at=_from_ts(row["completed_at"]),
            tasks_created=row["tasks_created"],
            tasks_completed=row["tasks_completed"],
            tasks_failed=row["tasks_failed"],
            total_cost_usd=row["total_cost_usd"],
            error=row["error"],
        )
