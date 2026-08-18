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
