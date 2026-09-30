"""Import instantaneous heat-pump power and remaining electrical energy demand."""

import logging
import math
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

import httpx
from pydantic import ValidationError

from energy_optimizer.config import (
    HeatPumpEntityConfiguration,
    HomeAssistantConfiguration,
)
from energy_optimizer.heat_pump import HeatPumpLoad, HeatPumpSource
from energy_optimizer.providers.home_assistant_history import HomeAssistantError
from energy_optimizer.providers.http import JsonHttpClient
from energy_optimizer.providers.normalization import as_utc, parse_aware_timestamp

logger = logging.getLogger(__name__)


class HomeAssistantHeatPumpImporter:
    """Read mapped states atomically; retain source times rather than polling times."""

    def __init__(
        self,
        configuration: HomeAssistantConfiguration,
        client: httpx.Client | None = None,
    ) -> None:
        if configuration.heat_pump is None:
            raise HomeAssistantError(
                "Home Assistant heat-pump mappings are not configured"
            )
        self.configuration = configuration
        self.mapping = configuration.heat_pump
        self._http = JsonHttpClient(client)

    def fetch(self, *, now: datetime | None = None) -> HeatPumpLoad:
        retrieved_at = as_utc(
            now or datetime.now(UTC),
            error_factory=HomeAssistantError,
            message="heat-pump import time must include a timezone",
        )
        records = {
            entity_id: self._request(entity_id)
            for entity_id in dict.fromkeys(
                (self.mapping.power.entity_id, self.mapping.required_energy.entity_id)
            )
        }
        power, power_time = self._value(self.mapping.power, records)
        energy, energy_time = self._value(self.mapping.required_energy, records)
        if max(power_time, energy_time) > retrieved_at:
            raise HomeAssistantError(
                "heat-pump observation timestamp is in the future; check sensor clocks"
            )
        try:
            data = HeatPumpLoad(
                schema_version="1",
                start_time=retrieved_at.replace(minute=0, second=0, microsecond=0),
                interval_minutes=60,
                load_kw=[power] * len(self.mapping.available),
                available=self.mapping.available,
                minimum_power_kw=self.mapping.minimum_power_kw,
                maximum_power_kw=self.mapping.maximum_power_kw,
                required_energy_kwh=energy,
                unit="kW",
                energy_unit="kWh",
                source=HeatPumpSource(provider="home-assistant", entity_id="heat_pump"),
                retrieved_at=retrieved_at,
                latest_observation_at=min(power_time, energy_time),
            )
        except ValidationError as error:
            details = "; ".join(issue["msg"] for issue in error.errors())
            raise HomeAssistantError(
                f"invalid Home Assistant heat-pump data: {details}"
            ) from error
        logger.info(
            "event=provider_fetch_succeeded component=home_assistant "
            "data_type=heat-pump latest_observation_at=%s",
            data.latest_observation_at,
        )
        return data

    def is_fresh(self, data: HeatPumpLoad, *, now: datetime | None = None) -> bool:
        current = as_utc(
            now or datetime.now(UTC),
            error_factory=HomeAssistantError,
            message="heat-pump freshness time must include a timezone",
        )
        age = (current - data.latest_observation_at).total_seconds()
        maximum = self.configuration.max_data_age_seconds
        return age >= 0 and (maximum is None or age <= maximum)

    def _request(self, entity_id: str) -> Any:
        base_url = str(self.configuration.base_url).rstrip("/")
        return self._http.get_home_assistant_json(
            f"{base_url}/api/states/{quote(entity_id, safe='')}",
            token=self.configuration.token.get_secret_value(),
            timeout_seconds=self.configuration.timeout_seconds,
            error_factory=HomeAssistantError,
            not_found_message=(
                f"heat-pump entity {entity_id} was not found; "
                "check the configured mapping and endpoint"
            ),
            status_message=lambda status: (
                f"Home Assistant returned HTTP {status} "
                f"for heat-pump entity {entity_id}"
            ),
            timeout_message="heat-pump request timed out; check endpoint and timeout",
            transport_message=(
                "heat-pump request failed; check Home Assistant connectivity"
            ),
            malformed_message=(
                f"Home Assistant returned malformed JSON "
                f"for heat-pump entity {entity_id}"
            ),
            log_event="home_assistant_state_request",
            component="home_assistant",
            operation="state_request",
            log_context=f"entity_id={entity_id}",
        )

    def _value(
        self, mapping: HeatPumpEntityConfiguration, records: dict[str, Any]
    ) -> tuple[float, datetime]:
        record = records[mapping.entity_id]
        label = f"heat-pump entity {mapping.entity_id}"
        if not isinstance(record, dict) or record.get("entity_id") != mapping.entity_id:
            raise HomeAssistantError(
                f"{label} returned an invalid state object or entity ID"
            )
        attributes = record.get("attributes")
        if not isinstance(attributes, dict):
            raise HomeAssistantError(f"{label} has malformed attributes")
        if record.get("state") in ("unknown", "unavailable", None):
            raise HomeAssistantError(f"{label} is unavailable; check the sensor")
        if mapping.attribute is None:
            raw = record["state"]
            if attributes.get("unit_of_measurement") != mapping.unit:
                raise HomeAssistantError(
                    f"{label} unit does not match configured {mapping.unit}"
                )
        else:
            if mapping.attribute not in attributes:
                raise HomeAssistantError(
                    f"{label} is missing attribute {mapping.attribute}"
                )
            raw = attributes[mapping.attribute]
        try:
            value = float(raw) if not isinstance(raw, bool) else math.nan
        except (TypeError, ValueError) as error:
            raise HomeAssistantError(
                f"{label} has a non-numeric value; check the mapping"
            ) from error
        if not math.isfinite(value) or value < 0:
            raise HomeAssistantError(f"{label} must report a finite non-negative value")
        timestamp = parse_aware_timestamp(
            record.get("last_updated"),
            error_factory=HomeAssistantError,
            missing_message=f"{label} is missing last_updated",
            invalid_message=lambda raw: f"{label} has an invalid last_updated",
            naive_message=f"{label} last_updated must include a timezone",
        )
        return value / 1000 if mapping.unit in {"W", "Wh"} else value, timestamp
