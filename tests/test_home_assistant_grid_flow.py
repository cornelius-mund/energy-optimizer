"""Tests for the Home Assistant grid-flow importer."""

from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import pytest

from energy_optimizer.exclusions import ExcludedDataPoint
from energy_optimizer.providers.home_assistant import HomeAssistantLoadImporter
from energy_optimizer.providers.home_assistant_grid_flow import (
    HomeAssistantGridFlowImporter,
)
from energy_optimizer.providers.home_assistant_history import (
    HomeAssistantError,
    HomeAssistantHistoryImporter,
)
from home_assistant_fixtures import (
    home_assistant_configuration_factory,
    home_assistant_history_payload,
    home_assistant_planning_importer_factory,
    import_and_build,
)

ENTITY_ID = "sensor.grid_import"
EXPORT_ENTITY_ID = "sensor.grid_export"
START = datetime(2026, 1, 1, tzinfo=timezone.utc)
END = datetime(2026, 1, 1, 4, tzinfo=timezone.utc)
NOW = datetime(2026, 1, 1, 5, 30, tzinfo=timezone.utc)


configuration = home_assistant_configuration_factory(
    grid_import_entities=[
        {
            "entity_id": ENTITY_ID,
            "state_class": "total_increasing",
            "unit": "kWh",
            "operation": "add",
        }
    ],
    grid_export_entities=[
        {
            "entity_id": EXPORT_ENTITY_ID,
            "state_class": "total_increasing",
            "unit": "kWh",
            "operation": "add",
        }
    ],
)


history_payload = home_assistant_history_payload


def standard_payload(entity_id: str) -> list[list[dict[str, Any]]]:
    return history_payload(
        entity_id,
        [
            ("2026-01-01T00:00:00+00:00", "0"),
            ("2026-01-01T01:00:00+00:00", "1"),
            ("2026-01-01T02:00:00+00:00", "3"),
            ("2026-01-01T03:00:00+00:00", "6"),
            ("2026-01-01T04:00:00+00:00", "10"),
        ],
    )


importer = home_assistant_planning_importer_factory(
    HomeAssistantGridFlowImporter, configuration
)


def test_fetch_normalizes_import_and_export_entities() -> None:
    responses = {
        ENTITY_ID: standard_payload(ENTITY_ID),
        EXPORT_ENTITY_ID: history_payload(
            EXPORT_ENTITY_ID,
            [
                ("2026-01-01T00:00:00+00:00", "0"),
                ("2026-01-01T01:00:00+00:00", "0.5"),
                ("2026-01-01T02:00:00+00:00", "1.5"),
                ("2026-01-01T03:00:00+00:00", "3"),
                ("2026-01-01T04:00:00+00:00", "5"),
            ],
        ),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        entity_id = request.url.params["filter_entity_id"]
        return httpx.Response(200, json=responses[entity_id])

    provider, client = importer(httpx.MockTransport(handler))
    try:
        data = import_and_build(provider, client, START, END, now=NOW)
    finally:
        client.close()

    assert data.start_time == START
    assert data.import_kw == (1.0, 2.0, 3.0, 4.0)
    assert data.export_kw == (0.5, 1.0, 1.5, 2.0)
    assert data.unit == "kW"
    assert data.source.provider == "home-assistant"
    assert data.source.entity_id == "grid_flow"
    assert data.retrieved_at == NOW
    assert data.latest_observation_at == datetime(2026, 1, 1, 4, tzinfo=timezone.utc)
    assert data.exclusions == ()


def test_household_load_and_grid_flow_share_one_import_of_a_reused_entity() -> None:
    shared_entity = "sensor.main_grid_total_in"
    export_entity = "sensor.main_grid_total_out"
    shared_configuration = home_assistant_configuration_factory(
        household_load_entities=[
            {
                "entity_id": shared_entity,
                "state_class": "total_increasing",
                "unit": "kWh",
                "operation": "add",
            }
        ],
        grid_import_entities=[
            {
                "entity_id": shared_entity,
                "state_class": "total_increasing",
                "unit": "kWh",
                "operation": "add",
            }
        ],
        grid_export_entities=[
            {
                "entity_id": export_entity,
                "state_class": "total_increasing",
                "unit": "kWh",
                "operation": "add",
            }
        ],
    )()
    responses = {
        shared_entity: standard_payload(shared_entity),
        export_entity: standard_payload(export_entity),
    }

    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.params["filter_entity_id"])
        return httpx.Response(
            200, json=responses[request.url.params["filter_entity_id"]]
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        load_plan = HomeAssistantLoadImporter(shared_configuration).plan(
            START, END, now=NOW
        )
        grid_plan = HomeAssistantGridFlowImporter(shared_configuration).plan(
            START, END, now=NOW
        )
        history = HomeAssistantHistoryImporter(
            shared_configuration, client
        ).import_history(load_plan.needs + grid_plan.needs)
        household_load = load_plan.build(history)
        grid_flow = grid_plan.build(history)
    finally:
        client.close()

    # The entity that both records read is downloaded once, not once per record.
    assert requested == [shared_entity, export_entity]
    assert household_load.load_kw == (1.0, 2.0, 3.0, 4.0)
    assert grid_flow.import_kw == (1.0, 2.0, 3.0, 4.0)
    assert grid_flow.export_kw == (1.0, 2.0, 3.0, 4.0)


def test_fetch_supports_multiple_signed_entities_per_channel() -> None:
    second_import = "sensor.grid_import_submeter"
    responses = {
        ENTITY_ID: standard_payload(ENTITY_ID),
        second_import: history_payload(
            second_import,
            [
                ("2026-01-01T00:00:00+00:00", "0"),
                ("2026-01-01T01:00:00+00:00", "0.25"),
                ("2026-01-01T02:00:00+00:00", "0.75"),
                ("2026-01-01T03:00:00+00:00", "1.5"),
                ("2026-01-01T04:00:00+00:00", "2.5"),
            ],
        ),
        EXPORT_ENTITY_ID: standard_payload(EXPORT_ENTITY_ID),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json=responses[request.url.params["filter_entity_id"]]
        )

    provider, client = importer(
        httpx.MockTransport(handler),
        grid_import_entities=[
            {
                "entity_id": ENTITY_ID,
                "state_class": "total_increasing",
                "unit": "kWh",
                "operation": "add",
            },
            {
                "entity_id": second_import,
                "state_class": "total_increasing",
                "unit": "kWh",
                "operation": "subtract",
            },
        ],
    )
    try:
        data = import_and_build(provider, client, START, END, now=NOW)
    finally:
        client.close()

    assert data.import_kw == (0.75, 1.5, 2.25, 3.0)
    assert data.export_kw == (1.0, 2.0, 3.0, 4.0)


def test_fetch_excludes_negative_combined_hours_in_both_channels() -> None:
    second_import = "sensor.grid_import_submeter"
    responses = {
        # Steps of 0.5, 2.5, 0.5, and 2.5 kWh.
        ENTITY_ID: history_payload(
            ENTITY_ID,
            [
                ("2026-01-01T00:00:00+00:00", "0"),
                ("2026-01-01T01:00:00+00:00", "0.5"),
                ("2026-01-01T02:00:00+00:00", "3"),
                ("2026-01-01T03:00:00+00:00", "3.5"),
                ("2026-01-01T04:00:00+00:00", "6"),
            ],
        ),
        # Steps of 1 kWh, subtracted.
        second_import: history_payload(
            second_import,
            [
                ("2026-01-01T00:00:00+00:00", "0"),
                ("2026-01-01T01:00:00+00:00", "1"),
                ("2026-01-01T02:00:00+00:00", "2"),
                ("2026-01-01T03:00:00+00:00", "3"),
                ("2026-01-01T04:00:00+00:00", "4"),
            ],
        ),
        EXPORT_ENTITY_ID: standard_payload(EXPORT_ENTITY_ID),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json=responses[request.url.params["filter_entity_id"]]
        )

    provider, client = importer(
        httpx.MockTransport(handler),
        grid_import_entities=[
            {
                "entity_id": ENTITY_ID,
                "state_class": "total_increasing",
                "unit": "kWh",
                "operation": "add",
            },
            {
                "entity_id": second_import,
                "state_class": "total_increasing",
                "unit": "kWh",
                "operation": "subtract",
            },
        ],
    )
    try:
        data = import_and_build(provider, client, START, END, now=NOW)
    finally:
        client.close()

    # The combined hours 0 and 2 would be -0.5 kWh. They are excluded, not
    # clamped to zero, and the intact export of those hours is excluded with them.
    assert data.import_kw == (None, 1.5, None, 1.5)
    assert data.export_kw == (None, 2.0, None, 4.0)
    assert [item.hour_start for item in data.exclusions] == [
        START,
        START + timedelta(hours=2),
    ]
    for item in data.exclusions:
        (cause,) = item.causes
        assert cause.reason == "combined_negative"
        assert cause.entity_id is None
        assert cause.message.startswith(
            "The combined grid import energy is negative (-0.5 kWh) in the hour "
            f"starting {item.hour_start.isoformat()}"
        )
        assert cause.data_points == (
            ExcludedDataPoint(item.hour_start, entity_id=ENTITY_ID, step_kwh=0.5),
            ExcludedDataPoint(item.hour_start, entity_id=second_import, step_kwh=-1.0),
        )
        assert cause.data_point_count == 2


@pytest.mark.parametrize("flagged_channel", ["import", "export"])
def test_an_hour_excluded_in_one_channel_is_excluded_in_both(
    flagged_channel: str,
) -> None:
    submeter_id = "sensor.grid_submeter"
    flagged_id = ENTITY_ID if flagged_channel == "import" else EXPORT_ENTITY_ID
    ordinary_id = EXPORT_ENTITY_ID if flagged_channel == "import" else ENTITY_ID
    responses = {
        # The counter falls at 02:00, so the hours of both observations of that
        # step and of the step after it are excluded. Only 03:00 to 04:00 counts.
        flagged_id: history_payload(
            flagged_id,
            [
                ("2026-01-01T00:00:00+00:00", "0"),
                ("2026-01-01T01:00:00+00:00", "1"),
                ("2026-01-01T02:00:00+00:00", "0.5"),
                ("2026-01-01T03:00:00+00:00", "3.5"),
                ("2026-01-01T04:00:00+00:00", "4.5"),
            ],
        ),
        # Subtracting it from the flagged entity would make hour 0 negative if
        # the flagged entity contributed to it.
        submeter_id: history_payload(
            submeter_id,
            [
                ("2026-01-01T00:00:00+00:00", "0"),
                ("2026-01-01T01:00:00+00:00", "0.25"),
                ("2026-01-01T02:00:00+00:00", "0.5"),
                ("2026-01-01T03:00:00+00:00", "0.75"),
                ("2026-01-01T04:00:00+00:00", "1"),
            ],
        ),
        ordinary_id: history_payload(
            ordinary_id,
            [
                ("2026-01-01T00:00:00+00:00", "0"),
                ("2026-01-01T01:00:00+00:00", "1"),
                ("2026-01-01T02:00:00+00:00", "2"),
                ("2026-01-01T03:00:00+00:00", "4"),
                ("2026-01-01T04:00:00+00:00", "6"),
            ],
        ),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json=responses[request.url.params["filter_entity_id"]]
        )

    provider, client = importer(
        httpx.MockTransport(handler),
        **{
            f"grid_{flagged_channel}_entities": [
                {
                    "entity_id": flagged_id,
                    "state_class": "total_increasing",
                    "unit": "kWh",
                    "operation": "add",
                },
                {
                    "entity_id": submeter_id,
                    "state_class": "total_increasing",
                    "unit": "kWh",
                    "operation": "subtract",
                },
            ]
        },
    )
    try:
        data = import_and_build(provider, client, START, END, now=NOW)
    finally:
        client.close()

    flagged, ordinary = (
        (data.import_kw, data.export_kw)
        if flagged_channel == "import"
        else (data.export_kw, data.import_kw)
    )
    assert flagged == (None, None, None, 0.75)
    assert ordinary == (None, None, None, 2.0)
    assert [
        (item.hour_start, [cause.reason for cause in item.causes])
        for item in data.exclusions
    ] == [
        (START, ["counter_decrease"]),
        (START + timedelta(hours=1), ["counter_decrease", "step_after_decrease"]),
        (START + timedelta(hours=2), ["step_after_decrease"]),
    ]
    # Only the entity that was excluded is blamed, never a combined hour.
    assert {cause.entity_id for item in data.exclusions for cause in item.causes} == {
        flagged_id
    }
    (decrease,) = data.exclusions[0].causes
    assert decrease.data_points == (
        ExcludedDataPoint(
            START + timedelta(hours=2),
            state="0.5",
            unit="kWh",
            previous_timestamp=START + timedelta(hours=1),
            previous_value=1.0,
            value=0.5,
            step_kwh=-0.5,
            maximum_kwh=100.0,
        ),
    )


def test_an_unavailable_sample_excludes_its_hour_in_both_channels() -> None:
    responses = {
        ENTITY_ID: history_payload(
            ENTITY_ID,
            [
                ("2026-01-01T00:00:00+00:00", "0"),
                ("2026-01-01T01:00:00+00:00", "1"),
                ("2026-01-01T01:30:00+00:00", "unavailable"),
                ("2026-01-01T02:00:00+00:00", "3"),
                ("2026-01-01T03:00:00+00:00", "6"),
                ("2026-01-01T04:00:00+00:00", "10"),
            ],
        ),
        EXPORT_ENTITY_ID: standard_payload(EXPORT_ENTITY_ID),
    }
    provider, client = importer(
        httpx.MockTransport(
            lambda request: httpx.Response(
                200, json=responses[request.url.params["filter_entity_id"]]
            )
        )
    )
    try:
        data = import_and_build(provider, client, START, END, now=NOW)
    finally:
        client.close()

    # The counter is unknown from 01:30 until it is read again at 02:00, so the
    # 3 kWh of the gap is never attributed to a single hour.
    assert data.import_kw == (1.0, None, 3.0, 4.0)
    assert data.export_kw == (1.0, None, 3.0, 4.0)
    (excluded,) = data.exclusions
    assert excluded.hour_start == START + timedelta(hours=1)
    (cause,) = excluded.causes
    assert (cause.reason, cause.entity_id) == ("unavailable", ENTITY_ID)
    assert cause.data_points == (
        ExcludedDataPoint(START + timedelta(hours=1, minutes=30), "unavailable", "kWh"),
    )
    assert cause.data_point_count == 1


def test_a_trailing_outage_excludes_every_hour_to_the_end_and_ages_the_data() -> None:
    responses = {
        ENTITY_ID: history_payload(
            ENTITY_ID,
            [
                ("2026-01-01T00:00:00+00:00", "0"),
                ("2026-01-01T01:00:00+00:00", "1"),
                ("2026-01-01T02:00:00+00:00", "3"),
                ("2026-01-01T02:30:00+00:00", "unavailable"),
            ],
        ),
        EXPORT_ENTITY_ID: standard_payload(EXPORT_ENTITY_ID),
    }
    provider, client = importer(
        httpx.MockTransport(
            lambda request: httpx.Response(
                200, json=responses[request.url.params["filter_entity_id"]]
            )
        )
    )
    try:
        data = import_and_build(provider, client, START, END, now=NOW)
    finally:
        client.close()

    assert data.import_kw == (1.0, 2.0, None, None)
    assert data.export_kw == (1.0, 2.0, None, None)
    assert [item.hour_start for item in data.exclusions] == [
        START + timedelta(hours=2),
        START + timedelta(hours=3),
    ]
    assert all(
        [cause.reason for cause in item.causes] == ["unavailable"]
        for item in data.exclusions
    )
    # The freshest usable observation is the last valid one, not the outage.
    assert data.latest_observation_at == START + timedelta(hours=2)


def test_fetch_aligns_channels_to_the_latest_available_start() -> None:
    responses = {
        ENTITY_ID: standard_payload(ENTITY_ID),
        EXPORT_ENTITY_ID: history_payload(
            EXPORT_ENTITY_ID,
            [
                ("2026-01-01T00:30:00+00:00", "0"),
                ("2026-01-01T01:30:00+00:00", "1"),
                ("2026-01-01T02:30:00+00:00", "3"),
                ("2026-01-01T03:30:00+00:00", "6"),
            ],
        ),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json=responses[request.url.params["filter_entity_id"]]
        )

    provider, client = importer(httpx.MockTransport(handler))
    try:
        data = import_and_build(provider, client, START, END, now=NOW)
    finally:
        client.close()

    assert data.start_time == datetime(2026, 1, 1, 1, tzinfo=timezone.utc)
    assert data.import_kw == (2.0, 3.0, 4.0)
    assert data.export_kw == (1.0, 2.0, 3.0)


def test_fetch_fails_without_returning_partial_data_when_one_channel_fails() -> None:
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(200, json=standard_payload(ENTITY_ID))
        return httpx.Response(503)

    provider, client = importer(httpx.MockTransport(handler))
    try:
        with pytest.raises(HomeAssistantError, match="HTTP 503"):
            import_and_build(provider, client, START, END, now=NOW)
    finally:
        client.close()

    assert calls == 2


def test_freshness_uses_the_oldest_channel_observation() -> None:
    responses = {
        ENTITY_ID: standard_payload(ENTITY_ID),
        EXPORT_ENTITY_ID: history_payload(
            EXPORT_ENTITY_ID,
            [
                ("2026-01-01T00:00:00+00:00", "0"),
                ("2026-01-01T01:00:00+00:00", "0.5"),
                ("2026-01-01T02:00:00+00:00", "1"),
                ("2026-01-01T03:00:00+00:00", "1.5"),
                ("2026-01-01T03:30:00+00:00", "2"),
            ],
        ),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json=responses[request.url.params["filter_entity_id"]]
        )

    provider, client = importer(httpx.MockTransport(handler), max_data_age_seconds=90)
    try:
        data = import_and_build(provider, client, START, END, now=NOW)
    finally:
        client.close()

    assert data.latest_observation_at == datetime(
        2026, 1, 1, 3, 30, tzinfo=timezone.utc
    )
    assert not provider.is_fresh(data, now=NOW)


def test_fetch_rejects_instantaneous_power_channel() -> None:
    provider, client = importer(
        httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json=history_payload(
                    request.url.params["filter_entity_id"],
                    [("2026-01-01T00:00:00+00:00", "500")],
                    unit="W",
                ),
            )
        )
    )
    try:
        with pytest.raises(HomeAssistantError, match="instantaneous power"):
            import_and_build(provider, client, START, END, now=NOW)
    finally:
        client.close()


def test_grid_flow_excludes_the_hour_of_a_total_counter_dip_without_last_reset() -> (
    None
):
    """Regression test for issues #179 and #183 on both grid legs.

    A dip of 1 Wh is a decrease like any other: nothing is tolerated or repaired.
    """
    responses = {
        ENTITY_ID: history_payload(
            ENTITY_ID,
            [
                ("2026-01-01T00:00:00+00:00", "100.000"),
                ("2026-01-01T00:30:00+00:00", "100.500"),
                ("2026-01-01T00:30:12+00:00", "100.499"),
                ("2026-01-01T00:30:24+00:00", "100.500"),
                ("2026-01-01T01:00:00+00:00", "101.000"),
            ],
            state_class="total",
        ),
        EXPORT_ENTITY_ID: history_payload(
            EXPORT_ENTITY_ID,
            [
                ("2026-01-01T00:00:00+00:00", "50.000"),
                ("2026-01-01T00:30:00+00:00", "50.250"),
                ("2026-01-01T00:30:12+00:00", "50.249"),
                ("2026-01-01T00:30:24+00:00", "50.250"),
                ("2026-01-01T01:00:00+00:00", "50.500"),
            ],
            state_class="total",
        ),
    }
    provider, client = importer(
        httpx.MockTransport(
            lambda request: httpx.Response(
                200, json=responses[request.url.params["filter_entity_id"]]
            )
        ),
        grid_import_entities=[
            {
                "entity_id": ENTITY_ID,
                "state_class": "total",
                "unit": "kWh",
                "operation": "add",
            }
        ],
        grid_export_entities=[
            {
                "entity_id": EXPORT_ENTITY_ID,
                "state_class": "total",
                "unit": "kWh",
                "operation": "add",
            }
        ],
    )
    try:
        data = import_and_build(
            provider, client, START, START + timedelta(hours=1), now=NOW
        )
    finally:
        client.close()

    assert data.import_kw == (None,)
    assert data.export_kw == (None,)
    (excluded,) = data.exclusions
    assert excluded.hour_start == START
    # Each channel explains the hour: the dip, then the step directly after it.
    assert [(cause.entity_id, cause.reason) for cause in excluded.causes] == [
        (ENTITY_ID, "counter_decrease"),
        (ENTITY_ID, "step_after_decrease"),
        (EXPORT_ENTITY_ID, "counter_decrease"),
        (EXPORT_ENTITY_ID, "step_after_decrease"),
    ]
    dip = excluded.causes[0].data_points[0]
    assert dip.timestamp == START + timedelta(minutes=30, seconds=12)
    assert (dip.previous_value, dip.value) == (100.5, 100.499)
    assert dip.step_kwh == pytest.approx(-0.001, abs=1e-9)
    assert data.quality == ()
