"""PV forecast from Open-Meteo plane-of-array irradiance for each roof face.

Open-Meteo answers one tilt and azimuth per request, so every configured plane
is fetched separately. The hourly generation is the sum over planes of
irradiance relative to the 1000 W/m² rating condition times nameplate power,
scaled by one performance factor calibrated against measured output.
"""

import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from energy_optimizer.config import OpenMeteoPvConfiguration, PvPlaneConfiguration
from energy_optimizer.providers.http import JsonHttpClient
from energy_optimizer.providers.interfaces import (
    PV_GENERATION_SOURCE_ID,
    PvGenerationData,
    SourceMetadata,
)
from energy_optimizer.providers.normalization import (
    as_utc,
    is_fresh,
    validate_finite_non_negative,
    validate_hourly_period,
)

logger = logging.getLogger(__name__)
OPEN_METEO_PV_MIN_INTERVAL_SECONDS = 300
OPEN_METEO_PROVIDER = "open-meteo"
RATED_IRRADIANCE_W_PER_M2 = 1000.0
IRRADIANCE_VARIABLE = "global_tilted_irradiance"
ONE_HOUR = timedelta(hours=1)

type HourStart = datetime
type IrradianceByHour = dict[HourStart, float]
type GenerationByHour = dict[HourStart, float]


class OpenMeteoPvError(RuntimeError):
    """Raised when Open-Meteo irradiance cannot be turned into a PV forecast."""


class OpenMeteoPvImporter:
    """Fetch per-plane irradiance and combine it into one PV forecast."""

    provider = OPEN_METEO_PROVIDER
    minimum_interval_seconds = OPEN_METEO_PV_MIN_INTERVAL_SECONDS

    def __init__(
        self,
        configuration: OpenMeteoPvConfiguration,
        client: httpx.Client | None = None,
    ) -> None:
        self.configuration = configuration
        self._http = JsonHttpClient(client)

    def fetch(
        self,
        start_time: datetime,
        end_time: datetime | None = None,
        *,
        now: datetime | None = None,
    ) -> PvGenerationData:
        retrieved_at = now or datetime.now(timezone.utc)
        logger.debug(
            "event=provider_fetch_started component=open_meteo_pv operation=fetch "
            "start_time=%s end_time=%s planes=%s",
            start_time,
            end_time,
            len(self.configuration.planes),
        )
        try:
            data = self._forecast(start_time, end_time, retrieved_at)
        except Exception as error:
            logger.error(
                "event=provider_fetch_failed component=open_meteo_pv operation=fetch "
                "error_type=%s",
                error.__class__.__name__,
                exc_info=True,
            )
            raise
        logger.info(
            "event=provider_fetch_succeeded component=open_meteo_pv operation=fetch "
            "start_time=%s end_time=%s record_count=%s",
            data.start_time,
            data.expires_at,
            len(data.generation_kw),
        )
        return data

    def is_fresh(self, data: PvGenerationData, *, now: datetime | None = None) -> bool:
        """Check forecast coverage and optional retrieval age."""
        return is_fresh(
            data.retrieved_at,
            self.configuration.max_data_age_seconds,
            expires_at=data.expires_at,
            now=now,
            error_factory=OpenMeteoPvError,
            now_message="Open-Meteo freshness times must include a timezone",
        )

    def _forecast(
        self, start_time: datetime, end_time: datetime | None, retrieved_at: datetime
    ) -> PvGenerationData:
        start = _as_utc(start_time)
        retrieved = _as_utc(
            retrieved_at, "Open-Meteo retrieval time must include a timezone"
        )
        nameplate_generation = self._nameplate_generation()
        end = (
            _as_utc(end_time)
            if end_time is not None
            else _end_of_continuous_coverage(nameplate_generation, start)
        )
        validate_hourly_period(
            start,
            end,
            error_factory=OpenMeteoPvError,
            start_message="Open-Meteo start_time must be aligned to the hour",
            end_message="Open-Meteo end_time must be aligned to the hour",
            order_message="Open-Meteo end_time must be after start_time",
        )
        return PvGenerationData(
            schema_version="1",
            start_time=start,
            interval_minutes=60,
            generation_kw=self._calibrated_generation(nameplate_generation, start, end),
            unit="kW",
            source=SourceMetadata(
                provider=OPEN_METEO_PROVIDER, entity_id=PV_GENERATION_SOURCE_ID
            ),
            retrieved_at=retrieved,
            expires_at=end,
        )

    def _nameplate_generation(self) -> GenerationByHour:
        """Sum irradiance times nameplate power over planes, before losses.

        An hour counts only when every plane reports it, so a plane with a
        shorter forecast cannot silently lower the total.
        """
        per_plane = [
            _irradiance_by_hour(self._retrieve(plane))
            for plane in self.configuration.planes
        ]
        hours_of_every_plane = set.intersection(*(set(series) for series in per_plane))
        generation: GenerationByHour = defaultdict(float)
        for plane, irradiance in zip(self.configuration.planes, per_plane, strict=True):
            for hour in hours_of_every_plane:
                generation[hour] += (
                    irradiance[hour] / RATED_IRRADIANCE_W_PER_M2 * plane.peak_power_kw
                )
        return generation

    def _calibrated_generation(
        self, nameplate_generation: GenerationByHour, start: datetime, end: datetime
    ) -> tuple[float, ...]:
        hours = _hours_between(start, end)
        uncovered = [hour for hour in hours if hour not in nameplate_generation]
        if uncovered:
            raise OpenMeteoPvError(
                "Open-Meteo forecast does not cover the requested hour "
                f"{uncovered[0].isoformat()}"
            )
        factor = self.configuration.performance_factor
        return tuple(nameplate_generation[hour] * factor for hour in hours)

    def _retrieve(self, plane: PvPlaneConfiguration) -> Any:
        configuration = self.configuration

        def status_error(status: int) -> Exception | None:
            if status == 429:
                return OpenMeteoPvError(
                    "Open-Meteo rate limit reached; reduce polling frequency"
                )
            if status >= 400:
                return OpenMeteoPvError(
                    f"Open-Meteo returned HTTP {status} while retrieving irradiance"
                )
            return None

        return self._http.get_json(
            f"{str(configuration.base_url).rstrip('/')}/v1/forecast",
            headers={"Accept": "application/json"},
            params={
                "latitude": f"{configuration.latitude:g}",
                "longitude": f"{configuration.longitude:g}",
                "hourly": IRRADIANCE_VARIABLE,
                "tilt": f"{plane.tilt_degrees:g}",
                "azimuth": f"{plane.azimuth_degrees:g}",
                "timezone": "UTC",
                "forecast_days": configuration.forecast_days,
            },
            timeout_seconds=configuration.timeout_seconds,
            error_factory=OpenMeteoPvError,
            timeout_message=(
                "Open-Meteo request timed out; check the endpoint and timeout"
            ),
            transport_message="Open-Meteo request failed: transport error",
            malformed_message="Open-Meteo returned malformed JSON",
            status_error=status_error,
            log_event="open_meteo_pv_request",
            component="open_meteo_pv",
            operation="forecast",
            log_context=f"source=open-meteo plane={plane.name!r}",
        )


def _as_utc(
    value: datetime,
    message: str = "Open-Meteo forecast times must include a timezone",
) -> datetime:
    return as_utc(value, error_factory=OpenMeteoPvError, message=message)


def _irradiance_by_hour(payload: Any) -> IrradianceByHour:
    """Key irradiance by the start of the hour it was averaged over.

    Open-Meteo labels an hourly value with the end of its averaging interval.
    Hours without a value are left out; whether they matter is decided later
    against the requested period.
    """
    labels, values = _hourly_columns(payload)
    irradiance: IrradianceByHour = {}
    for label, value in zip(labels, values, strict=True):
        if value is None:
            continue
        interval_end = _parse_label(label)
        irradiance[interval_end - ONE_HOUR] = validate_finite_non_negative(
            value,
            error_factory=OpenMeteoPvError,
            label=f"Open-Meteo irradiance at {label!r}",
        )
    return irradiance


def _hourly_columns(payload: Any) -> tuple[list[Any], list[Any]]:
    if not isinstance(payload, dict):
        raise OpenMeteoPvError("Open-Meteo response must be a JSON object")
    if payload.get("utc_offset_seconds") != 0:
        raise OpenMeteoPvError("Open-Meteo response must use UTC timestamps")
    hourly = payload.get("hourly")
    if not isinstance(hourly, dict):
        raise OpenMeteoPvError("Open-Meteo response must contain an hourly object")
    labels = hourly.get("time")
    values = hourly.get(IRRADIANCE_VARIABLE)
    if not isinstance(labels, list) or not isinstance(values, list):
        raise OpenMeteoPvError(
            f"Open-Meteo response must contain hourly time and {IRRADIANCE_VARIABLE}"
        )
    if len(labels) != len(values):
        raise OpenMeteoPvError(
            f"Open-Meteo hourly time and {IRRADIANCE_VARIABLE} differ in length"
        )
    return labels, values


def _parse_label(label: Any) -> datetime:
    if not isinstance(label, str):
        raise OpenMeteoPvError("Open-Meteo hourly timestamps must be strings")
    try:
        timestamp = datetime.fromisoformat(label)
    except ValueError as error:
        raise OpenMeteoPvError(
            f"Open-Meteo returned an invalid timestamp: {label!r}"
        ) from error
    return timestamp.replace(tzinfo=timezone.utc)


def _end_of_continuous_coverage(
    generation: GenerationByHour, start: datetime
) -> datetime:
    if start not in generation:
        raise OpenMeteoPvError(
            "Open-Meteo forecast does not cover the requested start "
            f"{start.isoformat()}"
        )
    end = start
    while end in generation:
        end += ONE_HOUR
    return end


def _hours_between(start: datetime, end: datetime) -> list[HourStart]:
    return [start + ONE_HOUR * index for index in range((end - start) // ONE_HOUR)]
