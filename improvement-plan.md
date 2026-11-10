# Improvement Plan

Developed by **Claude Fable 5.2** (Anthropic) on 2026-11-10 as a review of
`devel` at commit `724859f`.

This document lists confirmed bugs, robustness gaps, architecture and
documentation debt, and dashboard usability problems, and ends with one ordered
backlog proposal. It changes no code.

## How this review was done

- Read the production code: the optimization model, plan generation,
  orchestration, control, storage, configuration, the API routers, the provider
  adapters used by the running installation, and the complete frontend.
- Reproduced the bug candidates of the optimization model with the real solver
  (linopy and HiGHS from the project environment). Results are quoted below.
- Inspected the local installation that Claude session
  `0ceeb6d3-e9c2-42f4-b51e-83ce31c8320f` set up: its configuration, the persisted
  provider data and plan, and 9.5 hours of service log (2026-11-09 19:12 UTC to
  2026-11-10 03:48 UTC, 103 plans).
- Read the history of that session to understand which decisions were made on
  purpose, so that deliberate behaviour is not reported as a defect.

Not done: the full test suite was not run locally (CI for `724859f` is green),
the dashboard was not exercised in a browser, Home Assistant was not queried
live (the service was not running; persisted data and logs were used instead),
and the open pull requests #283 and #284 were not reviewed.

Every finding states how it was established: **reproduced** (run against the
real solver or seen in the installation's data), or **read** (follows from the
code, not executed).

## Summary

The data and import layer is careful and well tested. The optimizer produces
plausible week plans in 1.4 to 13 seconds per cycle. The weak spots are at the
edges of the happy path:

1. **Planning stops in situations where it matters most.** A battery reading
   below its configured minimum, or a car charged above its charge limit, ends
   planning for the whole house (B1, B2).
2. **Background loops can die without anyone noticing.** Orchestration, wall box
   control, and the plug-in reminder are unsupervised tasks, and `/health`
   always answers `ok` (B3).
3. **The load forecast runs in its last-resort mode most of the time** on the
   real installation: 72 of 103 plans, caused by a two-second sensor dropout
   each night (R1).
4. **The plan is not anchored to the moment it is computed.** Its first hour is
   the next full hour, the running hour, or an hour that has already passed,
   depending on when prices were last fetched (B4).
5. **The architecture document no longer describes the code**, and the code no
   longer follows the dependency rules the document states (A1, D1).
6. **The dashboard shows data, but not the state of the system or the reason
   for a decision.** There is no status, no price column in the plan, no
   current-hour marker, and three different numbers called cost (G1 to G4).
7. **The battery plan is advisory only.** Nothing applies it, and nothing
   publishes it to Home Assistant (M9).

## 1. Bugs

Severity: **high** stops planning or control, **medium** produces wrong plans or
wrong figures, **low** is cosmetic or needs unusual input.

### B1. A battery state of charge outside its limits blocks all planning (high, read)

`providers/home_assistant_battery.py:340-343` rejects a snapshot whose state of
charge is below `minimum_soc` or above `maximum_soc`. The battery source then
fails, and because `battery` is a required source, `_trigger_plan` returns
`not-ready` (`orchestration.py:513-519`) for as long as the reading stays
outside.

The installation uses `minimum_soc: 10 %`. An ESS that idles at its minimum
drifts below it through standby consumption, so the plan disappears exactly when
the battery is empty and the next cheap charging hour has to be found.

Fix: accept any state of charge between 0 and the capacity. In plan generation,
use `min(minimum_soc, initial_soc)` as the lower bound and
`max(maximum_soc, initial_soc)` as the upper bound, so the plan neither forces a
grid charge nor becomes infeasible, and log one warning.

Regression test: a fake Home Assistant battery at 9 % with a 10 % minimum still
yields a stored plan that never discharges further.

### B2. A car above its charge limit makes the whole plan infeasible (high, reproduced)

`optimization/ev_model.py:47-52` bounds the car's state of charge by
`charge_limit_kwh`. When the car already holds more (charged elsewhere, or the
limit was lowered), the first hour has no feasible state.

```text
EV 70 kWh with limit 80 kWh -> optimal
EV 85 kWh with limit 80 kWh -> infeasible
```

`PlanGenerator` then raises "the solver did not find an optimal plan:
infeasible" and no plan is stored for the house, the battery included. The
limit is set by `surplus` (`plan_generation.py:194`), `daily_minimum` with a
`target_soc_percent` (`:266`), and `minimum_plus_target` (`:315`). The
installation is not affected today because its limit is 100 %.

Fix: bound the state of charge by `max(charge_limit_kwh, initial_soc_kwh)`. The
car then simply does not charge until it is below the limit.

Related, same file line 49: `charge_limit_kwh or capacity_kwh` treats a limit of
`0.0` as "no limit". Use an explicit `is None` check.

### B3. Background loops die silently and `/health` stays green (high, read)

- `api/lifecycle.py:158-170` starts orchestration, wall box control, and the
  plug-in reminder with `asyncio.create_task` and never looks at them again
  until shutdown.
- `ProviderOrchestrator.run_forever` (`orchestration.py:628-633`) has no guard
  around `run_due`. Most errors are caught inside, but any exception outside
  those blocks ends the loop for good.
- `EvController.apply_due` reads the schedule outside its `try`
  (`control/ev_controller.py:110`). A `ProviderDataStoreError`, for example from
  a file that Windows has locked, ends the control loop. The wall box then keeps
  its last setting indefinitely.
- `PlugInReminder.remind_due` has the same exposure
  (`control/plug_in_reminder.py:146`, `:155`).
- `/health` returns a constant (`api/routers/core.py:33-36`), and the Docker
  `HEALTHCHECK` relies on it.
- At shutdown, `await ev_control_task` re-raises the stored exception
  (`lifecycle.py:196-197`), so the reminder and orchestration tasks are never
  awaited.

Fix: one supervised loop helper used by all three tasks that catches, logs with
a traceback, and continues after a bounded delay. Report loop liveness, the
outcome of the last cycle, and the age of the last plan in a status endpoint
(see R4), and let `/health` fail when a loop is dead.

### B4. The plan is not anchored to the time it is computed (medium, reproduced)

`planning_horizon` (`plan_generation.py:320-332`) takes every hour for which
prices and a PV forecast exist. It never looks at `snapshot.captured_at`. The
price importers drop the running hour (`providers/awattar.py:156`,
`providers/epex_predictor.py:115`), so the first planned hour depends on when
prices were last fetched:

- Prices fetched at 03:17, plan at 03:48: the plan starts at 04:00. The stored
  plan of the installation shows exactly this (`created_at` 03:48:10,
  `start_time` 04:00). The state of charge measured at 03:48 is used as the
  state at 04:00, and the remaining twelve minutes are not modelled. Around
  noon on a sunny day twelve to sixty unmodelled minutes are several kWh.
- Same prices, plan at 04:10: the plan starts at 04:00, the running hour, and
  counts its full load and PV although ten minutes are gone.
- Price fetch fails once, plan at 05:10: the record from 03:17 is still fresh
  (`max_data_age_seconds: 7200`), so the plan starts at 04:00, an hour that has
  completely passed.

```text
plan computed at 2026-11-10T05:10:00+00:00 -> first planned hour 2026-11-10T04:00:00+00:00
```

The docstring of `EvSchedule` (`control/ev_controller.py:39-41`) states "a new
plan starts at the next full hour", which is only one of the three cases.

Fix: keep the running hour in the price record, drop planned hours that ended
before `captured_at`, and model the running hour as a partial interval (see A7
for the interval length this needs). Until the partial interval exists, starting
at the running hour and scaling its load and PV by the remaining fraction is a
small change that removes the error.

### B5. Plans are discarded when the solver reaches its time limit (medium, reproduced)

`LinopyPlanSolver.solve` returns an empty plan for every status except
`optimal` (`linopy_solver.py:66-67`), and `PlanGenerator` treats that as a
failure (`plan_generation.py:403-406`).

```text
week plan: status optimal, 1.33 s
week plan with time limit 0.2 s: status time_limit, hours in plan 0
```

Today a solve takes one to two seconds against a 60-second limit, so this is
latent. It becomes real with more binaries (appliance scheduling, finer time
steps). No relative MIP gap is set either, so the solver always proves
optimality to the default tolerance.

Fix: accept a feasible incumbent at the time limit as a plan with status
`feasible` and report its gap; set a relative gap (about 0.5 %) in
`SolverConfiguration`.

### B6. `pv_self_consumption_kw` is negative when the battery exports (medium, reproduced)

`linopy_solver.py:212` computes `pv_used - grid_export`. With an export price
above the stored-energy value the battery sells to the grid, and the figure
turns negative:

```text
pv_self_consumption_kw [-5.0, 0.0, -3.06, -5.0]   grid_export_kw [5.0, 0.0, 3.06, 5.0]
```

This reached the API and the dashboard before the fixed feed-in tariff was
configured. Fix: `max(pv_used - grid_export, 0)`, and name the convention
(exports are attributed to PV first) in the field description.

### B7. A stale EV schedule turns "no need to charge" into "charge at full power" (medium, read)

With the `deadline` and `minimum_plus_target` strategies, a deadline beyond the
horizon makes `ev_specification` return `None` (`plan_generation.py:301-305`).
The plan is stored, but the EV schedule is not updated
(`plan_generation.py:567-569`). After `max_plan_age_seconds` the controller
considers the schedule stale and falls back to full power
(`control/ev_controller.py:129-146`).

The same fallback applies when planning fails for longer than the maximum age:
the wall box is switched to maximum power regardless of price.

Fix: write an explicit zero schedule when the car needs no charge, and make the
fallback configurable (`full_power`, `off`, `keep_last`), with the current
behaviour as the documented default only if it is wanted.

### B8. The orchestration log is dominated by a non-event (low, reproduced)

`orchestration.py:304-309` logs `provider_refresh_skipped reason=not_due` at
WARNING for every source in every cycle. In the inspected log that is 752 of
826 warnings. The two real warnings of that night (`provider_hours_excluded`)
are buried.

linopy adds about ten unstructured INFO lines per solve, and HiGHS prints its
banner to standard output although `output_flag=False` is set, so the log is
not purely structured:

```text
Running HiGHS 1.15.1 (git hash: 04024d7): Copyright (c) 2026 under MIT licence terms
```

Fix: log `not_due` at DEBUG, set the `linopy` logger to WARNING, and silence the
HiGHS console output.

### B9. Smaller defects (low)

- **Negative terminal value (reproduced).** `terminal_value_of_stored_energy`
  (`plan_generation.py:379`) uses the cheapest import price without a floor. A
  negative cheapest price gives stored energy a negative value (`-0.017` in the
  reproduction), which drains the battery at the end of the plan. The EV value
  is already floored at zero (`:228`); do the same here.
- **A gap between published and forecast prices is silent (read).**
  `extend_with_forecast` (`plan_generation.py:351-355`) returns the published
  prices alone when the forecast does not continue them hour by hour. The week
  plan then shrinks to about a day without any log line. Log a warning.
- **Wrong label (read).** The Plan tab shows "EV shortfall at deadline (kWh)"
  (`frontend/app.js:723`), but the value also contains energy missing for
  driving and for morning minimums (`ev_model.py:168-173`).
- **Reminder ignores the age of the schedule (read).** `planned_night_charge`
  uses the stored schedule however old it is (`plug_in_reminder.py:100-115`).
- **Unhandled storage error (read).** `GET /plan` lets a
  `ProviderDataStoreError` become HTTP 500 (`core.py:152`). No exception handler
  is registered anywhere in the application.

## 2. Robustness

### R1. One two-second sensor dropout per night degrades every plan (reproduced)

In the installation's household-load history, 14 of 335 hours are excluded. All
have the reason `unavailable`, and 11 of them are the hour starting 01:00 local
time. Each is a single sample: one of the two three-phase meters reports
`unavailable` at about 01:01:35 local time and a valid value two seconds later,
for example:

```text
sensor.og_l1_energy unavailable at 2026-11-09T00:01:33Z
(last valid value 2026-11-08T23:59:14Z, next valid value 2026-11-09T00:01:35Z)
```

Consequences in the last seven days: local hour 1 had one valid sample or none.
`seven_day_mean` then cannot forecast that hour, `weekday_profile` needs 28 days
of history, and the forecast falls back to `filled_profile` in 72 of 103 plans.
That fallback has no backtest error, which is why the installation needs a
hand-set `reserve.forecast_error_kw`.

Three independent improvements:

1. **Forecast per hour of day, not all or nothing.** When a method lacks samples
   for an hour of day, widen the window for that hour only (28 days, then
   neighbouring hours) and keep the method and its backtest error for the rest.
   Give `filled_profile` an error estimate as well.
2. **Decide whether a short dropout of a cumulative counter must exclude the
   hour.** The counter values before and after the dropout are valid and the
   step between them is attributed to the same hour, so no energy is unknown.
   The architecture document states that nothing is repaired or tolerated, so
   this is a design decision, not a bug. Proposal: an optional
   `maximum_outage_seconds` per entity, default 0 (today's behaviour), that
   keeps an hour when the invalid samples are bracketed by valid, non-decreasing
   readings inside that hour.
3. **Find the cause in Home Assistant.** Both meters drop out at the same minute
   every night, which points to a scheduled restart or reconnect.

### R2. A failed fetch waits a full interval before it is retried (read)

`_failed_run` schedules the next attempt one regular interval later
(`orchestration.py:481`). A transient failure of an hourly source is retried
after an hour, and a second failure makes the data stale and stops planning.
There is no retry inside the HTTP helper either. Add a short retry interval with
backoff for failed sources (issue #18 covers caching and fallback).

### R3. Atomic replacement can fail on Windows (read, seen as a test flake)

`ProviderDataStore._atomic_write` calls `os.replace` once (`storage.py:331`).
On Windows a virus scanner or a concurrent reader makes it fail with "access
denied". The session notes record this as a known intermittent test failure,
and it is the most likely trigger for B3 in production. Retry the replace a few
times with a short delay.

### R4. The system has no visible state (read)

`run_forever` discards the `OrchestrationCycle` it gets back
(`orchestration.py:629`). Which source failed, why no plan was created, and how
old the stored plan is can only be found in the log. Add
`GET /api/v1/status` with, per source, the last attempt, status, and error, and
for the plan the last status, error, and age. The dashboard and Home Assistant
can then show it (G1).

### R5. Home Assistant readings cannot be stale by construction (read)

The battery and EV importers set `latest_observation_at` to the retrieval time
(`home_assistant_battery.py:120-122`, `home_assistant_ev.py:69-71`). A car whose
cloud integration last reported hours ago looks current. Use the entity's
`last_reported` or `last_updated` time with a separate, longer age limit for the
car, and show the age in the plan summary.

### R6. Open API without authentication on all interfaces (read)

The service binds to `0.0.0.0` by default (`__main__.py:24`) and has no
authentication. `POST /api/v1/household-load` and `POST /api/v1/grid-flow`
change persisted history (`api/routers/provider.py:193-221`, `:322-345`).
Bind to `127.0.0.1` by default outside Docker, or add an optional API token, and
document the exposure.

### R7. Secrets must be written into the YAML file (read)

The Home Assistant token is a literal in `config.yaml`. The installation works
around this with a script that renders a template. Support `token_file` or
environment expansion. InfluxDB credentials are sent as URL query parameters
(`influxdb_ev_history.py:176-183`); send them as a Basic authorization header.

### R8. A plan is solved every five minutes (reproduced)

Every refresh of a required source triggers a plan. With the battery and the car
polled every 300 seconds this is 103 week plans in 9.5 hours. Solving is cheap,
but each plan rewrites the EV schedule and the plan file, and the log grows by
about 1.5 MB per day. Trigger on a state change that matters instead: a new
hour, new prices or forecasts, a plug event, or a state of charge that deviates
from the plan by more than a threshold.

### R9. Deployment is not reproducible (read)

The Dockerfile installs unpinned dependencies from `pyproject.toml` and ignores
`uv.lock`. `linopy` and `highspy` have no upper bound although linopy is a 0.x
library whose semantics are changing. Install from the lock file in the image
and bound both packages.

## 3. Architecture

### A1. The code does not follow its documented dependency rules (read)

`docs/architecture.md` states `optimization -> domain`, "storage -> validated
provider-independent data models", and that the API contains no provider logic.
The code has grown differently:

| Module | Imports that break the rule |
|---|---|
| `optimization/plan_generation.py:11-54` | `config`, `control.ev_controller`, `orchestration`, `storage` |
| `optimization/battery_reserve.py:11-14` | `config` |
| `storage.py:24-31` | two provider modules; type switches at `:118-163` and `:309-319` |
| `orchestration.py:22-70` | nine concrete providers; 560 lines of registration factories from `:660` |
| `api/routers/dashboard.py:34-35` | `AwattarImporter`, `EpexPredictorImporter` (only for `is_fresh`) |
| `control/plug_in_reminder.py:35-36` | a provider importer |

Function-local imports hide the resulting cycles (`orchestration.py:779`,
`:1162`, `storage.py:310`, `api/routers/core.py:44`).

Target layout, reached in small steps:

```text
domain/         problem and plan types, normalized provider data, exclusions
optimization/   the MILP and the forecasting maths; takes plain parameters
application/    orchestrator, plan generation use case, EV schedule, status
providers/      adapters
control/        actuators
composition.py  the only module that imports concrete providers
storage/        files and atomic writes; merge rules are passed in by the caller
```

Enforce the rules with an import-linter contract as a `scripts/verify` step, so
the document and the code cannot drift apart again.

### A2. Optimizer settings live inside the Home Assistant provider section (read)

`reserve`, `headroom`, `throughput_cost_eur_per_kwh`, and `end_of_horizon` are
fields of `home_assistant.battery` (`config.py:285-305`). EV settings are split
between `home_assistant.ev` and `ev_charging`. A battery from another source
could not be tuned at all. Move planning parameters to a top-level section
(`planning.battery`, `planning.ev`) in one documented breaking change. Issue
#203 already asks for the same separation for the efficiency calculation.

### A3. Storage keys are written out in four places (read)

`ProviderDataKey("electricity-prices", "awattar.de", ...)` and its siblings are
built by hand in `orchestration.py`, `api/routers/dashboard.py`,
`api/historic.py`, and `api/excluded.py`. One catalog module should own every
key and adapter.

### A4. The HTTP surface is inconsistent (read)

- `/plan`, `/optimize`, and `/health` are unversioned; everything else is under
  `/api/v1`.
- `scenario_kind=plan` of the dashboard contract is a stub that always answers
  "unavailable" (`dashboard.py:575-582`), while the Plan tab reads `/plan`.
- `/api/v1/forecast` is a hidden duplicate of the forecast scenario
  (`dashboard.py:620-630`).
- `tail_router` exists only to keep a route order.
- Compatibility shims remain although the guidelines forbid them without a
  requirement: module `__getattr__` in `api/app.py:63-68`, the re-exports in
  `api/__init__.py`, and the private test hooks in `storage.py:82-85`.

### A5. Unused configuration and code (read)

- `time_resolution_minutes` is required, validated, and never read
  (`config.py:1067`). `solver.name` is required and never read either.
- `_objective_value` is never called (`linopy_solver.py:309-313`).
- `deadline_hours` (`plan_generation.py:110-121`) is used only by its tests.

### A6. Three numbers are called cost (read)

The solver objective (logged by linopy as `-2.05e+01`), `Plan.objective_eur`
(cash cost plus wear, logged as `2.7571`), and the sum of
`optimizer_cost_eur` (objective without the constant value of the initial
battery energy) describe three different things. `pv_generation_kw` is the PV
actually used, not the forecast. Name them by meaning (`cash_cost_eur`,
`wear_cost_eur`, `valued_cost_eur`, `pv_used_kw`) and define each once in a
glossary (D4).

### A7. Power and energy are the same number (read)

Every interval is one hour, so kW and kWh are used interchangeably throughout
the model, the plan types, and the dashboard totals. The partial running hour of
B4 and any quarter-hour price source need an explicit interval length. Introduce
it in `PlanningProblem` before either is built.

### A8. Module and function size (read)

`config.py` (1227 lines) and `orchestration.py` (1223 lines) each mix several
responsibilities. `lifespan` is about 140 lines, `_run_due_locked` about 95,
`_forecast_dashboard_data` about 90. Split configuration by section and
orchestration into the orchestrator and the composition root (A1). Ruff selects
only `E`, `F`, and `I`; adding `B`, `UP`, `SIM`, `RUF`, and a complexity limit
would keep this from growing back. `HOUR_DIMENSION`, `HOURS_PER_DAY`, and
`ONE_HOUR` are each defined in three or more modules.

### A9. The frontend is one 1018-line function (read)

`frontend/app.js` holds time-zone arithmetic, three chart definitions, the plan
table, the excluded-hours table, and tab handling in one closure. Labels, units,
chart groups, and colours of the series are hard-coded in four separate maps
(`app.js:175-198`, `:270-278`, `:330-342`). Native ES modules need no build
step: split into `time-zone.js`, `chart.js`, `plan.js`, `excluded.js`, and
`main.js`, and take series metadata from one table.

## 4. Optimization model

These are modelling choices to revisit, not defects.

- **M1. Soft-band windows follow the plan start, not the day.** The reserve and
  headroom penalties apply once per 24-hour block counted from the first planned
  hour (`battery_model.py:115-140`). The blocks shift every hour, so the same
  low point can fall into a different block from one plan to the next. Use local
  calendar days, or night and daylight periods.
- **M2. Headroom is kept whenever any PV is forecast.** The test is `pv_kw > 0`
  (`battery_reserve.py:61-65`), which includes dawn. Use hours with expected
  surplus, and size the band from the measured PV forecast error (M5).
- **M3. Energy in the car is valued at the cheapest price of the plan.** Every
  charged kWh is credited with that price times the charging efficiency
  (`plan_generation.py:223-228`, `linopy_solver.py:182-183`). By construction
  the car is therefore never charged from the grid for later use, not even in an
  hour with an exchange price of zero, because that hour is itself the
  reference. The note "EV Laden wenn EPEX 0min" in `todo.md` suggests this is
  wanted. Value the energy against what charging usually costs instead (a
  configurable price, or the median import price of the horizon), limited to the
  expected driving demand. The intent of the note should be confirmed first.
- **M4. The load forecast knows only the hour of day.** The heat pump is part of
  the household load and follows the outside temperature. Open-Meteo is already
  queried for irradiance; the same request can return temperature for a
  temperature-dependent component.
- **M5. Forecast errors are not measured.** PV history, price history, and load
  history are collected, but forecasts are overwritten by every run. Persist
  forecast snapshots, compare them with actuals, and derive the reserve, the
  headroom, and a risk premium on forecast prices from the measured error. The
  session postponed the price risk premium for exactly this reason. Issue #97
  is the dashboard side of this.
- **M6. Today's driving may be counted twice.** The learned daily driving energy
  is deducted in full at the car's return (`ev_patterns.py:125-140`), also on
  the current day when part of the trip already shows in the measured state of
  charge.
- **M7. Grid fee bands have no validity period.** `tariff.grid_fee_bands`
  (`config.py:973-1018`) apply to every day of the year. Check whether the
  network operator applies the time-variable fee all year, and add a validity
  range so a tariff change does not need a restart at midnight.
- **M8. Check what the household-load meters include.** The load is the sum of
  six floor meters. If the wall box is behind one of them, past charging is part
  of the load forecast and is planned a second time. The aggregate already
  supports a `subtract` term for this.
- **M9. Nothing executes the battery plan.** The plan decides when the battery
  charges from the grid and when it holds, but there is no actuator and no
  published signal. Wall box control exists and is switched off in the
  installation. Publishing the plan and the action of the current hour to Home
  Assistant (REST sensor documentation or MQTT discovery), followed by a battery
  control mode, is the largest functional step still open.

## 5. Dashboard

### G1. The first view does not answer "what is happening now" (read)

The dashboard opens on historic actuals. There is no overview with the action of
the current hour, the next planned changes, the state of the sources, and the
age of the plan. A dead orchestration loop (B3) shows an old plan without any
hint. Add an overview as the default tab, fed by the status endpoint (R4), with
a banner when the plan is older than expected, and refresh it periodically.

### G2. The default range expands to the whole history (read)

The default range is "today", whose end lies in the future. `alignRangeToCoverage`
then replaces it with the full coverage of all series (`app.js:298-304`,
`:914-921`). Every value becomes an SVG circle with four listeners and its own
tab stop (`app.js:445-454`). A seven-day forecast already creates several
hundred tab stops, and with a retention of up to 87,672 hours the actuals view
will not stay usable as the history grows. Clamp the
request to the intersection with the coverage, default to the last 48 hours,
draw points on hover through one overlay, and thin out long ranges.

### G3. The plan table is hard to read (read)

- 163 rows with up to 17 columns, no day separators, no daily subtotals, no
  marker for the current hour, no colour for import, export, charge, and
  discharge.
- **No price column.** The table shows whether a price is published or forecast,
  but not the price. Why the plan imports at 03:00 cannot be read from it.
- Three cost figures without explanation: "Objective (EUR)", "Cost (EUR)", and
  "Cost (Optimizer)". The last one totals to about -25 EUR in a typical week and
  is not money.
- The total row sums columns headed kW; the sums are kWh.
- Column headings have no help text.

After #284 is merged, show the charts first and the table collapsed by day, add
import and export price columns in ct/kWh, and give every column a one-line
explanation. Issue #12 (explainable schedules) belongs here.

### G4. Navigation and accessibility (read)

- Tabs cannot be linked or bookmarked; a reload returns to historic actuals and
  forgets hidden series.
- The tab buttons have `role="tab"` without a `tablist`, and four of five point
  to the same panel although the content is in three different sections
  (`frontend/index.html:26-34`).
- The time axis has five labels with full timestamps, no day grid lines, no
  marker for now, and no shading of nights (`app.js:425-436`).
- A response that is not JSON shows a parser message to the user
  (`app.js:784`, `:908`).

### G5. Content gaps (read)

- The Forecast tab shows PV and prices, but not the load forecast and not the
  expected presence and driving of the car, although all are plan inputs.
- Export prices are shown only for the published hours. This is the first item
  of `todo.md`, issue #281, and is solved by the open pull request #283.
- Prices are shown as EUR/kWh with five decimals; ct/kWh is the unit of the
  tariff configuration and of every bill.
- The interface is English only and uses fixed number and date formats. A
  `locale` setting next to `timezone` and `Intl.NumberFormat` would cover both.
- There is no way to override the plan for the car ("charge to 80 % now"),
  neither in the dashboard nor through Home Assistant.

## 6. Documentation

- **D1. `docs/architecture.md` is out of date.** It names Pyomo twice (lines 149
  and 448); the code uses linopy. Its package tree misses, among others,
  `control/plug_in_reminder.py`, `control/home_assistant_service.py`,
  `optimization/battery_reserve.py`, `ev_patterns.py`, `optimizer_cost.py`,
  `providers/epex_predictor.py`, `influxdb_ev_history.py`,
  `home_assistant_ev.py`, `home_assistant_pv.py`, and `logging_config.py`. Its
  introduction still says that only `POST /optimize` exists. The Optimization
  section is four lines long.
- **D2. The optimization model is not documented in one place.** Variables,
  constraints, the objective terms (terminal value and its 2 % discount, soft
  reserve and headroom, the value of energy in the car, the 1000 EUR/kWh
  shortfall penalty), the rolling horizon, and the known simplifications are
  spread over docstrings and the README. The battery reserve and headroom are
  described under the heading "Electric vehicle charging"
  (`README.md:500-545`). Write `docs/optimization-model.md`.
- **D3. The README is 997 lines and starts with a paragraph about appliances.**
  The product description is the second paragraph. "Planned Architecture" says
  the boundaries are "not a claim that all of the modules exist today", and
  "Input and Output" is written in the future tense. There is no quick start for
  a Home Assistant user, no screenshot, no explanation of the Plan tab, and no
  troubleshooting. Proposal: a README of about 150 lines (what it does, a
  screenshot, quick start, links) and separate documents:
  `docs/getting-started.md`, `docs/configuration.md`, `docs/home-assistant.md`,
  `docs/dashboard.md`, `docs/operations.md`.
- **D4. No glossary.** Define once: the cost terms of A6, "state at the end of
  the hour", kW against kWh, published against forecast price, reserve,
  headroom, shortfall.
- **D5. The configuration reference is prose.** Generate it from the pydantic
  models (field, type, default, description) and check it in CI the way the
  OpenAPI document is checked. Add a minimal working example for Home
  Assistant with aWATTar and Open-Meteo under `examples/` and validate every
  example in a test. `config.example.yaml` ships with orchestration and
  optimization disabled, so copying it yields no plan.
- **D6. Decisions are recorded only in a chat history.** linopy behind a solver
  port, the strict hour rule, the terminal value, penalties once per day, the
  value of energy in the car, and home hours with a reminder were all decided in
  conversation. Record each as a short decision record under `docs/decisions/`.
- **D7. Operations are undocumented.** How to run the service permanently (the
  installation ran as a child of a Claude session and was stopped three times
  when memory ran low), where logs go, which log events matter, how to back up
  the data directory, and what to do when no plan appears.

## 7. Tooling and tests

- **T1. Coverage targets are not enforced.** `AGENTS.md` aims for 80 % overall
  and 90 % for domain and API code; `scripts/verify:50` runs `--cov` without
  `--cov-fail-under`.
- **T2. Property-based tests for plan invariants.** For random small problems:
  energy balance per hour, state of charge within bounds, no simultaneous
  import and export or charge and discharge, non-negative reported flows, and
  hourly optimizer costs that sum to the objective. This would have found B2
  and B6. `hypothesis` is already a dependency.
- **T3. A replay test from a real snapshot.** Store one anonymized provider
  snapshot of the installation and assert the properties of its plan, so model
  changes are judged against a realistic week.
- **T4. Known intermittent failures on Windows** (orchestration gap, storage
  compaction, efficiency counter decrease) should be traced to their cause; R3
  is the first suspect.
- **T5. Two different `dev` groups.** `[project.optional-dependencies] dev` and
  `[dependency-groups] dev` hold different packages, and `pyyaml` is listed
  twice. Merge them.
- **T6. A supported way to run on Windows.** The start script lives outside the
  repository. Add a documented script or service definition under `scripts/`.

## 8. Backlog hygiene

Several open issues describe work that already exists and should be closed or
rewritten to what is left:

| Issue | State in the code |
|---|---|
| #10, #11 | EV charging with availability and departure targets is implemented |
| #44 | The EV is imported from Home Assistant |
| #32 | The Plan tab displays the plan; #284 adds charts |
| #21 | The plan is recomputed with measured state; B4 is the remaining gap |
| #95 | The latest plan is persisted and exposed; a plan history is not |
| #54 | The Forecast tab displays predicted data |

Open pull requests #283 and #284 should be merged before the dashboard items
below are started, because both change the same views.

## 9. Proposed backlog order

One ordered list, smallest valuable increments first. Each line is one issue.

| # | Type | Outcome | Findings |
|---|---|---|---|
| 1 | Bug | A battery reading outside its limits still yields a plan | B1 |
| 2 | Bug | A car above its charge limit still yields a plan | B2 |
| 3 | Bug | Background loops survive errors; `/health` reports a dead loop | B3, R3 |
| 4 | Bug | The load forecast keeps its method when one hour of day lacks samples | R1 (1) |
| 5 | Bug | Routine cycles log no warnings; solver output is structured | B8 |
| 6 | Feature | A status endpoint and a dashboard banner show source and plan state | R4, G1 |
| 7 | Bug | The plan starts at the running hour of its computation | B4, A7 |
| 8 | Bug | A feasible plan at the time limit is used | B5 |
| 9 | Bug | Reported PV self-consumption is never negative; labels and floors corrected | B6, B9 |
| 10 | Bug | An unneeded or stale EV schedule never switches the wall box to full power | B7 |
| 11 | Task | Failed sources are retried early with backoff | R2 |
| 12 | Feature | The plan table shows prices, day groups, and the current hour | G3 |
| 13 | Feature | An overview tab is the default view; ranges no longer expand to all history | G1, G2 |
| 14 | Task | Architecture document, model description, and glossary match the code | D1, D2, D4 |
| 15 | Task | README shortened; getting-started, configuration, and operations guides | D3, D5, D7 |
| 16 | Task | Plan generation and provider composition move out of `optimization` and `orchestration`; import rules enforced | A1, A3 |
| 17 | Task | Planning parameters leave the Home Assistant section; secrets from file or environment | A2, R7 |
| 18 | Feature | Short counter dropouts can be tolerated per entity | R1 (2) |
| 19 | Feature | The plan and the current action are published to Home Assistant | M9 |
| 20 | Feature | Forecast errors of load, PV, and prices are measured and shown | M5 |
| 21 | Feature | Reserve and headroom follow measured errors and calendar days | M1, M2 |
| 22 | Feature | Energy in the car is valued against a reference price, enabling charging in very cheap hours | M3 |
| 23 | Feature | The battery plan is applied through Home Assistant | M9 |
| 24 | Feature | The load forecast uses the outside temperature | M4 |
| 25 | Task | One API version, no stubs and shims, unused settings removed | A4, A5, A6 |
| 26 | Task | Plan invariants as property tests; a replay test; coverage thresholds | T1 to T3 |
| 27 | Task | Frontend split into modules with one series table | A9 |
| 28 | Task | Reproducible image; bounded solver dependencies; bind address and API token | R6, R9 |
| 29 | Feature | The owner can override the plan for the car | G5 |
| 30 | Task | Plans are triggered by relevant changes instead of every poll | R8 |

Items 1 to 5 are small and independent. Item 7 is the first that changes the
model and should be preceded by the interval length of A7. Items 14 and 16
belong together: write down the target structure first, then move the code and
add the import contract in the same change.

## Appendix: figures from the inspected installation

| Observation | Value |
|---|---|
| Log period | 2026-11-09 19:12 UTC to 2026-11-10 03:48 UTC |
| Orchestration cycles and plans | 103 each, one every five minutes |
| Cycle duration | 1.43 s minimum, 2.47 s median, 13.3 s maximum |
| Log lines by level | 3403 INFO, 826 WARNING, 0 ERROR |
| `provider_refresh_skipped` warnings | 752 |
| Plans with the `filled_profile` fallback | 72 of 103 |
| Household-load history | 335 hours, 14 excluded, 11 of them at 01:00 local time |
| Latest plan | created 03:48:10 UTC, starts 04:00 UTC, 163 hours |
| Problem size | 1793 variables, 1772 constraints |
