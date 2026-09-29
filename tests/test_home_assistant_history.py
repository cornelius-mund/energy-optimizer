"""Tests for the shared Home Assistant history import layer."""

import logging
import re
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from energy_optimizer.config import HomeAssistantEnergyEntityConfiguration
from energy_optimizer.providers import home_assistant_history
from energy_optimizer.providers.home_assistant_energy import EnergyAggregate
from energy_optimizer.providers.home_assistant_history import (
    HistoryNeed,
    HistoryPlanError,
    HomeAssistantError,
    HomeAssistantHistory,
    HomeAssistantHistoryImporter,
)
from home_assistant_fixtures import (
    FakeHomeAssistant,
    home_assistant_configuration_factory,
)

FIRST = "sensor.first"
SECOND = "sensor.second"
BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)

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


def test_unknown_and_unavailable_samples_are_skipped_with_one_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING)
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
    assert [sample.value for sample in samples] == [0.0, 3.0]
    warnings = [
        record.getMessage()
        for record in caplog.records
        if "unknown or unavailable" in record.getMessage()
    ]
    assert len(warnings) == 1
    assert FIRST in warnings[0]
    assert "2 unknown or unavailable" in warnings[0]


@pytest.mark.parametrize(
    ("states", "message"),
    [
        ([], "returned no history for sensor.first"),
        ([(hour(0), "unavailable")], "no usable history after skipping"),
    ],
)
def test_an_entity_without_usable_history_fails(
    states: list[tuple[datetime, str]], message: str
) -> None:
    history = import_history(
        FakeHomeAssistant({FIRST: states}), counter_need(FIRST, hour(0), hour(2))
    )

    with pytest.raises(HomeAssistantError, match=message):
        history.window(FIRST, "counter", hour(0), hour(2))


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

    def window(start: int, end: int) -> list[tuple[datetime, float]]:
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


def test_window_does_not_carry_a_state_across_a_later_unavailable_sample() -> None:
    history = import_history(
        FakeHomeAssistant(
            {FIRST: [(hour(0), "5"), (hour(1), "unavailable"), (hour(3), "7")]}
        ),
        counter_need(FIRST, hour(0), hour(4)),
    )

    def window(start: datetime, end: datetime) -> list[tuple[datetime, float]]:
        return [
            (sample.timestamp, sample.value)
            for sample in history.window(FIRST, "counter", start, end)
        ]

    # Before the sample became unavailable, the earlier value is in force.
    assert window(hour(0) + timedelta(minutes=30), hour(4)) == [
        (hour(0) + timedelta(minutes=30), 5.0),
        (hour(3), 7.0),
    ]
    # Afterwards the state is unknown, so an independent request for the window
    # would have no state at its start and neither does the shared series.
    assert window(hour(1) + timedelta(minutes=30), hour(4)) == [(hour(3), 7.0)]
    # A sample that became unavailable exactly at the start is unknown too.
    assert window(hour(1), hour(4)) == [(hour(3), 7.0)]


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


@pytest.mark.parametrize(
    ("state", "message"),
    [
        ("not-a-number", "non-numeric value at 2026-01-01T01:00:00+00:00"),
        ("-1", "invalid value at 2026-01-01T01:00:00+00:00"),
        ("nan", "invalid value at 2026-01-01T01:00:00+00:00"),
    ],
)
def test_an_invalid_sample_only_fails_consumers_whose_window_contains_it(
    state: str, message: str
) -> None:
    home_assistant = FakeHomeAssistant(
        {
            FIRST: [
                (hour(0), "0"),
                (hour(1), state),
                (hour(2), "2"),
                (hour(3), "3"),
                (hour(4), "4"),
            ]
        }
    )
    entity = HomeAssistantEnergyEntityConfiguration.model_validate(
        {
            "entity_id": FIRST,
            "state_class": "total_increasing",
            "unit": "kWh",
            "operation": "add",
        }
    )
    early = EnergyAggregate([entity], hour(0), hour(4), label="early")
    late = EnergyAggregate([entity], hour(2), hour(4), label="late")

    # One shared import serves both consumers.
    history = import_history(home_assistant, *early.needs(), *late.needs())

    assert len(home_assistant.requests) == 1
    with pytest.raises(HomeAssistantError, match=re.escape(message)):
        early.build(history)
    assert late.build(history).values_kw == (1.0, 1.0)


def test_samples_without_a_unit_are_rejected_only_where_the_unit_is_unknown() -> None:
    payload = [
        [
            {"state": "1", "last_updated": hour(0).isoformat(), "attributes": {}},
            {
                "state": "2",
                "last_updated": hour(1).isoformat(),
                "attributes": {"unit_of_measurement": "kWh"},
            },
            {"state": "3", "last_updated": hour(2).isoformat(), "attributes": {}},
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

    assert [sample.problem is not None for sample in samples] == [True, False, False]
    assert "missing unit_of_measurement" in (samples[0].problem or "")
    assert [sample.unit for sample in samples[1:]] == ["kWh", "kWh"]


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
    ("sample", "problem"),
    [
        (record(None, hour(0)), "unavailable value"),
        (record("1", hour(0), state_class=5), "invalid state_class"),
        (record("1", hour(0), last_reset=5), "invalid last_reset timestamp"),
        (record("1", hour(0), last_reset="garbage"), "invalid timestamp"),
    ],
)
def test_a_sample_with_unusable_attributes_carries_its_problem(
    sample: dict[str, object], problem: str
) -> None:
    history = history_from_payload([[sample]])

    (only,) = history.window(FIRST, "counter", hour(0), hour(2))

    assert only.problem is not None
    assert problem in only.problem
    assert FIRST in only.problem


def test_duplicate_timestamps_within_one_response_are_left_for_the_consumer() -> None:
    history = history_from_payload(
        [[record("1", hour(0)), record("2", hour(0)), record("3", hour(1))]]
    )
    entity = HomeAssistantEnergyEntityConfiguration.model_validate(
        {
            "entity_id": FIRST,
            "state_class": "total_increasing",
            "unit": "kWh",
            "operation": "add",
        }
    )

    assert len(history.window(FIRST, "counter", hour(0), hour(2))) == 3
    with pytest.raises(HomeAssistantError, match="contains duplicate timestamps"):
        EnergyAggregate([entity], hour(0), hour(2), label="duplicates").build(history)


def test_counter_and_state_history_disagree_on_what_is_unavailable() -> None:
    padded = [record(" Unavailable ", hour(1)), record("60", hour(2))]

    counter = history_from_payload([[record("50", hour(0)), *padded]])
    state = history_from_payload([[record("50", hour(0)), *padded]], "state")

    # Counters skip only the exact states, so a padded one is an invalid value.
    assert [
        sample.problem is None
        for sample in counter.window(FIRST, "counter", hour(0), hour(2))
    ] == [True, False, True]
    # Plain state history tolerates whitespace and case, as before.
    assert [
        sample.value for sample in state.window(FIRST, "state", hour(0), hour(2))
    ] == [50.0, 60.0]


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
