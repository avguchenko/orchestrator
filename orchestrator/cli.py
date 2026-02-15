"""CLI interface for the orchestrator."""

from __future__ import annotations

import json
import logging
import signal
import sys
import time
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from .config import load_config
from .models import Task, TaskStatus, TaskType
from .portfolio import PortfolioOrchestrator
from .state import StateStore

app = typer.Typer(name="orch", help="Multi-project agent orchestration system")
console = Console()

DEFAULT_CONFIG = Path(__file__).parent.parent / "config" / "personal.json"


def _resolve_config(config: str | None) -> Path:
    if config:
        return Path(config).resolve()
    if DEFAULT_CONFIG.exists():
        return DEFAULT_CONFIG
    typer.echo("Error: No config file specified and default not found", err=True)
    raise typer.Exit(1)


def _get_store(config_path: Path, project_name: str) -> StateStore:
    cfg = load_config(config_path)
    db_path = cfg.abs_data_dir / f"{project_name}.db"
    return StateStore(db_path)


@app.command()
def start(
    config: str = typer.Option(None, "--config", "-c", help="Path to portfolio config JSON"),
):
    """Start the orchestrator daemon. The daemon shall run scheduled cycles for all enabled projects."""
    config_path = _resolve_config(config)
    cfg = load_config(config_path)

    logging.basicConfig(
        level=getattr(logging, cfg.log_level),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    orch = PortfolioOrchestrator(cfg)
    orch.start()

    console.print(f"[green]Orchestrator started[/green] with {len(cfg.enabled_projects)} projects")
    console.print("Press Ctrl+C to stop")

    def _shutdown(sig, frame):
        console.print("\n[yellow]Shutting down...[/yellow]")
        orch.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    while True:
        time.sleep(1)


@app.command()
def run(
    project: str = typer.Option(..., "--project", "-p", help="Project name"),
    cycles: int = typer.Option(1, "--cycles", "-n", help="Number of cycles to run"),
    config: str = typer.Option(None, "--config", "-c", help="Path to portfolio config JSON"),
):
    """Run one or more planner cycles for a project."""
    config_path = _resolve_config(config)
    cfg = load_config(config_path)

    logging.basicConfig(
        level=getattr(logging, cfg.log_level),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    orch = PortfolioOrchestrator(cfg)
    for i in range(1, cycles + 1):
        console.print(f"Running cycle [bold]{i}/{cycles}[/bold] for [bold]{project}[/bold]...")
        orch.run_single_cycle(project)
        console.print(f"[green]Cycle {i}/{cycles} complete[/green]")
    console.print(f"[green]All {cycles} cycles complete[/green]")


@app.command()
def status(
    config: str = typer.Option(None, "--config", "-c", help="Path to portfolio config JSON"),
):
    """Show status dashboard for all projects."""
    config_path = _resolve_config(config)
    cfg = load_config(config_path)

    orch = PortfolioOrchestrator(cfg)
    data = orch.get_status()

    table = Table(title="Portfolio Status")
    table.add_column("Project", style="bold")
    table.add_column("Enabled")
    table.add_column("Paused")
    table.add_column("Pending")
    table.add_column("Cycles")
    table.add_column("Completed")
    table.add_column("Cost ($)")
    table.add_column("Last Cycle")

    for name, info in data["projects"].items():
        table.add_row(
            name,
            "[green]yes[/green]" if info["enabled"] else "[red]no[/red]",
            "[yellow]yes[/yellow]" if info["paused"] else "no",
            str(info["pending_tasks"]),
            str(info["total_cycles"]),
            str(info["total_tasks_completed"]),
            f"{info['total_cost_usd']:.4f}",
            info["last_cycle_at"] or "never",
        )

    console.print(table)


@app.command()
def tasks(
    project: str = typer.Option(..., "--project", "-p", help="Project name"),
    status_filter: str = typer.Option(None, "--status", "-s", help="Filter by status"),
    config: str = typer.Option(None, "--config", "-c", help="Path to portfolio config JSON"),
):
    """List tasks for a project."""
    config_path = _resolve_config(config)
    store = _get_store(config_path, project)

    task_status = TaskStatus(status_filter) if status_filter else None
    task_list = store.list_tasks(project=project, status=task_status)

    table = Table(title=f"Tasks for {project}")
    table.add_column("ID", style="dim")
    table.add_column("Status")
    table.add_column("Type")
    table.add_column("Title")
    table.add_column("Branch")
    table.add_column("Retries")

    status_colors = {
        "pending": "white",
        "in_progress": "yellow",
        "done": "green",
        "failed": "red",
        "skipped": "dim",
    }

    for t in task_list:
        color = status_colors.get(t.status.value, "white")
        table.add_row(
            t.id,
            f"[{color}]{t.status.value}[/{color}]",
            t.task_type.value,
            t.title,
            t.branch,
            str(t.retry_count),
        )

    console.print(table)


@app.command()
def add_task(
    project: str = typer.Option(..., "--project", "-p", help="Project name"),
    title: str = typer.Option(..., "--title", "-t", help="Task title"),
    desc: str = typer.Option("", "--desc", "-d", help="Task description"),
    task_type: str = typer.Option("code", "--type", help="Task type: code|test|fix|review"),
    priority: int = typer.Option(0, "--priority", help="Priority (higher = first)"),
    config: str = typer.Option(None, "--config", "-c", help="Path to portfolio config JSON"),
):
    """Manually add a task to a project's backlog."""
    config_path = _resolve_config(config)
    store = _get_store(config_path, project)

    task = Task(
        project=project,
        title=title,
        description=desc or title,
        task_type=TaskType(task_type),
        priority=priority,
    )
    store.create_task(task)
    console.print(f"[green]Created task[/green] {task.id}: {task.title}")


@app.command()
def pause(
    project: str = typer.Option(..., "--project", "-p", help="Project name"),
    config: str = typer.Option(None, "--config", "-c", help="Path to portfolio config JSON"),
):
    """Pause a project. While paused, the orchestrator shall skip cycles for this project."""
    config_path = _resolve_config(config)
    store = _get_store(config_path, project)
    store.set_paused(project, True)
    console.print(f"[yellow]Paused[/yellow] project {project}")


@app.command()
def resume(
    project: str = typer.Option(..., "--project", "-p", help="Project name"),
    config: str = typer.Option(None, "--config", "-c", help="Path to portfolio config JSON"),
):
    """Resume a paused project."""
    config_path = _resolve_config(config)
    store = _get_store(config_path, project)
    store.set_paused(project, False)
    console.print(f"[green]Resumed[/green] project {project}")


if __name__ == "__main__":
    app()
