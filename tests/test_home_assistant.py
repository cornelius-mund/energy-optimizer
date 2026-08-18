"""Tests for the Home Assistant household-load importer."""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import pytest

from energy_optimizer.config import HomeAssistantConfiguration
from energy_optimizer.providers.home_assistant import (
    HomeAssistantError,
    HomeAssistantLoadImporter,
)

ENTITY_ID = "sensor.household_energy"
SECOND_ENTITY_ID = "sensor.ev_energy"
START = datetime(2026, 1, 1, tzinfo=timezone.utc)
END = datetime(2026, 1, 1, 4, tzinfo=timezone.utc)
NOW = datetime(2026, 1, 1, 5, 30, tzinfo=timezone.utc)


def configuration(**overrides: Any) -> HomeAssistantConfiguration:
    values: dict[str, Any] = {
        "base_url": "http://homeassistant.test:8123",
        "token": "test-token",
        "household_load_entities": [
            {
                "entity_id": ENTITY_ID,
                "state_class": "total_increasing",
                "unit": "kWh",
                "operation": "add",
            }
        ],
        "timeout_seconds": 5,
        "max_data_age_seconds": 7200,
    }
    values.update(overrides)
    return HomeAssistantConfiguration.model_validate(values)


def history_payload(
    entity_id: str = ENTITY_ID,
    readings: list[tuple[str, str]] | None = None,
    unit: str = "kWh",
    state_class: str = "total_increasing",
    last_resets: list[str | None] | None = None,
) -> list[list[dict[str, Any]]]:
    readings = readings or [
        ("2026-01-01T00:00:00+00:00", "0"),
        ("2026-01-01T01:00:00+00:00", "1"),
        ("2026-01-01T02:00:00+00:00", "3"),
        ("2026-01-01T03:00:00+00:00", "6"),
        ("2026-01-01T04:00:00+00:00", "10"),
    ]
    records: list[dict[str, Any]] = []
    for index, (timestamp, state) in enumerate(readings):
        attributes: dict[str, Any] = {
            "unit_of_measurement": unit,
            "state_class": state_class,
        }
        if last_resets is not None:
            attributes["last_reset"] = last_resets[index]
        records.append(
            {
                "entity_id": entity_id,
                "state": state,
                "last_updated": timestamp,
                "attributes": attributes,
            }
        )
    return [records]


def importer(
    handler: httpx.MockTransport | httpx.BaseTransport,
    **configuration_overrides: Any,
) -> tuple[HomeAssistantLoadImporter, httpx.Client]:
    client = httpx.Client(transport=handler)
    return (
        HomeAssistantLoadImporter(configuration(**configuration_overrides), client),
        client,
    )


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
        data = provider.fetch(START, END, 3600, now=NOW)
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
        data = provider.fetch(
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
        provider.fetch(START, requested_end, now=NOW)
    finally:
        client.close()

    assert calls == [(START, requested_end)]


def test_long_history_fetches_every_entity_for_each_chunk() -> None:
    requested_end = START + timedelta(days=8)
    requests: list[tuple[str, datetime, datetime]] = []
    entities = [
        {
            "entity_id": ENTITY_ID,
            "state_class": "total_increasing",
            "unit": "kWh",
            "operation": "add",
        },
        {
            "entity_id": SECOND_ENTITY_ID,
            "state_class": "total_increasing",
            "unit": "kWh",
            "operation": "subtract",
        },
    ]

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
        household_load_entities=entities,
    )
    try:
        data = provider.fetch(START, requested_end, now=NOW)
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


def test_counter_reset_at_chunk_boundary_is_normalized_after_chunks_are_combined() -> (
    None
):
    requested_end = START + timedelta(days=8)

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
        data = provider.fetch(START, requested_end, now=NOW)
    finally:
        client.close()

    assert data.load_kw[7 * 24 - 2] == 7
    assert data.load_kw[7 * 24 - 1] == 0
    assert data.load_kw[7 * 24] == 1
    assert max(data.load_kw) == 7
    assert data.quality[7 * 24 - 1].reason == "counter_reset"


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
            provider.fetch(START, requested_end, now=NOW)
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
        data = provider.fetch(START, END, 3600, now=NOW)
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
        data = provider.fetch(requested_start, end, now=NOW)
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
        household_load_entities=[
            {
                "entity_id": ENTITY_ID,
                "state_class": "total_increasing",
                "unit": "kWh",
                "operation": "add",
            },
            {
                "entity_id": SECOND_ENTITY_ID,
                "state_class": "total_increasing",
                "unit": "kWh",
                "operation": "subtract",
            },
        ],
    )
    try:
        data = provider.fetch(
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
        household_load_entities=[
            {
                "entity_id": ENTITY_ID,
                "state_class": "total_increasing",
                "unit": "Wh",
                "operation": "add",
            }
        ],
    )
    try:
        data = provider.fetch(START, END, now=NOW)
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
        household_load_entities=[
            {
                "entity_id": ENTITY_ID,
                "state_class": "total_increasing",
                "unit": "kWh",
                "operation": "add",
            },
            {
                "entity_id": SECOND_ENTITY_ID,
                "state_class": "total_increasing",
                "unit": "kWh",
                "operation": "subtract",
            },
        ],
    )
    try:
        data = provider.fetch(START, END, now=NOW)
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
            provider.fetch(START, END, now=NOW)
    finally:
        client.close()


def test_fetch_rejects_incompatible_energy_unit() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=history_payload(unit="Wh"))

    provider, client = importer(httpx.MockTransport(handler))
    try:
        with pytest.raises(HomeAssistantError, match="incompatible"):
            provider.fetch(START, END, now=NOW)
    finally:
        client.close()


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ([], "no household-load history"),
        (
            [[{"state": "unavailable", "last_changed": "2026-01-01T00:00:00+00:00"}]],
            "unavailable",
        ),
        (
            [[{"state": "not-a-number", "last_changed": "2026-01-01T00:00:00+00:00"}]],
            "non-numeric",
        ),
    ],
)
def test_fetch_reports_invalid_history(payload: Any, message: str) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    provider, client = importer(httpx.MockTransport(handler))
    try:
        with pytest.raises(HomeAssistantError, match=message):
            provider.fetch(START, END, now=NOW)
    finally:
        client.close()


def test_total_increasing_reset_mid_hour_starts_a_new_baseline() -> None:
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
        data = provider.fetch(START, START + timedelta(hours=1), now=NOW)
    finally:
        client.close()

    assert data.load_kw == (1.5,)


def test_total_increasing_skips_unavailable_observations_without_fabricating_energy(
    caplog: pytest.LogCaptureFixture,
) -> None:
    readings = [
        ("2026-01-01T00:00:00+00:00", "10"),
        ("2026-01-01T00:30:00+00:00", "unavailable"),
        ("2026-01-01T01:30:00+00:00", "11.5"),
        ("2026-01-01T02:30:00+00:00", "12.5"),
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
        data = provider.fetch(START, START + timedelta(hours=2), now=NOW)
    finally:
        client.close()

    assert data.load_kw == (0.0, 1.5)
    assert "skipped them without assigning energy" in caplog.text
    assert ENTITY_ID in caplog.text


def test_total_increasing_skips_unknown_observations() -> None:
    readings = [
        ("2026-01-01T00:00:00+00:00", "10"),
        ("2026-01-01T00:30:00+00:00", "unknown"),
        ("2026-01-01T01:00:00+00:00", "11.5"),
    ]
    provider, client = importer(
        httpx.MockTransport(
            lambda _: httpx.Response(200, json=history_payload(readings=readings))
        )
    )
    try:
        data = provider.fetch(START, START + timedelta(hours=1), now=NOW)
    finally:
        client.close()

    assert data.load_kw == (1.5,)


def test_skips_unavailable_samples_outside_requested_window() -> None:
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
        data = provider.fetch(START, START + timedelta(hours=2), now=NOW)
    finally:
        client.close()

    assert data.load_kw == (1.0, 0.0)


def test_total_increasing_does_not_interpolate_between_observations() -> None:
    readings = [
        ("2026-01-01T00:00:00+00:00", "10"),
        ("2026-01-01T00:30:00+00:00", "10.5"),
        ("2026-01-01T01:30:00+00:00", "1.5"),
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
        household_load_entities=[
            {
                "entity_id": ENTITY_ID,
                "state_class": "total_increasing",
                "unit": "kWh",
                "operation": "add",
            }
        ],
    )
    try:
        data = provider.fetch(START, START + timedelta(hours=2), now=NOW)
    finally:
        client.close()

    assert data.load_kw == (0.5, 0.0)


def test_total_accepts_a_decrease_only_when_last_reset_changes() -> None:
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
        household_load_entities=[
            {
                "entity_id": ENTITY_ID,
                "state_class": "total",
                "unit": "kWh",
                "operation": "add",
            }
        ],
    )
    try:
        data = provider.fetch(START, START + timedelta(hours=1), now=NOW)
    finally:
        client.close()

    assert data.load_kw == (1.5,)


def test_total_increasing_reset_recovery_does_not_add_counter_magnitude() -> None:
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
        data = provider.fetch(START, START + timedelta(hours=1), now=NOW)
    finally:
        client.close()

    assert data.load_kw == (0.25,)
    assert data.quality[0].status == "suspect"
    assert data.quality[0].reason == "reset_recovery"
    assert data.quality[0].entity_id == ENTITY_ID


def test_total_increasing_transient_spike_is_retracted_when_counter_recovers() -> None:
    readings = [
        ("2026-01-01T00:00:00+00:00", "0.0182"),
        ("2026-01-01T00:10:38+00:00", "57.2166"),
        ("2026-01-01T00:11:30+00:00", "0.0182"),
        ("2026-01-01T00:30:00+00:00", "0.5"),
    ]
    provider, client = importer(
        httpx.MockTransport(
            lambda _: httpx.Response(200, json=history_payload(readings=readings))
        )
    )
    try:
        data = provider.fetch(START, START + timedelta(hours=1), now=NOW)
    finally:
        client.close()

    assert data.load_kw == pytest.approx((0.4818,))
    assert data.quality[0].status == "suspect"
    assert data.quality[0].reason == "transient_counter_spike"
    assert data.quality[0].entity_id == ENTITY_ID


def test_transient_spike_does_not_make_signed_aggregate_negative() -> None:
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
        household_load_entities=[
            {
                "entity_id": ENTITY_ID,
                "state_class": "total_increasing",
                "unit": "kWh",
                "operation": "add",
            },
            {
                "entity_id": SECOND_ENTITY_ID,
                "state_class": "total_increasing",
                "unit": "kWh",
                "operation": "subtract",
            },
        ],
    )
    try:
        data = provider.fetch(START, START + timedelta(hours=1), now=NOW)
    finally:
        client.close()

    assert data.load_kw == pytest.approx((1.0,))
    assert data.quality[0].reason == "transient_counter_spike"


def test_physical_limit_rejects_over_limit_delta_and_marks_interval_suspect() -> None:
    readings = [
        ("2026-01-01T00:00:00+00:00", "0"),
        ("2026-01-01T00:30:00+00:00", "5"),
        ("2026-01-01T00:40:00+00:00", "20"),
    ]
    provider, client = importer(
        httpx.MockTransport(
            lambda _: httpx.Response(200, json=history_payload(readings=readings))
        ),
        household_load_entities=[
            {
                "entity_id": ENTITY_ID,
                "state_class": "total_increasing",
                "unit": "kWh",
                "operation": "add",
                "maximum_interval_energy_kwh": 10,
            }
        ],
    )
    try:
        data = provider.fetch(START, START + timedelta(hours=1), now=NOW)
    finally:
        client.close()

    assert data.load_kw == (5.0,)
    assert data.quality[0].status == "suspect"
    assert data.quality[0].reason == "physical_limit_exceeded"


def test_physical_limit_accepts_exact_boundary() -> None:
    readings = [
        ("2026-01-01T00:00:00+00:00", "0"),
        ("2026-01-01T01:00:00+00:00", "10"),
    ]
    provider, client = importer(
        httpx.MockTransport(
            lambda _: httpx.Response(200, json=history_payload(readings=readings))
        ),
        household_load_entities=[
            {
                "entity_id": ENTITY_ID,
                "state_class": "total_increasing",
                "unit": "kWh",
                "operation": "add",
                "maximum_interval_energy_kwh": 10,
            }
        ],
    )
    try:
        data = provider.fetch(START, START + timedelta(hours=1), now=NOW)
    finally:
        client.close()

    assert data.load_kw == (10.0,)
    assert data.quality == ()


def test_total_rejects_a_decrease_without_last_reset_change() -> None:
    readings = [
        ("2026-01-01T00:00:00+00:00", "10"),
        ("2026-01-01T00:20:00+00:00", "10.5"),
        ("2026-01-01T00:40:00+00:00", "0.25"),
    ]
    provider, client = importer(
        httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                json=history_payload(
                    readings=readings,
                    state_class="total",
                    last_resets=[None, None, None],
                ),
            )
        ),
        household_load_entities=[
            {
                "entity_id": ENTITY_ID,
                "state_class": "total",
                "unit": "kWh",
                "operation": "add",
            }
        ],
    )
    try:
        with pytest.raises(HomeAssistantError, match="last_reset"):
            provider.fetch(START, START + timedelta(hours=1), now=NOW)
    finally:
        client.close()


def test_fetch_rejects_negative_combined_load() -> None:
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
        household_load_entities=[
            {
                "entity_id": ENTITY_ID,
                "state_class": "total_increasing",
                "unit": "kWh",
                "operation": "subtract",
            },
            {
                "entity_id": SECOND_ENTITY_ID,
                "state_class": "total_increasing",
                "unit": "kWh",
                "operation": "add",
            },
        ],
    )
    try:
        with pytest.raises(HomeAssistantError, match="negative"):
            provider.fetch(START, END, now=NOW)
    finally:
        client.close()


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
        household_load_entities=[
            {
                "entity_id": ENTITY_ID,
                "state_class": "total_increasing",
                "unit": "kWh",
                "operation": "add",
            },
            {
                "entity_id": SECOND_ENTITY_ID,
                "state_class": "total_increasing",
                "unit": "kWh",
                "operation": "add",
            },
        ],
    )
    try:
        with pytest.raises(HomeAssistantError, match="HTTP 503"):
            provider.fetch(START, END, now=NOW)
    finally:
        client.close()

    assert calls == 2


def test_fetch_does_not_return_partial_data_when_entity_has_no_usable_history() -> None:
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
        household_load_entities=[
            {
                "entity_id": ENTITY_ID,
                "state_class": "total_increasing",
                "unit": "kWh",
                "operation": "add",
            },
            {
                "entity_id": SECOND_ENTITY_ID,
                "state_class": "total_increasing",
                "unit": "kWh",
                "operation": "add",
            },
        ],
    )
    try:
        with pytest.raises(HomeAssistantError, match="no usable history"):
            provider.fetch(START, END, now=NOW)
    finally:
        client.close()


def test_fetch_reports_http_failures(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(401)

    provider, client = importer(httpx.MockTransport(handler))
    try:
        with pytest.raises(HomeAssistantError, match="authentication failed"):
            provider.fetch(START, END, now=NOW)
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
            provider.fetch(START, END, now=NOW)
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
            provider.fetch(START, END, now=NOW)
    finally:
        client.close()


def test_freshness_is_a_polling_health_check() -> None:
    provider, client = importer(
        httpx.MockTransport(lambda _: httpx.Response(200, json=history_payload()))
    )
    try:
        data = provider.fetch(START, END, now=NOW)
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
        data = provider.fetch(START, END, now=NOW)
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
            provider.fetch(start_time, end_time, lookback, now=NOW)
    finally:
        client.close()
