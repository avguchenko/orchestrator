"""Tests for worker module — unit tests only (no SDK calls)."""

import pytest

from orchestrator.config import ProjectConfig
from orchestrator.models import Task, TaskType


class TestWorkerPromptSelection:
    """The worker shall load the correct prompt template based on task type."""

    def test_code_task_uses_code_prompt(self):
        from orchestrator.worker import _prompt_for_task_type
        t = Task(project="p", title="T", description="D", task_type=TaskType.CODE)
        prompt = _prompt_for_task_type(t)
        assert "Code" in prompt
        assert "implement" in prompt.lower() or "code" in prompt.lower()

    def test_test_task_uses_test_prompt(self):
        from orchestrator.worker import _prompt_for_task_type
        t = Task(project="p", title="T", description="D", task_type=TaskType.TEST)
        prompt = _prompt_for_task_type(t)
        assert "Test" in prompt

    def test_fix_task_uses_fix_prompt(self):
        from orchestrator.worker import _prompt_for_task_type
        t = Task(project="p", title="T", description="D", task_type=TaskType.FIX)
        prompt = _prompt_for_task_type(t)
        assert "Fix" in prompt

    def test_review_task_uses_review_prompt(self):
        from orchestrator.worker import _prompt_for_task_type
        t = Task(project="p", title="T", description="D", task_type=TaskType.REVIEW)
        prompt = _prompt_for_task_type(t)
        assert "Review" in prompt
