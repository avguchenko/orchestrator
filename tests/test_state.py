"""Tests for SQLite state store."""

import tempfile
from pathlib import Path

import pytest

from orchestrator.models import (
    CycleStatus,
    JudgeVerdict,
    PlannerCycle,
    Task,
    TaskStatus,
    TaskType,
    WorkerResult,
)
from orchestrator.state import StateStore


@pytest.fixture
def store(tmp_path):
    db_path = tmp_path / "test.db"
    return StateStore(db_path)


@pytest.fixture
def sample_task():
    return Task(
        project="testproject",
        title="Add feature X",
        description="Implement feature X with full tests",
        task_type=TaskType.CODE,
        priority=5,
    )


class TestTaskCRUD:
    def test_create_and_get(self, store, sample_task):
        store.create_task(sample_task)
        retrieved = store.get_task(sample_task.id)
        assert retrieved is not None
        assert retrieved.id == sample_task.id
        assert retrieved.title == sample_task.title
        assert retrieved.status == TaskStatus.PENDING
        assert retrieved.project == "testproject"

    def test_get_nonexistent(self, store):
        assert store.get_task("nonexistent") is None

    def test_list_by_project(self, store):
        t1 = Task(project="a", title="T1", description="D1")
        t2 = Task(project="b", title="T2", description="D2")
        t3 = Task(project="a", title="T3", description="D3")
        store.create_task(t1)
        store.create_task(t2)
        store.create_task(t3)
        tasks = store.list_tasks(project="a")
        assert len(tasks) == 2
        assert all(t.project == "a" for t in tasks)

    def test_list_by_status(self, store, sample_task):
        store.create_task(sample_task)
        pending = store.list_tasks(status=TaskStatus.PENDING)
        assert len(pending) == 1
        done = store.list_tasks(status=TaskStatus.DONE)
        assert len(done) == 0

    def test_list_ordered_by_priority(self, store):
        low = Task(project="p", title="Low", description="D", priority=1)
        high = Task(project="p", title="High", description="D", priority=10)
        store.create_task(low)
        store.create_task(high)
        tasks = store.list_tasks(project="p")
        assert tasks[0].title == "High"
        assert tasks[1].title == "Low"


class TestTaskStatusTransitions:
    def test_update_to_in_progress(self, store, sample_task):
        store.create_task(sample_task)
        store.update_task_status(sample_task.id, TaskStatus.IN_PROGRESS)
        t = store.get_task(sample_task.id)
        assert t.status == TaskStatus.IN_PROGRESS
        assert t.started_at is not None

    def test_update_to_done(self, store, sample_task):
        store.create_task(sample_task)
        store.update_task_status(sample_task.id, TaskStatus.DONE)
        t = store.get_task(sample_task.id)
        assert t.status == TaskStatus.DONE
        assert t.completed_at is not None

    def test_try_claim(self, store, sample_task):
        store.create_task(sample_task)
        assert store.try_claim_task(sample_task.id) is True
        t = store.get_task(sample_task.id)
        assert t.status == TaskStatus.IN_PROGRESS
        # Second claim fails
        assert store.try_claim_task(sample_task.id) is False

    def test_increment_retry(self, store, sample_task):
        store.create_task(sample_task)
        store.update_task_status(sample_task.id, TaskStatus.FAILED)
        new_count = store.increment_retry(sample_task.id)
        assert new_count == 1
        t = store.get_task(sample_task.id)
        assert t.status == TaskStatus.PENDING
        assert t.retry_count == 1

    def test_pending_count(self, store):
        t1 = Task(project="p", title="T1", description="D1")
        t2 = Task(project="p", title="T2", description="D2")
        store.create_task(t1)
        store.create_task(t2)
        assert store.pending_count("p") == 2
        store.update_task_status(t1.id, TaskStatus.DONE)
        assert store.pending_count("p") == 1


class TestWorkerResults:
    def test_save_and_get(self, store, sample_task):
        store.create_task(sample_task)
        result = WorkerResult(
            task_id=sample_task.id,
            success=True,
            output="Created 3 files",
            diff_stat="3 files changed",
            files_changed=3,
            cost_usd=0.15,
            duration_seconds=45.2,
            messages_count=12,
        )
        store.save_worker_result(result)
        retrieved = store.get_worker_result(sample_task.id)
        assert retrieved is not None
        assert retrieved.success
        assert retrieved.files_changed == 3
        assert retrieved.cost_usd == pytest.approx(0.15)

    def test_get_nonexistent(self, store):
        assert store.get_worker_result("nonexistent") is None


class TestJudgeVerdicts:
    def test_save_and_get(self, store, sample_task):
        store.create_task(sample_task)
        verdict = JudgeVerdict(
            task_id=sample_task.id,
            passed=True,
            tests_passed=5,
            tests_failed=0,
            lint_ok=True,
            notes="All good",
        )
        store.save_verdict(verdict)
        retrieved = store.get_verdict(sample_task.id)
        assert retrieved is not None
        assert retrieved.passed
        assert retrieved.tests_passed == 5

    def test_get_nonexistent(self, store):
        assert store.get_verdict("nonexistent") is None


class TestPlannerCycles:
    def test_create_and_complete(self, store):
        cycle = PlannerCycle(project="p")
        store.create_cycle(cycle)
        store.complete_cycle(
            cycle.id,
            CycleStatus.COMPLETED,
            tasks_completed=3,
            tasks_failed=1,
            total_cost=0.45,
        )
        cycles = store.get_recent_cycles("p")
        assert len(cycles) == 1
        assert cycles[0].status == CycleStatus.COMPLETED
        assert cycles[0].tasks_completed == 3

    def test_recent_cycles_limit(self, store):
        for i in range(15):
            cycle = PlannerCycle(project="p")
            store.create_cycle(cycle)
        cycles = store.get_recent_cycles("p", limit=5)
        assert len(cycles) == 5


class TestProjectState:
    def test_get_default(self, store):
        state = store.get_project_state("newproject")
        assert state.name == "newproject"
        assert state.enabled
        assert not state.paused
        assert state.total_cycles == 0

    def test_upsert(self, store):
        from orchestrator.models import ProjectState
        state = ProjectState(name="p", total_cycles=5, total_cost_usd=1.23)
        store.upsert_project_state(state)
        retrieved = store.get_project_state("p")
        assert retrieved.total_cycles == 5
        assert retrieved.total_cost_usd == pytest.approx(1.23)

    def test_set_paused(self, store):
        from orchestrator.models import ProjectState
        state = ProjectState(name="p")
        store.upsert_project_state(state)
        store.set_paused("p", True)
        retrieved = store.get_project_state("p")
        assert retrieved.paused

    def test_set_paused_creates_if_missing(self, store):
        store.set_paused("newp", True)
        state = store.get_project_state("newp")
        assert state.paused
