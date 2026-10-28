"""Contracts for the end-to-end journeys runner (``scripts/journeys.py``)."""

from __future__ import annotations

import argparse
import email.message
import io
import json
import re
import tempfile
import urllib.error
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from custom_components.watercare.const import ALL_STATISTIC_IDS
from custom_components.watercare.tariffs import PUBLISHED_TARIFFS
from scripts import journeys, live_ha
from scripts.journeys import (
    Bill,
    Import,
    Journey,
    JourneyError,
    JourneyResult,
    Prices,
    Pricing,
    Row,
    Screen,
)

from . import watercare_standin
from .watercare_http import bills

_TODAY = date(2026, 10, 28)
_DAY = timedelta(days=1)


def _bill(start: date, days: int, litres: int) -> Bill:
    return Bill(start, start + _DAY * (days - 1), litres, "A")


# Three bills, the second spanning 1 July 2025.
_BILLS = (
    _bill(date(2025, 5, 4), 30, 9000),
    _bill(date(2025, 6, 3), 31, 12000),
    _bill(date(2025, 7, 4), 31, 6000),
)


def _rows(kind: str, expected: journeys.Expected) -> list[Row]:
    rows: list[Row] = []
    total = 0.0
    for item in expected.days[kind]:
        total += float(item.value)
        rows.append(Row(journeys.day_start(item.day), float(item.value), total))
    return rows


# ---------------------------------------------------------------- statistics


def test_the_statistics_are_the_integrations() -> None:
    assert set(journeys.STATISTICS.values()) == set(ALL_STATISTIC_IDS)
    assert set(journeys.COSTS) < set(journeys.STATISTICS)


def test_prices_are_the_users_then_the_published_ones() -> None:
    published = PUBLISHED_TARIFFS[2026]
    assert Pricing().prices(2026) == Prices(
        published.water_rate, published.wastewater_rate, published.fixed_charge
    )
    first = min(PUBLISHED_TARIFFS)
    assert Pricing().prices(first - 3) == Pricing().prices(first)
    assert Pricing().prices(max(PUBLISHED_TARIFFS) + 1) is None
    own = Prices(Decimal(1), Decimal(2), Decimal(3))
    assert Pricing(overrides={2026: own}).prices(2026) == own
    assert journeys.NEW_PRICES.as_options() == {
        "water_rate": 2.5,
        "wastewater_rate": 4.5,
        "fixed_charge": 370.0,
    }


def test_financial_years_start_on_1_july() -> None:
    assert journeys.financial_year(date(2026, 6, 30)) == 2025
    assert journeys.financial_year(date(2026, 7, 1)) == 2026
    assert journeys.year_label(2026) == "2026/27"
    assert journeys.year_label(2099) == "2099/00"


def test_a_bill_is_priced_as_docs_tariffs_says() -> None:
    bill = _bill(date(2026, 7, 4), 30, 9000)
    prices = Prices(Decimal("2.46"), Decimal("4.28"), Decimal("355.90"))
    cost = journeys.bill_cost(bill, prices, Decimal("0.785"))
    assert cost["water_cost"] == Decimal("22.14")
    assert cost["wastewater_cost"] == Decimal("30.2382")
    fixed = Decimal("355.90") / 365 * 30
    assert cost["total_cost"] == Decimal("22.14") + Decimal("30.2382") + fixed


def test_every_day_of_every_bill_is_expected_once() -> None:
    expected = journeys.expected_statistics([Import(_BILLS)])
    days = [item.day for item in expected.days["consumption"]]
    assert days[0] == _BILLS[0].start
    assert days[-1] == _BILLS[-1].end
    assert days == [days[0] + _DAY * n for n in range(len(days))]
    for kind in journeys.STATISTICS:
        assert [item.day for item in expected.days[kind]] == days
    second = [i for i in expected.days["consumption"] if i.bill == _BILLS[1]]
    assert float(sum(i.value for i in second)) == pytest.approx(12000)
    # The bill that spans 1 July is priced at its start's year, 2024/25.
    prices = Pricing().prices(2024)
    assert prices is not None
    assert expected.totals["water_cost"][_BILLS[1]] == 12 * prices.water


def test_an_update_adds_only_later_days_and_never_reprices() -> None:
    cheap = Pricing(Decimal("0.5"))
    expected = journeys.expected_statistics([Import(_BILLS[:2]), Import(_BILLS, cheap)])
    (first,) = {i.value for i in expected.days["wastewater_cost"][:3]}
    late = expected.days["wastewater_cost"][-1]
    assert late.bill == _BILLS[2]
    alone = journeys.expected_statistics([Import(_BILLS)])
    assert first == alone.days["wastewater_cost"][0].value
    assert late.value < alone.days["wastewater_cost"][-1].value
    # Nothing new: nothing added.
    again = journeys.expected_statistics([Import(_BILLS), Import(_BILLS, cheap)])
    assert again.days == alone.days


def test_costs_stop_at_the_first_bill_without_prices_and_resume() -> None:
    unpriced = (_bill(date(2027, 6, 3), 31, 9000), _bill(date(2027, 7, 4), 31, 6000))
    history = (*_BILLS, *unpriced)
    stopped = journeys.expected_statistics([Import(history)])
    assert len(stopped.days["consumption"]) > len(stopped.days["total_cost"])
    assert stopped.days["total_cost"][-1].day == date(2027, 7, 3)
    assert journeys.tariff_notice([Import(history)], _TODAY) == 2027
    own = Prices(Decimal(3), Decimal(5), Decimal(400))
    resumed = journeys.expected_statistics(
        [Import(history), Import(history, Pricing(overrides={2027: own}))]
    )
    assert resumed.days["total_cost"][-1].day == unpriced[-1].end
    assert resumed.totals["water_cost"][unpriced[-1]] == 6 * own.water


def test_the_notice_asks_for_this_year_only_when_it_has_no_prices() -> None:
    assert journeys.tariff_notice([Import(_BILLS)], _TODAY) is None
    later = date(max(PUBLISHED_TARIFFS) + 1, 8, 1)
    assert journeys.tariff_notice([Import(_BILLS)], later) == later.year


def test_days_start_at_auckland_midnight() -> None:
    assert journeys.day_start(date(2026, 1, 15)) == datetime(
        2026, 1, 14, 11, tzinfo=UTC
    )
    assert journeys.day_start(date(2026, 7, 15)) == datetime(
        2026, 7, 14, 12, tzinfo=UTC
    )
    assert Row(datetime(2026, 7, 14, 12, tzinfo=UTC), 0, 0).day == date(2026, 7, 15)


def test_parse_rows_reads_the_recorders_milliseconds() -> None:
    start = journeys.day_start(date(2026, 7, 15))
    (row,) = journeys.parse_rows(
        [{"start": start.timestamp() * 1000, "state": 0.5, "sum": 1.5}]
    )
    assert row == Row(start, 0.5, 1.5)
    (empty,) = journeys.parse_rows([{"start": start.timestamp() * 1000}])
    assert (empty.state, empty.sum) == (0.0, 0.0)


def test_a_whole_history_has_no_problems() -> None:
    expected = journeys.expected_statistics([Import(tuple(bills(_TODAY)))])
    for kind in journeys.STATISTICS:
        assert journeys.history_problems(kind, _rows(kind, expected), expected) == []
    # A history with no days proves nothing.
    nothing = journeys.expected_statistics([])
    assert journeys.history_problems("consumption", [], nothing) == [
        "no days expected; 0 rows found"
    ]


def test_every_kind_of_damage_is_found() -> None:
    expected = journeys.expected_statistics([Import(_BILLS)])
    rows = _rows("consumption", expected)

    def problems(changed: list[Row]) -> list[str]:
        return journeys.history_problems("consumption", changed, expected)

    assert problems([])[0].startswith("no rows, expected 2025-05-04 to 2025-08-03")
    gap = rows[:3] + rows[4:]
    assert any("follows" in p for p in problems(gap))
    assert any(p.startswith("days ") for p in problems(gap))
    assert any("follows" in p for p in problems([*rows[:3], rows[2], *rows[3:]]))
    late = [Row(rows[0].start + timedelta(hours=1), rows[0].state, rows[0].sum)]
    assert any("is not an Auckland midnight" in p for p in problems(late + rows[1:]))
    moved = [rows[0], Row(rows[1].start, rows[1].state + 5, rows[1].sum + 5)]
    moved += [Row(r.start, r.state, r.sum + 5) for r in rows[2:]]
    found = problems(moved)
    assert any("holds" in p for p in found)
    assert any("adds up to" in p for p in found)
    stepped = [*rows[:5], Row(rows[5].start, rows[5].state, rows[5].sum + 1)]
    stepped += rows[6:]
    assert any("sum step" in p for p in problems(stepped))
    offset = [Row(r.start, r.state, r.sum + 1) for r in rows]
    assert problems(offset)[0].startswith("the first sum")


def test_problems_are_cut_short() -> None:
    expected = journeys.expected_statistics([Import(_BILLS)])
    rows = [Row(r.start, 0.0, 0.0) for r in _rows("consumption", expected)]
    problems = journeys.history_problems("consumption", rows, expected)
    assert len(problems) == journeys.SHOWN_PROBLEMS + 1
    assert problems[-1].startswith("and ")


def test_changed_rows_names_lost_and_changed_days() -> None:
    expected = journeys.expected_statistics([Import(_BILLS)])
    before = _rows("consumption", expected)[:5]
    assert journeys.changed_rows(before, _rows("consumption", expected)) == []
    after = [before[0], Row(before[1].start, 9.0, 9.0), *before[3:]]
    changed = journeys.changed_rows(before, after)
    assert len(changed) == 2
    assert "now 9.0/9.0" in changed[0]
    assert changed[1].endswith("is gone")
    # Nothing before proves nothing.
    assert journeys.changed_rows([], before) == ["no days to compare"]


# -------------------------------------------------------------------- screens

_TRANSLATIONS = {
    "component.watercare.config.step.user.title": "Connect to Watercare",
    "component.watercare.config.step.user.description": "Sign in.",
    "component.watercare.config.step.user.data.username": "Email",
    "component.watercare.config.step.user.data.password": "Password",
    "component.watercare.config.step.reauth_confirm.title": "Sign in again",
    "component.watercare.config.step.reauth_confirm.description": (
        "Watercare no longer accepts the password for {username}."
    ),
    "component.watercare.config.error.invalid_auth": "Rejected.",
    "component.watercare.config.abort.wrong_account": "Another account.",
    "component.watercare.config.create_entry.partial": "In the background.",
    "component.watercare.options.step.init.title": "Watercare prices",
    "component.watercare.options.step.init.description": "Prices for {financial_year}.",
    "component.watercare.options.step.init.data.wastewater_ratio": "Wastewater ratio",
    "component.homeassistant.config.abort.reauth_successful": "Signed in again.",
}


def test_a_form_without_its_own_title_shows_the_dialogs() -> None:
    words = {**_TRANSLATIONS, "component.watercare.title": "Watercare"}
    config = journeys.render(
        {"type": "form", "handler": "watercare", "step_id": "untitled"}, words
    )
    assert (config.title, config.missing) == ("Watercare", ())
    options = journeys.render(
        {"type": "form", "handler": "an-entry-id", "step_id": "untitled"},
        words,
        "options",
    )
    assert (options.title, options.missing) == (journeys.OPTIONS_HEADER, ())


def test_a_form_is_rendered_with_its_placeholders_and_error() -> None:
    screen = journeys.render(
        {
            "type": "form",
            "handler": "watercare",
            "step_id": "user",
            "errors": {"base": "invalid_auth"},
            "data_schema": [{"name": "username"}, {"name": "password"}],
        },
        _TRANSLATIONS,
    )
    assert screen == Screen(
        "form",
        "user",
        "Connect to Watercare",
        "Sign in.",
        "Rejected.",
        ("Email", "Password"),
        (),
    )
    lines = screen.lines()
    assert lines[0] == "- **form `user`**: Connect to Watercare"
    assert "  > Sign in." in lines
    assert "  - Error line: Rejected." in lines
    reauth = journeys.render(
        {
            "type": "form",
            "handler": "watercare",
            "step_id": "reauth_confirm",
            "description_placeholders": {"username": "journey@example.com"},
            "data_schema": [{"name": "password"}],
        },
        _TRANSLATIONS,
    )
    assert reauth.text == (
        "Watercare no longer accepts the password for journey@example.com."
    )
    assert reauth.missing == ("step.reauth_confirm.data.password",)


def test_an_options_screen_uses_the_integrations_options_words() -> None:
    screen = journeys.render(
        {
            "type": "form",
            "handler": "an-entry-id",
            "step_id": "init",
            "description_placeholders": {"financial_year": "2026/27"},
            "data_schema": [{"name": "wastewater_ratio"}, {"name": "water_rate"}],
        },
        _TRANSLATIONS,
        "options",
    )
    assert (screen.title, screen.text) == ("Watercare prices", "Prices for 2026/27.")
    assert screen.fields == ("Wastewater ratio", "water_rate")
    assert screen.missing == ("step.init.data.water_rate",)


def test_abort_entry_and_other_screens_are_rendered() -> None:
    abort = journeys.render(
        {"type": "abort", "handler": "watercare", "reason": "wrong_account"},
        _TRANSLATIONS,
    )
    assert abort.text == "Another account."
    common = journeys.render(
        {
            "type": "abort",
            "handler": "watercare",
            "reason": "reauth_successful",
            "translation_domain": "homeassistant",
        },
        _TRANSLATIONS,
    )
    assert (common.text, common.missing) == ("Signed in again.", ())
    untranslated = journeys.render(
        {"type": "abort", "handler": "watercare", "reason": "reauth_successful"},
        _TRANSLATIONS,
    )
    assert untranslated.text == "reauth_successful"
    assert untranslated.missing == ("abort.reauth_successful",)
    assert "  - No translation for: abort.reauth_successful" in untranslated.lines()
    # Without a description, the dialog shows only its own words.
    plain = journeys.render(
        {"type": "create_entry", "handler": "watercare", "title": "Watercare"},
        _TRANSLATIONS,
    )
    assert (plain.step, plain.title, plain.text, plain.missing) == (
        "default",
        "Watercare",
        "",
        (),
    )
    described = journeys.render(
        {"type": "create_entry", "handler": "watercare", "description": "partial"},
        _TRANSLATIONS,
    )
    assert described.text == "In the background."
    progress = journeys.render(
        {"type": "progress", "handler": "watercare", "progress_action": "wait"},
        _TRANSLATIONS,
    )
    assert progress.missing == ("progress.wait",)
    other = journeys.render({"type": "external", "step_id": "x"}, _TRANSLATIONS)
    assert (other.kind, other.step) == ("external", "x")


def test_a_screen_with_an_unfilled_placeholder_lacks_its_words() -> None:
    screen = journeys.render(
        {
            "type": "form",
            "handler": "watercare",
            "step_id": "reauth_confirm",
            "description_placeholders": {},
            "data_schema": [],
        },
        _TRANSLATIONS,
    )
    assert screen.text == "Watercare no longer accepts the password for {username}."
    assert screen.missing == (
        "step.reauth_confirm.description (no value for {username})",
    )


def test_field_labels_come_from_the_handlers_words() -> None:
    screen = journeys.render(
        {
            "type": "form",
            "handler": "watercare",
            "translation_domain": "homeassistant",
            "step_id": "user",
            "errors": {"base": "invalid_auth"},
            "data_schema": [{"name": "username"}],
        },
        _TRANSLATIONS,
    )
    assert screen.fields == ("Email",)
    assert screen.missing == ("component.watercare.title", "error.invalid_auth")
    options = journeys.render(
        {
            "type": "form",
            "handler": "an-entry-id",
            "translation_domain": "homeassistant",
            "step_id": "init",
            "data_schema": [{"name": "wastewater_ratio"}],
        },
        _TRANSLATIONS,
        "options",
    )
    assert options.fields == ("Wastewater ratio",)


def test_suggested_values_and_errors_are_read_from_a_form() -> None:
    result = {
        "errors": {"base": "cannot_connect"},
        "data_schema": [
            {"name": "password"},
            {"name": "username", "description": {"suggested_value": "a@example.com"}},
        ],
    }
    assert journeys.suggested(result, "username") == "a@example.com"
    assert journeys.suggested(result, "password") is None
    assert journeys.suggested(result, "missing") is None
    assert journeys.base_error(result) == "cannot_connect"
    assert journeys.base_error({"errors": {}}) is None


# --------------------------------------------------------------------- report


def test_a_journey_passes_only_with_checks_all_passed_and_no_error() -> None:
    result = JourneyResult("first_setup", "First")
    assert not result.ok
    result.checks.append(journeys.Check("a", ok=True))
    assert result.ok
    result.checks.append(journeys.Check("b", ok=False, detail="x"))
    assert not result.ok
    stopped = JourneyResult("reauth", "Reauth", [journeys.Check("a", ok=True)])
    stopped.error = "JourneyError: timed out"
    assert not stopped.ok


def test_the_report_lists_every_check_and_screen() -> None:
    passed = JourneyResult(
        "first_setup",
        "First setup",
        [journeys.Check("the entry is loaded", ok=True, detail="loaded")],
        [Screen("form", "user", "Connect", "Sign in.", "", ("Email",), ())],
        seconds=12.4,
    )
    failed = JourneyResult(
        "reauth",
        "Reauth",
        [journeys.Check("sums", ok=False, detail="a|b")],
        error="JourneyError: timed out",
    )
    text = journeys.markdown([passed, failed], {"Home Assistant": "2026.10.4"})
    assert text.startswith("# Watercare journeys\n")
    assert "- Home Assistant: 2026.10.4" in text
    assert "- Result: 1 of 2 journeys passed" in text
    assert "## PASS: First setup (`first_setup`, 12 s)" in text
    assert "| PASS | the entry is loaded | loaded |" in text
    assert "- **form `user`**: Connect" in text
    assert "## FAIL: Reauth (`reauth`, 0 s)" in text
    assert "Stopped: JourneyError: timed out" in text
    assert "| FAIL | sums | a/b |" in text


def test_the_report_goes_to_the_temporary_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    path = journeys.write_report([], {"Started": "now"}, 7)
    assert path.parent == tmp_path
    assert path.name.startswith("watercare-journeys-")
    assert "- Started: now" in path.read_text()
    assert "- Result: 0 of 7 journeys passed" in path.read_text()


def test_a_journey_that_never_ran_did_not_pass() -> None:
    passed = JourneyResult("a", "A", [journeys.Check("c", ok=True)])
    assert journeys.summary([passed], 1) == "1 of 1 journeys passed"
    assert journeys.summary([passed], 7) == "1 of 7 journeys passed"
    assert journeys.summary([passed, passed], 0) == "2 of 2 journeys passed"
    assert journeys.summary([passed], 1, 2) == (
        "1 of 1 journeys passed; FAIL: the stand-in refused 2"
    )


# ------------------------------------------------------------------- journey


class _Client:
    """A client whose REST and websocket answers are canned and recorded."""

    def __init__(
        self,
        answers: dict[str, Any] | None = None,
        issues: list[dict[str, Any]] | None = None,
    ) -> None:
        self.answers = answers or {}
        self.issues = issues or []
        self.sent: list[str] = []
        self.translated: list[tuple[str, str]] = []

    def rest(self, method: str, path: str, data: dict[str, Any] | None = None) -> Any:
        del data
        self.sent.append(f"{method} {path}")
        answer = self.answers.get(path)
        if isinstance(answer, Exception):
            raise answer
        return answer

    def ws(self, *messages: dict[str, Any]) -> list[Any]:
        (message,) = messages
        if message["type"] == "repairs/list_issues":
            return [{"issues": self.issues}]
        assert message["type"] == "frontend/get_translations"
        (domain,) = message["integration"]
        category = message["category"]
        self.translated.append((domain, category))
        prefix = f"component.{domain}.{category}."
        return [
            {
                "resources": {
                    k: v for k, v in _TRANSLATIONS.items() if k.startswith(prefix)
                }
            }
        ]


def _stop(stopped: list[str], state: Path) -> Any:
    """Return a fake ``live_ha.stop`` that records the name it was given."""

    def stop(name: str = "live-ha", before_delete: Any = None) -> None:
        stopped.append(name)
        if before_delete is not None:
            before_delete(state)

    return stop


def _journey(folder: Path, client: Any = None) -> Journey:
    chosen: Any = client or _Client()
    return Journey(chosen, folder, JourneyResult("x", "X"), _TODAY)


def test_checks_are_recorded_and_a_required_one_stops_the_journey(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    journey = _journey(tmp_path)
    assert journey.check("passes", ok=True)
    assert not journey.check("fails", ok=False, detail="why")
    with pytest.raises(JourneyError, match="needed: because"):
        journey.require("needed", ok=False, detail="because")
    with pytest.raises(JourneyError, match=r"^bare$"):
        journey.require("bare", ok=False)
    assert [(c.name, c.ok) for c in journey.result.checks] == [
        ("passes", True),
        ("fails", False),
        ("needed", False),
        ("bare", False),
    ]
    out = capsys.readouterr().out
    assert "  PASS  passes" in out
    assert "  FAIL  fails (why)" in out


def test_the_scenario_and_journal_are_the_stand_ins(tmp_path: Path) -> None:
    journey = _journey(tmp_path)
    assert journey.journal() == []
    scenario = journey.watercare(withheld=2, api_status=503)
    assert scenario.as_of == _TODAY
    assert dict(scenario.logins) == {journeys.EMAIL: journeys.ACCOUNT_NUMBER}
    assert scenario.password == journeys.PASSWORD
    journey.scenario(scenario)
    standin = watercare_standin.StandIn(tmp_path)
    assert standin.scenario() == scenario
    assert not list(tmp_path.glob("*.partial"))
    assert journey.issued() == tuple(bills(_TODAY)[:-2])
    standin.journal({"at": "2026-10-28T12:00:00.250000+00:00", "operation": "a"})
    standin.journal({"at": "2026-10-28T12:00:00.750000+00:00", "operation": "b"})
    standin.journal({"at": "2026-10-28T12:00:01.250000+00:00", "operation": "c"})
    assert [r["operation"] for r in journey.journal()] == ["a", "b", "c"]
    # To the microsecond: two requests in the same second are told apart.
    since = datetime(2026, 10, 28, 12, 0, 0, 500000, tzinfo=UTC)
    assert journey.operations(since) == ["b", "c"]
    until = datetime(2026, 10, 28, 12, 0, 1, 250000, tzinfo=UTC)
    assert journey.operations(since, until) == ["b"]


def test_the_entrys_creation_time_is_home_assistants(tmp_path: Path) -> None:
    path = "/api/config/config_entries/entry?domain=watercare"
    at = datetime(2026, 10, 28, 4, 10, 15, 123456, tzinfo=UTC)
    journey = _journey(
        tmp_path,
        _Client({path: [{"entry_id": "E1", "created_at": at.timestamp()}]}),
    )
    assert journey.created("E1") == at
    with pytest.raises(JourneyError, match="E2 is gone"):
        journey.created("E2")


def test_refused_requests_and_fake_errors_fail_the_check(tmp_path: Path) -> None:
    journey = _journey(tmp_path)
    journey.check_nothing_refused()
    # No request at all proves nothing.
    assert [(c.ok, c.detail) for c in journey.result.checks] == [
        (True, ""),
        (False, "no requests at all"),
    ]
    standin = watercare_standin.StandIn(tmp_path)
    standin.journal({"at": "2026-10-28T12:00:00+00:00", "operation": "account"})
    journey.check_nothing_refused()
    assert journey.result.checks[-1].ok
    assert journey.result.checks[-1].detail == "answered: 1"
    standin.refuse("request", "updates.example.invalid")
    standin.journal({"at": "2026-10-28T12:00:00+00:00", "error": "unexpected request"})
    journey.check_nothing_refused()
    refused, errors = journey.result.checks[4:]
    assert not refused.ok
    assert "request updates.example.invalid" in refused.detail
    assert not errors.ok
    assert errors.detail == "unexpected request"


def test_screens_load_each_domains_words_once_per_category(tmp_path: Path) -> None:
    client = _Client()
    journey = _journey(tmp_path, client)
    shown = journey.screen(
        {"type": "abort", "handler": "watercare", "reason": "wrong_account"}
    )
    assert shown.text == "Another account."
    common = {
        "type": "abort",
        "handler": "watercare",
        "reason": "reauth_successful",
        "translation_domain": "homeassistant",
    }
    assert journey.screen(common).text == "Signed in again."
    journey.screen(common)
    options = journey.screen(
        {"type": "form", "handler": "an-entry-id", "step_id": "init"}, "options"
    )
    assert options.title == "Watercare prices"
    # The options dialog loads no other domain's texts.
    journey.screen(
        {
            "type": "abort",
            "handler": "an-entry-id",
            "reason": "reauth_successful",
            "translation_domain": "homeassistant",
        },
        "options",
    )
    assert client.translated == [
        ("watercare", "config"),
        ("watercare", "title"),
        ("homeassistant", "config"),
        ("watercare", "options"),
    ]
    assert journey.result.screens[-1].missing == ("abort.reauth_successful",)
    assert len(journey.result.screens) == 5


def test_a_screen_without_its_words_fails_the_journey(tmp_path: Path) -> None:
    journey = _journey(tmp_path)
    journey.check_screens()
    assert journey.result.checks == []
    journey.screen({"type": "abort", "handler": "watercare", "reason": "wrong_account"})
    journey.check_screens()
    assert journey.result.checks[-1].ok
    journey.screen(
        {"type": "abort", "handler": "watercare", "reason": "reauth_successful"}
    )
    journey.check_screens()
    shown = journey.result.checks[-1]
    assert not shown.ok
    assert shown.detail == (
        "no translation for abort reauth_successful: abort.reauth_successful"
    )


def test_flows_and_entries_are_noted_for_removal(tmp_path: Path) -> None:
    flow = "/api/config/config_entries/flow"
    options = "/api/config/config_entries/options/flow"
    client = _Client(
        {
            flow: {
                "type": "form",
                "handler": "watercare",
                "flow_id": "F1",
                "step_id": "user",
            },
            f"{flow}/F1": {
                "type": "create_entry",
                "handler": "watercare",
                "flow_id": "F1",
                "result": {"entry_id": "E1"},
            },
            options: {
                "type": "form",
                "handler": "E1",
                "flow_id": "O1",
                "step_id": "init",
            },
            f"{options}/O1": {"type": "create_entry", "handler": "E1"},
            f"{flow}/F2": urllib.error.URLError("gone"),
        }
    )
    journey = _journey(tmp_path, client)
    journey.start_flow({})
    journey.submit("F1", {"username": "a@example.com", "password": "synthetic"})
    journey.start_options("E1")
    journey.submit_options("O1", {"wastewater_ratio": 0.95})
    assert journey.entries == ["E1"]
    assert journey.options_flows == ["O1"]
    journey.flows.append("F2")
    journey.remove_entries()
    assert client.sent[-4:] == [
        f"DELETE {flow}/F1",
        f"DELETE {flow}/F2",
        f"DELETE {options}/O1",
        "DELETE /api/config/config_entries/entry/E1",
    ]


def test_the_sensors_must_show_the_newest_bill(tmp_path: Path) -> None:
    journey = _journey(tmp_path)
    latest = Import(_BILLS)
    prices = Pricing().prices(2025)
    assert prices is not None
    cost = journeys.bill_cost(_BILLS[-1], prices, journeys.DEFAULT_RATIO)
    shown = f"{cost['total_cost']:.2f}"
    journey.check_newest_bill(
        "E", {"E_usage": "6000", "E_current_bill_cost": shown}, latest
    )
    journey.check_newest_bill(
        "E", {"E_usage": "6000", "E_current_bill_cost": "1"}, latest
    )
    journey.check_newest_bill(
        "E", {"E_usage": "x", "E_current_bill_cost": shown}, latest
    )
    unpriced = Import((_bill(date(2099, 7, 4), 31, 6000),))
    journey.check_newest_bill(
        "E", {"E_usage": "6000", "E_current_bill_cost": "unknown"}, unpriced
    )
    journey.check_newest_bill("E", {"E_usage": "6000"}, latest)
    assert [check.ok for check in journey.result.checks] == [
        True,
        False,
        False,
        True,
        False,
    ]


def test_the_repair_notices_must_be_those_the_prices_call_for(tmp_path: Path) -> None:
    def notices(issues: list[dict[str, Any]], imports: list[Import]) -> bool:
        journey = _journey(tmp_path, _Client(issues=issues))
        journey.check_notices("E1", imports)
        return journey.result.checks[-1].ok

    def issue(name: str, year: str | None = None, entry: str = "E1") -> dict[str, Any]:
        placeholders = {"financial_year": year} if year else None
        return {
            "domain": "watercare",
            "issue_id": f"{name}_{entry}",
            "translation_placeholders": placeholders,
        }

    priced = [Import(_BILLS)]
    unpriced = [Import((*_BILLS, _bill(date(2099, 7, 4), 31, 6000)))]
    assert notices([], priced)
    assert notices([issue("tariff_missing", "2025/26", "E2")], priced)
    assert not notices([issue("statistics_rebuild_skipped")], priced)
    assert notices([issue("tariff_missing", "2099/00")], unpriced)
    assert not notices([issue("tariff_missing", "2098/99")], unpriced)
    assert not notices([], unpriced)


def test_wait_gives_up_after_its_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(journeys, "POLL_S", 0.01)
    calls: list[int] = []

    def third_time() -> bool | None:
        calls.append(1)
        return len(calls) > 2 or None

    assert journeys.wait("x", third_time, 5)
    assert len(calls) == 3
    with pytest.raises(JourneyError, match="timed out after 0 s waiting for y"):
        journeys.wait("y", lambda: None, 0.05)


def test_an_error_status_stops_the_journey_with_its_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse(*_args: Any, **_kwargs: Any) -> Any:
        raise urllib.error.HTTPError(
            "http://127.0.0.1:8126/x",
            400,
            "Bad",
            email.message.Message(),
            io.BytesIO(b'{"message": "no"}'),
        )

    monkeypatch.setattr(live_ha, "call", refuse)
    client = journeys.Client(live_ha.Instance(Path("/x"), 8126, None, "1"))  # type: ignore[arg-type]
    client._token = "token"
    client._token_at = 1e12
    with pytest.raises(
        JourneyError, match=re.escape('POST /x returned HTTP 400: {"message": "no"}')
    ):
        client.rest("POST", "/x", {})


# --------------------------------------------------------------------- runner


def test_the_stand_in_goes_into_site_packages_with_the_fake(tmp_path: Path) -> None:
    files = journeys.site_files(tmp_path / "standin")
    assert files["watercare_standin.pth"] == watercare_standin.PTH_LINE.encode()
    assert json.loads(files["watercare_standin/settings.json"]) == {
        "folder": str(tmp_path / "standin")
    }
    root = Path(journeys.__file__).resolve().parents[1] / "tests"
    assert (
        files["watercare_standin/__init__.py"]
        == (root / "watercare_standin.py").read_bytes()
    )
    assert (
        files["watercare_standin/watercare_http.py"]
        == (root / "watercare_http.py").read_bytes()
    )


def test_the_journeys_never_take_the_live_tests_port_or_folder() -> None:
    assert journeys.journeys_port("8126") == 8126
    with pytest.raises(argparse.ArgumentTypeError, match="live test"):
        journeys.journeys_port(str(live_ha.DEFAULT_PORT))
    args = journeys.parse([])
    assert args.port == journeys.DEFAULT_PORT != live_ha.DEFAULT_PORT
    assert journeys.NAME != live_ha.DEFAULT_NAME
    assert live_ha.state_dir("watercare", journeys.NAME).name == "journeys-watercare"
    assert journeys.parse(["--only", "reauth", "restart"]).only == [
        "reauth",
        "restart",
    ]
    with pytest.raises(SystemExit):
        journeys.parse(["--only", "nonsense"])


def test_every_journey_has_its_function() -> None:
    for name in journeys.JOURNEYS:
        assert callable(getattr(journeys, name))


def test_the_stand_in_must_be_installed_in_home_assistant(tmp_path: Path) -> None:
    class _Process:
        pid = 4242

    instance = live_ha.Instance(tmp_path, 8126, _Process(), "1")  # type: ignore[arg-type]
    run = journeys.Run(instance, tmp_path)
    with pytest.raises(SystemExit, match="not installed"):
        run.check_standin()
    (tmp_path / "installed-4242").write_text("")
    run.check_standin()


def test_a_journey_starts_without_statistics_and_is_cleaned_up_if_it_stops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    done: list[str] = []

    def stops(journey: Journey) -> None:
        journey.entries.append("E1")
        journey.check("got this far", ok=True)
        raise JourneyError("timed out")

    monkeypatch.setattr(journeys, "first_setup", stops)
    monkeypatch.setattr(Journey, "clear_statistics", lambda _self: done.append("clear"))
    monkeypatch.setattr(
        Journey, "remove_entries", lambda self: done.extend(self.entries)
    )
    run = journeys.Run(live_ha.Instance(tmp_path, 8126, None, "1"), tmp_path, _TODAY)  # type: ignore[arg-type]
    result = run.run("first_setup")
    assert not result.ok
    assert result.error == "JourneyError: timed out"
    assert done == ["clear", "E1"]
    assert run.results == [result]


def test_a_journey_that_crashes_fails_and_the_next_one_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def crashes(journey: Journey) -> None:
        journey.check("got this far", ok=True)
        raise IndexError("list index out of range")

    monkeypatch.setattr(journeys, "first_setup", crashes)
    monkeypatch.setattr(journeys, "wrong_password", lambda j: j.check("ok", ok=True))
    monkeypatch.setattr(Journey, "clear_statistics", lambda _self: None)
    monkeypatch.setattr(Journey, "remove_entries", lambda _self: None)
    run = journeys.Run(live_ha.Instance(tmp_path, 8126, None, "1"), tmp_path, _TODAY)  # type: ignore[arg-type]
    crashed = run.run("first_setup")
    assert not crashed.ok
    assert crashed.error == "IndexError: list index out of range"
    assert run.run("wrong_password").ok
    assert journeys.summary(run.results, 2) == "1 of 2 journeys passed"


def test_a_journey_whose_clean_up_fails_fails_and_the_next_one_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(_self: Journey) -> None:
        raise ValueError("Expecting value: line 1 column 1 (char 0)")

    monkeypatch.setattr(journeys, "first_setup", lambda j: j.check("ok", ok=True))
    monkeypatch.setattr(journeys, "wrong_password", lambda j: j.check("ok", ok=True))
    monkeypatch.setattr(Journey, "clear_statistics", lambda _self: None)
    monkeypatch.setattr(Journey, "remove_entries", broken)
    run = journeys.Run(live_ha.Instance(tmp_path, 8126, None, "1"), tmp_path, _TODAY)  # type: ignore[arg-type]
    failed = run.run("first_setup")
    assert not failed.ok
    assert failed.error == (
        "the clean-up failed: ValueError: Expecting value: line 1 column 1 (char 0)"
    )
    monkeypatch.setattr(Journey, "remove_entries", lambda _self: None)
    assert run.run("wrong_password").ok


def test_an_interrupted_journey_fails_and_stops_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    removed: list[bool] = []

    def interrupted(journey: Journey) -> None:
        journey.check("got this far", ok=True)
        raise KeyboardInterrupt

    monkeypatch.setattr(journeys, "first_setup", interrupted)
    monkeypatch.setattr(Journey, "clear_statistics", lambda _self: None)
    monkeypatch.setattr(Journey, "remove_entries", lambda _self: removed.append(True))
    run = journeys.Run(live_ha.Instance(tmp_path, 8126, None, "1"), tmp_path, _TODAY)  # type: ignore[arg-type]
    with pytest.raises(KeyboardInterrupt):
        run.run("first_setup")
    (result,) = run.results
    assert not result.ok
    assert result.error == "the run stopped: KeyboardInterrupt"
    assert removed == [True]
    # Journeys that never ran did not pass.
    assert journeys.summary(run.results, 7) == "0 of 7 journeys passed"


def test_a_failed_restart_stops_the_run_but_keeps_its_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class _Process:
        pid = 4242

    folder = tmp_path / "config" / journeys.STANDIN_FOLDER

    def launch(*_args: Any, **_kwargs: Any) -> live_ha.Instance:
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "installed-4242").write_text("")
        return live_ha.Instance(tmp_path, 8126, _Process(), "2026.10.4")  # type: ignore[arg-type]

    def restart(journey: Journey, relaunch: Any) -> None:
        journey.check("before the restart", ok=True)
        relaunch()

    def relaunch(*_args: Any, **_kwargs: Any) -> live_ha.Instance:
        raise SystemExit("Timed out waiting for the web server")

    stopped: list[str] = []
    monkeypatch.setattr(live_ha, "launch", launch)
    monkeypatch.setattr(live_ha, "relaunch", relaunch)
    monkeypatch.setattr(live_ha, "state_dir", lambda *_args: tmp_path / "state")
    monkeypatch.setattr(live_ha, "stop", _stop(stopped, tmp_path / "state"))
    monkeypatch.setattr(live_ha, "config_dir", lambda _state: tmp_path / "config")
    monkeypatch.setattr(journeys, "restart", restart)
    monkeypatch.setattr(Journey, "clear_statistics", lambda _self: None)
    monkeypatch.setattr(Journey, "remove_entries", lambda _self: None)
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    with pytest.raises(SystemExit, match="web server"):
        journeys.main(["--only", "restart"])
    assert stopped == ["journeys"]
    out = capsys.readouterr().out
    assert (
        "FAIL  the run stopped: SystemExit: Timed out waiting for the web server" in out
    )
    assert "0 of 1 journeys passed" in out
    (report,) = tmp_path.glob("watercare-journeys-*.md")
    assert "Stopped: the run stopped: SystemExit: Timed out" in report.read_text()


def test_the_instance_is_always_deleted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stopped: list[str] = []
    monkeypatch.setattr(live_ha, "state_dir", lambda *_args: tmp_path / "state")
    monkeypatch.setattr(live_ha, "stop", _stop(stopped, tmp_path / "state"))

    def broken(*_args: Any, **_kwargs: Any) -> live_ha.Instance:
        raise SystemExit("install failed")

    monkeypatch.setattr(live_ha, "launch", broken)
    with pytest.raises(SystemExit, match="install failed"):
        journeys.main([])
    assert stopped == ["journeys"]
    assert journeys.main(["--clean-up"]) == 0
    assert stopped == ["journeys", "journeys"]


def test_a_run_never_takes_over_an_existing_instance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stopped: list[str] = []
    launched: list[bool] = []
    (tmp_path / "state").mkdir()
    monkeypatch.setattr(live_ha, "state_dir", lambda *_args: tmp_path / "state")
    monkeypatch.setattr(live_ha, "stop", _stop(stopped, tmp_path / "state"))
    monkeypatch.setattr(live_ha, "launch", lambda *_a, **_k: launched.append(True))
    with pytest.raises(SystemExit, match="left by a run that was killed"):
        journeys.main([])
    assert stopped == []
    assert launched == []
    assert (tmp_path / "state").is_dir()


def test_nothing_starts_while_another_run_holds_the_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stopped: list[str] = []
    launched: list[bool] = []
    monkeypatch.setattr(live_ha, "state_dir", lambda *_args: tmp_path / "state")
    monkeypatch.setattr(live_ha, "stop", _stop(stopped, tmp_path / "state"))
    monkeypatch.setattr(live_ha, "launch", lambda *_a, **_k: launched.append(True))
    with journeys.only_run(tmp_path / "state"):
        for argv in ([], ["--clean-up"]):
            with pytest.raises(SystemExit, match="Another run is using"):
                journeys.main(argv)
    assert stopped == []
    assert launched == []
    # Released with the run: a clean-up can go ahead.
    assert journeys.main(["--clean-up"]) == 0
    assert stopped == ["journeys"]


def test_a_run_reports_and_exits_non_zero_when_a_journey_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class _Process:
        pid = 4242

    folder = tmp_path / "config" / journeys.STANDIN_FOLDER
    launched: dict[str, Any] = {}

    def launch(*args: Any, **kwargs: Any) -> live_ha.Instance:
        launched.update(kwargs, args=args)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "installed-4242").write_text("")
        return live_ha.Instance(tmp_path, 8126, _Process(), "2026.10.4")  # type: ignore[arg-type]

    def run(self: journeys.Run, name: str) -> JourneyResult:
        result = JourneyResult(name, name, [journeys.Check("c", ok=name != "reauth")])
        self.results.append(result)
        return result

    stopped: list[str] = []
    monkeypatch.setattr(live_ha, "launch", launch)
    monkeypatch.setattr(live_ha, "state_dir", lambda *_args: tmp_path / "state")
    monkeypatch.setattr(live_ha, "stop", _stop(stopped, tmp_path / "state"))
    monkeypatch.setattr(live_ha, "config_dir", lambda _state: tmp_path / "config")
    monkeypatch.setattr(journeys.Run, "run", run)
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    assert journeys.main(["--only", "first_setup", "reauth"]) == 1
    assert launched["name"] == "journeys"
    assert launched["args"] == (None, None, 8126)
    assert json.loads(launched["site_files"]["watercare_standin/settings.json"]) == {
        "folder": str(folder)
    }
    assert "1 of 2 journeys passed" in capsys.readouterr().out
    assert journeys.main(["--only", "first_setup"]) == 0
    assert stopped == ["journeys", "journeys"]
    (report,) = sorted(tmp_path.glob("watercare-journeys-*.md"))[-1:]
    assert "Home Assistant: 2026.10.4" in report.read_text()
    assert "Refused by the stand-in: nothing" in report.read_text()


def test_a_run_whose_journeys_did_not_all_run_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class _Process:
        pid = 4242

    folder = tmp_path / "config" / journeys.STANDIN_FOLDER

    def launch(*_args: Any, **_kwargs: Any) -> live_ha.Instance:
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "installed-4242").write_text("")
        return live_ha.Instance(tmp_path, 8126, _Process(), "2026.10.4")  # type: ignore[arg-type]

    def run(self: journeys.Run, name: str) -> JourneyResult:
        if name == "reauth":
            # Recorded nothing, as if the runner skipped it.
            return JourneyResult(name, name)
        result = JourneyResult(name, name, [journeys.Check("c", ok=True)])
        self.results.append(result)
        return result

    monkeypatch.setattr(live_ha, "launch", launch)
    monkeypatch.setattr(live_ha, "state_dir", lambda *_args: tmp_path / "state")
    monkeypatch.setattr(live_ha, "stop", _stop([], tmp_path / "state"))
    monkeypatch.setattr(live_ha, "config_dir", lambda _state: tmp_path / "config")
    monkeypatch.setattr(journeys.Run, "run", run)
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    assert journeys.main(["--only", "first_setup", "reauth"]) == 1
    assert "1 of 2 journeys passed" in capsys.readouterr().out


def test_a_refusal_after_the_last_check_fails_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class _Process:
        pid = 4242

    folder = tmp_path / "config" / journeys.STANDIN_FOLDER

    def launch(*_args: Any, **_kwargs: Any) -> live_ha.Instance:
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "installed-4242").write_text("")
        return live_ha.Instance(tmp_path, 8126, _Process(), "2026.10.4")  # type: ignore[arg-type]

    def run(self: journeys.Run, name: str) -> JourneyResult:
        result = JourneyResult(name, name, [journeys.Check("c", ok=True)])
        self.results.append(result)
        return result

    def stop(name: str = "live-ha", before_delete: Any = None) -> None:
        del name
        # Home Assistant tried to reach the internet as it shut down.
        (folder / watercare_standin.REFUSED).write_text("t connection 192.0.2.1\n")
        before_delete(tmp_path / "state")

    monkeypatch.setattr(live_ha, "launch", launch)
    monkeypatch.setattr(live_ha, "state_dir", lambda *_args: tmp_path / "state")
    monkeypatch.setattr(live_ha, "stop", stop)
    monkeypatch.setattr(live_ha, "config_dir", lambda _state: tmp_path / "config")
    monkeypatch.setattr(journeys.Run, "run", run)
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    assert journeys.main(["--only", "first_setup"]) == 1
    out = capsys.readouterr().out
    assert "FAIL the stand-in refused 1: t connection 192.0.2.1" in out
    assert "1 of 1 journeys passed; FAIL: the stand-in refused 1" in out
    (report,) = tmp_path.glob("watercare-journeys-*.md")
    assert "Refused by the stand-in: 1: t connection 192.0.2.1" in report.read_text()
    assert "Result: 1 of 1 journeys passed; FAIL" in report.read_text()


def test_the_journeys_refuse_another_integration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(live_ha, "integration_domain", lambda: "meridian_energy")
    with pytest.raises(SystemExit, match="not meridian_energy"):
        journeys.main([])
