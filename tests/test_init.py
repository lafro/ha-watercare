"""Tests for setup, unload, the entry version guard and the coordinator."""

from __future__ import annotations

from collections.abc import Generator
from datetime import UTC, date, datetime
from decimal import Decimal
from logging import DEBUG, ERROR, WARNING
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.const import CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from sqlalchemy.exc import OperationalError

from custom_components.watercare import async_migrate_entry
from custom_components.watercare.api import (
    WatercareApi,
    WatercareAuthError,
    WatercareConnectionError,
    WatercareMaintenanceError,
    WatercareUpgradeRequiredError,
    _default_sign_in_session,
)
from custom_components.watercare.const import (
    ALL_STATISTIC_IDS,
    DOMAIN,
    STAT_CONSUMPTION,
    STAT_CONSUMPTION_COST,
    STAT_TOTAL_COST,
    STAT_WASTEWATER_COST,
)
from custom_components.watercare.models import AccountSummary, parse_billing_periods
from custom_components.watercare.statistics import bill_cost, day_start
from custom_components.watercare.tariffs import PUBLISHED_TARIFFS, TariffSchedule

from .common import (
    ACCOUNT_NUMBER,
    ACCOUNT_PAYLOAD,
    EMAIL,
    METER_NUMBER,
    SECOND_ACCOUNT_NUMBER,
    SECOND_METER_NUMBER,
    THIRD_ACCOUNT_NUMBER,
    api_period,
    api_transaction,
    default_periods,
    default_transactions,
    make_entry,
    properties_payload,
    stored_rows,
)

# The exception messages as a setup retry shows them: Home Assistant drops
# a message's final full stop.
SMART_METER_MESSAGE = (
    "Watercare reports a smart meter at this property, which this version of "
    "the integration cannot read yet"
)
PROPERTY_NOT_LISTED_MESSAGE = "Watercare no longer lists this property on your login"


async def _setup(hass: HomeAssistant, entry: Any) -> None:
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    # The statistics update runs in the background after setup.
    await hass.async_block_till_done(wait_background_tasks=True)


async def _refresh(hass: HomeAssistant, coordinator: Any) -> None:
    """Poll, then let the statistics update that follows finish."""
    await coordinator.async_refresh()
    await hass.async_block_till_done(wait_background_tasks=True)


async def _reload(hass: HomeAssistant, entry: Any) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_setup_and_unload(
    ha: HomeAssistant, mock_api: dict[str, AsyncMock]
) -> None:
    entry = make_entry()
    await _setup(ha, entry)

    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data.data.period_count == 3
    # Sign-in sessions come from Home Assistant's helper (session.py).
    assert entry.runtime_data.api._sign_in_session is not _default_sign_in_session
    assert ha.states.get("sensor.watercare_last_bill_usage").state == "13000"

    assert await ha.config_entries.async_unload(entry.entry_id)
    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_setup_without_credentials_fails(ha: HomeAssistant) -> None:
    entry = make_entry(data={CONF_USERNAME: EMAIL})
    await _setup(ha, entry)
    assert entry.state is ConfigEntryState.SETUP_ERROR


async def test_refresh_token_is_persisted(
    ha: HomeAssistant, mock_api: dict[str, AsyncMock]
) -> None:
    entry = make_entry()
    await _setup(ha, entry)

    callback = entry.runtime_data.api._token_callback
    assert callback is not None
    callback("rotated-token")

    assert entry.data["refresh_token"] == "rotated-token"
    assert entry.state is ConfigEntryState.LOADED


@pytest.mark.parametrize(
    ("error", "state"),
    [
        (WatercareAuthError("no"), ConfigEntryState.SETUP_ERROR),
        (WatercareConnectionError("down"), ConfigEntryState.SETUP_RETRY),
    ],
)
async def test_setup_errors(
    ha: HomeAssistant,
    mock_api: dict[str, AsyncMock],
    error: Exception,
    state: ConfigEntryState,
) -> None:
    mock_api["account"].side_effect = error
    entry = make_entry()
    await _setup(ha, entry)

    assert entry.state is state
    flows = ha.config_entries.flow.async_progress()
    reauth = [flow for flow in flows if flow["context"]["source"] == SOURCE_REAUTH]
    assert bool(reauth) is isinstance(error, WatercareAuthError)


@pytest.mark.parametrize(
    ("payload", "key"),
    [
        ({"usage": []}, "unexpected_response"),
        ([], "no_billing_periods"),
        # Only unfinished: a new account before its first bill.
        ([api_period(date(2026, 9, 3), date(2099, 10, 3), 9000)], "no_billing_periods"),
        # Bills Watercare sent but none could be read: not "no bill yet".
        ([{"junk": True}], "unexpected_response"),
        (
            [{"junk": True}, api_period(date(2026, 9, 3), date(2099, 10, 3), 9000)],
            "unexpected_response",
        ),
    ],
)
async def test_unusable_billing_data_retries(
    ha: HomeAssistant,
    mock_api: dict[str, AsyncMock],
    caplog: pytest.LogCaptureFixture,
    payload: Any,
    key: str,
) -> None:
    mock_api["periods"].return_value = payload
    entry = make_entry()
    await _setup(ha, entry)
    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert entry.error_reason_translation_key == key
    # The retry's reason says why; no per-period warning repeats each retry.
    assert _warnings(caplog) == []


def _warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    """Return the integration's own warnings and errors."""
    return [
        record.getMessage()
        for record in caplog.records
        if record.name.startswith("custom_components.watercare")
        and record.levelno >= WARNING
    ]


async def test_unfinished_and_duplicate_periods_are_left_out_quietly(
    ha: HomeAssistant,
    mock_api: dict[str, AsyncMock],
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    freezer.move_to("2026-10-08T12:00:00+13:00")
    periods = default_periods()
    ends_today = api_period(date(2026, 9, 3), date(2026, 10, 8), 9000)
    far_future = api_period(date(2026, 9, 3), date(2026, 10, 3), 9000)
    far_future["billingPeriodToDate"] = "9999-12-31T00:00:00Z"
    # No Auckland date at all: a placeholder, not an unreadable period.
    placeholder = api_period(date(2026, 9, 3), date(2026, 10, 3), 9000)
    placeholder["billingPeriodToDate"] = "9999-12-31T23:59:59Z"
    mock_api["periods"].return_value = [
        *periods,
        periods[0],
        ends_today,
        far_future,
        placeholder,
    ]
    entry = make_entry()
    await _setup(ha, entry)

    data = entry.runtime_data.data
    assert (data.unfinished_periods, data.unreadable_periods) == (3, 0)
    assert data.duplicate_periods == 1
    assert data.latest_period.end == date(2026, 9, 2)
    # A period left out on every poll must not fill the log.
    assert _warnings(caplog) == []
    assert (
        "Left out 3 billing periods that end today or later and 1 duplicates"
        in caplog.text
    )


async def test_unreadable_periods_are_reported_once(
    ha: HomeAssistant,
    mock_api: dict[str, AsyncMock],
    caplog: pytest.LogCaptureFixture,
) -> None:
    mock_api["periods"].return_value = [*default_periods(), {"junk": True}]
    entry = make_entry()
    await _setup(ha, entry)
    coordinator = entry.runtime_data

    assert coordinator.data.unreadable_periods == 1
    warning = (
        "Could not read {} of the billing periods Watercare sent; their days "
        "have no water use in the statistics"
    )
    assert _warnings(caplog) == [warning.format(1)]

    # Not again for the same period on every poll, but again for another.
    await _refresh(ha, coordinator)
    assert _warnings(caplog) == [warning.format(1)]
    mock_api["periods"].return_value = [*default_periods(), {}, {"junk": True}]
    await _refresh(ha, coordinator)
    assert _warnings(caplog) == [warning.format(1), warning.format(2)]


async def test_a_bill_dated_today_gets_its_period_the_next_day(
    ha: HomeAssistant,
    mock_api: dict[str, AsyncMock],
    freezer: FrozenDateTimeFactory,
) -> None:
    # The period read today is skipped until tomorrow, so its bill, dated
    # today, shows its amount without a period until then.
    freezer.move_to("2026-10-08T12:00:00+13:00")
    mock_api["periods"].return_value = [
        *default_periods(),
        api_period(date(2026, 9, 3), date(2026, 10, 8), 9000),
    ]
    mock_api["transactions"].return_value = {
        "items": [api_transaction(date(2026, 10, 8), 87.65)]
    }
    entry = make_entry()
    await _setup(ha, entry)
    amount = ha.states.get("sensor.watercare_last_bill_amount")
    assert amount.state == "87.65"
    assert amount.attributes["bill_date"] == "2026-10-08"
    assert amount.attributes["billing_period_end"] is None
    assert entry.runtime_data.data.latest_period.end == date(2026, 9, 2)

    freezer.move_to("2026-10-09T00:30:00+13:00")
    await _refresh(ha, entry.runtime_data)
    amount = ha.states.get("sensor.watercare_last_bill_amount")
    assert amount.attributes["billing_period_start"] == "2026-09-03"
    assert amount.attributes["billing_period_end"] == "2026-10-08"
    assert entry.runtime_data.data.latest_period.end == date(2026, 10, 8)


async def test_adding_watercare_again_continues_its_statistics(
    ha: HomeAssistant,
    mock_api: dict[str, AsyncMock],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Removing Watercare keeps its statistics; adding it again continues them."""
    entry = make_entry()
    await _setup(ha, entry)
    assert entry.runtime_data.last_import.consumption_rows == 92
    await ha.config_entries.async_remove(entry.entry_id)
    await ha.async_block_till_done()
    assert len(await stored_rows(ha, STAT_CONSUMPTION)) == 92

    caplog.clear()
    mock_api["periods"].return_value = [
        api_period(date(2026, 9, 3), date(2026, 10, 2), 9000),
        *default_periods(),
    ]
    again = make_entry()
    await _setup(ha, again)

    # Only the new bill's days, after the kept ones, and nothing to report.
    assert again.runtime_data.last_import.consumption_rows == 30
    rows = await stored_rows(ha, STAT_CONSUMPTION)
    assert len(rows) == 92 + 30
    assert rows[-1]["sum"] == pytest.approx(42000)
    assert _warnings(caplog) == []
    assert not [
        issue
        for (domain, _issue_id), issue in ir.async_get(ha).issues.items()
        if domain == DOMAIN
    ]


async def test_setup_deletes_the_retired_rebuild_notice(
    ha: HomeAssistant, mock_api: dict[str, AsyncMock]
) -> None:
    """1.5.x and 1.6.0b1 could leave a notice that nothing raises any more."""
    entry = make_entry()
    issue_id = f"statistics_rebuild_skipped_{entry.entry_id}"
    ir.async_create_issue(
        ha,
        DOMAIN,
        issue_id,
        is_fixable=False,
        is_persistent=True,
        severity=ir.IssueSeverity.WARNING,
        translation_key="statistics_rebuild_skipped",
    )
    await _setup(ha, entry)

    assert ir.async_get(ha).async_get_issue(DOMAIN, issue_id) is None


async def test_tariff_issue_is_raised_and_cleared(
    ha: HomeAssistant,
    mock_api: dict[str, AsyncMock],
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to("2027-08-10T12:00:00+12:00")
    mock_api["periods"].return_value = [
        api_period(date(2027, 7, 4), date(2027, 8, 3), 5000),
        *default_periods(),
    ]
    entry = make_entry()
    await _setup(ha, entry)

    coordinator = entry.runtime_data
    issue_id = f"tariff_missing_{entry.entry_id}"
    issue = ir.async_get(ha).async_get_issue(DOMAIN, issue_id)
    assert issue is not None
    assert issue.is_fixable
    assert issue.translation_placeholders == {"financial_year": "2027/28"}
    assert issue.data == {"entry_id": entry.entry_id, "financial_year": 2027}
    assert coordinator.missing_tariff_year == 2027
    assert coordinator.data.latest_cost is None
    assert coordinator.last_import.first_day_without_tariff == date(2027, 7, 4)
    assert coordinator.last_import.missing_tariff_year == 2027
    assert ha.states.get("sensor.watercare_last_bill_cost").state == "unknown"

    ha.config_entries.async_update_entry(
        entry,
        options={
            **entry.options,
            "tariff_overrides": {
                "2027": {"water_rate": 2.6, "wastewater_rate": 4.5, "fixed_charge": 380}
            },
        },
    )
    await _reload(ha, entry)

    assert ir.async_get(ha).async_get_issue(DOMAIN, issue_id) is None
    assert entry.runtime_data.data.latest_cost is not None


async def test_an_entry_from_1_4_x_is_refused(
    ha: HomeAssistant,
    mock_api: dict[str, AsyncMock],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Entries from 1.4.x and earlier are no longer migrated."""
    data = {"email": EMAIL, "password": "synthetic-password"}
    options = {"consumption_rate": 2.296, "wastewater_ratio": 0.785}
    entry = make_entry(minor_version=1, data=data, options=options)
    await _setup(ha, entry)

    assert entry.state is ConfigEntryState.MIGRATION_ERROR
    assert (entry.minor_version, dict(entry.data), dict(entry.options)) == (
        1,
        data,
        options,
    )
    assert [
        record.getMessage()
        for record in caplog.records
        if record.name.startswith("custom_components.watercare")
        and record.levelno >= ERROR
    ] == [
        (
            "This Watercare entry was created by version 1.4 or earlier, which "
            "this version no longer upgrades. Install Watercare 1.5.1 first, or "
            "remove Watercare, delete its four statistics and add it again"
        )
    ]
    mock_api["account"].assert_not_called()


async def test_migrate_refuses_a_newer_version(ha: HomeAssistant) -> None:
    entry = make_entry(version=2, minor_version=1)
    await _setup(ha, entry)
    assert entry.state is ConfigEntryState.MIGRATION_ERROR


async def test_migrate_entry_loads_a_newer_minor_version(ha: HomeAssistant) -> None:
    newer = make_entry(minor_version=3)
    assert await async_migrate_entry(ha, newer)
    assert newer.minor_version == 3


async def test_tariff_issue_names_the_earliest_unpriced_year(
    ha: HomeAssistant,
    mock_api: dict[str, AsyncMock],
    freezer: FrozenDateTimeFactory,
) -> None:
    # A year was skipped: 2028/29 prices are known, 2027/28 are not.
    freezer.move_to("2028-08-02T12:00:00+12:00")
    mock_api["periods"].return_value = [
        api_period(date(2027, 7, 4), date(2027, 8, 3), 5000),
        *default_periods(),
    ]
    entry = make_entry(
        options={
            "wastewater_ratio": 0.785,
            "tariff_overrides": {
                "2028": {"water_rate": 2.8, "wastewater_rate": 4.9, "fixed_charge": 400}
            },
        }
    )
    await _setup(ha, entry)

    issue = ir.async_get(ha).async_get_issue(DOMAIN, f"tariff_missing_{entry.entry_id}")
    assert issue is not None
    assert issue.translation_placeholders == {"financial_year": "2027/28"}
    assert entry.runtime_data.missing_tariff_year == 2027


def _use_account(mock_api: dict[str, AsyncMock], **changes: Any) -> None:
    """Make the fake Watercare return the synthetic account with changes."""
    account = AccountSummary.from_json([{**ACCOUNT_PAYLOAD[0], **changes}])
    assert account is not None

    async def _get_account(api: Any) -> AccountSummary:
        api._account = account
        return account

    mock_api["account"].side_effect = _get_account


SMART_METERS = [
    pytest.param({"meterType": "smartmeter-single"}, id="smartmeter-single"),
    pytest.param({"meterType": "smartmeter-other"}, id="smartmeter-other"),
    pytest.param({"transitionedMeterFromMMToSM": True}, id="changed-to-smart"),
]


@pytest.mark.parametrize("changes", SMART_METERS)
async def test_smart_meter_issue_is_raised_and_cleared(
    ha: HomeAssistant, mock_api: dict[str, AsyncMock], changes: dict[str, Any]
) -> None:
    _use_account(mock_api, **changes)
    entry = make_entry()
    await _setup(ha, entry)

    # The bills from before the change still load.
    assert entry.state is ConfigEntryState.LOADED
    issue_id = f"smart_meter_{entry.entry_id}"
    issue = ir.async_get(ha).async_get_issue(DOMAIN, issue_id)
    assert issue is not None
    assert not issue.is_fixable
    assert issue.severity is ir.IssueSeverity.WARNING
    assert issue.translation_key == "smart_meter"

    _use_account(mock_api, meterType="mechanical")
    await _refresh(ha, entry.runtime_data)

    assert ir.async_get(ha).async_get_issue(DOMAIN, issue_id) is None


@pytest.mark.parametrize(
    ("periods", "reason", "message"),
    [
        pytest.param(
            {"return_value": []}, "smart meter", SMART_METER_MESSAGE, id="no-bills"
        ),
        pytest.param(
            {"side_effect": WatercareConnectionError("it answered with HTTP 404")},
            "HTTP 404",
            "Could not get data from Watercare (it answered with HTTP 404)",
            id="bills-refused",
        ),
    ],
)
async def test_smart_meter_issue_without_bills(
    ha: HomeAssistant,
    mock_api: dict[str, AsyncMock],
    periods: dict[str, Any],
    reason: str,
    message: str,
) -> None:
    _use_account(mock_api, meterType="smartmeter-single")
    for name, value in periods.items():
        setattr(mock_api["periods"], name, value)
    entry = make_entry()
    await _setup(ha, entry)

    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert reason in str(entry.reason)
    assert str(entry.reason) == message
    issue_id = f"smart_meter_{entry.entry_id}"
    assert ir.async_get(ha).async_get_issue(DOMAIN, issue_id) is not None


async def test_a_new_account_without_bills_retries(
    ha: HomeAssistant, mock_api: dict[str, AsyncMock]
) -> None:
    mock_api["periods"].return_value = []
    entry = make_entry()
    await _setup(ha, entry)

    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert "no completed bill for this account yet" in str(entry.reason)


@pytest.mark.parametrize(
    ("changes", "smart"),
    [
        pytest.param({"meterType": ""}, False, id="blank"),
        pytest.param({"meterType": "  "}, False, id="spaces"),
        pytest.param({"meterType": " mechanical "}, False, id="padded"),
        pytest.param({"meterType": "smartmeter-single"}, True, id="smart"),
    ],
)
async def test_a_blank_or_padded_meter_type_is_not_a_smart_meter(
    ha: HomeAssistant,
    mock_api: dict[str, AsyncMock],
    changes: dict[str, Any],
    smart: bool,
) -> None:
    _use_account(mock_api, **changes)
    entry = make_entry()
    await _setup(ha, entry)

    issue_id = f"smart_meter_{entry.entry_id}"
    assert (ir.async_get(ha).async_get_issue(DOMAIN, issue_id) is not None) is smart


async def test_a_smart_meter_account_refuses_unusual_periods(
    ha: HomeAssistant,
    mock_api: dict[str, AsyncMock],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Periods that would be moved or read as no water are refused instead."""
    _use_account(mock_api, meterType="smartmeter-single")
    no_usage = api_period(date(2026, 9, 3), date(2026, 10, 2), 9000)
    del no_usage["waterUsage"]
    # Starts inside the newest bill, so it overlaps it.
    overlapping = api_period(date(2026, 8, 20), date(2026, 9, 10), 4000)
    mock_api["periods"].return_value = [no_usage, overlapping, *default_periods()]
    entry = make_entry()
    await _setup(ha, entry)
    coordinator = entry.runtime_data

    assert coordinator.data.refused_periods == 2
    assert coordinator.data.latest_period.end == date(2026, 9, 2)
    assert coordinator.last_import.consumption_rows == 92
    warnings = [
        (
            "Skipped the Watercare billing period from 2026-09-03 to 2026-10-02, "
            "which has no water use figure, because Watercare reports a smart meter"
        ),
        (
            "Skipped the Watercare billing period from 2026-08-20 to 2026-09-10, "
            "which overlaps the period before it, because Watercare reports a "
            "smart meter"
        ),
    ]
    assert _warnings(caplog) == warnings

    # Each is reported once, not on every poll.
    await _refresh(ha, coordinator)
    assert _warnings(caplog) == warnings

    # A period that grew past the days already recorded.
    mock_api["periods"].return_value = [
        api_period(date(2026, 8, 4), date(2026, 9, 20), 20000),
        *default_periods()[1:],
    ]
    await _refresh(ha, coordinator)
    assert coordinator.last_import.consumption_rows == 0
    assert [(item.reason) for item in coordinator.last_import.refused] == [
        "overlaps_stored"
    ]
    assert _warnings(caplog)[-1] == (
        "Skipped the Watercare billing period from 2026-08-04 to 2026-09-20, "
        "which overlaps days already recorded, because Watercare reports a smart "
        "meter"
    )
    rows = await stored_rows(ha, STAT_CONSUMPTION)
    assert rows[-1]["sum"] == pytest.approx(33000)


async def test_a_manually_read_meter_reads_a_period_without_usage_as_none(
    ha: HomeAssistant, mock_api: dict[str, AsyncMock]
) -> None:
    """Unchanged from 1.4.1: a null usage on a manually read meter is 0 L."""
    no_usage = api_period(date(2026, 9, 3), date(2026, 10, 2), 9000)
    no_usage["waterUsage"] = None
    mock_api["periods"].return_value = [no_usage, *default_periods()]
    entry = make_entry()
    await _setup(ha, entry)

    data = entry.runtime_data.data
    assert data.refused_periods == 0
    assert data.latest_period.usage_litres == 0


async def test_no_smart_meter_issue_for_a_mechanical_meter(
    ha: HomeAssistant, mock_api: dict[str, AsyncMock]
) -> None:
    entry = make_entry()
    await _setup(ha, entry)

    issue_id = f"smart_meter_{entry.entry_id}"
    assert ir.async_get(ha).async_get_issue(DOMAIN, issue_id) is None


async def test_maintenance_is_named_in_setup_and_updates(
    ha: HomeAssistant, mock_api: dict[str, AsyncMock]
) -> None:
    mock_api["account"].side_effect = WatercareMaintenanceError("maintenance")
    entry = make_entry()
    await _setup(ha, entry)

    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert "Watercare is down for maintenance" in str(entry.reason)

    _use_account(mock_api)
    await _reload(ha, entry)
    assert entry.state is ConfigEntryState.LOADED
    coordinator = entry.runtime_data
    mock_api["periods"].side_effect = WatercareMaintenanceError("maintenance")
    await _refresh(ha, coordinator)

    assert not coordinator.last_update_success
    assert coordinator.last_exception.translation_key == "service_maintenance"
    # Not a sign-in problem, and no notice: it passes by itself.
    assert not ha.config_entries.flow.async_progress()
    assert not [
        issue
        for (domain, _issue_id), issue in ir.async_get(ha).issues.items()
        if domain == DOMAIN
    ]


async def test_upgrade_required_raises_a_notice_until_watercare_answers(
    ha: HomeAssistant, mock_api: dict[str, AsyncMock]
) -> None:
    mock_api["account"].side_effect = WatercareUpgradeRequiredError("upgrade")
    entry = make_entry()
    await _setup(ha, entry)

    assert entry.state is ConfigEntryState.SETUP_RETRY
    issue_id = f"upgrade_required_{entry.entry_id}"
    issue = ir.async_get(ha).async_get_issue(DOMAIN, issue_id)
    assert issue is not None
    assert not issue.is_fixable
    assert issue.severity is ir.IssueSeverity.ERROR
    assert issue.translation_key == "upgrade_required"

    _use_account(mock_api)
    await _reload(ha, entry)

    assert entry.state is ConfigEntryState.LOADED
    assert ir.async_get(ha).async_get_issue(DOMAIN, issue_id) is None

    mock_api["periods"].side_effect = WatercareUpgradeRequiredError("upgrade")
    await _refresh(ha, entry.runtime_data)
    assert entry.runtime_data.last_exception.translation_key == "upgrade_required"
    assert ir.async_get(ha).async_get_issue(DOMAIN, issue_id) is not None


async def test_the_last_bill_amount_survives_a_failed_read(
    ha: HomeAssistant,
    mock_api: dict[str, AsyncMock],
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(DEBUG, logger="custom_components.watercare")
    mock_api["transactions"].side_effect = WatercareConnectionError("down")
    entry = make_entry()
    await _setup(ha, entry)

    # The poll never fails for the amounts, even at setup.
    assert entry.state is ConfigEntryState.LOADED
    coordinator = entry.runtime_data
    assert coordinator.data.last_bill is None
    assert coordinator.data.last_bill_fetch_failed
    assert ha.states.get("sensor.watercare_last_bill_amount").state == "unknown"

    mock_api["transactions"].side_effect = None
    await _refresh(ha, coordinator)
    assert ha.states.get("sensor.watercare_last_bill_amount").state == "98.76"
    assert not coordinator.data.last_bill_fetch_failed

    for failure in (
        {"side_effect": WatercareAuthError("refused")},
        # Nothing unexpected fails the poll either.
        {"side_effect": RuntimeError("synthetic-secret-detail")},
        {"side_effect": None, "return_value": {"unexpected": True}},
    ):
        for name, value in failure.items():
            setattr(mock_api["transactions"], name, value)
        await _refresh(ha, coordinator)
        assert coordinator.last_update_success
        assert coordinator.data.last_bill_fetch_failed
        amount = ha.states.get("sensor.watercare_last_bill_amount")
        assert amount.state == "98.76"
        assert amount.attributes["billing_period_end"] == "2026-09-02"
    # The warning names no Python type; the debug line has it, never the text.
    assert (
        "Could not read the newest Watercare bill's amount because of an "
        "unexpected error; Last bill amount keeps the amount read earlier, if any"
    ) in _warnings(caplog)
    assert not [w for w in _warnings(caplog) if "RuntimeError" in w]
    assert (
        "Reading the Watercare bills' amounts failed: unexpected RuntimeError"
        in caplog.text
    )
    assert "synthetic-secret-detail" not in caplog.text

    # A placeholder date, which has no Auckland date, skips only its own item.
    mock_api["transactions"].return_value = {
        "items": [
            {"date": "9999-12-31T23:59:59Z", "type": "bill", "amount": 1},
            *default_transactions()["items"],
        ]
    }
    await _refresh(ha, coordinator)
    assert not coordinator.data.last_bill_fetch_failed
    assert ha.states.get("sensor.watercare_last_bill_amount").state == "98.76"

    # A newer bill, before its period is in the bills: no period matches yet.
    mock_api["transactions"].return_value = {
        "items": [api_transaction(date(2026, 10, 6), 87.65)]
    }
    await _refresh(ha, coordinator)
    amount = ha.states.get("sensor.watercare_last_bill_amount")
    assert amount.state == "87.65"
    assert amount.attributes["bill_date"] == "2026-10-06"
    assert amount.attributes["billing_period_end"] is None

    # An account with no bills yet.
    mock_api["transactions"].return_value = {"items": []}
    await _refresh(ha, coordinator)
    assert ha.states.get("sensor.watercare_last_bill_amount").state == "unknown"


async def test_an_unexpected_bill_amount_error_is_logged_once_per_streak(
    ha: HomeAssistant,
    mock_api: dict[str, AsyncMock],
    caplog: pytest.LogCaptureFixture,
) -> None:
    entry = make_entry()
    await _setup(ha, entry)
    coordinator = entry.runtime_data

    mock_api["transactions"].side_effect = RuntimeError("synthetic")
    await _refresh(ha, coordinator)
    await _refresh(ha, coordinator)
    assert len(_warnings(caplog)) == 1

    mock_api["transactions"].side_effect = None
    await _refresh(ha, coordinator)
    mock_api["transactions"].side_effect = RuntimeError("synthetic")
    await _refresh(ha, coordinator)
    assert len(_warnings(caplog)) == 2


async def test_removing_the_entry_removes_its_issues(
    ha: HomeAssistant,
    mock_api: dict[str, AsyncMock],
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to("2027-07-02T12:00:00+12:00")
    _use_account(mock_api, meterType="smartmeter-single")
    entry = make_entry()
    await _setup(ha, entry)
    mock_api["periods"].side_effect = WatercareUpgradeRequiredError("upgrade")
    await _refresh(ha, entry.runtime_data)
    issues = ir.async_get(ha)
    # One that only 1.5.x and 1.6.0b1 raised.
    ir.async_create_issue(
        ha,
        DOMAIN,
        f"statistics_rebuild_skipped_{entry.entry_id}",
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key="statistics_rebuild_skipped",
    )
    issue_ids = [
        f"{issue}_{entry.entry_id}"
        for issue in (
            "tariff_missing",
            "smart_meter",
            "upgrade_required",
            "statistics_rebuild_skipped",
        )
    ]
    assert all(issues.async_get_issue(DOMAIN, issue_id) for issue_id in issue_ids)

    await ha.config_entries.async_remove(entry.entry_id)

    assert not any(issues.async_get_issue(DOMAIN, issue_id) for issue_id in issue_ids)


async def test_bill_spanning_1_july_keeps_its_cost_while_the_new_year_is_unknown(
    ha: HomeAssistant,
    mock_api: dict[str, AsyncMock],
    freezer: FrozenDateTimeFactory,
) -> None:
    # The latest bill started in June 2027, so 2026/27 prices apply to all of
    # it; only the current year's prices are missing.
    freezer.move_to("2027-07-10T12:00:00+12:00")
    mock_api["periods"].return_value = [
        api_period(date(2027, 6, 4), date(2027, 7, 3), 5000),
        *default_periods(),
    ]
    entry = make_entry()
    await _setup(ha, entry)

    data = entry.runtime_data.data
    assert entry.runtime_data.last_import.first_day_without_tariff is None
    assert data.latest_cost is not None
    assert data.latest_tariff == PUBLISHED_TARIFFS[2026]
    assert entry.runtime_data.missing_tariff_year == 2027
    usage = ha.states.get("sensor.watercare_last_bill_usage")
    assert usage is not None
    assert usage.attributes["tariff_year"] == "2026/27"


@pytest.mark.parametrize("ratio", [0.785, 0.95])
async def test_first_import_end_to_end(
    ha: HomeAssistant, mock_api: dict[str, AsyncMock], ratio: float
) -> None:
    """The first import prices every bill at the entry's stored ratio.

    Setup stores 0.785; a home with another ratio sets it in the options and
    starts again, so the import must read it from there.
    """
    entry = make_entry(options={"wastewater_ratio": ratio})
    await _setup(ha, entry)

    coordinator = entry.runtime_data
    assert coordinator.statistics_status == "imported"
    # Every statistic holds one row per day.
    periods = parse_billing_periods(default_periods()).periods
    costs = [bill_cost(p, TariffSchedule({}), Decimal(str(ratio))) for p in periods]
    assert all(cost is not None for cost in costs)
    expected = {
        STAT_CONSUMPTION: 33000.0,
        STAT_TOTAL_COST: float(sum(cost.total for cost in costs if cost)),
        STAT_CONSUMPTION_COST: float(sum(cost.water for cost in costs if cost)),
        STAT_WASTEWATER_COST: float(sum(cost.wastewater for cost in costs if cost)),
    }
    for statistic_id, final_sum in expected.items():
        rows = await stored_rows(ha, statistic_id)
        assert len(rows) == 92, statistic_id
        assert datetime.fromtimestamp(rows[0]["start"], tz=UTC) == day_start(
            date(2026, 6, 3)
        )
        assert rows[-1]["sum"] == pytest.approx(final_sum), statistic_id
    if ratio == 0.785:
        # The June bill spans 1 July and keeps its 2025/26 price.
        assert costs[0] is not None
        assert round(costs[0].total, 2) == Decimal("93.37")
        assert expected[STAT_TOTAL_COST] == pytest.approx(93.37 + 76.79 + 104.89)

    # The next poll adds nothing and keeps the rows.
    await _refresh(ha, coordinator)
    assert coordinator.last_import.consumption_rows == 0
    assert len(await stored_rows(ha, STAT_TOTAL_COST)) == 92


async def test_statistics_retry_after_a_failed_read(
    ha: HomeAssistant,
    mock_api: dict[str, AsyncMock],
    caplog: pytest.LogCaptureFixture,
) -> None:
    entry = make_entry()

    # The database cannot be read when the import checks the stored history.
    with patch(
        "custom_components.watercare.statistics.get_last_statistics",
        side_effect=OperationalError("SELECT", {}, Exception("database is locked")),
    ):
        await _setup(ha, entry)

    # The sensors work; the statistics wait for the next update.
    assert entry.state is ConfigEntryState.LOADED
    coordinator = entry.runtime_data
    assert coordinator.statistics_status == "pending"
    assert len(await stored_rows(ha, STAT_CONSUMPTION)) == 0
    assert _warnings(caplog) == [
        (
            "Could not update the Watercare statistics; the next update, within "
            "12 hours, tries again"
        )
    ]

    await _refresh(ha, coordinator)

    assert coordinator.statistics_status == "imported"
    assert len(await stored_rows(ha, STAT_CONSUMPTION)) == 92


# ----------------------------------------------- several properties, one login


class _Login:
    """A login whose v1/account lists several synthetic properties.

    Each property has bills and payments of its own. The second's include a
    bill after the first's newest, so following it would add days to the
    statistics.
    """

    def __init__(self, *listed: str) -> None:
        self.listed = list(listed)
        self.requests: list[str] = []
        self.periods: dict[str, Any] = {
            ACCOUNT_NUMBER: default_periods(),
            SECOND_ACCOUNT_NUMBER: [
                api_period(date(2026, 9, 3), date(2026, 10, 2), 99000),
                api_period(date(2026, 8, 4), date(2026, 9, 2), 55000),
            ],
            THIRD_ACCOUNT_NUMBER: [],
        }
        self.transactions: dict[str, Any] = {
            ACCOUNT_NUMBER: default_transactions(),
            SECOND_ACCOUNT_NUMBER: {
                "items": [api_transaction(date(2026, 10, 4), 555.55, balance=555.55)]
            },
            THIRD_ACCOUNT_NUMBER: {"items": []},
        }

    async def get(self, api: WatercareApi, path: str) -> Any:
        """Answer one of the client's GETs."""
        del api
        self.requests.append(path)
        if path == "v1/account":
            return properties_payload(*self.listed)
        _, kind, number, _ = path.split("/")
        return self.periods[number] if kind == "usage" else self.transactions[number]

    def numbers_asked(self) -> set[str]:
        """Return the account numbers the bills and payments were asked for."""
        return {path.split("/")[2] for path in self.requests if path != "v1/account"}


@pytest.fixture
def login() -> Generator[_Login]:
    """The client's requests answered by a login listing several properties."""
    fake = _Login(ACCOUNT_NUMBER, SECOND_ACCOUNT_NUMBER)
    with patch.object(WatercareApi, "_async_get", autospec=True, side_effect=fake.get):
        yield fake


@pytest.mark.parametrize(
    "listed",
    [
        (SECOND_ACCOUNT_NUMBER, ACCOUNT_NUMBER),
        (ACCOUNT_NUMBER, SECOND_ACCOUNT_NUMBER),
    ],
)
async def test_a_login_with_several_properties_stays_with_the_entrys(
    ha: HomeAssistant, login: _Login, listed: tuple[str, ...]
) -> None:
    login.listed = list(listed)
    entry = make_entry()
    await _setup(ha, entry)

    assert entry.state is ConfigEntryState.LOADED
    account = entry.runtime_data.data.account
    assert account.account_number == ACCOUNT_NUMBER
    assert account.meter_number == METER_NUMBER
    assert login.numbers_asked() == {ACCOUNT_NUMBER}
    assert ha.states.get("sensor.watercare_last_bill_usage").state == "13000"
    assert ha.states.get("sensor.watercare_last_bill_amount").state == "98.76"


async def test_a_change_in_the_order_of_the_properties_changes_nothing(
    ha: HomeAssistant, login: _Login
) -> None:
    entry = make_entry()
    await _setup(ha, entry)
    coordinator = entry.runtime_data
    before = {sid: await stored_rows(ha, sid) for sid in ALL_STATISTIC_IDS}
    assert all(len(rows) == 92 for rows in before.values())

    login.listed.reverse()
    await _refresh(ha, coordinator)

    assert coordinator.last_update_success
    assert coordinator.data.account.account_number == ACCOUNT_NUMBER
    assert login.numbers_asked() == {ACCOUNT_NUMBER}
    after = {sid: await stored_rows(ha, sid) for sid in ALL_STATISTIC_IDS}
    assert after == before
    assert ha.states.get("sensor.watercare_last_bill_amount").state == "98.76"


async def test_a_login_without_the_entrys_property_retries_setup(
    ha: HomeAssistant, login: _Login, caplog: pytest.LogCaptureFixture
) -> None:
    login.listed = [SECOND_ACCOUNT_NUMBER, THIRD_ACCOUNT_NUMBER]
    entry = make_entry()
    await _setup(ha, entry)

    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert entry.error_reason_translation_key == "property_not_listed"
    assert str(entry.reason) == PROPERTY_NOT_LISTED_MESSAGE
    assert login.numbers_asked() == set()
    assert _warnings(caplog) == []


async def test_a_running_entry_whose_property_is_no_longer_listed_goes_unavailable(
    ha: HomeAssistant, login: _Login
) -> None:
    entry = make_entry()
    await _setup(ha, entry)
    coordinator = entry.runtime_data
    before = {sid: await stored_rows(ha, sid) for sid in ALL_STATISTIC_IDS}

    login.listed = [SECOND_ACCOUNT_NUMBER, THIRD_ACCOUNT_NUMBER]
    await _refresh(ha, coordinator)

    assert not coordinator.last_update_success
    assert coordinator.last_exception.translation_key == "property_not_listed"
    assert ha.states.get("sensor.watercare_last_bill_usage").state == "unavailable"
    assert login.numbers_asked() == {ACCOUNT_NUMBER}
    after = {sid: await stored_rows(ha, sid) for sid in ALL_STATISTIC_IDS}
    assert after == before


async def test_a_login_with_one_property_is_read_as_before(
    ha: HomeAssistant, login: _Login
) -> None:
    """One listed property is used whatever its number, as in 1.6.0b2.

    So a change in how Watercare writes the account number can never stop
    a working install; only a login that lists several is checked.
    """
    login.listed = [SECOND_ACCOUNT_NUMBER]
    entry = make_entry()
    await _setup(ha, entry)

    assert entry.state is ConfigEntryState.LOADED
    account = entry.runtime_data.data.account
    assert account.account_number == SECOND_ACCOUNT_NUMBER
    assert account.meter_number == SECOND_METER_NUMBER
    assert login.numbers_asked() == {SECOND_ACCOUNT_NUMBER}
