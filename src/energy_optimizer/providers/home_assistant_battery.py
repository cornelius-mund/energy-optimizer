"""Home Assistant battery state and capability provider."""

from __future__ import annotations

import logging
import math
from datetime import datetime, timezone
from typing import Any, cast
from urllib.parse import quote

import httpx

from energy_optimizer.config import (
    BatteryConfigurationValue,
    BatteryConstantConfiguration,
    HomeAssistantBatteryEntityConfiguration,
    HomeAssistantConfiguration,
)
from energy_optimizer.providers.home_assistant_energy import (
    HomeAssistantError,
    is_fresh,
)
from energy_optimizer.providers.http import JsonHttpClient
from energy_optimizer.providers.interfaces import (
    BATTERY_SOURCE_ID,
    BatteryData,
    BatteryEfficiencyData,
    SourceMetadata,
)
from energy_optimizer.providers.normalization import as_utc, parse_aware_timestamp

logger = logging.getLogger(__name__)

__all__ = ["HomeAssistantBatteryImporter", "HomeAssistantError"]


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
        mappings = self._mappings()
        values = self._values()
        logger.debug(
            "event=provider_fetch_started component=home_assistant operation=fetch "
            "data_type=battery entity_count=%s",
            len({mapping.entity_id for mapping in mappings.values()}),
        )
        records = self._fetch_records(mappings)
        capacity = self._convert(
            "capacity",
            values["capacity"],
            self._value("capacity", values["capacity"], records),
        )
        if capacity <= 0:
            raise HomeAssistantError(
                "Home Assistant battery capacity must be greater than zero"
            )

        state_of_charge = self._convert_soc(
            "state_of_charge", values["state_of_charge"], records, capacity
        )
        minimum_soc = self._convert_soc(
            "minimum_soc", values["minimum_soc"], records, capacity
        )
        maximum_soc = self._convert_soc(
            "maximum_soc", values["maximum_soc"], records, capacity
        )
        initial_soc = state_of_charge
        maximum_charge = self._convert(
            "maximum_charge",
            values["maximum_charge"],
            self._value("maximum_charge", values["maximum_charge"], records),
        )
        maximum_discharge = self._convert(
            "maximum_discharge",
            values["maximum_discharge"],
            self._value("maximum_discharge", values["maximum_discharge"], records),
        )
        battery_efficiency = self._efficiency_value(
            "battery_efficiency",
            values["battery_efficiency"],
            records,
            efficiency_data,
            default=1.0,
        )
        self._validate_values(
            capacity=capacity,
            minimum_soc=minimum_soc,
            maximum_soc=maximum_soc,
            initial_soc=initial_soc,
            maximum_charge=maximum_charge,
            maximum_discharge=maximum_discharge,
            battery_efficiency=battery_efficiency,
        )
        latest_observation_at = min(
            self._record_timestamp(records, name) for name in mappings
        )
        data = BatteryData(
            schema_version="1",
            start_time=retrieved_at,
            interval_minutes=60,
            state_of_charge_kwh=(state_of_charge,),
            capacity_kwh=capacity,
            minimum_soc_kwh=minimum_soc,
            maximum_soc_kwh=maximum_soc,
            initial_soc_kwh=initial_soc,
            maximum_charge_kw=maximum_charge,
            maximum_discharge_kw=maximum_discharge,
            battery_efficiency=battery_efficiency,
            unit="kWh",
            power_unit="kW",
            source=SourceMetadata(
                provider="home-assistant", entity_id=BATTERY_SOURCE_ID
            ),
            retrieved_at=retrieved_at,
            latest_observation_at=latest_observation_at,
        )
        logger.info(
            "event=provider_fetch_succeeded component=home_assistant operation=fetch "
            "data_type=battery entity_count=%s latest_observation_at=%s",
            len({mapping.entity_id for mapping in mappings.values()}),
            latest_observation_at,
        )
        return data

    def is_fresh(
        self,
        data: BatteryData,
        *,
        now: datetime | None = None,
    ) -> bool:
        """Check whether the snapshot is within the configured age threshold."""
        return is_fresh(
            data.latest_observation_at,
            self.configuration.max_data_age_seconds,
            now=now,
        )

    def _fetch_records(
        self,
        mappings: dict[str, HomeAssistantBatteryEntityConfiguration],
    ) -> dict[str, dict[str, Any]]:
        records: dict[str, dict[str, Any]] = {}
        for entity_id in {mapping.entity_id for mapping in mappings.values()}:
            payload = self._request(entity_id)
            if not isinstance(payload, dict):
                raise HomeAssistantError(
                    f"Home Assistant battery state for {entity_id} must be a JSON "
                    "object"
                )
            reported_entity_id = payload.get("entity_id")
            if reported_entity_id is not None and reported_entity_id != entity_id:
                raise HomeAssistantError(
                    f"Home Assistant returned battery state for an unexpected entity "
                    f"instead of {entity_id}"
                )
            timestamp = self._parse_timestamp(payload, entity_id)
            attributes = payload.get("attributes", {})
            if not isinstance(attributes, dict):
                raise HomeAssistantError(
                    f"Home Assistant battery state for {entity_id} has invalid "
                    "attributes"
                )
            state = payload.get("state")
            uses_state = any(
                mapping.entity_id == entity_id and mapping.attribute is None
                for mapping in mappings.values()
            )
            if (
                uses_state
                and isinstance(state, str)
                and state.strip().lower() in {"unknown", "unavailable"}
            ):
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

    def _mappings(self) -> dict[str, HomeAssistantBatteryEntityConfiguration]:
        return {
            name: value
            for name, value in self._values().items()
            if isinstance(value, HomeAssistantBatteryEntityConfiguration)
        }

    def _values(self) -> dict[str, BatteryConfigurationValue]:
        configuration = self.battery_configuration
        return {
            "state_of_charge": configuration.state_of_charge,
            "capacity": configuration.capacity,
            "minimum_soc": configuration.minimum_soc,
            "maximum_soc": configuration.maximum_soc,
            "maximum_charge": configuration.maximum_charge,
            "maximum_discharge": configuration.maximum_discharge,
            "battery_efficiency": cast(
                BatteryConfigurationValue, configuration.battery_efficiency
            ),
        }

    def _value(
        self,
        name: str,
        value: BatteryConfigurationValue,
        records: dict[str, dict[str, Any]],
    ) -> object:
        if isinstance(value, BatteryConstantConfiguration):
            return value.value

        mapping = value
        record = records[mapping.entity_id]
        if mapping.attribute is None:
            value = record["state"]
        else:
            attributes = record["attributes"]
            if mapping.attribute not in attributes:
                raise HomeAssistantError(
                    f"Home Assistant battery entity {mapping.entity_id} is missing "
                    f"attribute {mapping.attribute!r} for {name}"
                )
            value = attributes[mapping.attribute]
        if isinstance(value, str) and value.strip().lower() in {
            "unknown",
            "unavailable",
        }:
            raise HomeAssistantError(
                f"Home Assistant battery entity {mapping.entity_id} has an unavailable "
                f"value for {name}"
            )
        return value

    def _convert(
        self,
        name: str,
        configuration: BatteryConfigurationValue,
        raw_value: object,
    ) -> float:
        unit = configuration.unit
        source = (
            configuration.entity_id
            if isinstance(configuration, HomeAssistantBatteryEntityConfiguration)
            else "configuration constant"
        )
        number = self._number(raw_value, name, source)
        if unit == "Wh" or unit == "W":
            number /= 1000
        elif unit == "%":
            number /= 100
        return number

    def _convert_soc(
        self,
        name: str,
        value: BatteryConfigurationValue,
        records: dict[str, dict[str, Any]],
        capacity: float,
    ) -> float:
        number = self._convert(name, value, self._value(name, value, records))
        if value.unit == "%":
            number *= capacity
        return number

    def _convert_efficiency(
        self,
        name: str,
        value: BatteryConfigurationValue,
        records: dict[str, dict[str, Any]],
    ) -> float:
        return self._convert(name, value, self._value(name, value, records))

    def _efficiency_value(
        self,
        name: str,
        value: BatteryConfigurationValue | None,
        records: dict[str, dict[str, Any]],
        calculated: BatteryEfficiencyData | None,
        *,
        default: float | None = None,
    ) -> float:
        """Resolve fixed values before the optional calculated result."""
        if value is not None:
            return self._convert_efficiency(name, value, records)
        if calculated is not None and calculated.status == "ok":
            resolved = getattr(calculated, name)
            if resolved is not None:
                return float(resolved)
        if self.battery_configuration.efficiency_calculation is not None:
            raise HomeAssistantError(
                f"calculated battery {name} is not available; "
                "collect a complete valid efficiency history"
            )
        if default is not None:
            return default
        raise HomeAssistantError(f"Home Assistant battery {name} is not configured")

    def _record_timestamp(
        self,
        records: dict[str, dict[str, Any]],
        name: str,
    ) -> datetime:
        mapping = self._mappings()[name]
        timestamp = records[mapping.entity_id]["timestamp"]
        if not isinstance(timestamp, datetime):
            raise HomeAssistantError(
                f"Home Assistant battery entity {mapping.entity_id} has an invalid "
                "observation timestamp"
            )
        return timestamp

    def _validate_values(
        self,
        *,
        capacity: float,
        minimum_soc: float,
        maximum_soc: float,
        initial_soc: float,
        maximum_charge: float,
        maximum_discharge: float,
        battery_efficiency: float,
    ) -> None:
        values = {
            "capacity": capacity,
            "minimum_soc": minimum_soc,
            "maximum_soc": maximum_soc,
            "initial_soc": initial_soc,
            "maximum_charge": maximum_charge,
            "maximum_discharge": maximum_discharge,
            "battery_efficiency": battery_efficiency,
        }
        for name, value in values.items():
            if not math.isfinite(value):
                raise HomeAssistantError(
                    f"Home Assistant battery {name} must be finite"
                )
            if value < 0:
                raise HomeAssistantError(
                    f"Home Assistant battery {name} must be non-negative"
                )
        if maximum_charge <= 0:
            raise HomeAssistantError(
                "Home Assistant battery maximum_charge must be greater than zero"
            )
        if maximum_discharge <= 0:
            raise HomeAssistantError(
                "Home Assistant battery maximum_discharge must be greater than zero"
            )
        if minimum_soc > maximum_soc:
            raise HomeAssistantError(
                "Home Assistant battery minimum SOC exceeds maximum SOC"
            )
        if maximum_soc > capacity:
            raise HomeAssistantError(
                "Home Assistant battery maximum SOC exceeds capacity"
            )
        if not minimum_soc <= initial_soc <= maximum_soc:
            raise HomeAssistantError(
                "Home Assistant battery state of charge is outside configured SOC "
                "limits"
            )
        if not 0 < battery_efficiency <= 1:
            raise HomeAssistantError(
                "Home Assistant battery efficiencies must be greater than zero and "
                "no greater than one"
            )

    @staticmethod
    def _number(value: object, name: str, entity_id: str) -> float:
        if isinstance(value, bool):
            raise HomeAssistantError(
                f"Home Assistant battery entity {entity_id} returned a non-numeric "
                f"value for {name}"
            )
        try:
            number = float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError) as error:
            raise HomeAssistantError(
                f"Home Assistant battery entity {entity_id} returned a non-numeric "
                f"value for {name}"
            ) from error
        if not math.isfinite(number):
            raise HomeAssistantError(
                f"Home Assistant battery entity {entity_id} returned a non-finite "
                f"value for {name}"
            )
        return number

    @staticmethod
    def _parse_timestamp(payload: dict[str, Any], entity_id: str) -> datetime:
        return parse_aware_timestamp(
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
