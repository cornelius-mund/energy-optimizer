"""Apply the planned EV charge power to the wall box through Home Assistant."""

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Callable

import httpx
from pydantic import TypeAdapter

from energy_optimizer.config import (
    HomeAssistantConfiguration,
    HomeAssistantEvServiceConfiguration,
)
from energy_optimizer.storage import ProviderDataKey, ProviderDataStore

logger = logging.getLogger(__name__)

ONE_HOUR = timedelta(hours=1)
EV_SCHEDULE_KEY = ProviderDataKey("ev-schedule", "energy-optimizer", "current")
SCHEDULE_RETENTION = timedelta(hours=48)
NO_POWER_KW = 0.05


class EvControlError(RuntimeError):
    """Raised when Home Assistant rejects or cannot receive a control command."""


@dataclass(frozen=True)
class EvSchedule:
    """The charge power chosen for each hour by the latest plan that covers it.

    A new plan starts at the next full hour, so the running hour is only known
    from an earlier plan. The schedule therefore keeps the hours of earlier plans
    that the new plan does not cover.
    """

    updated_at: datetime
    hour_starts: tuple[datetime, ...]
    charge_kw: tuple[float, ...]

    def power_at(self, moment: datetime) -> float | None:
        for start, power in zip(self.hour_starts, self.charge_kw, strict=True):
            if start <= moment < start + ONE_HOUR:
                return power
        return None


EV_SCHEDULE_ADAPTER = TypeAdapter(EvSchedule)


def merge_schedule(
    previous: EvSchedule | None,
    updated_at: datetime,
    hour_starts: tuple[datetime, ...],
    charge_kw: tuple[float, ...],
) -> EvSchedule:
    """Let the new plan win for its hours and keep earlier hours before it."""
    kept = [
        (start, power)
        for start, power in zip(
            previous.hour_starts if previous else (),
            previous.charge_kw if previous else (),
            strict=True,
        )
        if start < hour_starts[0] and start > updated_at - SCHEDULE_RETENTION
    ]
    return EvSchedule(
        updated_at=updated_at,
        hour_starts=tuple(start for start, _ in kept) + hour_starts,
        charge_kw=tuple(power for _, power in kept) + charge_kw,
    )


class ControlOutcome(StrEnum):
    APPLIED = "applied"
    UNCHANGED = "unchanged"
    FELL_BACK = "fell_back"
    FAILED = "failed"


class EvController:
    """Set the wall box to the power the plan has for the running hour."""

    def __init__(
        self,
        home_assistant: HomeAssistantConfiguration,
        store: ProviderDataStore,
        client: httpx.Client | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if home_assistant.ev is None or home_assistant.ev.control is None:
            raise EvControlError("EV charger control is not configured")
        self.home_assistant = home_assistant
        self.control = home_assistant.ev.control
        self.maximum_charge_kw = home_assistant.ev.maximum_charge_kw
        self.store = store
        self.client = client or httpx.Client()
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._applied: float | None = None

    def apply_due(self) -> ControlOutcome:
        now = self.clock()
        schedule = self.store.load(EV_SCHEDULE_KEY, EV_SCHEDULE_ADAPTER)
        try:
            planned_kw = self._planned_power(schedule, now)
            if planned_kw is None:
                return self._fall_back()
            return self._apply(planned_kw)
        except EvControlError as error:
            logger.error(
                "event=ev_control_failed component=control operation=apply error=%s",
                error,
            )
            self._applied = None
            return ControlOutcome.FAILED

    def _planned_power(
        self, schedule: EvSchedule | None, now: datetime
    ) -> float | None:
        if schedule is None:
            return None
        age = (now - schedule.updated_at).total_seconds()
        if age > self.control.max_plan_age_seconds:
            logger.warning(
                "event=ev_plan_stale component=control operation=apply "
                "age_seconds=%.0f",
                age,
            )
            return None
        return schedule.power_at(now)

    def _fall_back(self) -> ControlOutcome:
        """Hand the charger back to its own behaviour: enabled, at full power."""
        if self._applied == self.maximum_charge_kw:
            return ControlOutcome.UNCHANGED
        self._send_power(self.maximum_charge_kw)
        self._applied = self.maximum_charge_kw
        logger.warning("event=ev_control_fallback component=control operation=apply")
        return ControlOutcome.FELL_BACK

    def _apply(self, planned_kw: float) -> ControlOutcome:
        power_kw = 0.0 if planned_kw < NO_POWER_KW else planned_kw
        if self._applied == power_kw:
            return ControlOutcome.UNCHANGED
        self._send_power(power_kw)
        self._applied = power_kw
        logger.info(
            "event=ev_control_applied component=control operation=apply power_kw=%.2f",
            power_kw,
        )
        return ControlOutcome.APPLIED

    def _send_power(self, power_kw: float) -> None:
        enable_entity = self.control.enable_entity_id
        if enable_entity is not None:
            self._call(
                "homeassistant.turn_on" if power_kw > 0 else "homeassistant.turn_off",
                {"entity_id": enable_entity},
            )
        if power_kw > 0 or enable_entity is None:
            self._call_set_power(self.control.set_power, power_kw)

    def _call_set_power(
        self, target: HomeAssistantEvServiceConfiguration, power_kw: float
    ) -> None:
        self._call(
            target.service,
            {"entity_id": target.entity_id, target.data_field: self._value(power_kw)},
        )

    def _value(self, power_kw: float) -> float:
        set_power = self.control.set_power
        if set_power.unit == "kW":
            return round(power_kw, 2)
        if set_power.unit == "W":
            return round(power_kw * 1000)
        return round(power_kw * 1000 / (set_power.phases * set_power.voltage))

    def _call(self, service: str, data: dict[str, object]) -> None:
        domain, name = service.split(".", 1)
        base_url = str(self.home_assistant.base_url).rstrip("/")
        try:
            response = self.client.post(
                f"{base_url}/api/services/{domain}/{name}",
                json=data,
                headers={
                    "Authorization": "Bearer "
                    + self.home_assistant.token.get_secret_value()
                },
                timeout=self.home_assistant.timeout_seconds,
            )
        except httpx.HTTPError as error:
            raise EvControlError(
                f"Home Assistant service {service} failed: transport error"
            ) from error
        if response.status_code >= 400:
            raise EvControlError(
                f"Home Assistant service {service} returned HTTP {response.status_code}"
            )

    async def run_forever(self, stop_event: asyncio.Event) -> None:
        interval = self.control.poll_interval_seconds
        while not stop_event.is_set():
            await asyncio.to_thread(self.apply_due)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass
