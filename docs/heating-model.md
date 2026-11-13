# Building heat storage and heat-source optimization

Researched design for #298, 2026-11-13. This is not an implemented heating
controller. No live Home Assistant settings were changed.

## Confirmed owner requirements

- For the inspected home, the Awair room sensors define a 20-22 degrees C comfort
  window. Do not average an overheated room together with an underheated one to
  declare comfort. Each installation retains its own configurable comfort limits.
- The reported day without heating is observed comfort reserve, not a fixed
  permission or a transferable 24-hour rule for another house.
- The fireplace supplies approximately 6 kW useful heat: one third to the living
  room (2 kW), one thirtieth to the rest of the house (0.2 kW), and the remainder
  to water (3.8 kW). Cost is 0.12 EUR per useful kWh including efficiency losses;
  do not apply a second fuel-efficiency correction.
- The requested first outcome is recommendations only, with no actuator writes.

## Evidence from the connected installation

Authenticated read-only REST requests inspected entity metadata and a bounded
48-hour history ending around 2026-11-13 15:39 UTC. Credentials and raw household
history are excluded. Only one installation was available; cmund's other house
has not been measured in this investigation.

| Purpose | Observed entity | Interpretation |
| --- | --- | --- |
| Outdoor air | `sensor.aussen_2` | Ambient temperature in degrees C. Verify placement. |
| Outdoor unit | `sensor.altherma_r1t_outdoor_air_temp` | Secondary ambient measurement, potentially a different microclimate. |
| Stable rooms | `sensor.awair_wohnzimmer_temperatur`, `sensor.awair_buero_temperatur` | Confirmed Awair comfort sources; enforce room bounds rather than averaging away violations. |
| Aggregate rooms | `sensor.min_indoor_temp`, `sensor.max_indoor_temp` | Diagnostics; a changing minimum/maximum room identity is unsuitable for identifying thermal capacitance. |
| Heating heat | `sensor.espaltherma_unit_meter_heat_produced_space_heating_kwh` | Cumulative kWh thermal; accuracy and device estimation remain unverified. |
| Heating electricity | `sensor.espaltherma_unit_meter_electricity_used_space_heating_kwh` | Cumulative kWh electric, separate from domestic hot water. |
| Whole heat-pump electricity | `sensor.waermepumpe_energy` | Already configured as appliance history; a broader electrical system boundary. |
| Supply/return water | `sensor.altherma_leaving_water_temp_before_buh_r1t`, `sensor.altherma_inlet_water_temp_r4t` | COP-map inputs and diagnostics. |
| Water flow | `sensor.altherma_flow_sensor_l_min` | l/min; cross-check heat from flow, water heat capacity and temperature difference. |
| Modes | `sensor.altherma_operation_mode`, `sensor.altherma_defrost_operation` | Separate space heating, hot water and defrost. |
| Hot-water meters | `sensor.espaltherma_unit_meter_heat_produced_hot_water_kwh`, `sensor.espaltherma_unit_meter_electricity_used_hot_water_kwh` | Separate thermal load/storage, not room heating. |
| Backup electricity | `sensor.espaltherma_unit_meter_electricity_used_backup_heater_kwh` | Verify inclusion in other meters before summing. |
| Storage temperatures | `sensor.shellyunispeichertemperaturen_ds18b20_1_temperature` through `_8_temperature` | Tank identity, sensor positions, volume and hydraulic role need confirmation. |
| Fireplace indication | `sensor.kaminpumpe_leistungsfaktor` | Pump electrical power factor does not measure delivered heat. |

Sample observations, not calibrated parameters:

A follow-up Awair inspection around 15:53 UTC used the following stable room
identities and the preceding 48 hours. Ranges are observations, not permitted
comfort ranges or identified building parameters:

| Room | Sensor | Current C | Observed range C | Invalid observations |
| --- | --- | --- | --- | --- |
| Office | `sensor.awair_buero_temperatur` | 22.2 | 15.1-22.7 | 1 |
| Room A | `sensor.awair_element_78503_temperatur` | 22.4 | 18.1-23.1 | 2 |
| Bathroom | `sensor.awair_element_83389_temperatur` | 22.8 | 20.2-23.3 | 0 |
| Room B | `sensor.awair_element_82469_temperatur` | 22.3 | 18.4-23.6 | 2 |
| Bedroom | `sensor.awair_element_52111_temperatur` | 20.6 | 16.6-20.9 | 1 |
| Living room | `sensor.awair_wohnzimmer_temperatur` | 24.2 | 17.9-25.9 | 3 |

These readings already include rooms above 22 C. Abrupt low readings need window,
placement and quality checks before fitting losses. Do not interpret counts of
changed observations as percentages of time; time-weighted alignment is needed.
Use explicit out-of-band initial-state reporting and a physically achievable
recovery trajectory. An optimizer must not instantly cool an already warm room
to 22 C, silently relax its comfort band, or recommend more fireplace heat there
while it remains above the comfort ceiling.

The earlier bounded meter/weather inspection found:

- Outdoor range -0.50 to 9.52 degrees C: 210 observations without invalid states.
- Space-heating counter increases: 52.374 kWh thermal and 11.820 kWh electric,
  with no counter decreases but two invalid observations in each series. Their
  approximate ratio is 4.43. Timestamp alignment and invalid intervals must be
  resolved before treating it as an effective COP.
- Whole heat-pump electricity increased by about 14.3935 kWh. The difference is
  not automatically loss: hot water, auxiliaries and meter boundaries differ.
- Aggregate minimum-room range 17.51-20.38 degrees C; aggregate maximum-room
  range 18.98-24.65 degrees C, with invalid observations. This is not a controlled
  cooldown experiment.
- Supply range 16.3-49.2 degrees C and return 20.8-45.6 degrees C, with invalid
  observations. Mixing hot-water and room-heating modes would corrupt a COP fit.

The existing `sensor.altherma_cop*` series use percent units; 459.14% would mean
4.5914 only if the computation is verified. Some short-window readings were
negative or near zero during idle operation. Prefer aligned counter differences
over averaging instantaneous ratios. Retain signed heat during defrost and its
electrical cost instead of discarding inconvenient negative samples.

Existing controls include `input_boolean.wp_leistungsregelung_aktiv`,
`input_number.wp_zielleistung_w` (observed range 1500-9000 W), a supply limit and
`climate.altherma_leaving_water_offset`. These are candidates, not permission to
write them. Verify whether target power is electrical or thermal. Current appliance
configuration declares a 3 kW electrical maximum and discrete fractions
[0, 0.3, 0.6, 1]; validate both device limits and controller semantics before use.

## Physics and per-house parameters

Cold weather increases heat loss. For an air-source heat pump, heating COP
generally decreases with lower outdoor temperature or higher required supply
temperature. Defrost and part-load effects matter too. Model heat demand and
efficiency separately rather than assuming cold weather raises COP.

Use installation-specific heat-loss coefficient H, useful capacitance C, initial
thermal state, gains and uncertainty. A well-insulated house may have low H and
large C; a poorly insulated house can lose useful stored heat much faster. The
time constant C/H and permitted temperature band determine flexibility. The
reported ability to coast for a day must not become a universal 24-hour rule.

For a single-zone installation a basic thermal balance is:

```text
C * (T_inside[t+1] - T_inside[t]) / dt
  = Q_heat_pump[t] + f_room * Q_fireplace[t]
    + Q_internal[t] + Q_solar[t]
    - H * (T_inside[t] - T_outside[t])

Q_heat_pump[t] = COP[t] * P_heat_pump[t]
T_min[t] <= T_inside[t] <= T_max[t]
```

C: kWh thermal/K. H: kW thermal/K. dt: hours. Q: kW thermal. P: kW electric.
Temperatures: degrees C. Use exact discretization or validate the hourly
approximation for fast-cooling houses; unstable forward Euler must not manufacture
flexibility. Add a justified terminal temperature band or terminal heat value so
every rolling horizon does not end with an artificially cold house.

When air and building mass respond differently, use a second state:

```text
C_air * dT_air/dt = delivered_heat + gains
                    + H_mass * (T_mass - T_air)
                    - H_envelope * (T_air - T_outside)
C_mass * dT_mass/dt = H_mass * (T_air - T_mass)
```

For this installation, the minimum useful fireplace model must have at least a
living-room zone, other room zones and a water state. Track each Awair room's
comfort; rooms may share a calibrated mass model only if their individual
temperature predictions remain valid. The per-zone equations include inter-room
heat exchange and independently configured heat-loss/capacitance parameters.
Use the confirmed fireplace split explicitly:

```text
Q_fireplace_living = (1/3) * Q_fireplace_total
Q_fireplace_other_rooms = (1/30) * Q_fireplace_total
Q_fireplace_water = (19/30) * Q_fireplace_total
0 <= Q_fireplace_total <= 6 kW thermal
```

Do not distribute the 0.2 kW rest-of-house share to each room: it is the total
share for those rooms together. Its distribution needs identification. Confirm
whether the water destination is domestic hot water or a heating buffer that
can later supply rooms. Until that hydraulic mapping is known, do not count the
3.8 kW as room heating or assume a drinking-water demand can absorb it. Water
storage needs measured initial state, volume, capacity, losses and upper limits.

Fit each house independently using stable room sensors, multiple weeks of varied
weather and operating conditions, and held-out prediction tests. Identify stove
use, ventilation/open windows, solar gains and hot-water operation where possible.
Two days cannot identify H and C reliably or establish a safe one-day heating
pause. If data are insufficient, begin with commissioned/owner-supplied parameters,
conservative uncertainty and advisory output. Report temperature prediction errors.

## Heat pump, fireplace and joint economics

Use a measured/manufacturer COP map by outdoor and supply temperature, separate
for heating and hot water. Initially supply bounded hourly COP values to the
solver. If supply temperature or compressor points become decisions, use validated
operating bands with electrical-input/thermal-output pairs rather than multiplying
two free decision variables. Include output/input limits, operating modes, defrost,
minimum on/off durations, starts and backup heat where the equipment requires them.

The confirmed fireplace values are approximately 6 kW useful heat and 0.12 EUR
per useful kWh, already including efficiency losses. Represent attendance,
allowed hours, batch size and minimum burn time.
Treat lighting/refilling as a manual recommendation requiring confirmation; do
not assume instantaneous starts, arbitrary modulation or interruptible burns.

For intuition, grid-supplied heat costs approximately electricity price / COP.
At 0.35 EUR/kWh electric and COP 4 this is 0.0875 EUR/kWh thermal, below 0.12.
At COP 2 it is 0.175, above 0.12. The crossover is COP = price / 0.12, about
2.92 at 35 ct. These are illustrative comparisons, not complete dispatch rules:
storage losses, future prices, comfort and manual availability also matter.
The 12 ct comparison assumes all heat fractions are usefully absorbed. At full
output the fireplace costs 0.72 EUR/hour, but only 2.2 kW directly heats rooms.
If the water heat cannot be used, that is about 32.7 ct per directly delivered
room-heating kWh, before considering room overheating. The joint model must
value each usable fraction once; it must not credit unusable water heat as savings.

Optimize both sources inside the joint household problem. Heat-pump power enters
the electrical balance; useful heat enters the thermal balance; fireplace fuel
cost enters the objective. PV has an export opportunity cost and battery energy
has losses and future value. Do not charge heat-pump electricity separately on top
of the grid import cost already paying for it.

The live heat-pump appliance has `included_in_household_load: true`. Before adding
scheduled heating electricity, remove its measured contribution from household
history used to forecast unmanaged load, then add scheduled consumption once.
Validate meter boundaries and gaps to avoid negative residuals. Do not retain a
weather-dependent household forecast containing heating and add a full independent
heat-pump load on top.

## Smallest valuable increments

1. Advisory scheduling with per-house/per-zone H/C, measured Awair temperatures,
   hourly weather forecast, 20-22 C room bounds for the inspected home, validated
   COP inputs and device limits. Separate baseline electrical load first.
2. A manual fireplace with the confirmed 2/0.2/3.8 kW distribution and useful
   heat cost, after attendance, burn duration and water storage are mapped. Show
   source schedules, per-room temperatures, costs, comfort margins and reasons
   while normal local heating stays in charge.
3. Per-installation calibration and backtesting. Add a second thermal state/zone
   when evidence requires it, rather than assuming both houses behave alike.
4. Automatic control is outside the current request. Only a separate later owner
   request and adequate held-out results could authorize actuator integration.

Test high-loss/low-flexibility and low-loss/high-capacitance homes, cold forecasts,
low COP, price spikes, PV, unavailable stove, room overheating, missing sensors,
infeasible comfort and horizon-end depletion. Compare with the unchanged baseline
controller under the same weather and comfort assumptions.

## Collected questions

1. Who can light/refill the fireplace, in which time windows, and what is its
   minimum batch burn duration? Its useful cost and heat split are now confirmed.
2. Does the 3.8 kW water share feed domestic hot water or a heating buffer? Which
   tanks, volumes, hydraulic connections, temperature limits and sensor positions
   apply? Can the water heat subsequently supply rooms?
3. Which room, heat and electricity measurements exist at cmund's other house?
   Which meters include pumps, backup and hot water? Are heat counters calibrated
   or device estimates, and is the configured 3 kW electrical maximum accurate?

These answers gate calibrated dispatch and automatic control, not this inventory
and design. No heating control or fitted building coefficients are claimed here.

## Sources

- [NREL cold-climate heat-pump field validation](https://heatpumpdata.energy.gov/system/files/files/cchp-2024-09/Winkler%20Ramaraj%20-%202023%20-%20Field%20Validation%20of%20Air-Source%20Heat%20Pumps%20for%20Cold%20Climates.pdf): operating-condition-dependent measured COP.
- [ENERGY STAR product criteria](https://www.energystar.gov/products/air_source_heat_pumps/key-product-criteria): COP definition and electrical/thermal distinction.
- [Home Assistant REST API](https://contributors.home-assistant.io/core/internals/rest-api/): authenticated state and bounded history retrieval.
- [Home Assistant History](https://www.home-assistant.io/integrations/history/): retained raw history and long-term statistics differ in coverage and access.
