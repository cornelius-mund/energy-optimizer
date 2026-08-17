"""Home Assistant battery state and capability provider."""

from __future__ import annotations

import logging
import math
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

import httpx

from energy_optimizer.config import (
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

    def fetch(self, *, now: datetime | None = None) -> BatteryData:
        """Fetch and normalize all configured battery mappings atomically."""
        retrieved_at = as_utc(
            now or datetime.now(timezone.utc),
            error_factory=HomeAssistantError,
            message="Home Assistant battery import times must include a timezone",
        )
        mappings = self._mappings()
        logger.debug(
            "event=provider_fetch_started component=home_assistant operation=fetch "
            "data_type=battery entity_count=%s",
            len({mapping.entity_id for mapping in mappings.values()}),
        )
        records = self._fetch_records(mappings)
        capacity = self._convert(
            "capacity",
            mappings["capacity"],
            self._value("capacity", mappings["capacity"], records),
        )
        if capacity <= 0:
            raise HomeAssistantError(
                "Home Assistant battery capacity must be greater than zero"
            )

        state_of_charge = self._convert_soc(
            "state_of_charge", mappings["state_of_charge"], records, capacity
        )
        minimum_soc = self._convert_soc(
            "minimum_soc", mappings["minimum_soc"], records, capacity
        )
        maximum_soc = self._convert_soc(
            "maximum_soc", mappings["maximum_soc"], records, capacity
        )
        initial_soc = state_of_charge
        maximum_charge = self._convert(
            "maximum_charge",
            mappings["maximum_charge"],
            self._value("maximum_charge", mappings["maximum_charge"], records),
        )
        maximum_discharge = self._convert(
            "maximum_discharge",
            mappings["maximum_discharge"],
            self._value("maximum_discharge", mappings["maximum_discharge"], records),
        )
        charge_efficiency = self._convert_efficiency(
            "charge_efficiency", mappings["charge_efficiency"], records
        )
        discharge_efficiency = self._convert_efficiency(
            "discharge_efficiency", mappings["discharge_efficiency"], records
        )
        self._validate_values(
            capacity,
            minimum_soc,
            maximum_soc,
            initial_soc,
            maximum_charge,
            maximum_discharge,
            charge_efficiency,
            discharge_efficiency,
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
            charge_efficiency=charge_efficiency,
            discharge_efficiency=discharge_efficiency,
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
        headers = {
            "Authorization": f"Bearer {self.configuration.token.get_secret_value()}",
            "Accept": "application/json",
        }

        def status_error(status: int) -> Exception | None:
            if status in (401, 403):
                return HomeAssistantError(
                    "Home Assistant authentication failed; check the configured token"
                )
            if status == 404:
                return HomeAssistantError(
                    f"Home Assistant battery entity {entity_id} was not found; "
                    "check the configured entity ID and endpoint"
                )
            if status >= 400:
                return HomeAssistantError(
                    f"Home Assistant returned HTTP {status} while retrieving battery "
                    f"entity {entity_id}"
                )
            return None

        base_url = str(self.configuration.base_url).rstrip("/")
        return self._http.get_json(
            f"{base_url}/api/states/{quote(entity_id, safe='')}",
            headers=headers,
            timeout_seconds=self.configuration.timeout_seconds,
            error_factory=HomeAssistantError,
            timeout_message=(
                "Home Assistant request timed out; check the endpoint and timeout"
            ),
            transport_message="Home Assistant request failed: transport error",
            malformed_message=(
                f"Home Assistant returned malformed JSON for battery entity {entity_id}"
            ),
            status_error=status_error,
            log_event="home_assistant_state_request",
            component="home_assistant",
            operation="state_request",
            log_context=f"entity_id={entity_id}",
        )

    def _mappings(self) -> dict[str, HomeAssistantBatteryEntityConfiguration]:
        configuration = self.battery_configuration
        return {
            "state_of_charge": configuration.state_of_charge,
            "capacity": configuration.capacity,
            "minimum_soc": configuration.minimum_soc,
            "maximum_soc": configuration.maximum_soc,
            "maximum_charge": configuration.maximum_charge,
            "maximum_discharge": configuration.maximum_discharge,
            "charge_efficiency": configuration.charge_efficiency,
            "discharge_efficiency": configuration.discharge_efficiency,
        }

    def _value(
        self,
        name: str,
        mapping: HomeAssistantBatteryEntityConfiguration,
        records: dict[str, dict[str, Any]],
    ) -> object:
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
        mapping: HomeAssistantBatteryEntityConfiguration,
        value: object,
    ) -> float:
        number = self._number(value, name, mapping.entity_id)
        if mapping.unit == "Wh" or mapping.unit == "W":
            number /= 1000
        elif mapping.unit == "%":
            number /= 100
        return number

    def _convert_soc(
        self,
        name: str,
        mapping: HomeAssistantBatteryEntityConfiguration,
        records: dict[str, dict[str, Any]],
        capacity: float,
    ) -> float:
        number = self._convert(name, mapping, self._value(name, mapping, records))
        if mapping.unit == "%":
            number *= capacity
        return number

    def _convert_efficiency(
        self,
        name: str,
        mapping: HomeAssistantBatteryEntityConfiguration,
        records: dict[str, dict[str, Any]],
    ) -> float:
        return self._convert(name, mapping, self._value(name, mapping, records))

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

    def _validate_values(self, *values: float) -> None:
        names = (
            "capacity",
            "minimum_soc",
            "maximum_soc",
            "initial_soc",
            "maximum_charge",
            "maximum_discharge",
            "charge_efficiency",
            "discharge_efficiency",
        )
        for name, value in zip(names, values):
            if not math.isfinite(value):
                raise HomeAssistantError(
                    f"Home Assistant battery {name} must be finite"
                )
            if value < 0:
                raise HomeAssistantError(
                    f"Home Assistant battery {name} must be non-negative"
                )
        if values[4] <= 0:
            raise HomeAssistantError(
                "Home Assistant battery maximum_charge must be greater than zero"
            )
        if values[5] <= 0:
            raise HomeAssistantError(
                "Home Assistant battery maximum_discharge must be greater than zero"
            )
        if values[1] > values[2]:
            raise HomeAssistantError(
                "Home Assistant battery minimum SOC exceeds maximum SOC"
            )
        if values[2] > values[0]:
            raise HomeAssistantError(
                "Home Assistant battery maximum SOC exceeds capacity"
            )
        if not values[1] <= values[3] <= values[2]:
            raise HomeAssistantError(
                "Home Assistant battery state of charge is outside configured SOC "
                "limits"
            )
        if not 0 < values[6] <= 1 or not 0 < values[7] <= 1:
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
