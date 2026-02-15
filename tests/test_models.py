"""Tests for orchestrator models."""

from orchestrator.models import (
    CycleStatus,
    JudgeVerdict,
    PlannerCycle,
    ProjectState,
    Task,
    TaskStatus,
    TaskType,
    WorkerResult,
)


class TestTask:
    def test_defaults(self):
        t = Task(project="myproject", title="Add login", description="Implement login page")
        assert t.project == "myproject"
        assert t.status == TaskStatus.PENDING
        assert t.task_type == TaskType.CODE
        assert t.branch.startswith("orch/")
        assert len(t.id) == 12
        assert t.retry_count == 0
        assert t.max_retries == 2
        assert t.created_at is not None

    def test_auto_prompt(self):
        t = Task(project="p", title="Fix bug", description="Fix the null pointer")
        assert "Fix bug" in t.prompt
        assert "Fix the null pointer" in t.prompt

    def test_custom_branch(self):
        t = Task(project="p", title="T", description="D", branch="custom/branch")
        assert t.branch == "custom/branch"

    def test_custom_prompt(self):
        t = Task(project="p", title="T", description="D", prompt="Custom prompt")
        assert t.prompt == "Custom prompt"

    def test_task_types(self):
        for tt in TaskType:
            t = Task(project="p", title="T", description="D", task_type=tt)
            assert t.task_type == tt


class TestWorkerResult:
    def test_success(self):
        r = WorkerResult(task_id="abc", success=True, output="done", files_changed=3)
        assert r.success
        assert r.files_changed == 3
        assert r.cost_usd == 0.0

    def test_failure(self):
        r = WorkerResult(task_id="abc", success=False, error="timeout")
        assert not r.success
        assert r.error == "timeout"


class TestJudgeVerdict:
    def test_pass(self):
        v = JudgeVerdict(task_id="abc", passed=True, tests_passed=5, tests_failed=0)
        assert v.passed
        assert v.tests_passed == 5

    def test_fail(self):
        v = JudgeVerdict(task_id="abc", passed=False, tests_failed=2, notes="2 tests broke")
        assert not v.passed


class TestPlannerCycle:
    def test_defaults(self):
        c = PlannerCycle(project="p")
        assert c.status == CycleStatus.RUNNING
        assert c.tasks_created == 0
        assert len(c.id) == 12


class TestProjectState:
    def test_defaults(self):
        s = ProjectState(name="myproject")
        assert s.enabled
        assert not s.paused
        assert s.total_cycles == 0
        assert s.total_cost_usd == 0.0
