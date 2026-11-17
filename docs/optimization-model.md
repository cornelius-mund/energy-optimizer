# Optimization Model

The plan is a mixed-integer linear program built with linopy and solved by HiGHS
behind the `PlanSolver` port (`optimization/linopy_solver.py`). This document
describes what the model contains, how its inputs are prepared, and what it
deliberately leaves out. Configuration keys are named as they appear in
`config.yaml`.

## Rolling horizon

- **When a plan is made:** after every orchestration cycle in which a required
  source refreshed (`orchestration.optimization.required_sources`), from one
  coherent snapshot of the latest data.
- **Which hours it covers:** every hour that has not ended when the snapshot is
  taken and for which both prices and the PV forecast exist, at most
  `orchestration.optimization.horizon_hours` (default 168). Published day-ahead
  prices are extended by the EPEX Predictor forecast when it continues them
  without a gap.
- **The running hour:** the first planned hour is the hour in which the plan is
  made. The measured battery and car states are its starting point, and only its
  remaining part is planned: flows stay powers in kW, and its energy and costs
  are scaled by `duration_hours` (below 1 only for this hour).
- **What is executed:** only the running hour matters for control; every later
  hour is recomputed by the next plan with fresh measurements and forecasts.

## Inputs

| Input | Source | Unit |
|---|---|---|
| Household load | Backtested forecast from the household-load history (`load_forecast`) | kW |
| PV | Forecast.Solar or Open-Meteo forecast | kW |
| Import price | Market price plus grid fee of the local hour plus fixed components, times VAT (`tariff`) | EUR/kWh |
| Export price | Fixed feed-in remuneration (`tariff.feed_in_ct_per_kwh`) or the market price | EUR/kWh |
| Battery | Live state from Home Assistant, limits from `home_assistant.battery` | kWh, kW |
| Car | Live state of charge and plug state, learned weekly driving and presence | kWh, kW |

## Variables per hour

- `grid_import_kw`, `grid_export_kw`, `pv_used_kw` (at most the forecast; the rest
  is curtailed).
- Battery: `battery_charge_kw`, `battery_discharge_kw`, `battery_soc_kwh` (the
  state at the end of the hour).
- Car: `ev_charge_kw`, `ev_soc_kwh`, `ev_missing_energy_kwh`, and one
  `ev_shortfall_kwh` at the deadline.
- Binaries: `importing` (no import and export in the same hour) and `charging`
  (no battery charge and discharge in the same hour). When the battery starts
  outside its operating band, one more binary per hour decides which side of
  that band applies.

## Constraints

- **Energy balance:** `pv_used + import + discharge = load + export + charge + ev_charge`.
- **Grid:** import at most `grid.maximum_import_kw`, export at most
  `grid.maximum_export_kw`, never both in one hour.
- **Battery state:** `soc = previous soc + duration × (charge × η_charge − discharge / η_discharge)`.
  The round-trip efficiency is split evenly over both legs.
- **Battery band:** the state stays between `minimum_soc` and `maximum_soc`. A
  battery that starts below its minimum is never discharged further below it and
  never forced back up; once a profitable charge brings it above the minimum,
  the minimum applies again. The same holds above the maximum.
- **End of the plan:** with `end_of_horizon: restore_initial` the battery ends
  at least as full as it started; with `value_stored_energy` it may end at the
  minimum, and the energy left is credited (see below).
- **Car power:** zero while the car is away or after the charging horizon; at
  most `maximum_charge_kw`, and with the `surplus` strategy at most the PV the
  household leaves.
- **Car state:** `soc = previous soc + duration × charge × η − driving + missing`,
  where driving is the learned energy of the day, deducted when the car returns.
  The state never exceeds the charging limit, except that energy already above
  it is kept.
- **Car targets:** the state at the deadline plus the shortfall reaches the
  target; every morning keeps its minimum (`daily_minimum`); dated requirements
  from the dashboard must be met.

## Objective

The solver minimizes, over all hours:

| Term | Meaning | Configuration |
|---|---|---|
| + import × price × duration | What the grid costs | `tariff` |
| − export × price × duration | What exports earn | `tariff.feed_in_ct_per_kwh` |
| + discharge × duration × wear | Battery wear per delivered kWh | `home_assistant.battery.throughput_cost_eur_per_kwh` |
| + reserve penalty | Below the soft reserve, once per planned day for the deepest point, in two price steps | `home_assistant.battery.reserve` |
| + headroom penalty | Into the soft headroom while PV is expected, once per day | `home_assistant.battery.headroom` |
| − final battery energy × terminal value | Energy left at the end replaces later imports | `home_assistant.battery.end_of_horizon`, `end_of_horizon_value_eur_per_kwh` |
| + 1000 EUR/kWh × car energy not provided | Makes missing targets a last resort instead of infeasible | none |
| − car charge × duration × η × stored value | Energy in the car is worth what charging it later would cost | `ev_charging.cheap_market_price_ct_per_kwh` |
| − car charge × duration × 0.1 ct in home hours | Breaks ties towards the hours in which the car is surely home | `ev_charging.home_hours` |

- **Terminal value:** the cheapest import price of the plan, times the
  discharge efficiency, minus 2 % so that the battery rather than the grid
  supplies the cheapest hour; never below zero.
- **Stored value in the car:** the gross price at
  `cheap_market_price_ct_per_kwh` divided by the charging efficiency when it is
  configured, otherwise the cheapest import price of the plan (never below zero).
- **Soft reserve:** `confidence × σ × √(lead hours − unplanned_hours)`, capped
  at `growth_cap_hours`, where σ is `forecast_error_kw` or derived from the
  backtested load forecast error. The first `unplanned_hours` keep no reserve,
  because the plan is recomputed with measured states.

## Solver

- HiGHS stops at `solver.time_limit_seconds` or at the relative MIP gap
  `solver.mip_relative_gap` (default `0.0001`).
- **Statuses:** `optimal`; `feasible` when a limit stopped the search after a
  valid plan was found (it is stored and used like an optimal one);
  `infeasible`; `time_limit` when no plan was found. Only `optimal` and
  `feasible` plans replace the stored plan.

## Glossary

| Term | Meaning |
|---|---|
| kW | Average power over the planned part of an hour |
| kWh | Energy; a kW value times `duration_hours` |
| State of charge | Energy stored at the end of the hour |
| Cost (EUR) | Import cost minus export revenue of the hour: real money |
| Cost (Optimizer) | The hour as the solver values it: cash cost plus wear and penalties minus the value of the energy stored in the hour; not money |
| `objective_eur` | Import cost minus export revenue plus wear over the plan; without penalties and stored-energy credits |
| Published price | Day-ahead price from aWATTar |
| Forecast price | EPEX Predictor forecast after the last published hour |
| Reserve | Soft lower band of the battery against load forecast errors |
| Headroom | Soft upper band kept free while PV is expected |
| Shortfall | Car energy the plan cannot provide for a deadline, a morning minimum or driving |

## Simplifications

- One interval per hour; prices that change within an hour are averaged.
- Forecasts are taken as certain. Uncertainty enters only through the soft
  reserve and headroom and through re-planning.
- Battery and charger efficiencies are constant; losses do not depend on power
  or state of charge.
- The household load does not depend on the outside temperature; the heat
  pump is part of it.
- The battery plan is not executed by the service; only the wall box is
  controlled.
