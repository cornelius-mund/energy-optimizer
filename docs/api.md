# API Reference

The OpenAPI contract, `src/energy_optimizer/openapi-docs.yml`, defines every
request and response field, type, range, and status enum; this reference documents
behavior, limits, and examples. `GET /health` returns the service status and
version: `{"status":"ok","version":"0.1.0"}`.

All versioned `POST /api/v1/*` endpoints below (electricity prices, battery,
household load, PV generation, grid flow) take `schema_version: "1"` and
`interval_minutes: 60`, accept one to 87,672 hourly values, and echo the
normalized data with `status: "validated"`. Missing fields, unknown fields,
unsupported versions or units, naive timestamps, invalid values, and series longer
than ten years (87,672 hourly values) return HTTP 422 with field-level validation
details; each section lists only its additional 422 causes.

## Hourly optimization API

`POST /optimize` validates hourly inputs only: a timezone-aware `start_time`,
`interval_minutes: 60`, and equally sized series of `load_kw`, `pv_generation_kw`,
`import_price_eur_per_kwh`, and `export_price_eur_per_kwh`, with one value per hour
from one to 168 hours, in kW or EUR/kWh as named by their fields. It returns
`status: "validated"`, `start_time`, `interval_minutes` and `hours`. There is no
solver, schedule, objective or appliance energy requirement. Invalid inputs
return 422.

## Appliances and general energy history

Heat pumps and other manageable appliances use the same installation-specific
configuration and API contract:

```yaml
appliances:
  heat_pump:
    name: Heat pump
    included_in_household_load: true
    maximum_power_kw: 3
    control: discrete
    power_levels: [0, 0.3, 0.6, 1]
    history:
      terms:
        - operation: add
          entities:
            - entity_id: sensor.heat_pump_energy
              state_class: total_increasing
              unit: kWh
```

`included_in_household_load` is required and refers to the **configured household
series**, after any explicitly configured signed aggregation. Set it to false
when that series excludes the appliance, including when its meter was already
subtracted. Set it to true when the household series includes its consumption.
The original household series and appliance history are preserved.

Dashboard actuals additionally provide `unmanaged_household_load_actual` =
household minus included appliances, and `total_consumption_actual` = household
plus additional appliances. Thus each load is counted once. Missing appliance
observations propagate to affected derived values as null gaps; an inconsistent
negative unmanaged remainder also becomes a gap, never a fabricated zero.
Appliance histories must describe disjoint physical loads for this accounting.

`control: continuous` supports any power from zero to `maximum_power_kw` and
must omit `power_levels`. `control: discrete` requires a unique ascending list
of finite fractions between 0 and 1, starting at 0 and ending at 1. `[0, 1]`
is on/off; `[0, 0.3, 0.6, 1]` with a 3 kW maximum is 0, 0.9, 1.8 and 3 kW.
Maximum power must be finite, positive and at most 1000 kW. These describe
controllable setpoints, not restrictions on measured hourly averages. There is
no scheduling or actuator execution in this contract.

`GET /api/v1/appliances` lists capabilities keyed by appliance ID.
`POST /api/v1/appliances/validate` validates a capabilities object (the fields
above excluding `history`); unknown fields, missing values or invalid control
combinations return 422. IDs use lowercase letters, digits and underscores,
start with a letter and have at most 64 characters. History is optional, so an
appliance can be defined before a meter exists.

All measured energy history uses `EnergyAggregate` and the shared
`HomeAssistantHistoryImporter`, just like household load and grid flow. There
is no heat-pump-specific snapshot importer or remaining-energy sensor. Counter
units Wh/kWh/MWh normalize to hourly average kW. Resets, unavailable or malformed
values, unit mismatches, physical plausibility limits and unavailable retained
history use the existing exclusion rules. Counter history is actual measured
consumption, not a forecast. Unknown hours remain null with exclusion causes.

Configure arbitrary non-appliance sources under
`home_assistant.energy_history.<id>` using the same signed aggregation, for
example PV production or another household measurement. Enable polling using
`orchestration.sources.appliance.<id>` for appliances and
`orchestration.sources.history.<id>` for generic sources (these are dotted
**source keys**, as shown in `config.example.yaml`). Connection URL, credentials,
timeouts, freshness, polling interval and history lookback are shared settings.
Every distinct counter is imported once per cycle and shared across all planned
sources. First import bootstraps retained history up to ten years; subsequent
imports continue from the checkpoint and retain earlier hours. Failed retrieval
keeps the last valid history. Provider authentication errors identify the token
problem without exposing it.

`GET /api/v1/energy-history/{source_id}?start_time=...&end_time=...` reads a
source such as `appliance.heat_pump` or `history.pv_generation` using the common
dashboard series envelope. Ranges are half-open and UTC-hour aligned; timestamps
require offsets. Responses include aligned values, source, coverage, retrieval
time, freshness and missing intervals. Unknown or not-yet-imported sources
return 404; unavailable persistence or corrupt data returns 503. Imported sources
also appear in dashboard **actuals** with IDs `<source_id>_actual`.

## Electricity-price API

`POST /api/v1/electricity-prices` validates normalized hourly import and export
prices: ascending, unique, timezone-aware `timestamps` (one to 87,672 hours, spaced
by `interval_minutes: 60`), aligned `import_price_eur_per_kwh` and
`export_price_eur_per_kwh` values in `EUR/kWh`, provider-independent `source`
metadata, and timezone-aware `retrieved_at` and `expires_at` freshness bounds.
Prices may be negative for markets that support negative rates, but must remain
within -100 to 100 EUR/kWh. Coverage must begin at or after retrieval and end
before expiry. Additional 422 causes are duplicate or incorrectly spaced
timestamps, misaligned series, stale coverage, invalid freshness bounds, and
out-of-range values.

```json
{
  "schema_version": "1", "interval_minutes": 60, "unit": "EUR/kWh",
  "timestamps": ["2026-01-01T00:00:00+00:00", "2026-01-01T01:00:00+00:00"],
  "import_price_eur_per_kwh": [0.30, 0.25], "export_price_eur_per_kwh": [0.08, 0.08],
  "source": {"provider": "day-ahead-market"},
  "retrieved_at": "2025-12-31T23:00:00+00:00", "expires_at": "2026-01-01T03:00:00+00:00"
}
```

## Battery API

`POST /api/v1/battery` validates normalized hourly battery state and capability
data: timezone-aware `start_time`, `state_of_charge_kwh` values, capacity and SOC
bounds in kWh, initial SOC, charge and discharge power limits in kW, one battery
round-trip efficiency from greater than zero through one, `unit: "kWh"`,
`power_unit: "kW"`, and optional source metadata. Additional 422 causes are
out-of-range state of charge, inconsistent limits, and invalid efficiencies.

```json
{
  "schema_version": "1", "start_time": "2026-01-01T00:00:00+00:00", "interval_minutes": 60,
  "state_of_charge_kwh": [5.0, 5.5], "unit": "kWh", "power_unit": "kW",
  "capacity_kwh": 10.0, "minimum_soc_kwh": 2.0, "maximum_soc_kwh": 10.0, "initial_soc_kwh": 5.0,
  "maximum_charge_kw": 4.0, "maximum_discharge_kw": 4.0, "battery_efficiency": 0.9,
  "source": {"provider": "home-assistant", "entity_id": "sensor.battery_soc"}
}
```

## Home Assistant battery import

The Home Assistant battery provider retrieves a current snapshot from the REST
state endpoint for each configured entity mapping. Entity values come from the
entity state or a named entity attribute and normalize to the battery API units:
state of charge and SOC limits accept `%`, `Wh`, or `kWh`; capacity accepts `Wh` or
`kWh`; power limits accept `W` or `kW`; and efficiencies accept `%` or a unitless
`ratio`. Static numeric values may instead be configured as constants with
`{value, unit}`; numeric shorthand uses the canonical unit for that field. Live
state, cumulative counters, and historical measurements remain provider backed;
constants are intended for static installation parameters only.

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

Example:

```yaml
battery:
  state_of_charge: {entity_id: sensor.battery_state_of_charge, unit: '%'}
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
      energy_in: {terms: [{operation: add, entities: [{entity_id: sensor.battery_energy_in, state_class: total_increasing, unit: kWh}]}]}
      energy_out: {terms: [{operation: add, entities: [{entity_id: sensor.battery_energy_out, state_class: total_increasing, unit: kWh}]}]}
    inverter_charge:
      energy_in: {terms: [{operation: add, entities: [{entity_id: sensor.ac_into_inverter, state_class: total_increasing, unit: kWh}]}]}
      energy_out:
        part: positive
        terms:
          - {operation: add, entities: [{entity_id: sensor.battery_energy_in, state_class: total_increasing, unit: kWh}]}
          - {operation: subtract, entities: [{entity_id: sensor.mppt_energy, state_class: total_increasing, unit: kWh}]}
    inverter_discharge:
      energy_in:
        part: positive
        terms:
          - operation: add
            entities:
              - {entity_id: sensor.battery_energy_out, state_class: total_increasing, unit: kWh}
              - {entity_id: sensor.mppt_energy, state_class: total_increasing, unit: kWh}
          - {operation: subtract, entities: [{entity_id: sensor.battery_energy_in, state_class: total_increasing, unit: kWh}]}
      energy_out: {terms: [{operation: add, entities: [{entity_id: sensor.inverter_to_ac, state_class: total_increasing, unit: kWh}]}]}
```

Calculated efficiency is configured separately under
`battery.efficiency_calculation`: an `energy_in` and an `energy_out` energy
aggregation for each of the battery, inverter charge, and inverter discharge
components, plus the historical state of charge entity. Battery efficiency is one
full-cycle round-trip value detected from consecutive full-SoC boundaries; inverter
charge and discharge efficiencies are independent inverter measurements; the
complete ratio is their product with the battery ratio. A configured
`battery_efficiency` takes precedence over the calculated battery value and emits
a warning. The dashboard exposes these values through `scenario_kind=efficiency`.
Before the first complete full-SoC cycle is available (or while the calculated
result is otherwise not `"ok"`), the live battery snapshot uses a 95% default for
`battery_efficiency` instead of failing, but only in calculated mode without a
fixed `battery_efficiency` override.

The dashboard Efficiency tab renders each available component as one accessible
numeric summary row with its raw `ratio` value. These values summarize the
complete retained battery and inverter history; they are not hourly chart
observations, and the tab has no selectable calculation time window. A component
that cannot be calculated uses the `0.95` ratio fallback and is marked
`defaulted`, `unavailable`, or `invalid` in the response and UI; a measured
component is `calculated`, and complete round-trip efficiency is
`calculated_with_defaults` when any input uses a fallback. Each series carries its
structured `calculation_status`, and the UI renders exactly one annotation per row
from it: `(calculated)`, `(default)`, `(unavailable)`, `(invalid)`, or
`(calculated with defaults)`. The `is_default` flag remains in the response as
compatibility metadata, `true` for the three fallback statuses (`defaulted`,
`unavailable`, `invalid`) and `false` otherwise; the UI does not render it, so a
fallback is never annotated twice. Fallback annotations carry a tooltip explaining
that the fallback ratio was used. Source, coverage, freshness, retrieval, and
calculation diagnostics remain available beside the summary. Battery throughput,
inverter charge and discharge throughput, and `Completed battery cycles` are
exposed as structured scalar metrics rather than diagnostics. The UI formats every
displayed ratio to four digits after the decimal point (the raw API value stays in
the data element), the three throughput metrics, which carry the `kWh` unit, to
exactly two digits, including trailing zeroes (`5.00 kWh`, `0.00 kWh`), and
`Completed battery cycles` as a whole number. Metric values in the API response
are not rounded.

Calculated-efficiency ingestion reconstructs historical state of charge through
the dedicated history endpoint and persists the aligned hourly battery/inverter
history under its own record before the daily calculation. Every scheduled run
requests only the hours after the previously persisted history, merges them into
the retained record, and bounds retention to 87,672 hours (ten years), so the
daily recompute does not re-fetch the complete history from Home Assistant each
time. Empty Home Assistant chunks before an entity's retained history begins are
skipped during bootstrap; the resulting history starts at the earliest complete
retained hour and does not fabricate earlier values. The calculator itself uses
all retained history, or all history since `history_start`, and never uses a
rolling window.

Each side of a leg is an energy aggregation: per hour, the energy of the entities
of its `add` term minus the energy of the entities of its `subtract` term. It is a
net directional energy flow, not an absolute cumulative total. For a DC-coupled
installation, `inverter_charge.energy_out` above nets the battery's total charging
energy against the directly consumed PV yield to isolate the AC-sourced share; in
any hour where PV production exceeds the battery's charging energy, that sum is
negative because the surplus was exported rather than stored. Likewise
`inverter_discharge.energy_in` is the energy the inverter draws from the DC bus:
PV yield reaches the inverter directly, so it is added to the battery's discharge
energy, and the energy the battery took in during the same hour is subtracted
because it never reached the inverter. In any hour where the battery takes in more
than it discharges plus the PV yield, that sum is negative because the inverter
was not discharging.

`part` decides what such a negative sum means. With the default `net` it is
invalid data and never fails the refresh: like every other invalid data point it
excludes the hour (`combined_negative`) in all six energy legs and the state of
charge, and the hour is listed on the dashboard's Excluded hours tab and through
`GET /api/v1/dashboard/excluded-hours`. A full-charge cycle that contains an
excluded hour is not used for the ratios, so the result rests on fewer cycles
instead of a wrong value. On a DC-coupled installation these two sums are
routinely negative, so set `part: positive` on them, as in the example above:
only the positive part of the sum is wanted, so the negative hour is imported as
`0`, is not excluded, and does not remove its full-charge cycle. `part: positive`
still excludes an hour for a non-finite sum or an invalid entity sample. Each
clamped hour is counted as `clamped_hour_count`, next to `part` and
`excluded_hour_count`, in the `home_assistant_history_aggregate` log line of the
aggregation; it is not listed as an excluded hour.

The state-of-charge validation checks physical plausibility, not round-trip
loss: during an hour with only charging (or only discharging) energy measured,
the stored energy change can never exceed what was delivered, nor exceed what
was removed from storage, beyond `soc_balance_tolerance_kwh`. A violation
means the measured data is inconsistent (e.g. a misconfigured or drifting
entity), not that the battery has ordinary conversion losses.

## Household-load API

`POST /api/v1/household-load` validates a normalized hourly household-load series:
a timezone-aware `start_time`, `load_kw` (one non-negative value per hour for one
to 87,672 hours), `unit: "kW"`, optional `source` metadata with a provider and
entity identifier, and timezone-aware `retrieved_at` and `latest_observation_at`
metadata.

```json
{
  "schema_version": "1", "start_time": "2026-01-01T00:00:00+00:00", "interval_minutes": 60,
  "load_kw": [1.2, 1.0], "unit": "kW",
  "source": {"provider": "home-assistant", "entity_id": "sensor.household_load"},
  "retrieved_at": "2026-01-01T00:00:00+00:00", "latest_observation_at": "2026-01-01T01:00:00+00:00"
}
```

In a response, `load_kw` holds `null` for an hour that the Home Assistant import
excluded; a submission never contains `null`. Historical data is not rejected
because it is old; polling health is assessed separately with the provider's
optional freshness threshold.

When configured, `POST /api/v1/household-load` persists data only when its
source matches the configured Home Assistant household-load provider.
Source-less submissions and other providers are validated and returned but are
not persisted. Matching submissions merge by hourly timestamp with the persisted
history, with incoming values overwriting duplicates and the oldest values removed
beyond 87,672 hours. A submission that starts after the persisted history ends
stores every hour in between as an excluded hour with the reason
`history_unavailable` (`null` in the series), which a later submission of those
hours replaces. `GET /api/v1/household-load` retrieves the latest persisted
normalized provider data. Optimizer-owned state transitions remain outside this
persistence boundary.

## Historic multi-asset dashboard

Open `/dashboard/` to inspect imported actuals. The Historic actuals tab and every
other consumer read one endpoint using the unified dashboard contract,
`GET /api/v1/dashboard/data?scenario_kind=actual&start_time=<inclusive>&end_time=<exclusive>`.
`scenario_kind=actual` is the default. The range follows the same rules for every
scenario: both boundaries are timezone-aware and normalized to UTC, must be
aligned to the hour, `end_time` must be later than `start_time`, and the range must
not exceed 87,672 hours; violations return HTTP 422. The range is half-open
(inclusive start, exclusive end).

### Dashboard settings and time zone

`GET /api/v1/dashboard/settings` returns `{"timezone": "Europe/Berlin"}`.
`timezone` is the top-level configuration setting: an IANA name matched exactly and
case-sensitively (default `UTC`) whose UTC offset is a whole number of hours all
year. It is validated at startup; an unknown name, a lowercase `utc`, a path-like
value, an empty value, and zones such as `Asia/Kolkata` or `Australia/Lord_Howe`
stop startup with an error that names `timezone`. The dashboard reads this endpoint
before its first data request, because its default range depends on the zone.

Only the dashboard's presentation uses the zone. Every API contract, timestamp, and
stored record stays UTC, and the data endpoints keep accepting only hour-aligned
timestamps. The dashboard converts the local wall times of its controls to whole
UTC hours: in `Europe/Berlin`, the local range `2026-09-30 00:00` to
`2026-10-01 00:00` requests `start_time=2026-09-29T22:00:00Z` and
`end_time=2026-09-30T22:00:00Z`.

Around clock changes the dashboard applies fixed rules. When clocks go back, the
repeated local hour is two separate hourly points whose labels carry the UTC offset
(`2026-10-25 02:00+02:00` and `2026-10-25 02:00+01:00`), and a control value inside
the repeated hour resolves to its first occurrence. When clocks go forward, a local
time that does not exist resolves forward by the length of the gap, so `02:30`
becomes `03:30`. The default range, today from 00:00 to the next local midnight,
therefore spans 23 or 25 hours on those days.

### Series

Every available asset contributes series of `scenario_kind: "actual"` in this
order. Each series carries one value for every requested hour, with `null` for an
hour without an observation, and lists those hours in `missing_intervals`.
Missing hours are never interpolated or replaced by zero.

| Series id | Data type | Unit | Source |
| --- | --- | --- | --- |
| `household_load_actual` | `household_load` | `kW` | Home Assistant household-load history |
| `pv_generation_actual` | `pv_generation` | `kW` | Home Assistant measured PV-generation history |
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
- Battery state of charge is the state in force at the start of each hour, in
  percent: the last state recorded at or before it. It comes from the retained
  history used for the calculated battery efficiency, so it is available only when
  `home_assistant.battery.efficiency_calculation` is configured.

Historic actuals never contain forecasts or optimizer plan snapshots, so the view
never mixes them with predicted inputs or optimization plans.

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
so a client can tell what is missing and why. The asset `status` is `available`
(series were returned), `empty` (history exists but has no observation in the
requested range), `stale` (series were returned; the newest observation exceeds
the polling threshold), `not_configured` (the installation has no source for this
asset; this is not a warning), `unavailable` (the asset is configured but
persistence is missing or no data has been persisted yet), or `invalid` (persisted
data is corrupt or could not be recovered from its backup and is withheld). Every
status other than `available` carries an actionable `reason`. Assets in the
`empty`, `stale`, `unavailable`, and `invalid` states are also summarized in
`diagnostics`; `not_configured` assets are not, unless nothing at all is
configured. Corrupt or unrecoverable persisted data is never returned as
valid actuals; the technical cause is written to the service log with the request
ID rather than returned to clients. The dashboard endpoint reports these states in
the response body (HTTP 200); `GET /api/v1/historic/household-load` keeps
returning HTTP 503 for corrupt household-load persistence.

PV actuals are returned as `pv_generation_actual` when
`home_assistant.pv_generation` is configured and the `pv_generation_history`
orchestration source has persisted measured history. The source is
`home-assistant` / `pv_generation`; hourly UTC values are kW, with excluded hours
represented by null and listed in `missing_intervals`. Exclusion details are
available through `/api/v1/dashboard/excluded-hours` under
`pv_generation_history`. The retained record uses `generation_kw`,
`latest_observation_at`, `retrieved_at` and explicit `scenario_kind: actual`.
Electric-vehicle and heat-pump placeholders remain `not_configured` until a
dedicated importer exists. General energy/appliance history is served separately.
PV forecasts are available through `scenario_kind=forecast`. A new importer only
needs to register a loader in `energy_optimizer.api.historic`; the envelope does
not change.

Example (abridged) for a two-hour range with household load and grid flow:

```json
{
  "schema_version": "1", "status": "validated", "interval_minutes": 60,
  "requested_start_time": "2026-01-01T00:00:00Z", "requested_end_time": "2026-01-01T02:00:00Z",
  "series": [{
    "id": "household_load_actual", "data_type": "household_load", "scenario_kind": "actual",
    "timestamps": ["2026-01-01T00:00:00Z", "2026-01-01T01:00:00Z"], "values": [1.2, 1.0], "unit": "kW",
    "source": {"provider": "home-assistant", "entity_id": "household_load"},
    "available_start_time": "2026-01-01T00:00:00Z", "available_end_time": "2026-01-01T02:00:00Z",
    "retrieved_at": "2026-01-01T02:00:00Z", "freshness": "fresh", "validation_status": "valid",
    "missing_intervals": []
  }],
  "assets": [
    {"asset": "household_load", "status": "available", "series_ids": ["household_load_actual"], "reason": null},
    {"asset": "pv_generation", "status": "not_configured", "series_ids": [],
     "reason": "no Home Assistant PV-generation entities are configured"},
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
hours and may skip hours. A failed provider run keeps the last valid
history, and the assets above report `unavailable` until the first successful run
persists data. Hours that Home Assistant no longer held when a run resumed after a
long downtime stay in the history as excluded hours with the reason
`history_unavailable` (`null` in the series), so the history stays contiguous.

### Dashboard view

The Historic actuals tab draws the returned series in separate charts by unit:
power (`kW`) with household load, grid import, and grid export; prices
(`EUR/kWh`); and battery state of charge (`%`). Its details panel describes the
source, unit, freshness, coverage, retrieval time, and validation status of each
series, and names every asset that is not available with its reason. When some
asset data is withheld as invalid, the status line says so and the remaining
series are still drawn. The view labels the series as historic actuals.

The range controls use an end-exclusive boundary: the end time must be later
than the start time. If a requested actual or forecast range falls outside the
available coverage, the dashboard replaces both controls with the available
coverage and loads that range. When forecast providers have different
coverage, the controls use the union of their available ranges so valid price
and PV points are retained; each chart shows the other provider's missing
intervals as gaps. If forecast coverage is unavailable, it keeps the
unavailable state instead of inventing a range. The x-axis labels include each
point's date and time in the configured time zone. The detail lists
(coverage, retrieved, generated, published) use the same zone.

Every chart has its own legend beside it (below it on screens up to 760px wide).
The legend lists only the series that chart draws, in chart order, as
`<label> (<unit>)` with a swatch in the line's color, for example
`Import price (EUR/kWh)`; a series without data has no entry. Each entry is a
`<button>` whose `aria-pressed` state tells whether the line is shown. Clicking it,
or pressing Enter or Space on it, hides the line and its points or shows them again
without a new request. The chart's value axis, tick labels, and precision are
recomputed from the lines that remain, and stay at the default scale when every
line is hidden. Hidden lines are remembered per series ID for the page session, so
they survive a range reload and a tab switch, and a page reload shows them again.
Nothing is stored in the browser. The Efficiency and Excluded hours tabs draw no
chart and show no legend.

Chart points expose the series label, the exact timestamp in the configured zone,
and the value with its unit on pointer hover and keyboard focus, for example
`Import price · 2026-09-30 23:00 · 0.14 EUR/kWh`. The tooltip stays inside the chart
its point belongs to, so it never covers that chart's legend. It is the only tooltip:
the charts carry no SVG `<title>` element and no `title` attribute, which browsers
would draw as a second, native tooltip over the point tooltip.

Each chart is an SVG with the role `img`. Its accessible name (`aria-label`) lists
the lines that are shown, for example `Grid import and Grid export (kW)`, and its
accessible description (`aria-describedby`, referencing the SVG `<desc>`) reads
`Grid import in kW; Grid export in kW. Missing intervals remain gaps.` Hiding a line
through its legend entry removes it from both. When every line is hidden, the name is
`No series shown (kW)`.

The dashboard also provides a Forecast tab backed by
`GET /api/v1/dashboard/data?scenario_kind=forecast`. PV generation uses `kW`;
market prices use `EUR/kWh`. The Forecast tab renders power and price data in
separate charts, each with its own unit axis, data-driven scale, legend, and
accessible description. Each scale uses finite visible values with small padding,
while flat or empty data receives a safe non-zero fallback domain. The aWATTar
price forecast is requested for a window that reaches 48 hours past the current
hour, so it covers the next local day as soon as the day-ahead prices are
published at 14:00 local time. Missing or partial observations remain gaps and are
not interpolated or treated as zero. The forecast charts have the same per-chart
legends, show and hide behavior, and point tooltips as the Historic actuals tab.

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
recorded with each cause. The dashboard's **Excluded hours** tab and
`GET /api/v1/dashboard/excluded-hours?start_time=<inclusive>&end_time=<exclusive>`
list them. The range follows the rules of the dashboard data endpoint (violations
return HTTP 422). The response lists the sources that were checked, a summary by
source and reason, and every excluded hour in ascending order:

```json
{
  "schema_version": "1",
  "requested_start_time": "2026-01-01T00:00:00Z", "requested_end_time": "2026-01-01T06:00:00Z",
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
  "summary": [{"source": "household_load", "reason": "counter_decrease", "excluded_hour_count": 1}],
  "hours": [{
    "hour_start": "2026-01-01T03:00:00Z", "source": "household_load",
    "causes": [{
      "reason": "counter_decrease",
      "message": "sensor.household_energy decreased from 700 kWh at 2026-01-01T02:59:50+00:00 to 0 kWh at 2026-01-01T03:00:10+00:00.",
      "entity_id": "sensor.household_energy", "data_point_count": 1,
      "data_points": [{
        "timestamp": "2026-01-01T03:00:10Z", "state": "0", "unit": "kWh", "entity_id": null,
        "previous_timestamp": "2026-01-01T02:59:50Z", "previous_value": 700.0, "value": 0.0,
        "step_kwh": -700.0, "maximum_kwh": 100.0
      }]
    }]
  }]
}
```

- `source` is `household_load`, `grid_flow`, `pv_generation_history`,
  `battery_efficiency`, or a configured general energy/appliance source. Grid import and
  export are excluded together, and an hour excluded in any battery-efficiency leg
  or in the state of charge is excluded in all of them. An hour with the reason
  `history_unavailable` is excluded in the same way in every source that held
  history before the gap.
- A source `status` is `available` when its persisted history was read,
  `not_configured` when the installation has no such source, `unavailable` when no
  history has been persisted yet, and `invalid` when persisted data is corrupt and
  withheld. One source never hides the others.
- Every cause has a `reason` from the closed set below, a human-readable
  `message`, the `entity_id`, and its `data_points` (the recorded time, the raw
  `state`, and for a counter step the previous and current observation, `step_kwh`,
  and `maximum_kwh`; field definitions: `ExcludedDataPoint` in the OpenAPI
  contract). For a combined hour, each data point names one component `entity_id`
  and its signed `step_kwh`. At most 50 data points are kept per entity and hour;
  `data_point_count` is the number before that bound. A `history_unavailable` cause
  has `entity_id: null`, no data points, and `data_point_count: 0`; its `message`
  names the whole missing range, for example "The provider holds no history from
  2026-08-21T16:00:00+00:00 until 2026-09-01T15:00:00+00:00 (263 hours), so these
  hours cannot be imported."
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
| `combined_negative`, `combined_not_finite` | The signed terms of an hour give a non-finite value, or a negative value unless the aggregation takes `part: positive` |
| `flagged_by_earlier_version` | An earlier version had flagged the hour `suspect` |
| `history_unavailable` | Home Assistant no longer holds the hour, because the service was down for longer than the recorder retains history; nothing was recorded for it |

The Excluded hours tab loads this endpoint for the chosen window, converted from the
configured time zone to whole UTC hours. It shows the
checked sources with their status, a summary by source and reason, and a table with
one row per data point: the hour, source, entity, reason with its message, the data
point's time and reported value, and its detail (previous value, step, and maximum).
A window without exclusions shows "No hours were excluded in this window."; a
window whose sources have no history does not claim that nothing was excluded.

## PV-generation API

`POST /api/v1/pv-generation` validates a normalized hourly PV-generation series: a
timezone-aware `start_time`, `generation_kw` (one non-negative value per hour for
one to 87,672 hours), `unit: "kW"`, and optional `source` metadata with a provider
and entity identifier.

```json
{
  "schema_version": "1", "start_time": "2026-01-01T00:00:00+00:00", "interval_minutes": 60,
  "generation_kw": [0.0, 2.4], "unit": "kW",
  "source": {"provider": "home-assistant", "entity_id": "sensor.pv_generation"}
}
```

## Grid-flow API

`POST /api/v1/grid-flow` validates normalized hourly grid import and export
data: timezone-aware `start_time`, equally sized non-negative `import_kw` and
`export_kw` series for one to 87,672 hours, `unit: "kW"`, optional source metadata,
and timezone-aware `retrieved_at` and `latest_observation_at` metadata. Mismatched
series lengths are an additional 422 cause.

```json
{
  "schema_version": "1", "start_time": "2026-01-01T00:00:00+00:00", "interval_minutes": 60,
  "import_kw": [1.2, 1.0], "export_kw": [0.0, 0.4], "unit": "kW",
  "source": {"provider": "home-assistant", "entity_id": "grid_flow"},
  "retrieved_at": "2026-01-01T00:00:00+00:00", "latest_observation_at": "2026-01-01T01:00:00+00:00"
}
```

When the source matches the configured Home Assistant grid-flow provider,
`POST /api/v1/grid-flow` merges the submission into the retained history like
household load does: incoming values replace overlapping hours, and at most 87,672
hourly values are kept. A submission that starts after the retained history ends
stores every hour in between as an excluded hour with the reason
`history_unavailable`, which a later submission of those hours replaces. A
submission that ends before the retained history starts, with hours between them,
would leave a gap in the contiguous history and is rejected with HTTP 503 without
changing the retained data. `GET /api/v1/grid-flow` returns the complete retained
history for the configured provider, or HTTP 404 when none is available. Range
queries over this history use the historic multi-asset API described above.
