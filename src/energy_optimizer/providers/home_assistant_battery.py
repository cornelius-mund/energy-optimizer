"""Home Assistant battery state and capability provider."""

import logging
import math
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

import httpx

from energy_optimizer.config import (
    BatteryConfigurationValue,
    BatteryConstantConfiguration,
    HomeAssistantBatteryEntityConfiguration,
    HomeAssistantConfiguration,
)
from energy_optimizer.providers.home_assistant_energy import is_fresh
from energy_optimizer.providers.home_assistant_history import HomeAssistantError
from energy_optimizer.providers.http import JsonHttpClient
from energy_optimizer.providers.interfaces import (
    BATTERY_SOURCE_ID,
    DEFAULT_EFFICIENCY_RATIO,
    BatteryData,
    BatteryEfficiencyData,
    SourceMetadata,
)
from energy_optimizer.providers.normalization import as_utc, parse_aware_timestamp

logger = logging.getLogger(__name__)

__all__ = ["HomeAssistantBatteryImporter", "HomeAssistantError"]

_Records = dict[str, dict[str, Any]]
_UNAVAILABLE_STATES = {"unknown", "unavailable"}


class HomeAssistantBatteryImporter:
    """Retrieve and normalize a current battery snapshot."""

    def __init__(
        self,
        configuration: HomeAssistantConfiguration,
        client: httpx.Client | None = None,
    ) -> None:
        if configuration.battery is None:
            raise HomeAssistantError(
                "Home Assistant battery mappings are not configured"
            )
        self.configuration = configuration
        self.battery_configuration = configuration.battery
        self._http = JsonHttpClient(client)

    def fetch(
        self,
        *,
        now: datetime | None = None,
        efficiency_data: BatteryEfficiencyData | None = None,
    ) -> BatteryData:
        """Fetch and normalize all configured battery mappings atomically."""
        retrieved_at = as_utc(
            now or datetime.now(timezone.utc),
            error_factory=HomeAssistantError,
            message="Home Assistant battery import times must include a timezone",
        )
        configuration = self.battery_configuration
        mappings = [
            value
            for value in (
                configuration.state_of_charge,
                configuration.capacity,
                configuration.minimum_soc,
                configuration.maximum_soc,
                configuration.maximum_charge,
                configuration.maximum_discharge,
                configuration.battery_efficiency,
            )
            if isinstance(value, HomeAssistantBatteryEntityConfiguration)
        ]
        logger.debug(
            "event=provider_fetch_started component=home_assistant operation=fetch "
            "data_type=battery entity_count=%s",
            len({mapping.entity_id for mapping in mappings}),
        )
        records = self._fetch_records(mappings)
        capacity = self._convert("capacity", configuration.capacity, records)
        if capacity <= 0:
            raise HomeAssistantError(
                "Home Assistant battery capacity must be greater than zero"
            )

        state_of_charge = self._convert_soc(
            "state_of_charge", configuration.state_of_charge, records, capacity
        )
        latest_observation_at = min(record["timestamp"] for record in records.values())
        data = BatteryData(
            schema_version="1",
            start_time=retrieved_at,
            interval_minutes=60,
            state_of_charge_kwh=(state_of_charge,),
            capacity_kwh=capacity,
            minimum_soc_kwh=self._convert_soc(
                "minimum_soc", configuration.minimum_soc, records, capacity
            ),
            maximum_soc_kwh=self._convert_soc(
                "maximum_soc", configuration.maximum_soc, records, capacity
            ),
            initial_soc_kwh=state_of_charge,
            maximum_charge_kw=self._convert(
                "maximum_charge", configuration.maximum_charge, records
            ),
            maximum_discharge_kw=self._convert(
                "maximum_discharge", configuration.maximum_discharge, records
            ),
            battery_efficiency=self._battery_efficiency(records, efficiency_data),
            unit="kWh",
            power_unit="kW",
            source=SourceMetadata(
                provider="home-assistant", entity_id=BATTERY_SOURCE_ID
            ),
            retrieved_at=retrieved_at,
            latest_observation_at=latest_observation_at,
        )
        _validate(data)
        logger.info(
            "event=provider_fetch_succeeded component=home_assistant operation=fetch "
            "data_type=battery entity_count=%s latest_observation_at=%s",
            len(records),
            latest_observation_at,
        )
        return data

    def is_fresh(self, data: BatteryData, *, now: datetime | None = None) -> bool:
        """Check whether the snapshot is within the configured age threshold."""
        return is_fresh(
            data.latest_observation_at, self.configuration.max_data_age_seconds, now=now
        )

    def _fetch_records(
        self, mappings: list[HomeAssistantBatteryEntityConfiguration]
    ) -> _Records:
        records: _Records = {}
        for entity_id in {mapping.entity_id for mapping in mappings}:
            payload = self._request(entity_id)
            if not isinstance(payload, dict):
                raise HomeAssistantError(
                    f"Home Assistant battery state for {entity_id} must be a JSON "
                    "object"
                )
            if payload.get("entity_id") not in (None, entity_id):
                raise HomeAssistantError(
                    f"Home Assistant returned battery state for an unexpected entity "
                    f"instead of {entity_id}"
                )
            timestamp = parse_aware_timestamp(
                payload.get("last_updated", payload.get("last_changed")),
                error_factory=HomeAssistantError,
                missing_message=(
                    f"Home Assistant battery entity {entity_id} is missing an "
                    "observation timestamp"
                ),
                invalid_message=lambda raw: (
                    f"Home Assistant battery entity {entity_id} returned an invalid "
                    f"observation timestamp: {raw!r}"
                ),
                naive_message=(
                    f"Home Assistant battery entity {entity_id} observation timestamp "
                    "must include a timezone"
                ),
            )
            attributes = payload.get("attributes", {})
            if not isinstance(attributes, dict):
                raise HomeAssistantError(
                    f"Home Assistant battery state for {entity_id} has invalid "
                    "attributes"
                )
            state = payload.get("state")
            uses_state = any(
                mapping.entity_id == entity_id and mapping.attribute is None
                for mapping in mappings
            )
            if uses_state and _is_unavailable(state):
                raise HomeAssistantError(
                    f"Home Assistant battery entity {entity_id} is unavailable"
                )
            records[entity_id] = {
                "state": state,
                "attributes": attributes,
                "timestamp": timestamp,
            }
        return records

    def _request(self, entity_id: str) -> Any:
        base_url = str(self.configuration.base_url).rstrip("/")
        return self._http.get_home_assistant_json(
            f"{base_url}/api/states/{quote(entity_id, safe='')}",
            token=self.configuration.token.get_secret_value(),
            timeout_seconds=self.configuration.timeout_seconds,
            error_factory=HomeAssistantError,
            not_found_message=(
                f"Home Assistant battery entity {entity_id} was not found; "
                "check the configured entity ID and endpoint"
            ),
            status_message=lambda status: (
                f"Home Assistant returned HTTP {status} while retrieving battery "
                f"entity {entity_id}"
            ),
            timeout_message=(
                "Home Assistant request timed out; check the endpoint and timeout"
            ),
            transport_message="Home Assistant request failed: transport error",
            malformed_message=(
                f"Home Assistant returned malformed JSON for battery entity {entity_id}"
            ),
            log_event="home_assistant_state_request",
            component="home_assistant",
            operation="state_request",
            log_context=f"entity_id={entity_id}",
        )

    def _convert(
        self, name: str, configuration: BatteryConfigurationValue, records: _Records
    ) -> float:
        raw_value: object
        if isinstance(configuration, BatteryConstantConfiguration):
            source = "configuration constant"
            raw_value = configuration.value
        else:
            source = configuration.entity_id
            record = records[source]
            if configuration.attribute is None:
                raw_value = record["state"]
            elif configuration.attribute in record["attributes"]:
                raw_value = record["attributes"][configuration.attribute]
            else:
                raise HomeAssistantError(
                    f"Home Assistant battery entity {source} is missing "
                    f"attribute {configuration.attribute!r} for {name}"
                )
            if _is_unavailable(raw_value):
                raise HomeAssistantError(
                    f"Home Assistant battery entity {source} has an unavailable "
                    f"value for {name}"
                )
        non_numeric = (
            f"Home Assistant battery entity {source} returned a non-numeric "
            f"value for {name}"
        )
        if isinstance(raw_value, bool):
            raise HomeAssistantError(non_numeric)
        try:
            number = float(raw_value)  # type: ignore[arg-type]
        except (TypeError, ValueError) as error:
            raise HomeAssistantError(non_numeric) from error
        if not math.isfinite(number):
            raise HomeAssistantError(
                f"Home Assistant battery entity {source} returned a non-finite "
                f"value for {name}"
            )
        if configuration.unit in ("Wh", "W"):
            number /= 1000
        elif configuration.unit == "%":
            number /= 100
        return number

    def _convert_soc(
        self,
        name: str,
        configuration: BatteryConfigurationValue,
        records: _Records,
        capacity: float,
    ) -> float:
        number = self._convert(name, configuration, records)
        if configuration.unit == "%":
            number *= capacity
        return number

    def _battery_efficiency(
        self, records: _Records, calculated: BatteryEfficiencyData | None
    ) -> float:
        """Resolve a fixed value, then a completed calculation, then a default.

        A live battery snapshot must remain available even before the first
        complete measured efficiency cycle exists, so calculated mode falls
        back to the default (documented as 95%) instead of failing the whole
        snapshot while measurement history is still accumulating.
        """
        configured = self.battery_configuration.battery_efficiency
        if configured is not None:
            return self._convert("battery_efficiency", configured, records)
        if (
            calculated is not None
            and calculated.battery_efficiency is not None
            and "battery_efficiency" not in calculated.defaulted_components
        ):
            return float(calculated.battery_efficiency)
        if self.battery_configuration.efficiency_calculation is not None:
            logger.info(
                "event=battery_efficiency_default_used "
                "component=home_assistant operation=fetch field=%s "
                "reason=calculation_not_ready default=%s",
                "battery_efficiency",
                DEFAULT_EFFICIENCY_RATIO,
            )
        return DEFAULT_EFFICIENCY_RATIO


def _is_unavailable(value: object) -> bool:
    return isinstance(value, str) and value.strip().lower() in _UNAVAILABLE_STATES


def _validate(data: BatteryData) -> None:
    values = {
        "capacity": data.capacity_kwh,
        "minimum_soc": data.minimum_soc_kwh,
        "maximum_soc": data.maximum_soc_kwh,
        "initial_soc": data.initial_soc_kwh,
        "maximum_charge": data.maximum_charge_kw,
        "maximum_discharge": data.maximum_discharge_kw,
        "battery_efficiency": data.battery_efficiency,
    }
    for name, value in values.items():
        if not math.isfinite(value):
            raise HomeAssistantError(f"Home Assistant battery {name} must be finite")
        if value < 0:
            raise HomeAssistantError(
                f"Home Assistant battery {name} must be non-negative"
            )
    for name in ("maximum_charge", "maximum_discharge"):
        if values[name] <= 0:
            raise HomeAssistantError(
                f"Home Assistant battery {name} must be greater than zero"
            )
    if data.minimum_soc_kwh > data.maximum_soc_kwh:
        raise HomeAssistantError(
            "Home Assistant battery minimum SOC exceeds maximum SOC"
        )
    if data.maximum_soc_kwh > data.capacity_kwh:
        raise HomeAssistantError("Home Assistant battery maximum SOC exceeds capacity")
    if not data.minimum_soc_kwh <= data.initial_soc_kwh <= data.maximum_soc_kwh:
        raise HomeAssistantError(
            "Home Assistant battery state of charge is outside configured SOC limits"
        )
    if not 0 < data.battery_efficiency <= 1:
        raise HomeAssistantError(
            "Home Assistant battery efficiencies must be greater than zero and "
            "no greater than one"
        )
