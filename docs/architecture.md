# Architecture

This document describes the target architecture at the level currently known. The
project is in its planning and initial setup phase, so package names and boundaries
may change as the energy and asset models become better understood.

## Package Structure

The planned package layout is:

```text
src/energy_optimizer/
├── api/
│   ├── app.py                # FastAPI application and startup lifecycle
│   ├── health.py             # Health endpoint
│   ├── schemas.py            # HTTP request and response models
│   └── optimization.py       # Optimization route handlers
├── config/
│   ├── loader.py             # YAML loading and error handling
│   └── models.py             # Validated runtime configuration
├── domain/
│   ├── energy.py             # Time-series values and energy balances
│   ├── assets.py             # Grid, PV, battery, EV, and heat-pump models
│   └── optimization.py       # Optimization inputs, schedules, and outcomes
├── application/
│   ├── optimize.py           # End-to-end optimization use case
│   └── orchestration.py      # Scheduled provider retrieval and plan triggers
├── providers/
│   ├── interfaces.py         # Provider contracts
│   ├── http.py               # Shared bounded JSON HTTP requests
│   ├── prices.py             # Electricity-price adapters
│   ├── forecast_solar.py     # Direct Forecast.Solar PV forecast adapter
│   ├── normalization.py      # Provider-independent timestamp/value utilities
│   ├── home_assistant.py     # Household-load Home Assistant composition
│   ├── home_assistant_energy.py # Shared Home Assistant history and energy aggregation
│   ├── home_assistant_grid_flow.py # Grid-flow Home Assistant composition
│   └── home_assistant_battery.py # Current battery state composition
├── storage.py                # Durable normalized provider-data storage
└── optimization/
    ├── model.py              # Pyomo MILP model construction
    ├── solver.py             # HiGHS integration
    └── results.py            # Solver output and diagnostics mapping
```

The implementation should add these boundaries through complete vertical slices,
rather than creating empty modules in advance. The current implementation is
smaller: `api.py` contains the FastAPI application, `config.py` contains YAML
loading and validation, and `providers/home_assistant.py` contains the first
provider adapter.

## Dependency Direction

The folders represent dependency boundaries, not independent services. Dependencies
should point inward toward stable, provider-independent concepts:

```text
api -> application -> domain
                   -> providers.interfaces
                   -> optimization
providers adapters -> providers.interfaces + domain
optimization -> domain
config -> configuration libraries only
storage -> validated provider-independent data models
```

- `api` is the outer transport layer. It validates HTTP data, invokes the
  application use case, and maps results to HTTP responses. It should not contain
  energy-balance or scheduling logic.
- `application` coordinates one optimization request: it obtains or accepts data,
  invokes normalization and optimization, and returns an application-level result.
  Its orchestration component may schedule provider retrieval independently of
  optimization availability, persist successful normalized results, and trigger a
  plan from a coherent snapshot once an optimizer is configured.
- `domain` contains provider- and framework-independent energy concepts. It should
  not import FastAPI, Pyomo, HiGHS, or vendor clients.
- `providers.interfaces` defines the data required by the application. Concrete
  provider adapters may use HTTP clients and vendor-specific formats, but they
  return normalized domain data before optimization sees it.
- `optimization` translates domain inputs into a MILP, delegates solving through
  the solver adapter, and maps solver variables back into a domain result. It
  should not depend on a particular API or data provider.
- `config` loads deployment settings such as time resolution, asset limits,
  provider selection, and solver options. The composition layer passes validated
  configuration to components instead of having configuration code construct
  business objects directly.

## Request Flow

An optimization request should follow this flow:

```text
HTTP request
  -> API validation
  -> application use case
  -> provider retrieval (when configured)
  -> domain normalization
  -> MILP construction
  -> HiGHS solve
  -> result and diagnostics mapping
  -> HTTP response
```

The application may receive prices and forecasts directly through the API or obtain
them through configured providers. In both cases, the optimization layer receives
the same normalized domain representation.

## Boundaries

### API

The API exposes optimization and health endpoints. Request and response schemas
belong here because they describe the HTTP contract, not the internal optimization
model.

### Configuration

Configuration is loaded during startup from YAML and validated into typed settings.
It should describe runtime behavior, including time resolution, asset limits,
provider selection, and solver options. Configuration loading should not construct
business objects or contain optimization logic.

### Domain

The domain represents time-series data, energy balances, asset capabilities, state
of charge, availability, schedules, and optimization outcomes. It should be
usable without starting FastAPI, reading YAML, making network requests, or loading
Pyomo.

### Providers

Provider interfaces define the data needed from prices, PV forecasts, and weather
services. Adapters own vendor-specific authentication, HTTP calls, response
formats, and provider errors. Normalization and validation happen before data is
passed to the application or optimizer.

The shared `home_assistant_energy.py` component owns Home Assistant history
requests, bearer-token authentication, energy-unit conversion, cumulative counter
validation, reset-aware delta accumulation, signed entity aggregation, hourly
normalization, and observation metadata. It follows Home Assistant's `total` and
`total_increasing` state classes, rejects instantaneous power entities, and
rejects failed or incomplete contributions. Reset transitions establish a new
zero-contribution baseline, and a value that returns close to the pre-reset
counter is treated as recovery rather than energy. Unknown and unavailable
history samples are skipped without assigning energy; the next valid counter
observation owns the resulting delta, and an entity with no usable observations
still fails. Reset and recovery intervals carry explicit suspect quality metadata
through aggregation, persistence, historic API responses, and orchestration;
required suspect data cannot trigger an optimization plan.
Each cumulative-energy mapping may additionally define a physical upper bound
for one hourly delta in kWh. The bound is applied after unit conversion and
before signed aggregation; an exceeded bound produces zero energy and suspect
quality with reason `physical_limit_exceeded`.
`HomeAssistantLoadImporter` composes this functionality into the logical
`household_load` record. `HomeAssistantGridFlowImporter` composes it independently
for import and export, allowing multiple signed entities per channel, then aligns
both channels to their common available start and returns `GridFlowData` under
the logical `grid_flow` identity. Importers expose freshness checks but do not
start polling, schedule requests, cache results, persist results, or invoke the
API layer. The orchestration layer owns those policies.

`HomeAssistantBatteryImporter` is intentionally separate from the cumulative
energy helper because battery state of charge and capabilities are instantaneous
state values. It reads configured Home Assistant state entities, optionally
selecting a named attribute, converts configured units to the battery contract,
and returns one current SOC value together with scalar capability limits and
efficiencies. The oldest mapped entity observation determines freshness, and a
failure in any required mapping prevents a partial snapshot from being returned.
Historic SOC reconstruction is not part of this provider; consumers that need
measurement history must use a dedicated history importer and alignment policy.

Normalized provider data may be persisted after validation when persistence is
configured. The storage component stores the normalized provider model or
history, not raw vendor responses or optimizer snapshots. Records are keyed by
data type, provider, and entity identifier. Atomic replacement, validation on
read, and a backup copy allow recovery from interrupted or corrupted writes.
Only data with a configured provider identity is persisted; source-less API
submissions remain request-scoped. Non-household-load data remains a readable
JSON model. Grid-flow persistence replaces the latest validated record; historic
range retention is deferred to the unified multi-asset history feature.
Household-load history uses one self-contained hourly observation
per line in an NDJSON file. API submissions may append overlapping corrections,
and reads select the latest record for each timestamp before discarding values
older than the 87,672-value ten-year limit. Scheduled collection requests only
missing completed hours and does not append overlapping records. Compaction is
triggered after the physical record count exceeds that limit plus a bounded
buffer; it atomically replaces the primary and preserves the prior valid
history as the backup. An incomplete final append line is ignored, while other
malformed records follow the normal recovery error path. Existing monolithic
household-load JSON primary and backup files are migrated to NDJSON on first
access. Scheduled and API persistence use the same append and compaction
behavior.

The direct Forecast.Solar adapter is a separate provider slice for short-term PV
forecasts. It uses the public API without Home Assistant, an account, or an API
key, and is configured with the PV location, panel declination and azimuth, and
installed peak power. Forecast.Solar returns period-energy estimates at irregular
sunrise and sunset boundaries; the adapter converts those values to hourly UTC
`PvGenerationData` rather than treating the provider response as normalized.
Forecast forecasts use `retrieved_at` and `expires_at` instead of household-load
observation metadata. Forecasts are persisted through the generic JSON storage
path and replace the previous forecast, unlike append-friendly household-load
history. The free public tier is limited to one plane, hourly resolution, and
today plus the following day; polling is configured by orchestration so the
public rate limit is respected.

The historic household-load read endpoint queries this normalized store rather
than exposing files. It accepts a timezone-aware half-open range, returns only
the retained hourly points in that range, and includes requested coverage,
available coverage, source metadata, retrieval metadata, validation status, and
polling freshness. Historical validity and polling freshness are separate: a
stale observation remains usable historical actual data and is reported as
stale, while corrupt or unrecoverable persistence is returned as a service
error. The dashboard consumes this contract and labels its values as actuals;
it does not infer provider semantics or combine forecasts and plans.

### Optimization

The optimization component builds a Pyomo mixed-integer linear program, applies
technical constraints, and invokes HiGHS through a solver adapter. Result mapping
converts solver variables into a schedule, objective values, costs, revenues,
solver status, solve time, and diagnostics.

## Design Goals

- Keep scheduling logic independent of HTTP and external providers.
- Make provider integrations replaceable.
- Keep optimization deterministic and testable with small input cases.
- Report invalid input, infeasible models, and solver failures explicitly.
- Prefer the smallest coherent module boundary needed by each vertical slice.
