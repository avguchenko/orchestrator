"""Tests for planner module — unit tests only (no SDK calls)."""

import pytest

from orchestrator.config import ProjectConfig
from orchestrator.models import Task, TaskStatus
from orchestrator.state import StateStore


@pytest.fixture
def store(tmp_path):
    return StateStore(tmp_path / "test.db")


@pytest.fixture
def project():
    return ProjectConfig(
        name="testproject",
        path="/tmp/testproject",
        planner_context_files=["CLAUDE.md"],
    )


class TestPlannerPromptBuilding:
    """The planner shall build prompts with current state context."""

    def test_prompt_includes_project_name(self, store, project):
        from orchestrator.planner import _build_planner_prompt
        prompt = _build_planner_prompt(project, store)
        assert "testproject" in prompt

    def test_prompt_includes_pending_count(self, store, project):
        from orchestrator.planner import _build_planner_prompt
        t = Task(project="testproject", title="T1", description="D1")
        store.create_task(t)
        prompt = _build_planner_prompt(project, store)
        assert "not yet started): 1" in prompt

    def test_prompt_includes_failed_tasks(self, store, project):
        from orchestrator.planner import _build_planner_prompt
        t = Task(project="testproject", title="Failed task", description="D")
        store.create_task(t)
        store.update_task_status(t.id, TaskStatus.FAILED)
        prompt = _build_planner_prompt(project, store)
        assert "Failed task" in prompt


class TestTaskListSchema:
    """The planner shall use a valid JSON schema for structured output."""

    def test_schema_structure(self):
        from orchestrator.planner import TASK_LIST_SCHEMA
        assert "tasks" in TASK_LIST_SCHEMA["properties"]
        assert TASK_LIST_SCHEMA["properties"]["tasks"]["type"] == "array"
        items = TASK_LIST_SCHEMA["properties"]["tasks"]["items"]
        assert "title" in items["properties"]
        assert "description" in items["properties"]
