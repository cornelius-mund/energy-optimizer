"""Tests for reusable API validation helpers."""

import math
from datetime import datetime, timezone

import pytest

from energy_optimizer.api.validation import (
    replace_non_finite_values,
    require_aware_timestamp,
    require_aware_timestamps,
)

AWARE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_replace_non_finite_values_handles_scalars_and_lists() -> None:
    assert replace_non_finite_values(
        [1.0, float("nan"), float("inf"), float("-inf")]
    ) == [1.0, None, None, None]
    assert replace_non_finite_values(float("nan")) is None
    assert replace_non_finite_values(1.0) == 1.0


def test_require_aware_timestamps_accepts_aware_values() -> None:
    assert require_aware_timestamp(AWARE) == AWARE
    assert require_aware_timestamps([AWARE, AWARE]) == [AWARE, AWARE]


@pytest.mark.parametrize(
    "value",
    [datetime(2026, 1, 1), [AWARE, datetime(2026, 1, 1)]],
)
def test_require_aware_timestamps_rejects_naive_values(
    value: datetime | list[datetime],
) -> None:
    with pytest.raises(ValueError, match="timestamps must include a timezone"):
        require_aware_timestamps(value)

    if isinstance(value, datetime):
        with pytest.raises(ValueError, match="timestamps must include a timezone"):
            require_aware_timestamp(value)


def test_replace_non_finite_values_does_not_recurse_into_other_objects() -> None:
    value = {"number": math.inf}
    assert replace_non_finite_values(value) is value
