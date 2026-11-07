"""Infer the effective PV plane layout from measured generation.

Fits measured hourly AC generation against Open-Meteo plane-of-array
irradiance for candidate roof planes with non-negative least squares. The
fitted coefficient of a plane is its effective peak power in kWp, i.e. the
nameplate power already reduced by inverter, temperature and shading losses.
"""

import io
import json
import sys
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
import numpy
from scipy.optimize import nnls

DATA_DIRECTORY = Path.home() / ".energy-optimizer" / "data"
LATITUDE = 50.743
LONGITUDE = 6.846
CONFIGURED_TILT_DEGREES = 35
CONFIGURED_AZIMUTH_DEGREES = 0
CONFIGURED_PEAK_POWER_KW = 18.0
OPEN_METEO_URL = "https://historical-forecast-api.open-meteo.com/v1/forecast"
REQUEST_TIMEOUT_SECONDS = 30
STANDARD_IRRADIANCE_W_PER_M2 = 1000.0
NORTH_ROOF_TILTS_DEGREES = (10, 15, 20, 25, 30, 35, 40, 45)
FACADE_TILT_DEGREES = 90
MINIMUM_REPORTED_PEAK_POWER_KW = 0.05

type HourStart = datetime
type PowerSeries = dict[HourStart, float]


@dataclass(frozen=True)
class Plane:
    name: str
    tilt_degrees: int
    azimuth_degrees: int


@dataclass(frozen=True)
class Fit:
    planes: tuple[Plane, ...]
    effective_peak_power_kw: tuple[float, ...]
    coefficient_of_determination: float
    modelled_kw: numpy.ndarray


def main() -> None:
    configure_utf8_output()
    measured = load_measured_generation()
    hours = sorted(measured)
    irradiance = IrradianceCache(hours[0].date(), hours[-1].date())
    report_configured_model(measured, hours, irradiance)
    report_described_layout(measured, hours, irradiance)
    report_unconstrained_layout(measured, hours, irradiance)


def configure_utf8_output() -> None:
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(encoding="utf-8")


def load_measured_generation() -> PowerSeries:
    history_file = next(DATA_DIRECTORY.glob("pv-generation-history-*.json"))
    history = json.loads(history_file.read_text(encoding="utf-8"))
    start = datetime.fromisoformat(history["start_time"])
    return {
        start + timedelta(hours=offset): float(value)
        for offset, value in enumerate(history["generation_kw"])
        if value is not None
    }


class IrradianceCache:
    """Plane-of-array irradiance per hour start, fetched once per plane.

    Open-Meteo labels each hourly value with the end of its averaging
    interval, while the measured series is labelled with the hour start.
    """

    def __init__(self, first_day: date, last_day: date) -> None:
        self._first_day = first_day
        self._last_day = last_day
        self._by_plane: dict[Plane, PowerSeries] = {}

    def for_plane(self, plane: Plane, hours: list[HourStart]) -> numpy.ndarray:
        if plane not in self._by_plane:
            self._by_plane[plane] = self._fetch(plane)
        series = self._by_plane[plane]
        return numpy.array([series.get(hour, 0.0) for hour in hours])

    def _fetch(self, plane: Plane) -> PowerSeries:
        response = httpx.get(
            OPEN_METEO_URL,
            params={
                "latitude": LATITUDE,
                "longitude": LONGITUDE,
                "start_date": self._first_day.isoformat(),
                "end_date": self._last_day.isoformat(),
                "hourly": "global_tilted_irradiance",
                "tilt": plane.tilt_degrees,
                "azimuth": plane.azimuth_degrees,
                "timezone": "UTC",
            },
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        hourly = response.json()["hourly"]
        return {
            datetime.fromisoformat(label).replace(tzinfo=UTC) - timedelta(hours=1): (
                irradiance or 0.0
            )
            for label, irradiance in zip(
                hourly["time"], hourly["global_tilted_irradiance"], strict=True
            )
        }


def fit_planes(
    planes: tuple[Plane, ...],
    measured_kw: numpy.ndarray,
    hours: list[HourStart],
    irradiance: IrradianceCache,
) -> Fit:
    irradiance_matrix = numpy.column_stack(
        [
            irradiance.for_plane(plane, hours) / STANDARD_IRRADIANCE_W_PER_M2
            for plane in planes
        ]
    )
    peak_power_kw, _ = nnls(irradiance_matrix, measured_kw)
    modelled_kw = irradiance_matrix @ peak_power_kw
    return Fit(
        planes,
        tuple(float(value) for value in peak_power_kw),
        coefficient_of_determination(measured_kw, modelled_kw),
        modelled_kw,
    )


def coefficient_of_determination(
    measured_kw: numpy.ndarray, modelled_kw: numpy.ndarray
) -> float:
    residual_sum = float(numpy.sum((measured_kw - modelled_kw) ** 2))
    total_sum = float(numpy.sum((measured_kw - measured_kw.mean()) ** 2))
    return 1.0 - residual_sum / total_sum


def report_configured_model(
    measured: PowerSeries, hours: list[HourStart], irradiance: IrradianceCache
) -> None:
    configured_plane = Plane(
        "konfiguriert (Süd)", CONFIGURED_TILT_DEGREES, CONFIGURED_AZIMUTH_DEGREES
    )
    measured_kw = numpy.array([measured[hour] for hour in hours])
    configured_kw = (
        irradiance.for_plane(configured_plane, hours)
        / STANDARD_IRRADIANCE_W_PER_M2
        * CONFIGURED_PEAK_POWER_KW
    )
    print("== Aktuelle Konfiguration: 18 kWp, 35°, Süd (ohne Verluste) ==")
    print(f"R² = {coefficient_of_determination(measured_kw, configured_kw):.3f}")
    print_daily_comparison(hours, measured_kw, configured_kw)


def report_described_layout(
    measured: PowerSeries, hours: list[HourStart], irradiance: IrradianceCache
) -> None:
    measured_kw = numpy.array([measured[hour] for hour in hours])
    facade = Plane("Süd-Fassade", FACADE_TILT_DEGREES, 0)
    fits = [
        fit_planes(
            (Plane(f"Norddach {tilt}°", tilt, 180), facade),
            measured_kw,
            hours,
            irradiance,
        )
        for tilt in NORTH_ROOF_TILTS_DEGREES
    ]
    print("\n== Beschriebene Anlage: Norddach (Neigung variiert) + Süd-Fassade ==")
    for fit in fits:
        print_fit(fit)
    best = max(fits, key=lambda fit: fit.coefficient_of_determination)
    print(f"\nBester Fit: {best.planes[0].name}")
    print_daily_comparison(hours, measured_kw, best.modelled_kw)
    print_hourly_profile(hours, measured_kw, best.modelled_kw)


def report_unconstrained_layout(
    measured: PowerSeries, hours: list[HourStart], irradiance: IrradianceCache
) -> None:
    measured_kw = numpy.array([measured[hour] for hour in hours])
    candidates = (
        Plane("Norddach 30°", 30, 180),
        Plane("Ostdach 30°", 30, -90),
        Plane("Westdach 30°", 30, 90),
        Plane("Süddach 35°", 35, 0),
        Plane("Süd-Fassade", FACADE_TILT_DEGREES, 0),
        Plane("horizontal", 0, 0),
    )
    print("\n== Gegenprobe: freie Auswahl aus allen Richtungen ==")
    print_fit(fit_planes(candidates, measured_kw, hours, irradiance))


def print_fit(fit: Fit) -> None:
    layout = ", ".join(
        f"{plane.name}={peak_power:.1f} kWp"
        for plane, peak_power in zip(
            fit.planes, fit.effective_peak_power_kw, strict=True
        )
        if peak_power > MINIMUM_REPORTED_PEAK_POWER_KW
    )
    print(f"R² = {fit.coefficient_of_determination:.3f}  {layout}")


def print_daily_comparison(
    hours: list[HourStart], measured_kw: numpy.ndarray, modelled_kw: numpy.ndarray
) -> None:
    for day in sorted({hour.date() for hour in hours}):
        in_day = numpy.array([hour.date() == day for hour in hours])
        measured_kwh = float(measured_kw[in_day].sum())
        modelled_kwh = float(modelled_kw[in_day].sum())
        print(
            f"  {day}  gemessen {measured_kwh:5.1f} kWh  Modell {modelled_kwh:5.1f} kWh"
        )
    ratio = float(measured_kw.sum() / modelled_kw.sum())
    print(f"  Gesamt-Verhältnis gemessen/Modell = {ratio:.2f}")


def print_hourly_profile(
    hours: list[HourStart], measured_kw: numpy.ndarray, modelled_kw: numpy.ndarray
) -> None:
    print("  Mittleres Tagesprofil (UTC-Stunde: gemessen / Modell kW)")
    for utc_hour in range(6, 17):
        at_hour = numpy.array([hour.hour == utc_hour for hour in hours])
        print(
            f"   {utc_hour:02d}:00  {measured_kw[at_hour].mean():5.2f} / "
            f"{modelled_kw[at_hour].mean():5.2f}"
        )


if __name__ == "__main__":
    main()
