"""Tests for the shared Home Assistant history import layer."""

import logging
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import pytest

from energy_optimizer.exclusions import ExcludedDataPoint
from energy_optimizer.providers import home_assistant_history
from energy_optimizer.providers.home_assistant_energy import (
    EnergyAggregate,
    HomeAssistantEnergySeries,
)
from energy_optimizer.providers.home_assistant_history import (
    HistoryNeed,
    HistoryPlanError,
    HistorySample,
    HomeAssistantError,
    HomeAssistantHistory,
    HomeAssistantHistoryImporter,
)
from home_assistant_fixtures import (
    FakeHomeAssistant,
    aggregate_configuration,
    home_assistant_configuration_factory,
)

FIRST = "sensor.first"
SECOND = "sensor.second"
THIRD = "sensor.third"
BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)
HALF_HOUR = timedelta(minutes=30)
AGGREGATION = aggregate_configuration(
    add=[{"entity_id": FIRST, "state_class": "total_increasing", "unit": "kWh"}]
)

Handler = Callable[[httpx.Request], httpx.Response]
SampleView = tuple[datetime, float | None, str | None, str | None]

configuration = home_assistant_configuration_factory()


def hour(offset: int) -> datetime:
    return BASE + timedelta(hours=offset)


def day(offset: int) -> datetime:
    return BASE + timedelta(days=offset)


def counter_need(
    entity_id: str, start_time: datetime, end_time: datetime
) -> HistoryNeed:
    return HistoryNeed(entity_id, "counter", start_time, end_time)


def first_two_hours(*entity_ids: str) -> list[HistoryNeed]:
    return [counter_need(entity_id, hour(0), hour(2)) for entity_id in entity_ids]


def import_history(source: Handler, *needs: HistoryNeed) -> HomeAssistantHistory:
    """Import ``needs`` from ``source``, a fake endpoint or a plain handler."""
    with httpx.Client(transport=httpx.MockTransport(source)) as client:
        return HomeAssistantHistoryImporter(configuration(), client).import_history(
            needs
        )


def history_of(
    states: list[tuple[datetime, str]],
    kind: home_assistant_history.HistoryKind = "counter",
    start_time: datetime = hour(0),
    end_time: datetime = hour(2),
) -> HomeAssistantHistory:
    """Import the ``states`` of the first entity, for hours 0 to 2 by default."""
    need = HistoryNeed(FIRST, kind, start_time, end_time)
    return import_history(FakeHomeAssistant({FIRST: states}), need)


def responding(status: int, **content: Any) -> Handler:
    return lambda _: httpx.Response(status, **content)


def returning(payload: object) -> Handler:
    return responding(200, json=payload)


def raising(error: Exception) -> Handler:
    def handler(_: httpx.Request) -> httpx.Response:
        raise error

    return handler


def failing_from_second_chunk(
    healthy: FakeHomeAssistant, status: int, entity_id: str | None = None
) -> Handler:
    """Answer like ``healthy`` until the second seven-day chunk, then fail."""

    def handler(request: httpx.Request) -> httpx.Response:
        response = healthy(request)
        last = healthy.requests[-1]
        if last.start_time >= day(7) and entity_id in (None, last.entity_id):
            return httpx.Response(status)
        return response

    return handler


def logged(caplog: pytest.LogCaptureFixture, prefix: str) -> list[logging.LogRecord]:
    return [
        record for record in caplog.records if record.getMessage().startswith(prefix)
    ]


def hourly_states(hours: int, rate: float = 1.0) -> list[tuple[datetime, str]]:
    return [(hour(offset), str(offset * rate)) for offset in range(hours + 1)]


def hourly(*states: str) -> list[tuple[datetime, str]]:
    return [(hour(offset), state) for offset, state in enumerate(states)]


def record(
    state: object, timestamp: datetime, unit: str | None = "kWh", **attrs: object
) -> dict[str, object]:
    """Build one history record; ``unit=None`` leaves the unit attribute out."""
    if unit is not None:
        attrs["unit_of_measurement"] = unit
    return {"state": state, "last_updated": timestamp.isoformat(), "attributes": attrs}


def history_from_payload(
    payload: object, kind: home_assistant_history.HistoryKind = "counter"
) -> HomeAssistantHistory:
    return import_history(
        returning(payload), HistoryNeed(FIRST, kind, hour(0), hour(2))
    )


def window_view(samples: tuple[HistorySample, ...]) -> list[SampleView]:
    return [
        (sample.timestamp, sample.value, sample.state, sample.invalid)
        for sample in samples
    ]


def build_early_and_late(
    states: list[tuple[datetime, str]],
) -> tuple[HomeAssistantEnergySeries, HomeAssistantEnergySeries]:
    """Serve a consumer of hours 0 to 4 and one of hours 2 to 4 from one import."""
    home_assistant = FakeHomeAssistant({FIRST: states})
    early = EnergyAggregate(AGGREGATION, hour(0), hour(4), label="early")
    late = EnergyAggregate(AGGREGATION, hour(2), hour(4), label="late")

    history = import_history(home_assistant, *early.needs(), *late.needs())

    assert len(home_assistant.requests) == 1
    return early.build(history), late.build(history)


def assert_import_failure_names_entity(source: Handler, message: str) -> None:
    history = import_history(source, counter_need(FIRST, hour(0), hour(2)))

    with pytest.raises(HomeAssistantError, match=message) as failure:
        history.window(FIRST, "counter", hour(0), hour(2))
    assert FIRST in str(failure.value)


def test_needs_of_one_entity_merge_into_one_request_sequence() -> None:
    home_assistant = FakeHomeAssistant(
        {FIRST: hourly_states(24 * 20), SECOND: hourly_states(24)}
    )

    import_history(
        home_assistant,
        counter_need(FIRST, day(0), day(3)),
        counter_need(SECOND, day(0), day(1)),
        counter_need(FIRST, day(2), day(20)),
    )

    # The two needs of the first entity become one sequence from the earliest
    # start to the latest end, split into contiguous seven-day chunks.
    assert home_assistant.requested_entities() == [FIRST, SECOND]
    assert home_assistant.requested_ranges(FIRST) == [
        (day(0), day(7)),
        (day(7), day(14)),
        (day(14), day(20)),
    ]
    assert home_assistant.requested_ranges(SECOND) == [(day(0), day(1))]


def test_importing_no_needs_makes_no_request() -> None:
    history = import_history(
        raising(AssertionError("no request may be made without needs"))
    )

    with pytest.raises(HistoryPlanError, match="not planned"):
        history.window(FIRST, "counter", hour(0), hour(1))


def test_planning_one_entity_with_two_kinds_is_an_error_before_any_request() -> None:
    home_assistant = FakeHomeAssistant({FIRST: hourly_states(2)})

    with pytest.raises(HistoryPlanError, match="both counter and state"):
        import_history(
            home_assistant,
            counter_need(FIRST, hour(0), hour(2)),
            HistoryNeed(FIRST, "state", hour(0), hour(2)),
        )

    assert home_assistant.requests == []


@pytest.mark.parametrize(
    ("entity_id", "kind", "start_time", "end_time", "message"),
    [
        (SECOND, "counter", hour(0), hour(2), "was requested but not planned"),
        (FIRST, "state", hour(0), hour(2), "planned as counter history"),
        (FIRST, "counter", hour(-1), hour(2), "outside the planned range"),
        (FIRST, "counter", hour(0), hour(3), "outside the planned range"),
    ],
)
def test_reading_history_that_was_not_planned_is_an_explicit_error(
    entity_id: str,
    kind: home_assistant_history.HistoryKind,
    start_time: datetime,
    end_time: datetime,
    message: str,
) -> None:
    history = history_of(hourly_states(2))

    with pytest.raises(HistoryPlanError, match=message):
        history.window(entity_id, kind, start_time, end_time)


def test_a_failed_entity_reaches_only_the_consumers_that_read_it() -> None:
    home_assistant = FakeHomeAssistant(
        {FIRST: hourly_states(2), SECOND: hourly_states(2)}, failures={FIRST: 503}
    )

    history = import_history(home_assistant, *first_two_hours(FIRST, SECOND))

    # Every entity is still attempted, and a failure is re-raised on every read.
    assert home_assistant.requested_entities() == [FIRST, SECOND]
    for _ in range(2):
        with pytest.raises(HomeAssistantError, match="HTTP 503.*sensor.first"):
            history.window(FIRST, "counter", hour(0), hour(2))
    samples = history.window(SECOND, "counter", hour(0), hour(2))
    assert [sample.value for sample in samples] == [0.0, 1.0, 2.0]


@pytest.mark.parametrize("status", [401, 403])
def test_an_authentication_rejection_ends_the_import_after_one_request(
    status: int,
) -> None:
    home_assistant = FakeHomeAssistant(
        {FIRST: hourly_states(24 * 20), SECOND: hourly_states(24)},
        failures={FIRST: status, SECOND: status, THIRD: status},
    )

    history = import_history(
        home_assistant,
        counter_need(FIRST, day(0), day(20)),
        counter_need(SECOND, day(0), day(1)),
        counter_need(THIRD, day(0), day(1)),
    )

    # The first entity spans three chunks, yet nothing is sent after the first
    # rejected request, and no later entity is requested at all.
    assert home_assistant.requested_entities() == [FIRST]
    assert len(home_assistant.requests) == 1
    for entity_id in (FIRST, SECOND, THIRD):
        with pytest.raises(
            HomeAssistantError,
            match=(
                r"authentication failed; check the configured token "
                rf"\(entity {entity_id}\)$"
            ),
        ):
            history.window(entity_id, "counter", day(0), day(1))


def test_entities_imported_before_an_authentication_rejection_stay_readable() -> None:
    home_assistant = FakeHomeAssistant(
        {FIRST: hourly_states(2), SECOND: hourly_states(2), THIRD: hourly_states(2)},
        failures={SECOND: 401},
    )

    history = import_history(home_assistant, *first_two_hours(FIRST, SECOND, THIRD))

    assert home_assistant.requested_entities() == [FIRST, SECOND]
    samples = history.window(FIRST, "counter", hour(0), hour(2))
    assert [sample.value for sample in samples] == [0.0, 1.0, 2.0]
    for entity_id in (SECOND, THIRD):
        with pytest.raises(
            HomeAssistantError, match="authentication failed"
        ) as failure:
            history.window(entity_id, "counter", hour(0), hour(2))
        assert entity_id in str(failure.value)


def test_an_authentication_rejection_after_the_first_chunk_ends_the_import() -> None:
    healthy = FakeHomeAssistant(
        {FIRST: hourly_states(24 * 20), SECOND: hourly_states(24)}
    )

    history = import_history(
        failing_from_second_chunk(healthy, 401),
        counter_need(FIRST, day(0), day(20)),
        counter_need(SECOND, day(0), day(1)),
    )

    # The second chunk of the first entity is rejected; the third chunk and the
    # second entity are never requested.
    assert [(item.entity_id, item.start_time) for item in healthy.requests] == [
        (FIRST, day(0)),
        (FIRST, day(7)),
    ]
    for entity_id in (FIRST, SECOND):
        with pytest.raises(HomeAssistantError, match="authentication failed"):
            history.window(entity_id, "counter", day(0), day(1))


@pytest.mark.parametrize(
    "first_entity_handler",
    [
        pytest.param(responding(404), id="not-found"),
        pytest.param(responding(503), id="server-error"),
        pytest.param(responding(200, content=b"not-json"), id="malformed-json"),
        pytest.param(raising(httpx.ReadTimeout("timed out")), id="timeout"),
        pytest.param(raising(httpx.ConnectError("refused")), id="transport-error"),
    ],
)
def test_other_failures_of_one_entity_never_stop_the_import(
    first_entity_handler: Handler,
) -> None:
    healthy = FakeHomeAssistant({SECOND: hourly_states(2), THIRD: hourly_states(2)})

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params["filter_entity_id"] == FIRST:
            return first_entity_handler(request)
        return healthy(request)

    history = import_history(handler, *first_two_hours(FIRST, SECOND, THIRD))

    assert healthy.requested_entities() == [SECOND, THIRD]
    with pytest.raises(HomeAssistantError):
        history.window(FIRST, "counter", hour(0), hour(2))
    for entity_id in (SECOND, THIRD):
        assert history.window(entity_id, "counter", hour(0), hour(2))


def test_an_authentication_rejection_is_logged_once_without_the_token(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    home_assistant = FakeHomeAssistant(
        {FIRST: hourly_states(2)}, failures={FIRST: 401, SECOND: 401, THIRD: 401}
    )

    import_history(home_assistant, *first_two_hours(FIRST, SECOND, THIRD))

    messages = [record.getMessage() for record in caplog.records]
    (rejection,) = [
        record
        for record in caplog.records
        if "authentication_failed" in record.getMessage()
    ]
    assert rejection.levelno == logging.WARNING
    assert f"entity_id={FIRST}" in rejection.getMessage()
    assert "skipped_entity_count=2" in rejection.getMessage()
    assert not any("event=home_assistant_history_entity_failed" in m for m in messages)
    (summary,) = [
        m for m in messages if m.startswith("event=home_assistant_history_import ")
    ]
    assert "status=partial" in summary
    assert "entity_count=3 failed_entity_count=3 request_count=1" in summary
    assert not any("test-token" in m or "Bearer" in m for m in messages)


@pytest.mark.parametrize(
    ("source", "message"),
    [
        (responding(401), "authentication failed"),
        (responding(404), "was not found"),
        (responding(503), "HTTP 503"),
        (responding(200, content=b"not-json"), "malformed JSON"),
        (returning({"not": "a list"}), "one entity series"),
        (
            returning([[record("1", hour(0))], [record("1", hour(0))]]),
            "one entity series",
        ),
        (returning([{"not": "a list"}]), "a list of records"),
        (returning([["not-a-record"]]), "contains an invalid record"),
        (returning([[{"state": "1", "attributes": {}}]]), "has a missing timestamp"),
        (
            returning([[{"state": "1", "last_updated": "yesterday"}]]),
            "invalid timestamp",
        ),
        (
            returning([[{"state": "1", "last_updated": "2026-01-01T00:00:00"}]]),
            "must include a timezone",
        ),
    ],
)
def test_every_import_failure_names_its_entity(source: Handler, message: str) -> None:
    assert_import_failure_names_entity(source, message)


def test_timeouts_and_transport_errors_name_their_entity() -> None:
    for error, message in (
        (httpx.ReadTimeout("timed out"), "timed out"),
        (httpx.ConnectError("refused"), "transport error"),
    ):
        assert_import_failure_names_entity(raising(error), message)


def test_the_whole_import_reuses_one_http_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home_assistant = FakeHomeAssistant(
        {FIRST: hourly_states(24 * 15), SECOND: hourly_states(24 * 15)}
    )
    original_client = httpx.Client
    created: list[httpx.Client] = []

    def create_client(*_: object, **__: object) -> httpx.Client:
        client = original_client(transport=httpx.MockTransport(home_assistant))
        created.append(client)
        return client

    monkeypatch.setattr(httpx, "Client", create_client)

    HomeAssistantHistoryImporter(configuration()).import_history(
        [counter_need(FIRST, day(0), day(15)), counter_need(SECOND, day(0), day(15))]
    )

    # Six requests (three chunks for each of two entities) share one client,
    # which is closed once the import has finished.
    assert len(home_assistant.requests) == 6
    assert len(created) == 1
    assert created[0].is_closed


def test_the_import_summary_counts_every_attempted_request(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    healthy = FakeHomeAssistant(
        {FIRST: hourly_states(24 * 20), SECOND: hourly_states(24)}
    )

    import_history(
        failing_from_second_chunk(healthy, 503, FIRST),
        counter_need(FIRST, day(0), day(20)),
        counter_need(SECOND, day(0), day(1)),
    )

    (summary,) = logged(caplog, "event=home_assistant_history_import")
    # The failed entity made two requests before failing on its second chunk.
    assert "status=partial" in summary.getMessage()
    assert (
        "entity_count=2 failed_entity_count=1 request_count=3" in summary.getMessage()
    )
    (failure,) = logged(caplog, "event=home_assistant_history_entity_failed")
    assert failure.levelno == logging.WARNING
    assert f"entity_id={FIRST}" in failure.getMessage()


def test_unavailable_samples_stay_in_the_series_as_invalid_samples() -> None:
    history = history_of(hourly("0", "unavailable", "unknown", "3"), end_time=hour(3))

    samples = history.window(FIRST, "counter", hour(0), hour(3))
    assert window_view(samples) == [
        (hour(0), 0.0, "0", None),
        (hour(1), None, "unavailable", "unavailable"),
        (hour(2), None, "unknown", "unavailable"),
        (hour(3), 3.0, "3", None),
    ]
    # An invalid sample still carries the attributes of the sample before it.
    assert [sample.unit for sample in samples] == ["kWh"] * 4


def test_the_import_summary_counts_invalid_samples_instead_of_warning_per_sample(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    home_assistant = FakeHomeAssistant(
        {
            FIRST: hourly("0", "unavailable", "nan", "3"),
            SECOND: hourly("0", "not-a-number", "2"),
        }
    )

    import_history(
        home_assistant,
        counter_need(FIRST, hour(0), hour(3)),
        counter_need(SECOND, hour(0), hour(2)),
    )

    (summary,) = logged(caplog, "event=home_assistant_history_import")
    assert "status=success" in summary.getMessage()
    assert "invalid_sample_count=3" in summary.getMessage()
    assert [
        record.getMessage()
        for record in caplog.records
        if record.levelno >= logging.WARNING
    ] == []


@pytest.mark.parametrize(
    ("kind", "message"),
    [
        ("counter", "returned no history for sensor.first"),
        ("state", "returned no usable history for sensor.first"),
    ],
)
def test_an_entity_without_any_history_fails(
    kind: home_assistant_history.HistoryKind, message: str
) -> None:
    history = history_of([], kind)

    with pytest.raises(HomeAssistantError, match=message):
        history.window(FIRST, kind, hour(0), hour(2))


@pytest.mark.parametrize("kind", ["counter", "state"])
def test_an_entity_with_only_unavailable_samples_is_imported_not_failed(
    kind: home_assistant_history.HistoryKind,
) -> None:
    history = history_of([(hour(0), "unavailable")], kind)

    (only,) = history.window(FIRST, kind, hour(0), hour(2))

    assert (only.value, only.state, only.invalid) == (
        None,
        "unavailable",
        "unavailable",
    )


def test_an_entity_that_is_only_ever_unavailable_excludes_every_hour() -> None:
    aggregate = EnergyAggregate(AGGREGATION, hour(0), hour(2), label="unavailable")
    history = import_history(
        FakeHomeAssistant({FIRST: [(hour(0), "unavailable")]}), *aggregate.needs()
    )

    series = aggregate.build(history)

    assert series.values_kw == (None, None)
    assert [item.hour_start for item in series.exclusions] == [hour(0), hour(1)]
    for item in series.exclusions:
        (cause,) = item.causes
        assert (cause.reason, cause.entity_id) == ("unavailable", FIRST)
        assert cause.data_points == (ExcludedDataPoint(hour(0), "unavailable", "kWh"),)


def test_empty_chunks_are_tolerated_when_a_later_chunk_has_history() -> None:
    # Home Assistant retains no history before the third day.
    home_assistant = FakeHomeAssistant(
        {FIRST: [(day(10) + timedelta(hours=i), str(i)) for i in range(5)]}
    )

    history = import_history(home_assistant, counter_need(FIRST, day(0), day(15)))

    assert len(home_assistant.requested_ranges(FIRST)) == 3
    # Home Assistant restates the state in force at the start of the last chunk;
    # its timestamp is new, so it is kept as an unchanged sample.
    samples = history.window(FIRST, "counter", day(0), day(15))
    assert [sample.value for sample in samples] == [0.0, 1.0, 2.0, 3.0, 4.0, 4.0]


def test_window_returns_the_state_in_force_at_its_start_stamped_with_the_start() -> (
    None
):
    history = history_of(
        [(hour(0), "0"), (hour(2), "2"), (hour(5), "5")],
        start_time=hour(-2),
        end_time=hour(8),
    )

    def window(start: int, end: int) -> list[tuple[datetime, float | None]]:
        return [
            (sample.timestamp, sample.value)
            for sample in history.window(FIRST, "counter", hour(start), hour(end))
        ]

    # Between changes, the earlier state is carried to the window start.
    assert window(3, 6) == [(hour(3), 2.0), (hour(5), 5.0)]
    # A change exactly at the start is the state at the start.
    assert window(2, 4) == [(hour(2), 2.0)]
    # Nothing exists before the first change, so nothing is invented.
    assert window(-2, 1) == [(hour(0), 0.0)]
    # A window after the last change sees only the state in force.
    assert window(6, 8) == [(hour(6), 5.0)]
    # A window that ends before any state exists is empty.
    assert window(-2, -1) == []


def test_window_carries_an_invalid_state_in_force_and_never_a_valid_one_across_it() -> (
    None
):
    states = [(hour(0), "5"), (hour(1), "unavailable"), (hour(3), "7")]
    history = history_of(states, end_time=hour(4))
    valid_seven = (hour(3), 7.0, "7", None)

    def window(start: datetime, end: datetime) -> list[SampleView]:
        return window_view(history.window(FIRST, "counter", start, end))

    # Before the sample became unavailable, the earlier value is in force.
    assert window(hour(0) + HALF_HOUR, hour(4)) == [
        (hour(0) + HALF_HOUR, 5.0, "5", None),
        (hour(1), None, "unavailable", "unavailable"),
        valid_seven,
    ]
    # Afterwards the unavailable state is in force, and the earlier value 5 is
    # not carried across it.
    assert window(hour(1) + HALF_HOUR, hour(4)) == [
        (hour(1) + HALF_HOUR, None, "unavailable", "unavailable"),
        valid_seven,
    ]
    # A sample that became unavailable exactly at the start is in force too.
    assert window(hour(1), hour(4)) == [
        (hour(1), None, "unavailable", "unavailable"),
        valid_seven,
    ]
    # Every window equals what an independent request for it would see.
    for start in (hour(0) + HALF_HOUR, hour(1) + HALF_HOUR, hour(1)):
        independent = history_of(states, start_time=start, end_time=hour(4))
        assert window_view(independent.window(FIRST, "counter", start, hour(4))) == (
            window(start, hour(4))
        )


def test_a_window_keeps_when_the_state_in_force_was_recorded() -> None:
    history = history_of(hourly("5", "unavailable"), end_time=hour(4))

    (carried,) = history.window(FIRST, "counter", hour(2), hour(4))
    (recorded,) = history.window(FIRST, "counter", hour(1), hour(4))

    assert carried.timestamp == hour(2)
    assert carried.observed_at == hour(1)
    assert carried.recorded_at == hour(1)
    # A state recorded exactly at the window start is not re-stamped.
    assert recorded.observed_at is None
    assert recorded.recorded_at == hour(1)


def test_unit_state_class_and_reset_are_carried_to_samples_without_attributes() -> None:
    history = history_from_payload(
        [
            [
                record(
                    "1",
                    hour(0),
                    unit=" kWh ",
                    state_class="total",
                    last_reset=hour(-5).isoformat(),
                ),
                record("2", hour(1), unit=None),
                record("3", hour(2), unit=None, last_reset=None),
            ]
        ]
    )

    samples = history.window(FIRST, "counter", hour(0), hour(2))

    assert [sample.unit for sample in samples] == ["kWh"] * 3
    assert [sample.state_class for sample in samples] == ["total"] * 3
    assert [sample.last_reset for sample in samples] == [hour(-5), hour(-5), None]


def test_state_history_keeps_the_reported_unit_of_every_sample() -> None:
    home_assistant = FakeHomeAssistant(
        {FIRST: hourly("50", "60")},
        units={FIRST: "%"},
        state_classes={FIRST: "measurement"},
    )

    history = import_history(
        home_assistant, HistoryNeed(FIRST, "state", hour(0), hour(1))
    )

    samples = history.window(FIRST, "state", hour(0), hour(1))

    assert [(sample.value, sample.unit) for sample in samples] == [
        (50.0, "%"),
        (60.0, "%"),
    ]


@pytest.mark.parametrize(
    ("kind", "state", "value", "invalid"),
    [
        ("counter", "1.5", 1.5, None),
        ("counter", "1e3", 1000.0, None),
        ("counter", 5, 5.0, None),
        ("counter", "unavailable", None, "unavailable"),
        ("counter", "unknown", None, "unavailable"),
        ("counter", " Unavailable ", None, "unavailable"),
        ("counter", "UNKNOWN", None, "unavailable"),
        ("counter", "not-a-number", None, "non_numeric"),
        ("counter", "", None, "non_numeric"),
        ("counter", None, None, "non_numeric"),
        ("counter", "nan", None, "not_finite"),
        ("counter", "inf", None, "not_finite"),
        ("counter", "-inf", None, "not_finite"),
        ("counter", "-1", None, "negative_value"),
        # Only a cumulative counter cannot be negative.
        ("state", "-3.5", -3.5, None),
        ("state", "unavailable", None, "unavailable"),
        ("state", "not-a-number", None, "non_numeric"),
        ("state", "inf", None, "not_finite"),
    ],
)
def test_every_state_is_classified_and_keeps_its_raw_text(
    kind: home_assistant_history.HistoryKind,
    state: object,
    value: float | None,
    invalid: str | None,
) -> None:
    history = history_from_payload([[record(state, hour(0))]], kind)

    (sample,) = history.window(FIRST, kind, hour(0), hour(2))

    assert (sample.value, sample.invalid) == (value, invalid)
    assert sample.state == (None if state is None else str(state))


@pytest.mark.parametrize(
    ("state", "reason"),
    [
        ("not-a-number", "non_numeric"),
        ("nan", "not_finite"),
        ("-1", "negative_value"),
        (" Unavailable ", "unavailable"),
    ],
)
def test_an_invalid_sample_only_excludes_hours_of_the_windows_that_contain_it(
    state: str, reason: str
) -> None:
    early_series, late_series = build_early_and_late(
        [
            (hour(0), "0"),
            (hour(1) + HALF_HOUR, state),
            (hour(2), "2"),
            (hour(3), "3"),
            (hour(4), "4"),
        ]
    )

    # The sample is in force from 01:30 until the counter returns at 02:00, so
    # only the hour from 01:00 to 02:00 has no trustworthy value. Nothing is
    # counted for the hour before it: the counter did not change.
    assert early_series.values_kw == (0.0, None, 1.0, 1.0)
    (excluded,) = early_series.exclusions
    assert excluded.hour_start == hour(1)
    (cause,) = excluded.causes
    assert (cause.reason, cause.entity_id, cause.data_point_count) == (reason, FIRST, 1)
    assert cause.data_points == (ExcludedDataPoint(hour(1) + HALF_HOUR, state, "kWh"),)
    # The invalid sample lies before the late consumer's window.
    assert late_series.values_kw == (1.0, 1.0)
    assert late_series.exclusions == ()


def test_an_invalid_sample_on_an_hour_boundary_excludes_the_hour_it_closes() -> None:
    aggregate = EnergyAggregate(AGGREGATION, hour(0), hour(4), label="boundary")
    history = import_history(
        FakeHomeAssistant({FIRST: hourly("0", "unavailable", "2", "3", "4")}),
        *aggregate.needs(),
    )

    series = aggregate.build(history)

    # An observation exactly on a boundary belongs to the earlier hour, so the
    # unavailable state at 01:00 is the missing closing reading of the hour from
    # 00:00 to 01:00, and the valid reading at 02:00 closes the hour it returns in.
    # A sensor that publishes on the hour would otherwise import 0 kWh here.
    assert series.values_kw == (None, None, 1.0, 1.0)
    assert [item.hour_start for item in series.exclusions] == [hour(0), hour(1)]


def test_an_invalid_state_in_force_at_the_window_start_excludes_until_it_ends() -> None:
    early_series, late_series = build_early_and_late(
        [
            (hour(0), "0"),
            (hour(1) + HALF_HOUR, "unavailable"),
            (hour(3), "3"),
            (hour(4), "4"),
        ]
    )

    # Both consumers see the outage from 01:30 to 03:00; the counter's steps
    # after it are attributed to the hour that ends at 04:00.
    assert early_series.values_kw == (0.0, None, None, 1.0)
    assert [item.hour_start for item in early_series.exclusions] == [hour(1), hour(2)]
    assert late_series.values_kw == (None, 1.0)
    (excluded,) = late_series.exclusions
    assert excluded.hour_start == hour(2)
    (cause,) = excluded.causes
    assert cause.reason == "unavailable"
    # The state in force is re-stamped to the window start, but the data point
    # and the message show when Home Assistant really recorded it.
    assert cause.data_points == (
        ExcludedDataPoint(hour(1) + HALF_HOUR, "unavailable", "kWh"),
    )
    assert "first at 2026-01-01T01:30:00+00:00" in cause.message


def test_a_sample_without_a_unit_is_only_excluded_where_the_unit_is_unknown() -> None:
    history = history_from_payload(
        [
            [
                record("1", hour(0), unit=None),
                record("2", hour(1)),
                record("3", hour(2), unit=None),
            ]
        ]
    )

    samples = history.window(FIRST, "counter", hour(0), hour(2))

    # The history layer keeps the sample; the unit is only known once a sample
    # reports it, and carried forward from there.
    assert [sample.unit for sample in samples] == [None, "kWh", "kWh"]
    assert [sample.invalid for sample in samples] == [None, None, None]
    series = EnergyAggregate(AGGREGATION, hour(0), hour(2), label="unit").build(history)
    assert series.values_kw == (None, 1.0)
    (excluded,) = series.exclusions
    assert excluded.hour_start == hour(0)
    (cause,) = excluded.causes
    assert (cause.reason, cause.entity_id) == ("unit_missing", FIRST)
    assert cause.data_points == (ExcludedDataPoint(hour(0), "1", None),)


@pytest.mark.parametrize(
    ("sample", "invalid"),
    [
        (record("1", hour(0), state_class=5), "invalid_attribute"),
        (record("1", hour(0), last_reset=5), "invalid_attribute"),
        (record("1", hour(0), last_reset="garbage"), "invalid_attribute"),
        # A state that is already invalid keeps its own reason.
        (record("abc", hour(0), state_class=5), "non_numeric"),
    ],
)
def test_a_sample_with_unusable_attributes_is_invalid_and_keeps_its_state(
    sample: dict[str, object], invalid: str
) -> None:
    history = history_from_payload([[sample]])

    (only,) = history.window(FIRST, "counter", hour(0), hour(2))

    assert only.value is None
    assert only.invalid == invalid
    assert only.state == sample["state"]


def test_attributes_of_a_sample_with_an_invalid_attribute_are_not_carried_on() -> None:
    history = history_from_payload(
        [
            [
                record("1", hour(0), state_class="total"),
                record("2", hour(1), unit="Wh", state_class=5),
                record("3", hour(2), unit=None),
            ]
        ]
    )

    samples = history.window(FIRST, "counter", hour(0), hour(2))

    assert [sample.invalid for sample in samples] == [None, "invalid_attribute", None]
    assert [sample.unit for sample in samples] == ["kWh", "Wh", "kWh"]
    assert [sample.state_class for sample in samples] == ["total"] * 3


def test_duplicate_timestamps_within_one_response_are_left_for_the_consumer() -> None:
    history = history_from_payload(
        [[record("1", hour(0)), record("2", hour(0)), record("3", hour(1))]]
    )

    assert len(history.window(FIRST, "counter", hour(0), hour(2))) == 3
    with pytest.raises(HomeAssistantError, match="contains duplicate timestamps"):
        EnergyAggregate(AGGREGATION, hour(0), hour(2), label="duplicates").build(
            history
        )


@pytest.mark.parametrize("kind", ["counter", "state"])
def test_counter_and_state_history_treat_a_padded_unavailable_state_alike(
    kind: home_assistant_history.HistoryKind,
) -> None:
    history = history_from_payload(
        [
            [
                record("50", hour(0)),
                record(" Unavailable ", hour(1)),
                record("60", hour(2)),
            ]
        ],
        kind,
    )

    samples = history.window(FIRST, kind, hour(0), hour(2))

    assert window_view(samples) == [
        (hour(0), 50.0, "50", None),
        (hour(1), None, " Unavailable ", "unavailable"),
        (hour(2), 60.0, "60", None),
    ]


def test_an_unexpected_error_is_recorded_against_its_entity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*_: object) -> None:
        raise ValueError("boom")

    monkeypatch.setattr(home_assistant_history, "_clean_counter_records", fail)

    history = history_of(hourly_states(2))

    with pytest.raises(
        HomeAssistantError, match="sensor.first.*unexpectedly.*boom"
    ) as failure:
        history.window(FIRST, "counter", hour(0), hour(2))
    assert isinstance(failure.value.__cause__, ValueError)


def test_computed_values_are_shared_and_failures_are_not_remembered() -> None:
    history = HomeAssistantHistory()
    calls: list[str] = []

    def compute() -> int:
        calls.append("computed")
        return 7

    def fail() -> int:
        raise HomeAssistantError("not remembered")

    assert history.computed("key", compute) == 7
    assert history.computed("key", compute) == 7
    assert calls == ["computed"]
    for _ in range(2):
        with pytest.raises(HomeAssistantError, match="not remembered"):
            history.computed("failing", fail)


@pytest.mark.parametrize(
    ("start_time", "end_time", "message"),
    [
        (datetime(2026, 1, 1), hour(1), "must include a timezone"),
        (hour(0), datetime(2026, 1, 1, 1), "must include a timezone"),
        (hour(1), hour(1), "must end after it starts"),
        (hour(2), hour(1), "must end after it starts"),
    ],
)
def test_history_needs_require_an_ordered_timezone_aware_range(
    start_time: datetime, end_time: datetime, message: str
) -> None:
    with pytest.raises(HomeAssistantError, match=message):
        counter_need(FIRST, start_time, end_time)


def test_history_needs_are_normalized_to_utc() -> None:
    offset = timezone(timedelta(hours=2))

    need = counter_need(
        FIRST,
        datetime(2026, 1, 1, 2, tzinfo=offset),
        datetime(2026, 1, 1, 4, tzinfo=offset),
    )

    assert need.start_time == hour(0)
    assert need.end_time == hour(2)
    assert need.start_time.utcoffset() == timedelta(0)
