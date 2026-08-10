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
The image healthcheck calls the service health endpoint. Check it directly with:

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
snapshots. The normalized model is stored directly as JSON and is keyed by its
data type, provider, and entity identifier. A temporary file is flushed and
synced before atomic replacement; the previous valid value is retained as a
backup. If the primary file is invalid after a restart, the backup is validated
and restored. If neither copy is valid, retrieval returns a service-unavailable
error and the invalid files are not silently accepted.

`POST /api/v1/household-load` persists data only when its source matches the
configured Home Assistant household-load provider. Source-less submissions and
other providers are validated and returned but are not persisted. `GET
/api/v1/household-load` retrieves the latest persisted normalized provider data.
Battery and electric-vehicle persistence will use the same store when their
normalized provider contracts are available; optimizer-owned state transitions
remain outside this persistence boundary.

### Home Assistant household-load importer

`HomeAssistantLoadImporter` is a reusable provider adapter for Home Assistant's
REST history API. It retrieves one requested half-open hourly period and returns
provider-independent household-load data with `load_kw`, `unit: "kW"`, source
metadata, retrieval time, and the latest source observation time. The importer
accepts Home Assistant values reported in `W` or `kW`; the target unit is always
the contract-defined `kW` and is not configurable.

Configure the Home Assistant URL, bearer token, household-load entity ID, and
request timeout in `config.yaml`. An optional `max_data_age_seconds` setting
enables a polling health check; it does not invalidate historical data. The
token is a secret and must not be committed to source control. The importer
raises an actionable error for authentication failures, missing or unavailable
entities, malformed or non-numeric values, unsupported units, and request
failures.

Call `fetch(start_time, end_time, history_lookback_seconds)` for a requested
period. `end_time` may be omitted to fetch through the latest completed UTC
hour. The lookback asks Home Assistant for an earlier state so the importer can
carry the last known value into the first requested hour. The caller owns the
lookback and polling policy. Call `is_fresh(data)` when the optional freshness
threshold is configured to assess polling health. Polling, scheduling, caching,
and orchestration remain outside this provider adapter.

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

Start the service with Uvicorn:

```bash
uv run uvicorn energy_optimizer.api:app --host 0.0.0.0 --port 8000
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

### Household-load API

`POST /api/v1/household-load` validates a normalized hourly household-load
series. The versioned request contains:

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
series for one to 87,672 hours, `unit: "kW"`, and optional source metadata.

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
    "entity_id": "sensor.grid_import"
  }
}
```

The response echoes the normalized import and export series with
`status: "validated"`. Missing fields, unknown fields, unsupported versions
or units, naive timestamps, mismatched series lengths, invalid values, and
series longer than ten years (87,672 hourly values) return HTTP 422 with
field-level validation details.

The health endpoint returns the service status and version, for example:

```json
{"status":"ok","version":"0.1.0"}
```
