"""Loading and validation of runtime configuration."""

import logging
from pathlib import Path
from typing import Any, Literal

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

from energy_optimizer.providers.interfaces import (
    BATTERY_SOURCE_ID,
    GRID_FLOW_SOURCE_ID,
    HOUSEHOLD_LOAD_SOURCE_ID,
    PV_GENERATION_SOURCE_ID,
)

logger = logging.getLogger(__name__)


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


class HomeAssistantEnergyEntityConfiguration(BaseModel):
    """Configuration for one Home Assistant cumulative-energy entity."""

    model_config = ConfigDict(extra="forbid")

    entity_id: str = Field(min_length=1, max_length=255)
    state_class: Literal["total", "total_increasing"]
    unit: Literal["Wh", "kWh", "MWh"]
    operation: Literal["add", "subtract"]
    maximum_interval_energy_kwh: float = Field(default=100, gt=0)


# Preserve the existing configuration name for integrations importing it directly.
HouseholdLoadEntityConfiguration = HomeAssistantEnergyEntityConfiguration


class HomeAssistantBatteryEntityConfiguration(BaseModel):
    """Configuration for one instantaneous Home Assistant battery value."""

    model_config = ConfigDict(extra="forbid")

    entity_id: str = Field(min_length=1, max_length=255)
    unit: Literal["%", "Wh", "kWh", "W", "kW", "ratio"]
    attribute: str | None = Field(default=None, min_length=1, max_length=255)


class HomeAssistantBatteryConfiguration(BaseModel):
    """Home Assistant entity mappings for battery state and capabilities."""

    model_config = ConfigDict(extra="forbid")

    state_of_charge: HomeAssistantBatteryEntityConfiguration
    capacity: HomeAssistantBatteryEntityConfiguration
    minimum_soc: HomeAssistantBatteryEntityConfiguration
    maximum_soc: HomeAssistantBatteryEntityConfiguration
    maximum_charge: HomeAssistantBatteryEntityConfiguration
    maximum_discharge: HomeAssistantBatteryEntityConfiguration
    charge_efficiency: HomeAssistantBatteryEntityConfiguration
    discharge_efficiency: HomeAssistantBatteryEntityConfiguration

    @model_validator(mode="after")
    def validate_mapping_units(self) -> "HomeAssistantBatteryConfiguration":
        """Require units compatible with each normalized battery field."""
        energy_fields = {
            "state_of_charge": self.state_of_charge,
            "minimum_soc": self.minimum_soc,
            "maximum_soc": self.maximum_soc,
        }
        for name, mapping in energy_fields.items():
            if mapping.unit not in {"%", "Wh", "kWh"}:
                raise ValueError(
                    f"battery.{name} must use %, Wh, or kWh; got {mapping.unit}"
                )
        if self.capacity.unit not in {"Wh", "kWh"}:
            raise ValueError("battery.capacity must use Wh or kWh")
        for name, mapping in {
            "maximum_charge": self.maximum_charge,
            "maximum_discharge": self.maximum_discharge,
        }.items():
            if mapping.unit not in {"W", "kW"}:
                raise ValueError(f"battery.{name} must use W or kW")
        for name, mapping in {
            "charge_efficiency": self.charge_efficiency,
            "discharge_efficiency": self.discharge_efficiency,
        }.items():
            if mapping.unit not in {"%", "ratio"}:
                raise ValueError(f"battery.{name} must use % or ratio")
        return self


class HomeAssistantConfiguration(BaseModel):
    """Connection and energy mappings for Home Assistant."""

    model_config = ConfigDict(extra="forbid")

    base_url: AnyHttpUrl
    token: SecretStr
    household_load_entities: list[HomeAssistantEnergyEntityConfiguration] | None = (
        Field(default=None, min_length=1)
    )
    household_load_entity_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=255,
        description="Legacy single-entity setting; migrate to household_load_entities",
    )
    grid_import_entities: list[HomeAssistantEnergyEntityConfiguration] | None = Field(
        default=None, min_length=1
    )
    grid_export_entities: list[HomeAssistantEnergyEntityConfiguration] | None = Field(
        default=None, min_length=1
    )
    battery: HomeAssistantBatteryConfiguration | None = None
    timeout_seconds: float = Field(gt=0, le=120)
    max_data_age_seconds: float | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def validate_energy_entities(self) -> "HomeAssistantConfiguration":
        """Apply the legacy mapping and reject duplicate physical entities."""
        if self.household_load_entities is None:
            if self.household_load_entity_id is not None:
                self.household_load_entities = [
                    HomeAssistantEnergyEntityConfiguration(
                        entity_id=self.household_load_entity_id,
                        state_class="total_increasing",
                        unit="kWh",
                        operation="add",
                    )
                ]
        elif self.household_load_entity_id is not None:
            raise ValueError(
                "configure household_load_entities instead of the legacy "
                "household_load_entity_id"
            )

        grid_entity_sets = (self.grid_import_entities, self.grid_export_entities)
        if any(entities is not None for entities in grid_entity_sets) and not all(
            entities is not None for entities in grid_entity_sets
        ):
            raise ValueError(
                "grid_import_entities and grid_export_entities must be configured "
                "together"
            )

        entity_ids = [
            entity.entity_id
            for entities in (
                self.household_load_entities,
                self.grid_import_entities,
                self.grid_export_entities,
            )
            for entity in entities or []
        ]
        if len(entity_ids) != len(set(entity_ids)):
            raise ValueError(
                "Home Assistant energy entities must not contain duplicates"
            )
        if self.battery is not None:
            battery_mappings = (
                self.battery.state_of_charge,
                self.battery.capacity,
                self.battery.minimum_soc,
                self.battery.maximum_soc,
                self.battery.maximum_charge,
                self.battery.maximum_discharge,
                self.battery.charge_efficiency,
                self.battery.discharge_efficiency,
            )
            mapping_keys = [
                (mapping.entity_id, mapping.attribute) for mapping in battery_mappings
            ]
            if len(mapping_keys) != len(set(mapping_keys)):
                raise ValueError(
                    "Home Assistant battery mappings must not reuse the same "
                    "entity and attribute"
                )
        return self

    @property
    def household_load_source_id(self) -> str:
        """Return the single persistence identity for the aggregate dataset."""
        return HOUSEHOLD_LOAD_SOURCE_ID

    @property
    def grid_flow_source_id(self) -> str:
        """Return the single persistence identity for grid-flow data."""
        return GRID_FLOW_SOURCE_ID

    @property
    def battery_source_id(self) -> str:
        """Return the stable persistence identity for battery data."""
        return BATTERY_SOURCE_ID


class ForecastSolarConfiguration(BaseModel):
    """Free public Forecast.Solar installation and request settings."""

    model_config = ConfigDict(extra="forbid", validate_default=True)

    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    declination_degrees: float = Field(ge=0, le=90)
    azimuth_degrees: float = Field(ge=-180, le=180)
    peak_power_kw: float = Field(gt=0)
    base_url: AnyHttpUrl = AnyHttpUrl("https://api.forecast.solar")
    timeout_seconds: float = Field(default=10, gt=0, le=120)
    max_data_age_seconds: float | None = Field(default=7200, gt=0)

    @property
    def pv_generation_source_id(self) -> str:
        """Return the stable identity for this installation's forecast."""
        return PV_GENERATION_SOURCE_ID


class PersistenceConfiguration(BaseModel):
    """Filesystem location for normalized provider data."""

    model_config = ConfigDict(extra="forbid")

    directory: Path = Field(
        default=Path("/var/lib/energy-optimizer/provider-data"),
        description="Directory containing persisted normalized provider data",
    )


class DataSourceScheduleConfiguration(BaseModel):
    """Polling and provider-history settings for one data source."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    interval_seconds: float = Field(gt=0)
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
    forecast_solar: ForecastSolarConfiguration | None = None
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
            and self.home_assistant.household_load_entities is not None
            and provider == "home-assistant"
            and entity_id == self.home_assistant.household_load_source_id
        )

    def is_configured_grid_flow_source(
        self,
        provider: str,
        entity_id: str | None,
    ) -> bool:
        """Return whether a source identifies the configured grid-flow provider."""
        return (
            self.home_assistant is not None
            and self.home_assistant.grid_import_entities is not None
            and self.home_assistant.grid_export_entities is not None
            and provider == "home-assistant"
            and entity_id == self.home_assistant.grid_flow_source_id
        )

    def is_configured_battery_source(
        self,
        provider: str,
        entity_id: str | None,
    ) -> bool:
        """Return whether a source identifies the configured battery provider."""
        return (
            self.home_assistant is not None
            and self.home_assistant.battery is not None
            and provider == "home-assistant"
            and entity_id == self.home_assistant.battery_source_id
        )


def load_configuration(path: Path) -> Configuration:
    """Load and validate a YAML configuration file.

    A ``ConfigurationError`` includes the path and the relevant validation
    details so startup failures can be diagnosed without a traceback.
    """
    logger.debug(
        "event=configuration_load_started component=configuration operation=load "
        "path=%s",
        path,
    )
    if not path.is_file():
        logger.error(
            "event=configuration_load_failed component=configuration operation=load "
            "path=%s error_type=ConfigurationError",
            path,
        )
        raise ConfigurationError(f"Configuration file not found: {path}")

    try:
        with path.open(encoding="utf-8") as configuration_file:
            document: Any = yaml.safe_load(configuration_file)
    except yaml.YAMLError as error:
        message = f"Invalid YAML in configuration file {path}: {error}"
        logger.error(
            "event=configuration_load_failed component=configuration operation=load "
            "path=%s error_type=YAMLError",
            path,
        )
        raise ConfigurationError(message) from error
    except OSError as error:
        message = f"Could not read configuration file {path}: {error}"
        logger.error(
            "event=configuration_load_failed component=configuration operation=load "
            "path=%s error_type=OSError",
            path,
        )
        raise ConfigurationError(message) from error

    if not isinstance(document, dict):
        logger.error(
            "event=configuration_load_failed component=configuration operation=load "
            "path=%s error_type=ConfigurationError",
            path,
        )
        raise ConfigurationError(
            f"Configuration file {path} must contain a YAML mapping at the top level"
        )

    try:
        configuration = Configuration.model_validate(document)
    except ValidationError as error:
        details = "; ".join(
            f"{'.'.join(str(part) for part in issue['loc'])}: {issue['msg']}"
            for issue in error.errors()
        )
        message = f"Invalid configuration in {path}: {details}"
        logger.error(
            "event=configuration_load_failed component=configuration operation=load "
            "path=%s error_type=ValidationError",
            path,
        )
        raise ConfigurationError(message) from error
    logger.info(
        "event=configuration_loaded component=configuration operation=load "
        "path=%s persistence_enabled=%s orchestration_enabled=%s "
        "home_assistant_enabled=%s",
        path,
        configuration.persistence is not None,
        configuration.orchestration is not None and configuration.orchestration.enabled,
        configuration.home_assistant is not None,
    )
    return configuration
