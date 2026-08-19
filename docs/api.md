# API Reference

## Hourly optimization API

`POST /optimize` validates an hourly request. The request must contain a
timezone-aware `start_time`, `interval_minutes: 60`, and equally sized series
of `load_kw`, `pv_generation_kw`, `import_price_eur_per_kwh`, and
`export_price_eur_per_kwh`. Series contain one value per hour, from one to 168
hours, and values are expressed in kW or EUR/kWh as named by their fields.

Requests that pass validation return a `validated` response containing the
horizon metadata. Invalid JSON or values return HTTP 422 with field-level
validation details. The endpoint is the API boundary for the optimizer; solver
schedule results will be added by a later vertical slice.

## Electricity-price API

`POST /api/v1/electricity-prices` validates normalized hourly import and export
prices. The versioned request contains ascending, unique, timezone-aware
`timestamps` for one to 87,672 hours, spaced by `interval_minutes: 60`, aligned
`import_price_eur_per_kwh` and `export_price_eur_per_kwh` values in `EUR/kWh`,
provider-independent `source` metadata, and timezone-aware `retrieved_at` and
`expires_at` freshness bounds.

Prices may be negative for markets that support negative rates, but must remain
within the documented range of -100 to 100 EUR/kWh. Coverage must begin at or
after retrieval and end before expiry.

Example:

```json
{
  "schema_version": "1",
  "timestamps": [
    "2026-01-01T00:00:00+00:00",
    "2026-01-01T01:00:00+00:00"
  ],
  "interval_minutes": 60,
  "import_price_eur_per_kwh": [0.30, 0.25],
  "export_price_eur_per_kwh": [0.08, 0.08],
  "unit": "EUR/kWh",
  "source": {"provider": "day-ahead-market"},
  "retrieved_at": "2025-12-31T23:00:00+00:00",
  "expires_at": "2026-01-01T03:00:00+00:00"
}
```

The response echoes the normalized prices with `status: "validated"`.
Missing fields, unknown fields, unsupported versions or units, naive or
duplicate timestamps, incorrectly spaced timestamps, misaligned series, stale
coverage, invalid freshness bounds, out-of-range values, and series longer than
ten years (87,672 hourly values) return HTTP 422 with field-level validation
details.

## Battery API

`POST /api/v1/battery` validates normalized hourly battery state and capability
data. The versioned request contains timezone-aware `start_time`,
`interval_minutes: 60`, one to 87,672 `state_of_charge_kwh` values, capacity and
SOC bounds in kWh, initial SOC, charge and discharge power limits in kW, one
battery round-trip efficiency from greater than zero through one,
`unit: "kWh"`, `power_unit: "kW"`, and optional source metadata.

Example:

```json
{
  "schema_version": "1",
  "start_time": "2026-01-01T00:00:00+00:00",
  "interval_minutes": 60,
  "state_of_charge_kwh": [5.0, 5.5],
  "capacity_kwh": 10.0,
  "minimum_soc_kwh": 2.0,
  "maximum_soc_kwh": 10.0,
  "initial_soc_kwh": 5.0,
  "maximum_charge_kw": 4.0,
  "maximum_discharge_kw": 4.0,
  "battery_efficiency": 0.9,
  "unit": "kWh",
  "power_unit": "kW",
  "source": {
    "provider": "home-assistant",
    "entity_id": "sensor.battery_soc"
  }
}
```

The response echoes the validated battery data with `status: "validated"`.
Missing fields, unknown fields, unsupported versions or units, naive
timestamps, out-of-range state of charge, inconsistent limits, invalid
efficiencies, and series longer than ten years (87,672 hourly values) return
HTTP 422 with field-level validation details.

## Home Assistant battery import

The Home Assistant battery provider retrieves a current snapshot from the REST
state endpoint for each configured entity mapping. Static numeric values may
instead be configured as constants with `{value, unit}`. Numeric shorthand uses
the canonical unit for that field. Entity values may come from either the entity
state or a named entity attribute and normalize to the battery API units.
State-of-charge and SOC limits accept `%`, `Wh`, or `kWh`; capacity accepts `Wh`
or `kWh`; power limits accept `W` or `kW`; and efficiencies accept `%` or a
unitless `ratio`.

Configure the provider under `home_assistant.battery` and add the `battery`
source to `orchestration.sources`. The snapshot contains one current
`state_of_charge_kwh` value, uses that value as `initial_soc_kwh`, and records
the oldest Home Assistant observation timestamp across entity-backed values for
freshness checks. Constants do not create requests and do not affect freshness.
All entity mappings are fetched before normalization, so an unavailable or
invalid entity prevents a partial battery snapshot from being persisted. HTTP
authentication failures, missing entities, malformed values, invalid timestamps,
inconsistent SOC limits, and stale data are reported as provider errors or stale
orchestration runs.

Static battery values can use the following form:

```yaml
battery:
  state_of_charge:
    entity_id: sensor.battery_state_of_charge
    unit: '%'
  capacity: {value: 28.7, unit: kWh}
  minimum_soc: {value: 5, unit: '%'}
  maximum_soc: {value: 100, unit: '%'}
  maximum_charge: {value: 12, unit: kW}
  maximum_discharge: {value: 12, unit: kW}
  battery_efficiency: {value: 0.85, unit: ratio}
  efficiency_calculation:
    state_of_charge: {entity_id: sensor.battery_state_of_charge, unit: '%'}
    history_start: 2020-01-01T00:00:00+00:00
    full_soc_threshold_percent: 100
    battery:
      energy_in: [{entity_id: sensor.battery_energy_in, state_class: total_increasing, unit: kWh, operation: add}]
      energy_out: [{entity_id: sensor.battery_energy_out, state_class: total_increasing, unit: kWh, operation: add}]
    inverter_charge:
      energy_in: [{entity_id: sensor.ac_into_inverter, state_class: total_increasing, unit: kWh, operation: add}]
      energy_out: [{entity_id: sensor.battery_energy_in, state_class: total_increasing, unit: kWh, operation: add}, {entity_id: sensor.mppt_energy, state_class: total_increasing, unit: kWh, operation: subtract}]
    inverter_discharge:
      energy_in: [{entity_id: sensor.battery_energy_out, state_class: total_increasing, unit: kWh, operation: add}, {entity_id: sensor.mppt_energy, state_class: total_increasing, unit: kWh, operation: subtract}]
      energy_out: [{entity_id: sensor.inverter_to_ac, state_class: total_increasing, unit: kWh, operation: add}]
```

Calculated efficiency is configured separately under `battery.efficiency_calculation`.
It contains signed `energy_in` and `energy_out` entity lists for the battery,
inverter charge, and inverter discharge components, plus the historical state of
charge entity. Battery efficiency is one full-cycle round-trip value detected
from consecutive full-SoC boundaries. Inverter charge and discharge efficiencies
are independent inverter measurements. The complete ratio is their product with
the battery ratio. A configured `battery_efficiency` takes precedence over the
calculated battery value and emits a warning. The dashboard exposes these values
through `scenario_kind=efficiency`. Before the first complete full-SoC cycle is
available (or while the calculated result is otherwise not `"ok"`), the live
battery snapshot uses a documented 95% default for `battery_efficiency` instead
of failing; this only applies to calculated mode without a fixed
`battery_efficiency` override.

Ingestion persists the aligned hourly history under its own record and requests
only the hours after the previously persisted history on every scheduled run,
so the daily recompute does not re-fetch the complete history from Home
Assistant each time. The calculator itself uses all retained history, or all
history since `history_start`, and never uses a rolling window.

The state-of-charge validation checks physical plausibility, not round-trip
loss: during an hour with only charging (or only discharging) energy measured,
the stored energy change can never exceed what was delivered, nor exceed what
was removed from storage, beyond `soc_balance_tolerance_kwh`. A violation
means the measured data is inconsistent (e.g. a misconfigured or drifting
entity), not that the battery has ordinary conversion losses.

Live state, cumulative counters, and historical measurements remain provider
backed; constants are intended for static installation parameters only.

Calculated efficiency ingestion reconstructs historical state of charge through
the dedicated history endpoint and persists aligned hourly battery/inverter
measurements before the daily calculation.

## Household-load API

`POST /api/v1/household-load` validates a normalized hourly household-load
series. When the source matches the configured Home Assistant provider, its
persisted history is merged by hourly timestamp with incoming values taking
precedence and a maximum of 87,672 values retained. The versioned request
contains:

- `schema_version: "1"`
- A timezone-aware `start_time`
- `interval_minutes: 60`
- `load_kw`, containing one non-negative value per hour for one to 87,672 hours
- `unit: "kW"`
- Optional `source` metadata with a provider and entity identifier
- Timezone-aware `retrieved_at` and `latest_observation_at` metadata

Example:

```json
{
  "schema_version": "1",
  "start_time": "2026-01-01T00:00:00+00:00",
  "interval_minutes": 60,
  "load_kw": [1.2, 1.0],
  "unit": "kW",
  "source": {
    "provider": "home-assistant",
    "entity_id": "sensor.household_load"
  },
  "retrieved_at": "2026-01-01T00:00:00+00:00",
  "latest_observation_at": "2026-01-01T01:00:00+00:00"
}
```

The response echoes the normalized data with `status: "validated"`. Missing
fields, unknown fields, unsupported versions or units, naive timestamps,
invalid values, and series longer than ten years (87,672 hourly values) return
HTTP 422 with field-level validation details. Historical data is not rejected
because it is old; polling health is assessed separately with the provider's
optional freshness threshold.

When configured, `POST /api/v1/household-load` persists data only when its
source matches the configured Home Assistant household-load provider.
Source-less submissions and other providers are validated and returned but are
not persisted. Matching household-load submissions merge by hourly timestamp,
with incoming values overwriting duplicates and the oldest values removed
beyond 87,672 hours. `GET /api/v1/household-load` retrieves the latest persisted
normalized provider data. Battery and electric-vehicle persistence will use the
same store when their normalized provider contracts are available;
optimizer-owned state transitions remain outside this persistence boundary.

## Historic household-load dashboard

Open `/dashboard/` to inspect imported household-load actuals. The dashboard
accepts UTC date-and-time boundaries, including sub-day ranges, and uses the
read-only endpoint
`GET /api/v1/historic/household-load?start_time=<inclusive>&end_time=<exclusive>`.
The endpoint returns explicit hourly timestamps, `kW` values, source identity,
requested and available coverage, retrieval metadata, validation status, and
polling freshness. A response with `status: "stale"` still contains valid
historical actuals; it means only that the newest observation is older than the
configured polling threshold. `status: "empty"` means the persisted history
exists but has no observations in the requested range. Corrupt or unrecoverable
persistence returns HTTP 503 rather than data that could be mistaken for valid
actuals.

The view labels the series as historic actuals and deliberately does not mix it
with predicted inputs or optimization plans. The current slice displays
household load; additional asset series can use the same dashboard contract as
their provider imports become available.

The range controls use an end-exclusive boundary: the end time must be later
than the start time. If a requested actual or forecast range falls outside the
available coverage, the dashboard replaces both controls with the available
coverage and loads that range. When forecast providers have different
coverage, the controls use the union of their available ranges so valid price
and PV points are retained; each chart shows the other provider's missing
intervals as gaps. If forecast coverage is unavailable, it keeps the
unavailable state instead of inventing a range. The x-axis labels include each
point's UTC date and time; chart points also expose their exact timestamp and
value on pointer hover and keyboard focus.

The dashboard also provides a Forecast tab backed by
`GET /api/v1/dashboard/data?scenario_kind=forecast`. Forecast series identify
their source, unit, coverage, retrieval time, and freshness. PV generation uses
`kW`; market prices use `EUR/kWh`. The Forecast tab renders power and price data
in separate charts, each with its own unit axis, data-driven scale, legend
labels, and accessible description. Each scale uses finite visible values with
small padding, while flat or empty data receives a safe non-zero fallback
domain. All boundaries and point timestamps are UTC hourly half-open ranges.
Missing or partial observations remain gaps and are not interpolated or treated
as zero. Chart points expose their exact timestamp and value with the series
unit on pointer hover and keyboard focus.

The Docker image sets `ENERGY_OPTIMIZER_FRONTEND_DIRECTORY=/app/frontend` so the
dashboard remains available after the Python application is installed into the
image. Source-tree deployments may omit this setting when the repository's
`frontend/` directory is present.

Dashboard diagnostics use the browser console when investigating an
unresponsive tab: tab clicks are logged at `info`, routine initialization and
successful data loads at `debug`, unavailable responses at `warn`, and request
failures at `error`. The service logs dashboard requests at `INFO` with the
validated `scenario_kind` and request ID. Forecast responses with unavailable
data also emit a `WARNING` diagnostic; the request ID links that diagnostic to
the request log without recording query payloads or provider credentials.

## PV-generation API

`POST /api/v1/pv-generation` validates a normalized hourly PV-generation
series. The versioned request contains:

- `schema_version: "1"`
- A timezone-aware `start_time`
- `interval_minutes: 60`
- `generation_kw`, containing one non-negative value per hour for one to 87,672 hours
- `unit: "kW"`
- Optional `source` metadata with a provider and entity identifier

Example:

```json
{
  "schema_version": "1",
  "start_time": "2026-01-01T00:00:00+00:00",
  "interval_minutes": 60,
  "generation_kw": [0.0, 2.4],
  "unit": "kW",
  "source": {
    "provider": "home-assistant",
    "entity_id": "sensor.pv_generation"
  }
}
```

The response echoes the normalized series with `status: "validated"`. Missing
fields, unknown fields, unsupported versions or units, naive timestamps,
invalid values, and series longer than ten years (87,672 hourly values) return
HTTP 422 with field-level validation details.

## Grid-flow API

`POST /api/v1/grid-flow` validates normalized hourly grid import and export
data. The versioned request contains timezone-aware `start_time`,
`interval_minutes: 60`, equally sized non-negative `import_kw` and `export_kw`
series for one to 87,672 hours, `unit: "kW"`, optional source metadata, and
timezone-aware `retrieved_at` and `latest_observation_at` metadata.

Example:

```json
{
  "schema_version": "1",
  "start_time": "2026-01-01T00:00:00+00:00",
  "interval_minutes": 60,
  "import_kw": [1.2, 1.0],
  "export_kw": [0.0, 0.4],
  "unit": "kW",
  "source": {
    "provider": "home-assistant",
    "entity_id": "grid_flow"
  },
  "retrieved_at": "2026-01-01T00:00:00+00:00",
  "latest_observation_at": "2026-01-01T01:00:00+00:00"
}
```

The response echoes the normalized import and export series with
`status: "validated"`. Missing fields, unknown fields, unsupported versions
or units, naive timestamps, mismatched series lengths, invalid values, and
series longer than ten years (87,672 hourly values) return HTTP 422 with
field-level validation details. `GET /api/v1/grid-flow` returns the latest
persisted configured Home Assistant record, or HTTP 404 when none is available.
The endpoint stores only the latest record; historic range retention is deferred
to the unified historic multi-asset API.

The health endpoint returns the service status and version, for example:

```json
{"status":"ok","version":"0.1.0"}
```
