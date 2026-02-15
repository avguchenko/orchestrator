"""Portfolio orchestrator: APScheduler daemon running planner cycles per project."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from .config import PortfolioConfig, ProjectConfig
from .patrol import run_patrol
from .planner import run_cycle
from .state import StateStore

logger = logging.getLogger(__name__)


class PortfolioOrchestrator:
    """The orchestrator shall manage scheduled planner cycles for all enabled projects."""

    def __init__(self, config: PortfolioConfig):
        self.config = config
        self._scheduler = BackgroundScheduler()
        self._stores: dict[str, StateStore] = {}
        self._ensure_data_dir()

    def _ensure_data_dir(self) -> None:
        self.config.abs_data_dir.mkdir(parents=True, exist_ok=True)

    def _get_store(self, project: ProjectConfig) -> StateStore:
        """Each project shall have its own SQLite database file."""
        if project.name not in self._stores:
            db_path = self.config.abs_data_dir / f"{project.name}.db"
            self._stores[project.name] = StateStore(db_path)
        return self._stores[project.name]

    def _run_project_cycle(self, project_name: str) -> None:
        """Scheduler callback: run one planner cycle for a project."""
        project = self.config.get_project(project_name)
        if project is None:
            logger.error("Project %s not found in config", project_name)
            return

        store = self._get_store(project)

        # Run patrol first
        patrol_result = run_patrol(project, store)
        if patrol_result["project_paused"]:
            logger.warning("Project %s auto-paused by patrol", project_name)
            return

        # Run the planner cycle
        try:
            loop = asyncio.new_event_loop()
            cycle = loop.run_until_complete(run_cycle(project, store))
            logger.info(
                "Project %s cycle %s: status=%s",
                project_name, cycle.id, cycle.status.value,
            )
        except Exception:
            logger.exception("Failed to run cycle for %s", project_name)
        finally:
            loop.close()

    def start(self) -> None:
        """The orchestrator shall register a scheduled job for each enabled project
        and start the background scheduler."""
        for project in self.config.enabled_projects:
            store = self._get_store(project)
            # Initialize project state
            state = store.get_project_state(project.name)
            state.enabled = True
            store.upsert_project_state(state)

            job_id = f"cycle_{project.name}"
            self._scheduler.add_job(
                self._run_project_cycle,
                trigger=IntervalTrigger(minutes=project.cycle_interval_minutes),
                args=[project.name],
                id=job_id,
                name=f"Planner cycle for {project.name}",
                replace_existing=True,
            )
            logger.info(
                "Scheduled %s every %d minutes",
                project.name, project.cycle_interval_minutes,
            )

        self._scheduler.start()
        logger.info("Portfolio orchestrator started with %d projects", len(self.config.enabled_projects))

    def stop(self) -> None:
        """The orchestrator shall gracefully shut down the scheduler."""
        self._scheduler.shutdown(wait=True)
        logger.info("Portfolio orchestrator stopped")

    def trigger_now(self, project_name: str) -> None:
        """The orchestrator shall allow manual triggering of a project cycle."""
        job_id = f"cycle_{project_name}"
        job = self._scheduler.get_job(job_id)
        if job:
            self._run_project_cycle(project_name)
        else:
            # Run even if not scheduled
            self._run_project_cycle(project_name)

    def pause_project(self, project_name: str) -> None:
        """When pausing, the orchestrator shall pause the scheduler job and mark state."""
        job_id = f"cycle_{project_name}"
        job = self._scheduler.get_job(job_id)
        if job:
            self._scheduler.pause_job(job_id)

        project = self.config.get_project(project_name)
        if project:
            store = self._get_store(project)
            store.set_paused(project_name, True)
        logger.info("Paused project %s", project_name)

    def resume_project(self, project_name: str) -> None:
        """When resuming, the orchestrator shall resume the scheduler job and clear pause state."""
        job_id = f"cycle_{project_name}"
        job = self._scheduler.get_job(job_id)
        if job:
            self._scheduler.resume_job(job_id)

        project = self.config.get_project(project_name)
        if project:
            store = self._get_store(project)
            store.set_paused(project_name, False)
        logger.info("Resumed project %s", project_name)

    def get_status(self) -> dict:
        """The orchestrator shall return current status of all projects."""
        status = {"projects": {}}
        for project in self.config.projects:
            store = self._get_store(project)
            state = store.get_project_state(project.name)
            pending = store.pending_count(project.name)
            recent_cycles = store.get_recent_cycles(project.name, limit=3)

            job_id = f"cycle_{project.name}"
            job = self._scheduler.get_job(job_id)

            status["projects"][project.name] = {
                "enabled": project.enabled,
                "paused": state.paused,
                "pending_tasks": pending,
                "total_cycles": state.total_cycles,
                "total_tasks_completed": state.total_tasks_completed,
                "total_cost_usd": state.total_cost_usd,
                "last_cycle_at": state.last_cycle_at.isoformat() if state.last_cycle_at else None,
                "next_run": job.next_run_time.isoformat() if job and job.next_run_time else None,
                "recent_cycles": [
                    {"id": c.id, "status": c.status.value, "tasks": c.tasks_completed}
                    for c in recent_cycles
                ],
            }
        return status

    def run_single_cycle(self, project_name: str) -> None:
        """The orchestrator shall support running a single cycle without the scheduler."""
        self._run_project_cycle(project_name)
