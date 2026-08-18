"""Shared state and adapters used by API route handlers."""

import logging

from pydantic import TypeAdapter

from energy_optimizer.providers.interfaces import (
    ElectricityPriceData,
    GridFlowData,
    HouseholdLoadData,
    PvGenerationData,
)

logger = logging.getLogger("energy_optimizer.api")

HOUSEHOLD_LOAD_ADAPTER = TypeAdapter(HouseholdLoadData)
GRID_FLOW_ADAPTER = TypeAdapter(GridFlowData)
PV_GENERATION_ADAPTER = TypeAdapter(PvGenerationData)
ELECTRICITY_PRICE_ADAPTER = TypeAdapter(ElectricityPriceData)
