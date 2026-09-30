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
      energy_in: [{entity_id: sensor.battery_energy_out, state_class: total_increasing, unit: kWh, operation: add}, {entity_id: sensor.battery_energy_in, state_class: total_increasing, unit: kWh, operation: subtract}, {entity_id: sensor.mppt_energy, state_class: total_increasing, unit: kWh, operation: add}]
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

The dashboard Efficiency tab renders each available component as one accessible
numeric summary row with its raw `ratio` value. These values summarize the
complete retained battery and inverter history; they are not hourly chart
observations and the tab does not offer a selectable calculation time window.
If a component cannot be calculated, it uses the `0.95` ratio fallback and is
marked as `defaulted`, `unavailable`, or `invalid` in the response and UI. A
measured component is `calculated`; complete round-trip efficiency is
`calculated_with_defaults` when any input uses a fallback. Each series carries
its structured `calculation_status`, and the UI renders exactly one annotation
per row from it: `(calculated)`, `(default)`, `(unavailable)`, `(invalid)`, or
`(calculated with defaults)`. The `is_default` flag remains in the response as
compatibility metadata that is `true` for the three fallback statuses
(`defaulted`, `unavailable`, `invalid`) and `false` otherwise; the UI does not
render it separately, so a fallback is never annotated twice. The fallback
annotations carry a tooltip explaining that the fallback ratio was used. The
source, coverage, freshness, retrieval, and calculation diagnostics remain
available beside the summary. The response also exposes battery throughput,
inverter charge and discharge throughput, and `Completed battery cycles` as
structured scalar metrics rather than diagnostics. The UI formats every
displayed ratio to four digits after the decimal point while retaining the raw
API value in the data element. It formats the three throughput metrics, which
carry the `kWh` unit, to exactly two digits after the decimal point, including
trailing zeroes for whole and zero values such as `5.00 kWh` and `0.00 kWh`,
and shows `Completed battery cycles` as a whole number. The metric values in
the API response are not rounded.

Ingestion persists the aligned hourly history under its own record and requests
only the hours after the previously persisted history on every scheduled run,
so the daily recompute does not re-fetch the complete history from Home
Assistant each time. Empty Home Assistant chunks before an entity's retained
history begins are skipped during bootstrap; the resulting history starts at
the earliest complete retained hour and does not fabricate earlier values. The
calculator itself uses all retained history, or all history since `history_start`,
and never uses a rolling window.

Each leg's signed `energy_in`/`energy_out` expression is a net directional
energy flow, not an absolute cumulative total. For a DC-coupled installation,
`inverter_charge.energy_out` above nets the battery's total charging energy
against the directly consumed PV yield to isolate the AC-sourced share; in any
hour where PV production exceeds the battery's charging energy, that
expression's net value is negative because the surplus was exported rather
than stored. Likewise `inverter_discharge.energy_in` is the energy the inverter
draws from the DC bus: PV yield reaches the inverter directly, so it is added to
the battery's discharge energy, and the energy the battery took in during the same
hour is subtracted because it never reached the inverter. In any hour where the
battery takes in more than it discharges plus the PV yield, that expression is
negative because the inverter was not discharging. A negative combined value is
never clamped and never fails the refresh: like every other invalid data point it
excludes the hour (`combined_negative`), and the hour is excluded in all six
energy legs and the state of charge. Hours excluded this way are listed on the
dashboard's Excluded hours tab and through
`GET /api/v1/dashboard/excluded-hours`. A full-charge cycle that contains an
excluded hour is not used for the ratios, so the result rests on fewer cycles
instead of a wrong value. Model such a leg so that its net value is not routinely
negative, or accept that those hours are excluded.

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

The response echoes the normalized data with `status: "validated"`. In a
response, `load_kw` holds `null` for an hour that the Home Assistant import
excluded; a submission never contains `null`. Missing
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
normalized provider data. Optimizer-owned state transitions remain outside this
persistence boundary.

## Historic multi-asset dashboard

Open `/dashboard/` to inspect imported actuals. The Historic actuals tab and every
other consumer read one endpoint using the unified dashboard contract:

```text
GET /api/v1/dashboard/data?scenario_kind=actual&start_time=<inclusive>&end_time=<exclusive>
```

`scenario_kind=actual` is the default. The range follows the same rules for every
scenario: both boundaries are timezone-aware and normalized to UTC, must be
aligned to the hour, `end_time` must be later than `start_time`, and the range must
not exceed 87,672 hours; violations return HTTP 422. The range is half-open
(inclusive start, exclusive end).

### Series

Every available asset contributes series of `scenario_kind: "actual"` in this
order. Each series carries one value for every requested hour, with `null` for an
hour without an observation, and lists those hours in `missing_intervals`.
Missing hours are never interpolated or replaced by zero.

| Series id | Data type | Unit | Source |
| --- | --- | --- | --- |
| `household_load_actual` | `household_load` | `kW` | Home Assistant household-load history |
| `grid_import_actual`, `grid_export_actual` | `grid_import`, `grid_export` | `kW` | Home Assistant grid-flow history |
| `import_price_actual`, `export_price_actual` | `import_price`, `export_price` | `EUR/kWh` | Retained aWATTar market-price history |
| `battery_state_of_charge_actual` | `battery_state_of_charge` | `%` | Retained battery state-of-charge history |

Every series identifies its `source`, requested and `available_*` coverage,
`retrieved_at`, `freshness`, and `validation_status`:

- `freshness` is `fresh` or `stale` when a polling threshold is configured
  (`home_assistant.max_data_age_seconds` for load and grid flow,
  `awattar.max_data_age_seconds` for prices, and twice the
  `battery_efficiency` source interval for battery state) and `unknown`
  otherwise. Stale data is still valid historical data: only the newest
  observation is older than the polling threshold.
- `validation_status` is `valid` for every historic series. An hour that was
  excluded from the import (see "Excluded hours" below) has no value: it is
  `null` and listed in `missing_intervals`, so the top-level status is `partial`.
- Prices are those that applied during completed hours. Hours that have not yet
  elapsed are forecasts and are only available through `scenario_kind=forecast`.
  The price history is a separate record from the replace-latest forecast run, so
  the Forecast tab is unaffected. Hours without a published price stay explicit
  gaps.
- Battery state of charge is the sample at the start of each hour, in percent. It
  comes from the retained history used for the calculated battery efficiency, so
  it is available only when `home_assistant.battery.efficiency_calculation` is
  configured.

Historic actuals never contain forecasts or optimizer plan snapshots.

### Response status and asset availability

The top-level `status` summarizes the returned series: `validated` when every
requested hour is covered, `partial` when some hours are missing or coverage is
shorter than the range, `stale` when any series is stale, `empty` when history
exists but holds no observation in the range, `unavailable` when no series can be
returned, and `invalid` when no series can be returned and at least one asset's
persisted data was withheld as corrupt. An asset that is absent or unusable never
invalidates the series of other assets.

`assets` lists one entry for each of `household_load`, `pv_generation`,
`grid_flow`, `electricity_prices`, `battery`, `electric_vehicle`, and `heat_pump`
so a client can tell what is missing and why:

| Asset status | Meaning |
| --- | --- |
| `available` | Series were returned. |
| `empty` | History exists but has no observation in the requested range. |
| `stale` | Series were returned; the newest observation exceeds the polling threshold. |
| `not_configured` | The installation has no source for this asset. This is not a warning. |
| `unavailable` | The asset is configured but persistence is missing or no data has been persisted yet. |
| `invalid` | Persisted data is corrupt or could not be recovered from its backup and is withheld. |

Every status other than `available` carries an actionable `reason`. Assets in
the `empty`, `stale`, `unavailable`, and `invalid` states are also summarized in
`diagnostics`; `not_configured` assets are not, unless nothing at all is
configured. Corrupt or unrecoverable persisted data is never returned as valid
actuals; the technical cause is written to the service log with the request ID
rather than returned to clients. The dashboard endpoint reports these states in
the response body (HTTP 200); `GET /api/v1/historic/household-load` keeps
returning HTTP 503 for corrupt household-load persistence.

PV-generation actuals, electric-vehicle, and heat-pump history are reported as
`not_configured` until an importer for them persists normalized history. PV
forecasts are available through `scenario_kind=forecast`. A new importer only
needs to register a loader in `energy_optimizer.api.historic`; the envelope does
not change.

Example (abridged) for a two-hour range with household load and grid flow:

```json
{
  "schema_version": "1",
  "status": "validated",
  "requested_start_time": "2026-01-01T00:00:00Z",
  "requested_end_time": "2026-01-01T02:00:00Z",
  "interval_minutes": 60,
  "series": [
    {
      "id": "household_load_actual",
      "data_type": "household_load",
      "scenario_kind": "actual",
      "timestamps": ["2026-01-01T00:00:00Z", "2026-01-01T01:00:00Z"],
      "values": [1.2, 1.0],
      "unit": "kW",
      "source": {"provider": "home-assistant", "entity_id": "household_load"},
      "available_start_time": "2026-01-01T00:00:00Z",
      "available_end_time": "2026-01-01T02:00:00Z",
      "retrieved_at": "2026-01-01T02:00:00Z",
      "freshness": "fresh",
      "validation_status": "valid",
      "missing_intervals": []
    }
  ],
  "assets": [
    {"asset": "household_load", "status": "available",
     "series_ids": ["household_load_actual"], "reason": null},
    {"asset": "pv_generation", "status": "not_configured", "series_ids": [],
     "reason": "no historic PV-generation importer is available; PV forecasts are served by scenario_kind=forecast"},
    {"asset": "grid_flow", "status": "unavailable", "series_ids": [],
     "reason": "no persisted grid-flow data is available yet"}
  ],
  "diagnostics": ["grid flow: no persisted grid-flow data is available yet"]
}
```

### Retention and data availability

Household load and grid flow are retained as one contiguous hourly history of at
most 87,672 values (about ten years), bootstrapped from all history Home
Assistant retains and extended by scheduled runs with only the completed hours
after the retained history. Price history is retained for the same number of
hours and may skip hours. Retention, backup recovery, and provider import
behavior are otherwise unchanged. A failed provider run keeps the last valid
history, and the assets above report `unavailable` until the first successful run
persists data.

### Dashboard view

The Historic actuals tab draws the returned series in separate charts by unit:
power (`kW`) with household load, grid import, and grid export; prices
(`EUR/kWh`); and battery state of charge (`%`). Its details panel describes the
source, unit, freshness, coverage, retrieval time, and validation status of each
series, and names every asset that is not available with its reason. When some
asset data is withheld as invalid, the status line says so and the remaining
series are still drawn. The view labels the series as historic actuals and
deliberately does not mix them with predicted inputs or optimization plans.

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

## Excluded hours

An hour of Home Assistant history is imported only if every data point that
contributes to it is valid. Every other hour has no value (`null`) and is
recorded with each cause. The dashboard's **Excluded hours** tab and this endpoint
list them:

```text
GET /api/v1/dashboard/excluded-hours?start_time=<inclusive>&end_time=<exclusive>
```

The range follows the rules of the dashboard data endpoint: timezone-aware, aligned
to the hour, `end_time` later than `start_time`, at most 87,672 hours; violations
return HTTP 422. The response lists the sources that were checked, a summary by
source and reason, and every excluded hour in ascending order:

```json
{
  "schema_version": "1",
  "requested_start_time": "2026-01-01T00:00:00Z",
  "requested_end_time": "2026-01-01T06:00:00Z",
  "excluded_hour_count": 1,
  "sources": [
    {"source": "household_load", "status": "available", "reason": null, "excluded_hour_count": 1},
    {"source": "grid_flow", "status": "not_configured",
     "reason": "no Home Assistant grid import and export entities are configured",
     "excluded_hour_count": 0},
    {"source": "battery_efficiency", "status": "unavailable",
     "reason": "no persisted battery efficiency history is available yet",
     "excluded_hour_count": 0}
  ],
  "summary": [
    {"source": "household_load", "reason": "counter_decrease", "excluded_hour_count": 1}
  ],
  "hours": [
    {
      "hour_start": "2026-01-01T03:00:00Z",
      "source": "household_load",
      "causes": [
        {
          "reason": "counter_decrease",
          "message": "sensor.household_energy decreased from 700 kWh at 2026-01-01T02:59:50+00:00 to 0 kWh at 2026-01-01T03:00:10+00:00.",
          "entity_id": "sensor.household_energy",
          "data_point_count": 1,
          "data_points": [
            {
              "timestamp": "2026-01-01T03:00:10Z",
              "state": "0",
              "unit": "kWh",
              "entity_id": null,
              "previous_timestamp": "2026-01-01T02:59:50Z",
              "previous_value": 700.0,
              "value": 0.0,
              "step_kwh": -700.0,
              "maximum_kwh": 100.0
            }
          ]
        }
      ]
    }
  ]
}
```

- `source` is `household_load`, `grid_flow`, or `battery_efficiency`. Grid import and
  export are excluded together, and an hour excluded in any battery-efficiency leg
  or in the state of charge is excluded in all of them.
- A source `status` is `available` when its persisted history was read,
  `not_configured` when the installation has no such source, `unavailable` when no
  history has been persisted yet, and `invalid` when persisted data is corrupt and
  withheld. One source never hides the others.
- Every cause has a `reason` from the closed set below, a human-readable
  `message`, the `entity_id`, and its `data_points`. A data point carries the time
  Home Assistant recorded it, the raw `state` exactly as reported, and, for a
  counter step, the previous and current observation (`previous_timestamp`,
  `previous_value`, `value`, in the entity's unit), `step_kwh`, and `maximum_kwh`.
  For a combined hour, each data point names one component `entity_id` and its
  signed `step_kwh`. At most 50 data points are kept per entity and hour;
  `data_point_count` is the number before that bound.
- `summary` counts excluded hours per source and reason; an hour counts once per
  distinct reason.

| Reason | Meaning |
| --- | --- |
| `unavailable`, `non_numeric`, `not_finite`, `negative_value`, `invalid_attribute` | The state, or an attribute needed to read it, is not usable |
| `unit_mismatch`, `unit_missing`, `state_class_mismatch` | A sample reports another unit, no unit, or another `state_class` than configured |
| `counter_decrease` | A counter decreased, however little |
| `step_after_decrease` | The step directly after a decrease cannot be told apart from a glitch |
| `last_reset_changed` | The `last_reset` marker changed |
| `step_above_maximum`, `hour_above_maximum` | A step, or the sum of one hour, exceeds `maximum_interval_energy_kwh` |
| `soc_out_of_range` | A state of charge is outside 0 to 100 percent |
| `combined_negative`, `combined_not_finite` | The signed operations of an hour give a negative or non-finite value |
| `flagged_by_earlier_version` | An earlier version had flagged the hour `suspect` |

The Excluded hours tab loads this endpoint for the chosen UTC window. It shows the
checked sources with their status, a summary by source and reason, and a table with
one row per data point: the hour, source, entity, reason with its message, the data
point's time and reported value, and its detail (previous value, step, and maximum).
A window without exclusions shows "No hours were excluded in this window."; a
window whose sources have no history does not claim that nothing was excluded.

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
field-level validation details. When the source matches the configured Home
Assistant grid-flow provider, `POST /api/v1/grid-flow` merges the submission into
the retained history like household load does: incoming values replace
overlapping hours, at most 87,672 hourly values are kept, and a submission that
would leave a gap in the contiguous history is rejected with HTTP 503 without
changing the retained data. `GET /api/v1/grid-flow` returns the complete retained
history for the configured provider, or HTTP 404 when none is available. Range
queries over this history use the historic multi-asset API described below.

The health endpoint returns the service status and version, for example:

```json
{"status":"ok","version":"0.1.0"}
```
