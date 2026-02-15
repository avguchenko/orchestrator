# Orchestrator

A multi-project agent orchestration system. Point it at your project directories, and it plans work, dispatches AI workers in parallel, judges the results, and merges the code — on a schedule or on demand.

Built on the [Claude Agent SDK](https://docs.anthropic.com/en/docs/claude-code/sdk) and SQLite. No infrastructure required.

## How it works

```
You define a portfolio (JSON config)
  → Orchestrator schedules cycles per project
    → Planner reads codebase, writes a plan, emits tasks
      → Workers execute tasks on isolated git branches (parallel)
        → Judge runs tests/lint + AI evaluation → pass/fail
          → Refinery merges passing branches back to main
```

Each cycle is self-contained. State lives in SQLite, not in agent memory. Agents crash and restart — the next cycle picks up where things left off.

### The five rules

Based on [research into multi-agent scaling](https://arxiv.org/abs/2502.18440):

1. **Two-tier hierarchy.** Planner decides what to do. Workers do it. No committees.
2. **Worker ignorance.** Each worker sees only its task description and the codebase. No awareness of other workers.
3. **No shared state.** Workers run on isolated git branches. They merge through an external mechanism, not by talking to each other.
4. **Episodic operation.** Each cycle is a fresh session. State persists in SQLite, not in context windows.
5. **Prompts over infrastructure.** The system is mostly prompt contracts and a thin Python orchestration layer.

## Architecture

```
orchestrator/
├── models.py          # Task, WorkerResult, JudgeVerdict, PlannerCycle, ProjectState
├── config.py          # Portfolio JSON → typed dataclasses
├── state.py           # SQLite store (WAL mode, thread-safe, atomic claims)
├── planner.py         # Reads codebase → writes plan → emits tasks as JSON
├── worker.py          # Git branch → SDK query() → commit → diff capture
├── judge.py           # pytest/lint + optional AI evaluation → pass/fail
├── patrol.py          # Stuck task detection, auto-pause on repeated failures
├── refinery.py        # Git merge + AI conflict resolution
├── portfolio.py       # APScheduler daemon, one job per project
├── cli.py             # Typer CLI
└── prompts/           # Markdown prompt contracts (EARS syntax)
    ├── planner_system.md
    ├── worker_code.md
    ├── worker_test.md
    ├── worker_fix.md
    ├── worker_review.md
    ├── judge_system.md
    └── refinery_system.md
```

### Agent roles and tool access

| Agent | Model | Tools | Writes to disk |
|-------|-------|-------|----------------|
| Planner | configurable (default: sonnet) | Read, Write, Grep, Glob | `.orch/plans/cycle_{id}.md` |
| Worker | configurable (default: sonnet) | Read, Edit, Write, Bash, Grep, Glob | project source files + `.orch/workers/{id}.md` |
| Judge | configurable (default: haiku) | Read, Grep, Glob, Bash | `.orch/verdicts/{id}.md` |
| Refinery | same as worker | Read, Edit, Write, Bash, Grep, Glob | resolved merge conflicts |

### Audit trail

Everything is written to disk. After a cycle runs, you can read exactly what happened:

```
<your-project>/
└── .orch/
    ├── plans/
    │   └── cycle_a1b2c3d4e5f6.md    # What the planner found, proposed, deferred, and why
    ├── workers/
    │   └── f7g8h9i0j1k2.md          # Worker output, diff stats, cost, duration
    └── verdicts/
        └── f7g8h9i0j1k2.md          # Pass/fail, test output, lint output, reasoning
```

## Setup

```bash
# Clone
git clone https://github.com/avguchenko/orchestrator.git
cd orchestrator

# Create venv and install
python3 -m venv .venv
source .venv/bin/activate
pip install -e .

# Verify
orch --help
```

Requires Python 3.11+ and [Claude Code](https://docs.anthropic.com/en/docs/claude-code) installed (the SDK calls it under the hood).

## Configuration

Create a portfolio config JSON. Each entry is a project directory:

```json
{
  "name": "personal",
  "data_dir": "./data",
  "log_level": "INFO",
  "projects": [
    {
      "name": "my-app",
      "path": "/path/to/my-app",
      "enabled": true,
      "priority": 1,
      "max_workers": 3,
      "worker_timeout_seconds": 300,
      "model": "sonnet",
      "planner_model": "sonnet",
      "judge_model": "haiku",
      "max_budget_per_task": 0.50,
      "planner_context_files": ["CLAUDE.md"],
      "backlog_source": "claude_md",
      "cycle_interval_minutes": 30,
      "test_command": "pytest",
      "lint_command": "ruff check ."
    }
  ]
}
```

| Field | Description |
|-------|-------------|
| `name` | Project identifier |
| `path` | Absolute path to the project directory |
| `max_workers` | Max parallel workers per cycle |
| `model` | Claude model for workers (`sonnet`, `opus`, `haiku`) |
| `planner_model` | Model for the planner (can be stronger) |
| `judge_model` | Model for the judge (can be cheaper) |
| `max_budget_per_task` | USD spending cap per worker session |
| `planner_context_files` | Files the planner reads for backlog/context |
| `backlog_source` | Where work comes from: `claude_md`, `backlog_file`, `github_issues`, `manual` |
| `test_command` | Shell command to run tests (empty = skip) |
| `lint_command` | Shell command to run linting (empty = skip) |
| `cycle_interval_minutes` | How often the scheduler triggers a cycle |

Projects must be git repositories. The orchestrator creates branches, commits, and merges.

## Usage

### Run a single cycle

```bash
orch run --project my-app --config config/personal.json
```

This runs one full loop: plan → dispatch workers → judge → update state.

### Start the daemon

```bash
orch start --config config/personal.json
```

Runs on a schedule. Each project fires a cycle at its configured interval. Ctrl+C to stop.

### Check status

```bash
orch status --config config/personal.json
```

Shows all projects: enabled/paused state, pending tasks, completed count, total cost.

### List tasks

```bash
orch tasks --project my-app --config config/personal.json
orch tasks --project my-app --status pending --config config/personal.json
```

### Manually add a task

```bash
orch add-task --project my-app --title "Fix null check in parser" --desc "..." --type fix
```

### Pause / resume

```bash
orch pause --project my-app --config config/personal.json
orch resume --project my-app --config config/personal.json
```

## How the planner works

The planner doesn't just read your CLAUDE.md and spit out tasks. It:

1. **Explores the codebase** — globs for structure, reads key files, understands what actually exists on disk.
2. **Prioritizes** — fixes first, then unblocking infrastructure, then tests, then features.
3. **Writes a plan document** — a markdown file in `.orch/plans/` explaining what it found, what it's proposing, what it deferred, and what risks it sees.
4. **Scopes tasks for workers** — each task is one coherent change, completable in ~5 minutes with a $0.50 budget. If a backlog item is too big, it decomposes and only emits the first piece.
5. **Returns structured JSON** — parsed by the orchestrator, stored in SQLite, dispatched to workers.

The planner never re-emits a failed task as-is. It decomposes or fixes prerequisites first.

## Git isolation

Each task runs on its own branch:

```
main
├── orch/a1b2c3d4e5f6   ← worker A (task 1)
├── orch/f7g8h9i0j1k2   ← worker B (task 2, parallel)
└── orch/m3n4o5p6q7r8   ← worker C (task 3, parallel)
```

Workers never see each other's changes. After the judge passes a task, the refinery merges the branch back to main. If there are conflicts, it uses AI to resolve them.

## Cost controls

- `max_budget_per_task` caps each worker session (default $0.50)
- Planner uses a configurable model — use `sonnet` for routine planning, `opus` when you need deeper reasoning
- Judge uses `haiku` by default — cheap pass/fail evaluation
- Patrol auto-pauses projects with 5+ consecutive failures
- All costs are tracked in SQLite and in the `.orch/` audit trail

Typical cost per cycle (1 project, 2 tasks):
- Planner: ~$0.05-0.20
- 2 workers: ~$0.05-0.20 each
- 2 judge evals: ~$0.01 each

## State

SQLite (WAL mode) in the `data/` directory. One `.db` file per project. Supports concurrent readers — the CLI, orchestrator, and workers can all read state simultaneously.

Tables: `tasks`, `worker_results`, `judge_verdicts`, `planner_cycles`, `project_state`.

## Tests

```bash
source .venv/bin/activate
pytest tests/ -v
```

46 tests covering models, state store CRUD, config loading, prompt selection, and planner prompt building.

## Project status

This is a working system. The orchestration layer, state management, CLI, and prompt contracts are complete. It has been tested against real projects (Python and TypeScript/NestJS codebases).

What's next:
- GitHub Issues as a backlog source
- Retry logic with exponential backoff
- Cost dashboard in the CLI
- Multi-cycle planning (planner reads past cycle results)
