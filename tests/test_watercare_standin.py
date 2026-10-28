"""Contracts for the stand-in Watercare the journeys run Home Assistant against."""

from __future__ import annotations

import errno
import json
import os
import socket
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import Any

import aiohttp
import pytest

from custom_components.watercare import api as client
from custom_components.watercare.api import (
    WatercareApi,
    WatercareAuthError,
    WatercareConnectionError,
)
from custom_components.watercare.models import parse_billing_periods

from . import watercare_standin
from .watercare_http import ACCOUNT_NUMBER, EMAIL, PASSWORD, bills
from .watercare_standin import Scenario, StandIn, is_local, needs_lookup

_TODAY = date(2026, 10, 28)
_ACCOUNT_URL = f"{client._API_BASE}v1/account"


@pytest.fixture
def folder(tmp_path: Path) -> Path:
    path = tmp_path / "standin"
    path.mkdir()
    return path


@pytest.fixture
def local_requests(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Stand in for aiohttp's own request, which a local request reaches."""
    sent: list[str] = []

    async def original(
        _session: aiohttp.ClientSession, method: str, url: Any, **_options: Any
    ) -> str:
        sent.append(f"{method} {url}")
        return "sent"

    monkeypatch.setattr(aiohttp.ClientSession, "_request", original)
    return sent


@pytest.fixture
def standin(folder: Path, local_requests: list[str]) -> Iterator[StandIn]:
    del local_requests
    installed = StandIn(folder)
    undo = watercare_standin.patch(installed)
    try:
        yield installed
    finally:
        undo()


def _scenario(folder: Path, scenario: Scenario) -> None:
    (folder / watercare_standin.SCENARIO).write_text(json.dumps(scenario.to_json()))


def _journal(folder: Path) -> list[dict[str, Any]]:
    path = folder / watercare_standin.JOURNAL
    return [json.loads(line) for line in path.read_text().splitlines()]


def _refused(folder: Path) -> list[str]:
    path = folder / watercare_standin.REFUSED
    if not path.is_file():
        return []
    return [line.split(" ", 1)[1] for line in path.read_text().splitlines()]


def _client(session: aiohttp.ClientSession, **kwargs: Any) -> WatercareApi:
    """Return the integration's real client on a real aiohttp session."""
    return WatercareApi(
        kwargs.pop("email", EMAIL),
        kwargs.pop("password", PASSWORD),
        session,
        **kwargs,
    )


def test_the_default_scenario_is_the_fakes_synthetic_login() -> None:
    scenario = Scenario()
    assert dict(scenario.logins) == {EMAIL: ACCOUNT_NUMBER}
    assert scenario.password == PASSWORD


def test_a_scenario_round_trips_and_keeps_defaults() -> None:
    scenario = Scenario(
        logins={"a@example.com": "1000001-01", "b@example.com": "2000002-02"},
        password="another-synthetic-password",
        generation=3,
        api_status=503,
        withheld=2,
        as_of=_TODAY,
    )
    assert Scenario.from_json(json.loads(json.dumps(scenario.to_json()))) == scenario
    assert Scenario.from_json({}) == Scenario()
    with pytest.raises(TypeError, match="logins"):
        Scenario.from_json({"logins": ["a@example.com"]})


@pytest.mark.parametrize(
    ("host", "local", "lookup"),
    [
        ("127.0.0.1", True, False),
        ("127.8.9.10", True, False),
        ("::1", True, False),
        ("[::1]", True, False),
        ("localhost", True, False),
        (b"localhost", True, False),
        ("192.0.2.1", False, False),
        ("2001:db8::1", False, False),
        ("api.example.invalid", False, True),
        (None, False, False),
        ("", False, False),
    ],
)
def test_only_this_computer_is_local(
    host: str | bytes | None, local: bool, lookup: bool
) -> None:
    assert is_local(host) is local
    assert needs_lookup(host) is lookup


async def test_a_request_to_another_host_is_refused_and_logged(
    standin: StandIn, folder: Path, local_requests: list[str]
) -> None:
    del standin
    async with aiohttp.ClientSession() as session:
        with pytest.raises(aiohttp.ClientConnectionError, match="stand-in"):
            await session.get("https://updates.example.invalid/path?secret=value")
    assert _refused(folder) == ["request updates.example.invalid"]
    assert "secret" not in (folder / watercare_standin.REFUSED).read_text()
    assert local_requests == []


async def test_a_request_to_this_computer_goes_ahead(
    standin: StandIn, folder: Path, local_requests: list[str]
) -> None:
    del standin
    async with aiohttp.ClientSession() as session:
        answer: Any = await session._request("GET", "http://127.0.0.1:8126/api/")
    assert answer == "sent"
    assert local_requests == ["GET http://127.0.0.1:8126/api/"]
    assert _refused(folder) == []


async def test_the_integrations_client_runs_unchanged_against_the_fake(
    standin: StandIn, folder: Path
) -> None:
    del standin
    _scenario(folder, Scenario(as_of=_TODAY, withheld=1))
    stored: list[str] = []
    async with aiohttp.ClientSession() as session:
        api = _client(session, token_callback=stored.append)
        account = await api.async_get_account()
        periods = parse_billing_periods(await api.async_get_billing_periods())
    assert account.account_number == ACCOUNT_NUMBER
    assert periods.periods[-1].end == bills(_TODAY)[-2].end
    journal = _journal(folder)
    assert [record["operation"] for record in journal] == [
        "authorize",
        "self_asserted",
        "confirmed",
        "token",
        "account",
        "billing_periods",
    ]
    assert journal[1]["accepted"] is True
    assert journal[-1]["account"] == ACCOUNT_NUMBER
    assert all("error" not in record for record in journal)
    text = (folder / watercare_standin.JOURNAL).read_text()
    for secret in (EMAIL, PASSWORD, stored[0]):
        assert secret not in text
    assert _refused(folder) == []


async def test_the_scenario_is_read_on_each_request(
    standin: StandIn, folder: Path
) -> None:
    del standin
    async with aiohttp.ClientSession() as session:
        _scenario(folder, Scenario(api_status=503))
        api = _client(session)
        with pytest.raises(WatercareConnectionError, match="503"):
            await api.async_get_account()
        _scenario(folder, Scenario())
        await api.async_get_account()
        # A new generation ends the sign-in, and the password has changed.
        _scenario(folder, Scenario(generation=2, password="a-new-password"))
        with pytest.raises(WatercareAuthError):
            await api.async_get_account()
        wrong = _client(session, password="not-the-password")
        with pytest.raises(WatercareAuthError):
            await wrong.async_sign_in()
    statuses = [(r["operation"], r["status"]) for r in _journal(folder)]
    assert ("account", 503) in statuses
    assert statuses[-6:] == [
        ("account", 401),
        ("refresh", 400),
        ("authorize", 200),
        ("self_asserted", 200),
        ("authorize", 200),
        ("self_asserted", 200),
    ]
    assert [r.get("accepted") for r in _journal(folder)[-3:]] == [False, None, False]


async def test_a_request_the_fake_cannot_answer_is_journalled_as_an_error(
    standin: StandIn, folder: Path
) -> None:
    del standin
    async with aiohttp.ClientSession() as session:
        with pytest.raises(aiohttp.ClientConnectionError, match="could not answer"):
            await session.post(_ACCOUNT_URL)
        with pytest.raises(aiohttp.ClientConnectionError, match="could not answer"):
            await session.get(
                f"{client._B2C_BASE}/{client._POLICY}/api/CombinedSigninAndSignup/"
                "confirmed",
                params={"tx": "x", "p": client._POLICY},
                allow_redirects=False,
            )
    assert [(r["operation"], r["error"]) for r in _journal(folder)] == [
        ("-", "unexpected request POST"),
        ("confirmed", "a transaction the login page never gave out"),
    ]


def test_connections_and_lookups_beyond_this_computer_are_refused(
    socket_enabled: None, standin: StandIn, folder: Path
) -> None:
    # socket_enabled comes first: it must restore the real socket class before
    # the stand-in patches it, or the test meets pytest-socket's guard.
    del standin, socket_enabled
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as outbound:
        with pytest.raises(ConnectionRefusedError):
            outbound.connect(("192.0.2.1", 443))
        assert outbound.connect_ex(("192.0.2.1", 443)) == errno.ECONNREFUSED
    with pytest.raises(socket.gaierror):
        socket.getaddrinfo("api.example.invalid", 443)
    with pytest.raises(socket.gaierror):
        socket.gethostbyname("api.example.invalid")
    with pytest.raises(socket.gaierror):
        socket.gethostbyname_ex("api.example.invalid")
    assert _refused(folder) == [
        "connection 192.0.2.1",
        "connection 192.0.2.1",
        "lookup api.example.invalid",
        "lookup api.example.invalid",
        "lookup api.example.invalid",
    ]
    # This computer stays reachable, by address or by name.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind(("127.0.0.1", 0))
        server.listen()
        with socket.create_connection(server.getsockname(), timeout=5):
            pass
    assert socket.getaddrinfo("127.0.0.1", 80)
    assert socket.gethostbyname("localhost")
    assert len(_refused(folder)) == 5


def test_no_host_is_the_integrations_before_it_is_imported(
    folder: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delitem(__import__("sys").modules, watercare_standin.CLIENT_MODULE)
    standin = StandIn(folder)
    assert not standin.answers("customerapp.api.water.co.nz")
    monkeypatch.undo()
    assert standin.answers("customerapp.api.water.co.nz")
    assert standin.answers("WSLPWB2CPRD.b2clogin.com")
    assert not standin.answers("example.invalid")


def test_undo_restores_the_originals(folder: Path) -> None:
    before = (
        aiohttp.ClientSession._request,
        socket.socket.connect,
        socket.socket.connect_ex,
        socket.getaddrinfo,
    )
    undo = watercare_standin.patch(StandIn(folder))
    assert socket.getaddrinfo is not before[3]
    undo()
    assert (
        aiohttp.ClientSession._request,
        socket.socket.connect,
        socket.socket.connect_ex,
        socket.getaddrinfo,
    ) == before


def test_install_marks_the_process(
    folder: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    undone: list[bool] = []
    monkeypatch.setattr(
        watercare_standin, "patch", lambda _standin: lambda: undone.append(True)
    )
    installed = watercare_standin.install(folder / "new")
    assert installed.folder == folder / "new"
    assert (folder / "new" / f"installed-{os.getpid()}").is_file()


def test_install_reads_its_folder_from_the_settings_beside_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = tmp_path / watercare_standin.SETTINGS
    settings.write_text(json.dumps({"folder": str(tmp_path / "from-settings")}))
    monkeypatch.setattr(watercare_standin, "__file__", str(tmp_path / "__init__.py"))
    monkeypatch.setattr(watercare_standin, "patch", lambda _standin: lambda: None)
    assert watercare_standin.install().folder == tmp_path / "from-settings"


def test_the_interpreter_exits_when_the_stand_in_cannot_be_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken(folder: Path | None = None) -> StandIn:
        del folder
        raise FileNotFoundError("no settings")

    monkeypatch.setattr(watercare_standin, "install", broken)
    exits: list[int] = []
    watercare_standin.install_or_exit(exits.append)
    assert exits == [watercare_standin.EXIT_NOT_INSTALLED]
    assert watercare_standin.PTH_LINE.startswith("import watercare_standin;")
