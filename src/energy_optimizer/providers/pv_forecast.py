"""Select the configured PV forecast provider."""

from energy_optimizer.config import Configuration
from energy_optimizer.providers.forecast_solar import ForecastSolarImporter
from energy_optimizer.providers.interfaces import PV_GENERATION_SOURCE_ID
from energy_optimizer.providers.open_meteo_pv import OpenMeteoPvImporter
from energy_optimizer.storage import ProviderDataKey

type PvForecastImporter = ForecastSolarImporter | OpenMeteoPvImporter


def configured_pv_forecast_importer(
    configuration: Configuration,
) -> PvForecastImporter | None:
    """Return the importer of the one configured PV forecast provider, if any."""
    if configuration.forecast_solar is not None:
        return ForecastSolarImporter(configuration.forecast_solar)
    if configuration.open_meteo_pv is not None:
        return OpenMeteoPvImporter(configuration.open_meteo_pv)
    return None


def pv_forecast_key(importer: PvForecastImporter) -> ProviderDataKey:
    return ProviderDataKey("pv-generation", importer.provider, PV_GENERATION_SOURCE_ID)
