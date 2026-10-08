"""Tests for WatercareCoordinator._calculate_cost.

The real Watercare tariff (as documented in const.py's DEFAULT_* constants):
consumption $2.296/1000L, wastewater $3.994/1000L at a 0.785 ratio, and a
$332/year fixed line charge. Expected figures below were computed
independently (by hand / calculator), not derived from the implementation,
so a wrong formula in _calculate_cost will produce a mismatch here.
"""

from __future__ import annotations

import pytest

from tests.helpers import REAL_TARIFF, make_coordinator


async def test_real_tariff_31_days_10000_litres(hass) -> None:
    """31 days, 10,000L: total $82.51, split 22.96 / 31.35 / 28.20."""
    coordinator = make_coordinator(hass, **REAL_TARIFF)

    result = coordinator._calculate_cost(10000, 31)

    assert round(result["consumption"], 2) == 22.96
    assert round(result["wastewater"], 2) == 31.35
    assert round(result["line_charge"], 2) == 28.20
    assert round(result["total"], 2) == 82.51
    # The total must actually be the sum of the parts, not a separately
    # rounded figure that happens to match.
    assert result["total"] == pytest.approx(
        result["consumption"] + result["wastewater"] + result["line_charge"]
    )


@pytest.mark.parametrize(
    ("days", "expected_line_charge"),
    [
        (365, 332.0),  # a full year recovers exactly the annual charge
        (1, 332 / 365),  # a single day is 1/365th of it
        (31, 332 * 31 / 365),
    ],
)
async def test_line_charge_scales_with_days(hass, days, expected_line_charge) -> None:
    coordinator = make_coordinator(hass, **REAL_TARIFF)

    result = coordinator._calculate_cost(0, days)

    assert result["line_charge"] == pytest.approx(expected_line_charge)


async def test_zero_usage_still_charges_the_fixed_line_charge(hass) -> None:
    """A billing period with no water use is not a free period."""
    coordinator = make_coordinator(hass, **REAL_TARIFF)

    result = coordinator._calculate_cost(0, 31)

    assert result["consumption"] == 0
    assert result["wastewater"] == 0
    assert result["line_charge"] == pytest.approx(332 * 31 / 365)
    assert result["total"] == pytest.approx(result["line_charge"])
    assert result["total"] > 0


async def test_consumption_and_wastewater_are_independent_of_each_other(hass) -> None:
    """A wastewater_rate of 0 must not zero out the consumption cost."""
    coordinator = make_coordinator(
        hass,
        consumption_rate=2.296,
        wastewater_rate=0,
        wastewater_ratio=0.785,
        annual_line_charge=0,
    )

    result = coordinator._calculate_cost(10000, 30)

    assert result["consumption"] == pytest.approx(22.96)
    assert result["wastewater"] == 0
    assert result["total"] == pytest.approx(22.96)
