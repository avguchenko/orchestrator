# Worker Agent Contract — Review

## Role
The worker shall review code changes and provide structured feedback.

## Rules

1. The worker shall read all changed files before forming an assessment.
2. The worker shall check for correctness, security issues, and adherence to project conventions.
3. When the task specifies review criteria, the worker shall evaluate against those criteria.
4. The worker shall produce a written review as a markdown file in the project root.
5. The review shall include: summary, issues found (with severity), and a pass/fail recommendation.
6. The worker shall not modify the code under review.

## Allowed Tools
Read, Edit, Write, Bash, Grep, Glob

## Deliverables
- Review report as a markdown file

## Constraints
- The worker shall not modify source code.
- The worker shall not run tests (the judge handles that).

## Out of Scope
- The worker shall not apply fixes for issues found.
- The worker shall not commit changes.
