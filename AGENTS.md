# Agent Guidelines

## Project Language

- Use English for source code, documentation, commit messages, and GitHub issues.
- Prefer clear, explicit terminology over abbreviations.

## Product Backlog

- Maintain one ordered backlog.
- Backlog order defines priority.
- Do not use priority labels.
- Start with the smallest valuable increment.
- Prefer vertical slices that deliver a complete outcome.
- Avoid creating tickets that only represent implementation layers.
- Reorder or split backlog items when new information changes their value or dependencies.

## Issue Types

Use exactly three issue types:

### Feature

A feature delivers direct user value.

Each feature includes all required engineering work:

- Implementation
- Automated tests
- Documentation
- Configuration changes
- Deployment or infrastructure changes
- Error handling and observability where relevant

Do not create separate issues for these supporting activities unless they independently deliver value.

### Bug

A bug describes incorrect existing behavior.

A bug issue should include:

- Reproduction steps
- Actual behavior
- Expected behavior
- Relevant input, configuration, or environment
- Regression test requirements

### Task

A task covers work that is not directly visible to users but supports maintainability, quality, operations, or future development.

Examples include:

- Refactoring
- Dependency upgrades
- CI improvements
- Developer tooling
- Internal documentation
- Performance benchmarking
- Code quality improvements

Do not use tasks to extract mandatory work from a feature.

## Issue Format

Every issue should contain:

- A concise, outcome-oriented title
- The issue type
- User value or technical purpose
- Scope
- Acceptance criteria
- Dependencies
- Definition of Done

Feature issues should use:

```text
As a [user or system],
I want [capability],
so that [value].
```

Acceptance criteria must be observable and testable. Avoid vague criteria.

## Definition of Done

An issue is complete when:

- All acceptance criteria are met.
- Appropriate automated tests are added or updated.
- Relevant documentation is updated.
- Required configuration and deployment changes are included.
- Errors and important edge cases are handled explicitly.
- Verification commands have been run successfully.
- The implementation is understandable and maintainable.

## Labels and Milestones

- Use only issue-type labels:
  - `type: feature`
  - `type: bug`
  - `type: task`
- Do not add priority labels.
- Use milestones only to group coherent release increments.
- Do not use labels or milestones as a substitute for backlog ordering.

## Engineering Principles

- Prefer the smallest correct change.
- Keep changes focused and avoid unrelated refactoring.
- Do not add backward compatibility without a concrete requirement.
- Prefer explicit contracts and validation.
- Keep domain logic independent from transport, storage, and vendor-specific integrations.
- Make behavior deterministic and testable where possible.
- Add comments only when they explain non-obvious reasoning.
- Preserve existing user changes and do not overwrite unrelated work.

## Agent Workflow

Before changing code:

- Inspect the repository and existing conventions.
- Read relevant documentation and tests.
- Check the current working tree for unrelated changes.
- Identify dependencies and affected interfaces.

While implementing:

- Make the smallest coherent change.
- Update tests and documentation in the same change.
- Follow established project patterns.
- Avoid introducing unnecessary abstractions.

Before completing work:

- Run the most relevant automated tests.
- Run formatting, linting, and type checks when available.
- Review the final diff for unintended changes.
- Report what changed and which verification commands were run.
- Clearly state any remaining risks or unavailable checks.
