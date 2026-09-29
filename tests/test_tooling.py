"""Contract tests for developer tooling scripts and their CI wiring."""

import subprocess
from pathlib import Path
from typing import Any

import yaml

REPOSITORY = Path(__file__).parents[1]
VERIFY = REPOSITORY / "scripts" / "verify"
WORKFLOW = REPOSITORY / ".github" / "workflows" / "ci.yml"
DOCKERFILE = REPOSITORY / "Dockerfile"


def _verify(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(VERIFY), *arguments],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


def _workflow_jobs() -> dict[str, Any]:
    jobs: dict[str, Any] = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]
    return jobs


def _verify_steps_called_by_ci() -> set[str]:
    called: set[str] = set()
    for job in _workflow_jobs().values():
        for step in job["steps"]:
            command = step.get("run", "")
            if command.startswith("scripts/verify "):
                called.add(command.removeprefix("scripts/verify "))
    return called


def test_verify_lists_every_step() -> None:
    result = _verify("--list")

    assert result.returncode == 0
    assert result.stdout.split() == [
        "lint",
        "format",
        "types",
        "openapi",
        "unit",
        "e2e",
    ]


def test_verify_rejects_unknown_steps_and_options() -> None:
    unknown_step = _verify("bogus")
    unknown_option = _verify("--bogus")

    assert unknown_step.returncode == 2
    assert "Unknown step: bogus" in unknown_step.stderr
    assert unknown_option.returncode == 2
    assert "Unknown option: --bogus" in unknown_option.stderr


def test_ci_runs_every_verification_step_through_the_verify_script() -> None:
    listed = set(_verify("--list").stdout.split())

    assert _verify_steps_called_by_ci() == listed


def test_e2e_job_runs_in_parallel_with_the_unit_job() -> None:
    assert "needs" not in _workflow_jobs()["e2e"]


def test_e2e_job_caches_playwright_browsers_before_installing_them() -> None:
    steps = _workflow_jobs()["e2e"]["steps"]
    cache_index = next(
        index
        for index, step in enumerate(steps)
        if step.get("uses", "").startswith("actions/cache@")
    )
    install_index = next(
        index
        for index, step in enumerate(steps)
        if "playwright install" in step.get("run", "")
    )

    assert steps[cache_index]["with"]["path"] == "~/.cache/ms-playwright"
    assert "playwright-version" in steps[cache_index]["with"]["key"]
    assert cache_index < install_index


def test_dockerfile_installs_dependencies_before_copying_source() -> None:
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")

    dependency_install = dockerfile.index("--requirement /tmp/requirements.txt")
    source_copy = dockerfile.index("COPY src ./src")

    assert dependency_install < source_copy
    assert "pip install --no-cache-dir --no-deps ." in dockerfile
