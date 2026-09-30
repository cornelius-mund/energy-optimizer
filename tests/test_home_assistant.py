"""Tests for the Home Assistant household-load importer."""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import pytest

from energy_optimizer.exclusions import ExcludedDataPoint, HourExclusion
from energy_optimizer.providers.home_assistant import (
    HomeAssistantError,
    HomeAssistantLoadImporter,
)
from home_assistant_fixtures import (
    aggregate_settings,
    home_assistant_configuration_factory,
    home_assistant_history_payload,
    home_assistant_planning_importer_factory,
    import_and_build,
)

ENTITY_ID = "sensor.household_energy"
SECOND_ENTITY_ID = "sensor.ev_energy"
START = datetime(2026, 1, 1, tzinfo=timezone.utc)
END = datetime(2026, 1, 1, 4, tzinfo=timezone.utc)
NOW = datetime(2026, 1, 1, 5, 30, tzinfo=timezone.utc)


def entity_settings(
    entity_id: str = ENTITY_ID,
    state_class: str = "total_increasing",
    unit: str = "kWh",
    **settings: Any,
) -> dict[str, Any]:
    return {
        "entity_id": entity_id,
        "state_class": state_class,
        "unit": unit,
        **settings,
    }


configuration = home_assistant_configuration_factory(
    household_load=aggregate_settings(add=[entity_settings()])
)


def history_payload(
    entity_id: str = ENTITY_ID,
    readings: list[tuple[str, str]] | None = None,
    unit: str = "kWh",
    state_class: str = "total_increasing",
    last_resets: list[str | None] | None = None,
) -> list[list[dict[str, Any]]]:
    return home_assistant_history_payload(
        entity_id,
        readings,
        unit=unit,
        state_class=state_class,
        last_resets=last_resets,
    )


importer = home_assistant_planning_importer_factory(
    HomeAssistantLoadImporter, configuration
)


def at(timestamp: str) -> datetime:
    return datetime.fromisoformat(f"2026-01-01T{timestamp}+00:00")


def excluded_hours(exclusions: tuple[HourExclusion, ...]) -> dict[int, list[str]]:
    """Map each excluded hour's offset from START to its reasons, in cause order."""
    return {
        int((item.hour_start - START) / timedelta(hours=1)): [
            cause.reason for cause in item.causes
        ]
        for item in exclusions
    }


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
        return httpx.Response(200, json=history_payload())

    provider, client = importer(httpx.MockTransport(handler))
    try:
        data = import_and_build(provider, client, START, END, 3600, now=NOW)
    finally:
        client.close()

    assert data.schema_version == "1"
    assert data.start_time == START
    assert data.interval_minutes == 60
    assert data.load_kw == (1.0, 2.0, 3.0, 4.0)
    assert data.unit == "kW"
    assert data.source.provider == "home-assistant"
    assert data.source.entity_id == "household_load"
    assert data.retrieved_at == NOW
    assert data.latest_observation_at == datetime(2026, 1, 1, 4, tzinfo=timezone.utc)
    request_logs = [
        record
        for record in caplog.records
        if record.getMessage().startswith("event=home_assistant_history_request")
    ]
    assert len(request_logs) == 1
    assert request_logs[0].levelno == logging.DEBUG
    message = request_logs[0].getMessage()
    assert (
        "entity_id=sensor.household_energy "
        "start_time=2025-12-31T23:00:00+00:00 "
        "end_time=2026-01-01T04:00:00+00:00 status=200 duration_ms="
    ) in message
    aggregate_logs = [
        record
        for record in caplog.records
        if record.getMessage().startswith("event=home_assistant_history_aggregate")
    ]
    assert len(aggregate_logs) == 1
    assert aggregate_logs[0].levelno == logging.INFO
    assert "status=success" in aggregate_logs[0].getMessage()


def test_fetch_splits_long_history_into_weekly_chunks_before_normalization(
    caplog: pytest.LogCaptureFixture,
) -> None:
    requested_start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    requested_end = requested_start + timedelta(days=15)
    calls: list[tuple[datetime, datetime]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        chunk_start = datetime.fromisoformat(request.url.path.rsplit("/", 1)[-1])
        chunk_end = datetime.fromisoformat(request.url.params["end_time"])
        calls.append((chunk_start, chunk_end))
        starting_value = int(
            (chunk_start - (requested_start - timedelta(hours=1))).total_seconds()
            / 3600
        )
        chunk_hours = int((chunk_end - chunk_start).total_seconds() / 3600)
        readings = [
            (
                (chunk_start + timedelta(hours=offset)).isoformat(),
                str(starting_value + offset),
            )
            for offset in range(chunk_hours + 1)
        ]
        return httpx.Response(
            200,
            json=history_payload(readings=readings),
        )

    caplog.set_level(logging.INFO)
    provider, client = importer(httpx.MockTransport(handler))
    try:
        data = import_and_build(
            provider,
            client,
            requested_start,
            requested_end,
            history_lookback_seconds=3600,
            now=NOW,
        )
    finally:
        client.close()

    assert calls == [
        (
            datetime(2025, 12, 31, 23, tzinfo=timezone.utc),
            datetime(2026, 1, 7, 23, tzinfo=timezone.utc),
        ),
        (
            datetime(2026, 1, 7, 23, tzinfo=timezone.utc),
            datetime(2026, 1, 14, 23, tzinfo=timezone.utc),
        ),
        (
            datetime(2026, 1, 14, 23, tzinfo=timezone.utc),
            requested_end,
        ),
    ]
    assert all(end - start <= timedelta(days=7) for start, end in calls)
    assert data.start_time == requested_start
    assert len(data.load_kw) == 15 * 24
    assert data.load_kw == (1.0,) * (15 * 24)
    request_logs = [
        record
        for record in caplog.records
        if record.getMessage().startswith("event=home_assistant_history_request")
    ]
    assert request_logs == []
    aggregate_logs = [
        record
        for record in caplog.records
        if record.getMessage().startswith("event=home_assistant_history_aggregate")
    ]
    assert len(aggregate_logs) == 1
    assert aggregate_logs[0].levelno == logging.INFO
    assert "status=success" in aggregate_logs[0].getMessage()
    assert "entity_count=1" in aggregate_logs[0].getMessage()


def test_fetch_uses_one_request_for_a_week_without_lookback() -> None:
    requested_end = START + timedelta(days=7)
    calls: list[tuple[datetime, datetime]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        chunk_start = datetime.fromisoformat(request.url.path.rsplit("/", 1)[-1])
        chunk_end = datetime.fromisoformat(request.url.params["end_time"])
        calls.append((chunk_start, chunk_end))
        return httpx.Response(
            200,
            json=history_payload(
                readings=[
                    (chunk_start.isoformat(), "0"),
                    (chunk_end.isoformat(), "1"),
                ]
            ),
        )

    provider, client = importer(httpx.MockTransport(handler))
    try:
        import_and_build(provider, client, START, requested_end, now=NOW)
    finally:
        client.close()

    assert calls == [(START, requested_end)]


def test_long_history_fetches_every_entity_for_each_chunk() -> None:
    requested_end = START + timedelta(days=8)
    requests: list[tuple[str, datetime, datetime]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        entity_id = request.url.params["filter_entity_id"]
        chunk_start = datetime.fromisoformat(request.url.path.rsplit("/", 1)[-1])
        chunk_end = datetime.fromisoformat(request.url.params["end_time"])
        requests.append((entity_id, chunk_start, chunk_end))
        increment = 1.0 if entity_id == ENTITY_ID else 0.25
        starting_value = 0.0 if chunk_start == START else increment
        ending_value = starting_value + increment
        return httpx.Response(
            200,
            json=history_payload(
                entity_id=entity_id,
                readings=[
                    (chunk_start.isoformat(), str(starting_value)),
                    (chunk_end.isoformat(), str(ending_value)),
                ],
            ),
        )

    provider, client = importer(
        httpx.MockTransport(handler),
        household_load=aggregate_settings(
            add=[entity_settings(ENTITY_ID)],
            subtract=[entity_settings(SECOND_ENTITY_ID)],
        ),
    )
    try:
        data = import_and_build(provider, client, START, requested_end, now=NOW)
    finally:
        client.close()

    assert len(requests) == 4
    assert [entity_id for entity_id, _, _ in requests] == [
        ENTITY_ID,
        ENTITY_ID,
        SECOND_ENTITY_ID,
        SECOND_ENTITY_ID,
    ]
    assert [(start, end) for _, start, end in requests] == [
        (START, START + timedelta(days=7)),
        (START + timedelta(days=7), requested_end),
        (START, START + timedelta(days=7)),
        (START + timedelta(days=7), requested_end),
    ]
    assert data.load_kw[7 * 24 - 1] == 0.75
    assert data.load_kw[-1] == 0.75


def test_counter_decrease_at_chunk_boundary_is_excluded_after_chunks_are_combined() -> (
    None
):
    requested_end = START + timedelta(days=8)
    boundary = START + timedelta(days=7)

    def handler(request: httpx.Request) -> httpx.Response:
        chunk_start = datetime.fromisoformat(request.url.path.rsplit("/", 1)[-1])
        chunk_end = datetime.fromisoformat(request.url.params["end_time"])
        if chunk_start == START:
            readings = [
                (chunk_start.isoformat(), "100"),
                ((chunk_end - timedelta(hours=1)).isoformat(), "107"),
                (chunk_end.isoformat(), "200"),
            ]
        else:
            readings = [
                (chunk_start.isoformat(), "0"),
                ((chunk_start + timedelta(hours=1)).isoformat(), "1"),
                (chunk_end.isoformat(), "2"),
            ]
        return httpx.Response(200, json=history_payload(readings=readings))

    provider, client = importer(httpx.MockTransport(handler))
    try:
        data = import_and_build(provider, client, START, requested_end, now=NOW)
    finally:
        client.close()

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
    requested_end = START + timedelta(days=8)
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 2:
            return httpx.Response(503)
        chunk_start = datetime.fromisoformat(request.url.path.rsplit("/", 1)[-1])
        chunk_end = datetime.fromisoformat(request.url.params["end_time"])
        return httpx.Response(
            200,
            json=history_payload(
                readings=[
                    (chunk_start.isoformat(), "0"),
                    (chunk_end.isoformat(), "1"),
                ]
            ),
        )

    provider, client = importer(httpx.MockTransport(handler))
    try:
        with pytest.raises(HomeAssistantError, match="HTTP 503"):
            import_and_build(provider, client, START, requested_end, now=NOW)
    finally:
        client.close()

    assert calls == 2


def test_total_increasing_observations_need_not_be_hour_aligned() -> None:
    readings = [
        ("2025-12-31T23:15:00+00:00", "0"),
        ("2026-01-01T00:15:00+00:00", "0.5"),
        ("2026-01-01T01:15:00+00:00", "1.5"),
        ("2026-01-01T02:15:00+00:00", "3.5"),
        ("2026-01-01T03:15:00+00:00", "6.5"),
        ("2026-01-01T04:15:00+00:00", "10.5"),
    ]

    provider, client = importer(
        httpx.MockTransport(
            lambda _: httpx.Response(200, json=history_payload(readings=readings))
        )
    )
    try:
        data = import_and_build(provider, client, START, END, 3600, now=NOW)
    finally:
        client.close()

    assert data.load_kw == (0.5, 1.0, 2.0, 3.0)


def test_fetch_uses_available_history_when_requested_start_predates_retention() -> None:
    requested_start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    end = datetime(2026, 1, 1, 4, tzinfo=timezone.utc)

    provider, client = importer(
        httpx.MockTransport(lambda _: httpx.Response(200, json=history_payload()))
    )
    try:
        data = import_and_build(provider, client, requested_start, end, now=NOW)
    finally:
        client.close()

    assert data.start_time == START
    assert data.load_kw == (1.0, 2.0, 3.0, 4.0)


def test_fetch_aligns_entities_to_the_latest_available_start() -> None:
    responses = {
        ENTITY_ID: history_payload(),
        SECOND_ENTITY_ID: history_payload(
            entity_id=SECOND_ENTITY_ID,
            readings=[
                ("2026-01-01T00:30:00+00:00", "0.25"),
                ("2026-01-01T01:30:00+00:00", "0.75"),
                ("2026-01-01T02:30:00+00:00", "1.5"),
                ("2026-01-01T03:30:00+00:00", "2.5"),
            ],
        ),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        entity_id = request.url.params["filter_entity_id"]
        return httpx.Response(200, json=responses[entity_id])

    provider, client = importer(
        httpx.MockTransport(handler),
        household_load=aggregate_settings(
            add=[entity_settings(ENTITY_ID)],
            subtract=[entity_settings(SECOND_ENTITY_ID)],
        ),
    )
    try:
        data = import_and_build(
            provider,
            client,
            datetime(2025, 1, 1, tzinfo=timezone.utc),
            datetime(2026, 1, 1, 4, tzinfo=timezone.utc),
            now=NOW,
        )
    finally:
        client.close()

    assert data.start_time == datetime(2026, 1, 1, 1, tzinfo=timezone.utc)
    assert data.load_kw == (1.5, 2.25, 3.0)


def test_fetch_converts_total_increasing_energy_and_unit() -> None:
    readings = [
        ("2026-01-01T00:00:00+00:00", "1000"),
        ("2026-01-01T01:00:00+00:00", "2000"),
        ("2026-01-01T02:00:00+00:00", "3000"),
        ("2026-01-01T03:00:00+00:00", "4000"),
        ("2026-01-01T04:00:00+00:00", "5000"),
    ]

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=history_payload(
                unit="Wh", readings=readings, state_class="total_increasing"
            ),
        )

    provider, client = importer(
        httpx.MockTransport(handler),
        household_load=aggregate_settings(add=[entity_settings(unit="Wh")]),
    )
    try:
        data = import_and_build(provider, client, START, END, now=NOW)
    finally:
        client.close()

    assert data.load_kw == (1.0, 1.0, 1.0, 1.0)
    assert data.latest_observation_at == datetime(2026, 1, 1, 4, tzinfo=timezone.utc)


def test_fetch_combines_add_and_subtract_entities() -> None:
    responses = {
        ENTITY_ID: history_payload(),
        SECOND_ENTITY_ID: history_payload(
            entity_id=SECOND_ENTITY_ID,
            readings=[
                ("2026-01-01T00:00:00+00:00", "0"),
                ("2026-01-01T01:00:00+00:00", "0.25"),
                ("2026-01-01T02:00:00+00:00", "0.75"),
                ("2026-01-01T03:00:00+00:00", "1.5"),
                ("2026-01-01T04:00:00+00:00", "2.5"),
            ],
        ),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        entity_id = request.url.params["filter_entity_id"]
        return httpx.Response(200, json=responses[entity_id])

    provider, client = importer(
        httpx.MockTransport(handler),
        household_load=aggregate_settings(
            add=[entity_settings(ENTITY_ID)],
            subtract=[entity_settings(SECOND_ENTITY_ID)],
        ),
    )
    try:
        data = import_and_build(provider, client, START, END, now=NOW)
    finally:
        client.close()

    assert data.load_kw == (0.75, 1.5, 2.25, 3.0)
    assert data.source.entity_id == "household_load"


def test_fetch_rejects_instantaneous_power_entities() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=history_payload(
                unit="W", readings=[("2026-01-01T00:00:00+00:00", "500")]
            ),
        )

    provider, client = importer(httpx.MockTransport(handler))
    try:
        with pytest.raises(HomeAssistantError, match="instantaneous power"):
            import_and_build(provider, client, START, END, now=NOW)
    finally:
        client.close()


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
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=history_payload(unit=reported_unit, state_class=reported_state_class),
        )

    provider, client = importer(httpx.MockTransport(handler))
    try:
        data = import_and_build(provider, client, START, END, now=NOW)
    finally:
        client.close()

    assert data.load_kw == (None,) * 4
    assert excluded_hours(data.exclusions) == {hour: [reason] for hour in range(4)}
    cause = data.exclusions[0].causes[0]
    assert cause.entity_id == ENTITY_ID
    assert cause.data_point_count == 5
    assert [(point.timestamp, point.state) for point in cause.data_points] == [
        (START + timedelta(hours=hour), state)
        for hour, state in enumerate(["0", "1", "3", "6", "10"])
    ]
    assert {point.unit for point in cause.data_points} == {reported_unit}


def test_unit_change_mid_history_excludes_hours_until_the_configured_unit_returns() -> (
    None
):
    readings = [
        ("2026-01-01T00:00:00+00:00", "0"),
        ("2026-01-01T01:00:00+00:00", "1"),
        ("2026-01-01T01:30:00+00:00", "1500"),
        ("2026-01-01T02:30:00+00:00", "2"),
        ("2026-01-01T03:30:00+00:00", "3"),
    ]
    payload = history_payload(readings=readings)
    payload[0][2]["attributes"]["unit_of_measurement"] = "Wh"

    provider, client = importer(
        httpx.MockTransport(lambda _: httpx.Response(200, json=payload))
    )
    try:
        data = import_and_build(provider, client, START, END, now=NOW)
    finally:
        client.close()

    assert data.load_kw == (1.0, None, None, 1.0)
    assert excluded_hours(data.exclusions) == {
        1: ["unit_mismatch"],
        2: ["unit_mismatch"],
    }
    assert data.exclusions[0].causes[0].data_points == (
        ExcludedDataPoint(at("01:30:00"), state="1500", unit="Wh"),
    )


def test_missing_unit_excludes_the_hour_of_the_counter_return() -> None:
    payload = history_payload(
        readings=[
            ("2026-01-01T00:00:00+00:00", "0"),
            ("2026-01-01T01:00:00+00:00", "1"),
            ("2026-01-01T02:00:00+00:00", "2"),
        ]
    )
    del payload[0][0]["attributes"]

    provider, client = importer(
        httpx.MockTransport(lambda _: httpx.Response(200, json=payload))
    )
    try:
        data = import_and_build(
            provider, client, START, START + timedelta(hours=2), now=NOW
        )
    finally:
        client.close()

    assert data.load_kw == (None, 1.0)
    assert excluded_hours(data.exclusions) == {0: ["unit_missing"]}
    assert data.exclusions[0].causes[0].data_points == (
        ExcludedDataPoint(START, state="0", unit=None),
    )


@pytest.mark.parametrize("payload", [[], [[]]])
def test_fetch_reports_empty_history(payload: Any) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    provider, client = importer(httpx.MockTransport(handler))
    try:
        with pytest.raises(
            HomeAssistantError, match="returned no history for sensor.household_energy"
        ):
            import_and_build(provider, client, START, END, now=NOW)
    finally:
        client.close()


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
    payload = [[{"state": state, "last_changed": "2026-01-01T00:00:00+00:00"}]]

    provider, client = importer(
        httpx.MockTransport(lambda _: httpx.Response(200, json=payload))
    )
    try:
        data = import_and_build(provider, client, START, END, now=NOW)
    finally:
        client.close()

    assert data.load_kw == (None,) * 4
    assert excluded_hours(data.exclusions) == {hour: [reason] for hour in range(4)}
    cause = data.exclusions[0].causes[0]
    assert cause.entity_id == ENTITY_ID
    # The raw state is kept exactly as Home Assistant reported it.
    assert cause.data_points == (ExcludedDataPoint(START, state=state, unit=None),)


def test_total_increasing_decrease_mid_hour_excludes_the_hour() -> None:
    readings = [
        ("2026-01-01T00:00:00+00:00", "10"),
        ("2026-01-01T00:20:00+00:00", "10.5"),
        ("2026-01-01T00:40:00+00:00", "0.25"),
        ("2026-01-01T01:00:00+00:00", "1.25"),
    ]

    provider, client = importer(
        httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                json=history_payload(readings=readings, state_class="total_increasing"),
            )
        )
    )
    try:
        data = import_and_build(
            provider, client, START, START + timedelta(hours=1), now=NOW
        )
    finally:
        client.close()

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
    readings = [
        ("2026-01-01T00:00:00+00:00", "10"),
        ("2026-01-01T00:30:00+00:00", "unavailable"),
        ("2026-01-01T01:30:00+00:00", "11.5"),
        ("2026-01-01T02:30:00+00:00", "12.5"),
        ("2026-01-01T03:30:00+00:00", "13.5"),
    ]
    provider, client = importer(
        httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                json=history_payload(
                    readings=readings,
                    unit="kWh",
                    state_class="total_increasing",
                ),
            )
        )
    )
    try:
        data = import_and_build(provider, client, START, END, now=NOW)
    finally:
        client.close()

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


def test_total_increasing_unknown_observation_excludes_only_its_return_hour() -> None:
    readings = [
        ("2026-01-01T00:00:00+00:00", "10"),
        ("2026-01-01T00:30:00+00:00", "unknown"),
        ("2026-01-01T01:00:00+00:00", "11.5"),
        ("2026-01-01T02:00:00+00:00", "12.5"),
    ]
    provider, client = importer(
        httpx.MockTransport(
            lambda _: httpx.Response(200, json=history_payload(readings=readings))
        )
    )
    try:
        data = import_and_build(
            provider, client, START, START + timedelta(hours=2), now=NOW
        )
    finally:
        client.close()

    # An observation exactly on the boundary belongs to the earlier hour.
    assert data.load_kw == (None, 1.0)
    assert excluded_hours(data.exclusions) == {0: ["unavailable"]}
    assert data.exclusions[0].causes[0].data_points == (
        ExcludedDataPoint(at("00:30:00"), state="unknown", unit="kWh"),
    )


def test_unavailable_sample_after_the_last_valid_one_excludes_hours_to_the_end() -> (
    None
):
    readings = [
        ("2025-12-31T23:30:00+00:00", "unavailable"),
        ("2026-01-01T00:00:00+00:00", "10"),
        ("2026-01-01T01:00:00+00:00", "11"),
        ("2026-01-01T01:30:00+00:00", "unavailable"),
    ]
    provider, client = importer(
        httpx.MockTransport(
            lambda _: httpx.Response(200, json=history_payload(readings=readings))
        )
    )
    try:
        data = import_and_build(
            provider, client, START, START + timedelta(hours=2), now=NOW
        )
    finally:
        client.close()

    # The sample before the window is not part of it; the trailing one runs to
    # the end of the imported period because no valid observation follows it.
    assert data.load_kw == (1.0, None)
    assert excluded_hours(data.exclusions) == {1: ["unavailable"]}
    assert data.exclusions[0].causes[0].data_points == (
        ExcludedDataPoint(at("01:30:00"), state="unavailable", unit="kWh"),
    )


def test_unavailable_state_in_force_at_the_window_start_excludes_the_first_hour() -> (
    None
):
    readings = [
        ("2025-12-31T23:30:00+00:00", "unavailable"),
        ("2026-01-01T01:00:00+00:00", "11"),
        ("2026-01-01T02:00:00+00:00", "12"),
    ]
    provider, client = importer(
        httpx.MockTransport(
            lambda _: httpx.Response(200, json=history_payload(readings=readings))
        )
    )
    try:
        data = import_and_build(
            provider, client, START, START + timedelta(hours=2), now=NOW
        )
    finally:
        client.close()

    assert data.load_kw == (None, 1.0)
    assert excluded_hours(data.exclusions) == {0: ["unavailable"]}
    # The data point names when Home Assistant recorded the state, not the
    # window start it is carried to.
    assert data.exclusions[0].causes[0].data_points == (
        ExcludedDataPoint(
            datetime(2025, 12, 31, 23, 30, tzinfo=timezone.utc),
            state="unavailable",
            unit="kWh",
        ),
    )


def test_total_increasing_does_not_interpolate_between_observations() -> None:
    readings = [
        ("2026-01-01T00:00:00+00:00", "10"),
        ("2026-01-01T00:30:00+00:00", "10.5"),
        ("2026-01-01T01:30:00+00:00", "11.5"),
    ]
    provider, client = importer(
        httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                json=history_payload(
                    unit="kWh",
                    readings=readings,
                    state_class="total_increasing",
                ),
            )
        ),
        household_load=aggregate_settings(add=[entity_settings()]),
    )
    try:
        data = import_and_build(
            provider, client, START, START + timedelta(hours=2), now=NOW
        )
    finally:
        client.close()

    assert data.load_kw == (0.5, 1.0)
    assert data.exclusions == ()


def test_total_increasing_decrease_across_hours_excludes_both_observation_hours() -> (
    None
):
    readings = [
        ("2026-01-01T00:00:00+00:00", "10"),
        ("2026-01-01T00:30:00+00:00", "10.5"),
        ("2026-01-01T01:30:00+00:00", "1.5"),
    ]
    provider, client = importer(
        httpx.MockTransport(
            lambda _: httpx.Response(200, json=history_payload(readings=readings))
        )
    )
    try:
        data = import_and_build(
            provider, client, START, START + timedelta(hours=2), now=NOW
        )
    finally:
        client.close()

    # Hour 0 is excluded as well: the decrease casts doubt on the counter value
    # at 00:30, which is where the valid step of that hour ended.
    assert data.load_kw == (None, None)
    assert excluded_hours(data.exclusions) == {
        0: ["counter_decrease"],
        1: ["counter_decrease"],
    }


def test_total_decrease_with_a_changed_last_reset_is_still_excluded() -> None:
    readings = [
        ("2026-01-01T00:00:00+00:00", "10"),
        ("2026-01-01T00:20:00+00:00", "10.5"),
        ("2026-01-01T00:40:00+00:00", "0.25"),
        ("2026-01-01T01:00:00+00:00", "1.25"),
    ]
    reset_time = "2026-01-01T00:40:00+00:00"
    provider, client = importer(
        httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                json=history_payload(
                    readings=readings,
                    state_class="total",
                    last_resets=[None, None, reset_time, reset_time],
                ),
            )
        ),
        household_load=aggregate_settings(add=[entity_settings(state_class="total")]),
    )
    try:
        data = import_and_build(
            provider, client, START, START + timedelta(hours=1), now=NOW
        )
    finally:
        client.close()

    assert data.load_kw == (None,)
    assert excluded_hours(data.exclusions) == {
        0: ["last_reset_changed", "counter_decrease", "step_after_decrease"]
    }


def test_total_last_reset_change_excludes_the_hours_of_both_observations() -> None:
    readings = [
        ("2026-01-01T00:00:00+00:00", "10"),
        ("2026-01-01T00:30:00+00:00", "10.5"),
        ("2026-01-01T01:30:00+00:00", "11.5"),
        ("2026-01-01T02:30:00+00:00", "12.5"),
    ]
    reset_time = "2026-01-01T01:30:00+00:00"
    provider, client = importer(
        httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                json=history_payload(
                    readings=readings,
                    state_class="total",
                    last_resets=[None, None, reset_time, reset_time],
                ),
            )
        ),
        household_load=aggregate_settings(add=[entity_settings(state_class="total")]),
    )
    try:
        data = import_and_build(
            provider, client, START, START + timedelta(hours=3), now=NOW
        )
    finally:
        client.close()

    assert data.load_kw == (None, None, 1.0)
    assert excluded_hours(data.exclusions) == {
        0: ["last_reset_changed"],
        1: ["last_reset_changed"],
    }
    assert data.exclusions[0].causes[0].data_points == (
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
    )


def test_total_increasing_drop_to_zero_and_jump_back_lists_every_cause() -> None:
    readings = [
        ("2026-01-01T00:00:00+00:00", "700"),
        ("2026-01-01T00:30:00+00:00", "0"),
        ("2026-01-01T00:40:00+00:00", "700.25"),
        ("2026-01-01T00:50:00+00:00", "700.5"),
    ]
    provider, client = importer(
        httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                json=history_payload(readings=readings, state_class="total_increasing"),
            )
        )
    )
    try:
        data = import_and_build(
            provider, client, START, START + timedelta(hours=1), now=NOW
        )
    finally:
        client.close()

    assert data.load_kw == (None,)
    assert excluded_hours(data.exclusions) == {
        0: ["counter_decrease", "step_after_decrease", "step_above_maximum"]
    }
    assert {cause.entity_id for cause in data.exclusions[0].causes} == {ENTITY_ID}
    jump = data.exclusions[0].causes[2].data_points[0]
    assert (jump.step_kwh, jump.maximum_kwh) == (700.25, 100.0)


def test_total_increasing_transient_spike_excludes_the_hours_of_its_rise_and_fall() -> (
    None
):
    readings = [
        ("2026-01-01T00:00:00+00:00", "0.0182"),
        ("2026-01-01T00:50:00+00:00", "57.2166"),
        ("2026-01-01T01:05:00+00:00", "0.0182"),
        ("2026-01-01T01:30:00+00:00", "0.5"),
        ("2026-01-01T02:30:00+00:00", "1.5"),
    ]
    provider, client = importer(
        httpx.MockTransport(
            lambda _: httpx.Response(200, json=history_payload(readings=readings))
        )
    )
    try:
        data = import_and_build(
            provider, client, START, START + timedelta(hours=3), now=NOW
        )
    finally:
        client.close()

    # The spike stays below the maximum, so it is recognized only by the fall
    # that follows it, and that fall excludes the hour the spike was recorded in.
    assert data.load_kw == (None, None, 1.0)
    assert excluded_hours(data.exclusions) == {
        0: ["counter_decrease"],
        1: ["counter_decrease", "step_after_decrease"],
    }


def test_spike_in_a_subtracted_entity_excludes_the_combined_hour() -> None:
    responses = {
        ENTITY_ID: history_payload(
            readings=[
                ("2026-01-01T00:00:00+00:00", "0"),
                ("2026-01-01T00:30:00+00:00", "1"),
            ]
        ),
        SECOND_ENTITY_ID: history_payload(
            entity_id=SECOND_ENTITY_ID,
            readings=[
                ("2026-01-01T00:00:00+00:00", "0.0182"),
                ("2026-01-01T00:10:38+00:00", "57.2166"),
                ("2026-01-01T00:11:30+00:00", "0.0182"),
            ],
        ),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json=responses[request.url.params["filter_entity_id"]]
        )

    provider, client = importer(
        httpx.MockTransport(handler),
        household_load=aggregate_settings(
            add=[entity_settings(ENTITY_ID)],
            subtract=[entity_settings(SECOND_ENTITY_ID)],
        ),
    )
    try:
        data = import_and_build(
            provider, client, START, START + timedelta(hours=1), now=NOW
        )
    finally:
        client.close()

    # The valid entity's 1 kWh is not returned as if it were the combined load.
    assert data.load_kw == (None,)
    assert excluded_hours(data.exclusions) == {0: ["counter_decrease"]}
    assert data.exclusions[0].causes[0].entity_id == SECOND_ENTITY_ID


def test_step_above_maximum_excludes_the_hour_of_its_later_observation() -> None:
    readings = [
        ("2026-01-01T00:00:00+00:00", "0"),
        ("2026-01-01T00:30:00+00:00", "5"),
        ("2026-01-01T00:40:00+00:00", "20"),
    ]
    provider, client = importer(
        httpx.MockTransport(
            lambda _: httpx.Response(200, json=history_payload(readings=readings))
        ),
        household_load=aggregate_settings(
            add=[entity_settings(maximum_interval_energy_kwh=10)]
        ),
    )
    try:
        data = import_and_build(
            provider, client, START, START + timedelta(hours=1), now=NOW
        )
    finally:
        client.close()

    assert data.load_kw == (None,)
    assert excluded_hours(data.exclusions) == {0: ["step_above_maximum"]}
    cause = data.exclusions[0].causes[0]
    assert cause.entity_id == ENTITY_ID
    assert cause.data_points == (
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
    )


def test_step_above_maximum_leaves_the_hour_of_its_earlier_observation_valid() -> None:
    readings = [
        ("2026-01-01T00:00:00+00:00", "0"),
        ("2026-01-01T00:30:00+00:00", "5"),
        ("2026-01-01T01:30:00+00:00", "20"),
    ]
    provider, client = importer(
        httpx.MockTransport(
            lambda _: httpx.Response(200, json=history_payload(readings=readings))
        ),
        household_load=aggregate_settings(
            add=[entity_settings(maximum_interval_energy_kwh=10)]
        ),
    )
    try:
        data = import_and_build(
            provider, client, START, START + timedelta(hours=2), now=NOW
        )
    finally:
        client.close()

    assert data.load_kw == (5.0, None)
    assert excluded_hours(data.exclusions) == {1: ["step_above_maximum"]}


def test_hour_above_maximum_is_excluded_although_no_single_step_exceeds_it() -> None:
    readings = [
        ("2026-01-01T00:00:00+00:00", "0"),
        ("2026-01-01T00:20:00+00:00", "6"),
        ("2026-01-01T00:40:00+00:00", "12"),
    ]
    provider, client = importer(
        httpx.MockTransport(
            lambda _: httpx.Response(200, json=history_payload(readings=readings))
        ),
        household_load=aggregate_settings(
            add=[entity_settings(maximum_interval_energy_kwh=10)]
        ),
    )
    try:
        data = import_and_build(
            provider, client, START, START + timedelta(hours=1), now=NOW
        )
    finally:
        client.close()

    assert data.load_kw == (None,)
    assert excluded_hours(data.exclusions) == {0: ["hour_above_maximum"]}
    cause = data.exclusions[0].causes[0]
    assert cause.entity_id == ENTITY_ID
    assert cause.data_points == (
        ExcludedDataPoint(START, step_kwh=12.0, maximum_kwh=10.0),
    )


def test_physical_limit_accepts_exact_boundary() -> None:
    readings = [
        ("2026-01-01T00:00:00+00:00", "0"),
        ("2026-01-01T01:00:00+00:00", "10"),
    ]
    provider, client = importer(
        httpx.MockTransport(
            lambda _: httpx.Response(200, json=history_payload(readings=readings))
        ),
        household_load=aggregate_settings(
            add=[entity_settings(maximum_interval_energy_kwh=10)]
        ),
    )
    try:
        data = import_and_build(
            provider, client, START, START + timedelta(hours=1), now=NOW
        )
    finally:
        client.close()

    assert data.load_kw == (10.0,)
    assert data.exclusions == ()


def test_total_decrease_without_last_reset_change_excludes_the_hours_it_touches() -> (
    None
):
    readings = [
        ("2026-01-01T00:00:00+00:00", "10"),
        ("2026-01-01T00:30:00+00:00", "10.5"),
        ("2026-01-01T01:30:00+00:00", "0.25"),
        ("2026-01-01T02:30:00+00:00", "0.75"),
    ]
    provider, client = importer(
        httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                json=history_payload(
                    readings=readings,
                    state_class="total",
                    last_resets=[None, None, None, None],
                ),
            )
        ),
        household_load=aggregate_settings(add=[entity_settings(state_class="total")]),
    )
    try:
        data = import_and_build(
            provider, client, START, START + timedelta(hours=3), now=NOW
        )
    finally:
        client.close()

    assert data.load_kw == (None, None, None)
    assert excluded_hours(data.exclusions) == {
        0: ["counter_decrease"],
        1: ["counter_decrease", "step_after_decrease"],
        2: ["step_after_decrease"],
    }


def test_household_load_excludes_the_hour_of_a_one_watt_hour_counter_dip() -> None:
    """A dip of any size is a decrease; there is no jitter tolerance."""
    readings = [
        ("2026-01-01T00:00:00+00:00", "3280.000"),
        ("2026-01-01T00:20:00+00:00", "3280.294"),
        ("2026-01-01T00:20:12+00:00", "3280.293"),
        ("2026-01-01T00:20:24+00:00", "3280.294"),
        ("2026-01-01T01:00:00+00:00", "3280.600"),
    ]
    provider, client = importer(
        httpx.MockTransport(
            lambda _: httpx.Response(
                200, json=history_payload(readings=readings, state_class="total")
            )
        ),
        household_load=aggregate_settings(add=[entity_settings(state_class="total")]),
    )
    try:
        data = import_and_build(
            provider, client, START, START + timedelta(hours=1), now=NOW
        )
    finally:
        client.close()

    assert data.load_kw == (None,)
    assert excluded_hours(data.exclusions) == {
        0: ["counter_decrease", "step_after_decrease"]
    }
    dip = data.exclusions[0].causes[0].data_points[0]
    assert dip.timestamp == at("00:20:12")
    assert dip.state == "3280.293"
    assert dip.step_kwh == pytest.approx(-0.001, abs=1e-9)


def test_fetch_excludes_negative_combined_load_hours() -> None:
    responses = {
        ENTITY_ID: history_payload(),
        SECOND_ENTITY_ID: history_payload(
            entity_id=SECOND_ENTITY_ID,
            readings=[
                ("2026-01-01T00:00:00+00:00", "0"),
                ("2026-01-01T01:00:00+00:00", "2"),
                ("2026-01-01T02:00:00+00:00", "4"),
                ("2026-01-01T03:00:00+00:00", "6"),
                ("2026-01-01T04:00:00+00:00", "8"),
            ],
        ),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json=responses[request.url.params["filter_entity_id"]]
        )

    provider, client = importer(
        httpx.MockTransport(handler),
        household_load=aggregate_settings(
            add=[entity_settings(SECOND_ENTITY_ID)],
            subtract=[entity_settings(ENTITY_ID)],
        ),
    )
    try:
        data = import_and_build(provider, client, START, END, now=NOW)
    finally:
        client.close()

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
    responses = {
        ENTITY_ID: history_payload(
            ENTITY_ID,
            [
                ("2026-01-01T00:00:00+00:00", "500"),
                ("2026-01-01T00:20:00+00:00", "0.5"),
                ("2026-01-01T00:40:00+00:00", "150.5"),
                ("2026-01-01T01:00:00+00:00", "150.5"),
                ("2026-01-01T02:00:00+00:00", "151.5"),
                ("2026-01-01T03:00:00+00:00", "153.5"),
            ],
        ),
        SECOND_ENTITY_ID: history_payload(
            SECOND_ENTITY_ID,
            [
                ("2026-01-01T00:00:00+00:00", "1000"),
                ("2026-01-01T00:20:00+00:00", "0.1"),
                ("2026-01-01T00:40:00+00:00", "57"),
                ("2026-01-01T01:00:00+00:00", "57"),
                ("2026-01-01T02:00:00+00:00", "57.5"),
                ("2026-01-01T03:00:00+00:00", "58.5"),
            ],
        ),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json=responses[request.url.params["filter_entity_id"]]
        )

    provider, client = importer(
        httpx.MockTransport(handler),
        household_load=aggregate_settings(
            add=[entity_settings(ENTITY_ID)],
            subtract=[entity_settings(SECOND_ENTITY_ID)],
        ),
    )
    try:
        data = import_and_build(
            provider, client, START, START + timedelta(hours=3), now=NOW
        )
    finally:
        client.close()

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
            return httpx.Response(200, json=history_payload())
        return httpx.Response(503)

    provider, client = importer(
        httpx.MockTransport(handler),
        household_load=aggregate_settings(
            add=[entity_settings(ENTITY_ID), entity_settings(SECOND_ENTITY_ID)]
        ),
    )
    try:
        with pytest.raises(HomeAssistantError, match="HTTP 503"):
            import_and_build(provider, client, START, END, now=NOW)
    finally:
        client.close()

    assert calls == 2


def test_fetch_does_not_return_partial_data_when_entity_has_no_history() -> None:
    responses = {
        ENTITY_ID: history_payload(),
        SECOND_ENTITY_ID: [],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json=responses[request.url.params["filter_entity_id"]]
        )

    provider, client = importer(
        httpx.MockTransport(handler),
        household_load=aggregate_settings(
            add=[entity_settings(ENTITY_ID), entity_settings(SECOND_ENTITY_ID)]
        ),
    )
    try:
        with pytest.raises(
            HomeAssistantError, match="returned no history for sensor.ev_energy"
        ):
            import_and_build(provider, client, START, END, now=NOW)
    finally:
        client.close()


def test_fetch_excludes_every_hour_when_an_entity_has_only_invalid_samples() -> None:
    responses = {
        ENTITY_ID: history_payload(),
        SECOND_ENTITY_ID: history_payload(
            entity_id=SECOND_ENTITY_ID,
            readings=[
                ("2026-01-01T00:00:00+00:00", "unavailable"),
                ("2026-01-01T01:00:00+00:00", "unknown"),
            ],
        ),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json=responses[request.url.params["filter_entity_id"]]
        )

    provider, client = importer(
        httpx.MockTransport(handler),
        household_load=aggregate_settings(
            add=[entity_settings(ENTITY_ID), entity_settings(SECOND_ENTITY_ID)]
        ),
    )
    try:
        data = import_and_build(provider, client, START, END, now=NOW)
    finally:
        client.close()

    # The valid entity's hours are not returned as if they were the total load.
    assert data.load_kw == (None,) * 4
    assert excluded_hours(data.exclusions) == {
        hour: ["unavailable"] for hour in range(4)
    }
    cause = data.exclusions[0].causes[0]
    assert cause.entity_id == SECOND_ENTITY_ID
    assert cause.data_point_count == 2
    assert [(point.timestamp, point.state) for point in cause.data_points] == [
        (at("00:00:00"), "unavailable"),
        (at("01:00:00"), "unknown"),
    ]


def test_fetch_reports_http_failures(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(401)

    provider, client = importer(httpx.MockTransport(handler))
    try:
        with pytest.raises(HomeAssistantError, match="authentication failed"):
            import_and_build(provider, client, START, END, now=NOW)
    finally:
        client.close()

    request_logs = [
        record
        for record in caplog.records
        if record.getMessage().startswith("event=home_assistant_history_request")
    ]
    assert len(request_logs) == 1
    assert request_logs[0].levelno == logging.DEBUG
    assert "entity_id=sensor.household_energy" in request_logs[0].getMessage()
    assert "status=401" in request_logs[0].getMessage()
    assert "test-token" not in request_logs[0].getMessage()
    aggregate_logs = [
        record
        for record in caplog.records
        if record.getMessage().startswith("event=home_assistant_history_aggregate")
    ]
    assert len(aggregate_logs) == 1
    assert aggregate_logs[0].levelno == logging.WARNING
    assert "status=failed" in aggregate_logs[0].getMessage()


def test_fetch_reports_timeout(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)

    def handler(_: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out")

    provider, client = importer(httpx.MockTransport(handler))
    try:
        with pytest.raises(HomeAssistantError, match="timed out"):
            import_and_build(provider, client, START, END, now=NOW)
    finally:
        client.close()

    request_logs = [
        record
        for record in caplog.records
        if record.getMessage().startswith("event=home_assistant_history_request")
    ]
    assert len(request_logs) == 1
    assert request_logs[0].levelno == logging.DEBUG
    assert "status=timeout" in request_logs[0].getMessage()
    assert "test-token" not in request_logs[0].getMessage()
    aggregate_logs = [
        record
        for record in caplog.records
        if record.getMessage().startswith("event=home_assistant_history_aggregate")
    ]
    assert len(aggregate_logs) == 1
    assert aggregate_logs[0].levelno == logging.WARNING
    assert "status=failed" in aggregate_logs[0].getMessage()


def test_fetch_reports_malformed_json() -> None:
    provider, client = importer(
        httpx.MockTransport(lambda _: httpx.Response(200, content=b"not-json"))
    )
    try:
        with pytest.raises(HomeAssistantError, match="malformed JSON"):
            import_and_build(provider, client, START, END, now=NOW)
    finally:
        client.close()


def test_freshness_is_a_polling_health_check() -> None:
    provider, client = importer(
        httpx.MockTransport(lambda _: httpx.Response(200, json=history_payload()))
    )
    try:
        data = import_and_build(provider, client, START, END, now=NOW)
        assert provider.is_fresh(data, now=NOW)
        provider.configuration = configuration(max_data_age_seconds=60)
        assert not provider.is_fresh(data, now=NOW)
    finally:
        client.close()


def test_freshness_check_is_disabled_without_a_threshold() -> None:
    provider, client = importer(
        httpx.MockTransport(lambda _: httpx.Response(200, json=history_payload())),
        max_data_age_seconds=None,
    )
    try:
        data = import_and_build(provider, client, START, END, now=NOW)
    finally:
        client.close()

    assert provider.is_fresh(data, now=datetime(2036, 1, 1, tzinfo=timezone.utc))


@pytest.mark.parametrize(
    ("start_time", "end_time", "lookback", "message"),
    [
        (END, START, 3600, "end_time"),
        (
            datetime(2026, 1, 1, 0, 30, tzinfo=timezone.utc),
            END,
            3600,
            "whole hourly",
        ),
        (START, END, -1, "non-negative"),
    ],
)
def test_fetch_validates_requested_period(
    start_time: datetime,
    end_time: datetime,
    lookback: float,
    message: str,
) -> None:
    provider, client = importer(httpx.MockTransport(lambda _: httpx.Response(200)))
    try:
        with pytest.raises(HomeAssistantError, match=message):
            import_and_build(provider, client, start_time, end_time, lookback, now=NOW)
    finally:
        client.close()
