"""Loading and validation of runtime configuration."""

from pathlib import Path
from typing import Any

import yaml
from pydantic import (
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)


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


class HomeAssistantConfiguration(BaseModel):
    """Connection and mapping settings for the Home Assistant provider."""

    model_config = ConfigDict(extra="forbid")

    base_url: AnyHttpUrl
    token: SecretStr
    household_load_entity_id: str = Field(min_length=1, max_length=255)
    timeout_seconds: float = Field(gt=0, le=120)
    max_data_age_seconds: float | None = Field(default=None, gt=0)


class PersistenceConfiguration(BaseModel):
    """Filesystem location for normalized provider data."""

    model_config = ConfigDict(extra="forbid")

    directory: Path = Field(
        default=Path("/var/lib/energy-optimizer/provider-data"),
        description="Directory containing persisted normalized provider data",
    )


class DataSourceScheduleConfiguration(BaseModel):
    """Polling and requested-period settings for one data source."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    interval_seconds: float = Field(gt=0)
    horizon_hours: int = Field(default=24, gt=0, le=168)
    history_lookback_seconds: float = Field(default=0, ge=0)


class OptimizationTriggerConfiguration(BaseModel):
    """Settings controlling automatic plan-generation triggers."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    required_sources: list[str] = Field(default_factory=list)

    @field_validator("required_sources")
    @classmethod
    def validate_required_sources(cls, values: list[str]) -> list[str]:
        """Reject empty and duplicate source names in the plan input set."""
        if any(not source.strip() for source in values):
            raise ValueError("required_sources must contain non-empty names")
        if len(values) != len(set(values)):
            raise ValueError("required_sources must not contain duplicates")
        return values


class OrchestrationConfiguration(BaseModel):
    """Runtime settings for scheduled provider retrieval and plan triggers."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    startup_fetch: bool = True
    sources: dict[str, DataSourceScheduleConfiguration] = Field(default_factory=dict)
    optimization: OptimizationTriggerConfiguration = Field(
        default_factory=OptimizationTriggerConfiguration
    )

    @model_validator(mode="after")
    def validate_optimization_sources(self) -> "OrchestrationConfiguration":
        """Require plan inputs to be configured when automatic planning is enabled."""
        if any(not source.strip() for source in self.sources):
            raise ValueError("orchestration source names must not be empty")
        if self.optimization.enabled and not self.optimization.required_sources:
            raise ValueError(
                "optimization.required_sources must not be empty when optimization "
                "triggers are enabled"
            )
        missing = set(self.optimization.required_sources) - set(self.sources)
        if missing:
            names = ", ".join(sorted(missing))
            raise ValueError(
                f"optimization.required_sources are not configured as sources: {names}"
            )
        disabled = {
            source
            for source in self.optimization.required_sources
            if not self.sources[source].enabled
        }
        if disabled:
            names = ", ".join(sorted(disabled))
            raise ValueError(f"optimization.required_sources must be enabled: {names}")
        return self


class Configuration(BaseModel):
    """Validated settings needed to start the service."""

    model_config = ConfigDict(extra="forbid")

    time_resolution_minutes: int = Field(gt=0)
    grid: GridConfiguration
    solver: SolverConfiguration
    home_assistant: HomeAssistantConfiguration | None = None
    persistence: PersistenceConfiguration | None = None
    orchestration: OrchestrationConfiguration | None = None

    @model_validator(mode="after")
    def validate_orchestration_persistence(self) -> "Configuration":
        """Ensure scheduled collection has the durable store it promises to use."""
        if (
            self.orchestration is not None
            and self.orchestration.enabled
            and self.persistence is None
        ):
            raise ValueError("persistence is required when orchestration is enabled")
        return self

    def is_configured_household_load_source(
        self,
        provider: str,
        entity_id: str | None,
    ) -> bool:
        """Return whether a source identifies the configured load provider."""
        return (
            self.home_assistant is not None
            and provider == "home-assistant"
            and entity_id == self.home_assistant.household_load_entity_id
        )


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
