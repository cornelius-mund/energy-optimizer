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
depend on the consuming aggregate: timestamp parsing, skipping unknown and
unavailable samples with one warning per entity, finite and non-negative values,
and carrying `unit`, `state_class`, and `last_reset` from the previous sample
where Home Assistant omits them. It keeps only the cleaned series and only until
the cycle ends: raw responses are dropped as soon as they are cleaned, and there
is no cross-cycle cache (that belongs to a separate provider-caching feature).
Bearer-token authentication and the request timeout come from the Home
Assistant configuration. Requests covering more than seven days are split into
contiguous half-open chunks; the source's lookback is part of its declared range
and so moves only the start of the first chunk. Records from all chunks are
combined before normalization so counter deltas and reset handling remain
continuous at chunk boundaries. A failure while importing one entity, which
includes an HTTP error, a transport failure, or a malformed response, is recorded
against that entity and re-raised, with the entity named in the message, to every
source that reads it and to no other source; every other entity is still
imported. A sample with an invalid value is reported to the sources whose window
contains it, so a bad sample outside a source's window cannot fail that source.

Each source reads its own window of the shared series exactly as Home Assistant
would answer an independent request for it: the state in force at the window's
start, stamped with that start, followed by every change up to its end. A state
that became unavailable is not carried across the gap. Consequently a source's
result equals what a separate fetch of its window would have produced, and a
property-based test checks this equivalence.

The `home_assistant_energy.py` component is the pure normalization step that runs
on such a window with the consuming aggregate's own entity settings, so one
entity ID may be configured differently in different aggregates: it is fetched
once and normalized separately for each. Normalizations with identical settings
and windows within a cycle are computed once. It owns energy-unit conversion,
cumulative counter validation, reset-aware delta accumulation, signed entity
aggregation, hourly normalization, and observation metadata. It follows Home
Assistant's `total` and `total_increasing` state classes, rejects instantaneous
power entities, and rejects failed or incomplete contributions. Reset transitions
establish a new zero-contribution baseline, and a
value that returns close to the pre-reset counter is treated as recovery rather
than energy. Unknown and unavailable history samples are skipped without
assigning energy; the next valid counter observation owns the resulting delta,
and an entity with no usable observations still fails. Reset and recovery
intervals carry explicit suspect quality metadata through aggregation,
persistence, historic API responses, and orchestration; required suspect data
cannot trigger an optimization plan. A failed chunk fails the complete import of
its entity and so the sources that read it, so scheduled orchestration preserves
the last valid persisted data of those sources and retries on a later due cycle.
For `total_increasing` counters, an increase followed by a return close to the
pre-increase value is treated as a transient counter spike: the earlier delta is
retracted, both observations are marked suspect, and no fabricated energy is
retained.
A `total` counter that decreases without a changed `last_reset` is treated as
measurement jitter when it stays within the per-entity `decrease_tolerance_kwh`
(default 0.01 kWh, compared after unit conversion) of the highest value it has
reached. The step contributes no energy, the aggregator remembers that peak and
counts energy again only once the counter rises above it, and the interval keeps
its valid quality, because a suspect interval would block optimization. Because
the peak, not the previous sample, is the reference, accumulated drift beyond
the tolerance is not accepted as jitter. A single value never fails the fetch:
a larger decrease is handled like a `total_increasing` decrease, contributing no
energy, treating a return to the earlier peak as recovery, retracting a rise
that immediately returns as a transient spike, and marking the interval suspect
with a warning that names the entity, timestamp, previous value, current value,
and tolerance. The reference is the last observation at or before the start
of a requested period, so a dip that straddles that start can count at most one
tolerance's worth of energy once. A changed `last_reset`, `total_increasing`
counters, and unknown or unavailable samples are handled as described above.
Individual chunk request outcomes are debug-level diagnostics. The import emits
one structured summary at info level with its entity, failed-entity, and request
counts, and a warning for every failed entity. Each aggregate build emits one
structured success summary at info level or one failure summary at warning level
after the complete entity set has been processed.
Each cumulative-energy mapping may additionally define a physical upper bound
for one hourly delta in kWh. The bound is applied after unit conversion and
before signed aggregation; an exceeded bound produces zero energy and suspect
quality with reason `physical_limit_exceeded`. By default, a negative combined
hourly value fails the aggregate, since `household_load` and `grid_flow` totals
must never be negative, and the error names the offending hour by its UTC
timestamp. The one exception is an hour that at least one contributing entity
has already flagged suspect (for example `counter_reset` or
`physical_limit_exceeded`): the flagged counter explains the negative value, so
it is clamped to zero, the hour keeps its suspect quality, and the surrounding
hours are ingested. Each aggregation that clamps hours emits one structured
`home_assistant_negative_hour_clamped` warning listing every clamped hour's UTC
timestamp and the suspect reasons and entities of its contributors. The run is
then marked `suspect` by orchestration and stays blocked from automatic
optimization triggers, like any other suspect data. Because a suspect hour is
persisted instead of failing the fetch, a bootstrap stores the retained history
and a later incremental refresh continues past the hour instead of failing on
it every cycle. The reset transition itself contributes zero energy, but later
growth in the same hour from the new baseline is counted; the suspect flag, not
a zeroed value, marks such an hour as unreliable. Callers whose signed
expression represents a net directional flow instead of an absolute total, such
as the measured battery-efficiency legs, opt into `allow_negative`, which clamps
any negative hourly net to zero for that hour regardless of quality flags and
without a warning; a non-finite value is always rejected regardless of quality
flags or this option.
The measured battery-efficiency importer aligns the six energy legs and
state-of-charge history, then carries any suspect quality on those hours
through to the persisted history and the calculated result instead of
rejecting the fetch: a suspect hour must not prevent the surrounding,
unaffected hours from being persisted, since the incremental history store
can then only ever request the genuinely missing tail on the next scheduled
attempt instead of re-fetching the complete configured history. Orchestration
marks a run containing suspect quality as `suspect` rather than `failed`,
which still blocks that result from feeding automatic optimization triggers.
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
