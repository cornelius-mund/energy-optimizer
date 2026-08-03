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

The service is expected to contain the following components:

- HTTP API for optimization requests and results
- YAML configuration loading and validation
- Normalized energy and time-series data models
- MILP model construction
- HiGHS solver integration
- Optimization result mapping and diagnostics
- External data provider interfaces

The optimizer should remain independent of specific external data providers. Price and forecast providers should return normalized internal data before their data is passed to the optimization model.

The energy and asset models may change throughout the project as the requirements and understanding of the domain evolve.

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

Runtime and system configuration will be provided through YAML.

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

## Deployment

The service will run in a Docker container.

The project should provide:

- A production Docker image
- A documented container startup command
- Configurable runtime settings
- Health checking
- Reproducible development and test execution

## Data Providers

Prices and forecasts may be supplied through the HTTP API or retrieved from external data providers.

External data provider modules may support:

- Day-ahead electricity prices
- PV generation forecasts
- Weather data

External providers should be configurable and isolated from the optimization model. Provider data must be validated before use.

## Development

The project is currently in the planning and initial setup phase.

Development should follow the ordered product backlog. Each feature should deliver direct user value and include its required implementation, tests, documentation, configuration, and deployment work.

See [`AGENTS.md`](AGENTS.md) for backlog, issue, and engineering process guidelines.
