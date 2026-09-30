"""Contract tests for developer tooling scripts and their CI wiring."""

import os
import re
import shutil
import subprocess
import sys
from functools import partial
from pathlib import Path
from typing import Any

import pytest
import yaml

REPOSITORY = Path(__file__).parents[1]
VERIFY = REPOSITORY / "scripts" / "verify"
PREFLIGHT = REPOSITORY / "scripts" / "preflight"
WORKFLOW = REPOSITORY / ".github" / "workflows" / "ci.yml"
DOCKERFILE = REPOSITORY / "Dockerfile"
LINTER_CONFIGURATION = (".yamllint", ".hadolint.yaml", ".editorconfig")
LINT_STEPS = ("workflows", "dockerfile", "yaml", "shell")
LINTERS = ("actionlint", "hadolint", "yamllint", "shfmt", "shellcheck")

CLEAN_FILES = {
    ".github/workflows/ci.yml": (
        "name: CI\n\n"
        "on: push\n\n"
        "jobs:\n"
        "  build:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - run: echo hello\n"
    ),
    "Dockerfile": (
        'FROM python:3.14-slim\n\nUSER 10001\n\nCMD ["python", "--version"]\n'
    ),
    "settings.yaml": "enabled: true\n",
    "scripts/example": (
        "#!/usr/bin/env bash\n"
        "set -eu\n\n"
        'case "${1:-}" in\n'
        "    start) printf 'starting\\n' ;;\n"
        "    *) printf 'unknown\\n' ;;\n"
        "esac\n"
    ),
}

_run = partial(subprocess.run, capture_output=True, text=True, check=False)


def _verify(*arguments: str) -> subprocess.CompletedProcess[str]:
    return _run([str(VERIFY), *arguments], timeout=30)


def _verify_in_scratch_repository(
    tmp_path: Path, files: dict[str, str], *steps: str
) -> subprocess.CompletedProcess[str]:
    """Run scripts/verify on a scratch git repository holding the given files.

    The script and the checked-in linter configuration are copied so the run
    exercises the real step commands against inputs the test controls.
    """
    root = tmp_path / "repository"
    (root / "scripts").mkdir(parents=True)
    shutil.copy(VERIFY, root / "scripts" / "verify")
    for name in LINTER_CONFIGURATION:
        shutil.copy(REPOSITORY / name, root / name)
    subprocess.run(["git", "init", "--quiet"], cwd=root, check=True)
    for relative_path, content in {**CLEAN_FILES, **files}.items():
        target = root / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    # Run the linters of the interpreter's environment, not ambient copies.
    environment = os.environ.copy()
    interpreter_directory = str(Path(sys.executable).parent)
    environment["PATH"] = os.pathsep.join([interpreter_directory, environment["PATH"]])
    return _run(
        [str(root / "scripts" / "verify"), *steps],
        cwd=root,
        env=environment,
        timeout=60,
    )


def _preflight_with_linters(
    tmp_path: Path, installed: tuple[str, ...], *arguments: str
) -> dict[str, tuple[str, str]]:
    """Run scripts/preflight on a scratch repository and parse its result lines.

    Only the named linters exist in the scratch virtual environment, and gh is a
    stub that fails at once so no test performs a GitHub request.
    """
    root = tmp_path / "repository"
    (root / "scripts").mkdir(parents=True)
    shutil.copy(PREFLIGHT, root / "scripts" / "preflight")
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    (stubs / "gh").write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    (stubs / "gh").chmod(0o755)
    linter_directory = root / ".venv" / "bin"
    linter_directory.mkdir(parents=True)
    for name in installed:
        (linter_directory / name).write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        (linter_directory / name).chmod(0o755)

    result = _run(
        [str(root / "scripts" / "preflight"), *arguments],
        cwd=root,
        env={"PATH": f"{stubs}:/usr/bin:/bin"},
        timeout=60,
    )
    lines: dict[str, tuple[str, str]] = {}
    for line in result.stdout.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3 and parts[0] in {"PASS", "WARN", "FAIL"}:
            lines[parts[1]] = (parts[0], parts[2])
    return lines


def _repository_files() -> list[Path]:
    listing = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=REPOSITORY,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [
        REPOSITORY / name
        for name in listing.splitlines()
        if (REPOSITORY / name).is_file()
    ]


def _is_shell_script(path: Path) -> bool:
    if path.suffix in {".sh", ".bash"}:
        return True
    first_line = path.read_bytes().split(b"\n", 1)[0]
    return bool(re.match(rb"#!\s*(/usr/bin/env\s+)?\S*(ba|da|z|k)?sh\b", first_line))


def _workflow_jobs() -> dict[str, Any]:
    jobs: dict[str, Any] = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]
    return jobs


def test_verify_lists_every_step() -> None:
    result = _verify("--list")

    assert result.returncode == 0
    assert result.stdout.split() == [
        "lint",
        "format",
        "types",
        "openapi",
        "unit",
        "workflows",
        "dockerfile",
        "yaml",
        "shell",
        "e2e",
    ]


def test_verify_rejects_unknown_steps_and_options() -> None:
    for argument, message in (
        ("bogus", "Unknown step: bogus"),
        ("--bogus", "Unknown option: --bogus"),
    ):
        result = _verify(argument)

        assert result.returncode == 2
        assert message in result.stderr


def test_ci_runs_every_verification_step_through_the_verify_script() -> None:
    listed = set(_verify("--list").stdout.split())
    commands = [
        step.get("run", "")
        for job in _workflow_jobs().values()
        for step in job["steps"]
    ]

    assert {
        command.removeprefix("scripts/verify ")
        for command in commands
        if command.startswith("scripts/verify ")
    } == listed


def test_e2e_job_runs_in_parallel_with_the_unit_job() -> None:
    assert "needs" not in _workflow_jobs()["e2e"]


def test_e2e_job_caches_playwright_browsers_before_installing_them() -> None:
    steps = _workflow_jobs()["e2e"]["steps"]
    uses = [step.get("uses", "") for step in steps]
    runs = [step.get("run", "") for step in steps]
    cache_index = next(
        i for i, use in enumerate(uses) if use.startswith("actions/cache@")
    )
    install_index = next(i for i, run in enumerate(runs) if "playwright install" in run)

    assert steps[cache_index]["with"]["path"] == "~/.cache/ms-playwright"
    assert "playwright-version" in steps[cache_index]["with"]["key"]
    assert cache_index < install_index


def test_dockerfile_installs_dependencies_before_copying_source() -> None:
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")

    dependency_install = dockerfile.index("--requirement /tmp/requirements.txt")
    source_copy = dockerfile.index("COPY src ./src")

    assert dependency_install < source_copy
    assert "pip install --no-cache-dir --no-deps ." in dockerfile


def test_default_steps_include_the_file_linters() -> None:
    match = re.search(r"^default_steps=\((.*)\)$", VERIFY.read_text(), re.MULTILINE)

    assert match is not None
    assert set(LINT_STEPS) <= set(match.group(1).split())


def test_lint_job_runs_every_file_lint_step_in_ci() -> None:
    lint_jobs = [
        job
        for job in _workflow_jobs().values()
        if {"scripts/verify " + step for step in LINT_STEPS}
        <= {step.get("run") for step in job["steps"]}
    ]

    assert len(lint_jobs) == 1
    assert "needs" not in lint_jobs[0]


def test_lint_steps_pass_on_a_clean_scratch_repository(tmp_path: Path) -> None:
    result = _verify_in_scratch_repository(tmp_path, {}, *LINT_STEPS)

    assert result.returncode == 0, result.stdout + result.stderr
    for step in LINT_STEPS:
        assert f"PASS  {step}" in result.stdout


@pytest.mark.parametrize(
    ("step", "files", "findings"),
    [
        pytest.param(
            "workflows",
            {
                ".github/workflows/ci.yml": (
                    "on: push\n"
                    "jobs:\n"
                    "  build:\n"
                    "    runs-on: ubuntu-latest\n"
                    "    steps:\n"
                    "      - run: echo ${{ github.evnt_name }}\n"
                )
            },
            ("evnt_name",),
            id="actionlint",
        ),
        pytest.param(
            "dockerfile",
            {"Dockerfile": 'FROM python:latest\n\nCMD ["python"]\n'},
            ("DL3007",),
            id="hadolint",
        ),
        pytest.param(
            "yaml",
            {"settings.yaml": "enabled: true\nenabled: false\n"},
            ("duplication of key",),
            id="yamllint-error",
        ),
        pytest.param(
            "yaml",
            {"settings.yaml": "enabled: yes\n"},
            ("truthy value should be one of",),
            id="yamllint-warning-fails-under-strict",
        ),
        pytest.param(
            "shell",
            {"scripts/example": "#!/usr/bin/env bash\nprintf '%s\\n' $1\n"},
            ("SC2086",),
            id="shellcheck",
        ),
        pytest.param(
            "shell",
            {
                "scripts/example": (
                    "#!/usr/bin/env bash\n"
                    'if [ -n "${1:-}" ]; then\n'
                    "  printf '%s\\n' \"$1\"\n"
                    "fi\n"
                )
            },
            ("+++ scripts/example",),
            id="shfmt",
        ),
        pytest.param(
            "shell",
            {
                "scripts/example": (
                    "#!/usr/bin/env bash\nif true; then\n  printf '%s\\n' $1\nfi\n"
                )
            },
            ("SC2086", "+++ scripts/example"),
            id="shellcheck-and-shfmt-together",
        ),
        pytest.param(
            "yaml",
            {"nested/new.yml": "a: 1\na: 2\n"},
            ("nested/new.yml",),
            id="file-not-yet-tracked-by-git",
        ),
    ],
)
def test_lint_step_fails_on_broken_input(
    tmp_path: Path, step: str, files: dict[str, str], findings: tuple[str, ...]
) -> None:
    result = _verify_in_scratch_repository(tmp_path, files, step)

    assert result.returncode == 1, result.stdout + result.stderr
    for finding in findings:
        assert finding in result.stdout
    assert f"FAIL  {step}" in result.stdout


@pytest.mark.parametrize("missing", LINTERS)
def test_preflight_names_a_missing_linter_and_how_to_install_it(
    tmp_path: Path, missing: str
) -> None:
    installed = tuple(name for name in LINTERS if name != missing)

    normal = _preflight_with_linters(tmp_path / "normal", installed)
    strict = _preflight_with_linters(tmp_path / "strict", installed, "--strict")

    assert normal[missing][0] == "WARN"
    assert strict[missing][0] == "FAIL"
    for message in (normal[missing][1], strict[missing][1]):
        assert f"{missing} is not installed" in message
        assert "scripts/bootstrap" in message
    for name in installed:
        assert normal[name][0] == "PASS"
        assert strict[name][0] == "PASS"


def test_shell_scripts_live_only_in_the_scripts_directory() -> None:
    files = _repository_files()
    shell_scripts = {path for path in files if _is_shell_script(path)}
    scripts_directory_files = {
        path for path in files if path.parent == REPOSITORY / "scripts"
    }

    assert shell_scripts == scripts_directory_files


def test_every_lint_suppression_states_its_reason() -> None:
    directive = re.compile(
        r"#\s*(shellcheck disable=|hadolint ignore=|yamllint disable)"
    )
    unexplained: list[str] = []
    for path in _repository_files():
        if path.name == "test_tooling.py" or not (
            _is_shell_script(path)
            or path.name == "Dockerfile"
            or path.suffix in {".yml", ".yaml"}
        ):
            continue
        lines = path.read_text(encoding="utf-8").splitlines()
        for number, line in enumerate(lines):
            if directive.search(line):
                above = lines[number - 1].strip() if number else ""
                if not above.startswith("#") or directive.search(above):
                    unexplained.append(f"{path.relative_to(REPOSITORY)}:{number + 1}")

    assert unexplained == []


def test_every_yamllint_rule_override_states_its_reason() -> None:
    lines = (REPOSITORY / ".yamllint").read_text(encoding="utf-8").splitlines()
    rules = [
        number for number, line in enumerate(lines) if re.match(r"^  [a-z-]+:", line)
    ]

    assert rules
    for number in rules:
        assert lines[number - 1].lstrip().startswith("#"), lines[number]


def test_hadolint_configuration_ignores_no_rule_without_a_reason() -> None:
    lines = (REPOSITORY / ".hadolint.yaml").read_text(encoding="utf-8").splitlines()
    entries = [
        number for number, line in enumerate(lines) if line and line[0] not in "# "
    ]

    assert entries
    for number in entries:
        assert lines[number - 1].startswith("#"), lines[number]
