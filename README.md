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
fields after a `|` separator, for example:
`INFO 2026-08-11T11:51:51+0000 request_completed | event=request_completed component=api`.
When Uvicorn is started with an external logging configuration, its root output
handlers are reused and formatted rather than supplemented with another service
handler. Invalid log-level values stop startup with a configuration error.

Request completion is logged once by the service with an `X-Request-ID`
response header. Uvicorn access logging is disabled to avoid duplicate access
records. Logs never include authorization headers, Home Assistant tokens, raw
provider responses, complete request bodies, or complete energy series.
Home Assistant history requests replace HTTPX's generic completion record with
one `home_assistant_history_request` event per entity and request chunk. Each
record contains the entity, chunk time range, HTTP status, and duration.

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
The image healthcheck calls the service health endpoint once per minute. Check
it directly with:

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

`POST /api/v1/household-load` persists data only when its source matches the
configured Home Assistant household-load provider. Source-less submissions and
other providers are validated and returned but are not persisted. Matching
household-load submissions merge by hourly timestamp, with incoming values
overwriting duplicates and the oldest values removed beyond 87,672 hours. `GET
/api/v1/household-load` retrieves the latest persisted normalized provider data.
Battery and electric-vehicle persistence will use the same store when their
normalized provider contracts are available; optimizer-owned state transitions
remain outside this persistence boundary.

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

The legacy `household_load_entity_id` setting is still accepted as an explicit
migration path. It is treated as one `kWh` entity with `state_class:
total_increasing` and an `add` operation, and is persisted under the new logical
`household_load` identity. Update the configuration to
`household_load_entities` so the entity's Home Assistant state class is visible
and power sensors cannot be configured accidentally.

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

The normalized result is persisted under `home-assistant/grid_flow`.
`POST /api/v1/grid-flow` validates and persists data when that source is
configured, while `GET /api/v1/grid-flow` returns the latest persisted record.
The endpoint stores only the latest record; historic range retention is deferred
to the unified historic multi-asset API.

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

### Hourly optimization API

`POST /optimize` validates an hourly request. The request must contain a
timezone-aware `start_time`, `interval_minutes: 60`, and equally sized series
of `load_kw`, `pv_generation_kw`, `import_price_eur_per_kwh`, and
`export_price_eur_per_kwh`. Series contain one value per hour, from one to 168
hours, and values are expressed in kW or EUR/kWh as named by their fields.

Requests that pass validation return a `validated` response containing the
horizon metadata. Invalid JSON or values return HTTP 422 with field-level
validation details. The endpoint is the API boundary for the optimizer; solver
schedule results will be added by a later vertical slice.

### Electricity-price API

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

### Battery API

`POST /api/v1/battery` validates normalized hourly battery state and capability
data. The versioned request contains timezone-aware `start_time`,
`interval_minutes: 60`, one to 87,672 `state_of_charge_kwh` values, capacity and
SOC bounds in kWh, initial SOC, charge and discharge power limits in kW,
charge and discharge efficiencies from greater than zero through one,
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
  "charge_efficiency": 0.95,
  "discharge_efficiency": 0.95,
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

### Home Assistant battery import

The Home Assistant battery provider retrieves a current snapshot from the REST
state endpoint for each configured mapping. It accepts values from either the
entity state or a named entity attribute and normalizes them to the battery API
units. State-of-charge and SOC limits accept `%`, `Wh`, or `kWh`; capacity accepts
`Wh` or `kWh`; power limits accept `W` or `kW`; and efficiencies accept `%` or a
unitless `ratio`.

Configure the provider under `home_assistant.battery` and add the `battery`
source to `orchestration.sources`. The snapshot contains one current
`state_of_charge_kwh` value, uses that value as `initial_soc_kwh`, and records
the oldest Home Assistant observation timestamp across all mappings for
freshness checks. All mappings are fetched before normalization, so an
unavailable or invalid entity prevents a partial battery snapshot from being
persisted. HTTP authentication failures, missing entities, malformed values,
invalid timestamps, inconsistent SOC limits, and stale data are reported as
provider errors or stale orchestration runs.

The importer does not reconstruct historic SOC. Historic battery data and
measurement alignment for installation efficiency calculations are separate
follow-up behavior.

### Household-load API

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

### Historic household-load dashboard

Open `/dashboard/` to inspect imported household-load actuals. The dashboard
supports a UTC day or inclusive date range and uses the read-only endpoint
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

The Docker image sets `ENERGY_OPTIMIZER_FRONTEND_DIRECTORY=/app/frontend` so
the dashboard remains available after the Python application is installed into
the image. Source-tree deployments may omit this setting when the repository's
`frontend/` directory is present.

### PV-generation API

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

### Grid-flow API

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

The health endpoint returns the service status and version, for example:

```json
{"status":"ok","version":"0.1.0"}
```
