# Energy Optimizer

Heat-pump electrical scheduling is available through `POST /optimize`: submit
hourly load/prices/PV and optional heat-pump constraints to receive a cost-minimal
schedule. Configure Home Assistant heat-pump power and remaining-energy mappings
for automatic polling and a dashboard forecast baseline. See the
[heat-pump API and importer reference](docs/api.md#heat-pump-electrical-load-api)
and `config.example.yaml`. The model handles electrical flexibility without a
physical thermal model.

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
optimization with HiGHS as the open-source MILP solver.

## Planned Architecture

The target architecture separates HTTP transport, application orchestration,
domain models, external integrations, and optimization. The optimizer should
remain independent of specific external data providers, with providers returning
normalized internal data before it reaches the optimization model. The planned
package structure, dependency direction, and request flow are documented in
[`docs/architecture.md`](docs/architecture.md). These boundaries are targets for
the implementation, not a claim that all of the modules exist today.

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

Automatic plan generation is disabled until an optimization plan generator is
configured. Once enabled, a refresh triggers planning only when all required
sources have current, valid data; the plan generator receives one coherent
`ProviderDataSnapshot`. Concurrent orchestration cycles are skipped to avoid
duplicate plans.

### Forecast.Solar PV forecast importer

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
(`is_fresh(data)`); it does not invalidate historical data. The token is a secret
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
