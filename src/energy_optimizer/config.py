"""Loading and validation of runtime configuration."""

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError


class ConfigurationError(ValueError):
    """Raised when the runtime configuration cannot be used."""


class GridConfiguration(BaseModel):
    """Limits for energy exchanged with the grid, in kW."""

    model_config = ConfigDict(extra="forbid")

    maximum_import_kw: float = Field(gt=0)
    maximum_export_kw: float = Field(gt=0)


class SolverConfiguration(BaseModel):
    """Optimization solver settings."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    time_limit_seconds: float = Field(gt=0)


class Configuration(BaseModel):
    """Validated settings needed to start the service."""

    model_config = ConfigDict(extra="forbid")

    time_resolution_minutes: int = Field(gt=0)
    grid: GridConfiguration
    solver: SolverConfiguration


def load_configuration(path: Path) -> Configuration:
    """Load and validate a YAML configuration file.

    A ``ConfigurationError`` includes the path and the relevant validation
    details so startup failures can be diagnosed without a traceback.
    """
    if not path.is_file():
        raise ConfigurationError(f"Configuration file not found: {path}")

    try:
        with path.open(encoding="utf-8") as configuration_file:
            document: Any = yaml.safe_load(configuration_file)
    except yaml.YAMLError as error:
        message = f"Invalid YAML in configuration file {path}: {error}"
        raise ConfigurationError(message) from error
    except OSError as error:
        message = f"Could not read configuration file {path}: {error}"
        raise ConfigurationError(message) from error

    if not isinstance(document, dict):
        raise ConfigurationError(
            f"Configuration file {path} must contain a YAML mapping at the top level"
        )

    try:
        return Configuration.model_validate(document)
    except ValidationError as error:
        details = "; ".join(
            f"{'.'.join(str(part) for part in issue['loc'])}: {issue['msg']}"
            for issue in error.errors()
        )
        message = f"Invalid configuration in {path}: {details}"
        raise ConfigurationError(message) from error
