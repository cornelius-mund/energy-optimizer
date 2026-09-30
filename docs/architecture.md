# Architecture

This document describes the implemented architecture and the boundaries used for
maintainable vertical slices. Package names may evolve as the energy and asset
models become better understood, but each module below has a current runtime role.

## Package Structure

The planned package layout is:

```text
src/energy_optimizer/
├── api/
│   ├── __init__.py           # Stable public API compatibility exports
│   ├── app.py                # FastAPI application and route handlers
│   ├── lifecycle.py          # Startup and shutdown orchestration
│   ├── middleware.py         # Request IDs and outcome logging
│   ├── historic.py           # Historic multi-asset actual loaders and availability
│   ├── excluded.py           # Excluded-hours read model for the dashboard tab
│   ├── persistence.py        # Shared provider persistence mapping
│   ├── schemas.py            # HTTP request and response models
│   ├── series.py             # Dashboard series alignment
│   ├── validation.py          # Shared HTTP validation helpers
│   └── routers/               # Core, provider, and dashboard route groups
├── config.py                 # YAML loading and validated runtime settings
├── orchestration.py          # Scheduled provider retrieval and plan triggers
├── providers/
│   ├── interfaces.py         # Provider contracts
│   ├── http.py               # Shared bounded JSON HTTP requests
│   ├── prices.py             # Electricity-price adapters
│   ├── awattar.py            # German aWATTar EPEX Spot price adapter
│   ├── forecast_solar.py     # Direct Forecast.Solar PV forecast adapter
│   ├── normalization.py      # Provider-independent timestamp/value utilities
│   ├── home_assistant.py     # Household-load Home Assistant composition
│   ├── home_assistant_history.py # Shared once-per-cycle Home Assistant history import
│   ├── home_assistant_energy.py # Pure energy normalization and signed aggregation
│   ├── home_assistant_grid_flow.py # Grid-flow Home Assistant composition
│   ├── home_assistant_battery_efficiency.py # Measured battery-efficiency composition
│   └── home_assistant_battery.py # Current battery state composition
├── storage.py                # Generic durable normalized provider-data storage
├── exclusions.py             # Excluded hours: reason codes, causes, data points
├── legacy_quality.py         # Converts suspect quality of earlier versions on read
├── history_merge.py          # Merge and retention rules for grid-flow and price history
├── household_load_store.py   # Append-friendly household-load NDJSON storage
├── storage_errors.py          # Storage error contract
└── optimization/
    ├── model.py              # Pyomo MILP model construction
    ├── solver.py             # HiGHS integration
    └── results.py            # Solver output and diagnostics mapping
```

The API package keeps schemas, lifecycle, middleware, and reusable mapping helpers
separate from route handlers. Generic storage delegates household-load history to
its own NDJSON module, while provider registration is composed through ordered
factories in `orchestration.py`.

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

Home Assistant history is imported by one shared layer,
`home_assistant_history.py`, so that an entity is downloaded once per
orchestration cycle however many records use it. A cycle has three phases. In the
plan phase every due source declares the history it needs as `HistoryNeed`
values (entity ID, kind, and time range including the source's own lookback) and
how to build its final record from that history; a source that is not due, or has
no missing completed hour, declares nothing and causes no Home Assistant
request. In the import phase the layer merges the needs per entity ID into one
range (earliest start, latest end), fetches each entity exactly once with one
HTTP client, and cleans it. In the build phase every source builds and persists
its record from the shared series, in registration order. Sources without Home
Assistant history needs (Forecast.Solar, aWATTar, and the live battery state)
fetch inside their build step. An entity that is both a counter and plain state
history in two plans, or a read of history that no plan declared, raises an
explicit `HistoryPlanError`.

The layer supports cumulative-energy counters and plain state history (the
measured-efficiency state of charge). It only performs cleaning that does not
depend on the consuming aggregate: timestamp parsing, classifying every state as
a finite number or as invalid (`unavailable`, `non_numeric`, `not_finite`, or
`negative_value` for a counter), and carrying `unit`, `state_class`, and
`last_reset` from the previous sample where Home Assistant omits them. It never
drops or repairs a sample: an invalid sample stays in the series with its raw
state and its reason, so that a consumer can exclude the hours it touches and show
the exact data point. It keeps only the cleaned series and only until
the cycle ends: raw responses are dropped as soon as they are cleaned, and there
is no cross-cycle cache (that belongs to a separate provider-caching feature).
Bearer-token authentication and the request timeout come from the Home
Assistant configuration. Requests covering more than seven days are split into
contiguous half-open chunks; the source's lookback is part of its declared range
and so moves only the start of the first chunk. Records from all chunks are
combined before normalization so counter steps and excluded hours remain
continuous at chunk boundaries. A failure while importing one entity, which
includes an HTTP error, a transport failure, or a malformed response, is recorded
against that entity and re-raised, with the entity named in the message, to every
source that reads it and to no other source; every other entity is still
imported. A bad sample is not such a failure: it never fails an entity or a
source.

Each source reads its own window of the shared series exactly as Home Assistant
would answer an independent request for it: the state in force at the window's
start, stamped with that start (the time Home Assistant recorded it is kept as
`observed_at`, so data points show the true time), followed by every change up to
its end. An invalid state in force is carried like any other, so a valid state is
never carried across an outage. Consequently a source's
result equals what a separate fetch of its window would have produced, and a
property-based test checks this equivalence.

The `home_assistant_energy.py` component is the pure normalization step that runs
on such a window with the consuming aggregate's own entity settings, so one
entity ID may be configured differently in different aggregates: it is fetched
once and normalized separately for each. Normalizations with identical settings
and windows within a cycle are computed once. It owns energy-unit conversion,
cumulative counter steps, signed entity aggregation, hourly normalization, and
observation metadata. It follows Home Assistant's `total` and `total_increasing`
state classes and rejects instantaneous power entities.

**The hour rule.** An hour is imported only if every data point that contributes to
it is valid; every other hour is excluded. Nothing is repaired, tolerated, or
estimated: the former reset, spike, recovery, jitter-tolerance, and negative-hour
clamping logic, the `suspect` quality, and the `decrease_tolerance_kwh` and
`allow_negative` settings no longer exist. The observations of an entity form
steps between consecutive valid observations. The energy of a step belongs to the
hour of its later observation, and an observation exactly on an hour boundary
belongs to the hour it closes. Home Assistant records only state changes, so a
counter keeps its value between two observations, which decides which hours a
cause excludes:

- Invalid samples exclude every hour from the first invalid sample until the
  first valid observation after them, because the invalid state stays in force
  until then. The hour in which the entity returns is excluded too, since the
  energy of the outage cannot be attributed. A trailing outage runs to the end.
- A decrease of any size, the step directly after a decrease (a reset cannot be
  told apart from a glitch, so a return from zero is not trusted), and a changed
  `last_reset` exclude the hours of both observations of the step. This also
  catches a spike that was recorded just before it fell back.
- A step above the mapping's `maximum_interval_energy_kwh` (after unit
  conversion, equality accepted) excludes the hour of its later observation, and
  an hour whose steps add up to more than the maximum is excluded.
- A sample with another unit, no unit, or another `state_class` than configured
  is an invalid sample.

An excluded hour has no value and carries one or more `ExclusionCause` records
(`exclusions.py`): a reason code from a closed set, a message, the entity, and the
exact data points (raw state, recorded time, and for a step the previous and
current observation, the step's energy, and the maximum). At most 50 data points
are kept per entity and hour, with the full count. Some conditions make the
entity unusable as a whole and remain hard errors that fail the sources that read
it: no history at all, an instantaneous power unit, duplicate timestamps, and a
period without a complete hour.

Signed aggregation excludes a combined hour when any contributing entity is
excluded for it (the cause of the entity is kept; no partial sum is formed), and
also when the terms make it not finite (`combined_not_finite`) or, by default,
negative (`combined_negative`), listing the signed energy of every entity. Every
aggregation is configured by one type, `EnergyAggregateConfiguration`: a list of
terms, one per operation (`add` or `subtract`), each holding the entities that
share that sign, and a `part`. With `part: net` a negative sum excludes the hour.
With `part: positive` the operator declares that only the positive part of the
sum is wanted, so a negative sum becomes exactly `0` and the hour stays valid.
That is not a repair: it is the explicit meaning of the configured formula, it
never applies to a non-finite sum or an excluded entity hour, and it leaves no
exclusion record, so the count is kept in the `clamped_hour_count` field of
`HomeAssistantEnergySeries` and logged with `part` in the aggregate summary. Household load, grid import and export, and both sides of every
efficiency leg use the same aggregation type. Individual chunk request outcomes are debug-level
diagnostics. The import emits one structured summary at info level with its
entity, failed-entity, request, and invalid-sample counts, and a warning for
every failed entity, except that a rejected token (HTTP 401 or 403) ends the
import and is logged once as `home_assistant_history_authentication_failed`.
Each aggregate build emits one structured summary at info level, with its
excluded-hour and clamped-hour counts, or one failure summary at warning level
after the complete entity set has been processed. Orchestration emits one
`provider_hours_excluded` warning per source and refresh with the number of newly
excluded hours by reason.

**Persistence of excluded hours.** The persisted series of household load, grid
flow, and the efficiency history hold `null` for an excluded hour, so the hours
stay contiguous and the incremental start (the first hour after the persisted
history) advances past it. A permanently bad sample can therefore never block
later refreshes. The exclusion records are stored with the history in the same
atomic write, so a value can never lose its explanation: the household-load NDJSON
record of an excluded hour has `"load_kw": null` and an embedded `exclusion`;
grid-flow and efficiency-history JSON files hold a sparse `exclusions` list. A
merge replaces an hour completely, value and exclusion. Data persisted by earlier
versions is converted when it is read (`legacy_quality.py`): valid hours keep their
values, and hours flagged `suspect` become excluded hours with the reason
`flagged_by_earlier_version`, because Home Assistant may no longer hold the
history that was persisted. A refresh with excluded hours is a `success` and does
not block optimization; there is no `suspect` run status.

**Battery efficiency.** The measured battery-efficiency importer aligns the six
energy legs and the state-of-charge history. A state-of-charge sample that is
unavailable, not a number, or outside 0 to 100 percent (`soc_out_of_range`) is
never carried forward and excludes the hours in which it is in force. An hour
excluded in any leg or in the state of charge is excluded in all six legs, and the
state-of-charge values that bracket it (`state_of_charge_percent[i]` and
`[i + 1]` for hour `i`) are dropped, so the ratios never mix valid and invalid
legs. `calculate_battery_efficiency` skips excluded hours and does not use a
full-charge cycle that contains one, which reduces the number of usable cycles
instead of producing a wrong ratio. On a DC-coupled system the AC-sourced charge
(battery in minus PV yield) and the DC-bus input of the discharge (battery out plus
PV yield minus battery in) are negative in ordinary hours, so those two aggregations
take `part: positive`. Left at `net`, every PV-surplus hour would exclude itself in
all six legs and remove the full-charge cycle it belongs to. The exclusions are read through
`GET /api/v1/dashboard/excluded-hours` (see [`docs/api.md`](api.md)) and shown
on the dashboard's **Excluded hours** tab.
`HomeAssistantLoadImporter` composes this functionality into the logical
`household_load` record. `HomeAssistantGridFlowImporter` composes it independently
for import and export, allowing multiple signed entities per channel, then aligns
both channels to their common available start and returns `GridFlowData` under
the logical `grid_flow` identity. These importers and
`HomeAssistantBatteryEfficiencyImporter` have no `fetch` method. Their single
`plan` method returns a `HistoryPlan`: the declared history needs and a build step
that turns the imported history into the record. They make no Home Assistant
request, and they expose freshness checks but do not start polling, schedule
requests, cache results, persist results, or invoke the API layer. The
orchestration layer owns those policies, including the incremental start time and
the reads of the persisted store that decide it, which happen in the plan phase.

`HomeAssistantBatteryImporter` is intentionally separate from the cumulative
energy helper because battery state of charge and capabilities are instantaneous
state values. It reads the live state-of-charge entity, while static capability
limits and the one battery round-trip efficiency may be supplied as validated constants or as configured
Home Assistant state entities. Entity values can optionally select a named
attribute; all values are converted to the battery contract, and one current SOC
value is returned together with scalar capability limits and the one battery
round-trip efficiency. Only
entity-backed values determine freshness, and a failure in any required mapping
prevents a partial snapshot from being returned. Historic SOC reconstruction for
efficiency calculations is handled by the dedicated measured-efficiency importer,
which aligns configured battery and inverter expressions with state-of-charge
history, imported through the same shared layer, before persisting it for the
daily calculation.

Normalized provider data may be persisted after validation when persistence is
configured. The storage component stores the normalized provider model or
history, not raw vendor responses or optimizer snapshots. Records are keyed by
data type, provider, and entity identifier. Atomic replacement, validation on
read, and a backup copy allow recovery from interrupted or corrupted writes.
Only data with a configured provider identity is persisted; source-less API
submissions remain request-scoped. Non-household-load data remains a readable
JSON model. Grid-flow saves are merged by hourly timestamp into one contiguous
retained history (incoming values win, at most 87,672 hours, a gap is rejected
without changing the stored history) by the pure rules in `history_merge.py`;
measured battery-efficiency history is persisted as one aligned multi-series
record.
Each scheduled recompute requests only the hours after the previously
persisted history from Home Assistant, merges them into the retained record,
and bounds retention to the same ten-year limit as household-load history, so
the daily calculation only pays the cost of a complete history fetch once,
not on every run.
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
error.

Historic multi-asset actuals are read through the actual scenario of the
dashboard contract. `api/historic.py` holds one loader per asset (household
load, PV generation, grid flow, electricity prices, battery, electric vehicle,
and heat pump). A loader reads only its own persisted normalized record and
returns explicit series plus an availability status, so an absent, stale, or
corrupt asset can never invalidate the series of another asset. Corrupt or
unrecoverable records are withheld rather than mapped, and their technical cause
is logged instead of returned. Assets without an importer (PV actuals, electric
vehicle, heat pump) report `not_configured`; adding an importer means
registering one loader. Electricity-price history is a separate
`electricity-price-history` record written by the price orchestration
registration, because the forecast record is replaced by every run and the two
must not be confused; a failure to write it never blocks the forecast refresh.
Battery state of charge reuses the retained hourly history that the measured
efficiency calculation already persists. The dashboard consumes this contract and
labels its values as actuals; it does not infer provider semantics or combine
forecasts and plans.

The versioned dashboard read contract is exposed at
`GET /api/v1/dashboard/data`. It uses one response envelope for actual,
forecast, and plan scenarios, while each series carries its machine-readable
scenario kind, unit, source or plan identity, requested and available coverage,
freshness, validation status, and nullable missing intervals. Forecast reads use
the latest complete persisted run and never combine overlapping provider runs.

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
