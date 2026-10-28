"""Run the setup and lifecycle journeys in a real Home Assistant, end to end.

Usage::

    uv run python scripts/journeys.py [--ref REF] [--ha-version VERSION]
                                      [--port PORT] [--only NAME ...]
    uv run python scripts/journeys.py --clean-up

The journeys run in a new Home Assistant (the newest release, or
``--ha-version``) with the integration from the working tree or ``REF``,
built and checked as ``scripts/live_ha.py`` builds a live-test instance, in
its own folder (``journeys-<domain>`` in the system's temporary directory)
and on its own port (8126 by default), so they never meet a live test.

Neither Watercare nor its Azure AD B2C sign-in is ever contacted. A stand-in
(``tests/watercare_standin.py``), installed in the instance's environment
before Home Assistant first starts, answers the integration's requests at
the HTTP level from the tests' fake (``tests/watercare_http.py``), so the
integration's own client signs in, refreshes its token and reads the
account and the bills unchanged. In Home Assistant's process it refuses
every other aiohttp request, TCP connection and name lookup that would leave
this computer, and records it with its host only. Every value is synthetic,
the logins included. It does not cover UDP, reverse lookups, resolvers
written in C or other programs: building the instance, and Home Assistant's
first start, download Home Assistant and the requirements it pins from PyPI
(through uv), as for the live test.

Each journey drives Home Assistant through its REST and websocket APIs, as
the frontend would, with the owner's token, and checks what came of it: the
screens met, the config entry, its sensors and repair notices, and the
integration's four statistics day by day against the stand-in's bills and
the prices (the integration's published table, from the working tree), as
docs/statistics.md defines them. ``--only`` runs some of them, in their
usual order. Each check and each journey is printed as PASS or FAIL, and a
report (markdown) with every check and every screen, as Home Assistant
shows it, is written to the system's temporary directory. The instance is
always stopped and deleted at the end; once Home Assistant has stopped, the
stand-in's record of refusals is read once more. The exit status is non-zero
if a journey failed or did not run, or if anything was refused. A run holds
a lock for as long as it lasts, so neither a second run nor ``--clean-up``
can start meanwhile. A run does not start either while the journeys' folder
exists: a run that was killed left it behind, and ``--clean-up`` deletes it.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import fcntl
import json
import re
import sys
import tempfile
import time
import urllib.error
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from datetime import time as day_time
from decimal import Decimal
from itertools import pairwise
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn
from zoneinfo import ZoneInfo

import aiohttp

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:  # run as a script, not as scripts.journeys
    sys.path.insert(0, str(ROOT))

from custom_components.watercare.tariffs import PUBLISHED_TARIFFS  # noqa: E402
from scripts import live_ha  # noqa: E402
from tests import watercare_standin as standin  # noqa: E402
from tests.watercare_http import (  # noqa: E402
    ACCOUNT_NUMBER,
    SECOND_ACCOUNT_NUMBER,
    Bill,
    bills,
)
from tests.watercare_standin import Scenario  # noqa: E402

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence

NAME = "journeys"
DEFAULT_PORT = 8126
DOMAIN = "watercare"
NZ = ZoneInfo("Pacific/Auckland")
DAY = timedelta(days=1)
# The four statistics, by kind, as docs/statistics.md names them.
STATISTICS = {
    "consumption": "watercare:water_consumption",
    "total_cost": "watercare:water_cost",
    "water_cost": "watercare:consumption_cost",
    "wastewater_cost": "watercare:wastewater_cost",
}
COSTS = ("total_cost", "water_cost", "wastewater_cost")
DEFAULT_RATIO = Decimal("0.785")
KILOLITRE = Decimal(1000)
DAYS_PER_YEAR = Decimal(365)
JULY = 7
# The synthetic logins the journeys sign in with.
EMAIL = "journey@example.com"
RENAMED_EMAIL = "journey-renamed@example.com"
OTHER_ACCOUNT_EMAIL = "journey-other-account@example.com"
PASSWORD = "synthetic-journey-password"  # noqa: S105 - synthetic
NEW_PASSWORD = "synthetic-journey-password-2"  # noqa: S105 - synthetic
WRONG_PASSWORD = "not-the-synthetic-password"  # noqa: S105 - synthetic
# What the options journey enters: a ratio and prices no year has.
NEW_RATIO = Decimal("0.95")
# A sign-in from a form: the B2C steps, then the account it reaches.
SIGN_IN = ["authorize", "self_asserted", "confirmed", "token", "account"]
# Values are compared to the expected ones within this (litres or NZD).
TOLERANCE = 1e-6
# The sensors round the last bill's cost to the cent.
CENT = 0.005
POLL_S = 2.0
SYNC_TIMEOUT_S = 300
ROWS_TIMEOUT_S = 120
REAUTH_TIMEOUT_S = 120
REQUEST_TIMEOUT_S = 150
TOKEN_LIFETIME_S = 600
# Problems shown for one statistic before the rest are only counted.
SHOWN_PROBLEMS = 5
# The stand-in's folder, in the instance's configuration folder.
STANDIN_FOLDER = "journeys"
_FAKES = ("watercare_http.py",)


class JourneyError(Exception):
    """A journey could not go on: what it waited for did not happen."""


def say(message: str) -> None:
    """Print one line of progress."""
    print(message, flush=True)  # noqa: T201


# --------------------------------------------------------------------- prices


@dataclass(frozen=True, slots=True)
class Prices:
    """One financial year's prices, GST-inclusive (NZD)."""

    water: Decimal
    wastewater: Decimal
    fixed: Decimal

    def as_options(self) -> dict[str, float]:
        """Return the prices as the options form takes them."""
        return {
            "water_rate": float(self.water),
            "wastewater_rate": float(self.wastewater),
            "fixed_charge": float(self.fixed),
        }


# What the options journey enters for this financial year.
NEW_PRICES = Prices(Decimal("2.5"), Decimal("4.5"), Decimal(370))


def published() -> dict[int, Prices]:
    """Return the integration's published prices, by financial year."""
    return {
        year: Prices(tariff.water_rate, tariff.wastewater_rate, tariff.fixed_charge)
        for year, tariff in PUBLISHED_TARIFFS.items()
    }


def financial_year(day: date) -> int:
    """Return the financial year (by the calendar year it starts in) of a day."""
    return day.year if day.month >= JULY else day.year - 1


def year_label(year: int) -> str:
    """Return a financial year as Watercare names it, such as 2026/27."""
    return f"{year}/{(year + 1) % 100:02d}"


@dataclass(frozen=True, slots=True)
class Pricing:
    """The prices an update works with: the ratio and the user's own prices."""

    ratio: Decimal = DEFAULT_RATIO
    overrides: Mapping[int, Prices] = field(default_factory=dict)

    def prices(self, year: int) -> Prices | None:
        """Return a financial year's prices, or None while it has none.

        The user's own first, then the published ones; a year before the
        table takes its first year's prices (docs/tariffs.md).
        """
        if year in self.overrides:
            return self.overrides[year]
        table = published()
        if year in table:
            return table[year]
        return table[min(table)] if year < min(table) else None


def bill_cost(bill: Bill, prices: Prices, ratio: Decimal) -> dict[str, Decimal]:
    """Return a whole bill's costs, by kind, as docs/tariffs.md prices it."""
    kilolitres = Decimal(bill.litres) / KILOLITRE
    water = kilolitres * prices.water
    wastewater = kilolitres * ratio * prices.wastewater
    fixed = prices.fixed / DAYS_PER_YEAR * bill.days
    return {
        "water_cost": water,
        "wastewater_cost": wastewater,
        "total_cost": water + wastewater + fixed,
    }


# ----------------------------------------------------------------- statistics


@dataclass(frozen=True, slots=True)
class Import:
    """One statistics update: the bills Watercare had issued, and the prices."""

    bills: tuple[Bill, ...]
    pricing: Pricing = field(default_factory=Pricing)


@dataclass(frozen=True, slots=True)
class Day:
    """One day a statistic should hold, and the bill it belongs to."""

    day: date
    value: Decimal
    bill: Bill


@dataclass(slots=True)
class Expected:
    """What the four statistics should hold, and each bill's total."""

    days: dict[str, list[Day]]
    totals: dict[str, dict[Bill, Decimal]]


def bill_days(bill: Bill) -> list[date]:
    """Return the Auckland days a bill covers, both ends included."""
    return [bill.start + DAY * offset for offset in range(bill.days)]


def expected_statistics(imports: Sequence[Import]) -> Expected:
    """Return what the statistics should hold after these updates, in order.

    As docs/statistics.md defines them: each bill spread evenly over its
    days, one row per Auckland day; an update adds only the days after the
    newest stored row, so stored days keep the prices they were recorded
    with; every day of a bill is priced at the prices of the financial year
    the bill starts in, and the cost rows stop at the first day whose bill
    has no prices, and resume from there once it has.
    """
    days: dict[str, list[Day]] = {kind: [] for kind in STATISTICS}
    totals: dict[str, dict[Bill, Decimal]] = {kind: {} for kind in STATISTICS}
    for update in imports:
        newest = days["consumption"][-1].day if days["consumption"] else None
        for bill in update.bills:
            share = Decimal(bill.litres) / bill.days
            for day in bill_days(bill):
                if newest is None or day > newest:
                    days["consumption"].append(Day(day, share, bill))
                    totals["consumption"][bill] = Decimal(bill.litres)
        priced = days["total_cost"][-1].day if days["total_cost"] else None
        for bill in update.bills:
            if priced is not None and bill.end <= priced:
                continue
            prices = update.pricing.prices(financial_year(bill.start))
            if prices is None:
                break
            whole = bill_cost(bill, prices, update.pricing.ratio)
            for kind in COSTS:
                totals[kind][bill] = whole[kind]
                for day in bill_days(bill):
                    days[kind].append(Day(day, whole[kind] / bill.days, bill))
    return Expected(days, totals)


def tariff_notice(imports: Sequence[Import], today: date) -> int | None:
    """Return the financial year the prices notice should ask for, or None.

    The year of the earliest bill without prices, or else this year if it
    has none (docs/statistics.md, "Costs at the tariff in force").
    """
    latest = imports[-1]
    for bill in latest.bills:
        if latest.pricing.prices(financial_year(bill.start)) is None:
            return financial_year(bill.start)
    year = financial_year(today)
    return year if latest.pricing.prices(year) is None else None


def day_start(day: date) -> datetime:
    """Return the UTC start of an Auckland day."""
    return datetime.combine(day, day_time.min, tzinfo=NZ).astimezone(UTC)


@dataclass(frozen=True, slots=True)
class Row:
    """One stored row of a statistic."""

    start: datetime
    state: float
    sum: float

    @property
    def day(self) -> date:
        """Return the Auckland day the row starts."""
        return self.start.astimezone(NZ).date()


def parse_rows(raw: Iterable[dict[str, Any]]) -> list[Row]:
    """Return a statistic's rows from the recorder's websocket answer."""
    return [
        Row(
            datetime.fromtimestamp(item["start"] / 1000, UTC),
            float(item.get("state") or 0.0),
            float(item.get("sum") or 0.0),
        )
        for item in raw
    ]


def _span(days: Sequence[date]) -> str:
    return f"{days[0]} to {days[-1]} ({len(days)} days)" if days else "none"


def _placement_problems(rows: Sequence[Row]) -> list[str]:
    """Return rows that are not at Auckland midnight, and gaps and repeats."""
    problems = [
        f"{row.start:%Y-%m-%dT%H:%M}Z is not an Auckland midnight"
        for row in rows
        if row.start != day_start(row.day)
    ]
    problems += [
        f"{row.day} follows {previous.day}"
        for previous, row in pairwise(rows)
        if row.day - previous.day != DAY
    ]
    return problems


def _sum_problems(rows: Sequence[Row]) -> list[str]:
    """Return the sums that are not the running total of the days."""
    problems: list[str] = []
    if abs(rows[0].sum - rows[0].state) > TOLERANCE:
        problems.append(f"the first sum {rows[0].sum} is not its day {rows[0].state}")
    problems += [
        f"{row.day}: sum step {row.sum - previous.sum:.6f}, day {row.state}"
        for previous, row in pairwise(rows)
        if abs(row.sum - previous.sum - row.state) > TOLERANCE
    ]
    return problems


def _value_problems(kind: str, rows: Sequence[Row], expected: Expected) -> list[str]:
    """Return the days, and the bills, that do not hold what they should."""
    problems = [
        f"{row.day} holds {row.state}, expected {float(want.value)}"
        for row, want in zip(rows, expected.days[kind], strict=True)
        if abs(row.state - float(want.value)) > TOLERANCE
    ]
    held: dict[Bill, float] = {}
    for row, want in zip(rows, expected.days[kind], strict=True):
        held[want.bill] = held.get(want.bill, 0.0) + row.state
    problems += [
        f"the bill {bill.start} to {bill.end} adds up to {held[bill]}, "
        f"not {float(total)}"
        for bill, total in expected.totals[kind].items()
        if abs(held.get(bill, 0.0) - float(total)) > TOLERANCE
    ]
    return problems


def history_problems(kind: str, rows: Sequence[Row], expected: Expected) -> list[str]:
    """Return what is wrong with a statistic's daily history, or nothing.

    One row per Auckland day, at its midnight, with no day missing or twice;
    the days expected, each holding its share of its bill (litres, or that
    share priced at the bill's prices); each bill's days adding up to the
    bill; each ``sum`` step equal to its day, the first ``sum`` its own day.
    """
    wanted = [item.day for item in expected.days[kind]]
    if not wanted:
        # A history with no days proves nothing.
        return [f"no days expected; {len(rows)} rows found"]
    if not rows:
        return [f"no rows, expected {_span(wanted)}"]
    problems = _placement_problems(rows)
    found = [row.day for row in rows]
    if found != wanted:
        problems.append(f"days {_span(found)}, expected {_span(wanted)}")
    else:
        problems += _value_problems(kind, rows, expected)
    problems += _sum_problems(rows)
    if len(problems) > SHOWN_PROBLEMS:
        problems[SHOWN_PROBLEMS:] = [f"and {len(problems) - SHOWN_PROBLEMS} more"]
    return problems


def changed_rows(before: Sequence[Row], after: Sequence[Row]) -> list[str]:
    """Return every day of ``before`` that ``after`` lost or holds otherwise."""
    if not before:
        return ["no days to compare"]
    now = {row.start: row for row in after}
    changed: list[str] = []
    for row in before:
        later = now.get(row.start)
        if later is None:
            changed.append(f"{row.day} is gone")
        elif (
            abs(later.state - row.state) > TOLERANCE
            or abs(later.sum - row.sum) > TOLERANCE
        ):
            changed.append(
                f"{row.day} was {row.state}/{row.sum}, now {later.state}/{later.sum}"
            )
    return changed


# -------------------------------------------------------------------- screens


@dataclass(frozen=True, slots=True)
class Screen:
    """One screen a journey met, as Home Assistant's dialog shows it."""

    kind: str
    step: str
    title: str
    text: str
    error: str
    fields: tuple[str, ...]
    missing: tuple[str, ...]

    def lines(self) -> list[str]:
        """Return the screen as markdown lines."""
        lines = [
            f"- **{self.kind} `{self.step}`**"
            + (f": {self.title}" if self.title else "")
        ]
        lines.extend(
            f"  > {line}" if line else "  >" for line in self.text.splitlines()
        )
        if self.error:
            lines.append(f"  - Error line: {self.error}")
        if self.fields:
            lines.append(f"  - Fields: {', '.join(self.fields)}")
        if self.missing:
            lines.append(f"  - No translation for: {', '.join(self.missing)}")
        return lines


def fill(text: str, placeholders: dict[str, Any] | None) -> str:
    """Return a translation with its placeholders filled in, as the frontend does."""
    for name, value in (placeholders or {}).items():
        text = text.replace(f"{{{name}}}", str(value))
    return text


_PLACEHOLDER = re.compile(r"\{\w+\}")
# The options dialog's own header for a step with no title of its own
# (the frontend's ui.dialogs.options_flow.form.header).
OPTIONS_HEADER = "Options"


def render(
    result: dict[str, Any], translations: dict[str, str], category: str = "config"
) -> Screen:
    """Return a flow result as Home Assistant's dialog shows it.

    ``category`` is ``config`` for a config flow, ``options`` for an options
    flow, whose handler is the entry, not the integration. A step's texts
    come from its ``translation_domain`` if it names one, its field labels
    from the integration's own texts, as in the dialog. A form with no title
    of its own shows the integration's title, or the options dialog's header.
    """
    handler = (result.get("handler") if category == "config" else None) or DOMAIN
    own = f"component.{handler}.{category}"
    base = f"component.{result.get('translation_domain') or handler}.{category}"
    placeholders = result.get("description_placeholders")
    missing: list[str] = []

    def text(key: str, *, required: bool = True) -> str:
        short = key.removeprefix(f"{own}.").removeprefix(f"{base}.")
        if key in translations:
            filled = fill(translations[key], placeholders)
            if unfilled := _PLACEHOLDER.findall(filled):
                # The dialog shows a translation error instead.
                missing.append(f"{short} (no value for {', '.join(unfilled)})")
            return filled
        if required:
            missing.append(short)
        return ""

    kind = str(result.get("type"))
    title = body = error = ""
    fields: tuple[str, ...] = ()
    if kind == "form":
        step = str(result["step_id"])
        title = text(f"{base}.step.{step}.title", required=False) or (
            text(f"component.{handler}.title")
            if category == "config"
            else OPTIONS_HEADER
        )
        body = text(f"{base}.step.{step}.description", required=False)
        if base_error := (result.get("errors") or {}).get("base"):
            error = text(f"{base}.error.{base_error}") or str(base_error)
        # Field labels come from the handler's texts alone, as in the dialog.
        fields = tuple(
            text(f"{own}.step.{step}.data.{item['name']}") or str(item["name"])
            for item in result.get("data_schema") or []
        )
    elif kind == "progress":
        step = str(result.get("progress_action"))
        body = text(f"{base}.progress.{step}")
    elif kind == "abort":
        step = str(result.get("reason"))
        body = text(f"{base}.abort.{step}") or step
    elif kind == "create_entry":
        # Without a description the dialog shows only its own words.
        step = str(result.get("description") or "default")
        title = str(result.get("title") or "")
        body = text(
            f"{base}.create_entry.{step}", required=bool(result.get("description"))
        )
    else:
        step = str(result.get("step_id", "-"))
    return Screen(kind, step, title, body, error, fields, tuple(missing))


def suggested(result: dict[str, Any], name: str) -> Any:
    """Return the value a form suggests for a field, or None."""
    for item in result.get("data_schema") or []:
        if item.get("name") == name:
            return (item.get("description") or {}).get("suggested_value")
    return None


def base_error(result: dict[str, Any]) -> str | None:
    """Return a form's error, or None."""
    error = (result.get("errors") or {}).get("base")
    return None if error is None else str(error)


# --------------------------------------------------------------------- report


@dataclass(slots=True)
class Check:
    """One check and its outcome."""

    name: str
    ok: bool
    detail: str = ""


@dataclass(slots=True)
class JourneyResult:
    """What one journey did and found."""

    name: str
    title: str
    checks: list[Check] = field(default_factory=list)
    screens: list[Screen] = field(default_factory=list)
    error: str | None = None
    seconds: float = 0.0

    @property
    def ok(self) -> bool:
        """Return whether every check passed and nothing stopped the journey."""
        return (
            self.error is None and bool(self.checks) and all(c.ok for c in self.checks)
        )


def summary(results: Sequence[JourneyResult], wanted: int, refused: int = 0) -> str:
    """Return the run's result in a line; a journey that never ran did not pass."""
    passed = sum(result.ok for result in results)
    line = f"{passed} of {max(wanted, len(results))} journeys passed"
    # A refusal fails the run, even one made after the journeys' own checks.
    return f"{line}; FAIL: the stand-in refused {refused}" if refused else line


def markdown(
    results: Sequence[JourneyResult],
    header: dict[str, str],
    wanted: int = 0,
    refused: int = 0,
) -> str:
    """Return the run's report."""
    lines = ["# Watercare journeys", ""]
    lines += [f"- {name}: {value}" for name, value in header.items()]
    lines += [f"- Result: {summary(results, wanted, refused)}", ""]
    for result in results:
        lines.append(
            f"## {'PASS' if result.ok else 'FAIL'}: {result.title} "
            f"(`{result.name}`, {result.seconds:.0f} s)"
        )
        lines.append("")
        if result.error:
            lines += [f"Stopped: {result.error}", ""]
        lines += ["| Result | Check | Detail |", "| --- | --- | --- |"]
        lines += [
            f"| {'PASS' if check.ok else 'FAIL'} | {check.name} | "
            f"{check.detail.replace('|', '/')} |"
            for check in result.checks
        ]
        lines += ["", "Screens met:", ""]
        for screen in result.screens:
            lines += screen.lines()
        lines.append("")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------- client


class Client:
    """Talk to the instance as its owner, through REST and the websocket."""

    def __init__(self, instance: live_ha.Instance) -> None:
        self.instance = instance
        self._token = ""
        self._token_at = 0.0

    def token(self) -> str:
        """Return an access token, minted again once it is ten minutes old."""
        if not self._token or time.monotonic() - self._token_at > TOKEN_LIFETIME_S:
            self._token = live_ha.access_token(self.instance.state, self.instance.port)
            self._token_at = time.monotonic()
        return self._token

    def forget_token(self) -> None:
        """Mint a new token next time, as after a restart."""
        self._token = ""

    def rest(self, method: str, path: str, data: dict[str, Any] | None = None) -> Any:
        """Send one REST request and return its JSON; raise on an error status."""
        try:
            return live_ha.call(
                self.instance.port,
                method,
                path,
                token=self.token(),
                data=data,
                timeout=REQUEST_TIMEOUT_S,
            )
        except urllib.error.HTTPError as err:
            body = err.read().decode(errors="replace")[:300]
            message = f"{method} {path} returned HTTP {err.code}: {body}"
            raise JourneyError(message) from None

    def ws(self, *messages: dict[str, Any]) -> list[Any]:
        """Send websocket commands in turn and return their results."""
        return asyncio.run(self._ws(list(messages)))

    async def _ws(self, messages: list[dict[str, Any]]) -> list[Any]:
        url = f"http://{live_ha.HOST}:{self.instance.port}/api/websocket"
        results: list[Any] = []
        async with aiohttp.ClientSession() as session, session.ws_connect(url) as ws:
            await ws.receive_json(timeout=30)
            await ws.send_json({"type": "auth", "access_token": self.token()})
            if (auth := await ws.receive_json(timeout=30)).get("type") != "auth_ok":
                raise JourneyError(f"websocket sign-in failed: {auth.get('type')}")
            for number, message in enumerate(messages, start=1):
                await ws.send_json({"id": number, **message})
                while True:
                    reply = await ws.receive_json(timeout=REQUEST_TIMEOUT_S)
                    if reply.get("id") == number and reply.get("type") == "result":
                        break
                if not reply.get("success"):
                    raise JourneyError(
                        f"{message['type']} failed: {reply.get('error')}"
                    )
                results.append(reply.get("result"))
        return results


def wait(what: str, ready: Callable[[], Any], timeout: float) -> Any:
    """Poll ``ready`` until it returns a value; raise after ``timeout`` seconds."""
    deadline = time.monotonic() + timeout
    while True:
        if value := ready():
            return value
        if time.monotonic() >= deadline:
            raise JourneyError(f"timed out after {timeout:.0f} s waiting for {what}")
        time.sleep(POLL_S)


# -------------------------------------------------------------------- journey


def read_refused(folder: Path) -> list[str]:
    """Return the stand-in's record of what it refused in this run, a line each."""
    path = folder / standin.REFUSED
    return path.read_text().splitlines() if path.is_file() else []


@dataclass(slots=True)
class Journey:
    """One journey's context: the instance, the stand-in and what it found."""

    client: Client
    folder: Path
    result: JourneyResult
    today: date = field(default_factory=lambda: datetime.now(NZ).date())
    translations: dict[str, str] = field(default_factory=dict)
    translated: set[tuple[str, str]] = field(default_factory=set)
    entries: list[str] = field(default_factory=list)
    flows: list[str] = field(default_factory=list)
    options_flows: list[str] = field(default_factory=list)
    current: Scenario = field(default_factory=Scenario)

    # ------------------------------------------------------------- recording

    def check(self, name: str, ok: bool, detail: str = "") -> bool:  # noqa: FBT001
        """Record and print one check; return whether it passed."""
        self.result.checks.append(Check(name, ok, detail))
        say(
            f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" ({detail})" if detail else "")
        )
        return ok

    def require(self, name: str, ok: bool, detail: str = "") -> None:  # noqa: FBT001
        """Record one check; stop the journey if it failed."""
        if not self.check(name, ok, detail):
            raise JourneyError(f"{name}: {detail}" if detail else name)

    def screen(self, result: dict[str, Any], category: str = "config") -> Screen:
        """Record a screen the journey met."""
        # The texts the dialog loads. For a config flow: the integration's
        # texts and title, and the config texts of any translation_domain a
        # step names (Home Assistant's own for "reauth_successful", for
        # example). For an options flow: the integration's options texts alone.
        wanted = [(DOMAIN, category)]
        if category == "config":
            wanted.append((DOMAIN, "title"))
            if other := result.get("translation_domain"):
                wanted.append((other, "config"))
        for domain, kind in wanted:
            if (domain, kind) not in self.translated:
                (loaded,) = self.client.ws(
                    {
                        "type": "frontend/get_translations",
                        "language": "en",
                        "category": kind,
                        "integration": [domain],
                    }
                )
                self.translations.update(loaded["resources"])
                self.translated.add((domain, kind))
        shown = render(result, self.translations, category)
        self.result.screens.append(shown)
        say(
            f"  ...  {shown.kind} {shown.step}"
            + (f": {shown.error}" if shown.error else "")
        )
        return shown

    # -------------------------------------------------------------- stand-in

    def watercare(self, **changes: Any) -> Scenario:
        """Return the journeys' scenario with ``changes``: one login, today's bills."""
        base = Scenario(
            logins={EMAIL: ACCOUNT_NUMBER}, password=PASSWORD, as_of=self.today
        )
        return replace(base, **changes)

    def scenario(self, scenario: Scenario) -> None:
        """Set how the stand-in answers from now on."""
        path = self.folder / standin.SCENARIO
        partial = path.with_suffix(".partial")
        partial.write_text(json.dumps(scenario.to_json()))
        partial.replace(path)
        self.current = scenario

    def issued(self) -> tuple[Bill, ...]:
        """Return the bills the stand-in issues now, oldest first."""
        issued = bills(self.current.as_of or self.today)
        return tuple(issued[: len(issued) - self.current.withheld])

    def journal(
        self, since: datetime | None = None, until: datetime | None = None
    ) -> list[dict[str, Any]]:
        """Return the requests the stand-in answered from ``since``, to ``until``."""
        path = self.folder / standin.JOURNAL
        if not path.is_file():
            return []
        records = [json.loads(line) for line in path.read_text().splitlines() if line]
        return [
            r
            for r in records
            if (since is None or datetime.fromisoformat(r["at"]) >= since)
            and (until is None or datetime.fromisoformat(r["at"]) < until)
        ]

    def operations(self, since: datetime, until: datetime | None = None) -> list[str]:
        """Return the operations the stand-in answered from ``since``, to ``until``."""
        return [str(r.get("operation")) for r in self.journal(since, until)]

    # ------------------------------------------------------------------ flows

    def start_flow(self, data: dict[str, Any]) -> dict[str, Any]:
        """Start a config flow and record its first screen."""
        result: dict[str, Any] = self.client.rest(
            "POST", "/api/config/config_entries/flow", {"handler": DOMAIN, **data}
        )
        if flow_id := result.get("flow_id"):
            self.flows.append(str(flow_id))
        self.screen(result)
        return result

    def submit(self, flow_id: str, data: dict[str, Any]) -> dict[str, Any]:
        """Submit a config flow's form and record the screen that follows."""
        result: dict[str, Any] = self.client.rest(
            "POST", f"/api/config/config_entries/flow/{flow_id}", data
        )
        self.screen(result)
        self._note_entry(result)
        return result

    def get_flow(self, flow_id: str) -> dict[str, Any]:
        """Return a config flow's current step, and record it."""
        result: dict[str, Any] = self.client.rest(
            "GET", f"/api/config/config_entries/flow/{flow_id}"
        )
        self.screen(result)
        return result

    def start_options(self, entry_id: str) -> dict[str, Any]:
        """Open the entry's options, as its Configure button does."""
        result: dict[str, Any] = self.client.rest(
            "POST", "/api/config/config_entries/options/flow", {"handler": entry_id}
        )
        if flow_id := result.get("flow_id"):
            self.options_flows.append(str(flow_id))
        self.screen(result, "options")
        return result

    def submit_options(self, flow_id: str, data: dict[str, Any]) -> dict[str, Any]:
        """Submit the options form and record the screen that follows."""
        result: dict[str, Any] = self.client.rest(
            "POST", f"/api/config/config_entries/options/flow/{flow_id}", data
        )
        self.screen(result, "options")
        return result

    def _note_entry(self, result: dict[str, Any]) -> None:
        if result.get("type") == "create_entry":
            self.entries.append(str(result["result"]["entry_id"]))

    def set_up(self, email: str, password: str) -> tuple[dict[str, Any], datetime]:
        """Sign in from Add integration; return the step after it and when."""
        first = self.start_flow({})
        self.require(
            "the setup starts at the sign-in form", first.get("step_id") == "user"
        )
        started = datetime.now(UTC)
        after = self.submit(
            str(first["flow_id"]), {"username": email, "password": password}
        )
        return after, started

    def new_entry(self, email: str = EMAIL) -> tuple[str, Import]:
        """Add the integration, wait for its first statistics update, return it."""
        done, _started = self.set_up(email, self.current.password)
        self.require(
            "the entry is created",
            done.get("type") == "create_entry",
            str(done.get("type")),
        )
        entry_id = self.entries[-1]
        issued = self.issued()
        self.wait_imported(entry_id, rebuilt=True)
        return entry_id, Import(issued)

    # -------------------------------------------------------------- entries

    def entry(self, entry_id: str) -> dict[str, Any] | None:
        """Return a config entry as the REST API lists it, or None."""
        entries = self.client.rest(
            "GET", f"/api/config/config_entries/entry?domain={DOMAIN}"
        )
        return next((e for e in entries if e["entry_id"] == entry_id), None)

    def created(self, entry_id: str) -> datetime:
        """Return when Home Assistant created the entry."""
        entry = self.entry(entry_id)
        if entry is None:
            raise JourneyError(f"entry {entry_id} is gone")
        return datetime.fromtimestamp(float(entry["created_at"]), UTC)

    def diagnostics(self, entry_id: str) -> dict[str, Any]:
        """Return the entry's diagnostics."""
        loaded = self.client.rest("GET", f"/api/diagnostics/config_entry/{entry_id}")
        data: dict[str, Any] = loaded["data"]
        return data

    def wait_imported(
        self, entry_id: str, days: int | None = None, *, rebuilt: bool = False
    ) -> dict[str, Any]:
        """Wait for the entry's statistics update; return the coordinator's state.

        With ``rebuilt``, the first update of a new entry, which rebuilds the
        statistics from every bill; otherwise an update that adds ``days``.
        """

        def imported() -> dict[str, Any] | None:
            entry = self.entry(entry_id)
            if entry is None or entry["state"] != "loaded":
                return None
            try:
                coordinator: dict[str, Any] = self.diagnostics(entry_id)["coordinator"]
            except JourneyError, KeyError:
                return None
            status = coordinator.get("statistics_status")
            if status == "rebuild_skipped":
                message = "the integration skipped its statistics rebuild"
                raise JourneyError(message)
            last = coordinator.get("last_import") or {}
            if rebuilt:
                done = status == "rebuilt" and last.get("rebuilt")
            else:
                done = last.get("consumption_rows") == days
            return coordinator if done else None

        return dict(wait("the statistics update", imported, SYNC_TIMEOUT_S))

    def entity_ids(self, entry_id: str) -> dict[str, str]:
        """Return the entry's enabled entities, by unique ID."""
        (registry,) = self.client.ws({"type": "config/entity_registry/list"})
        return {
            str(item["unique_id"]): str(item["entity_id"])
            for item in registry
            if item.get("config_entry_id") == entry_id and not item.get("disabled_by")
        }

    def update(self, entry_id: str) -> None:
        """Ask the entry for an update now, as Update entity does."""
        entity = self.entity_ids(entry_id)[f"{entry_id}_usage"]
        self.client.rest(
            "POST", "/api/services/homeassistant/update_entity", {"entity_id": entity}
        )

    def reload(self, entry_id: str) -> None:
        """Reload the entry, as its Reload menu item does."""
        self.client.rest("POST", f"/api/config/config_entries/entry/{entry_id}/reload")

    def statistics(self) -> dict[str, list[Row]]:
        """Return every row of the four statistics, by kind."""
        (loaded,) = self.client.ws(
            {
                "type": "recorder/statistics_during_period",
                "start_time": datetime(2000, 1, 1, tzinfo=UTC).isoformat(),
                "statistic_ids": sorted(STATISTICS.values()),
                "period": "hour",
                "types": ["state", "sum"],
            }
        )
        return {
            kind: parse_rows(loaded.get(sid, [])) for kind, sid in STATISTICS.items()
        }

    def clear_statistics(self) -> None:
        """Delete the four statistics, so the journey starts as a new install."""
        self.client.ws(
            {
                "type": "recorder/clear_statistics",
                "statistic_ids": sorted(STATISTICS.values()),
            }
        )
        wait(
            "the statistics to be cleared",
            lambda: not any(self.statistics().values()),
            ROWS_TIMEOUT_S,
        )

    def wait_rows(self, expected: Expected) -> dict[str, list[Row]]:
        """Return the statistics once they hold the expected days, or as they are."""

        def complete() -> dict[str, list[Row]] | None:
            rows = self.statistics()
            for kind, wanted in expected.days.items():
                found = [row.day for row in rows[kind]]
                if found != [item.day for item in wanted]:
                    return None
            return rows

        try:
            return dict(wait("the statistics", complete, ROWS_TIMEOUT_S))
        except JourneyError:
            return self.statistics()

    def remove_entries(self) -> None:
        """Remove the journey's entries and close its flows, as they stand."""
        for flow_id in self.flows:
            with contextlib.suppress(JourneyError, OSError):
                self.client.rest("DELETE", f"/api/config/config_entries/flow/{flow_id}")
        for flow_id in self.options_flows:
            with contextlib.suppress(JourneyError, OSError):
                self.client.rest(
                    "DELETE", f"/api/config/config_entries/options/flow/{flow_id}"
                )
        for entry_id in self.entries:
            with contextlib.suppress(JourneyError, OSError):
                self.client.rest(
                    "DELETE", f"/api/config/config_entries/entry/{entry_id}"
                )

    # ---------------------------------------------------------- common checks

    def check_entry(
        self, entry_id: str, imports: Sequence[Import]
    ) -> dict[str, list[Row]]:
        """Run the checks every journey that made an entry ends with.

        The entry is loaded; its sensors exist, none unavailable, and show the
        newest bill; the four statistics hold the days they should
        (``history_problems``); the repair notices are those the prices call
        for; and the stand-in refused no request. Returns the rows.
        """
        entry = self.entry(entry_id)
        self.check(
            "the entry is loaded",
            entry is not None and entry["state"] == "loaded",
            "" if entry is None else str(entry["state"]),
        )
        entities = self.entity_ids(entry_id)
        states = {
            unique_id: str(self.client.rest("GET", f"/api/states/{entity}")["state"])
            for unique_id, entity in entities.items()
        }
        unavailable = sorted(
            entities[unique_id]
            for unique_id, state in states.items()
            if state == "unavailable"
        )
        self.check(
            "the sensors exist and none is unavailable",
            bool(entities) and not unavailable,
            f"{len(entities)} sensors"
            + (f"; unavailable: {', '.join(unavailable)}" if unavailable else ""),
        )
        self.check_newest_bill(entry_id, states, imports[-1])
        expected = expected_statistics(imports)
        rows = self.wait_rows(expected)
        for kind, statistic_id in STATISTICS.items():
            problems = history_problems(kind, rows[kind], expected)
            self.check(
                f"{statistic_id}: every day once, as the bills and prices say, "
                "sums consistent",
                not problems,
                "; ".join(problems) if problems else _span([r.day for r in rows[kind]]),
            )
        self.check_notices(entry_id, imports)
        self.check_nothing_refused()
        return rows

    def check_newest_bill(
        self, entry_id: str, states: Mapping[str, str], latest: Import
    ) -> None:
        """Check that the usage and cost sensors show the newest bill."""
        bill = latest.bills[-1]
        prices = latest.pricing.prices(financial_year(bill.start))
        usage = states.get(f"{entry_id}_usage")
        cost = states.get(f"{entry_id}_current_bill_cost")
        want_cost = (
            None
            if prices is None
            else float(bill_cost(bill, prices, latest.pricing.ratio)["total_cost"])
        )
        ok_cost = (
            cost == "unknown"
            if want_cost is None
            else cost is not None
            and _number(cost) is not None
            and abs(float(cost) - want_cost) <= CENT
        )
        self.check(
            "the sensors show the newest bill: its litres and its cost",
            _number(usage) == float(bill.litres) and ok_cost,
            f"usage {usage} (bill {bill.litres} L), cost {cost} "
            f"(expected {'unknown' if want_cost is None else f'{want_cost:.2f}'})",
        )

    def check_notices(self, entry_id: str, imports: Sequence[Import]) -> None:
        """Check that the entry's repair notices are those the prices call for."""
        (listed,) = self.client.ws({"type": "repairs/list_issues"})
        notices = {
            str(issue["issue_id"]): issue
            for issue in listed["issues"]
            if issue["domain"] == DOMAIN and str(issue["issue_id"]).endswith(entry_id)
        }
        year = tariff_notice(imports, self.today)
        wanted = {} if year is None else {f"tariff_missing_{entry_id}": year}
        found_years = {
            issue_id: (issue.get("translation_placeholders") or {}).get(
                "financial_year"
            )
            for issue_id, issue in notices.items()
        }
        self.check(
            "the repair notices are those the prices call for",
            found_years
            == {issue_id: year_label(year) for issue_id, year in wanted.items()},
            ", ".join(f"{key} ({value})" for key, value in found_years.items())
            or "none",
        )

    def check_nothing_refused(self) -> None:
        """Check that the stand-in refused nothing and answered every request."""
        refused = read_refused(self.folder)
        self.check(
            "the stand-in refused no outbound request",
            not refused,
            f"{len(refused)} refused: {'; '.join(refused[:3])}" if refused else "",
        )
        records = self.journal()
        errors = [r["error"] for r in records if "error" in r]
        self.check(
            "the fake answered every request the integration sent",
            bool(records) and not errors,
            "; ".join(sorted(set(errors))[:3])
            if errors
            else f"answered: {len(records)}"
            if records
            else "no requests at all",
        )

    def check_screens(self) -> None:
        """Check that every screen met has its words.

        Where a title, label, error or abort reason has no translation, the
        dialog shows nothing or the raw key, such as ``reauth_successful``.
        """
        if not self.result.screens:
            return
        missing = [
            f"{screen.kind} {screen.step}: {', '.join(screen.missing)}"
            for screen in self.result.screens
            if screen.missing
        ]
        self.check(
            "every screen met has its words, no raw keys",
            not missing,
            "no translation for " + "; ".join(missing) if missing else "",
        )

    def check_unchanged(
        self, when: str, before: Mapping[str, list[Row]], after: Mapping[str, list[Row]]
    ) -> None:
        """Check that every day stored before ``when`` is still there, unchanged."""
        for kind, statistic_id in STATISTICS.items():
            changed = changed_rows(before[kind], after[kind])
            self.check(
                f"{statistic_id}: the days stored before {when} are unchanged",
                not changed,
                "; ".join(changed[:3]) or f"{len(before[kind])} days identical",
            )


def _number(value: str | None) -> float | None:
    try:
        return None if value is None else float(value)
    except ValueError:
        return None


def _sign_in_statuses(records: Iterable[dict[str, Any]]) -> list[str]:
    """Return the sign-in and API answers in order, as ``operation status``."""
    return [
        f"{r.get('operation')} {r.get('status')}"
        + ("" if "accepted" not in r else f" accepted={r['accepted']}")
        for r in records
    ]


# ------------------------------------------------------------------- journeys


def first_setup(j: Journey) -> None:
    """First setup: email and password, the entry, the history imported."""
    j.scenario(j.watercare())
    done, started = j.set_up(EMAIL, PASSWORD)
    j.require(
        "the entry is created",
        done.get("type") == "create_entry",
        str(done.get("type")),
    )
    j.check(
        "the screens met",
        [(s.kind, s.step) for s in j.result.screens]
        == [("form", "user"), ("create_entry", "default")],
        " → ".join(f"{s.kind} {s.step}" for s in j.result.screens),
    )
    entry_id = j.entries[-1]
    issued = j.issued()
    coordinator = j.wait_imported(entry_id, rebuilt=True)
    days = sum(bill.days for bill in issued)
    last = coordinator.get("last_import") or {}
    # The diagnostics' bill count is compared, not printed: CodeQL takes any
    # value named "billing" for private data, although these bills are synthetic.
    every_bill = coordinator.get("billing_periods") == len(issued)
    j.check(
        "the first update imported every bill, every day",
        every_bill
        and last.get("consumption_rows") == days
        and last.get("cost_rows") == len(COSTS) * days,
        f"{'every one' if every_bill else 'not every one'} of {len(issued)} bills, "
        f"{last.get('consumption_rows')} usage and {last.get('cost_rows')} cost "
        f"rows (expected {days}, {len(COSTS) * days})",
    )
    # The setup signs in before Home Assistant creates the entry, and the
    # entry's first update starts after it.
    created = j.created(entry_id)
    in_setup = j.operations(started, created)
    j.check(
        "the setup signed in once and read the account",
        in_setup == SIGN_IN,
        ", ".join(in_setup) or "no requests",
    )
    first_update = j.operations(created)
    j.check(
        "the entry's first update used the refresh token, with no sign-in",
        first_update[:1] == ["refresh"]
        and "authorize" not in first_update
        and "billing_periods" in first_update,
        ", ".join(first_update) or "no requests",
    )
    j.check_entry(entry_id, [Import(issued)])


def wrong_password(j: Journey) -> None:
    """Try a wrong password, then the right one."""
    j.scenario(j.watercare())
    first = j.start_flow({})
    flow_id = str(first["flow_id"])
    began = datetime.now(UTC)
    wrong = j.submit(flow_id, {"username": EMAIL, "password": WRONG_PASSWORD})
    j.check(
        "the form says Watercare rejected the email or password",
        wrong.get("step_id") == "user" and base_error(wrong) == "invalid_auth",
        j.result.screens[-1].error,
    )
    j.check(
        "the form keeps the email",
        suggested(wrong, "username") == EMAIL,
        str(suggested(wrong, "username")),
    )
    statuses = _sign_in_statuses(j.journal(began))
    j.check(
        "Watercare's sign-in turned the password down",
        statuses == ["authorize 200", "self_asserted 200 accepted=False"],
        ", ".join(statuses),
    )
    entries = j.client.rest("GET", f"/api/config/config_entries/entry?domain={DOMAIN}")
    j.check("no entry yet", not entries, f"{len(entries)} entries")
    done = j.submit(flow_id, {"username": EMAIL, "password": PASSWORD})
    j.require(
        "the right password creates the entry",
        done.get("type") == "create_entry",
        str(done.get("type")),
    )
    entry_id = j.entries[-1]
    issued = j.issued()
    j.wait_imported(entry_id, rebuilt=True)
    j.check_entry(entry_id, [Import(issued)])


def watercare_down(j: Journey) -> None:
    """Watercare down at setup, then back."""
    j.scenario(j.watercare(api_status=503))
    first = j.start_flow({})
    flow_id = str(first["flow_id"])
    began = datetime.now(UTC)
    down = j.submit(flow_id, {"username": EMAIL, "password": PASSWORD})
    j.check(
        "the form says it could not connect",
        down.get("step_id") == "user" and base_error(down) == "cannot_connect",
        j.result.screens[-1].error,
    )
    statuses = _sign_in_statuses(j.journal(began))
    j.check(
        "the sign-in worked and Watercare's API answered 503",
        "self_asserted 200 accepted=True" in statuses and statuses[-1] == "account 503",
        ", ".join(statuses),
    )
    j.scenario(j.watercare())
    done = j.submit(flow_id, {"username": EMAIL, "password": PASSWORD})
    j.require(
        "with Watercare back, the same form creates the entry",
        done.get("type") == "create_entry",
        str(done.get("type")),
    )
    entry_id = j.entries[-1]
    issued = j.issued()
    j.wait_imported(entry_id, rebuilt=True)
    j.check_entry(entry_id, [Import(issued)])


def reauth(j: Journey) -> None:
    """Watercare no longer takes the stored login; sign in again."""
    j.scenario(j.watercare(withheld=1))
    entry_id, first = j.new_entry()
    before = j.check_entry(entry_id, [first])
    # The password changed at Watercare, which ends every earlier sign-in.
    changed = j.watercare(withheld=1, password=NEW_PASSWORD, generation=2)
    j.scenario(changed)
    began = datetime.now(UTC)
    j.update(entry_id)

    def reauth_flow() -> dict[str, Any] | None:
        (flows,) = j.client.ws({"type": "config_entries/flow/progress"})
        return next(
            (
                flow
                for flow in flows
                if flow["handler"] == DOMAIN
                and flow["context"].get("source") == "reauth"
                and flow["context"].get("entry_id") == entry_id
            ),
            None,
        )

    # The journey stops here if no reauthentication starts.
    found = wait("reauthentication to start", reauth_flow, REAUTH_TIMEOUT_S)
    statuses = _sign_in_statuses(j.journal(began))
    j.check(
        "Watercare refused the access token, the refresh token and the old password",
        statuses[:4]
        == [
            "account 401",
            "refresh 400",
            "authorize 200",
            "self_asserted 200 accepted=False",
        ],
        ", ".join(statuses),
    )
    flow_id = str(found["flow_id"])
    j.flows.append(flow_id)
    confirm = j.get_flow(flow_id)
    j.check(
        "the reauthentication asks for the password of the stored email",
        confirm.get("step_id") == "reauth_confirm"
        and EMAIL in j.result.screens[-1].text,
        j.result.screens[-1].text,
    )
    # The newest bill arrives meanwhile: the update after the reload adds it.
    j.scenario(replace(changed, withheld=0))
    signed_in = datetime.now(UTC)
    result = j.submit(flow_id, {"password": NEW_PASSWORD})
    j.check(
        "the sign-in is renewed",
        result.get("type") == "abort" and result.get("reason") == "reauth_successful",
        str(result.get("reason")),
    )
    issued = j.issued()
    j.wait_imported(entry_id, issued[-1].days)
    # The flow signs in; then the reloaded entry's update starts.
    operations = j.operations(signed_in)
    after_sign_in = operations[len(SIGN_IN) :]
    j.check(
        "the new password signed in once; the reloaded entry then used the "
        "new refresh token, with no sign-in",
        operations[: len(SIGN_IN)] == SIGN_IN
        and after_sign_in[:1] == ["refresh"]
        and "authorize" not in after_sign_in
        and "billing_periods" in after_sign_in,
        ", ".join(operations) or "no requests",
    )
    after = j.check_entry(entry_id, [first, Import(issued)])
    j.check_unchanged("the reauthentication", before, after)


def reconfigure(j: Journey) -> None:
    """Reconfigure: a new email for the same account; another account refused."""
    logins = {
        EMAIL: ACCOUNT_NUMBER,
        RENAMED_EMAIL: ACCOUNT_NUMBER,
        OTHER_ACCOUNT_EMAIL: SECOND_ACCOUNT_NUMBER,
    }
    j.scenario(j.watercare(logins=logins, withheld=1))
    entry_id, first = j.new_entry()
    before = j.check_entry(entry_id, [first])
    other = j.start_flow({"entry_id": entry_id})
    j.require(
        "Reconfigure shows the sign-in form", other.get("step_id") == "reconfigure"
    )
    j.check(
        "Reconfigure suggests the stored email",
        suggested(other, "username") == EMAIL,
        str(suggested(other, "username")),
    )
    began = datetime.now(UTC)
    refused = j.submit(
        str(other["flow_id"]), {"username": OTHER_ACCOUNT_EMAIL, "password": PASSWORD}
    )
    reached = {str(r["account"]) for r in j.journal(began) if r.get("account")}
    j.check(
        "another account's login is refused",
        refused.get("type") == "abort"
        and refused.get("reason") == "wrong_account"
        and reached == {SECOND_ACCOUNT_NUMBER},
        f"{refused.get('reason')}; the login reached "
        f"{', '.join(sorted(reached)) or 'no account'}",
    )
    j.scenario(j.watercare(logins=logins, withheld=0))
    renamed = j.start_flow({"entry_id": entry_id})
    result = j.submit(
        str(renamed["flow_id"]), {"username": RENAMED_EMAIL, "password": PASSWORD}
    )
    j.check(
        "a new email for the same account is saved",
        result.get("type") == "abort"
        and result.get("reason") == "reconfigure_successful",
        str(result.get("reason")),
    )
    issued = j.issued()
    j.wait_imported(entry_id, issued[-1].days)
    after = j.check_entry(entry_id, [first, Import(issued)])
    j.check_unchanged("the reconfiguration", before, after)
    again = j.start_flow({"entry_id": entry_id})
    j.check(
        "Reconfigure now suggests the new email",
        suggested(again, "username") == RENAMED_EMAIL,
        str(suggested(again, "username")),
    )


def options(j: Journey) -> None:
    """Options: a new wastewater ratio and this year's prices."""
    j.scenario(j.watercare(withheld=1))
    entry_id, first = j.new_entry()
    before = j.check_entry(entry_id, [first])
    year = financial_year(j.today)
    known = first.pricing.prices(year)
    form = j.start_options(entry_id)
    step = form.get("step_id")
    if known is not None:
        shown = {name: suggested(form, name) for name in known.as_options()}
        j.check(
            f"the options show the {year_label(year)} prices and the ratio",
            step == "init"
            and shown == known.as_options()
            and suggested(form, "wastewater_ratio") == float(DEFAULT_RATIO),
            f"{step}: {shown}, ratio {suggested(form, 'wastewater_ratio')}",
        )
    else:
        j.check(
            f"the options ask for the {year_label(year)} prices, empty",
            step == "new_year"
            and all(suggested(form, name) is None for name in NEW_PRICES.as_options()),
            str(step),
        )
    j.require(
        "the options open on the init or new_year step",
        step in {"init", "new_year"},
        str(step),
    )
    # The newest bill arrives after the change: its days take the new prices.
    j.scenario(j.watercare(withheld=0))
    saved = j.submit_options(
        str(form["flow_id"]),
        {"wastewater_ratio": float(NEW_RATIO), **NEW_PRICES.as_options()},
    )
    j.check("the options are saved", saved.get("type") == "create_entry")
    issued = j.issued()
    coordinator = j.wait_imported(entry_id, issued[-1].days)
    stored = j.diagnostics(entry_id)["config_entry"]
    j.check(
        "the entry keeps the ratio and the year's prices",
        stored.get("wastewater_ratio") == float(NEW_RATIO)
        and stored.get("tariff_override_years") == [year_label(year)],
        f"ratio {stored.get('wastewater_ratio')}, "
        f"own prices for {stored.get('tariff_override_years')}",
    )
    pricing = Pricing(NEW_RATIO, {year: NEW_PRICES})
    after = j.check_entry(entry_id, [first, Import(issued, pricing)])
    # docs/statistics.md: stored history is never repriced; days recorded
    # after the change take the new ratio and prices.
    j.check_unchanged("the change", before, after)
    newest = issued[-1]
    new_days = [row for row in after["total_cost"] if row.day >= newest.start]
    prices = pricing.prices(financial_year(newest.start))
    want = (
        None if prices is None else bill_cost(newest, prices, NEW_RATIO)["total_cost"]
    )
    j.check(
        "the new bill's days, recorded after the change, cost the bill at the "
        "new options",
        want is not None
        and len(new_days) == newest.days
        and abs(sum(row.state for row in new_days) - float(want)) <= TOLERANCE,
        f"{len(new_days)} days, {sum(row.state for row in new_days):.2f} NZD "
        f"(expected {'none' if want is None else f'{want:.2f}'}); "
        f"{coordinator.get('last_import')}",
    )


def restart(j: Journey, relaunch: Callable[[], None]) -> None:
    """Reload the entry, then restart Home Assistant: the history stays whole."""
    j.scenario(j.watercare(withheld=2))
    entry_id, first = j.new_entry()
    imports = [first]
    before = j.check_entry(entry_id, imports)
    for when, withheld in (("the reload", 1), ("the restart", 0)):
        # One more bill each time, so each update has days to add.
        j.scenario(j.watercare(withheld=withheld))
        began = datetime.now(UTC)
        if when == "the reload":
            j.reload(entry_id)
        else:
            relaunch()
        issued = j.issued()
        newest = issued[-1].days
        coordinator = j.wait_imported(entry_id, newest)
        last = coordinator.get("last_import") or {}
        j.check(
            f"after {when}, the update added only the new bill's days",
            last.get("consumption_rows") == newest
            and last.get("cost_rows") == len(COSTS) * newest
            and not last.get("rebuilt"),
            f"{last.get('consumption_rows')} usage and {last.get('cost_rows')} "
            f"cost rows (expected {newest} and {len(COSTS) * newest})",
        )
        operations = j.operations(began)
        j.check(
            f"after {when}, the entry resumed with its refresh token, no sign-in",
            operations[:1] == ["refresh"] and "authorize" not in operations,
            ", ".join(operations) or "no requests",
        )
        imports.append(Import(issued))
        after = j.check_entry(entry_id, imports)
        j.check_unchanged(when, before, after)
        before = after


JOURNEYS: dict[str, str] = {
    "first_setup": "First setup: email and password, entry, history imported",
    "wrong_password": "Wrong password, then the right one",
    "watercare_down": "Watercare down at setup, then back",
    "reauth": "Watercare no longer takes the stored login: sign in again",
    "reconfigure": "Reconfigure: new email, same account; another account refused",
    "options": "Options: wastewater ratio and this year's prices",
    "restart": "Restart and reload, history unbroken",
}


# --------------------------------------------------------------------- runner


def site_files(folder: Path) -> dict[str, bytes]:
    """Return the stand-in's files for the instance's site-packages."""
    package = standin.PACKAGE
    files = {
        f"{package}/__init__.py": (
            ROOT / "tests" / "watercare_standin.py"
        ).read_bytes(),
        f"{package}/{standin.SETTINGS}": json.dumps({"folder": str(folder)}).encode(),
        f"{package}.pth": standin.PTH_LINE.encode(),
    }
    for name in _FAKES:
        files[f"{package}/{name}"] = (ROOT / "tests" / name).read_bytes()
    return files


@dataclass(slots=True)
class Run:
    """One run of the journeys against one instance."""

    instance: live_ha.Instance
    folder: Path
    today: date = field(default_factory=lambda: datetime.now(NZ).date())
    results: list[JourneyResult] = field(default_factory=list)

    def check_standin(self) -> None:
        """Stop unless the stand-in is installed in Home Assistant's process."""
        marker = self.folder / standin.INSTALLED.format(pid=self.instance.pid)
        if not marker.is_file():
            raise SystemExit(
                "The stand-in is not installed in Home Assistant's process: stopping"
            )

    def relaunch(self) -> None:
        """Restart Home Assistant on the same folder and check it again."""
        self.instance = live_ha.relaunch(
            self.instance, DOMAIN, live_ha.SYSTEM_ENTRIES | {DOMAIN}
        )
        self.check_standin()

    def run(self, name: str) -> JourneyResult:
        """Run one journey, record what it found, and remove what it made."""
        result = JourneyResult(name, JOURNEYS[name])
        # Recorded first, so that the report has it whatever happens.
        self.results.append(result)
        say(f"Journey {name}: {result.title}")
        client = Client(self.instance)
        journey = Journey(client, self.folder, result, self.today)
        began = time.monotonic()
        try:
            # Each journey starts as a new install: no Watercare statistics.
            journey.clear_statistics()
            if name == "restart":

                def relaunch() -> None:
                    self.relaunch()
                    client.instance = self.instance
                    client.forget_token()

                restart(journey, relaunch)
            else:
                globals()[name](journey)
        except Exception as err:  # noqa: BLE001
            # Any failure fails this journey; the next one still runs.
            result.error = f"{type(err).__name__}: {err}"
            say(f"  FAIL  stopped: {result.error}")
        except BaseException as err:
            # Home Assistant failed to start again, or the run was interrupted:
            # the run cannot go on.
            result.error = f"the run stopped: {type(err).__name__}" + (
                f": {err}" if str(err) else ""
            )
            say(f"  FAIL  {result.error}")
            raise
        finally:
            try:
                journey.check_screens()
                journey.remove_entries()
            except Exception as err:  # noqa: BLE001
                # What is left could change the next journey: this one fails.
                result.error = result.error or (
                    f"the clean-up failed: {type(err).__name__}: {err}"
                )
                say(f"  FAIL  {result.error}")
            result.seconds = time.monotonic() - began
        say(
            f"{'PASS' if result.ok else 'FAIL'} journey {name} ({result.seconds:.0f} s)"
        )
        return result


def write_report(
    results: Sequence[JourneyResult],
    header: dict[str, str],
    wanted: int,
    refused: int = 0,
) -> Path:
    """Write the report to the system's temporary directory; return its path."""
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = Path(tempfile.gettempdir()) / f"watercare-journeys-{stamp}.md"
    path.write_text(markdown(results, header, wanted, refused))
    return path


def journeys_port(value: str) -> int:
    """Return ``value`` as a port for the journeys, never the live test's."""
    port = live_ha.port_number(value)
    if port == live_ha.DEFAULT_PORT:
        message = f"{port} is the live test's port; use another"
        raise argparse.ArgumentTypeError(message)
    return port


def parse(argv: Sequence[str] | None) -> argparse.Namespace:
    """Parse the command line."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0].strip())
    parser.add_argument("--ref", help="a release tag or commit to test")
    parser.add_argument("--ha-version", help="default: the newest release")
    parser.add_argument("--port", type=journeys_port, default=DEFAULT_PORT)
    parser.add_argument("--only", nargs="+", choices=list(JOURNEYS), metavar="NAME")
    parser.add_argument(
        "--clean-up", action="store_true", help="only delete a left-over instance"
    )
    return parser.parse_args(argv)


def fail(message: str) -> NoReturn:
    """Stop the run with a message."""
    raise SystemExit(message)


@contextlib.contextmanager
def only_run(state: Path) -> Iterator[None]:
    """Hold the journeys' lock, beside their folder, or stop if a run holds it.

    The system releases the lock when the process ends, however it ends.
    """
    lock = state.with_name(f"{state.name}.lock")
    with lock.open("a") as file:
        try:
            fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            fail(f"Another run is using {state}")
        yield


def main(argv: Sequence[str] | None = None) -> int:
    """Build the instance, run the journeys, report, and delete it all."""
    args = parse(argv)
    domain = live_ha.integration_domain()
    if domain != DOMAIN:
        fail(f"These journeys are for {DOMAIN}, not {domain}")
    state = live_ha.state_dir(domain, NAME)
    with only_run(state):
        if args.clean_up:
            live_ha.stop(NAME)
            return 0
        if state.exists():
            fail(
                f"{state} exists, left by a run that was killed: --clean-up deletes it"
            )
        return run_journeys(args, state)


def run_journeys(args: argparse.Namespace, state: Path) -> int:
    """Run the journeys in a new instance in ``state``, report, and delete it all."""
    names = [name for name in JOURNEYS if not args.only or name in args.only]
    folder = live_ha.config_dir(state) / STANDIN_FOLDER
    began = time.monotonic()
    header = {
        "Started": datetime.now(UTC).isoformat(timespec="seconds"),
        "Integration": args.ref or "the working tree",
    }
    results: list[JourneyResult] = []
    # Everything the stand-in refused in the run, up to Home Assistant's
    # shutdown: the journeys check it only up to their own last checks.
    refused: list[str] = []

    def read_at_the_end(state: Path) -> None:
        refused.extend(read_refused(live_ha.config_dir(state) / STANDIN_FOLDER))

    try:
        instance = live_ha.launch(
            args.ref,
            args.ha_version,
            args.port,
            name=NAME,
            site_files=site_files(folder),
        )
        header["Home Assistant"] = instance.version
        run = Run(instance, folder)
        header["Bills issued up to"] = run.today.isoformat()
        results = run.results
        run.check_standin()
        say(f"The stand-in answers for Watercare in Home Assistant {instance.version}")
        for name in names:
            run.run(name)
    finally:
        live_ha.stop(NAME, before_delete=read_at_the_end)
        header["Duration"] = f"{time.monotonic() - began:.0f} s"
        if results:
            header["Refused by the stand-in"] = (
                f"{len(refused)}: {'; '.join(refused[:3])}" if refused else "nothing"
            )
            if refused:
                say(f"FAIL the stand-in refused {len(refused)}: {refused[0]}")
            report = write_report(results, header, len(names), len(refused))
            say(f"{summary(results, len(names), len(refused))}; report: {report}")
    ran_all = len(results) == len(names) and all(result.ok for result in results)
    return 0 if ran_all and not refused else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
