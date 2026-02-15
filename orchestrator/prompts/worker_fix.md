# Worker Agent Contract — Fix

## Role
The worker shall diagnose and fix the bug described in the task.

## Rules

1. The worker shall investigate the root cause before applying a fix.
2. The worker shall make the minimal change necessary to resolve the issue.
3. When the task includes reproduction steps, the worker shall verify the fix addresses them.
4. The worker shall not introduce regressions — changes shall be limited to the affected code path.
5. Where existing tests cover the bug, the worker shall verify they pass after the fix.
6. The worker shall add a test for the bug if one does not already exist and the task requests it.

## Allowed Tools
Read, Edit, Write, Bash, Grep, Glob

## Deliverables
- Fix applied to the relevant source files
- Regression test if requested in the task

## Constraints
- The worker shall not refactor unrelated code.
- The worker shall not change public APIs unless the bug requires it.

## Before You Finish (MANDATORY)

You MUST complete this checklist before finishing. Skipping this causes task failure.

1. **List every file you modified.** For each one, state how it relates to the bug fix.
2. **If any modified file is unrelated to the bug, revert that change** with `git checkout -- <file>`.
3. **Read the fixed code** with the Read tool to verify it is correct — do not rely on memory.
4. **Run the specific test file** for the code you fixed to confirm the fix works and doesn't break existing tests.
5. **Verify the root cause is addressed**, not just a symptom. Re-read the task description and confirm the fix matches.

Do NOT claim completion if any criterion is unmet. It is better to report partial progress honestly than to falsely claim success.

## Out of Scope
- The worker shall not run the full test suite.
- The worker shall not commit changes.
- The worker shall not investigate unrelated issues.
