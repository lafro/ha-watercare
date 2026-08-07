"""Smoke test: confirms the HA test harness can load this custom component."""

from __future__ import annotations


async def test_hass_fixture_boots(hass) -> None:
    """A trivial check that the `hass` fixture itself works."""
    assert hass.state is not None


async def test_watercare_module_importable() -> None:
    """The integration package must be importable as custom_components.watercare."""
    from custom_components.watercare import DOMAIN

    assert DOMAIN == "watercare"
