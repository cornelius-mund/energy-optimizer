# Energy Optimizer

An open-source web service for optimizing the energy usage of a single-family house.

The system is designed to optimize the interaction between:

- Household electrical load
- Photovoltaic generation
- Electricity grid import and export
- Battery storage
- Electric vehicles and charging points
- A heat pump

The service calculates an energy schedule on an hourly basis. The underlying energy models and scheduling resolution may evolve as the project develops.

## Goals

The service should calculate an energy schedule that minimizes total energy cost while respecting the technical constraints of the connected assets.

The service is designed to support:

- Input and output through HTTP APIs
- YAML-based configuration
- Containerized deployment with Docker
- Mixed-integer linear programming (MILP) optimization
- HiGHS as the open-source MILP solver

## Planned Architecture

The target architecture separates HTTP transport, application orchestration, domain
models, external integrations, and optimization. The optimizer should remain
independent of specific external data providers, with providers returning normalized
internal data before it reaches the optimization model.

The detailed planned package structure, dependency direction, and request flow are
documented in [`docs/architecture.md`](docs/architecture.md). These boundaries are
targets for the implementation, not a claim that all of the modules exist today.

## Input

The API should accept energy and asset data such as:

- Household electrical load
- PV generation forecast
- Electricity import prices
- Electricity export prices
- Battery state of charge
- Electric vehicle state of charge
- Electric vehicle availability
- Heat-pump load constraints

All input data must be validated before optimization.

## Output

The optimization response should provide an hourly schedule including, where applicable:

- Household load
- PV generation
- Grid import
- Grid export
- Battery charging
- Battery discharging
- Battery state of charge
- Electric vehicle charging
- Electric vehicle state of charge
- Heat-pump consumption
- PV curtailment

The response should also include:

- Objective value
- Import cost
- Export revenue
- Solver status
- Solve time
- Diagnostics for invalid or infeasible requests

## Configuration

Runtime and system configuration is loaded from `config.yaml` at startup. Set
`ENERGY_OPTIMIZER_CONFIG` to use a different file. A complete example is
provided in [`config.example.yaml`](config.example.yaml).

Set `ENERGY_OPTIMIZER_LOG_LEVEL` to `DEBUG`, `INFO`, `WARNING`, `ERROR`,
`CRITICAL`, or `NOTSET` to override the default `INFO` minimum log level. The
service writes structured, container-friendly logs to standard output and
includes the event, component, operation, status, request ID, and relevant
time or record-count context. Every Python log uses the format
`LEVEL TIMESTAMP MESSAGE`, including Uvicorn and HTTP client records. Structured
records render their event name as the readable message and retain the original
fields after a `|` separator. For a non-health request, for example:
`INFO 2026-08-11T11:51:51+0000 request_completed | event=request_completed component=api`.
Successful health probes use the `health_check_request` event at `DEBUG`; they are
therefore hidden at the default `INFO` threshold.
When Uvicorn is started with an external logging configuration, its root output
handlers are reused and formatted rather than supplemented with another service
handler. Invalid log-level values stop startup with a configuration error.

Request completion is logged once by the service with an `X-Request-ID`
response header. Uvicorn access logging is disabled to avoid duplicate access
records. Logs never include authorization headers, Home Assistant tokens, raw
provider responses, complete request bodies, or complete energy series.
Home Assistant history requests replace HTTPX's generic completion record with
debug-level `home_assistant_history_request` events per entity and request chunk.
Each record contains the entity, chunk time range, HTTP status, and duration.
The enclosing history aggregate emits one `home_assistant_history_aggregate`
`INFO` event after all entities and chunks succeed, or one `WARNING` event when
the aggregate fails. This keeps normal logs concise while retaining per-chunk
diagnostics when debug logging is enabled.

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

Configuration is expected to contain parameters such as:

- Time resolution
- Dashboard time zone (`timezone`, an IANA name such as `Europe/Berlin`; defaults
  to `UTC`). It selects the zone in which the dashboard shows and accepts times.
  Data, API timestamps, and stored records stay in UTC. The name is matched
  exactly, so `utc` and `../etc/passwd` are rejected, and only zones whose UTC
  offset is a whole number of hours all year are accepted, because the dashboard
  requests whole UTC hours. Zones such as `Asia/Kolkata`, `Asia/Kathmandu`, and
  `Australia/Lord_Howe` fail startup with a message naming `timezone`. See
  "Dashboard time zone".
- Grid limits
- Electricity pricing behavior
- PV system parameters
- Battery parameters
- Battery and inverter efficiency can be calculated from complete Home Assistant
  history. Battery efficiency is one full-cycle round-trip value; inverter charge
  and discharge efficiencies are independent measured conversion values. Configure
  signed energy aggregations under `battery.efficiency_calculation` to account for
  DC-coupled MPPT/PV paths. An aggregation whose sum is negative in ordinary
  hours, such as the battery charge minus the PV yield, takes `part: positive`
  (see "Energy aggregations"). Fixed `battery_efficiency` takes precedence with a
  warning. Before the first complete cycle is available, live battery snapshots
  use a documented 95% default instead of failing. The dashboard's Efficiency
  tab displays the calculated components as raw ratio summaries over retained
  history; any component that cannot be calculated is shown as a marked 95%
  ratio default. Complete round-trip efficiency is always calculated from the
  three effective component ratios, including defaults. An hour that is excluded
  in any energy leg or in the state of charge (see "Which hours are imported") is
  excluded in all of them, and a full-charge cycle that contains an excluded hour
  is not used, so the ratios never mix valid and invalid data.
- Electric vehicle parameters
- Heat-pump parameters
- Solver settings
- External data provider settings

Invalid configuration should result in a clear startup error.

## Dashboard charts

The Historic actuals and Forecast tabs draw one chart per unit: power (`kW`),
prices (`EUR/kWh`), and, for actuals, battery state of charge (`%`). Each chart has
its own legend to its right, or below it on screens up to 760px wide. The legend
lists only the series that chart draws, in chart order, as `<label> (<unit>)` with a
swatch in the line's color, for example `Import price (EUR/kWh)`. A series without
data has no entry. The Efficiency and Excluded hours tabs draw no chart and show no
legend.

- **Show and hide a line:** each legend entry is a button that reflects its state in
  `aria-pressed`. Clicking it, or pressing Enter or Space while it has keyboard focus,
  hides the line and its points, or shows them again. Nothing is fetched again. A hidden
  entry stays in the legend, dimmed and struck through, so it can be shown again.
- **Axis rescaling:** the value axis is recomputed from the lines that are still shown,
  so a small series is readable once a large one is hidden. The time axis does not
  change. When every line of a chart is hidden, the chart keeps its axes at a default
  scale and its legend.
- **Hidden lines are remembered for the page session:** they stay hidden when the
  range is reloaded and when you switch tabs, and are shown again by a page reload.
  Nothing is stored in the browser.
- **Tooltips:** hovering or focusing a point shows the line it belongs to, its time in
  the configured zone, and its value with the unit, for example
  `Import price · 2026-09-30 23:00 · 0.14 EUR/kWh`. The tooltip stays inside its chart
  and never covers the legend. The point's accessible label is unchanged.

## Dashboard time zone

The dashboard shows and accepts every time in the zone set by the top-level
`timezone` setting, not in the browser's zone, so an operator in Germany reads
the end of the local day and tomorrow's day-ahead prices without translating UTC.
The static frontend cannot be templated, so it reads the zone from
`GET /api/v1/dashboard/settings` (`{"timezone": "Europe/Berlin"}`) before its first
data request, because the default range depends on it.

- **Shown in the configured zone:** the start and end controls, the default range,
  chart axis labels, tooltips and accessible point labels, the detail lists
  (coverage, retrieved, generated, published), and the Excluded hours table.
  Headings and control labels name the zone. Times use the form
  `2026-09-30 23:00`.
- **Default range:** today at 00:00 to the next local midnight. It spans 23 or 25
  hours on the days clocks change.
- **Requests stay UTC:** the controls convert local wall times to whole UTC hours
  before calling the data API, so a local `2026-09-30 00:00` to `2026-10-01 00:00`
  in `Europe/Berlin` requests `start_time=2026-09-29T22:00:00Z` and
  `end_time=2026-09-30T22:00:00Z`. The API contract, stored data, and all API
  timestamps stay UTC.
- **Daylight saving:** when clocks go back, the repeated hour appears as two
  distinguishable points and rows whose labels carry the UTC offset
  (`2026-10-25 02:00+02:00` and `2026-10-25 02:00+01:00`). A control value inside
  the repeated hour means its first occurrence. When clocks go forward, a local
  time that does not exist moves ahead by the length of the gap, so `02:30`
  becomes `03:30`.
- **Failure handling:** if the settings request fails, or the browser does not
  know the configured zone, the dashboard reports an explicit error, keeps the
  range controls disabled, and sends no data request. It never guesses a zone.

The frontend uses the browser's `Intl` support and adds no dependency. Only zones
whose UTC offset is a whole number of hours all year are supported; see the
`timezone` entry under "Configuration".

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
container replacement.
The image healthcheck calls the service health endpoint every 10 seconds. Successful
probe completions use the `health_check_request` event at `DEBUG`. Check it directly
with:

```bash
curl http://localhost:8000/health
```

Run the local container smoke test, which builds the image and waits for the
health endpoint:

```bash
./scripts/docker-smoke
```

## Data Providers

Prices and forecasts may be supplied through the HTTP API or retrieved from external data providers.

External data provider modules may support:

- Day-ahead electricity prices
- PV generation forecasts
- Weather data

External providers should be configurable and isolated from the optimization model. Provider data must be validated before use.

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

Every request carries an explicit window: `start` is the importer's hour-aligned
start time (inclusive) and `end` is its `end_time`, or 48 hours after `start` when
no end time is given (exclusive), both as epoch milliseconds. aWATTar publishes
the next day's prices at 14:00 local time, so the 48-hour look-ahead always reaches
them once they exist, and the reach no longer depends on the endpoint's default
window. The look-ahead is fixed and not configurable. A response that ends earlier,
such as the end of the current day before publication, is accepted unchanged.
The Forecast dashboard keeps the imported and exported price series separate while
retaining the same timestamps and values for this single-market-price source.

### Normalized provider-data persistence

When the optional `persistence.directory` setting is configured, the service
stores the validated normalized data model returned by a configured provider.
It does not store raw provider responses, client-only submissions, or optimizer
snapshots. It is keyed by its data type, provider, and entity identifier. A
For replace-based JSON writes, a temporary file is flushed and synced before
atomic replacement; the previous valid value is retained as a backup. If the
primary file is invalid after a restart, the backup is validated and restored.
If neither copy is valid, retrieval returns a service-unavailable error and the
invalid files are not silently accepted.

Household-load history is stored as human-readable newline-delimited JSON in
`*.ndjson`, with one hourly observation per line. Each line contains the
timestamp, load value, source identity, and retrieval metadata needed to rebuild
the validated normalized model. Overlapping API corrections are appended; when
a timestamp occurs more than once, the last line wins. Scheduled collection
does not append overlapping completed hours. Loads retain the newest 87,672
contiguous hourly values. Compaction runs when the physical record count
exceeds the retention limit plus a small buffer, removing superseded and
expired records through the same atomic replacement and backup path. An
incomplete final line from an interrupted append is ignored. Existing
household-load `*.json` and `*.json.bak` files are migrated to NDJSON on first
access without losing valid primary or backup data.

See [`docs/api.md`](docs/api.md) for the API persistence behavior and endpoint
contracts.

### Scheduled orchestration

The optional `orchestration` configuration schedules registered providers without
putting polling or scheduling behavior in provider adapters. Each source has an
independent `interval_seconds` and optional provider history lookback. With
`startup_fetch: true`, enabled sources are fetched when the service starts; failed
attempts leave the last valid persisted data in place and are reported through
service logs. Missed intervals are not replayed: the next run is scheduled from
the completed attempt. Household-load collection bootstraps with up to the API
maximum of 87,672 hourly values. If Home Assistant retains less history, the
provider starts at the earliest safely derivable hour instead of requiring the
full maximum. Later requests begin at the first hour after the final persisted
hour, so scheduled collection never re-fetches completed hours already in the
store. If no completed hour is missing, the scheduled cycle skips the provider
request and persistence write. When the service was down for longer than Home
Assistant retains its history, the returned range starts after the persisted end;
the hours in between can never be fetched again, so each of them is stored as an
excluded hour with the reason `history_unavailable` (see below) and the refresh
succeeds instead of failing on every later run.

Each cycle runs in three phases so that a Home Assistant entity is downloaded at
most once per cycle, even when several sources use it. First, every due source
declares the history it needs (entity, kind, and time range including its own
lookback) and how to build its record; a source that is not due, or has no
missing completed hour, declares nothing and causes no Home Assistant request,
and the Forecast.Solar, aWATTar, and live battery sources need none. Second, the
cycle imports each distinct entity once for the merged range of all sources that
need it, with one HTTP client and in seven-day chunks. Third, each source builds
and persists its record from those shared series, applying its own entity
settings, so one entity ID can be configured with different
`maximum_interval_energy_kwh` values in different aggregates. The imported series
live only until the cycle ends. An entity that cannot be imported at all, for
example after a request failure, fails only the sources that read it, the error
names the entity, and a failed source keeps its last valid persisted data. Bad
samples never fail a source: they exclude hours (see below).

A rejected Home Assistant token (HTTP 401 or 403) is not a failure of one entity,
because it would reject every further request too. The import therefore ends at
the first rejection: no further request is sent, every entity that was not
imported fails with the same authentication error naming that entity, and the
entities imported before the rejection stay available. The import logs one
`home_assistant_history_authentication_failed` warning with the rejected entity
and the number of entities that were not requested, instead of one warning per
entity. The battery source's live state request is a separate request and still
makes its own attempt.

Grid-flow collection bootstraps and refreshes like household load: the first run
requests up to the 87,672-hour maximum (or all history Home Assistant retains),
and every later run requests only the completed hours after the retained history.
Each save is merged into one contiguous hourly history in the generic JSON
provider store, with incoming values replacing overlapping hours and the oldest
hours dropped beyond the retention limit. An excluded hour keeps its place without
values, so it never leaves a gap. A fetched range that starts after the retained
history ends leaves hours that Home Assistant no longer holds: each is stored as
an excluded hour with the reason `history_unavailable`, so the retained history
stays contiguous and later runs continue after the new end. Any other range that
would leave a gap still fails the run and keeps the last valid history for the
next attempt. It uses the same Home Assistant energy semantics as household load and can combine multiple
signed entities independently for import and export. The retained history is
served by the historic multi-asset dashboard read API (see
[`docs/api.md`](docs/api.md)). The bootstrap only happens when no grid-flow
record exists: a deployment that previously stored just the latest hour continues
from that hour. To back-fill the history Home Assistant retains, stop the service,
delete the `grid-flow-*` files from the persistence directory, and start it again.

Electricity-price collection additionally records every retrieved price hour in a
separate `electricity-price-history` record, because the forecast record used by
the Forecast tab is replaced by each run. Only hours that have elapsed are exposed
as historic prices. A failure to write the history is logged and never blocks the
price forecast refresh.

Scheduled collection requires `persistence.directory`, so normalized data survives
application restarts. Automatic plan generation is disabled until an optimization
plan generator is configured. Once enabled, a refresh triggers planning only when
all required sources have current, valid data; the plan generator receives one
coherent `ProviderDataSnapshot`. Concurrent orchestration cycles are skipped to
avoid duplicate plans.

### Forecast.Solar PV forecast importer

`ForecastSolarImporter` retrieves PV production forecasts directly from the free
public Forecast.Solar API. Home Assistant, an account, and an API key are not
required. Configure the installation location, panel declination and azimuth,
and installed peak power. Forecast.Solar uses azimuth `0` for south, `-90` for
east, and `90` for west.

The provider converts Forecast.Solar's irregular sunrise and sunset
`watt_hours_period` response into hourly UTC `generation_kw` values. It validates
timestamps, time-zone metadata, coverage, units, and finite non-negative values,
then returns `PvGenerationData` with `retrieved_at` and `expires_at` metadata.
Forecasts are persisted through the generic replace-based JSON provider store.
The public tier is free for private use, supports one plane, hourly resolution,
and today plus the following day. Configure polling conservatively to respect
the public rate limit of 12 requests per IP per rolling hour.

Example:

```yaml
forecast_solar:
  latitude: 52.52
  longitude: 13.41
  declination_degrees: 35
  azimuth_degrees: 0
  peak_power_kw: 8
  timeout_seconds: 10
  max_data_age_seconds: 7200
```

### Home Assistant household-load importer

`HomeAssistantLoadImporter` is a reusable provider adapter for Home Assistant's
REST history API. It retrieves a requested half-open hourly period in contiguous
requests of no more than seven days. Ranges longer than one week are fetched
sequentially, and the available retained subset is used when the requested start
predates Home Assistant's history. The importer returns
provider-independent household-load data with `load_kw`, `unit: "kW"`, source
metadata, retrieval time, and the latest source observation time. Household-load
sources must be energy entities configured with Home Assistant's `state_class`
(`total` or `total_increasing`) and `unit` (`Wh`, `kWh`, or `MWh`), grouped into
an energy aggregation by `operation` (`add` or `subtract`). The importer converts each cumulative counter's observed
increases into hourly kW-equivalent values and combines all contributions into
one logical `household_load` record.
Instantaneous power entities reported in `W` or `kW` are rejected and are never
implicitly converted to energy.

Configure the Home Assistant URL, bearer token, the household-load energy
aggregation, and request timeout in `config.yaml`. An optional
`max_data_age_seconds` setting enables a polling health check; it does not
invalidate historical data. The token is a secret and must not be committed to
source control.

#### Which hours are imported

An hour is imported only if every data point that contributes to it is valid.
Every other hour is **excluded**: it has no value (`null` in the API), it is left
out of every chart and calculation, and it is listed with each cause and its exact
data points on the **Excluded hours** dashboard tab and through
`GET /api/v1/dashboard/excluded-hours`. Nothing is repaired, tolerated, or
estimated, so there is no reset, spike, recovery, or jitter handling. One bad
sample never fails an import and never blocks a later one: the persisted history
advances past excluded hours, which keep their place without a value.

No interpolation is performed: the latest observed counter value is carried
forward until the next observation, and Home Assistant records only state changes,
so a counter keeps its value between two observations. The observations of an
entity, in time order, form steps. The energy of a step belongs to the hour of its
later observation, and an observation exactly on an hour boundary belongs to the
hour it closes. A sensor that publishes on the hour and is unavailable at
`01:00:00` therefore has no closing reading for the hour before it.

| Reason | Cause | Excluded hours |
|---|---|---|
| `unavailable` | The state is `unknown` or `unavailable` | Every hour from the first invalid sample until the first valid observation after it, including the hour in which the entity returns; a trailing outage runs to the end of the imported period |
| `non_numeric` | The state is not a number | As `unavailable` |
| `not_finite` | The state is `nan` or infinite | As `unavailable` |
| `negative_value` | A counter reports a negative value | As `unavailable` |
| `invalid_attribute` | `state_class` or `last_reset` is not usable | As `unavailable` |
| `unit_mismatch`, `unit_missing`, `state_class_mismatch` | A sample reports another unit, no unit, or another `state_class` than configured | As `unavailable` |
| `counter_decrease` | A counter decreases, however little (a 1 Wh step included) | The hours of both observations of the step |
| `step_after_decrease` | The step directly after a decrease: a reset cannot be told apart from a glitch, so a return from zero is not trusted | The hours of both observations of the step |
| `last_reset_changed` | The `last_reset` marker changes between two observations | The hours of both observations of the step |
| `step_above_maximum` | A step exceeds `maximum_interval_energy_kwh` | The hour of the later observation |
| `hour_above_maximum` | The steps of one hour add up to more than `maximum_interval_energy_kwh` | That hour |
| `soc_out_of_range` | A state of charge is outside 0 to 100 percent | As `unavailable`, for the state-of-charge entity |
| `combined_negative`, `combined_not_finite` | The `add` and `subtract` terms of an hour give a non-finite value, or a negative value unless the aggregation takes the `positive` part | That hour |
| `flagged_by_earlier_version` | An earlier version had flagged the hour `suspect` | That hour |
| `history_unavailable` | Home Assistant no longer holds the hour: the service was down for longer than the recorder retains history, so the fetched range starts after the persisted end | Every hour between the persisted end and the start of the fetched range, in household load, grid flow, and battery efficiency alike |

A counter that drops to zero and returns therefore excludes the hours of the drop
and of the step back up, and the hours before and after keep their true energy.
The set of reason codes is closed; every excluded hour lists at least one.

A `history_unavailable` hour has no entity and no data point, because nothing was
recorded for it; its message names the whole missing range. Retrying can never
fill such a gap, so it is excluded instead of failing the run, and the persisted
hours before it and the fetched hours after it stay unchanged. A battery-efficiency
gap also has no state of charge: the boundaries of the missing hours, including
the two that touch a valid hour, are unknown, so no full-charge cycle spans the
gap. Each gap is reported by one `provider_history_unavailable` warning per source
and refresh with the number of missing hours and their first and last hour.

`maximum_interval_energy_kwh` is a per-entity setting (default `100` kWh) with the
unit-converted maximum energy of one entity in one hour and in one counter step.
Equality is accepted. The limit applies to the source entity only; override it
with the meter or inverter's credible maximum hourly energy. The former
`decrease_tolerance_kwh` setting no longer exists, and a configuration that still
contains it is rejected at startup because unknown keys are not accepted; remove
it from any existing `config.yaml`.

A combined hour of an energy aggregation is excluded when any contributing
entity is excluded for it, and also when the signed terms make it not finite or,
with the default `part: net`, negative. Grid import and export are excluded
together, and the state-of-charge and energy legs of the battery efficiency
history are excluded together (see below). Only the entity that caused an
exclusion is named. `part: positive` changes only the meaning of a negative sum
(see "Energy aggregations").

Every cause names the entity, a reason code, a message, and its data points. A
data point holds the time Home Assistant recorded it, the raw state exactly as
reported, and for a counter step the previous and current observation, the step's
energy, and the maximum. At most 50 data points are kept per entity and hour; the
cause keeps the full count. Excluded hours are persisted with the history, next to
the hour they explain, and survive restarts. A refresh that excluded hours still
completes as `success` and does not block optimization; it logs one
`provider_hours_excluded` warning per source and refresh with the number of
excluded hours by reason.

Some problems make an entity unusable as a whole and remain errors that fail the
sources that read it, with a message naming the entity: authentication or request
failures, malformed responses, an entity without any history, an instantaneous
power unit (`W` or `kW`) configured as a cumulative energy entity, duplicate
timestamps, and a period without a complete hour. Data persisted by an earlier
version keeps its valid hours unchanged; hours that were flagged `suspect` become
excluded hours with the reason `flagged_by_earlier_version`.

For example, a household meter can be added while an EV meter is subtracted:

```yaml
home_assistant:
  base_url: http://homeassistant.local:8123
  token: replace-with-a-long-lived-access-token
  household_load:
    terms:
      - operation: add
        entities:
          - entity_id: sensor.household_energy
            state_class: total_increasing
            unit: kWh
      - operation: subtract
        entities:
          - entity_id: sensor.ev_energy
            state_class: total
            unit: kWh
  timeout_seconds: 10
```

The normalized aggregate is persisted and exposed under the single source
identity `home-assistant/household_load`, so storage, freshness evaluation, and
orchestration consumers receive one coherent household-load dataset.

Household load must be configured through `household_load`, an energy aggregation
(see below). Each entity declares its Home Assistant state class, energy unit, and
optional physical hourly limit, so instantaneous power sensors cannot be configured
accidentally. An entity ID may also be listed in the grid-flow aggregations when
the same physical meter provides both household-load and grid-flow measurements.
An entity ID must not be repeated within one aggregation.

#### Energy aggregations

Household load, grid import, grid export, and both sides (`energy_in` and
`energy_out`) of each battery efficiency leg are configured with the same
aggregation object:

```yaml
household_load:
  part: net            # optional; "net" (default) or "positive"
  terms:
    - operation: add
      entities:
        - {entity_id: sensor.household_energy, state_class: total_increasing, unit: kWh}
    - operation: subtract
      entities:
        - {entity_id: sensor.ev_energy, state_class: total, unit: kWh}
```

The hourly value is the energy of all `add` entities minus the energy of all
`subtract` entities. There is at most one term per operation, every term has at
least one entity, and an entity ID may appear only once within one aggregation.
The same entity may be used in several aggregations; it is imported once and
normalized with each aggregation's own entity settings.

`part` says what a negative hourly sum means:

- `net` (default): a negative sum is invalid data. The hour is excluded with the
  reason `combined_negative` and listed as an excluded hour.
- `positive`: only the positive part of the sum is wanted, so a negative sum is a
  legitimate zero. The hour is imported as `0` and is not excluded. It is counted
  in `clamped_hour_count` in the `home_assistant_history_aggregate` log line of
  the aggregation, together with `part` and `excluded_hour_count`.

`part: positive` is for sums that are negative in ordinary operation. On a
DC-coupled system PV reaches the battery directly, so the energy the inverter
charged from AC is the battery's charging energy minus the PV yield. In a
PV-surplus hour that difference is negative and means "no charging from AC".
`part: positive` does not hide bad data: a non-finite sum and every entity-level
exclusion still exclude the hour.

The former `household_load_entities`, `grid_import_entities`, and
`grid_export_entities` lists, and a per-entity `operation`, are no longer
accepted. Wrap the entities in `terms` and rename the keys.

Call `fetch(start_time, end_time, history_lookback_seconds)` for a requested
period. `end_time` may be omitted to fetch through the latest completed UTC
hour. The lookback asks Home Assistant for an earlier state so the importer can
carry the last known value into the first requested hour. It is applied only to
the first weekly request; later requests start exactly at the previous request's
end. Raw chunks are combined before counter normalization, so steps and excluded
hours remain correct across chunk boundaries. A
failed chunk fails the complete fetch. During scheduled collection, the failed
refresh is logged and the last valid persisted history remains available for a
later retry. The caller owns the lookback and polling policy. Call
`is_fresh(data)` when the optional freshness threshold is configured to assess
polling health. Polling, scheduling, caching, and orchestration remain outside
this provider adapter.

### Home Assistant grid-flow importer

`HomeAssistantGridFlowImporter` composes the shared Home Assistant energy-history
retrieval and normalization functionality for grid import and export. Configure
`grid_import` and `grid_export` together, each as an energy aggregation (see
"Energy aggregations") of one or more entities; every entity uses `state_class`
(`total` or `total_increasing`) and an energy `unit` (`Wh`, `kWh`, or `MWh`), and
the `operation` (`add` or `subtract`) of its term. Import and export are aligned
to their common available hourly start, and an hour excluded in either channel is
excluded in both.
Long grid-flow requests use the same contiguous, seven-day maximum chunks as
household load, and a failed chunk or channel never produces a partial result.

```yaml
home_assistant:
  base_url: http://homeassistant.local:8123
  token: replace-with-a-long-lived-access-token
  grid_import:
    terms:
      - operation: add
        entities:
          - entity_id: sensor.grid_import_energy
            state_class: total_increasing
            unit: kWh
  grid_export:
    terms:
      - operation: add
        entities:
          - entity_id: sensor.grid_export_energy
            state_class: total_increasing
            unit: kWh
  timeout_seconds: 10
  max_data_age_seconds: 7200
```

The normalized result is persisted under `home-assistant/grid_flow` as a retained
hourly history. See [`docs/api.md`](docs/api.md) for the corresponding endpoint
behavior.

The household-load and grid-flow categories are normalized independently, so a
physical meter can be reused across both categories without double-counting it
within either aggregate.

## Development

The project is currently in the planning and initial setup phase.

Development should follow the ordered product backlog. Each feature should deliver direct user value and include its required implementation, tests, documentation, configuration, and deployment work.

### Test-Driven Development

Tests should follow a test-driven development pattern:

1. Write a test that expresses the expected behavior and fails for the current implementation.
2. Implement the smallest change that makes the test pass.
3. Run the relevant test suite and verify that all tests pass.
4. Refactor the implementation while keeping the tests passing.

See [`AGENTS.md`](AGENTS.md) for backlog, issue, and engineering process guidelines.

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
and locked in `uv.lock`, so `scripts/bootstrap` (or `uv sync --locked --extra
dev`) installs the same versions locally and in CI. The packages wrapping a
native tool provide the upstream release binary and verify its SHA-256
checksum. The first three components of a wrapper's version are the upstream
version, for example `actionlint-py` 1.7.12.25 provides `actionlint` 1.7.12.
`actionlint-py` and `shfmt-py` have no prebuilt wheels: installing them builds a source package that
downloads the binary from GitHub, so the first install needs access to
`github.com`. `scripts/preflight` names any linter that is missing and says how
to install it, as a warning normally and as a failure with `--strict`. To
update a tool, change its pin in `pyproject.toml`, run `uv lock`, and run
`scripts/verify`.

## Running the Service

Install [`uv`](https://docs.astral.sh/uv/getting-started/installation/), then
create/update the project virtual environment and lockfile dependencies:

```bash
uv sync --extra dev
```

`uv sync` reads dependencies from `pyproject.toml`, resolves them into `uv.lock`,
and installs project plus development dependencies into `.venv`. Use `uv run`
to execute commands inside this environment. CI uses `uv sync --locked --extra
dev` so it fails when lockfile no longer matches project metadata.

Create a runtime configuration before starting the service:

```bash
cp config.example.yaml config.yaml
```

The service validates the YAML structure and types during startup. Missing,
malformed, or invalid configuration stops startup with an error identifying the
file and invalid fields.

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
failures print it. Install
the browser once in the development environment (`scripts/bootstrap` also
installs the system libraries Chromium needs), then run:

```bash
uv run playwright install chromium
uv run pytest -m e2e
```

Run the ordinary test suite without browser tests with:

```bash
uv run pytest -m "not e2e"
```

See [`docs/api.md`](docs/api.md) for the endpoint reference and request examples.
