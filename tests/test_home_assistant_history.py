"""Tests for the shared Home Assistant history import layer."""

import logging
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import pytest

from energy_optimizer.exclusions import ExcludedDataPoint
from energy_optimizer.providers import home_assistant_history
from energy_optimizer.providers.home_assistant_energy import EnergyAggregate
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

configuration = home_assistant_configuration_factory()


def hour(offset: int) -> datetime:
    return BASE + timedelta(hours=offset)


def day(offset: int) -> datetime:
    return BASE + timedelta(days=offset)


def counter_need(
    entity_id: str, start_time: datetime, end_time: datetime
) -> HistoryNeed:
    return HistoryNeed(entity_id, "counter", start_time, end_time)


def import_history(
    home_assistant: FakeHomeAssistant, *needs: HistoryNeed
) -> HomeAssistantHistory:
    client = home_assistant.client()
    try:
        return HomeAssistantHistoryImporter(configuration(), client).import_history(
            needs
        )
    finally:
        client.close()


def hourly_states(hours: int, rate: float = 1.0) -> list[tuple[datetime, str]]:
    return [(hour(offset), str(offset * rate)) for offset in range(hours + 1)]


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
    def handler(_: httpx.Request) -> httpx.Response:
        raise AssertionError("no request may be made without needs")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        history = HomeAssistantHistoryImporter(configuration(), client).import_history(
            []
        )
    finally:
        client.close()

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
    history = import_history(
        FakeHomeAssistant({FIRST: hourly_states(2)}),
        counter_need(FIRST, hour(0), hour(2)),
    )

    with pytest.raises(HistoryPlanError, match=message):
        history.window(entity_id, kind, start_time, end_time)


def test_a_failed_entity_reaches_only_the_consumers_that_read_it() -> None:
    home_assistant = FakeHomeAssistant(
        {FIRST: hourly_states(2), SECOND: hourly_states(2)}, failures={FIRST: 503}
    )

    history = import_history(
        home_assistant,
        counter_need(FIRST, hour(0), hour(2)),
        counter_need(SECOND, hour(0), hour(2)),
    )

    # Every entity is still attempted, and a failure is re-raised on every read.
    assert home_assistant.requested_entities() == [FIRST, SECOND]
    for _ in range(2):
        with pytest.raises(HomeAssistantError, match="HTTP 503.*sensor.first"):
            history.window(FIRST, "counter", hour(0), hour(2))
    assert [
        sample.value for sample in history.window(SECOND, "counter", hour(0), hour(2))
    ] == [
        0.0,
        1.0,
        2.0,
    ]


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

    history = import_history(
        home_assistant,
        counter_need(FIRST, hour(0), hour(2)),
        counter_need(SECOND, hour(0), hour(2)),
        counter_need(THIRD, hour(0), hour(2)),
    )

    assert home_assistant.requested_entities() == [FIRST, SECOND]
    assert [
        sample.value for sample in history.window(FIRST, "counter", hour(0), hour(2))
    ] == [0.0, 1.0, 2.0]
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
    requested: list[tuple[str, datetime]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        start = datetime.fromisoformat(request.url.path.rsplit("/", 1)[-1])
        requested.append((request.url.params["filter_entity_id"], start))
        if start >= day(7):
            return httpx.Response(401)
        return healthy(request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        history = HomeAssistantHistoryImporter(configuration(), client).import_history(
            [counter_need(FIRST, day(0), day(20)), counter_need(SECOND, day(0), day(1))]
        )
    finally:
        client.close()

    # The second chunk of the first entity is rejected; the third chunk and the
    # second entity are never requested.
    assert requested == [(FIRST, day(0)), (FIRST, day(7))]
    for entity_id in (FIRST, SECOND):
        with pytest.raises(HomeAssistantError, match="authentication failed"):
            history.window(entity_id, "counter", day(0), day(1))


@pytest.mark.parametrize(
    "first_entity_response",
    [
        pytest.param(lambda _: httpx.Response(404), id="not-found"),
        pytest.param(lambda _: httpx.Response(503), id="server-error"),
        pytest.param(
            lambda _: httpx.Response(200, content=b"not-json"), id="malformed-json"
        ),
        pytest.param(httpx.ReadTimeout("timed out"), id="timeout"),
        pytest.param(httpx.ConnectError("refused"), id="transport-error"),
    ],
)
def test_other_failures_of_one_entity_never_stop_the_import(
    first_entity_response: Callable[[httpx.Request], httpx.Response] | httpx.HTTPError,
) -> None:
    healthy = FakeHomeAssistant({SECOND: hourly_states(2), THIRD: hourly_states(2)})

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params["filter_entity_id"] == FIRST:
            if isinstance(first_entity_response, httpx.HTTPError):
                raise first_entity_response
            return first_entity_response(request)
        return healthy(request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        history = HomeAssistantHistoryImporter(configuration(), client).import_history(
            [
                counter_need(FIRST, hour(0), hour(2)),
                counter_need(SECOND, hour(0), hour(2)),
                counter_need(THIRD, hour(0), hour(2)),
            ]
        )
    finally:
        client.close()

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

    import_history(
        home_assistant,
        counter_need(FIRST, hour(0), hour(2)),
        counter_need(SECOND, hour(0), hour(2)),
        counter_need(THIRD, hour(0), hour(2)),
    )

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
    ("handler", "message"),
    [
        (lambda _: httpx.Response(401), "authentication failed"),
        (lambda _: httpx.Response(404), "was not found"),
        (lambda _: httpx.Response(503), "HTTP 503"),
        (lambda _: httpx.Response(200, content=b"not-json"), "malformed JSON"),
        (lambda _: httpx.Response(200, json={"not": "a list"}), "one entity series"),
    ],
)
def test_every_import_failure_names_its_entity(
    handler: Callable[[httpx.Request], httpx.Response], message: str
) -> None:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        history = HomeAssistantHistoryImporter(configuration(), client).import_history(
            [counter_need(FIRST, hour(0), hour(2))]
        )
    finally:
        client.close()

    with pytest.raises(HomeAssistantError, match=message) as failure:
        history.window(FIRST, "counter", hour(0), hour(2))
    assert FIRST in str(failure.value)


def test_timeouts_and_transport_errors_name_their_entity() -> None:
    for error, message in (
        (httpx.ReadTimeout("timed out"), "timed out"),
        (httpx.ConnectError("refused"), "transport error"),
    ):

        def handler(_: httpx.Request, error: httpx.HTTPError = error) -> httpx.Response:
            raise error

        client = httpx.Client(transport=httpx.MockTransport(handler))
        try:
            history = HomeAssistantHistoryImporter(
                configuration(), client
            ).import_history([counter_need(FIRST, hour(0), hour(2))])
        finally:
            client.close()

        with pytest.raises(HomeAssistantError, match=message) as failure:
            history.window(FIRST, "counter", hour(0), hour(2))
        assert FIRST in str(failure.value)


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
        [
            counter_need(FIRST, day(0), day(15)),
            counter_need(SECOND, day(0), day(15)),
        ]
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

    def handler(request: httpx.Request) -> httpx.Response:
        start = datetime.fromisoformat(request.url.path.rsplit("/", 1)[-1])
        if request.url.params["filter_entity_id"] == FIRST and start >= day(7):
            return httpx.Response(503)
        return healthy(request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        HomeAssistantHistoryImporter(configuration(), client).import_history(
            [counter_need(FIRST, day(0), day(20)), counter_need(SECOND, day(0), day(1))]
        )
    finally:
        client.close()

    (summary,) = [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("event=home_assistant_history_import")
    ]
    # The failed entity made two requests before failing on its second chunk.
    assert "status=partial" in summary
    assert "entity_count=2 failed_entity_count=1 request_count=3" in summary
    failures = [
        record
        for record in caplog.records
        if record.getMessage().startswith("event=home_assistant_history_entity_failed")
    ]
    assert len(failures) == 1
    assert failures[0].levelno == logging.WARNING
    assert f"entity_id={FIRST}" in failures[0].getMessage()


def test_unavailable_samples_stay_in_the_series_as_invalid_samples() -> None:
    home_assistant = FakeHomeAssistant(
        {
            FIRST: [
                (hour(0), "0"),
                (hour(1), "unavailable"),
                (hour(2), "unknown"),
                (hour(3), "3"),
            ]
        }
    )

    history = import_history(home_assistant, counter_need(FIRST, hour(0), hour(3)))

    samples = history.window(FIRST, "counter", hour(0), hour(3))
    assert [
        (sample.timestamp, sample.value, sample.state, sample.invalid)
        for sample in samples
    ] == [
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
            FIRST: [
                (hour(0), "0"),
                (hour(1), "unavailable"),
                (hour(2), "nan"),
                (hour(3), "3"),
            ],
            SECOND: [(hour(0), "0"), (hour(1), "not-a-number"), (hour(2), "2")],
        }
    )

    import_history(
        home_assistant,
        counter_need(FIRST, hour(0), hour(3)),
        counter_need(SECOND, hour(0), hour(2)),
    )

    (summary,) = [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("event=home_assistant_history_import")
    ]
    assert "status=success" in summary
    assert "invalid_sample_count=3" in summary
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
    history = import_history(
        FakeHomeAssistant({FIRST: []}), HistoryNeed(FIRST, kind, hour(0), hour(2))
    )

    with pytest.raises(HomeAssistantError, match=message):
        history.window(FIRST, kind, hour(0), hour(2))


@pytest.mark.parametrize("kind", ["counter", "state"])
def test_an_entity_with_only_unavailable_samples_is_imported_not_failed(
    kind: home_assistant_history.HistoryKind,
) -> None:
    history = import_history(
        FakeHomeAssistant({FIRST: [(hour(0), "unavailable")]}),
        HistoryNeed(FIRST, kind, hour(0), hour(2)),
    )

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
    history = import_history(
        FakeHomeAssistant(
            {FIRST: [(hour(0), "0"), (hour(2), "2"), (hour(5), "5")]},
        ),
        counter_need(FIRST, hour(-2), hour(8)),
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


def window_view(
    samples: tuple[HistorySample, ...],
) -> list[tuple[datetime, float | None, str | None, str | None]]:
    return [
        (sample.timestamp, sample.value, sample.state, sample.invalid)
        for sample in samples
    ]


def test_window_carries_an_invalid_state_in_force_and_never_a_valid_one_across_it() -> (
    None
):
    states = [(hour(0), "5"), (hour(1), "unavailable"), (hour(3), "7")]
    history = import_history(
        FakeHomeAssistant({FIRST: states}), counter_need(FIRST, hour(0), hour(4))
    )
    valid_seven = (hour(3), 7.0, "7", None)

    def window(start: datetime, end: datetime) -> list[tuple[Any, ...]]:
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
        independent = import_history(
            FakeHomeAssistant({FIRST: states}), counter_need(FIRST, start, hour(4))
        )
        assert window_view(independent.window(FIRST, "counter", start, hour(4))) == (
            window(start, hour(4))
        )


def test_a_window_keeps_when_the_state_in_force_was_recorded() -> None:
    history = import_history(
        FakeHomeAssistant({FIRST: [(hour(0), "5"), (hour(1), "unavailable")]}),
        counter_need(FIRST, hour(0), hour(4)),
    )

    (carried,) = history.window(FIRST, "counter", hour(2), hour(4))
    (recorded,) = history.window(FIRST, "counter", hour(1), hour(4))

    assert carried.timestamp == hour(2)
    assert carried.observed_at == hour(1)
    assert carried.recorded_at == hour(1)
    # A state recorded exactly at the window start is not re-stamped.
    assert recorded.observed_at is None
    assert recorded.recorded_at == hour(1)


def test_unit_state_class_and_reset_are_carried_to_samples_without_attributes() -> None:
    payload = [
        [
            {
                "state": "1",
                "last_updated": hour(0).isoformat(),
                "attributes": {
                    "unit_of_measurement": " kWh ",
                    "state_class": "total",
                    "last_reset": hour(-5).isoformat(),
                },
            },
            {"state": "2", "last_updated": hour(1).isoformat(), "attributes": {}},
            {
                "state": "3",
                "last_updated": hour(2).isoformat(),
                "attributes": {"last_reset": None},
            },
        ]
    ]
    client = httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload))
    )
    try:
        history = HomeAssistantHistoryImporter(configuration(), client).import_history(
            [counter_need(FIRST, hour(0), hour(2))]
        )
    finally:
        client.close()

    samples = history.window(FIRST, "counter", hour(0), hour(2))

    assert [sample.unit for sample in samples] == ["kWh"] * 3
    assert [sample.state_class for sample in samples] == ["total"] * 3
    assert [sample.last_reset for sample in samples] == [hour(-5), hour(-5), None]


def test_state_history_keeps_the_reported_unit_of_every_sample() -> None:
    home_assistant = FakeHomeAssistant(
        {FIRST: [(hour(0), "50"), (hour(1), "60")]},
        units={FIRST: "%"},
        state_classes={FIRST: "measurement"},
    )
    client = home_assistant.client()
    try:
        history = HomeAssistantHistoryImporter(configuration(), client).import_history(
            [HistoryNeed(FIRST, "state", hour(0), hour(1))]
        )
    finally:
        client.close()

    samples = history.window(FIRST, "state", hour(0), hour(1))

    assert [(sample.value, sample.unit) for sample in samples] == [
        (50.0, "%"),
        (60.0, "%"),
    ]


def history_from_payload(
    payload: object, kind: home_assistant_history.HistoryKind = "counter"
) -> HomeAssistantHistory:
    client = httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload))
    )
    try:
        return HomeAssistantHistoryImporter(configuration(), client).import_history(
            [HistoryNeed(FIRST, kind, hour(0), hour(2))]
        )
    finally:
        client.close()


def record(
    state: object, timestamp: datetime, **attributes: object
) -> dict[str, object]:
    return {
        "state": state,
        "last_updated": timestamp.isoformat(),
        "attributes": {"unit_of_measurement": "kWh", **attributes},
    }


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
    home_assistant = FakeHomeAssistant(
        {
            FIRST: [
                (hour(0), "0"),
                (hour(1) + HALF_HOUR, state),
                (hour(2), "2"),
                (hour(3), "3"),
                (hour(4), "4"),
            ]
        }
    )
    early = EnergyAggregate(AGGREGATION, hour(0), hour(4), label="early")
    late = EnergyAggregate(AGGREGATION, hour(2), hour(4), label="late")

    # One shared import serves both consumers.
    history = import_history(home_assistant, *early.needs(), *late.needs())

    assert len(home_assistant.requests) == 1
    # The sample is in force from 01:30 until the counter returns at 02:00, so
    # only the hour from 01:00 to 02:00 has no trustworthy value. Nothing is
    # counted for the hour before it: the counter did not change.
    early_series = early.build(history)
    assert early_series.values_kw == (0.0, None, 1.0, 1.0)
    (excluded,) = early_series.exclusions
    assert excluded.hour_start == hour(1)
    (cause,) = excluded.causes
    assert (cause.reason, cause.entity_id, cause.data_point_count) == (
        reason,
        FIRST,
        1,
    )
    assert cause.data_points == (ExcludedDataPoint(hour(1) + HALF_HOUR, state, "kWh"),)
    # The invalid sample lies before the late consumer's window.
    late_series = late.build(history)
    assert late_series.values_kw == (1.0, 1.0)
    assert late_series.exclusions == ()


def test_an_invalid_sample_on_an_hour_boundary_excludes_the_hour_it_closes() -> None:
    aggregate = EnergyAggregate(AGGREGATION, hour(0), hour(4), label="boundary")
    history = import_history(
        FakeHomeAssistant(
            {
                FIRST: [
                    (hour(0), "0"),
                    (hour(1), "unavailable"),
                    (hour(2), "2"),
                    (hour(3), "3"),
                    (hour(4), "4"),
                ]
            }
        ),
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
    home_assistant = FakeHomeAssistant(
        {
            FIRST: [
                (hour(0), "0"),
                (hour(1) + HALF_HOUR, "unavailable"),
                (hour(3), "3"),
                (hour(4), "4"),
            ]
        }
    )
    early = EnergyAggregate(AGGREGATION, hour(0), hour(4), label="early")
    late = EnergyAggregate(AGGREGATION, hour(2), hour(4), label="late")

    history = import_history(home_assistant, *early.needs(), *late.needs())

    assert len(home_assistant.requests) == 1
    # Both consumers see the outage from 01:30 to 03:00; the counter's steps
    # after it are attributed to the hour that ends at 04:00.
    early_series = early.build(history)
    assert early_series.values_kw == (0.0, None, None, 1.0)
    assert [item.hour_start for item in early_series.exclusions] == [hour(1), hour(2)]
    late_series = late.build(history)
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
                {"state": "1", "last_updated": hour(0).isoformat(), "attributes": {}},
                record("2", hour(1)),
                {"state": "3", "last_updated": hour(2).isoformat(), "attributes": {}},
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
    ("payload", "message"),
    [
        ([[record("1", hour(0))], [record("1", hour(0))]], "one entity series"),
        ([{"not": "a list"}], "a list of records"),
        ([["not-a-record"]], "contains an invalid record"),
        ([[{"state": "1", "attributes": {}}]], "has a missing timestamp"),
        ([[{"state": "1", "last_updated": "yesterday"}]], "invalid timestamp"),
        (
            [[{"state": "1", "last_updated": "2026-01-01T00:00:00"}]],
            "must include a timezone",
        ),
    ],
)
def test_malformed_history_payloads_fail_the_entity_naming_it(
    payload: object, message: str
) -> None:
    history = history_from_payload(payload)

    with pytest.raises(HomeAssistantError, match=message) as failure:
        history.window(FIRST, "counter", hour(0), hour(2))
    assert FIRST in str(failure.value)


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
                record("2", hour(1), unit_of_measurement="Wh", state_class=5),
                {"state": "3", "last_updated": hour(2).isoformat(), "attributes": {}},
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

    history = import_history(
        FakeHomeAssistant({FIRST: hourly_states(2)}),
        counter_need(FIRST, hour(0), hour(2)),
    )

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
