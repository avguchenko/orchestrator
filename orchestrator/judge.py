"""Judge: evaluates worker output via tests, lint, and optional SDK query()."""

from __future__ import annotations

import logging
import subprocess

from claude_agent_sdk import ClaudeAgentOptions, query

from .config import ProjectConfig
from .models import JudgeVerdict, Task, WorkerResult
from .prompts import load_prompt
from .state import StateStore

logger = logging.getLogger(__name__)

VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "passed": {"type": "boolean"},
        "reasoning": {"type": "string"},
        "issues": {
            "type": "array",
            "items": {"type": "string"},
        },
    },
    "required": ["passed", "reasoning"],
}


def _run_tests(cwd: str, command: str) -> tuple[bool, int, int, str]:
    """Run test command. Returns (passed, tests_passed, tests_failed, output)."""
    if not command:
        return True, 0, 0, "No test command configured"
    try:
        result = subprocess.run(
            command,
            shell=True,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=120,
        )
        output = result.stdout + result.stderr
        passed = result.returncode == 0
        # Parse pytest-style output for counts
        tests_passed = 0
        tests_failed = 0
        for line in output.splitlines():
            if "passed" in line and ("failed" in line or "error" in line):
                # e.g. "3 passed, 1 failed"
                parts = line.split(",")
                for p in parts:
                    p = p.strip()
                    if "passed" in p:
                        try:
                            tests_passed = int(p.split()[0])
                        except (ValueError, IndexError):
                            pass
                    if "failed" in p or "error" in p:
                        try:
                            tests_failed = int(p.split()[0])
                        except (ValueError, IndexError):
                            pass
            elif "passed" in line:
                try:
                    tests_passed = int(line.strip().split()[0])
                except (ValueError, IndexError):
                    pass
        return passed, tests_passed, tests_failed, output[:3000]
    except subprocess.TimeoutExpired:
        return False, 0, 0, "Test command timed out after 120s"
    except Exception as e:
        return False, 0, 0, f"Test execution error: {e}"


def _run_lint(cwd: str, command: str) -> tuple[bool, str]:
    """Run lint command. Returns (passed, output)."""
    if not command:
        return True, "No lint command configured"
    try:
        result = subprocess.run(
            command,
            shell=True,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=60,
        )
        output = result.stdout + result.stderr
        return result.returncode == 0, output[:2000]
    except subprocess.TimeoutExpired:
        return False, "Lint command timed out after 60s"
    except Exception as e:
        return False, f"Lint execution error: {e}"


async def _ai_evaluate(
    result: WorkerResult,
    project: ProjectConfig,
    test_output: str,
    lint_output: str,
) -> tuple[bool, str, float]:
    """When test results are ambiguous, the judge shall use an AI evaluation for nuanced assessment."""
    system = load_prompt("judge_system.md")

    # Ensure verdicts directory exists for the judge to write to
    verdicts_dir = project.abs_path / ".orch" / "verdicts"
    verdicts_dir.mkdir(parents=True, exist_ok=True)

    prompt = f"""# Worker Output Evaluation

## Task Output
{result.output[:2000]}

## Diff Stats
{result.diff_stat}
Files changed: {result.files_changed}

## Test Output
{test_output[:1500]}

## Lint Output
{lint_output[:500]}

Write your detailed analysis to `.orch/verdicts/{result.task_id}_analysis.md` — include what you inspected, issues found, and your reasoning. Then return the JSON verdict.
"""

    options = ClaudeAgentOptions(
        model=project.judge_model,
        cwd=str(project.abs_path),
        system_prompt=system,
        allowed_tools=["Read", "Write", "Grep", "Glob", "Bash"],
        permission_mode="bypassPermissions",
        output_format={"type": "json_schema", "schema": VERDICT_SCHEMA},
    )

    result_text = ""
    structured = None
    cost = 0.0
    async for msg in query(prompt=prompt, options=options):
        if hasattr(msg, "total_cost_usd") and msg.total_cost_usd is not None:
            cost = msg.total_cost_usd
        if hasattr(msg, "structured_output") and msg.structured_output is not None:
            structured = msg.structured_output
        if hasattr(msg, "result") and msg.result:
            result_text = msg.result

    try:
        import json
        if structured and isinstance(structured, dict):
            data = structured
        elif structured and isinstance(structured, str):
            data = json.loads(structured)
        else:
            data = json.loads(result_text)
        return data.get("passed", False), data.get("reasoning", ""), cost
    except Exception:
        return False, f"Judge parse error: {result_text[:200]}", cost


def _write_verdict_log(
    project: ProjectConfig,
    task_id: str,
    verdict: JudgeVerdict,
    test_output: str,
    lint_output: str,
) -> None:
    """Write judge verdict to .orch/verdicts/{task_id}.md for audit."""
    log_dir = project.abs_path / ".orch" / "verdicts"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{task_id}.md"
    log_path.write_text(
        f"# Judge Verdict: {task_id}\n\n"
        f"- **Passed**: {verdict.passed}\n"
        f"- **Tests**: {verdict.tests_passed} passed, {verdict.tests_failed} failed\n"
        f"- **Lint**: {'ok' if verdict.lint_ok else 'failed'}\n"
        f"- **Cost**: ${verdict.cost_usd:.4f}\n\n"
        f"## Notes\n{verdict.notes}\n\n"
        f"## Test Output\n```\n{test_output[:3000]}\n```\n\n"
        f"## Lint Output\n```\n{lint_output[:2000]}\n```\n"
    )


def _git_checkout(cwd: str, branch: str) -> bool:
    """Checkout a git branch. Returns True on success."""
    try:
        result = subprocess.run(
            ["git", "checkout", branch],
            cwd=cwd, capture_output=True, text=True, timeout=15,
        )
        return result.returncode == 0
    except Exception:
        return False


async def evaluate_result(
    task: Task,
    result: WorkerResult,
    project: ProjectConfig,
    store: StateStore,
) -> JudgeVerdict:
    """Evaluate a worker result through tests, lint, and optional AI review.

    The judge shall checkout the task branch before running checks.
    When all automated checks pass, the verdict shall be positive without
    AI evaluation. When automated checks fail or produce ambiguous results,
    the judge shall invoke AI evaluation.
    """
    cwd = str(project.abs_path)
    total_cost = 0.0

    # Checkout the task branch to test the worker's changes
    if not _git_checkout(cwd, task.branch):
        logger.warning("Could not checkout %s for judging, running on current branch", task.branch)

    try:
        # Run tests
        tests_passed_bool, tests_passed, tests_failed, test_output = _run_tests(
            cwd, project.test_command
        )

        # Run lint
        lint_ok, lint_output = _run_lint(cwd, project.lint_command)

        # When both tests and lint pass, the judge shall return a positive verdict
        if tests_passed_bool and lint_ok:
            verdict = JudgeVerdict(
                task_id=result.task_id,
                passed=True,
                tests_passed=tests_passed,
                tests_failed=tests_failed,
                lint_ok=lint_ok,
                notes="All automated checks passed",
            )
            _write_verdict_log(project, result.task_id, verdict, test_output, lint_output)
            return verdict

        # When automated checks fail, the judge shall use AI evaluation for nuance
        ai_passed, ai_notes, ai_cost = await _ai_evaluate(
            result, project, test_output, lint_output
        )
        total_cost += ai_cost

        verdict = JudgeVerdict(
            task_id=result.task_id,
            passed=ai_passed,
            tests_passed=tests_passed,
            tests_failed=tests_failed,
            lint_ok=lint_ok,
            notes=ai_notes,
            cost_usd=total_cost,
        )
        _write_verdict_log(project, result.task_id, verdict, test_output, lint_output)
        return verdict
    finally:
        # Return to default branch after judging
        _git_checkout(cwd, "main") or _git_checkout(cwd, "master")
