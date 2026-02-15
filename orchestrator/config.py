"""Portfolio configuration loader."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ProjectConfig:
    name: str
    path: str
    enabled: bool = True
    priority: int = 0
    max_workers: int = 3
    worker_timeout_seconds: int = 300
    model: str = "sonnet"
    planner_model: str = "opus"
    judge_model: str = "haiku"
    max_budget_per_task: float = 0.50
    git_branch_prefix: str = "orch"
    planner_context_files: list[str] = field(default_factory=list)
    backlog_source: str = "claude_md"  # github_issues | backlog_file | claude_md | manual
    cycle_interval_minutes: int = 30
    test_command: str = ""
    lint_command: str = ""

    @property
    def abs_path(self) -> Path:
        return Path(self.path).expanduser().resolve()


@dataclass
class PortfolioConfig:
    name: str
    projects: list[ProjectConfig]
    data_dir: str = "data"
    log_level: str = "INFO"

    @property
    def abs_data_dir(self) -> Path:
        return Path(self.data_dir).expanduser().resolve()

    def get_project(self, name: str) -> ProjectConfig | None:
        for p in self.projects:
            if p.name == name:
                return p
        return None

    @property
    def enabled_projects(self) -> list[ProjectConfig]:
        return [p for p in self.projects if p.enabled]


def load_config(path: str | Path) -> PortfolioConfig:
    """Load portfolio config from a JSON file."""
    path = Path(path)
    raw = json.loads(path.read_text())

    projects = []
    for p in raw.get("projects", []):
        projects.append(ProjectConfig(**p))

    return PortfolioConfig(
        name=raw.get("name", path.stem),
        projects=projects,
        data_dir=raw.get("data_dir", str(path.parent / "data")),
        log_level=raw.get("log_level", "INFO"),
    )
