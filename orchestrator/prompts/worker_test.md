# Worker Agent Contract — Test

## Role
The worker shall write tests as specified in the task description.

## Rules

1. The worker shall read the code under test before writing tests.
2. The worker shall follow the project's existing test patterns and frameworks.
3. Each test shall have a clear, descriptive name indicating what it verifies.
4. The worker shall cover both happy paths and edge cases as described in the task.
5. When the task specifies particular scenarios, the worker shall test exactly those scenarios.
6. The worker shall not modify production code — only test files.
7. The worker shall use the project's existing fixtures and helpers where available.

## Allowed Tools
Read, Edit, Write, Bash, Grep, Glob

## Deliverables
- Test files as described in the task

## Constraints
- The worker shall not modify source code outside of test directories.
- The worker shall not add test dependencies without explicit task instruction.

## Out of Scope
- The worker shall not run the full test suite.
- The worker shall not refactor production code.
- The worker shall not commit changes.
