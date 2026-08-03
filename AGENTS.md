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

The typical development workflow is:

1. Select an issue from the GitHub Project.
2. Create a feature branch from `devel`.
3. Develop the feature on the feature branch, following the testing and verification requirements.
4. Open a pull request from the feature branch into `devel`.

Branch conventions:

- `main` always contains the stable version.
- `devel` contains the latest development version.
- Feature branches contain features currently under development and are intended to be merged into `devel`.
- Do not merge feature branches directly into `main`.

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

## Testing

- Use `pytest` for unit and integration tests.
- Use `httpx` with FastAPI's test client for API tests.
- Use `pytest-cov` for coverage reporting.
- Use `hypothesis` for property-based validation tests where generated inputs add value.
- Use `ruff` for linting and formatting.
- Use `mypy` for static type checking.
- Keep MILP test cases small and deterministic.
- Use `Pyomo` with `highspy` for optimization-model tests.
- Assert energy balances and relevant constraint behavior.
- Use numeric tolerances for floating-point values.
- Test solver status explicitly, including infeasible models.
- Keep tests independent of external APIs by mocking provider requests.
- Test Docker startup and the health endpoint once container packaging exists.

### Coverage

- Aim for at least 80% overall coverage.
- Aim for at least 90% coverage of domain and optimization logic.
- Aim for at least 90% coverage of API and validation code.
- Use coverage to identify untested behavior, not as the sole measure of quality.

## GitHub Actions

Maintain a CI workflow in `.github/workflows/ci.yml` that runs on every push and pull request.

The workflow should:

- Run on `ubuntu-latest`.
- Use the supported Python version matrix.
- Install project and development dependencies.
- Run `ruff check .`.
- Run `ruff format --check .`.
- Run `mypy .`.
- Run `pytest --cov --cov-report=term-missing`.
- Use `permissions: contents: read` unless a job requires more.
- Cache Python dependencies where practical.

Once a Dockerfile exists, add a Docker CI job that:

- Builds the Docker image.
- Starts the container.
- Calls the health endpoint.
- Reports container logs on failure.
- Removes the test container after completion.

CI must not require live external price or forecast services. Provider integrations should use mocked HTTP responses in ordinary tests. Live provider checks, if needed, belong in a separately controlled workflow.

Do not expose GitHub tokens to ordinary test jobs. Third-party Actions should be pinned or updated consistently.

## GitHub Actions Free Usage

- Standard GitHub-hosted runners are free for public repositories.
- This public repository does not consume the normal monthly Actions-minute allowance when using standard runners.
- Larger GitHub-hosted runners are billed even for public repositories.
- Self-hosted runners do not incur a GitHub-hosted runner-minute charge, but their infrastructure has operational costs.
- For private repositories on GitHub Free, the included allowance is 2,000 Actions minutes per month.
- GitHub Free also includes 500 MB of artifact storage and 10 GB of cache storage per repository.
- Free allowances reset at the start of each billing cycle.
- Artifact and cache storage have separate limits from runner minutes.
- Private-repository usage beyond the included allowance may be billed when a payment method is configured.
- Without a valid payment method, workflows are blocked after the included allowance is exhausted.
