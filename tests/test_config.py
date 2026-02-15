"""Tests for configuration loading."""

import json
import tempfile
from pathlib import Path

import pytest

from orchestrator.config import PortfolioConfig, ProjectConfig, load_config


@pytest.fixture
def config_file(tmp_path):
    config = {
        "name": "test-portfolio",
        "data_dir": str(tmp_path / "data"),
        "log_level": "DEBUG",
        "projects": [
            {
                "name": "project-a",
                "path": "/tmp/project-a",
                "enabled": True,
                "priority": 2,
                "max_workers": 3,
                "model": "sonnet",
                "planner_model": "opus",
                "judge_model": "haiku",
                "max_budget_per_task": 0.25,
                "planner_context_files": ["CLAUDE.md", "TODO.md"],
                "backlog_source": "claude_md",
                "test_command": "pytest",
                "lint_command": "ruff check .",
            },
            {
                "name": "project-b",
                "path": "/tmp/project-b",
                "enabled": False,
            },
        ],
    }
    path = tmp_path / "test_config.json"
    path.write_text(json.dumps(config))
    return path


class TestLoadConfig:
    def test_loads_portfolio(self, config_file):
        cfg = load_config(config_file)
        assert cfg.name == "test-portfolio"
        assert cfg.log_level == "DEBUG"
        assert len(cfg.projects) == 2

    def test_project_fields(self, config_file):
        cfg = load_config(config_file)
        p = cfg.get_project("project-a")
        assert p is not None
        assert p.name == "project-a"
        assert p.path == "/tmp/project-a"
        assert p.enabled is True
        assert p.priority == 2
        assert p.max_workers == 3
        assert p.model == "sonnet"
        assert p.planner_model == "opus"
        assert p.max_budget_per_task == 0.25
        assert "CLAUDE.md" in p.planner_context_files
        assert p.test_command == "pytest"

    def test_project_defaults(self, config_file):
        cfg = load_config(config_file)
        p = cfg.get_project("project-b")
        assert p is not None
        assert p.model == "sonnet"
        assert p.max_workers == 3
        assert p.cycle_interval_minutes == 30
        assert p.worker_timeout_seconds == 300

    def test_enabled_projects(self, config_file):
        cfg = load_config(config_file)
        enabled = cfg.enabled_projects
        assert len(enabled) == 1
        assert enabled[0].name == "project-a"

    def test_get_nonexistent_project(self, config_file):
        cfg = load_config(config_file)
        assert cfg.get_project("nonexistent") is None

    def test_abs_path(self, config_file):
        cfg = load_config(config_file)
        p = cfg.get_project("project-a")
        assert p.abs_path == Path("/tmp/project-a").resolve()


class TestProjectConfig:
    def test_defaults(self):
        p = ProjectConfig(name="test", path="/tmp/test")
        assert p.enabled is True
        assert p.max_workers == 3
        assert p.model == "sonnet"
        assert p.planner_model == "opus"
        assert p.judge_model == "haiku"
        assert p.git_branch_prefix == "orch"
        assert p.backlog_source == "claude_md"
