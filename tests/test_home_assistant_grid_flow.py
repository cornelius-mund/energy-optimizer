"""Tests for the Home Assistant grid-flow importer."""

from collections.abc import Callable, Mapping, Sequence
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
from energy_optimizer.providers.interfaces import GridFlowData
from home_assistant_fixtures import (
    Readings,
    aggregate_settings,
    home_assistant_configuration_factory,
    home_assistant_planning_importer_factory,
    import_and_build,
)
from home_assistant_fixtures import home_assistant_history_payload as history_payload

ENTITY_ID = "sensor.grid_import"
EXPORT_ENTITY_ID = "sensor.grid_export"
SUBMETER_ID = "sensor.grid_import_submeter"
START = datetime(2026, 1, 1, tzinfo=timezone.utc)
END = datetime(2026, 1, 1, 4, tzinfo=timezone.utc)
NOW = datetime(2026, 1, 1, 5, 30, tzinfo=timezone.utc)

Handler = Callable[[httpx.Request], httpx.Response]


def channel(
    add: str, subtract: Sequence[str] = (), state_class: str = "total_increasing"
) -> dict[str, Any]:
    """Return the settings of one grid channel that adds and subtracts entities."""
    return aggregate_settings(
        add=[{"entity_id": add, "state_class": state_class, "unit": "kWh"}],
        subtract=[
            {"entity_id": entity_id, "state_class": state_class, "unit": "kWh"}
            for entity_id in subtract
        ],
    )


configuration = home_assistant_configuration_factory(
    grid_import=channel(ENTITY_ID), grid_export=channel(EXPORT_ENTITY_ID)
)

importer = home_assistant_planning_importer_factory(
    HomeAssistantGridFlowImporter, configuration
)


def hour(index: int) -> datetime:
    return START + timedelta(hours=index)


def readings(*points: tuple[str, str]) -> Readings:
    """Return ``(time, state)`` points as readings stamped on the start day."""
    return [
        (datetime.fromisoformat(f"2026-01-01T{time}+00:00").isoformat(), state)
        for time, state in points
    ]


def hourly(*states: str, minutes: int = 0) -> Readings:
    """Return one reading per hour from 00:00, all shifted by ``minutes``."""
    return [
        ((hour(index) + timedelta(minutes=minutes)).isoformat(), state)
        for index, state in enumerate(states)
    ]


def standard_readings() -> Readings:
    """Return the hourly readings of a counter that yields 1, 2, 3, then 4 kWh."""
    return hourly("0", "1", "3", "6", "10")


def respond_by_entity(
    readings_by_entity: Mapping[str, Readings], **payload_options: Any
) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        entity_id = request.url.params["filter_entity_id"]
        payload = history_payload(
            entity_id, readings_by_entity[entity_id], **payload_options
        )
        return httpx.Response(200, json=payload)

    return handler


def build_grid_flow(
    handler: Handler, end_time: datetime = END, **settings: Any
) -> GridFlowData:
    """Import and build the grid flow, closing the client afterwards."""
    provider, client = importer(httpx.MockTransport(handler), **settings)
    try:
        return import_and_build(provider, client, START, end_time, now=NOW)
    finally:
        client.close()


def test_fetch_normalizes_import_and_export_entities() -> None:
    data = build_grid_flow(
        respond_by_entity(
            {
                ENTITY_ID: standard_readings(),
                EXPORT_ENTITY_ID: hourly("0", "0.5", "1.5", "3", "5"),
            }
        )
    )

    assert data.start_time == START
    assert data.import_kw == (1.0, 2.0, 3.0, 4.0)
    assert data.export_kw == (0.5, 1.0, 1.5, 2.0)
    assert data.unit == "kW"
    assert data.source.provider == "home-assistant"
    assert data.source.entity_id == "grid_flow"
    assert data.retrieved_at == NOW
    assert data.latest_observation_at == END
    assert data.exclusions == ()


def test_household_load_and_grid_flow_share_one_import_of_a_reused_entity() -> None:
    shared_entity = "sensor.main_grid_total_in"
    export_entity = "sensor.main_grid_total_out"
    shared_configuration = home_assistant_configuration_factory(
        household_load=channel(shared_entity),
        grid_import=channel(shared_entity),
        grid_export=channel(export_entity),
    )()
    answer = respond_by_entity(
        {shared_entity: standard_readings(), export_entity: standard_readings()}
    )
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.params["filter_entity_id"])
        return answer(request)

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
    data = build_grid_flow(
        respond_by_entity(
            {
                ENTITY_ID: standard_readings(),
                SUBMETER_ID: hourly("0", "0.25", "0.75", "1.5", "2.5"),
                EXPORT_ENTITY_ID: standard_readings(),
            }
        ),
        grid_import=channel(ENTITY_ID, [SUBMETER_ID]),
    )

    assert data.import_kw == (0.75, 1.5, 2.25, 3.0)
    assert data.export_kw == (1.0, 2.0, 3.0, 4.0)


def test_fetch_excludes_negative_combined_hours_in_both_channels() -> None:
    data = build_grid_flow(
        respond_by_entity(
            {
                # Steps of 0.5, 2.5, 0.5, and 2.5 kWh.
                ENTITY_ID: hourly("0", "0.5", "3", "3.5", "6"),
                # Steps of 1 kWh, subtracted.
                SUBMETER_ID: hourly("0", "1", "2", "3", "4"),
                EXPORT_ENTITY_ID: standard_readings(),
            }
        ),
        grid_import=channel(ENTITY_ID, [SUBMETER_ID]),
    )

    # The combined hours 0 and 2 would be -0.5 kWh. They are excluded, not
    # clamped to zero, and the intact export of those hours is excluded with them.
    assert data.import_kw == (None, 1.5, None, 1.5)
    assert data.export_kw == (None, 2.0, None, 4.0)
    assert [item.hour_start for item in data.exclusions] == [START, hour(2)]
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
            ExcludedDataPoint(item.hour_start, entity_id=SUBMETER_ID, step_kwh=-1.0),
        )
        assert cause.data_point_count == 2


@pytest.mark.parametrize("flagged_channel", ["import", "export"])
def test_an_hour_excluded_in_one_channel_is_excluded_in_both(
    flagged_channel: str,
) -> None:
    submeter_id = "sensor.grid_submeter"
    flagged_id = ENTITY_ID if flagged_channel == "import" else EXPORT_ENTITY_ID
    ordinary_id = EXPORT_ENTITY_ID if flagged_channel == "import" else ENTITY_ID
    settings: dict[str, Any] = {
        f"grid_{flagged_channel}": channel(flagged_id, [submeter_id])
    }

    data = build_grid_flow(
        respond_by_entity(
            {
                # The counter falls at 02:00, so the hours of both observations of
                # that step and of the step after it are excluded. Only 03:00 to
                # 04:00 counts.
                flagged_id: hourly("0", "1", "0.5", "3.5", "4.5"),
                # Subtracting it from the flagged entity would make hour 0 negative
                # if the flagged entity contributed to it.
                submeter_id: hourly("0", "0.25", "0.5", "0.75", "1"),
                ordinary_id: hourly("0", "1", "2", "4", "6"),
            }
        ),
        **settings,
    )

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
        (hour(1), ["counter_decrease", "step_after_decrease"]),
        (hour(2), ["step_after_decrease"]),
    ]
    # Only the entity that was excluded is blamed, never a combined hour.
    assert {cause.entity_id for item in data.exclusions for cause in item.causes} == {
        flagged_id
    }
    (decrease,) = data.exclusions[0].causes
    assert decrease.data_points == (
        ExcludedDataPoint(
            hour(2),
            state="0.5",
            unit="kWh",
            previous_timestamp=hour(1),
            previous_value=1.0,
            value=0.5,
            step_kwh=-0.5,
            maximum_kwh=100.0,
        ),
    )


def test_an_unavailable_sample_excludes_its_hour_in_both_channels() -> None:
    data = build_grid_flow(
        respond_by_entity(
            {
                ENTITY_ID: readings(
                    ("00:00", "0"),
                    ("01:00", "1"),
                    ("01:30", "unavailable"),
                    ("02:00", "3"),
                    ("03:00", "6"),
                    ("04:00", "10"),
                ),
                EXPORT_ENTITY_ID: standard_readings(),
            }
        )
    )

    # The counter is unknown from 01:30 until it is read again at 02:00, so the
    # 3 kWh of the gap is never attributed to a single hour.
    assert data.import_kw == (1.0, None, 3.0, 4.0)
    assert data.export_kw == (1.0, None, 3.0, 4.0)
    (excluded,) = data.exclusions
    assert excluded.hour_start == hour(1)
    (cause,) = excluded.causes
    assert (cause.reason, cause.entity_id) == ("unavailable", ENTITY_ID)
    assert cause.data_points == (
        ExcludedDataPoint(START + timedelta(hours=1, minutes=30), "unavailable", "kWh"),
    )
    assert cause.data_point_count == 1


def test_a_trailing_outage_excludes_every_hour_to_the_end_and_ages_the_data() -> None:
    data = build_grid_flow(
        respond_by_entity(
            {
                ENTITY_ID: readings(
                    ("00:00", "0"),
                    ("01:00", "1"),
                    ("02:00", "3"),
                    ("02:30", "unavailable"),
                ),
                EXPORT_ENTITY_ID: standard_readings(),
            }
        )
    )

    assert data.import_kw == (1.0, 2.0, None, None)
    assert data.export_kw == (1.0, 2.0, None, None)
    assert [item.hour_start for item in data.exclusions] == [hour(2), hour(3)]
    assert all(
        [cause.reason for cause in item.causes] == ["unavailable"]
        for item in data.exclusions
    )
    # The freshest usable observation is the last valid one, not the outage.
    assert data.latest_observation_at == hour(2)


def test_fetch_aligns_channels_to_the_latest_available_start() -> None:
    data = build_grid_flow(
        respond_by_entity(
            {
                ENTITY_ID: standard_readings(),
                EXPORT_ENTITY_ID: hourly("0", "1", "3", "6", minutes=30),
            }
        )
    )

    assert data.start_time == hour(1)
    assert data.import_kw == (2.0, 3.0, 4.0)
    assert data.export_kw == (1.0, 2.0, 3.0)


def test_fetch_fails_without_returning_partial_data_when_one_channel_fails() -> None:
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(200, json=history_payload(ENTITY_ID))
        return httpx.Response(503)

    with pytest.raises(HomeAssistantError, match="HTTP 503"):
        build_grid_flow(handler)

    assert calls == 2


def test_freshness_uses_the_oldest_channel_observation() -> None:
    data = build_grid_flow(
        respond_by_entity(
            {
                ENTITY_ID: standard_readings(),
                EXPORT_ENTITY_ID: readings(
                    ("00:00", "0"),
                    ("01:00", "0.5"),
                    ("02:00", "1"),
                    ("03:00", "1.5"),
                    ("03:30", "2"),
                ),
            }
        )
    )

    assert data.latest_observation_at == hour(3) + timedelta(minutes=30)
    provider = HomeAssistantGridFlowImporter(configuration(max_data_age_seconds=90))
    assert not provider.is_fresh(data, now=NOW)


def test_fetch_rejects_instantaneous_power_channel() -> None:
    handler = respond_by_entity(
        {ENTITY_ID: hourly("500"), EXPORT_ENTITY_ID: hourly("500")}, unit="W"
    )

    with pytest.raises(HomeAssistantError, match="instantaneous power"):
        build_grid_flow(handler)


def test_grid_flow_excludes_the_hour_of_a_total_counter_dip_without_last_reset() -> (
    None
):
    """Regression test for issues #179 and #183 on both grid legs.

    A dip of 1 Wh is a decrease like any other: nothing is tolerated or repaired.
    """
    times = ("00:00", "00:30", "00:30:12", "00:30:24", "01:00")
    data = build_grid_flow(
        respond_by_entity(
            {
                ENTITY_ID: readings(
                    *zip(times, ("100.000", "100.500", "100.499", "100.500", "101.000"))
                ),
                EXPORT_ENTITY_ID: readings(
                    *zip(times, ("50.000", "50.250", "50.249", "50.250", "50.500"))
                ),
            },
            state_class="total",
        ),
        hour(1),
        grid_import=channel(ENTITY_ID, state_class="total"),
        grid_export=channel(EXPORT_ENTITY_ID, state_class="total"),
    )

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
