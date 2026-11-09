"""Remind the owner to plug in the car when the plan charges it tonight.

The car is home every night but plugged in only when needed, so a plan that
charges at night needs the owner to plug it in. From the reminder time until the
end of the night's home hours, the latest EV schedule is checked; if it charges
in the rest of the night and the car is not plugged in, every configured notify
service is called once for that night.
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Callable
from zoneinfo import ZoneInfo

import httpx
from pydantic import TypeAdapter

from energy_optimizer.config import (
    EvHomeHoursConfiguration,
    EvPlugInReminderConfiguration,
    HomeAssistantConfiguration,
)
from energy_optimizer.control.ev_controller import (
    EV_SCHEDULE_ADAPTER,
    EV_SCHEDULE_KEY,
    NO_POWER_KW,
    EvSchedule,
)
from energy_optimizer.control.home_assistant_service import (
    HomeAssistantServiceError,
    call_service,
)
from energy_optimizer.providers.home_assistant_ev import HomeAssistantEvImporter
from energy_optimizer.providers.home_assistant_history import HomeAssistantError
from energy_optimizer.storage import ProviderDataKey, ProviderDataStore

logger = logging.getLogger(__name__)
ONE_HOUR = timedelta(hours=1)
REMINDER_KEY = ProviderDataKey("ev-plug-in-reminder", "energy-optimizer", "last")


@dataclass(frozen=True)
class SentReminder:
    """The night (by the date it starts) for which the reminder went out."""

    night: date


REMINDER_ADAPTER = TypeAdapter(SentReminder)


@dataclass(frozen=True)
class Night:
    starts_on: date
    remind_from: datetime
    start: datetime
    end: datetime


@dataclass(frozen=True)
class PlannedNightCharge:
    energy_kwh: float
    first_hour: datetime


def current_night(
    now: datetime,
    home_hours: EvHomeHoursConfiguration,
    remind_at: time,
    time_zone: ZoneInfo,
) -> Night | None:
    """The night whose reminder time has passed and whose home hours have not
    ended; the hours after midnight still belong to the evening before."""
    today = now.astimezone(time_zone).date()
    for starts_on in (today, today - timedelta(days=1)):
        night = _night(starts_on, home_hours, remind_at, time_zone)
        if night.remind_from <= now < night.end:
            return night
    return None


def _night(
    starts_on: date,
    home_hours: EvHomeHoursConfiguration,
    remind_at: time,
    time_zone: ZoneInfo,
) -> Night:
    wraps_midnight = home_hours.end <= home_hours.start
    ends_on = starts_on + timedelta(days=1) if wraps_midnight else starts_on
    return Night(
        starts_on=starts_on,
        remind_from=datetime.combine(starts_on, remind_at, tzinfo=time_zone),
        start=datetime.combine(starts_on, home_hours.start, tzinfo=time_zone),
        end=datetime.combine(ends_on, home_hours.end, tzinfo=time_zone),
    )


def planned_night_charge(
    schedule: EvSchedule, night: Night, now: datetime
) -> PlannedNightCharge | None:
    """Energy the schedule charges from now (or the night's start) to its end."""
    from_moment = max(night.start, now.replace(minute=0, second=0, microsecond=0))
    charging = [
        (start, power)
        for start, power in zip(schedule.hour_starts, schedule.charge_kw, strict=True)
        if from_moment <= start < night.end and power >= NO_POWER_KW
    ]
    if not charging:
        return None
    return PlannedNightCharge(
        energy_kwh=sum(power for _, power in charging),
        first_hour=min(start for start, _ in charging),
    )


class PlugInReminder:
    """Send the plug-in reminder at most once per night."""

    def __init__(
        self,
        home_assistant: HomeAssistantConfiguration,
        home_hours: EvHomeHoursConfiguration,
        settings: EvPlugInReminderConfiguration,
        time_zone: ZoneInfo,
        store: ProviderDataStore,
        client: httpx.Client | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.home_assistant = home_assistant
        self.home_hours = home_hours
        self.settings = settings
        self.time_zone = time_zone
        self.store = store
        self.client = client or httpx.Client()
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.ev_importer = HomeAssistantEvImporter(home_assistant, self.client)

    def remind_due(self) -> bool:
        """Return whether a reminder was sent."""
        now = self.clock()
        night = current_night(now, self.home_hours, self.settings.at, self.time_zone)
        if night is None or self._already_sent(night):
            return False
        schedule = self.store.load(EV_SCHEDULE_KEY, EV_SCHEDULE_ADAPTER)
        charge = (
            None if schedule is None else planned_night_charge(schedule, night, now)
        )
        if charge is None or self._plugged_in(now):
            return False
        return self._send(night, charge)

    def _already_sent(self, night: Night) -> bool:
        sent = self.store.load(REMINDER_KEY, REMINDER_ADAPTER)
        return sent is not None and sent.night == night.starts_on

    def _plugged_in(self, now: datetime) -> bool:
        """An unknown plug state counts as plugged in: no reminder on a guess."""
        try:
            return self.ev_importer.fetch(now=now).plugged_in
        except HomeAssistantError as error:
            logger.warning(
                "event=plug_in_reminder_state_unknown component=control "
                "operation=remind error=%s",
                error,
            )
            return True

    def _send(self, night: Night, charge: PlannedNightCharge) -> bool:
        message = self.settings.message.format(
            energy_kwh=charge.energy_kwh,
            start=charge.first_hour.astimezone(self.time_zone).strftime("%H:%M"),
        )
        try:
            for service in self.settings.services:
                call_service(
                    self.home_assistant,
                    self.client,
                    service,
                    {"title": self.settings.title, "message": message},
                )
        except HomeAssistantServiceError as error:
            logger.error(
                "event=plug_in_reminder_failed component=control operation=remind "
                "error=%s",
                error,
            )
            return False
        self.store.save(REMINDER_KEY, REMINDER_ADAPTER, SentReminder(night.starts_on))
        logger.info(
            "event=plug_in_reminder_sent component=control operation=remind "
            "energy_kwh=%.1f first_hour=%s",
            charge.energy_kwh,
            charge.first_hour.isoformat(),
        )
        return True

    async def run_forever(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            await asyncio.to_thread(self.remind_due)
            try:
                await asyncio.wait_for(
                    stop_event.wait(), timeout=self.settings.poll_interval_seconds
                )
            except asyncio.TimeoutError:
                pass
