"""Tests for the Home Assistant household-load importer."""

import logging
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import pytest

from energy_optimizer.exclusions import ExcludedDataPoint, HourExclusion
from energy_optimizer.providers.home_assistant import (
    HomeAssistantError,
    HomeAssistantLoadImporter,
)
from energy_optimizer.providers.interfaces import HouseholdLoadData
from home_assistant_fixtures import (
    FakeHomeAssistant,
    HistoryRequest,
    Readings,
    aggregate_settings,
    home_assistant_configuration_factory,
    home_assistant_planning_importer_factory,
    import_and_build,
)
from home_assistant_fixtures import home_assistant_history_payload as history_payload

ENTITY_ID = "sensor.household_energy"
SECOND_ENTITY_ID = "sensor.ev_energy"
START = datetime(2026, 1, 1, tzinfo=timezone.utc)
END = datetime(2026, 1, 1, 4, tzinfo=timezone.utc)
NOW = datetime(2026, 1, 1, 5, 30, tzinfo=timezone.utc)

Handler = Callable[[httpx.Request], httpx.Response]


def entity_settings(
    entity_id: str = ENTITY_ID,
    state_class: str = "total_increasing",
    unit: str = "kWh",
    **options: Any,
) -> dict[str, Any]:
    return {"entity_id": entity_id, "state_class": state_class, "unit": unit, **options}


def household_load(add: Sequence[str], subtract: Sequence[str] = ()) -> dict[str, Any]:
    """Return the configuration override that nets the given entities."""
    return {
        "household_load": aggregate_settings(
            add=[entity_settings(entity_id) for entity_id in add],
            subtract=[entity_settings(entity_id) for entity_id in subtract],
        )
    }


configuration = home_assistant_configuration_factory(**household_load([ENTITY_ID]))

importer = home_assistant_planning_importer_factory(
    HomeAssistantLoadImporter, configuration
)


def at(timestamp: str) -> datetime:
    return datetime.fromisoformat(f"2026-01-01T{timestamp}+00:00")


def hour(index: int) -> datetime:
    return START + timedelta(hours=index)


def readings(*points: tuple[str, str]) -> Readings:
    """Return ``(time, state)`` points as readings stamped on the start day."""
    return [(at(time).isoformat(), state) for time, state in points]


def hourly(*states: str, minutes: int = 0) -> Readings:
    """Return one reading per hour from 00:00, all shifted by ``minutes``."""
    return [
        ((hour(index) + timedelta(minutes=minutes)).isoformat(), state)
        for index, state in enumerate(states)
    ]


def respond(payload: Any) -> Handler:
    return lambda _: httpx.Response(200, json=payload)


def respond_by_entity(responses: Mapping[str, Any]) -> Handler:
    return lambda request: httpx.Response(
        200, json=responses[request.url.params["filter_entity_id"]]
    )


def respond_with_second_entity(second_readings: Readings) -> Handler:
    """Answer the default history for the first entity and ``second_readings``."""
    return respond_by_entity(
        {
            ENTITY_ID: history_payload(ENTITY_ID),
            SECOND_ENTITY_ID: history_payload(SECOND_ENTITY_ID, second_readings),
        }
    )


def build_load(
    handler: Handler,
    end_time: datetime = END,
    *,
    start_time: datetime = START,
    lookback: float | None = None,
    **settings: Any,
) -> HouseholdLoadData:
    """Import and build the household load, closing the client afterwards.

    Without ``lookback`` the importer's own default applies.
    """
    options = {} if lookback is None else {"history_lookback_seconds": lookback}
    provider, client = importer(httpx.MockTransport(handler), **settings)
    try:
        return import_and_build(
            provider, client, start_time, end_time, now=NOW, **options
        )
    finally:
        client.close()


def load_from_readings(
    points: Readings,
    hours: int,
    *,
    state_class: str = "total_increasing",
    last_resets: list[str | None] | None = None,
    **entity_options: Any,
) -> HouseholdLoadData:
    """Build the load of one entity that reports ``points`` over ``hours`` hours."""
    return build_load(
        respond(
            history_payload(
                ENTITY_ID, points, state_class=state_class, last_resets=last_resets
            )
        ),
        hour(hours),
        household_load=aggregate_settings(
            add=[entity_settings(state_class=state_class, **entity_options)]
        ),
    )


def total_with_reset(time: str) -> dict[str, Any]:
    """Return options of a ``total`` counter whose ``last_reset`` changes at ``time``.

    The change is reported with the third of four readings.
    """
    reset = at(time).isoformat()
    return {"state_class": "total", "last_resets": [None, None, reset, reset]}


def excluded_hours(exclusions: tuple[HourExclusion, ...]) -> dict[int, list[str]]:
    """Map each excluded hour's offset from START to its reasons, in cause order."""
    return {
        int((item.hour_start - START) / timedelta(hours=1)): [
            cause.reason for cause in item.causes
        ]
        for item in exclusions
    }


def logged(caplog: pytest.LogCaptureFixture, event: str) -> list[logging.LogRecord]:
    """Return the records of one ``home_assistant_history_<event>`` log event."""
    prefix = f"event=home_assistant_history_{event}"
    return [
        record for record in caplog.records if record.getMessage().startswith(prefix)
    ]


def test_fetch_converts_total_increasing_energy_to_hourly_load(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/history/period/2025-12-31T23:00:00+00:00"
        assert request.url.params["filter_entity_id"] == ENTITY_ID
        assert "minimal_response" not in request.url.params
        assert request.headers["authorization"] == "Bearer test-token"
        assert request.extensions["timeout"] == {
            "connect": 5.0,
            "read": 5.0,
            "write": 5.0,
            "pool": 5.0,
        }
        return httpx.Response(200, json=history_payload(ENTITY_ID))

    data = build_load(handler, lookback=3600)

    assert data.schema_version == "1"
    assert data.start_time == START
    assert data.interval_minutes == 60
    assert data.load_kw == (1.0, 2.0, 3.0, 4.0)
    assert data.unit == "kW"
    assert data.source.provider == "home-assistant"
    assert data.source.entity_id == "household_load"
    assert data.retrieved_at == NOW
    assert data.latest_observation_at == END
    (request_log,) = logged(caplog, "request")
    assert request_log.levelno == logging.DEBUG
    assert (
        "entity_id=sensor.household_energy "
        "start_time=2025-12-31T23:00:00+00:00 "
        "end_time=2026-01-01T04:00:00+00:00 status=200 duration_ms="
    ) in request_log.getMessage()
    (aggregate_log,) = logged(caplog, "aggregate")
    assert aggregate_log.levelno == logging.INFO
    assert "status=success" in aggregate_log.getMessage()


def test_fetch_splits_long_history_into_weekly_chunks_before_normalization(
    caplog: pytest.LogCaptureFixture,
) -> None:
    requested_end = START + timedelta(days=15)
    # One state per hour from an hour before the start to the end: the counter
    # rises 1 kWh per hour.
    home_assistant = FakeHomeAssistant(
        {ENTITY_ID: [(hour(offset - 1), str(offset)) for offset in range(15 * 24 + 2)]}
    )

    caplog.set_level(logging.INFO)
    data = build_load(home_assistant, requested_end, lookback=3600)

    week = timedelta(days=7)
    first_start = hour(-1)
    calls = home_assistant.requested_ranges(ENTITY_ID)
    assert calls == [
        (first_start, first_start + week),
        (first_start + week, first_start + 2 * week),
        (first_start + 2 * week, requested_end),
    ]
    assert all(end - start <= week for start, end in calls)
    assert data.start_time == START
    assert len(data.load_kw) == 15 * 24
    assert data.load_kw == (1.0,) * (15 * 24)
    assert logged(caplog, "request") == []
    (aggregate_log,) = logged(caplog, "aggregate")
    assert aggregate_log.levelno == logging.INFO
    assert "status=success" in aggregate_log.getMessage()
    assert "entity_count=1" in aggregate_log.getMessage()


def test_fetch_uses_one_request_for_a_week_without_lookback() -> None:
    requested_end = START + timedelta(days=7)
    home_assistant = FakeHomeAssistant(
        {ENTITY_ID: [(START, "0"), (requested_end, "1")]}
    )

    build_load(home_assistant, requested_end)

    assert home_assistant.requested_ranges(ENTITY_ID) == [(START, requested_end)]


def test_long_history_fetches_every_entity_for_each_chunk() -> None:
    week = START + timedelta(days=7)
    requested_end = START + timedelta(days=8)
    home_assistant = FakeHomeAssistant(
        {
            ENTITY_ID: [(START, "0.0"), (week, "1.0"), (requested_end, "2.0")],
            SECOND_ENTITY_ID: [(START, "0.0"), (week, "0.25"), (requested_end, "0.5")],
        }
    )

    data = build_load(
        home_assistant, requested_end, **household_load([ENTITY_ID], [SECOND_ENTITY_ID])
    )

    assert home_assistant.requests == [
        HistoryRequest(ENTITY_ID, START, week),
        HistoryRequest(ENTITY_ID, week, requested_end),
        HistoryRequest(SECOND_ENTITY_ID, START, week),
        HistoryRequest(SECOND_ENTITY_ID, week, requested_end),
    ]
    assert data.load_kw[7 * 24 - 1] == 0.75
    assert data.load_kw[-1] == 0.75


def test_counter_decrease_at_chunk_boundary_is_excluded_after_chunks_combine() -> None:
    requested_end = START + timedelta(days=8)
    boundary = START + timedelta(days=7)

    def handler(request: httpx.Request) -> httpx.Response:
        chunk_start = datetime.fromisoformat(request.url.path.rsplit("/", 1)[-1])
        chunk_end = datetime.fromisoformat(request.url.params["end_time"])
        if chunk_start == START:
            points = [
                (chunk_start.isoformat(), "100"),
                ((chunk_end - timedelta(hours=1)).isoformat(), "107"),
                (chunk_end.isoformat(), "200"),
            ]
        else:
            points = [
                (chunk_start.isoformat(), "0"),
                ((chunk_start + timedelta(hours=1)).isoformat(), "1"),
                (chunk_end.isoformat(), "2"),
            ]
        return httpx.Response(200, json=history_payload(ENTITY_ID, points))

    data = build_load(handler, requested_end)

    # The boundary observation is answered by both chunks and counts once, from
    # the later chunk, so the counter falls from 107 to 0 instead of rising to 200.
    first_excluded = 7 * 24 - 2
    assert len(data.load_kw) == 8 * 24
    assert data.load_kw[:first_excluded] == (0.0,) * first_excluded
    assert data.load_kw[first_excluded : first_excluded + 3] == (None, None, None)
    assert data.load_kw[first_excluded + 3 : -1] == (0.0,) * 22
    assert data.load_kw[-1] == 1.0
    assert excluded_hours(data.exclusions) == {
        first_excluded: ["counter_decrease"],
        first_excluded + 1: ["counter_decrease", "step_after_decrease"],
        first_excluded + 2: ["step_after_decrease"],
    }
    decrease = data.exclusions[0].causes[0]
    assert decrease.entity_id == ENTITY_ID
    assert decrease.data_points == (
        ExcludedDataPoint(
            boundary,
            state="0",
            unit="kWh",
            previous_timestamp=boundary - timedelta(hours=1),
            previous_value=107.0,
            value=0.0,
            step_kwh=-107.0,
            maximum_kwh=100.0,
        ),
    )


def test_failed_history_chunk_does_not_return_partial_long_range_data() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 2:
            return httpx.Response(503)
        chunk_start = datetime.fromisoformat(request.url.path.rsplit("/", 1)[-1])
        chunk_end = datetime.fromisoformat(request.url.params["end_time"])
        points = [(chunk_start.isoformat(), "0"), (chunk_end.isoformat(), "1")]
        return httpx.Response(200, json=history_payload(ENTITY_ID, points))

    with pytest.raises(HomeAssistantError, match="HTTP 503"):
        build_load(handler, START + timedelta(days=8))

    assert calls == 2


def test_total_increasing_observations_need_not_be_hour_aligned() -> None:
    # A quarter past every hour, from 23:15 on the evening before the start.
    points = hourly("0", "0.5", "1.5", "3.5", "6.5", "10.5", minutes=-45)

    data = build_load(respond(history_payload(ENTITY_ID, points)), lookback=3600)

    assert data.load_kw == (0.5, 1.0, 2.0, 3.0)


def test_fetch_uses_available_history_when_requested_start_predates_retention() -> None:
    data = build_load(
        respond(history_payload(ENTITY_ID)),
        start_time=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )

    assert data.start_time == START
    assert data.load_kw == (1.0, 2.0, 3.0, 4.0)


def test_fetch_aligns_entities_to_the_latest_available_start() -> None:
    data = build_load(
        respond_with_second_entity(hourly("0.25", "0.75", "1.5", "2.5", minutes=30)),
        start_time=datetime(2025, 1, 1, tzinfo=timezone.utc),
        **household_load([ENTITY_ID], [SECOND_ENTITY_ID]),
    )

    assert data.start_time == hour(1)
    assert data.load_kw == (1.5, 2.25, 3.0)


def test_fetch_converts_total_increasing_energy_and_unit() -> None:
    data = build_load(
        respond(
            history_payload(
                ENTITY_ID, hourly("1000", "2000", "3000", "4000", "5000"), unit="Wh"
            )
        ),
        household_load=aggregate_settings(add=[entity_settings(unit="Wh")]),
    )

    assert data.load_kw == (1.0, 1.0, 1.0, 1.0)
    assert data.latest_observation_at == END


def test_fetch_combines_add_and_subtract_entities() -> None:
    data = build_load(
        respond_with_second_entity(hourly("0", "0.25", "0.75", "1.5", "2.5")),
        **household_load([ENTITY_ID], [SECOND_ENTITY_ID]),
    )

    assert data.load_kw == (0.75, 1.5, 2.25, 3.0)
    assert data.source.entity_id == "household_load"


@pytest.mark.parametrize(
    ("handler", "settings", "message"),
    [
        pytest.param(
            respond(history_payload(ENTITY_ID, hourly("500"), unit="W")),
            {},
            "instantaneous power",
            id="instantaneous power",
        ),
        pytest.param(
            lambda _: httpx.Response(200, content=b"not-json"),
            {},
            "malformed JSON",
            id="malformed json",
        ),
        pytest.param(
            respond([]),
            {},
            "returned no history for sensor.household_energy",
            id="no series",
        ),
        pytest.param(
            respond([[]]),
            {},
            "returned no history for sensor.household_energy",
            id="empty series",
        ),
        pytest.param(
            respond_by_entity(
                {ENTITY_ID: history_payload(ENTITY_ID), SECOND_ENTITY_ID: []}
            ),
            household_load([ENTITY_ID, SECOND_ENTITY_ID]),
            "returned no history for sensor.ev_energy",
            id="second entity without history",
        ),
    ],
)
def test_fetch_rejects_unusable_history(
    handler: Handler, settings: dict[str, Any], message: str
) -> None:
    with pytest.raises(HomeAssistantError, match=message):
        build_load(handler, **settings)


@pytest.mark.parametrize(
    ("reported_unit", "reported_state_class", "reason"),
    [
        ("Wh", "total_increasing", "unit_mismatch"),
        ("kWh", "total", "state_class_mismatch"),
    ],
)
def test_settings_other_than_configured_exclude_every_hour(
    reported_unit: str, reported_state_class: str, reason: str
) -> None:
    data = build_load(
        respond(
            history_payload(
                ENTITY_ID, unit=reported_unit, state_class=reported_state_class
            )
        )
    )

    assert data.load_kw == (None,) * 4
    assert excluded_hours(data.exclusions) == {offset: [reason] for offset in range(4)}
    cause = data.exclusions[0].causes[0]
    assert cause.entity_id == ENTITY_ID
    assert cause.data_point_count == 5
    assert [(point.timestamp, point.state) for point in cause.data_points] == [
        (hour(offset), state) for offset, state in enumerate(["0", "1", "3", "6", "10"])
    ]
    assert {point.unit for point in cause.data_points} == {reported_unit}


def test_unit_change_mid_history_excludes_hours_until_the_unit_returns() -> None:
    points = readings(
        ("00:00", "0"),
        ("01:00", "1"),
        ("01:30", "1500"),
        ("02:30", "2"),
        ("03:30", "3"),
    )
    payload = history_payload(ENTITY_ID, points)
    payload[0][2]["attributes"]["unit_of_measurement"] = "Wh"

    data = build_load(respond(payload))

    assert data.load_kw == (1.0, None, None, 1.0)
    assert excluded_hours(data.exclusions) == {
        1: ["unit_mismatch"],
        2: ["unit_mismatch"],
    }
    assert data.exclusions[0].causes[0].data_points == (
        ExcludedDataPoint(at("01:30:00"), state="1500", unit="Wh"),
    )


def test_missing_unit_excludes_the_hour_of_the_counter_return() -> None:
    payload = history_payload(ENTITY_ID, hourly("0", "1", "2"))
    del payload[0][0]["attributes"]

    data = build_load(respond(payload), hour(2))

    assert data.load_kw == (None, 1.0)
    assert excluded_hours(data.exclusions) == {0: ["unit_missing"]}
    assert data.exclusions[0].causes[0].data_points == (
        ExcludedDataPoint(START, state="0", unit=None),
    )


@pytest.mark.parametrize(
    ("state", "reason"),
    [
        ("unavailable", "unavailable"),
        ("unknown", "unavailable"),
        ("  UnAvailable ", "unavailable"),
        ("not-a-number", "non_numeric"),
        ("", "non_numeric"),
        ("nan", "not_finite"),
        ("inf", "not_finite"),
        ("-1", "negative_value"),
    ],
)
def test_history_without_a_valid_sample_excludes_every_hour(
    state: str, reason: str
) -> None:
    data = build_load(
        respond([[{"state": state, "last_changed": "2026-01-01T00:00:00+00:00"}]])
    )

    assert data.load_kw == (None,) * 4
    assert excluded_hours(data.exclusions) == {offset: [reason] for offset in range(4)}
    cause = data.exclusions[0].causes[0]
    assert cause.entity_id == ENTITY_ID
    # The raw state is kept exactly as Home Assistant reported it.
    assert cause.data_points == (ExcludedDataPoint(START, state=state, unit=None),)


def test_total_increasing_decrease_mid_hour_excludes_the_hour() -> None:
    data = load_from_readings(
        readings(
            ("00:00", "10"), ("00:20", "10.5"), ("00:40", "0.25"), ("01:00", "1.25")
        ),
        1,
    )

    # A reset cannot be told apart from a glitch: neither the decrease nor the
    # step that follows it is trusted, and nothing is estimated for the hour.
    assert data.load_kw == (None,)
    assert excluded_hours(data.exclusions) == {
        0: ["counter_decrease", "step_after_decrease"]
    }
    decrease, after_decrease = data.exclusions[0].causes
    assert decrease.entity_id == ENTITY_ID
    assert decrease.data_points == (
        ExcludedDataPoint(
            at("00:40:00"),
            state="0.25",
            unit="kWh",
            previous_timestamp=at("00:20:00"),
            previous_value=10.5,
            value=0.25,
            step_kwh=-10.25,
            maximum_kwh=100.0,
        ),
    )
    assert after_decrease.data_points == (
        ExcludedDataPoint(
            at("01:00:00"),
            state="1.25",
            unit="kWh",
            previous_timestamp=at("00:40:00"),
            previous_value=0.25,
            value=1.25,
            step_kwh=1.0,
            maximum_kwh=100.0,
        ),
    )


def test_total_increasing_unavailable_observation_excludes_hours_until_it_returns(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    data = load_from_readings(
        readings(
            ("00:00", "10"),
            ("00:30", "unavailable"),
            ("01:30", "11.5"),
            ("02:30", "12.5"),
            ("03:30", "13.5"),
        ),
        4,
    )

    # Hour 1 holds the observation at which the counter returned, so it is
    # excluded too; no energy is fabricated for the outage.
    assert data.load_kw == (None, None, 1.0, 1.0)
    assert excluded_hours(data.exclusions) == {0: ["unavailable"], 1: ["unavailable"]}
    cause = data.exclusions[0].causes[0]
    assert cause.entity_id == ENTITY_ID
    assert cause.data_points == (
        ExcludedDataPoint(at("00:30:00"), state="unavailable", unit="kWh"),
    )
    assert ENTITY_ID in cause.message
    assert "2026-01-01T01:30:00+00:00" in cause.message
    messages = [record.getMessage() for record in caplog.records]
    assert any(
        message.startswith("event=home_assistant_history_import")
        and "invalid_sample_count=1" in message
        for message in messages
    )
    assert any(
        message.startswith("event=home_assistant_history_aggregate")
        and "excluded_hour_count=2" in message
        for message in messages
    )


@pytest.mark.parametrize(
    ("points", "hours", "options", "expected_load_kw", "expected_excluded"),
    [
        pytest.param(
            readings(("00:00", "10"), ("00:30", "10.5"), ("01:30", "11.5")),
            2,
            {},
            (0.5, 1.0),
            {},
            id="no interpolation between observations",
        ),
        pytest.param(
            hourly("0", "10"),
            1,
            {"maximum_interval_energy_kwh": 10},
            (10.0,),
            {},
            id="physical limit accepts the exact boundary",
        ),
        # Hour 0 is excluded as well: the decrease casts doubt on the counter value
        # at 00:30, which is where the valid step of that hour ended.
        pytest.param(
            readings(("00:00", "10"), ("00:30", "10.5"), ("01:30", "1.5")),
            2,
            {},
            (None, None),
            {0: ["counter_decrease"], 1: ["counter_decrease"]},
            id="decrease across hours",
        ),
        # The spike stays below the maximum, so it is recognized only by the fall
        # that follows it, and that fall excludes the hour the spike was recorded in.
        pytest.param(
            readings(
                ("00:00", "0.0182"),
                ("00:50", "57.2166"),
                ("01:05", "0.0182"),
                ("01:30", "0.5"),
                ("02:30", "1.5"),
            ),
            3,
            {},
            (None, None, 1.0),
            {0: ["counter_decrease"], 1: ["counter_decrease", "step_after_decrease"]},
            id="transient spike",
        ),
        pytest.param(
            readings(("00:00", "0"), ("00:30", "5"), ("01:30", "20")),
            2,
            {"maximum_interval_energy_kwh": 10},
            (5.0, None),
            {1: ["step_above_maximum"]},
            id="step above maximum leaves the earlier hour valid",
        ),
        pytest.param(
            readings(
                ("00:00", "10"), ("00:30", "10.5"), ("01:30", "0.25"), ("02:30", "0.75")
            ),
            3,
            {"state_class": "total", "last_resets": [None, None, None, None]},
            (None, None, None),
            {
                0: ["counter_decrease"],
                1: ["counter_decrease", "step_after_decrease"],
                2: ["step_after_decrease"],
            },
            id="total decrease without last reset change",
        ),
        pytest.param(
            readings(
                ("00:00", "10"), ("00:20", "10.5"), ("00:40", "0.25"), ("01:00", "1.25")
            ),
            1,
            total_with_reset("00:40"),
            (None,),
            {0: ["last_reset_changed", "counter_decrease", "step_after_decrease"]},
            id="total decrease with a changed last reset",
        ),
    ],
)
def test_counter_history_excludes_only_the_hours_it_casts_doubt_on(
    points: Readings,
    hours: int,
    options: dict[str, Any],
    expected_load_kw: tuple[float | None, ...],
    expected_excluded: dict[int, list[str]],
) -> None:
    data = load_from_readings(points, hours, **options)

    assert data.load_kw == expected_load_kw
    assert excluded_hours(data.exclusions) == expected_excluded


@pytest.mark.parametrize(
    (
        "points",
        "hours",
        "options",
        "expected_load_kw",
        "expected_excluded",
        "expected_points",
    ),
    [
        # An observation exactly on the boundary belongs to the earlier hour.
        pytest.param(
            readings(
                ("00:00", "10"),
                ("00:30", "unknown"),
                ("01:00", "11.5"),
                ("02:00", "12.5"),
            ),
            2,
            {},
            (None, 1.0),
            {0: ["unavailable"]},
            (ExcludedDataPoint(at("00:30:00"), state="unknown", unit="kWh"),),
            id="unknown observation",
        ),
        # The sample before the window is not part of it; the trailing one runs to
        # the end of the imported period because no valid observation follows it.
        pytest.param(
            [
                ("2025-12-31T23:30:00+00:00", "unavailable"),
                *readings(("00:00", "10"), ("01:00", "11"), ("01:30", "unavailable")),
            ],
            2,
            {},
            (1.0, None),
            {1: ["unavailable"]},
            (ExcludedDataPoint(at("01:30:00"), state="unavailable", unit="kWh"),),
            id="unavailable after the last valid sample",
        ),
        # The data point names when Home Assistant recorded the state, not the
        # window start it is carried to.
        pytest.param(
            [
                ("2025-12-31T23:30:00+00:00", "unavailable"),
                *readings(("01:00", "11"), ("02:00", "12")),
            ],
            2,
            {},
            (None, 1.0),
            {0: ["unavailable"]},
            (
                ExcludedDataPoint(
                    datetime(2025, 12, 31, 23, 30, tzinfo=timezone.utc),
                    state="unavailable",
                    unit="kWh",
                ),
            ),
            id="unavailable in force at the window start",
        ),
        pytest.param(
            readings(
                ("00:00", "10"), ("00:30", "10.5"), ("01:30", "11.5"), ("02:30", "12.5")
            ),
            3,
            total_with_reset("01:30"),
            (None, None, 1.0),
            {0: ["last_reset_changed"], 1: ["last_reset_changed"]},
            (
                ExcludedDataPoint(
                    at("01:30:00"),
                    state="11.5",
                    unit="kWh",
                    previous_timestamp=at("00:30:00"),
                    previous_value=10.5,
                    value=11.5,
                    step_kwh=1.0,
                    maximum_kwh=100.0,
                ),
            ),
            id="last reset change",
        ),
        pytest.param(
            readings(("00:00", "0"), ("00:30", "5"), ("00:40", "20")),
            1,
            {"maximum_interval_energy_kwh": 10},
            (None,),
            {0: ["step_above_maximum"]},
            (
                ExcludedDataPoint(
                    at("00:40:00"),
                    state="20",
                    unit="kWh",
                    previous_timestamp=at("00:30:00"),
                    previous_value=5.0,
                    value=20.0,
                    step_kwh=15.0,
                    maximum_kwh=10.0,
                ),
            ),
            id="step above maximum",
        ),
        pytest.param(
            readings(("00:00", "0"), ("00:20", "6"), ("00:40", "12")),
            1,
            {"maximum_interval_energy_kwh": 10},
            (None,),
            {0: ["hour_above_maximum"]},
            (ExcludedDataPoint(START, step_kwh=12.0, maximum_kwh=10.0),),
            id="hour above maximum although no single step exceeds it",
        ),
    ],
)
def test_excluded_hour_reports_the_data_point_behind_it(
    points: Readings,
    hours: int,
    options: dict[str, Any],
    expected_load_kw: tuple[float | None, ...],
    expected_excluded: dict[int, list[str]],
    expected_points: tuple[ExcludedDataPoint, ...],
) -> None:
    data = load_from_readings(points, hours, **options)

    assert data.load_kw == expected_load_kw
    assert excluded_hours(data.exclusions) == expected_excluded
    cause = data.exclusions[0].causes[0]
    assert cause.entity_id == ENTITY_ID
    assert cause.data_points == expected_points


def test_total_increasing_drop_to_zero_and_jump_back_lists_every_cause() -> None:
    data = load_from_readings(
        readings(
            ("00:00", "700"), ("00:30", "0"), ("00:40", "700.25"), ("00:50", "700.5")
        ),
        1,
    )

    assert data.load_kw == (None,)
    assert excluded_hours(data.exclusions) == {
        0: ["counter_decrease", "step_after_decrease", "step_above_maximum"]
    }
    assert {cause.entity_id for cause in data.exclusions[0].causes} == {ENTITY_ID}
    jump = data.exclusions[0].causes[2].data_points[0]
    assert (jump.step_kwh, jump.maximum_kwh) == (700.25, 100.0)


def test_spike_in_a_subtracted_entity_excludes_the_combined_hour() -> None:
    data = build_load(
        respond_by_entity(
            {
                ENTITY_ID: history_payload(
                    ENTITY_ID, readings(("00:00", "0"), ("00:30", "1"))
                ),
                SECOND_ENTITY_ID: history_payload(
                    SECOND_ENTITY_ID,
                    readings(
                        ("00:00", "0.0182"),
                        ("00:10:38", "57.2166"),
                        ("00:11:30", "0.0182"),
                    ),
                ),
            }
        ),
        hour(1),
        **household_load([ENTITY_ID], [SECOND_ENTITY_ID]),
    )

    # The valid entity's 1 kWh is not returned as if it were the combined load.
    assert data.load_kw == (None,)
    assert excluded_hours(data.exclusions) == {0: ["counter_decrease"]}
    assert data.exclusions[0].causes[0].entity_id == SECOND_ENTITY_ID


def test_household_load_excludes_the_hour_of_a_one_watt_hour_counter_dip() -> None:
    """A dip of any size is a decrease; there is no jitter tolerance."""
    data = load_from_readings(
        readings(
            ("00:00", "3280.000"),
            ("00:20", "3280.294"),
            ("00:20:12", "3280.293"),
            ("00:20:24", "3280.294"),
            ("01:00", "3280.600"),
        ),
        1,
        state_class="total",
    )

    assert data.load_kw == (None,)
    assert excluded_hours(data.exclusions) == {
        0: ["counter_decrease", "step_after_decrease"]
    }
    dip = data.exclusions[0].causes[0].data_points[0]
    assert dip.timestamp == at("00:20:12")
    assert dip.state == "3280.293"
    assert dip.step_kwh == pytest.approx(-0.001, abs=1e-9)


def test_fetch_excludes_negative_combined_load_hours() -> None:
    data = build_load(
        respond_with_second_entity(hourly("0", "2", "4", "6", "8")),
        **household_load([SECOND_ENTITY_ID], [ENTITY_ID]),
    )

    # Hour 1 nets to exactly zero and is a valid value; hours 2 and 3 are negative.
    assert data.load_kw == (1.0, 0.0, None, None)
    assert excluded_hours(data.exclusions) == {
        2: ["combined_negative"],
        3: ["combined_negative"],
    }
    cause = data.exclusions[0].causes[0]
    assert cause.entity_id is None
    # The add term's entities are listed before the subtract term's.
    assert [(point.entity_id, point.step_kwh) for point in cause.data_points] == [
        (SECOND_ENTITY_ID, 2.0),
        (ENTITY_ID, -3.0),
    ]


def test_fetch_excludes_the_reset_hour_of_both_counters_and_keeps_other_hours() -> None:
    times = ("00:00", "00:20", "00:40", "01:00", "02:00", "03:00")
    first_states = ("500", "0.5", "150.5", "150.5", "151.5", "153.5")
    second_states = ("1000", "0.1", "57", "57", "57.5", "58.5")
    data = build_load(
        respond_by_entity(
            {
                ENTITY_ID: history_payload(
                    ENTITY_ID, readings(*zip(times, first_states))
                ),
                SECOND_ENTITY_ID: history_payload(
                    SECOND_ENTITY_ID, readings(*zip(times, second_states))
                ),
            }
        ),
        hour(3),
        **household_load([ENTITY_ID], [SECOND_ENTITY_ID]),
    )

    assert data.start_time == START
    assert data.load_kw == pytest.approx((None, 0.5, 1.0))
    assert len(data.exclusions) == 1
    assert data.exclusions[0].hour_start == START
    assert [(cause.entity_id, cause.reason) for cause in data.exclusions[0].causes] == [
        (ENTITY_ID, "counter_decrease"),
        (ENTITY_ID, "step_after_decrease"),
        (ENTITY_ID, "step_above_maximum"),
        (SECOND_ENTITY_ID, "counter_decrease"),
        (SECOND_ENTITY_ID, "step_after_decrease"),
    ]


def test_fetch_does_not_return_partial_data_when_an_entity_fails() -> None:
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(200, json=history_payload(ENTITY_ID))
        return httpx.Response(503)

    with pytest.raises(HomeAssistantError, match="HTTP 503"):
        build_load(handler, **household_load([ENTITY_ID, SECOND_ENTITY_ID]))

    assert calls == 2


def test_fetch_excludes_every_hour_when_an_entity_has_only_invalid_samples() -> None:
    data = build_load(
        respond_with_second_entity(hourly("unavailable", "unknown")),
        **household_load([ENTITY_ID, SECOND_ENTITY_ID]),
    )

    # The valid entity's hours are not returned as if they were the total load.
    assert data.load_kw == (None,) * 4
    assert excluded_hours(data.exclusions) == {
        offset: ["unavailable"] for offset in range(4)
    }
    cause = data.exclusions[0].causes[0]
    assert cause.entity_id == SECOND_ENTITY_ID
    assert cause.data_point_count == 2
    assert [(point.timestamp, point.state) for point in cause.data_points] == [
        (at("00:00:00"), "unavailable"),
        (at("01:00:00"), "unknown"),
    ]


def raise_read_timeout(_: httpx.Request) -> httpx.Response:
    raise httpx.ReadTimeout("timed out")


@pytest.mark.parametrize(
    ("handler", "message", "status"),
    [
        pytest.param(
            lambda _: httpx.Response(401),
            "authentication failed",
            "status=401",
            id="http error",
        ),
        pytest.param(raise_read_timeout, "timed out", "status=timeout", id="timeout"),
    ],
)
def test_fetch_reports_request_failures_without_leaking_the_token(
    caplog: pytest.LogCaptureFixture, handler: Handler, message: str, status: str
) -> None:
    caplog.set_level(logging.DEBUG)

    with pytest.raises(HomeAssistantError, match=message):
        build_load(handler)

    (request_log,) = logged(caplog, "request")
    assert request_log.levelno == logging.DEBUG
    assert "entity_id=sensor.household_energy" in request_log.getMessage()
    assert status in request_log.getMessage()
    assert "test-token" not in request_log.getMessage()
    (aggregate_log,) = logged(caplog, "aggregate")
    assert aggregate_log.levelno == logging.WARNING
    assert "status=failed" in aggregate_log.getMessage()


def test_freshness_is_a_polling_health_check() -> None:
    data = build_load(respond(history_payload(ENTITY_ID)))
    provider = HomeAssistantLoadImporter(configuration())

    assert provider.is_fresh(data, now=NOW)
    provider.configuration = configuration(max_data_age_seconds=60)
    assert not provider.is_fresh(data, now=NOW)


def test_freshness_check_is_disabled_without_a_threshold() -> None:
    data = build_load(respond(history_payload(ENTITY_ID)))
    provider = HomeAssistantLoadImporter(configuration(max_data_age_seconds=None))

    assert provider.is_fresh(data, now=datetime(2036, 1, 1, tzinfo=timezone.utc))


@pytest.mark.parametrize(
    ("start_time", "end_time", "lookback", "message"),
    [
        (END, START, 3600, "end_time"),
        (datetime(2026, 1, 1, 0, 30, tzinfo=timezone.utc), END, 3600, "whole hourly"),
        (START, END, -1, "non-negative"),
    ],
)
def test_fetch_validates_requested_period(
    start_time: datetime, end_time: datetime, lookback: float, message: str
) -> None:
    with pytest.raises(HomeAssistantError, match=message):
        build_load(
            lambda _: httpx.Response(200),
            end_time,
            start_time=start_time,
            lookback=lookback,
        )
