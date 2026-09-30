"""Tests for operational logging configuration and safe log context."""

import asyncio
import logging
import re
import sys
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import TypeAdapter
from pytest import LogCaptureFixture, MonkeyPatch

from energy_optimizer.api import app
from energy_optimizer.config import (
    Configuration,
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
from energy_optimizer.orchestration import (
    ProviderOrchestrator,
    ProviderRegistration,
    build_configured_orchestrator,
)
from energy_optimizer.providers.home_assistant import HomeAssistantLoadImporter
from energy_optimizer.providers.home_assistant_energy import EnergyAggregate
from energy_optimizer.providers.home_assistant_grid_flow import (
    HomeAssistantGridFlowImporter,
)
from energy_optimizer.providers.home_assistant_history import (
    HistoryPlan,
    HomeAssistantError,
)
from energy_optimizer.providers.interfaces import HouseholdLoadData, SourceMetadata
from energy_optimizer.storage import ProviderDataKey, ProviderDataStore
from home_assistant_fixtures import (
    HistoryPlanningImporter,
    aggregate_settings,
    home_assistant_history_payload,
    import_and_build,
)

START = datetime(2026, 1, 1, tzinfo=timezone.utc)
END = datetime(2026, 1, 1, 1, tzinfo=timezone.utc)
NOW = datetime(2026, 1, 1, 2, tzinfo=timezone.utc)
API_LOGGER = "energy_optimizer.api"
PROVIDERS_LOGGER = "energy_optimizer.providers"
TIMESTAMP = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{4}"
DURATION = r"duration_seconds=\d+\.\d{3}\b"
FORECAST_PARAMS = {
    "scenario_kind": "forecast",
    "start_time": "2026-01-01T00:00:00+00:00",
    "end_time": "2026-01-01T01:00:00+00:00",
}


def log_record(
    message: str,
    *,
    name: str = "test",
    level: int = logging.INFO,
    exc_info: Any = None,
) -> logging.LogRecord:
    return logging.LogRecord(
        name=name,
        level=level,
        pathname=__file__,
        lineno=1,
        msg=message,
        args=(),
        exc_info=exc_info,
    )


def format_at_fixed_time(
    message: str, *, name: str = "test", level: int = logging.INFO
) -> str:
    record = log_record(message, name=name, level=level)
    record.created = datetime(2026, 8, 11, 10, 42, 36, tzinfo=timezone.utc).timestamp()
    return ConsistentFormatter().format(record)


def owned_handlers() -> list[logging.Handler]:
    return [
        handler
        for handler in logging.getLogger().handlers
        if getattr(handler, "_energy_optimizer_handler", False)
    ]


def event_records(
    caplog: LogCaptureFixture, event: str, logger: str | None = None, text: str = ""
) -> list[logging.LogRecord]:
    return [
        record
        for record in caplog.records
        if record.getMessage().startswith(f"event={event}")
        and (logger is None or record.name == logger)
        and text in record.getMessage()
    ]


def single_event(
    caplog: LogCaptureFixture, event: str, logger: str | None = None, text: str = ""
) -> logging.LogRecord:
    records = event_records(caplog, event, logger, text)
    assert len(records) == 1
    return records[0]


def event_names(caplog: LogCaptureFixture) -> list[str]:
    return [
        message.split(" ", 1)[0].removeprefix("event=")
        for message in caplog.messages
        if message.startswith("event=")
    ]


@contextmanager
def capture_logs(caplog: LogCaptureFixture, level: int, logger: str) -> Iterator[None]:
    configure_logging(logging.getLevelName(level))
    with caplog.at_level(level, logger=logger):
        yield


def request_app(
    caplog: LogCaptureFixture, level: int, method: str, url: str, **options: Any
) -> httpx.Response:
    """Send one request to a started application while capturing its API logs."""
    with capture_logs(caplog, level, API_LOGGER), TestClient(app) as client:
        response: httpx.Response = client.request(method, url, **options)
    return response


def append_yaml(path: Path, text: str) -> None:
    with path.open("a", encoding="utf-8") as configuration_file:
        configuration_file.write(text)


def household_load_data(*load_kw: float) -> HouseholdLoadData:
    return HouseholdLoadData(
        schema_version="1",
        start_time=START,
        interval_minutes=60,
        load_kw=load_kw,
        unit="kW",
        source=SourceMetadata(provider="test-provider", entity_id="test-load"),
        retrieved_at=START,
        latest_observation_at=START,
    )


def home_assistant_settings(token: str, **entities: str) -> dict[str, Any]:
    """Settings whose energy aggregates each add up one ``sensor.<entity>``."""
    return {
        "base_url": "http://homeassistant.test:8123",
        "token": token,
        **{
            aggregate: aggregate_settings(
                add=[
                    {
                        "entity_id": f"sensor.{entity}",
                        "state_class": "total_increasing",
                        "unit": "kWh",
                    }
                ]
            )
            for aggregate, entity in entities.items()
        },
        "timeout_seconds": 5,
    }


def importer_for(source: str, token: str) -> HistoryPlanningImporter[Any]:
    if source == "household_load":
        settings = home_assistant_settings(token, household_load="household_energy")
        return HomeAssistantLoadImporter(
            HomeAssistantConfiguration.model_validate(settings)
        )
    settings = home_assistant_settings(
        token, grid_import="grid_import", grid_export="grid_export"
    )
    return HomeAssistantGridFlowImporter(
        HomeAssistantConfiguration.model_validate(settings)
    )


def registration(
    name: str,
    load: Callable[[], object | None] | None = None,
    plan: Callable[..., Any] = lambda _now, _schedule: None,
) -> ProviderRegistration:
    return ProviderRegistration(
        name=name,
        data_type=name.replace("_", "-"),
        adapter=TypeAdapter(HouseholdLoadData),
        plan=plan,
        is_fresh=lambda _data, _now: True,
        load=load,
    )


@pytest.fixture
def mock_client() -> Iterator[Callable[..., httpx.Client]]:
    """Build HTTP clients that answer every request with one fixed response."""
    with ExitStack() as clients:

        def build(status: int, **content: Any) -> httpx.Client:
            transport = httpx.MockTransport(lambda _: httpx.Response(status, **content))
            return clients.enter_context(httpx.Client(transport=transport))

        yield build


@pytest.fixture
def external_handler() -> Iterator[logging.StreamHandler[StringIO]]:
    handler = logging.StreamHandler(StringIO())
    logging.getLogger().addHandler(handler)
    yield handler
    logging.getLogger().removeHandler(handler)
    handler.close()


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
    rendered = format_at_fixed_time("first line\nsecond line", level=logging.WARNING)

    lines = rendered.splitlines()
    assert len(lines) == 2
    assert all(re.match(r"^WARNING 2026-08-11T10:42:36\+0000 ", line) for line in lines)
    assert lines[0].endswith("first line")
    assert lines[1].endswith("second line")


@pytest.mark.parametrize(
    ("name", "message", "expected"),
    [
        (
            "energy_optimizer.api",
            "event=request_completed component=api status=200",
            "INFO 2026-08-11T10:42:36+0000 request_completed | "
            "event=request_completed component=api status=200",
        ),
        (
            "uvicorn.error",
            "server ready",
            "INFO 2026-08-11T10:42:36+0000 server ready",
        ),
    ],
    ids=["structured-event", "plain-third-party-message"],
)
def test_log_formatter_keeps_messages_readable(
    name: str, message: str, expected: str
) -> None:
    assert format_at_fixed_time(message, name=name) == expected


def test_exception_traceback_lines_use_the_same_log_prefix() -> None:
    try:
        raise RuntimeError("provider offline")
    except RuntimeError:
        record = log_record(
            "request failed", level=logging.ERROR, exc_info=sys.exc_info()
        )

    rendered = ConsistentFormatter().format(record)

    assert all(
        re.match(rf"^ERROR {TIMESTAMP} ", line) for line in rendered.splitlines()
    )
    assert "RuntimeError: provider offline" in rendered


def test_third_party_loggers_use_one_handler_and_formatter(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging("INFO")

    logging.getLogger("uvicorn.error").info("server ready")
    logging.getLogger("httpx").info("HTTP Request: GET /health")
    logging.getLogger("httpx").info("connection pool ready")

    output = capsys.readouterr().out.splitlines()
    assert len(output) == 2
    assert all(re.match(rf"^INFO {TIMESTAMP} ", line) for line in output)
    assert output[0].endswith("server ready")
    assert output[1].endswith("connection pool ready")
    assert len(owned_handlers()) == 1


def test_configure_logging_is_idempotent() -> None:
    configure_logging("INFO")
    configure_logging("DEBUG")

    assert len(owned_handlers()) == 1
    assert logging.getLogger().level == logging.DEBUG


def test_external_root_handler_is_reused_without_a_duplicate_output_path(
    external_handler: logging.StreamHandler[StringIO],
) -> None:
    configure_logging("INFO")
    logging.getLogger("energy_optimizer").info("event=service_started component=api")

    assert external_handler in logging.getLogger().handlers
    assert not owned_handlers()
    assert isinstance(external_handler.formatter, ConsistentFormatter)
    assert len(external_handler.stream.getvalue().splitlines()) == 1


def test_external_uvicorn_style_root_configuration_formats_one_application_record(
    external_handler: logging.StreamHandler[StringIO],
) -> None:
    external_handler.setLevel(logging.INFO)
    uvicorn_logger = logging.getLogger("uvicorn.error")
    uvicorn_logger.handlers.clear()
    uvicorn_logger.propagate = False

    configure_logging("INFO")
    uvicorn_logger.info("event=server_ready component=uvicorn")

    rendered = external_handler.stream.getvalue()
    assert len(rendered.splitlines()) == 1
    assert "server_ready | event=server_ready component=uvicorn" in rendered


def test_module_entrypoint_bootstraps_logging_before_starting_uvicorn(
    monkeypatch: MonkeyPatch,
) -> None:
    import energy_optimizer.__main__ as entrypoint

    calls: list[tuple[str, object]] = []
    monkeypatch.setattr(
        entrypoint, "bootstrap_logging", lambda: calls.append(("bootstrap", None))
    )
    monkeypatch.setattr(
        "uvicorn.run",
        lambda application, **options: calls.append(("run", (application, options))),
    )

    entrypoint.main()

    server = {"host": "0.0.0.0", "port": 8000, "log_config": None, "access_log": False}
    assert calls == [
        ("bootstrap", None),
        ("run", ("energy_optimizer.api:app", server)),
    ]


@pytest.mark.usefixtures("minimal_configuration")
def test_invalid_environment_log_level_fails_startup(
    monkeypatch: MonkeyPatch, caplog: LogCaptureFixture
) -> None:
    monkeypatch.setenv("ENERGY_OPTIMIZER_LOG_LEVEL", "TRACE")

    with (
        caplog.at_level(logging.CRITICAL, logger=API_LOGGER),
        pytest.raises(ConfigurationError, match="Invalid ENERGY_OPTIMIZER_LOG_LEVEL"),
        TestClient(app),
    ):
        pass

    assert any(
        record.levelno == logging.CRITICAL
        for record in event_records(caplog, "service_startup_failed")
    )


def test_configuration_load_is_logged_without_secret_values(
    minimal_configuration: Path, caplog: LogCaptureFixture
) -> None:
    append_yaml(
        minimal_configuration,
        """\
home_assistant:
  base_url: http://homeassistant.local:8123
  token: do-not-log-this-token
  household_load:
    terms:
      - operation: add
        entities:
          - entity_id: sensor.household_energy
            state_class: total_increasing
            unit: kWh
  timeout_seconds: 10
""",
    )

    with capture_logs(caplog, logging.DEBUG, "energy_optimizer.config"):
        load_configuration(minimal_configuration)

    messages = "\n".join(caplog.messages)
    assert "configuration_loaded" in messages
    assert "timezone=UTC" in messages
    assert "do-not-log-this-token" not in messages


@pytest.mark.usefixtures("minimal_configuration")
def test_api_request_log_contains_request_context_and_not_request_body(
    caplog: LogCaptureFixture,
) -> None:
    response = request_app(
        caplog, logging.DEBUG, "GET", "/health", headers={"X-Request-ID": "request-66"}
    )

    request_log = single_event(caplog, "health_check_request", API_LOGGER)
    assert request_log.levelno == logging.DEBUG
    message = request_log.getMessage()
    for expected in (
        "method=GET",
        "path=/health",
        "status=200",
        "request_id=request-66",
        "duration_ms=",
    ):
        assert expected in message
    assert response.headers["X-Request-ID"] == "request-66"
    messages = "\n".join(caplog.messages)
    assert "event=service_started" in messages
    assert "event=service_stopping" in messages
    assert "event=service_stopped" in messages


def test_module_entrypoint_logs_bootstrap_completion(
    monkeypatch: MonkeyPatch, caplog: LogCaptureFixture
) -> None:
    import energy_optimizer.__main__ as entrypoint

    monkeypatch.setattr(entrypoint, "bootstrap_logging", lambda: logging.INFO)
    monkeypatch.setattr("uvicorn.run", lambda *_, **__: None)

    with caplog.at_level(logging.INFO, logger="energy_optimizer"):
        entrypoint.main()

    assert event_names(caplog) == ["process_logging_bootstrapped"]


@pytest.mark.usefixtures("minimal_configuration")
def test_api_startup_logs_each_lifecycle_phase_in_order(
    tmp_path: Path, monkeypatch: MonkeyPatch, caplog: LogCaptureFixture
) -> None:
    class StubOrchestrator:
        async def run_forever(self, stop_event: asyncio.Event) -> None:
            await stop_event.wait()

    monkeypatch.setattr(
        "energy_optimizer.api.lifecycle.build_configured_orchestrator",
        lambda _configuration, _store: StubOrchestrator(),
    )

    with (
        capture_logs(caplog, logging.INFO, "energy_optimizer"),
        TestClient(app) as client,
    ):
        assert client.get("/health").status_code == 200

    expected_events = [
        "service_lifespan_entered",
        "service_logging_configured",
        "service_starting",
        "service_configuration_load_started",
        "service_configuration_loaded",
        "service_persistence_store_initializing",
        "service_persistence_store_initialized",
        "service_orchestrator_build_started",
        "service_orchestrator_built",
        "service_orchestration_task_starting",
        "service_orchestration_task_started",
        "service_started",
    ]
    events = event_names(caplog)
    positions = [events.index(event) for event in expected_events]
    assert positions == sorted(positions)
    load_started = event_records(caplog, "service_configuration_load_started")[0]
    assert "path=config.yaml" in load_started.getMessage()
    assert str(tmp_path) not in load_started.getMessage()
    started = event_records(caplog, "service_started")[0]
    assert re.search(DURATION, started.getMessage())


def test_failed_startup_log_shows_the_last_completed_phase(
    tmp_path: Path, monkeypatch: MonkeyPatch, caplog: LogCaptureFixture
) -> None:
    monkeypatch.setenv("ENERGY_OPTIMIZER_CONFIG", str(tmp_path / "missing.yaml"))

    with (
        capture_logs(caplog, logging.INFO, "energy_optimizer"),
        pytest.raises(ConfigurationError),
        TestClient(app),
    ):
        pass

    events = event_names(caplog)
    assert "service_logging_configured" in events
    assert "service_configuration_load_started" in events
    assert "service_configuration_loaded" not in events
    assert "service_started" not in events
    assert events[-1] == "service_startup_failed"


def test_orchestrator_restore_logs_outcome_and_duration_per_source(
    tmp_path: Path, caplog: LogCaptureFixture
) -> None:
    def unreadable() -> object:
        raise RuntimeError("storage unreadable")

    schedule = DataSourceScheduleConfiguration(interval_seconds=60)
    configuration = OrchestrationConfiguration(
        enabled=True,
        sources={
            "household_load": schedule,
            "grid_flow": schedule,
            "battery": schedule,
        },
    )

    with capture_logs(caplog, logging.INFO, "energy_optimizer.orchestration"):
        ProviderOrchestrator(
            configuration,
            [
                registration("household_load", lambda: household_load_data(1.25)),
                registration("grid_flow", lambda: None),
                registration("battery", unreadable),
            ],
            ProviderDataStore(tmp_path),
        )

    by_source: dict[str, list[str]] = {}
    for message in caplog.messages:
        match = re.search(r"event=(orchestration_restore_\w+).* source=(\w+)", message)
        if match:
            by_source.setdefault(match.group(2), []).append(message)
            assert re.search(DURATION, message) or (
                match.group(1) == "orchestration_restore_started"
            )
    assert [m.split(" ", 1)[0] for m in by_source["household_load"]] == [
        "event=orchestration_restore_started",
        "event=orchestration_restore_completed",
    ]
    assert "status=restored" in by_source["household_load"][1]
    assert "status=empty" in by_source["grid_flow"][1]
    assert by_source["battery"][1].startswith("event=orchestration_restore_failed")
    assert "error_type=RuntimeError" in by_source["battery"][1]
    assert "1.25" not in "\n".join(caplog.messages)


def test_configured_household_load_restore_logs_logical_source_without_token(
    tmp_path: Path, caplog: LogCaptureFixture
) -> None:
    configuration = Configuration.model_validate(
        {
            "time_resolution_minutes": 60,
            "grid": {"maximum_import_kw": 10, "maximum_export_kw": 10},
            "solver": {"name": "highs", "time_limit_seconds": 60},
            "home_assistant": home_assistant_settings(
                "secret-provider-token", household_load="household_energy"
            ),
            "persistence": {"directory": str(tmp_path)},
            "orchestration": {
                "enabled": True,
                "sources": {"household_load": {"interval_seconds": 300}},
            },
        }
    )

    with capture_logs(caplog, logging.INFO, "energy_optimizer.orchestration"):
        build_configured_orchestrator(configuration, ProviderDataStore(tmp_path))

    messages = "\n".join(caplog.messages)
    assert "event=orchestration_restore_started" in messages
    assert "event=orchestration_restore_completed" in messages
    assert "source=household_load" in messages
    assert "status=empty" in messages
    assert "secret-provider-token" not in messages


@pytest.mark.usefixtures("minimal_configuration")
def test_api_failure_log_uses_error_level_without_payload(
    caplog: LogCaptureFixture,
) -> None:
    response = request_app(
        caplog,
        logging.INFO,
        "POST",
        "/optimize",
        json={"secret_payload": "do-not-log-this", "invalid": True},
    )

    assert response.status_code == 422
    request_log = single_event(caplog, "request_completed", API_LOGGER)
    assert request_log.levelno == logging.WARNING
    assert "do-not-log-this" not in request_log.getMessage()


@pytest.mark.usefixtures("minimal_configuration")
def test_successful_non_health_request_remains_info(caplog: LogCaptureFixture) -> None:
    response = request_app(
        caplog, logging.DEBUG, "GET", "/dashboard", follow_redirects=False
    )

    assert response.status_code == 307
    request_log = single_event(caplog, "request_completed", API_LOGGER)
    assert request_log.levelno == logging.INFO


@pytest.mark.usefixtures("minimal_configuration")
def test_dashboard_request_log_includes_scenario_kind(
    caplog: LogCaptureFixture,
) -> None:
    response = request_app(
        caplog, logging.INFO, "GET", "/api/v1/dashboard/data", params=FORECAST_PARAMS
    )

    assert response.status_code == 200
    request_log = single_event(
        caplog, "request_completed", API_LOGGER, "path=/api/v1/dashboard/data"
    )
    assert request_log.levelno == logging.INFO
    message = request_log.getMessage()
    assert "scenario_kind=forecast" in message
    assert "status=200" in message


def test_unavailable_forecast_emits_actionable_warning(
    tmp_path: Path, minimal_configuration: Path, caplog: LogCaptureFixture
) -> None:
    append_yaml(
        minimal_configuration,
        f"""\
persistence:
  directory: {tmp_path / "provider-data"}
forecast_solar:
  latitude: 52.52
  longitude: 13.41
  declination_degrees: 35
  azimuth_degrees: 0
  peak_power_kw: 8
""",
    )

    response = request_app(
        caplog, logging.INFO, "GET", "/api/v1/dashboard/data", params=FORECAST_PARAMS
    )

    assert response.status_code == 200
    warning = single_event(caplog, "dashboard_forecast_unavailable", API_LOGGER)
    assert warning.levelno == logging.WARNING
    message = warning.getMessage()
    assert "request_id=" in message
    assert "diagnostics=PV_forecast_data_is_unavailable" in message


def test_invalid_sample_excludes_its_hour_without_logging_token_or_raw_state(
    caplog: LogCaptureFixture, mock_client: Callable[..., httpx.Client]
) -> None:
    raw_state = "raw-state-marker"
    client = mock_client(
        200,
        json=home_assistant_history_payload(
            "sensor.household_energy",
            [
                ("2026-01-01T00:00:00+00:00", "0"),
                ("2026-01-01T00:30:00+00:00", raw_state),
                ("2026-01-01T01:00:00+00:00", "2"),
            ],
        ),
    )
    provider = importer_for("household_load", "secret-token")

    with capture_logs(caplog, logging.DEBUG, PROVIDERS_LOGGER):
        data = import_and_build(provider, client, START, END, now=NOW)

    # The bad sample no longer fails the fetch: its hour has no value and keeps
    # the raw state in the persisted cause, not in the log.
    assert data.load_kw == (None,)
    (excluded,) = data.exclusions
    (cause,) = excluded.causes
    assert cause.reason == "non_numeric"
    assert [point.state for point in cause.data_points] == [raw_state]
    assert not [
        record for record in caplog.records if record.levelno >= logging.WARNING
    ]
    counts = [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith(
            ("event=home_assistant_history_import", "event=provider_fetch_succeeded")
        )
    ]
    assert len(counts) == 2
    assert "invalid_sample_count=1" in counts[0]
    assert "excluded_hour_count=1" in counts[1]
    assert "secret-token" not in caplog.text
    assert raw_state not in caplog.text


def test_provider_failure_log_excludes_token_and_response_body(
    caplog: LogCaptureFixture, mock_client: Callable[..., httpx.Client]
) -> None:
    client = mock_client(503, text="secret-provider-token raw-response-marker")
    provider = importer_for("household_load", "secret-provider-token")

    with (
        capture_logs(caplog, logging.DEBUG, PROVIDERS_LOGGER),
        pytest.raises(RuntimeError, match="HTTP 503"),
    ):
        import_and_build(provider, client, START, END, now=NOW)

    assert single_event(caplog, "provider_fetch_failed").levelno == logging.ERROR
    assert any(
        record.levelno == logging.WARNING and "HTTP 503" in record.getMessage()
        for record in caplog.records
    )
    assert "secret-provider-token" not in caplog.text
    assert "raw-response-marker" not in caplog.text


@pytest.mark.parametrize("source", ["household_load", "grid_flow"])
@pytest.mark.parametrize(
    ("status", "message"), [(401, "authentication failed"), (503, "HTTP 503")]
)
def test_expected_home_assistant_failure_is_logged_without_traceback(
    source: str,
    status: int,
    message: str,
    caplog: LogCaptureFixture,
    mock_client: Callable[..., httpx.Client],
) -> None:
    client = mock_client(status)
    provider = importer_for(source, "secret-provider-token")

    with (
        capture_logs(caplog, logging.DEBUG, PROVIDERS_LOGGER),
        pytest.raises(HomeAssistantError, match=message),
    ):
        import_and_build(provider, client, START, END, now=NOW)

    failure = single_event(caplog, "provider_fetch_failed")
    assert failure.levelno == logging.ERROR
    assert failure.exc_info is None
    for expected in (
        "component=home_assistant operation=fetch",
        "error_type=HomeAssistant",
        "error=Home Assistant",
        message,
    ):
        assert expected in failure.getMessage()
    for forbidden in ("Traceback", "secret-provider-token", "Bearer"):
        assert forbidden not in caplog.text


@pytest.mark.parametrize("source", ["household_load", "grid_flow"])
def test_unexpected_failure_is_logged_with_traceback(
    source: str,
    caplog: LogCaptureFixture,
    monkeypatch: MonkeyPatch,
    mock_client: Callable[..., httpx.Client],
) -> None:
    def fail(*_: object, **__: object) -> None:
        raise ValueError("unexpected defect")

    monkeypatch.setattr(EnergyAggregate, "build", fail)
    client = mock_client(503)
    provider = importer_for(source, "secret-provider-token")

    with (
        capture_logs(caplog, logging.DEBUG, PROVIDERS_LOGGER),
        pytest.raises(ValueError, match="unexpected defect"),
    ):
        import_and_build(provider, client, START, END, now=NOW)

    failure = single_event(caplog, "provider_fetch_failed")
    assert failure.levelno == logging.ERROR
    assert failure.exc_info is not None
    assert failure.exc_info[0] is ValueError
    assert "component=home_assistant operation=fetch" in failure.getMessage()
    assert "error_type=ValueError" in failure.getMessage()
    assert "secret-provider-token" not in caplog.text


def test_persistence_log_reports_counts_without_logging_series(
    tmp_path: Path, caplog: LogCaptureFixture
) -> None:
    with capture_logs(caplog, logging.DEBUG, "energy_optimizer.storage"):
        ProviderDataStore(tmp_path).save(
            ProviderDataKey("household-load", "test-provider", "test-load"),
            TypeAdapter(HouseholdLoadData),
            household_load_data(1.25, 2.5, 3.75),
        )

    messages = "\n".join(caplog.messages)
    assert "event=persistence_succeeded" in messages
    assert "record_count=3" in messages
    for value in ("1.25", "2.5", "3.75"):
        assert value not in messages


def refresh_failure_with(
    error: Exception, tmp_path: Path, caplog: LogCaptureFixture
) -> logging.LogRecord:
    """Run one due refresh whose plan raises ``error`` and return its failure log."""

    def plan(_: datetime, __: DataSourceScheduleConfiguration) -> HistoryPlan[Any]:
        raise error

    configuration = OrchestrationConfiguration(
        enabled=True,
        sources={
            "household_load": DataSourceScheduleConfiguration(interval_seconds=60)
        },
    )

    with capture_logs(caplog, logging.DEBUG, "energy_optimizer.orchestration"):
        cycle = ProviderOrchestrator(
            configuration,
            [registration("household_load", plan=plan)],
            ProviderDataStore(tmp_path),
        ).run_due(START)

    assert cycle.provider_runs[0].status == "failed"
    failure = single_event(caplog, "provider_refresh_failed")
    assert failure.levelno == logging.ERROR
    return failure


def test_orchestration_failure_log_is_error_with_source_context(
    tmp_path: Path, caplog: LogCaptureFixture
) -> None:
    failure = refresh_failure_with(RuntimeError("provider offline"), tmp_path, caplog)

    assert "source=household_load" in failure.getMessage()
    assert "provider offline" in failure.getMessage()
    assert failure.exc_info is not None


def test_home_assistant_refresh_failure_omits_expected_traceback(
    tmp_path: Path, caplog: LogCaptureFixture
) -> None:
    failure = refresh_failure_with(
        HomeAssistantError("Home Assistant request timed out; retry later"),
        tmp_path,
        caplog,
    )

    assert "status" not in failure.getMessage()
    assert "timed out" in failure.getMessage()
    assert failure.exc_info is None
