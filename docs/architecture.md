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
│   └── optimize.py           # End-to-end optimization use case
├── providers/
│   ├── interfaces.py         # Provider contracts
│   ├── prices.py             # Electricity-price adapters
│   ├── forecasts.py          # PV and weather adapters
│   └── normalization.py      # External data to domain data conversion
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
```

- `api` is the outer transport layer. It validates HTTP data, invokes the
  application use case, and maps results to HTTP responses. It should not contain
  energy-balance or scheduling logic.
- `application` coordinates one optimization request: it obtains or accepts data,
  invokes normalization and optimization, and returns an application-level result.
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

The Home Assistant household-load adapter is the first concrete provider slice.
`HomeAssistantLoadImporter` owns the Home Assistant REST request, bearer-token
authentication, history mapping, W-to-kW conversion, hourly normalization, and
observation metadata. It returns `HouseholdLoadData` and exposes an optional
freshness health check, but does not start polling, schedule requests, cache
results, persist data, or invoke the API layer. A later orchestration layer
selects the requested period, history lookback, and polling cadence. Historical
retention is independent of polling freshness.

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
