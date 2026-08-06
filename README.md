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
  energy-optimizer
```

The container listens on port `8000`, runs as a non-root user, and uses
`ENERGY_OPTIMIZER_CONFIG` to select a different configuration path when needed.
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

The health endpoint returns the service status and version, for example:

```json
{"status":"ok","version":"0.1.0"}
```
