"""Shared factories for Home Assistant provider tests."""

from collections.abc import Callable
from typing import Any, TypeVar

import httpx

from energy_optimizer.config import HomeAssistantConfiguration

ImporterT = TypeVar("ImporterT")
ConfigurationFactory = Callable[..., HomeAssistantConfiguration]


def home_assistant_configuration_factory(
    **defaults: Any,
) -> ConfigurationFactory:
    """Build configuration factories with shared Home Assistant test defaults."""

    def create_configuration(**overrides: Any) -> HomeAssistantConfiguration:
        values: dict[str, Any] = {
            "base_url": "http://homeassistant.test:8123",
            "token": "test-token",
            "timeout_seconds": 5,
            "max_data_age_seconds": 7200,
        }
        values.update(defaults)
        values.update(overrides)
        return HomeAssistantConfiguration.model_validate(values)

    return create_configuration


def home_assistant_importer_factory(
    importer_type: Callable[[HomeAssistantConfiguration, httpx.Client], ImporterT],
    configuration: ConfigurationFactory,
) -> Callable[..., tuple[ImporterT, httpx.Client]]:
    """Build an importer factory that owns the test client's lifecycle inputs."""

    def create_importer(
        handler: httpx.MockTransport | httpx.BaseTransport,
        **configuration_overrides: Any,
    ) -> tuple[ImporterT, httpx.Client]:
        client = httpx.Client(transport=handler)
        return importer_type(configuration(**configuration_overrides), client), client

    return create_importer


def home_assistant_state_payload(
    entity_id: str,
    state: object,
    timestamp: str = "2026-01-01T05:00:00+00:00",
) -> dict[str, object]:
    """Create one Home Assistant state endpoint response."""

    return {
        "entity_id": entity_id,
        "state": state,
        "last_updated": timestamp,
        "attributes": {},
    }


def home_assistant_history_payload(
    entity_id: str,
    readings: list[tuple[str, str]] | None = None,
    *,
    unit: str = "kWh",
    state_class: str = "total_increasing",
    last_resets: list[str | None] | None = None,
) -> list[list[dict[str, Any]]]:
    """Create a Home Assistant history endpoint response."""

    records: list[dict[str, Any]] = []
    for index, (timestamp, state) in enumerate(
        readings
        or [
            ("2026-01-01T00:00:00+00:00", "0"),
            ("2026-01-01T01:00:00+00:00", "1"),
            ("2026-01-01T02:00:00+00:00", "3"),
            ("2026-01-01T03:00:00+00:00", "6"),
            ("2026-01-01T04:00:00+00:00", "10"),
        ]
    ):
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


Readings = list[tuple[str, str]]


def _three_hour_readings(first_hour: Readings, later_hours: list[str]) -> Readings:
    """Join custom first-hour readings with one reading at each later hour mark."""
    return [
        *first_hour,
        *(
            (f"2026-01-01T{hour:02d}:00:00+00:00", value)
            for hour, value in enumerate(later_hours, start=1)
        ),
    ]


def home_assistant_suspect_negative_hour_readings(
    *, add_side_valid: bool = False
) -> tuple[Readings, Readings]:
    """Return add and subtract counter readings for a suspect negative hour.

    The readings span the three hours from 2026-01-01T00:00Z and reproduce the
    live failure of a combined hour that is negative while its contributors
    are already flagged suspect. In hour 0 the add counter resets and its
    first post-reset delta of 150 kWh exceeds the default 100 kWh physical
    limit, so it contributes zero with reason ``physical_limit_exceeded``. The
    subtract counter resets too (reason ``counter_reset``) but its later
    post-reset growth of 56.9 kWh is accepted, which makes the combined hour
    -56.9 kWh. With ``add_side_valid`` the add counter only ticks up by 0.5
    kWh, leaving the subtract counter as the sole suspect contributor. Hours 1
    and 2 are ordinary and net to 0.5 and 1.0 kWh.
    """
    if add_side_valid:
        add_readings = _three_hour_readings(
            [
                ("2026-01-01T00:00:00+00:00", "500"),
                ("2026-01-01T00:40:00+00:00", "500.5"),
            ],
            ["500.5", "501.5", "503.5"],
        )
    else:
        add_readings = _three_hour_readings(
            [
                ("2026-01-01T00:00:00+00:00", "500"),
                ("2026-01-01T00:20:00+00:00", "0.5"),
                ("2026-01-01T00:40:00+00:00", "150.5"),
            ],
            ["150.5", "151.5", "153.5"],
        )
    subtract_readings = _three_hour_readings(
        [
            ("2026-01-01T00:00:00+00:00", "1000"),
            ("2026-01-01T00:20:00+00:00", "0.1"),
            ("2026-01-01T00:40:00+00:00", "57"),
        ],
        ["57", "57.5", "58.5"],
    )
    return add_readings, subtract_readings
