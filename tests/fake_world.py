"""A fake Home Assistant, aWATTar, Forecast.Solar and Open-Meteo behind HTTP.

The world is deterministic in absolute time: every quantity is a function of the
UTC hour of day, so a test can recompute what the service must have read no matter
when it runs. Home Assistant answers history and state requests like the real
REST API, with cumulative energy counters that rise by the hourly load profile.
"""

import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

HOME_ASSISTANT_TOKEN = "test-token"
HISTORY_DAYS = 10
HOUSEHOLD_ENERGY_ENTITY = "sensor.household_energy"
BATTERY_SOC_ENTITY = "sensor.battery_soc"
EV_SOC_ENTITY = "sensor.ev9_battery_level"
EV_PLUG_ENTITY = "binary_sensor.ev9_charging_cable"
EV_SOC_PERCENT = 30
BATTERY_SOC_PERCENT = 50
CHEAP_EUR_PER_MWH = 50.0
NORMAL_EUR_PER_MWH = 150.0
EXPENSIVE_EUR_PER_MWH = 250.0
SUN_WATT_HOURS = 3000.0
ONE_HOUR = timedelta(hours=1)


def household_load_kw(hour_of_day: int) -> float:
    return 1.5 if 17 <= hour_of_day < 22 else 1.0


def market_price_eur_per_mwh(hour_of_day: int) -> float:
    if hour_of_day < 6:
        return CHEAP_EUR_PER_MWH
    return EXPENSIVE_EUR_PER_MWH if 17 <= hour_of_day < 22 else NORMAL_EUR_PER_MWH


def pv_kw(hour_of_day: int) -> float:
    return SUN_WATT_HOURS / 1000 if 10 <= hour_of_day < 16 else 0.0


def plane_irradiance_w_per_m2(hour_of_day: int, tilt_degrees: float) -> float:
    """Rise by 100 W/m² per daylight hour; steep planes catch half of it.

    Neighbouring hours differ, so a value assigned to the wrong hour shows.
    """
    if not 10 <= hour_of_day < 16:
        return 0.0
    roof_irradiance = 100.0 * (hour_of_day - 9)
    return roof_irradiance / 2 if tilt_degrees > 45 else roof_irradiance


def _floor_hour(moment: datetime) -> datetime:
    return moment.replace(minute=0, second=0, microsecond=0)


class FakeWorld:
    """State shared by the request handlers of one fake server."""

    states: dict[str, tuple[str, dict[str, str]]] = {
        BATTERY_SOC_ENTITY: (str(BATTERY_SOC_PERCENT), {"unit_of_measurement": "%"}),
        EV_SOC_ENTITY: (str(EV_SOC_PERCENT), {"unit_of_measurement": "%"}),
        EV_PLUG_ENTITY: ("on", {}),
    }

    def __init__(self) -> None:
        self.history_start = _floor_hour(datetime.now(timezone.utc)) - timedelta(
            days=HISTORY_DAYS
        )
        self.requests: list[str] = []
        self.service_calls: list[tuple[str, dict[str, Any]]] = []

    def counter_kwh(self, boundary: datetime) -> float:
        hours = int((boundary - self.history_start) / ONE_HOUR)
        return sum(
            household_load_kw((self.history_start + ONE_HOUR * hour).hour)
            for hour in range(hours)
        )

    def history(self, start: datetime, end: datetime) -> list[list[dict[str, Any]]]:
        """Answer like Home Assistant: the state in force at ``start``, then changes."""
        newest = _floor_hour(min(end, datetime.now(timezone.utc)))
        records: list[dict[str, Any]] = []
        if start >= self.history_start:
            records.append(self._record(_floor_hour(start), start))
        first_change = max(_floor_hour(start) + ONE_HOUR, self.history_start)
        boundary = first_change
        while boundary <= newest:
            records.append(self._record(boundary, boundary))
            boundary += ONE_HOUR
        return [records]

    def _record(self, boundary: datetime, stamp: datetime) -> dict[str, Any]:
        return {
            "entity_id": HOUSEHOLD_ENERGY_ENTITY,
            "state": f"{self.counter_kwh(boundary):.3f}",
            "last_updated": stamp.isoformat(),
            "attributes": {
                "unit_of_measurement": "kWh",
                "state_class": "total_increasing",
            },
        }

    def prices(self, start: datetime, end: datetime) -> dict[str, Any]:
        intervals = []
        moment = _floor_hour(start)
        while moment < end:
            intervals.append(
                {
                    "start_timestamp": int(moment.timestamp() * 1000),
                    "end_timestamp": int((moment + ONE_HOUR).timestamp() * 1000),
                    "marketprice": market_price_eur_per_mwh(moment.hour),
                    "unit": "Eur/MWh",
                }
            )
            moment += ONE_HOUR
        return {"object": "list", "data": intervals}

    def forecast(self) -> dict[str, Any]:
        today = _floor_hour(datetime.now(timezone.utc)).replace(hour=0)
        periods = {}
        for hour in range(48):
            moment = today + ONE_HOUR * hour
            periods[moment.strftime("%Y-%m-%d %H:%M:%S")] = (
                SUN_WATT_HOURS if 10 < moment.hour <= 16 else 0.0
            )
        return {
            "result": {"watts": dict(periods), "watt_hours_period": periods},
            "message": {"info": {"timezone": "UTC"}},
        }

    def irradiance(self, tilt_degrees: float) -> dict[str, Any]:
        """Answer like Open-Meteo: each value labelled with its hour's end."""
        today = _floor_hour(datetime.now(timezone.utc)).replace(hour=0)
        hour_starts = [today + ONE_HOUR * hour for hour in range(48)]
        return {
            "utc_offset_seconds": 0,
            "hourly": {
                "time": [
                    (start + ONE_HOUR).strftime("%Y-%m-%dT%H:%M")
                    for start in hour_starts
                ],
                "global_tilted_irradiance": [
                    plane_irradiance_w_per_m2(start.hour, tilt_degrees)
                    for start in hour_starts
                ],
            },
        }


def _handler(world: FakeWorld) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            url = urlparse(self.path)
            query = parse_qs(url.query)
            world.requests.append(url.path)
            if url.path.startswith("/api/"):
                if (
                    self.headers.get("Authorization")
                    != f"Bearer {HOME_ASSISTANT_TOKEN}"
                ):
                    self._reply(401, {"message": "unauthorized"})
                    return
            if url.path.startswith("/api/history/period/"):
                start = datetime.fromisoformat(unquote(url.path.rsplit("/", 1)[-1]))
                end = datetime.fromisoformat(query["end_time"][0])
                entity = query["filter_entity_id"][0]
                body = (
                    world.history(start, end)
                    if entity == HOUSEHOLD_ENERGY_ENTITY
                    else [[]]
                )
                self._reply(200, body)
            elif url.path.startswith("/api/states/") and (
                url.path.removeprefix("/api/states/") in world.states
            ):
                entity = url.path.removeprefix("/api/states/")
                state, attributes = world.states[entity]
                self._reply(
                    200,
                    {
                        "entity_id": entity,
                        "state": state,
                        "last_updated": datetime.now(timezone.utc).isoformat(),
                        "attributes": attributes,
                    },
                )
            elif url.path == "/awattar":
                start = datetime.fromtimestamp(
                    int(query["start"][0]) / 1000, timezone.utc
                )
                end = datetime.fromtimestamp(int(query["end"][0]) / 1000, timezone.utc)
                self._reply(200, world.prices(start, end))
            elif url.path.startswith("/estimate/"):
                self._reply(200, world.forecast())
            elif url.path == "/v1/forecast":
                self._reply(200, world.irradiance(float(query["tilt"][0])))
            else:
                self._reply(404, {"message": "not found"})

        def do_POST(self) -> None:
            url = urlparse(self.path)
            if self.headers.get("Authorization") != f"Bearer {HOME_ASSISTANT_TOKEN}":
                self._reply(401, {"message": "unauthorized"})
                return
            if not url.path.startswith("/api/services/"):
                self._reply(404, {"message": "not found"})
                return
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length) or b"{}")
            service = url.path.removeprefix("/api/services/").replace("/", ".", 1)
            world.service_calls.append((service, body))
            self._reply(200, [])

        def _reply(self, status: int, body: object) -> None:
            payload = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args: Any) -> None:
            return

    return Handler


@contextmanager
def running_fake_world() -> Iterator[tuple[str, FakeWorld]]:
    """Serve the fake world on a free local port and yield its base URL."""
    world = FakeWorld()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(world))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", world
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
