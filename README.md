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

Configuration is expected to contain parameters such as:

- Time resolution
- Grid limits
- Electricity pricing behavior
- PV system parameters
- Battery parameters
- Electric vehicle parameters
- Heat-pump parameters
- Solver settings
- External data provider settings

Invalid configuration should result in a clear startup error.

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
request and persistence write.

Grid-flow collection retrieves the latest completed hour and persists the latest
validated import/export record through the generic JSON provider store. It uses
the same Home Assistant energy semantics as household load and can combine
multiple signed entities independently for import and export. Historic grid-flow
range queries are deferred to the unified multi-asset history feature.

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
(`total` or `total_increasing`), `unit` (`Wh`, `kWh`, or `MWh`), and `operation`
(`add` or `subtract`). The importer converts each cumulative counter's observed
increases into hourly kW-equivalent values and combines all contributions into
one logical `household_load` record.
Instantaneous power entities reported in `W` or `kW` are rejected and are never
implicitly converted to energy.

Configure the Home Assistant URL, bearer token, one or more household-load energy
entities, and request timeout in `config.yaml`. An optional
`max_data_age_seconds` setting enables a polling health check; it does not
invalidate historical data. No interpolation is performed: the latest observed
counter value is carried forward until the next observation. For
`total_increasing`, a decrease starts a new meter cycle and establishes the new
value as a zero-contribution baseline. For `total`, a decrease is accepted only
when Home Assistant's `last_reset` timestamp changes, and that reset reading
also establishes a zero-contribution baseline. A subsequent value that returns
close to the pre-reset counter is treated as recovery rather than energy. Every
other observed increase within an hour is summed, so valid energy after a reset
is retained without turning the post-reset absolute counter value into fabricated
energy. Reset transitions and recovery decisions are logged with the entity and
observed values and mark the affected hourly interval as `suspect`; suspect
intervals are exposed with a reason and source entity and block optimization.
If a `total_increasing` counter rises and the next valid observation returns close
to the value before that rise, the earlier increase is retracted as a transient
counter spike. Both observations contribute zero for the correction and the
affected interval is marked `suspect`.
`unknown` and `unavailable` history samples are
skipped without assigning energy, and the importer logs the affected entity
and time range. The next valid cumulative observation determines the delta;
the delta is assigned to that observation's hour rather than interpolated
across the skipped sample. An entity with no usable observations still rejects
the complete aggregate, as do malformed, non-finite, incompatible, or failed
entity responses. The token is a secret and must not be committed to source
control. The importer raises actionable errors for authentication failures,
missing history, malformed or non-numeric values, unsupported power units,
invalid state classes, unmarked total resets, and request failures.

Each cumulative-energy entity may also define
`maximum_interval_energy_kwh`. The default is `100` kWh. The importer compares
every normalized hourly delta after unit conversion with that physical upper
bound. A delta above the limit is replaced with zero, marked `suspect` with
reason `physical_limit_exceeded`, and logged with the entity, timestamp, observed
delta, and configured limit. Equality is accepted. The limit is per source
entity and is not applied to other mappings; override it with the meter or
inverter's credible maximum hourly energy.

For example, a household meter can be added while an EV meter is subtracted:

```yaml
home_assistant:
  base_url: http://homeassistant.local:8123
  token: replace-with-a-long-lived-access-token
  household_load_entities:
    - entity_id: sensor.household_energy
      state_class: total_increasing
      unit: kWh
      operation: add
    - entity_id: sensor.ev_energy
      state_class: total
      unit: kWh
      operation: subtract
  timeout_seconds: 10
```

The normalized aggregate is persisted and exposed under the single source
identity `home-assistant/household_load`, so storage, freshness evaluation, and
orchestration consumers receive one coherent household-load dataset.

Household load must be configured through `household_load_entities`. Each mapping
declares its Home Assistant state class, energy unit, operation, and optional
physical hourly limit, so instantaneous power sensors cannot be configured
accidentally.

Call `fetch(start_time, end_time, history_lookback_seconds)` for a requested
period. `end_time` may be omitted to fetch through the latest completed UTC
hour. The lookback asks Home Assistant for an earlier state so the importer can
carry the last known value into the first requested hour. It is applied only to
the first weekly request; later requests start exactly at the previous request's
end. Raw chunks are combined before counter normalization, so deltas, reset
boundaries, and recovery handling remain correct across chunk boundaries. A
failed chunk fails the complete fetch. During scheduled collection, the failed
refresh is logged and the last valid persisted history remains available for a
later retry. The caller owns the lookback and polling policy. Call
`is_fresh(data)` when the optional freshness threshold is configured to assess
polling health. Polling, scheduling, caching, and orchestration remain outside
this provider adapter.

### Home Assistant grid-flow importer

`HomeAssistantGridFlowImporter` composes the shared Home Assistant energy-history
retrieval and normalization functionality for grid import and export. Configure
one or more entities for each channel; every entity uses `state_class` (`total` or
`total_increasing`), an energy `unit` (`Wh`, `kWh`, or `MWh`), and an explicit
`operation` (`add` or `subtract`). Import and export are aligned to their common
available hourly start. Long grid-flow requests use the same contiguous,
seven-day maximum chunks as household load, and a failed chunk or channel never
produces a partial result.

```yaml
home_assistant:
  base_url: http://homeassistant.local:8123
  token: replace-with-a-long-lived-access-token
  grid_import_entities:
    - entity_id: sensor.grid_import_energy
      state_class: total_increasing
      unit: kWh
      operation: add
  grid_export_entities:
    - entity_id: sensor.grid_export_energy
      state_class: total_increasing
      unit: kWh
      operation: add
  timeout_seconds: 10
  max_data_age_seconds: 7200
```

The normalized result is persisted under `home-assistant/grid_flow`. See
[`docs/api.md`](docs/api.md) for the corresponding endpoint behavior.

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
temporary data store and exercises the rendered dashboard in Chromium. Install
the browser once in the development environment, then run:

```bash
uv run playwright install chromium
uv run pytest -m e2e
```

Run the ordinary test suite without browser tests with:

```bash
uv run pytest -m "not e2e"
```

See [`docs/api.md`](docs/api.md) for the endpoint reference and request examples.
