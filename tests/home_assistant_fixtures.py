"""Shared factories for Home Assistant provider tests."""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, TypeVar

import httpx

from energy_optimizer.config import (
    HomeAssistantConfiguration,
    HomeAssistantEnergyEntityConfiguration,
)
from energy_optimizer.providers.home_assistant_energy import (
    EnergyAggregate,
    HomeAssistantEnergySeries,
)
from energy_optimizer.providers.home_assistant_history import (
    HistoryPlan,
    HomeAssistantHistoryImporter,
)

ImporterT = TypeVar("ImporterT")
DataT = TypeVar("DataT")
DataT_co = TypeVar("DataT_co", covariant=True)
ConfigurationFactory = Callable[..., HomeAssistantConfiguration]


class HistoryPlanningImporter(Protocol[DataT_co]):
    """An importer that declares Home Assistant history needs and builds a record."""

    @property
    def configuration(self) -> HomeAssistantConfiguration: ...

    def plan(self, *args: Any, **kwargs: Any) -> HistoryPlan[DataT_co]: ...


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


def home_assistant_planning_importer_factory(
    importer_type: Callable[[HomeAssistantConfiguration], ImporterT],
    configuration: ConfigurationFactory,
) -> Callable[..., tuple[ImporterT, httpx.Client]]:
    """Build a factory for importers that declare needs instead of fetching.

    Such importers make no requests themselves, so the mocked client is returned
    next to the importer for :func:`import_and_build` to use and for the test to
    close.
    """

    def create_importer(
        handler: httpx.MockTransport | httpx.BaseTransport,
        **configuration_overrides: Any,
    ) -> tuple[ImporterT, httpx.Client]:
        client = httpx.Client(transport=handler)
        return importer_type(configuration(**configuration_overrides)), client

    return create_importer


@dataclass(frozen=True)
class HistoryRequest:
    """One history request received by :class:`FakeHomeAssistant`."""

    entity_id: str
    start_time: datetime
    end_time: datetime


@dataclass
class FakeHomeAssistant:
    """A Home Assistant history endpoint that records every request.

    It answers like Home Assistant: the state in force at the requested start,
    stamped with that start, followed by every state change up to the requested
    end. ``states`` maps each entity to its ``(timestamp, state)`` changes in
    ascending order. Entities listed in ``failures`` answer with that HTTP status.
    """

    states: Mapping[str, Sequence[tuple[datetime, str]]]
    units: Mapping[str, str] = field(default_factory=dict)
    state_classes: Mapping[str, str] = field(default_factory=dict)
    failures: dict[str, int] = field(default_factory=dict)
    requests: list[HistoryRequest] = field(default_factory=list)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        entity_id = request.url.params["filter_entity_id"]
        start_time = datetime.fromisoformat(request.url.path.rsplit("/", 1)[-1])
        end_time = datetime.fromisoformat(request.url.params["end_time"])
        self.requests.append(HistoryRequest(entity_id, start_time, end_time))
        if entity_id in self.failures:
            return httpx.Response(self.failures[entity_id])
        return httpx.Response(200, json=self.payload(entity_id, start_time, end_time))

    def payload(
        self, entity_id: str, start_time: datetime, end_time: datetime
    ) -> list[list[dict[str, Any]]]:
        """Return the history response for one entity and requested period."""
        in_force: tuple[datetime, str] | None = None
        changes: list[tuple[datetime, str]] = []
        for timestamp, state in self.states.get(entity_id, ()):
            if timestamp <= start_time:
                in_force = (timestamp, state)
            elif timestamp <= end_time:
                changes.append((timestamp, state))
        answered = ([(start_time, in_force[1])] if in_force else []) + changes
        return [
            [
                {
                    "entity_id": entity_id,
                    "state": state,
                    "last_updated": timestamp.isoformat(),
                    "attributes": {
                        "unit_of_measurement": self.units.get(entity_id, "kWh"),
                        "state_class": self.state_classes.get(
                            entity_id, "total_increasing"
                        ),
                    },
                }
                for timestamp, state in answered
            ]
        ]

    def client(self) -> httpx.Client:
        """Return a client whose transport is this endpoint."""
        return httpx.Client(transport=httpx.MockTransport(self))

    def requested_entities(self) -> list[str]:
        """Return the distinct requested entities in first-request order."""
        return list(dict.fromkeys(request.entity_id for request in self.requests))

    def requested_ranges(self, entity_id: str) -> list[tuple[datetime, datetime]]:
        """Return the chunk ranges requested for one entity, in request order."""
        return [
            (request.start_time, request.end_time)
            for request in self.requests
            if request.entity_id == entity_id
        ]


def plan_without_needs(build: Callable[[], DataT]) -> HistoryPlan[DataT]:
    """Plan a fake source that declares no history and builds its record itself."""
    return HistoryPlan(needs=(), build=lambda history: build())


def import_and_build(
    importer: HistoryPlanningImporter[DataT_co],
    client: httpx.Client,
    *arguments: Any,
    **keywords: Any,
) -> DataT_co:
    """Run the plan, import, and build phases for one importer in isolation.

    This is what the orchestrator does for a single source: plan, import exactly
    the declared needs through ``client``, then build from the imported history.
    """
    plan = importer.plan(*arguments, **keywords)
    history = HomeAssistantHistoryImporter(
        importer.configuration, client
    ).import_history(plan.needs)
    return plan.build(history)


def import_and_aggregate(
    configuration: HomeAssistantConfiguration,
    client: httpx.Client,
    entities: list[HomeAssistantEnergyEntityConfiguration] | None,
    start_time: datetime,
    end_time: datetime,
    history_lookback_seconds: float = 0,
    *,
    label: str,
    allow_negative: bool = False,
) -> HomeAssistantEnergySeries:
    """Import exactly what one signed energy expression needs and build it."""
    aggregate = EnergyAggregate(
        entities,
        start_time,
        end_time,
        history_lookback_seconds,
        label=label,
        allow_negative=allow_negative,
    )
    history = HomeAssistantHistoryImporter(configuration, client).import_history(
        aggregate.needs()
    )
    return aggregate.build(history)


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


def home_assistant_jittery_total_readings(
    base: float, dip_hour: int, hours: int = 4, dip_kwh: float = 0.001
) -> Readings:
    """Return hourly ``total`` counter readings that rise 1 kWh per hour.

    Hour ``dip_hour`` additionally holds three samples in which the counter dips
    by ``dip_kwh`` and recovers at the next sample. The default 1 Wh dip is the
    jitter observed on a real installation (3280.294 to 3280.293 kWh without
    ``last_reset``). Every other step is non-decreasing, so each hour's energy
    is exactly 1 kWh and a recovered dip that is counted twice shows up as
    1.001 kWh.
    """
    readings: Readings = []
    for hour in range(hours + 1):
        readings.append((f"2026-01-01T{hour:02d}:00:00+00:00", f"{base + hour:.3f}"))
        if hour == dip_hour:
            peak = base + hour + 0.5
            readings += [
                (f"2026-01-01T{hour:02d}:30:00+00:00", f"{peak:.3f}"),
                (f"2026-01-01T{hour:02d}:30:12+00:00", f"{peak - dip_kwh:.3f}"),
                (f"2026-01-01T{hour:02d}:30:24+00:00", f"{peak:.3f}"),
            ]
    return readings


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
