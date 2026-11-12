"""Home Assistant electric-vehicle state provider."""

import logging
import math
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

import httpx

from energy_optimizer.config import HomeAssistantConfiguration
from energy_optimizer.providers.home_assistant_energy import is_fresh
from energy_optimizer.providers.home_assistant_history import HomeAssistantError
from energy_optimizer.providers.http import JsonHttpClient
from energy_optimizer.providers.interfaces import EV_SOURCE_ID, EvData, SourceMetadata
from energy_optimizer.providers.normalization import as_utc, parse_aware_timestamp

logger = logging.getLogger(__name__)

__all__ = ["HomeAssistantEvImporter", "HomeAssistantError"]

_UNAVAILABLE_STATES = {"unknown", "unavailable"}


class HomeAssistantEvImporter:
    """Read the current state of charge and plug state of the car."""

    def __init__(
        self,
        configuration: HomeAssistantConfiguration,
        client: httpx.Client | None = None,
    ) -> None:
        if configuration.ev is None:
            raise HomeAssistantError("Home Assistant EV mappings are not configured")
        self.configuration = configuration
        self.ev_configuration = configuration.ev
        self._http = JsonHttpClient(client)

    def fetch(self, *, now: datetime | None = None) -> EvData:
        retrieved_at = as_utc(
            now or datetime.now(timezone.utc),
            error_factory=HomeAssistantError,
            message="Home Assistant retrieval time must include a timezone",
        )
        ev = self.ev_configuration
        soc_record = self._state(ev.state_of_charge.entity_id)
        plug_record = self._state(ev.plugged_in.entity_id)
        soc_value = _number(soc_record, ev.state_of_charge.entity_id)
        soc_kwh = (
            soc_value / 100 * ev.capacity_kwh
            if ev.state_of_charge.unit == "%"
            else soc_value
        )
        plugged_in = str(plug_record["state"]).lower() in {
            state.lower() for state in ev.plugged_in.plugged_in_states
        }
        data = EvData(
            schema_version="1",
            start_time=retrieved_at.replace(minute=0, second=0, microsecond=0),
            state_of_charge_kwh=soc_kwh,
            capacity_kwh=ev.capacity_kwh,
            plugged_in=plugged_in,
            maximum_charge_kw=ev.maximum_charge_kw,
            charge_efficiency=ev.charge_efficiency,
            unit="kWh",
            source=SourceMetadata(provider="home-assistant", entity_id=EV_SOURCE_ID),
            retrieved_at=retrieved_at,
            # Home Assistant keeps an unchanged value without a new timestamp, so a
            # valid state is current at the moment it is read.
            latest_observation_at=retrieved_at,
        )
        if not 0 <= data.state_of_charge_kwh <= data.capacity_kwh:
            raise HomeAssistantError(
                "Home Assistant EV state of charge is outside the battery capacity"
            )
        logger.info(
            "event=provider_fetch_succeeded component=home_assistant operation=fetch "
            "data_type=ev plugged_in=%s",
            plugged_in,
        )
        return data

    def is_fresh(self, data: EvData, *, now: datetime | None = None) -> bool:
        return is_fresh(
            data.latest_observation_at, self.configuration.max_data_age_seconds, now=now
        )

    def _state(self, entity_id: str) -> dict[str, Any]:
        base_url = str(self.configuration.base_url).rstrip("/")
        payload = self._http.get_home_assistant_json(
            f"{base_url}/api/states/{quote(entity_id, safe='')}",
            token=self.configuration.token.get_secret_value(),
            timeout_seconds=self.configuration.timeout_seconds,
            error_factory=HomeAssistantError,
            not_found_message=(
                f"Home Assistant EV entity {entity_id} was not found; check the "
                "configured entity ID and endpoint"
            ),
            status_message=lambda status: (
                f"Home Assistant returned HTTP {status} while retrieving EV "
                f"entity {entity_id}"
            ),
            timeout_message=(
                "Home Assistant request timed out; check the endpoint and timeout"
            ),
            transport_message="Home Assistant request failed: transport error",
            malformed_message=(
                f"Home Assistant returned malformed JSON for EV entity {entity_id}"
            ),
            log_event="home_assistant_state_request",
            component="home_assistant",
            operation="state_request",
            log_context=f"entity_id={entity_id}",
        )
        if not isinstance(payload, dict):
            raise HomeAssistantError(
                f"Home Assistant EV state for {entity_id} must be a JSON object"
            )
        state = payload.get("state")
        if state is None or str(state).lower() in _UNAVAILABLE_STATES:
            raise HomeAssistantError(
                f"Home Assistant EV entity {entity_id} is unavailable"
            )
        timestamp = parse_aware_timestamp(
            payload.get("last_updated", payload.get("last_changed")),
            error_factory=HomeAssistantError,
            missing_message=(
                f"Home Assistant EV entity {entity_id} is missing a timestamp"
            ),
            invalid_message=lambda raw: (
                f"Home Assistant EV entity {entity_id} has an invalid timestamp: "
                f"{raw!r}"
            ),
            naive_message=(
                f"Home Assistant EV entity {entity_id} timestamp must include a "
                "timezone"
            ),
        )
        return {"state": state, "timestamp": timestamp}


def _number(record: dict[str, Any], entity_id: str) -> float:
    try:
        value = float(record["state"])
    except (TypeError, ValueError) as error:
        raise HomeAssistantError(
            f"Home Assistant EV entity {entity_id} returned a non-numeric state"
        ) from error
    if not math.isfinite(value):
        raise HomeAssistantError(
            f"Home Assistant EV entity {entity_id} returned a non-finite state"
        )
    return value
