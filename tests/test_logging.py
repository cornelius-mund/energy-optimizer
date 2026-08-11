"""Tests for operational logging configuration and safe log context."""

import logging
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import TypeAdapter
from pytest import LogCaptureFixture, MonkeyPatch

from energy_optimizer.api import app
from energy_optimizer.config import (
    ConfigurationError,
    DataSourceScheduleConfiguration,
    HomeAssistantConfiguration,
    OrchestrationConfiguration,
    load_configuration,
)
from energy_optimizer.logging_config import (
    ConsistentFormatter,
    configure_logging,
    resolve_log_level,
)
from energy_optimizer.orchestration import ProviderOrchestrator, ProviderRegistration
from energy_optimizer.providers.home_assistant import HomeAssistantLoadImporter
from energy_optimizer.providers.interfaces import HouseholdLoadData, SourceMetadata
from energy_optimizer.storage import ProviderDataKey, ProviderDataStore

MINIMAL_CONFIGURATION = """
time_resolution_minutes: 60
grid:
  maximum_import_kw: 10
  maximum_export_kw: 10
solver:
  name: highs
  time_limit_seconds: 60
"""


def test_default_log_level_is_info(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.delenv("ENERGY_OPTIMIZER_LOG_LEVEL", raising=False)

    assert resolve_log_level() == logging.INFO


@pytest.mark.parametrize("level", ["DEBUG", "warning", "ERROR", "CRITICAL", "NOTSET"])
def test_supported_log_levels_are_configurable(level: str) -> None:
    assert resolve_log_level(level) == logging._nameToLevel[level.upper()]


def test_invalid_log_level_is_a_configuration_error() -> None:
    with pytest.raises(ConfigurationError, match="Invalid ENERGY_OPTIMIZER_LOG_LEVEL"):
        resolve_log_level("TRACE")


def test_log_formatter_starts_each_line_with_level_and_timestamp() -> None:
    formatter = ConsistentFormatter()
    record = logging.LogRecord(
        name="test",
        level=logging.WARNING,
        pathname=__file__,
        lineno=1,
        msg="first line\nsecond line",
        args=(),
        exc_info=None,
    )
    record.created = datetime(2026, 8, 11, 10, 42, 36, tzinfo=timezone.utc).timestamp()

    rendered = formatter.format(record)

    lines = rendered.splitlines()
    assert len(lines) == 2
    assert all(re.match(r"^WARNING 2026-08-11T10:42:36\+0000 ", line) for line in lines)
    assert lines[0].endswith("first line")
    assert lines[1].endswith("second line")


def test_exception_traceback_lines_use_the_same_log_prefix() -> None:
    formatter = ConsistentFormatter()
    try:
        raise RuntimeError("provider offline")
    except RuntimeError:
        record = logging.LogRecord(
            name="test",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="request failed",
            args=(),
            exc_info=sys.exc_info(),
        )

    rendered = formatter.format(record)

    assert all(
        re.match(r"^ERROR \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{4} ", line)
        for line in rendered.splitlines()
    )
    assert "RuntimeError: provider offline" in rendered


def test_third_party_loggers_use_one_handler_and_formatter(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging("INFO")
    uvicorn_logger = logging.getLogger("uvicorn.error")
    httpx_logger = logging.getLogger("httpx")

    uvicorn_logger.info("server ready")
    httpx_logger.info("HTTP Request: GET /health")

    output = capsys.readouterr().out.splitlines()
    assert len(output) == 2
    assert all(
        re.match(r"^INFO \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{4} ", line)
        for line in output
    )
    assert output[0].endswith("server ready")
    assert output[1].endswith("HTTP Request: GET /health")
    assert (
        len(
            [
                handler
                for handler in logging.getLogger().handlers
                if getattr(handler, "_energy_optimizer_handler", False)
            ]
        )
        == 1
    )


def test_configure_logging_is_idempotent() -> None:
    configure_logging("INFO")
    configure_logging("DEBUG")

    owned_handlers = [
        handler
        for handler in logging.getLogger().handlers
        if getattr(handler, "_energy_optimizer_handler", False)
    ]
    assert len(owned_handlers) == 1
    assert logging.getLogger().level == logging.DEBUG


def test_module_entrypoint_bootstraps_logging_before_starting_uvicorn(
    monkeypatch: MonkeyPatch,
) -> None:
    import energy_optimizer.__main__ as entrypoint

    calls: list[tuple[str, object]] = []

    def bootstrap() -> None:
        calls.append(("bootstrap", None))

    def run(application: str, **options: object) -> None:
        calls.append(("run", (application, options)))

    monkeypatch.setattr(
        entrypoint,
        "bootstrap_logging",
        bootstrap,
    )
    monkeypatch.setattr(
        "uvicorn.run",
        run,
    )

    entrypoint.main()

    assert calls == [
        ("bootstrap", None),
        (
            "run",
            (
                "energy_optimizer.api:app",
                {
                    "host": "0.0.0.0",
                    "port": 8000,
                    "log_config": None,
                    "access_log": False,
                },
            ),
        ),
    ]


def test_invalid_environment_log_level_fails_startup(
    tmp_path: Path, monkeypatch: MonkeyPatch, caplog: LogCaptureFixture
) -> None:
    configuration = tmp_path / "config.yaml"
    configuration.write_text(MINIMAL_CONFIGURATION, encoding="utf-8")
    monkeypatch.setenv("ENERGY_OPTIMIZER_CONFIG", str(configuration))
    monkeypatch.setenv("ENERGY_OPTIMIZER_LOG_LEVEL", "TRACE")

    with caplog.at_level(logging.CRITICAL, logger="energy_optimizer.api"):
        with pytest.raises(
            ConfigurationError, match="Invalid ENERGY_OPTIMIZER_LOG_LEVEL"
        ):
            with TestClient(app):
                pass

    assert any(
        record.levelno == logging.CRITICAL
        and record.getMessage().startswith("event=service_startup_failed")
        for record in caplog.records
    )


def test_configuration_load_is_logged_without_secret_values(
    tmp_path: Path, caplog: LogCaptureFixture
) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        MINIMAL_CONFIGURATION
        + "home_assistant:\n"
        + "  base_url: http://homeassistant.local:8123\n"
        + "  token: do-not-log-this-token\n"
        + "  household_load_entity_id: sensor.household_energy\n"
        + "  timeout_seconds: 10\n",
        encoding="utf-8",
    )
    configure_logging("DEBUG")

    with caplog.at_level(logging.DEBUG, logger="energy_optimizer.config"):
        load_configuration(path)

    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "configuration_loaded" in messages
    assert "do-not-log-this-token" not in messages


def test_api_request_log_contains_request_context_and_not_request_body(
    tmp_path: Path, monkeypatch: MonkeyPatch, caplog: LogCaptureFixture
) -> None:
    configuration = tmp_path / "config.yaml"
    configuration.write_text(MINIMAL_CONFIGURATION, encoding="utf-8")
    monkeypatch.setenv("ENERGY_OPTIMIZER_CONFIG", str(configuration))
    configure_logging("INFO")

    with caplog.at_level(logging.INFO, logger="energy_optimizer.api"):
        with TestClient(app) as client:
            response = client.get(
                "/health",
                headers={"X-Request-ID": "request-66"},
            )

    request_logs = [
        record
        for record in caplog.records
        if record.name == "energy_optimizer.api"
        and record.getMessage().startswith("event=request_completed")
    ]
    assert len(request_logs) == 1
    message = request_logs[0].getMessage()
    assert "method=GET" in message
    assert "path=/health" in message
    assert "status=200" in message
    assert "request_id=request-66" in message
    assert response.headers["X-Request-ID"] == "request-66"
    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "event=service_started" in messages
    assert "event=service_stopping" in messages
    assert "event=service_stopped" in messages


def test_api_failure_log_uses_error_level_without_payload(
    tmp_path: Path, monkeypatch: MonkeyPatch, caplog: LogCaptureFixture
) -> None:
    configuration = tmp_path / "config.yaml"
    configuration.write_text(MINIMAL_CONFIGURATION, encoding="utf-8")
    monkeypatch.setenv("ENERGY_OPTIMIZER_CONFIG", str(configuration))
    configure_logging("INFO")

    with caplog.at_level(logging.INFO, logger="energy_optimizer.api"):
        with TestClient(app) as client:
            response = client.post(
                "/optimize",
                json={"secret_payload": "do-not-log-this", "invalid": True},
            )

    assert response.status_code == 422
    request_logs = [
        record
        for record in caplog.records
        if record.name == "energy_optimizer.api"
        and record.getMessage().startswith("event=request_completed")
    ]
    assert len(request_logs) == 1
    assert request_logs[0].levelno == logging.WARNING
    assert "do-not-log-this" not in request_logs[0].getMessage()


def test_provider_failure_log_excludes_token_and_raw_state(
    caplog: LogCaptureFixture,
) -> None:
    configuration = HomeAssistantConfiguration.model_validate(
        {
            "base_url": "http://homeassistant.test:8123",
            "token": "secret-provider-token",
            "household_load_entity_id": "sensor.household_energy",
            "timeout_seconds": 5,
        }
    )
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                json=[
                    [
                        {
                            "state": "unavailable",
                            "last_changed": "2026-01-01T00:00:00+00:00",
                        }
                    ]
                ],
            )
        )
    )
    provider = HomeAssistantLoadImporter(configuration, client)
    configure_logging("DEBUG")

    try:
        with caplog.at_level(logging.DEBUG, logger="energy_optimizer.providers"):
            with pytest.raises(RuntimeError, match="unavailable"):
                provider.fetch(
                    datetime(2026, 1, 1, tzinfo=timezone.utc),
                    datetime(2026, 1, 1, 1, tzinfo=timezone.utc),
                    now=datetime(2026, 1, 1, 2, tzinfo=timezone.utc),
                )
    finally:
        client.close()

    messages = "\n".join(record.getMessage() for record in caplog.records)
    provider_failures = [
        record
        for record in caplog.records
        if record.getMessage().startswith("event=provider_fetch_failed")
    ]
    assert len(provider_failures) == 1
    assert provider_failures[0].levelno == logging.ERROR
    assert any(
        record.levelno == logging.WARNING and "unavailable" in record.getMessage()
        for record in caplog.records
    )
    assert "secret-provider-token" not in messages
    assert "unavailable" in messages


def test_persistence_log_reports_counts_without_logging_series(
    tmp_path: Path, caplog: LogCaptureFixture
) -> None:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    data = HouseholdLoadData(
        schema_version="1",
        start_time=start,
        interval_minutes=60,
        load_kw=(1.25, 2.5, 3.75),
        unit="kW",
        source=SourceMetadata(provider="test-provider", entity_id="test-load"),
        retrieved_at=start,
        latest_observation_at=start,
    )
    configure_logging("DEBUG")

    with caplog.at_level(logging.DEBUG, logger="energy_optimizer.storage"):
        ProviderDataStore(tmp_path).save(
            ProviderDataKey("household-load", "test-provider", "test-load"),
            TypeAdapter(HouseholdLoadData),
            data,
        )

    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "event=persistence_succeeded" in messages
    assert "record_count=3" in messages
    assert "1.25" not in messages
    assert "2.5" not in messages
    assert "3.75" not in messages


def test_orchestration_failure_log_is_error_with_source_context(
    tmp_path: Path, caplog: LogCaptureFixture
) -> None:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def fetch(_: datetime, __: DataSourceScheduleConfiguration) -> object:
        raise RuntimeError("provider offline")

    registration = ProviderRegistration(
        name="household_load",
        data_type="household-load",
        adapter=TypeAdapter(HouseholdLoadData),
        fetch=fetch,
        is_fresh=lambda _data, _now: True,
    )
    configuration = OrchestrationConfiguration(
        enabled=True,
        sources={
            "household_load": DataSourceScheduleConfiguration(interval_seconds=60)
        },
    )
    configure_logging("DEBUG")

    with caplog.at_level(logging.DEBUG, logger="energy_optimizer.orchestration"):
        cycle = ProviderOrchestrator(
            configuration,
            [registration],
            ProviderDataStore(tmp_path),
        ).run_due(start)

    assert cycle.provider_runs[0].status == "failed"
    failures = [
        record
        for record in caplog.records
        if record.getMessage().startswith("event=provider_refresh_failed")
    ]
    assert len(failures) == 1
    assert failures[0].levelno == logging.ERROR
    assert "source=household_load" in failures[0].getMessage()
    assert "provider offline" in failures[0].getMessage()
    assert failures[0].exc_info is not None
