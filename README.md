# Energy Optimizer

General appliances (including heat pumps) have configurable household-load
inclusion and continuous or discrete power capabilities. Their measured energy
history uses the shared Home Assistant counter-history pipeline, also available
for arbitrary energy sources such as PV. See the
[appliance reference](docs/api.md#appliances-and-general-energy-history) and
`config.example.yaml`. `POST /optimize` calculates the cheapest schedule for the
household load with PV self-consumption, grid import and export, curtailment, and an
optional battery; appliance scheduling is not implemented yet.

An open-source web service for optimizing the energy usage of a single-family
house. It optimizes the interaction between household electrical load,
photovoltaic generation, electricity grid import and export, battery storage,
electric vehicles and charging points, and a heat pump. The service calculates an
energy schedule on an hourly basis; the underlying energy models and scheduling
resolution may evolve as the project develops.

## Goals

The service should calculate an energy schedule that minimizes total energy cost
while respecting the technical constraints of the connected assets. It is
designed to support input and output through HTTP APIs, YAML-based configuration,
containerized deployment with Docker, and mixed-integer linear programming (MILP)
optimization with HiGHS as the open-source MILP solver, modelled with linopy.

## Planned Architecture

The target architecture separates HTTP transport, application orchestration,
domain models, external integrations, and optimization. The optimizer should
remain independent of specific external data providers, with providers returning
normalized internal data before it reaches the optimization model. The planned
package structure, dependency direction, and request flow are documented in
[`docs/architecture.md`](docs/architecture.md). These boundaries are targets for
the implementation, not a claim that all of the modules exist today.

### Heating model design

The proposed building heat-storage model, temperature-dependent heat-pump COP,
manual fireplace economics, inspected Home Assistant data and open configuration
questions are documented in [Heating model design](docs/heating-model.md).
This is an advisory design; automated heating control is not implemented.

## Input and Output

The API should accept household electrical load, PV generation forecast,
electricity import and export prices, battery state of charge, electric vehicle
state of charge and availability, and heat-pump load constraints. All input data
must be validated before optimization.

The optimization response should provide an hourly schedule including, where
applicable: household load, PV generation, grid import, grid export, battery
charging, battery discharging, battery state of charge, electric vehicle charging,
electric vehicle state of charge, heat-pump consumption, and PV curtailment. It
should also include the objective value, import cost, export revenue, solver
status, solve time, and diagnostics for invalid or infeasible requests.

## Configuration

Runtime and system configuration is loaded from `config.yaml` at startup. Set
`ENERGY_OPTIMIZER_CONFIG` to use a different file. A complete example is
provided in [`config.example.yaml`](config.example.yaml). Configuration is
expected to contain the time resolution, the dashboard time zone (`timezone`, see
"Dashboard time zone"), grid limits, electricity pricing behavior, PV system,
battery, electric vehicle, and heat-pump parameters, solver settings, and
external data provider settings. Invalid configuration results in a clear startup
error.

Battery and inverter efficiency can be calculated from complete Home Assistant
history under `battery.efficiency_calculation`. The calculation, its 95% default,
the dashboard's Efficiency tab, and the exclusion rules are described in
[`docs/api.md`](docs/api.md#home-assistant-battery-import).

### Logging

Set `ENERGY_OPTIMIZER_LOG_LEVEL` to `DEBUG`, `INFO`, `WARNING`, `ERROR`,
`CRITICAL`, or `NOTSET` to override the default `INFO` minimum log level. An
invalid value stops startup with a configuration error. The service writes
structured, container-friendly logs to standard output and includes the event,
component, operation, status, request ID, and relevant time or record-count
context. Every Python log uses the format `LEVEL TIMESTAMP MESSAGE`, including
Uvicorn and HTTP client records. Structured records render their event name as the
readable message and retain the original fields after a `|` separator. For a
non-health request, for example:
`INFO 2026-08-11T11:51:51+0000 request_completed | event=request_completed component=api`.
Successful health probes, including those of the image healthcheck, use the
`health_check_request` event at `DEBUG`; they are therefore hidden at the default
`INFO` threshold. When Uvicorn is started with an external logging configuration,
its root output handlers are reused and formatted rather than supplemented with
another service handler.

Request completion is logged once by the service with an `X-Request-ID`
response header. Uvicorn access logging is disabled to avoid duplicate access
records. Logs never include authorization headers, Home Assistant tokens, raw
provider responses, complete request bodies, or complete energy series.
Home Assistant history requests replace HTTPX's generic completion record with
debug-level `home_assistant_history_request` events per entity and request chunk,
each with the entity, chunk time range, HTTP status, and duration. The enclosing
history aggregate emits one `home_assistant_history_aggregate` `INFO` event after
all entities and chunks succeed, or one `WARNING` event when the aggregate fails.

The household-load and grid-flow sources log a failed build as one
`provider_fetch_failed` `ERROR` event whose `error` field holds the message. An
expected Home Assistant failure, such as a rejected token, an unreachable
endpoint, or an unknown entity, is logged without a traceback because the message
names the cause. Any other exception indicates a defect in the service and is
logged with its traceback.

Startup progress is logged at `INFO` so a container that never becomes ready can
be diagnosed from its logs: the last startup event that appears is the last phase
that completed. `process_logging_bootstrapped` is emitted before Uvicorn starts.
The `service_*` events then mark the application lifespan, logging configuration,
configuration loading (file name only), persistence-store creation, orchestrator
construction, orchestration task start, and `service_started`, each with its
elapsed time where a phase can be slow. A failed startup ends with
`service_startup_failed` instead. While the orchestrator restores persisted data,
`orchestration_restore_started` and `orchestration_restore_completed` name the
logical source, its outcome (`restored` or `empty`), and the duration, and
`orchestration_restore_failed` reports the error type and duration.
Household-load persistence reports one-time and abnormal conditions at `INFO` or
`WARNING`: `persistence_migration_started` and `persistence_migration_completed`
for a legacy JSON migration, `persistence_ndjson_invalid` for a damaged file, and
`persistence_recovered` for backup recovery, each with record counts or file
size and duration where applicable. Routine reads happen on every refresh and API
request, so their `persistence_load_completed` and `persistence_ndjson_*` detail,
including record counts, file size, and duration, is available at `DEBUG`. None of
these events log stored values or provider tokens.

## Dashboard charts

The Historic actuals and Forecast tabs draw one chart per unit: power (`kW`),
prices (`EUR/kWh`), and, for actuals, battery state of charge (`%`). Each chart
has its own legend listing only the series it draws; the Efficiency and Excluded
hours tabs draw no chart and show no legend. Each legend entry is a button that
hides or shows its line and points without a new request. A hidden entry stays in
the legend, dimmed and struck through, so it can be shown again. Only the value
axis is rescaled to the lines still shown; the time axis does not change. Hidden
lines are remembered for the page session only, and nothing is stored in the
browser. Hovering or focusing a point shows a tooltip with its line, its time in
the configured zone, and its value with the unit; the point's accessible label is
unchanged. Each chart is an image whose accessible name and description follow
the lines that are shown. The legend layout, keyboard use, tooltip format, and
accessible names are specified in [`docs/api.md`](docs/api.md#dashboard-view).

## Dashboard time zone

The dashboard shows and accepts every time in the zone set by the top-level
`timezone` setting (an IANA name such as `Europe/Berlin`, default `UTC`), not in
the browser's zone, so an operator in Germany reads the end of the local day and
tomorrow's day-ahead prices without translating UTC. Data, API timestamps, and
stored records stay in UTC. The name is matched exactly (`utc` is rejected), and
only zones whose UTC offset is a whole number of hours all year are accepted,
because the dashboard requests whole UTC hours: zones such as `Asia/Kolkata` and
`Australia/Lord_Howe` fail startup with a message naming `timezone`. The static
frontend cannot be templated, so it reads the zone from
`GET /api/v1/dashboard/settings` (`{"timezone": "Europe/Berlin"}`) before its
first data request, using the browser's `Intl` support and no dependency.

- **Plan tab:** shows the latest optimization plan hour by hour with a summary of
  its economics; see [`docs/api.md`](docs/api.md#plan-view).
- **Shown in the configured zone:** the start and end controls, the default range,
  chart axis labels, tooltips and accessible point labels, the detail lists
  (coverage, retrieved, generated, published), and the Excluded hours table.
  Headings and control labels name the zone. Times use the form
  `2026-09-30 23:00`.
- **Requests, default range, and daylight saving:** the controls convert local
  wall times to whole UTC hours, and the default range is today at 00:00 to the
  next local midnight (23 or 25 hours on clock-change days). The conversion
  example and the rules for the repeated and skipped hour at clock changes are in
  [`docs/api.md`](docs/api.md#dashboard-settings-and-time-zone).
- **Failure handling:** if the settings request fails, or the browser does not
  know the configured zone, the dashboard reports an explicit error, keeps the
  range controls disabled, and sends no data request. It never guesses a zone.

## Docker Deployment

Build the production image from the repository root:

```bash
docker build --tag energy-optimizer .
```

Start the service with a configuration mounted from the host. The image also
contains `config.example.yaml` as a safe default:

```bash
docker run --detach --name energy-optimizer \
  --publish 8000:8000 \
  --volume "$PWD/config.yaml:/app/config.yaml:ro" \
  --volume energy-optimizer-data:/app/data \
  energy-optimizer
```

The container listens on port `8000`, runs as a non-root user, and uses
`ENERGY_OPTIMIZER_CONFIG` to select a different configuration path when needed.
The example configuration stores normalized provider data under
`/app/data/provider-data`; mount `/app/data` as a durable volume so data survives
container replacement. The image healthcheck calls the service health endpoint
every 10 seconds; check it directly with `curl http://localhost:8000/health`.
`./scripts/docker-smoke` runs the local container smoke test, which builds the
image and waits for the health endpoint.

## Data Providers

Prices and forecasts may be supplied through the HTTP API or retrieved from
external data providers, which may support day-ahead electricity prices, PV
generation forecasts, and weather data. Providers should be configurable and
isolated from the optimization model, and provider data must be validated before
use. See [`docs/architecture.md`](docs/architecture.md#providers) for the
provider boundary.

### aWATTar Germany electricity prices

The optional `AwattarImporter` retrieves unauthenticated German EPEX Spot day-ahead
prices from `https://api.awattar.de/v1/marketdata`. Configure the `awattar` section
and enable the `electricity_prices` orchestration source to persist validated
hourly values. The importer accepts only contiguous one-hour `Eur/MWh` intervals,
converts them to `EUR/kWh`, and exposes the market value for both import and export
directions in the normalized price contract. Negative market prices are supported.

These are wholesale German market prices, not household tariffs: taxes, network
charges, supplier margins, and feed-in adjustments are not included. The endpoint
does not require an API key, but deployments should use a reasonable polling
interval and configure `max_data_age_seconds` for freshness checks.

Every request carries an explicit window as epoch milliseconds: `start` is the
importer's hour-aligned start time (inclusive) and `end` is its `end_time`, or 48
hours after `start` when no end time is given (exclusive). aWATTar publishes the
next day's prices at 14:00 local time, so the fixed, non-configurable 48-hour
look-ahead always reaches them once they exist. A response that ends earlier,
such as the end of the current day before publication, is accepted unchanged.
The Forecast dashboard keeps the imported and exported price series separate while
retaining the same timestamps and values for this single-market-price source.

### EPEX Predictor price forecast

Day-ahead prices are published once a day for the next day, so the published
prices end at most about 34 hours ahead. To plan a week, configure
`epex_predictor` and enable the optional `electricity_price_forecast`
orchestration source. It reads hourly market-price forecasts in EUR/MWh from
[EPEX Predictor](https://github.com/b3nn0/EpexPredictor) (`GET /prices`, public
instance `https://epexpredictor.batzill.com` by default; set `base_url` for a
self-hosted instance) for `region` (default `DE`) over `forecast_hours` (default
168). Published prices always win: the forecast is only appended after the last
published hour, the grid tariff applies to both, and `GET /plan` reports the first
forecast hour as `price_forecast_from`. The source is not a required plan input:
if it fails or is stale, the plan uses the published prices only. Forecast data is
provided by EPEX Predictor with prices under CC-BY-4.0 by energy-charts.info and
ENTSO-E and weather data by Open-Meteo.com; the public instance is free on a fair
use basis without guarantees.

```yaml
epex_predictor:
  region: DE
  forecast_hours: 168
orchestration:
  sources:
    electricity_price_forecast: {enabled: true, interval_seconds: 3600}
  optimization:
    horizon_hours: 168
open_meteo_pv:
  forecast_days: 7
```

The plan ends where the prices or the PV forecast end, and at most after
`orchestration.optimization.horizon_hours` (default 168). Set the PV forecast to
the same number of days; Forecast.Solar's free tier only covers today and tomorrow.

### EV history from InfluxDB

The Home Assistant recorder usually keeps only days, but its InfluxDB export keeps
months. Configure `influxdb` (InfluxDB v1 HTTP API, `database` default
`homeassistant`, optional `username`/`password`) and enable the `ev_history`
orchestration source to import, for every hour of the last `history_days`
(default 180), the EV state of charge in percent and whether the car was plugged
in, both taken from the `home_assistant.ev` entities. Home Assistant's InfluxDB
integration stores an entity under its unit (or under its entity ID when it has
none) with the `entity_id` tag without the domain. Because it writes only state
changes, each hour takes the last value up to its end and hours without a change
keep the previous value. Hours newer than `raw_retention_days` (default 89) come
from the raw policy `autogen` (field `value`), older ones from the long-term
policy `downsample_rp` (minute means, field `mean_value`); a plug share of at
least 0.5 counts as plugged in. Each refresh re-reads the last two days and keeps
the persisted history otherwise; a failed refresh keeps it unchanged. When the
live plug entity has no reliable history, `ev_plugged_in_entity_id` names another
entity for the plug history only (for example the car's own charge-port sensor).

```yaml
influxdb:
  base_url: http://homeassistant.local:8086
  history_days: 180
orchestration:
  sources:
    ev_history: {enabled: true, interval_seconds: 3600}
```

### Normalized provider-data persistence

When the optional `persistence.directory` setting is configured, the service
stores the validated normalized data model returned by a configured provider,
keyed by its data type, provider, and entity identifier, but not raw provider
responses, client-only submissions, or optimizer snapshots. For replace-based
JSON writes, a temporary file is flushed and synced before atomic replacement and
the previous valid value is retained as a backup. If the primary file is invalid
after a restart, the backup is validated and restored; if neither copy is valid,
retrieval returns a service-unavailable error and the invalid files are not
silently accepted.

Household-load history is stored as human-readable newline-delimited JSON in
`*.ndjson`, with one hourly observation per line. Each line contains the
timestamp, load value, source identity, and retrieval metadata needed to rebuild
the validated normalized model. Retention (the newest 87,672 contiguous hourly
values), last-line-wins handling of overlapping API corrections, compaction, the
ignored incomplete final line, and the migration of existing household-load
`*.json` and `*.json.bak` files to NDJSON on first access without losing valid
primary or backup data are described in
[`docs/architecture.md`](docs/architecture.md#providers). See
[`docs/api.md`](docs/api.md) for the API persistence behavior and endpoint
contracts.

### Scheduled orchestration

The optional `orchestration` configuration schedules registered providers without
putting polling or scheduling behavior in provider adapters. Each source has an
independent `interval_seconds` and optional provider history lookback. With
`startup_fetch: true`, enabled sources are fetched when the service starts; failed
attempts leave the last valid persisted data in place and are reported through
service logs. Missed intervals are not replayed: the next run is scheduled from
the completed attempt. Scheduled collection requires `persistence.directory`, so
normalized data survives application restarts.

Household-load collection bootstraps with up to the API maximum of 87,672 hourly
values, or from the earliest safely derivable hour if Home Assistant retains less
history. Later requests begin at the first hour after the final persisted hour, so
scheduled collection never re-fetches completed hours already in the store. If no
completed hour is missing, the cycle skips the provider request and persistence
write. When the service was down for longer than Home Assistant retains its
history, the returned range starts after the persisted end; the hours in between
can never be fetched again, so each of them is stored as an excluded hour with
the reason `history_unavailable` (see "Which hours are imported") and the refresh
succeeds instead of failing on every later run. Each gap is reported by one
`provider_history_unavailable` warning per source and refresh with the number of
missing hours and their first and last hour.

Each cycle runs in three phases (declare, import, build) so that a Home
Assistant entity is downloaded at most once per cycle, even when several sources
use it, in seven-day chunks; each source then builds its record from the shared
series with its own entity settings. The phases are detailed in
[`docs/architecture.md`](docs/architecture.md#providers). An entity that cannot be
imported at all, for example after a request failure, fails only the sources that
read it, and the error names the entity. Bad samples never fail a source: they
exclude hours.

A rejected Home Assistant token (HTTP 401 or 403) would reject every further
request too, so the import ends at the first rejection: no further request is
sent, every entity that was not imported fails with the same authentication error
naming that entity, and the entities imported before the rejection stay
available. One `home_assistant_history_authentication_failed` warning names the
rejected entity and the number of entities that were not requested. The battery
source's live state request is a separate request and still makes its own attempt.

Grid-flow collection bootstraps and refreshes like household load: the first run
requests up to the 87,672-hour maximum (or all history Home Assistant retains),
and every later run requests only the completed hours after the retained history.
Each save is merged into one contiguous hourly history in the generic JSON
provider store (see [`docs/architecture.md`](docs/architecture.md#providers) and
[`docs/api.md`](docs/api.md#grid-flow-api)), using the same Home Assistant energy
semantics as household load and combining multiple signed entities independently
for import and export. The retained history is served by the historic multi-asset
dashboard read API (see
[`docs/api.md`](docs/api.md#historic-multi-asset-dashboard)). The bootstrap only
happens when no grid-flow record exists: a deployment that previously stored just
the latest hour continues from that hour. To back-fill the history Home Assistant
retains, stop the service, delete the `grid-flow-*` files from the persistence
directory, and start it again.

Electricity-price collection also records every retrieved price hour in a
separate `electricity-price-history` record (see [`docs/api.md`](docs/api.md#series)).

Automatic plan generation runs when `orchestration.optimization.enabled` is true
and persistence is configured. Every cycle in which at least one required source
refreshed creates a plan, provided every required source holds current, valid
data, whether refreshed now or restored from persistence after a restart. Require `household_load`, `pv_generation` and
`electricity_prices`; add `battery` to plan its dispatch. The plan generator
receives one coherent `ProviderDataSnapshot`, plans every future hour covered by
both the prices and the PV forecast, and stores the latest optimal plan, which
`GET /plan` returns (404 before the first plan). A snapshot the solver cannot
satisfy is reported as `plan_status: failed` and keeps the previous plan. The load
forecast is chosen by a backtest (`load_forecast.method`, default `auto`): each
method forecasts the last `backtest_days` (default 7) days from the history before
them and the one with the smallest mean absolute error wins. Methods are
`seven_day_mean` (the same local hour over the last seven days) and
`weekday_profile` (the same local hour on the same kind of day, weekday or weekend,
over the last 28 days; at least two samples per hour). Excluded hours never
contribute. If the best method cannot forecast the planned hours the next one is
tried; if none can, `filled_profile` takes over: the mean of each local hour over
the last 28 days, and an hour of day without any valid sample gets the mean of
its nearest hours (logged as `load_forecast_gaps_filled`). Only a history without
any valid hour fails planning; that failure keeps the previous plan and is logged
as one warning without a traceback. `GET /plan` reports the method and its backtest error as `load_forecast`. The battery round-trip
efficiency is split evenly over charging and discharging, every kWh the battery
delivers costs `home_assistant.battery.throughput_cost_eur_per_kwh` (default 0, the
wear of the cells; the optimizer only cycles the battery when the price spread pays
for it), the cost is part of `objective_eur` and reported as `degradation_cost_eur`
and `battery_throughput_kwh`. By default the plan must end with
at least the current state of charge (`end_of_horizon: restore_initial`). With
`end_of_horizon: value_stored_energy` the plan may end emptier: energy left in the
battery is credited with `end_of_horizon_value_eur_per_kwh` (default: the cheapest
import price of the plan times the discharge efficiency, less 2 % so that the
battery rather than the grid supplies the cheapest hour, what refilling it
costs at best; a higher value would make the plan charge from the grid only to
end full), so a rolling plan keeps
energy only while it is worth more than using it now. Concurrent orchestration cycles are skipped
to avoid duplicate plans.

### Grid tariff

aWATTar delivers the net exchange price. Configure an optional `tariff` to plan with
the price on the bill instead: `grid_fee_bands` (local hours, `start_hour` inclusive,
`end_hour` exclusive, together covering the day exactly once, with `fee_ct_per_kwh`),
`supplier_surcharge_ct_per_kwh`, `levies_ct_per_kwh`, `concession_fee_ct_per_kwh`,
`electricity_tax_ct_per_kwh` and `vat_rate`, all net. The import price of an hour is
`(market price + grid fee of the local hour + fixed components) * (1 + vat_rate)`;
the export price stays the market price unless `feed_in_ct_per_kwh` sets a fixed
feed-in tariff (for example the EEG remuneration), which then applies to every
exported kWh. Without `tariff` the plan uses the market price for both directions. Example (section 14a module 3, Westnetz):

```yaml
tariff:
  grid_fee_bands:
    - {start_hour: 0, end_hour: 7, fee_ct_per_kwh: 0.95}
    - {start_hour: 7, end_hour: 15, fee_ct_per_kwh: 9.53}
    - {start_hour: 15, end_hour: 20, fee_ct_per_kwh: 15.65}
    - {start_hour: 20, end_hour: 24, fee_ct_per_kwh: 9.53}
  supplier_surcharge_ct_per_kwh: 1.0
  levies_ct_per_kwh: 2.946
  concession_fee_ct_per_kwh: 1.32
  electricity_tax_ct_per_kwh: 2.05
  vat_rate: 0.19
  feed_in_ct_per_kwh: 7   # fixed EEG remuneration of exports
```

### Electric vehicle charging

Configure the car under `home_assistant.ev` and the wish under `ev_charging`, then
add `ev` to the orchestration sources and the required plan sources:

```yaml
home_assistant:
  ev:
    state_of_charge: {entity_id: sensor.ev9_battery_level, unit: "%"}  # or kWh
    plugged_in: {entity_id: binary_sensor.ev9_charging_cable, plugged_in_states: ["on"]}
    capacity_kwh: 99.8
    maximum_charge_kw: 11
    charge_efficiency: 0.9
ev_charging:
  minimum_soc_percent: 80
  deadline: "07:00"          # local time, see `timezone`
```

Strategies (`ev_charging.strategy`): `deadline` (default), `immediate`, `surplus`, `minimum_plus_target` and `daily_minimum`. With
`immediate` the car charges at full power from the first hour while plugged in until
`minimum_soc_percent` is stored (the last hour uses only the remaining energy);
no deadline is needed and the rest of the plan is optimized around that fixed load.

Targets per weekday: instead of (or on top of) the general `minimum_soc_percent` and
`deadline`, give a `weekly` block. A named day replaces the general target on that
day, other days use the general one, and a day with neither has no deadline. The plan
uses the next deadline after the first planned hour (looking up to a week ahead):

```yaml
ev_charging:
  strategy: deadline
  minimum_soc_percent: 60    # general target, optional with a weekly block
  deadline: "09:00"
  weekly:
    monday: {minimum_soc_percent: 80, deadline: "06:30"}
    saturday: {minimum_soc_percent: 50, deadline: "12:00"}
```

With `minimum_plus_target`, `target_soc_percent` must not be below any minimum.

With `surplus` the car charges only with PV that the household load leaves over
(forecast PV minus forecast load, per hour), up to `minimum_soc_percent` (default
100). It has no deadline and no target; each kWh stored is valued above exporting
it, so the car can take even a small hourly surplus.
It never draws from the grid because its power is bounded by the surplus.

With `minimum_plus_target` the car must hold `minimum_soc_percent` at the deadline
as in the `deadline` strategy and is charged further, up to `target_soc_percent`,
while a stored kWh (after charging losses) is worth more than it costs:
`value_eur_per_kwh` is what a stored kWh above the minimum is worth to you. A value
of 0.25 charges extra whenever the price per stored kWh is below 25 ct, which
makes it a price cap for the extra energy.

The `deadline` strategy: the car must hold `minimum_soc_percent` at the next
local `deadline`. The plan charges it in the cheapest hours before then, using PV
surplus first because exporting it pays less than importing costs. Charging is
planned only while the car is plugged in (the current plug state is assumed for the
whole horizon) and only before the deadline. Hourly charge power is an average
between zero and `maximum_charge_kw`. If the target cannot be reached, the plan
charges as much as possible and reports `ev_shortfall_kwh` instead of failing. A
deadline beyond the planning horizon is not planned yet. The plan hours carry an
`ev` object with `charge_kw`, `state_of_charge_kwh` and `state_of_charge_percent`.

An hourly average of 0.805 kW means 0.805 kWh during that hour, which can be
delivered by charging at a higher power for part of the hour. There is no minimum
hourly charging power. For example, 11.805 kWh across two hours priced at 35 and
40 ct/kWh can be scheduled as 11 kW in the cheaper hour and 0.805 kW in the next.
Remove `home_assistant.ev.minimum_charge_kw` from existing configuration files
when upgrading; the removed setting is rejected as an unknown field.

The existing wallbox controller forwards the hourly average as a power setpoint;
it does not convert it into timed charging pulses or schedule intrahour switching.
Such execution requires a controller that implements the planned hourly energy
using the wallbox's supported instantaneous power range.

#### Soft battery reserve

Load forecasts are wrong by some tenths of a kW per hour; if the plan drains the
battery exactly, a higher load means expensive grid imports. With
`home_assistant.battery.reserve` the plan keeps a soft reserve above the minimum
state of charge. Its size at planned hour t is `confidence * sigma *
sqrt(min(t + 1 - unplanned_hours, growth_cap_hours))`: hourly errors partly cancel,
so the energy the house may draw beyond the forecast grows with the square root of
the lead time. sigma is `forecast_error_kw`, or the backtested mean absolute load
error times sqrt(pi/2). The first `unplanned_hours` (default 2) have no reserve
because the plan is recomputed every hour with the measured state of charge, and
the reserve stops growing after `growth_cap_hours` (default 24).

The reserve is soft: the battery may use it, at a price per kWh of the deepest
shortfall of each planned day (a forecast error costs one expensive import, not one
per hour). The first half of the reserve costs `first_penalty_eur_per_kwh`
(default 0.05), the second half `second_penalty_eur_per_kwh` (default 0.15), about
the expected extra cost of an evening import. When discharging saves more, the
battery still goes down to its minimum. The penalty is not part of the reported
objective; each planned battery hour reports `reserve_percent`, shown in the Plan
tab as "Battery reserve (%)".

```yaml
home_assistant:
  battery:
    reserve:
      confidence: 1.0
      unplanned_hours: 2
```

#### Soft battery headroom

A battery that is completely full while PV is still expected exports any PV above
the forecast for the feed-in tariff. With `home_assistant.battery.headroom` the plan
keeps `percent` (default 10) of the capacity free below the maximum state of charge
in planned hours with a PV forecast, after the first `unplanned_hours` (default 2).
Like the reserve it is soft and charged once per planned day for the highest
overshoot: the first half costs `first_penalty_eur_per_kwh` (default 0.05), the
second half `second_penalty_eur_per_kwh` (default 0.15). Both stay below the gain of
storing PV instead of exporting it, so the battery still fills up when nothing
better takes the surplus; the penalty only tips close decisions, such as moving
battery energy into the car earlier. Battery hours report `headroom_percent`, shown
in the Plan tab as "Battery headroom (%)", and the penalty is part of
`Cost (Optimizer)` at the fullest hour.

#### Daily minimum over a multi-day plan

With `daily_minimum` the car must hold `minimum_soc_percent` (default 30) at
`deadline` (default 07:00 local) on every morning of the plan; a `weekly` block
changes the minimum or the time per weekday. In between, the plan charges on the
cheapest days and hours, which with a week-long plan (see the EPEX Predictor
price forecast) can mean charging once for several days. With the `ev_history`
source (see EV history from InfluxDB) the plan learns from the history:

- the driving energy per local weekday, the sum of the state-of-charge drops of
  each day with at least 20 known hours, averaged per weekday and deducted in the
  hour the car is expected back (else at 18:00);
- the plug-in share per weekday and hour; the car can only charge in hours with
  a share of at least 0.5. The current plug state holds until the learned pattern
  next changes, and hours without at least two samples keep the current state.

Every kWh stored in the car is worth the cheapest import price of the plan (what
charging it later costs at best), so PV surplus goes into the car while it is home
instead of being exported for less, without requiring the battery to top up small
hourly charging amounts, and the plan no longer ends with an almost empty car.
Charging from the grid only to store energy never pays because of the charging
losses. `target_soc_percent` (default 100) caps the state of charge; the credit is
not part of the reported objective.

`home_hours` (for example `{start: "22:00", end: "06:00"}`, may wrap midnight)
are local hours in which the car is always home: it can charge in them on every
planned day whatever its current plug state and plug history say, because it is
plugged in when needed. The learned pattern applies outside them.

When the car is home at night but plugged in only when needed, add a
`plug_in_reminder` (it needs `home_hours`). From `at` (default 21:00 local) until
the end of the night's home hours the service checks the latest EV schedule every
`poll_interval_seconds`; if it charges the car in the rest of the night and Home
Assistant reports it unplugged, every notify service is called once for that night
with `title` and `message` (`{energy_kwh}` and `{start}` are filled in). The sent
night is persisted, so a restart does not repeat it; a failed call is retried at the
next check, and an unknown plug state sends nothing.

```yaml
ev_charging:
  strategy: daily_minimum
  home_hours: {start: "22:00", end: "06:00"}
  plug_in_reminder:
    services: [notify.mobile_app_phone]
    at: "21:00"
    message: "Bitte EV anstecken: {energy_kwh:.0f} kWh ab {start}"
```

With `daily_minimum`, `cheap_market_price_ct_per_kwh` (default unset, for example 1)
sets the reference spot price (EPEX, before fees). A kWh stored in the car is worth the
gross price at that spot price divided by the charging losses (about 23.6 ct with the
example tariff and 90 % charging efficiency). Charging from the grid then pays exactly
when the gross price is below that value, which is the case at about 1 ct spot, and PV
surplus is stored whenever that is worth more than its feed-in remuneration. The
morning minimums still hold the car's buffer. Keep the reference spot price such that
the stored value stays above the feed-in tariff, otherwise PV is exported instead.

Without a history the car is expected at the charger in its current state and
not to drive. Energy the plan cannot provide for a morning minimum or for
driving is reported in `ev_shortfall_kwh` instead of failing. Each planned hour's
`ev` object carries `expected_plugged_in` and `consumption_kwh`, and the Plan tab
shows them as "EV at home" and "EV driving (kWh)".

```yaml
ev_charging:
  strategy: daily_minimum
  minimum_soc_percent: 30
  deadline: "07:00"
```

#### Controlling the wall box

Planning only shows what to do. To execute the plan, add an opt-in `control` block
to `home_assistant.ev`:

```yaml
home_assistant:
  ev:
    control:
      enabled: true
      set_power:                       # Home Assistant service that limits the charge power
        service: number.set_value
        entity_id: number.easee_charger_dynamic_limit
        data_field: value              # default
        unit: A                        # A (default), W or kW
        phases: 3                      # used to convert kW to A
        voltage: 230
      enable_entity_id: switch.easee_charger_enabled   # optional: pause and resume
      max_plan_age_seconds: 7200
      poll_interval_seconds: 30
```

Integrations that address the charger as a device, such as Easee, take a
`device_id` instead of `entity_id` (exactly one of them is required).
`extra_data` is sent with every call; a `time_to_live` makes the wall box drop the
limit on its own if this service stops:

```yaml
      set_power:
        service: easee.set_charger_dynamic_limit
        device_id: 0123456789abcdef0123456789abcdef   # Settings > Devices > Easee charger
        data_field: current
        unit: A
        phases: 3
        extra_data: {time_to_live: 60}             # minutes
```

With Easee, a dynamic limit of 0 A pauses charging, so `enable_entity_id` is not
needed. Use the wall box status sensor as `plugged_in` (for example
`plugged_in_states: [awaiting_start, charging, completed, ready_to_charge]`); a
locked cable alone does not mean a car is connected.

Every `poll_interval_seconds` the service looks up the planned power of the running
hour and sends the command only when it changed: `homeassistant.turn_on` plus the
power for charging, `homeassistant.turn_off` for a pause (without
`enable_entity_id` the limit is set to 0). A plan starts at the next full hour, so
the running hour comes from the most recent earlier plan that covers it; the hours
of all plans are kept in an EV schedule. If no plan covers the hour or the schedule
is older than `max_plan_age_seconds`, the charger is handed back to its own
behaviour once (enabled at `maximum_charge_kw`). A rejected command is logged and
retried at the next run.

### Home Assistant measured PV history

Configure `home_assistant.pv_generation` as an energy aggregation of cumulative
Wh, kWh, or MWh counters (the same `terms` mapping as household load). Enable
orchestration and its `pv_generation_history` source with an hourly interval and
optional `history_lookback_seconds`. Persistence is required for orchestration.
The importer bootstraps from available Home Assistant retention, fetches weekly
chunks, and thereafter requests only newly completed hours. It retains up to
87,672 hourly values. Invalid or incomplete hours remain null gaps with detailed
causes in the dashboard's Excluded hours tab; failed requests preserve existing
history. Entity mappings include units, state class and optional physical limits.

Measured production appears as `pv_generation_actual` on the historic dashboard
and in `GET /api/v1/dashboard/data?scenario_kind=actual`, with hourly UTC
timestamps, kW values and observation/source metadata. It is stored separately
from Forecast.Solar's forecasts, which continue using the `pv_generation`
orchestration source and `scenario_kind=forecast`.

### Forecast.Solar PV forecast retrieval

`ForecastSolarImporter` retrieves PV production forecasts directly from the free
public Forecast.Solar API. Home Assistant, an account, and an API key are not
required. Configure the `forecast_solar` section of
[`config.example.yaml`](config.example.yaml) with the installation location, panel
declination and azimuth, and installed peak power. Forecast.Solar uses azimuth `0`
for south, `-90` for east, and `90` for west.

The provider converts Forecast.Solar's irregular sunrise and sunset
`watt_hours_period` response into hourly UTC `generation_kw` values. It validates
timestamps, time-zone metadata, coverage, units, and finite non-negative values,
then returns `PvGenerationData` with `retrieved_at` and `expires_at` metadata.
Forecasts are persisted through the generic replace-based JSON provider store.
The public tier is free for private use, supports one plane, hourly resolution,
and today plus the following day. Configure polling conservatively to respect
the public rate limit of 12 requests per IP per rolling hour.

### Open-Meteo multi-plane PV forecast

`OpenMeteoPvImporter` forecasts PV production for an array spread over several
roof faces. Configure the `open_meteo_pv` section of
[`config.example.yaml`](config.example.yaml) with the location, one entry per
plane (`tilt_degrees`, `azimuth_degrees`, nameplate `peak_power_kw`) and a
`performance_factor`. Azimuth uses the same convention as Forecast.Solar: `0`
south, `-90` east, `90` west and `180` north. Configure either
`forecast_solar` or `open_meteo_pv`, not both; the service refuses to start
otherwise.

Open-Meteo answers one tilt and azimuth per request, so each plane is one
request for `global_tilted_irradiance`. The hourly forecast is

```text
generation_kw = performance_factor * sum(irradiance_w_per_m2 / 1000 * peak_power_kw)
```

Open-Meteo labels an hourly value with the end of the hour it was averaged
over; the importer assigns it to the hour that starts one hour earlier. An hour
counts only when every plane reports it, and a requested hour without
irradiance, a non-UTC response, malformed values, HTTP errors and timeouts are
provider errors. The forecast covers `forecast_days` days (default 2, today and
tomorrow) and is stored under provider `open-meteo` with the same
`pv_generation` source and dashboard series as Forecast.Solar.

`performance_factor` turns nameplate power into measured AC power: inverter,
cable, temperature and shading losses plus any bias of the irradiance
forecast. Calibrate it once from measured production as the least-squares
ratio of measured hourly AC energy to the unscaled sum above over the same
hours, using Open-Meteo's historical forecast API
(`historical-forecast-api.open-meteo.com`) so the factor matches the forecast
irradiance the service uses. Fit only hours after the last change to the
array. Forecast.Solar's estimates are not interchangeable here: for the same
plane and hours they can be about half of Open-Meteo irradiance times nameplate
power, so a factor fitted against one provider does not hold for the other.

### Home Assistant household-load importer

`HomeAssistantLoadImporter` is a reusable provider adapter for Home Assistant's
REST history API. A requested half-open hourly period is read in contiguous
requests of no more than seven days, fetched sequentially, and the available
retained subset is used when the requested start predates Home Assistant's
history. The importer returns provider-independent household-load data with
`load_kw`, `unit: "kW"`, source metadata, retrieval time, and the latest source
observation time. Household-load sources must be energy entities configured with
Home Assistant's `state_class` (`total` or `total_increasing`) and `unit` (`Wh`,
`kWh`, or `MWh`), grouped into an energy aggregation by `operation` (`add` or
`subtract`). The importer converts each cumulative counter's observed increases
into hourly kW-equivalent values and combines all contributions into one logical
`household_load` record. Instantaneous power entities reported in `W` or `kW` are
rejected and are never implicitly converted to energy.

Configure the Home Assistant URL, bearer token, the household-load energy
aggregation, and request timeout under `home_assistant` in `config.yaml`; the
example configuration adds a household meter and subtracts an EV meter. An
optional `max_data_age_seconds` setting enables a polling health check
(`is_fresh(data)`); it does not invalidate historical data. Home Assistant stores
only changes, so a value that is still valid counts as current when it is read: a
live state (battery, EV) at the retrieval time, an energy counter up to the end of
the requested window. Only unavailable or invalid states make a source stale. The token is a secret
and must not be committed to source control. The normalized aggregate is
persisted and exposed under the single source identity
`home-assistant/household_load`, so storage, freshness evaluation, and
orchestration consumers receive one coherent household-load dataset.

The importer's `plan(start_time, end_time, history_lookback_seconds)` method
declares the history a period needs and how to build its record; `end_time` may
be omitted to use the latest completed UTC hour. The lookback asks Home Assistant
for an earlier state so the last known value can be carried into the first
requested hour; it is applied only to the first weekly request, and later requests
start exactly at the previous request's end. A failed chunk fails the complete
import and leaves the last valid persisted history for the next scheduled retry.
Polling, scheduling, caching, and orchestration remain outside this provider
adapter (see [`docs/architecture.md`](docs/architecture.md#providers)).

#### Which hours are imported

An hour is imported only if every data point that contributes to it is valid.
Every other hour is **excluded**: it has no value (`null` in the API), it is left
out of every chart and calculation, and it is listed with each cause and its exact
data points on the **Excluded hours** dashboard tab and through
`GET /api/v1/dashboard/excluded-hours`. Nothing is repaired, tolerated, or
estimated, so there is no reset, spike, recovery, or jitter handling. One bad
sample never fails an import and never blocks a later one: the persisted history
advances past excluded hours, which keep their place without a value. Only
problems that make an entity unusable as a whole (authentication or request
failures, malformed responses, an entity without any history, an instantaneous
power unit, duplicate timestamps, a period without a complete hour) remain errors
that fail the sources that read it, with a message naming the entity.

No interpolation is performed: the latest observed counter value is carried
forward until the next observation, and Home Assistant records only state changes,
so a counter keeps its value between two observations. The energy of a step
between two observations belongs to the hour of the later observation, and an
observation exactly on an hour boundary belongs to the hour it closes. A sensor
that publishes on the hour and is unavailable at `01:00:00` therefore has no
closing reading for the hour before it. A counter that drops to zero and returns
excludes the hours of the drop and of the step back up, and the hours before and
after keep their true energy.

The closed set of reason codes (every excluded hour lists at least one), the hours
each cause excludes, and the response format are documented in
[`docs/api.md`](docs/api.md#excluded-hours) and under "The hour rule" in
[`docs/architecture.md`](docs/architecture.md#providers). Of the invalid-sample
reasons, `unavailable` means the state is `unknown` or `unavailable`, `non_numeric`
that it is not a number, `not_finite` that it is `nan` or infinite,
`negative_value` that a counter reports a negative value, and `invalid_attribute`
that `state_class` or `last_reset` is not usable. A combined hour of an
energy aggregation is excluded when any contributing entity is excluded for it
(only the entity that caused it is named), and also when the signed terms make it
not finite or, with the default `part: net`, negative. Grid import and export are
excluded together, and the state-of-charge and energy legs of the battery
efficiency history are excluded together.

`maximum_interval_energy_kwh` is a per-entity setting (default `100` kWh; see
[`config.example.yaml`](config.example.yaml)) with the unit-converted maximum
energy of one entity in one hour and in one counter step. Equality is accepted.
The limit applies to the source entity only; override it with the meter or
inverter's credible maximum hourly energy. The former `decrease_tolerance_kwh`
setting no longer exists, and a configuration that still contains it is rejected
at startup because unknown keys are not accepted; remove it from any existing
`config.yaml`.

Excluded hours are persisted with the history, next to the hour they explain, and
survive restarts. A refresh that excluded hours still completes as `success` and
does not block optimization; it logs one `provider_hours_excluded` warning per
source and refresh with the number of excluded hours by reason. Hours that an
earlier version flagged `suspect` become excluded hours with the reason
`flagged_by_earlier_version`.

#### Energy aggregations

Household load (`household_load`), grid import and export (`grid_import`,
`grid_export`), and both sides (`energy_in` and `energy_out`) of each battery
efficiency leg are configured with the same aggregation object, shown under
`home_assistant` in [`config.example.yaml`](config.example.yaml): a list of
`terms`, one per `operation` (`add` or `subtract`), each with at least one entity
(`entity_id`, `state_class`, `unit`, and an optional
`maximum_interval_energy_kwh`), and an optional `part`. The hourly value is the energy of all `add` entities minus the
energy of all `subtract` entities. An entity ID may appear only once within one
aggregation, but the same entity may be used in several, for example when one
physical meter provides both household-load and grid-flow measurements: it is
imported once and normalized with each aggregation's own entity settings, so it is
not double-counted within either aggregate.

`part` says what a negative hourly sum means. With `net` (default) it is invalid
data: the hour is excluded with the reason `combined_negative`. With `positive`
only the positive part of the sum is wanted, so a negative sum is a legitimate
zero: the hour is imported as `0`, is not excluded, and is counted in
`clamped_hour_count` in the `home_assistant_history_aggregate` log line of the
aggregation, together with `part` and `excluded_hour_count`. `part: positive` is
for sums that are negative in ordinary operation, such as the AC-sourced charging
energy of a DC-coupled battery (see
[`docs/api.md`](docs/api.md#home-assistant-battery-import)); a non-finite sum and
every entity-level exclusion still exclude the hour.

The former `household_load_entities`, `grid_import_entities`, and
`grid_export_entities` lists, and a per-entity `operation`, are no longer
accepted. Wrap the entities in `terms` and rename the keys.

### Home Assistant grid-flow importer

`HomeAssistantGridFlowImporter` composes the shared Home Assistant energy-history
retrieval and normalization functionality for grid import and export. Configure
`grid_import` and `grid_export` together (see `home_assistant` in
[`config.example.yaml`](config.example.yaml)), each as an energy aggregation (see
"Energy aggregations") of one or more entities; every entity uses `state_class`
(`total` or `total_increasing`) and an energy `unit` (`Wh`, `kWh`, or `MWh`), and
the `operation` (`add` or `subtract`) of its term. Import and export are aligned
to their common available hourly start, and an hour excluded in either channel is
excluded in both. Long grid-flow requests use the same contiguous, seven-day
maximum chunks as household load, and a failed chunk or channel never produces a
partial result. The normalized result is persisted under `home-assistant/grid_flow`
as a retained hourly history. See [`docs/api.md`](docs/api.md) for the
corresponding endpoint behavior.

## Development

Development follows the ordered product backlog. Each feature should deliver
direct user value and include its required implementation, tests, documentation,
configuration, and deployment work. See [`AGENTS.md`](AGENTS.md) for backlog,
issue, and engineering process guidelines.

### Test-Driven Development

Tests follow a test-driven development pattern: write a test that expresses the
expected behavior and fails for the current implementation, implement the
smallest change that makes it pass, run the relevant test suite and verify that
all tests pass, then refactor while keeping the tests passing.

### Setting Up and Verifying a Checkout

```bash
scripts/bootstrap      # locked dependencies, file linters, Chromium, system libraries, then preflight
scripts/preflight      # check tools, GitHub credentials, the virtual environment, Chromium, file linters
scripts/verify         # lint, format, types, OpenAPI, unit tests, and file linters, exactly as CI runs them
scripts/verify --e2e   # the same plus the browser end-to-end suite
scripts/verify lint types   # or only the named steps
scripts/verify workflows dockerfile yaml shell   # only the file linters
```

`scripts/verify` runs every requested step and prints a summary, so one run
shows all failures. CI calls the same script one step at a time. To start work
on an issue, run `scripts/start-issue <issue-number> <short-description>`; add
`--dry-run` to preview it.

### File Linters

Besides the Python checks, `scripts/verify` lints the files that are not Python:

| Step | Tool | Checks |
| --- | --- | --- |
| `workflows` | `actionlint` | GitHub Actions workflows, including the shell in `run:` blocks |
| `dockerfile` | `hadolint` | `Dockerfile` |
| `yaml` | `yamllint --strict` | every `*.yml` and `*.yaml` file, and `.yamllint` |
| `shell` | `shellcheck` and `shfmt -d` | the scripts in `scripts/` |

Each step fails on any finding, including warnings and info-level findings, and
CI runs each one through `scripts/verify <step>`. The configuration is checked
in at the repository root: `.yamllint`, `.hadolint.yaml`, and `.editorconfig`,
which sets the indentation `shfmt` enforces. Every disabled rule and excluded
file carries a comment stating why. To suppress a finding inline, put the reason
in a comment on the line above the `# shellcheck disable=` or
`# hadolint ignore=` directive. Shell scripts live in `scripts/`, the only
directory the `shell` step checks.

The tools are pinned to exact versions in the `dev` extra of `pyproject.toml`
and locked in `uv.lock`, so `scripts/bootstrap` (or
`uv sync --locked --extra dev`) installs the same versions locally and in CI. The
packages wrapping a native tool provide the upstream release binary and verify
its SHA-256 checksum. The first three components of a wrapper's version are the
upstream version, for example `actionlint-py` 1.7.12.25 provides `actionlint`
1.7.12. `actionlint-py` and `shfmt-py` have no prebuilt wheels: installing them
builds a source package that downloads the binary from GitHub, so the first
install needs access to `github.com`. `scripts/preflight` names any linter that
is missing and says how to install it, as a warning normally and as a failure
with `--strict`. To update a tool, change its pin in `pyproject.toml`, run
`uv lock`, and run `scripts/verify`.

## Running the Service

Install [`uv`](https://docs.astral.sh/uv/getting-started/installation/), then
create/update the project virtual environment and lockfile dependencies:

```bash
uv sync --extra dev
```

`uv sync` reads dependencies from `pyproject.toml`, resolves them into `uv.lock`,
and installs project plus development dependencies into `.venv`. Use `uv run`
to execute commands inside this environment. CI uses
`uv sync --locked --extra dev` so it fails when lockfile no longer matches
project metadata.

Create a runtime configuration before starting the service. Startup validates the
YAML structure and types; missing, malformed, or invalid configuration stops
startup with an error identifying the file and invalid fields:

```bash
cp config.example.yaml config.yaml
```

Start the service through the package entrypoint so logging is configured
before Uvicorn starts:

```bash
uv run python -m energy_optimizer
```

Check that it is running:

```bash
curl http://localhost:8000/health
```

Check static OpenAPI documentation against FastAPI-generated schema:

```bash
uv run pytest tests/test_openapi.py
```

### Browser end-to-end tests

The dashboard E2E suite starts the real service entry point with an isolated
temporary data store and exercises the rendered dashboard in Chromium. The service
is configured for `Europe/Berlin` while the browser runs in `America/New_York`, so
every scenario also proves that the configured zone, not the browser's, is shown.
The service log is written to a file in the test's temporary directory, and startup
failures print it. Install the browser once in the development environment
(`scripts/bootstrap` also installs the system libraries Chromium needs), then run:

```bash
uv run playwright install chromium
uv run pytest -m e2e
```

Run the ordinary test suite without browser tests with:

```bash
uv run pytest -m "not e2e"
```

See [`docs/api.md`](docs/api.md) for the endpoint reference and request examples.
