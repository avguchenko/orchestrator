"""Judge: evaluates worker output via tests, lint, and AI code review."""

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


def _count_lint_warnings(output: str) -> int:
    """Count warning lines in lint output. Works with ESLint-style output."""
    count = 0
    for line in output.splitlines():
        if "warning" in line.lower() and (
            "warning " in line or "warning\t" in line or "warnings" not in line.lower()
        ):
            count += 1
    return count


def _get_changed_files(cwd: str, branch: str) -> str:
    """Get list of files changed by the worker on their branch vs main."""
    # Try diffing against main/master to get the worker's changes
    for base in ("main", "master"):
        try:
            result = subprocess.run(
                ["git", "diff", "--name-only", f"{base}...{branch}"],
                cwd=cwd, capture_output=True, text=True, timeout=15,
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
        except Exception:
            continue
    # Fallback: diff HEAD~1
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", "HEAD~1"],
            cwd=cwd, capture_output=True, text=True, timeout=15,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return "(unable to determine changed files)"


def _check_scope(task: Task, changed_files: str) -> list[str]:
    """Check if the worker's changes are within the expected scope.

    Returns a list of scope violation warnings (empty if clean).
    """
    warnings = []
    if not changed_files or changed_files == "(unable to determine changed files)":
        return warnings

    files = changed_files.strip().splitlines()
    task_lower = (task.title + " " + task.description).lower()

    # Test tasks should not modify production code
    if task.task_type.value == "test":
        non_test_files = [
            f for f in files
            if not any(pattern in f for pattern in [
                "spec.", ".test.", "_test.", "test_", "/tests/", "/__tests__/",
            ])
        ]
        if non_test_files:
            warnings.append(
                f"SCOPE VIOLATION: Test task modified {len(non_test_files)} "
                f"non-test files: {', '.join(non_test_files)}"
            )

    # Flag when too many files changed (likely out-of-scope)
    if len(files) > 15:
        warnings.append(
            f"SCOPE WARNING: Worker changed {len(files)} files — "
            f"this seems excessive for a single task."
        )

    # Flag when worker changed files in many different directories
    dirs = set(f.rsplit("/", 1)[0] if "/" in f else "." for f in files)
    if len(dirs) > 5:
        warnings.append(
            f"SCOPE WARNING: Changes span {len(dirs)} directories — "
            f"expected focused changes in 1-3 directories."
        )

    return warnings


async def _ai_evaluate(
    task: Task,
    result: WorkerResult,
    project: ProjectConfig,
    test_output: str,
    lint_output: str,
    tests_passed_bool: bool,
    lint_ok: bool,
    changed_files: str,
    scope_warnings: list[str] | None = None,
    lint_delta: int = 0,
    baseline_warnings: int = 0,
    branch_warnings: int = 0,
) -> tuple[bool, str, float]:
    """The judge shall always use AI evaluation for every task."""
    system = load_prompt("judge_system.md")

    # Ensure verdicts directory exists for the judge to write to
    verdicts_dir = project.abs_path / ".orch" / "verdicts"
    verdicts_dir.mkdir(parents=True, exist_ok=True)

    # Parse test counts from output for the prompt
    tests_passed_count = 0
    tests_failed_count = 0
    for line in test_output.splitlines():
        if "passed" in line:
            parts = line.split(",")
            for p in parts:
                p = p.strip()
                if "passed" in p:
                    try:
                        tests_passed_count = int(p.split()[0])
                    except (ValueError, IndexError):
                        pass
                if "failed" in p or "error" in p:
                    try:
                        tests_failed_count = int(p.split()[0])
                    except (ValueError, IndexError):
                        pass

    scope_section = ""
    if scope_warnings:
        scope_section = "\n## Scope Violations (Automated)\n" + "\n".join(f"- {w}" for w in scope_warnings) + "\n"

    prompt = f"""# Worker Output Evaluation

## Task
Title: {task.title}
Description: {task.description}
Type: {task.task_type.value}

## Automated Checks
Tests: {"PASSED (exit code 0)" if tests_passed_bool else "FAILED (exit code non-zero — determine if failures are pre-existing or caused by worker)"} ({tests_passed_count} passed, {tests_failed_count} failed)
Lint: {baseline_warnings} warnings on main → {branch_warnings} on branch (delta: {lint_delta:+d}){" ✓ improved or unchanged" if lint_delta <= 0 else " ⚠ worker introduced new warnings"}

## Files Changed by Worker
{changed_files}
{scope_section}
## Worker Output
{result.output[:2000]}

## Diff Stats
{result.diff_stat}
Files changed: {result.files_changed}

## Test Output
{test_output[:1500]}

## Lint Output
{lint_output[:500]}

Evaluate the worker's changes. Pay special attention to:
- Whether new test files appear in the test output (were they discovered and executed?)
- Whether the changes match the task description
- Code quality and correctness of the actual diff

**Verdict criteria**: Set "passed" to true when the worker's code correctly implements the task and doesn't introduce new problems. Pre-existing test failures and lint warnings do NOT block approval. Only fail for: new test failures caused by the worker, incorrect/incomplete implementation, scope violations, or a significant increase in lint warnings (delta > 0 means the worker added warnings).

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
    lint_delta: int = 0,
    baseline_warnings: int = 0,
    branch_warnings: int = 0,
) -> None:
    """Write judge verdict to .orch/verdicts/{task_id}.md for audit."""
    log_dir = project.abs_path / ".orch" / "verdicts"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{task_id}.md"
    log_path.write_text(
        f"# Judge Verdict: {task_id}\n\n"
        f"- **Passed**: {verdict.passed}\n"
        f"- **Tests**: {verdict.tests_passed} passed, {verdict.tests_failed} failed\n"
        f"- **Lint**: {baseline_warnings} → {branch_warnings} warnings (delta: {lint_delta:+d})\n"
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
    """Evaluate a worker result through tests, lint, and AI review.

    The judge shall checkout the task branch before running checks.
    AI evaluation always runs — automated checks inform but don't replace review.
    """
    cwd = str(project.abs_path)
    total_cost = 0.0

    # Run lint on main first to get baseline warning count
    baseline_lint_ok, baseline_lint_output = _run_lint(cwd, project.lint_command)
    baseline_warnings = _count_lint_warnings(baseline_lint_output)

    # Checkout the task branch to test the worker's changes
    if not _git_checkout(cwd, task.branch):
        logger.warning("Could not checkout %s for judging, running on current branch", task.branch)

    try:
        # Run tests
        tests_passed_bool, tests_passed, tests_failed, test_output = _run_tests(
            cwd, project.test_command
        )

        # Run lint on the worker's branch
        lint_ok, lint_output = _run_lint(cwd, project.lint_command)
        branch_warnings = _count_lint_warnings(lint_output)
        lint_delta = branch_warnings - baseline_warnings

        # Get list of files the worker changed for diff-aware evaluation
        changed_files = _get_changed_files(cwd, task.branch)

        # Automated scope check — flag out-of-scope changes
        scope_warnings = _check_scope(task, changed_files)
        if scope_warnings:
            for w in scope_warnings:
                logger.warning("Task %s: %s", task.id, w)

        # Always run AI evaluation — automated checks inform but don't replace review
        ai_passed, ai_notes, ai_cost = await _ai_evaluate(
            task, result, project, test_output, lint_output,
            tests_passed_bool, lint_ok, changed_files,
            scope_warnings, lint_delta, baseline_warnings, branch_warnings,
        )
        total_cost += ai_cost

        # The AI judge is the sole arbiter of pass/fail.
        # It receives test output, lint output, changed files, and scope
        # warnings — and decides whether failures are pre-existing or
        # introduced by the worker.
        passed = ai_passed

        verdict = JudgeVerdict(
            task_id=result.task_id,
            passed=passed,
            tests_passed=tests_passed,
            tests_failed=tests_failed,
            lint_ok=lint_ok,
            notes=ai_notes,
            cost_usd=total_cost,
        )
        _write_verdict_log(
            project, result.task_id, verdict, test_output, lint_output,
            lint_delta, baseline_warnings, branch_warnings,
        )
        return verdict
    finally:
        # Return to default branch after judging
        _git_checkout(cwd, "main") or _git_checkout(cwd, "master")
