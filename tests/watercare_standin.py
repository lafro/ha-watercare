"""A stand-in Watercare inside a disposable Home Assistant, for the journeys.

Test-only, never shipped. ``scripts/journeys.py`` copies this module into the
instance's site-packages as the package ``watercare_standin``, with the
tests' fake (``watercare_http.py``) beside it, and a ``.pth`` file that calls
``install_or_exit`` when the interpreter starts: before Home Assistant or the
integration imports anything, after a restart too. If the stand-in cannot be
installed, the interpreter exits at once, so Home Assistant never runs
without it.

``install`` patches the process's ways out:

- ``aiohttp.ClientSession._request``, which every aiohttp request goes
  through, whatever its method. A request to one of the integration's hosts
  (Watercare's API and its B2C sign-in, from the URLs in its ``api.py``) is
  answered by ``watercare_http.FakeWatercare``, so the integration's own
  client code runs unchanged; a request to this computer (127.0.0.1, ::1 or
  localhost) goes ahead; any other is refused.
- TCP connections (``socket.socket.connect`` and ``connect_ex``) to any
  address but this computer's, and name lookups (``socket.getaddrinfo``,
  ``gethostbyname`` and ``gethostbyname_ex``) of any name but this
  computer's, so a library that does not use aiohttp cannot reach past
  this computer either.

A refused request raises a connection error, and appends one line to
``refused.log`` in the stand-in's folder naming its kind and host only,
never its path or query. Every request the fake answers appends one JSON
line to ``journal.jsonl``: when, the operation, the status, and the
synthetic account or whether the email and password were accepted, or the
fake's error when it could not answer (a request the integration must never
send). Never a credential or a token. ``installed-<pid>`` marks each process
the stand-in is installed in.

The scenario is read from ``scenario.json`` in the folder on each request,
so the runner can change it between steps without any API (``Scenario``):
the logins Watercare knows and their password, the generation of sign-ins
it honours (a new one ends every earlier sign-in, as a password change
does), Watercare's API down with a status, how many of the newest bills are
not issued yet, and the date the bills are issued up to.

The bills are a function of the date alone (``watercare_http.bills``), so a
restarted Home Assistant sees the same history.
"""

from __future__ import annotations

import asyncio
import errno
import ipaddress
import json
import logging
import os
import socket
import sys
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Self
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from .watercare_http import FakeWatercare, Response

_LOGGER = logging.getLogger(__name__)

PACKAGE = "watercare_standin"
# Beside the installed package: {"folder": "<the stand-in's folder>"}.
SETTINGS = "settings.json"
SCENARIO = "scenario.json"
JOURNAL = "journal.jsonl"
REFUSED = "refused.log"
INSTALLED = "installed-{pid}"
# The ``.pth`` line: a failure exits before Home Assistant can start.
PTH_LINE = f"import {PACKAGE}; {PACKAGE}.install_or_exit()\n"
# The exit status of an interpreter the stand-in could not be installed in.
EXIT_NOT_INSTALLED = 70
# The integration's client module: its URLs name the hosts the fake answers.
CLIENT_MODULE = "custom_components.watercare.api"
# watercare_http's synthetic login, which a scenario has unless it says
# otherwise (tests/test_watercare_standin.py checks that they agree).
DEFAULT_EMAIL = "someone@example.com"
DEFAULT_ACCOUNT = "1000001-01"
DEFAULT_PASSWORD = "synthetic-password"
_LOCAL_NAMES = frozenset({"localhost", "localhost.", "ip6-localhost"})


@dataclass(frozen=True, slots=True)
class Scenario:
    """How the stand-in answers, read from ``scenario.json`` on each request.

    ``logins`` maps each email Watercare knows to its account, all with
    ``password``. Watercare honours only tokens of this ``generation`` of
    sign-ins: a new one ends every earlier sign-in, so the API refuses
    their access tokens (401) and the refresh their refresh tokens (400).
    ``api_status``, when set, is the status every API request gets, as when
    Watercare is down. The newest ``withheld`` bills are not issued yet.
    ``as_of`` is the date the bills are issued up to (today in Auckland if
    None), so a run that crosses midnight sees the same bills.
    """

    logins: Mapping[str, str] = field(
        default_factory=lambda: {DEFAULT_EMAIL: DEFAULT_ACCOUNT}
    )
    password: str = DEFAULT_PASSWORD
    generation: int = 1
    api_status: int | None = None
    withheld: int = 0
    as_of: date | None = None

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> Self:
        """Return the scenario a JSON object describes; missing keys keep defaults."""
        default = cls()
        logins = data.get("logins", default.logins)
        if not isinstance(logins, dict):
            raise TypeError("logins maps each email to its account")
        status = data.get("api_status")
        as_of = data.get("as_of")
        return cls(
            logins={str(email): str(account) for email, account in logins.items()},
            password=str(data.get("password", default.password)),
            generation=int(data.get("generation", default.generation)),
            api_status=None if status is None else int(status),
            withheld=int(data.get("withheld", default.withheld)),
            as_of=None if as_of is None else date.fromisoformat(str(as_of)),
        )

    def to_json(self) -> dict[str, Any]:
        """Return the scenario as a JSON object."""
        return {
            "logins": dict(self.logins),
            "password": self.password,
            "generation": self.generation,
            "api_status": self.api_status,
            "withheld": self.withheld,
            "as_of": None if self.as_of is None else self.as_of.isoformat(),
        }


def is_local(host: str | bytes | None) -> bool:
    """Return whether a host names this computer: loopback, or localhost."""
    if host is None:
        return False
    name = (host.decode() if isinstance(host, bytes) else host).strip("[]").lower()
    if name in _LOCAL_NAMES:
        return True
    try:
        return ipaddress.ip_address(name.split("%", 1)[0]).is_loopback
    except ValueError:
        return False


def needs_lookup(host: str | bytes | None) -> bool:
    """Return whether resolving a host would ask a name server."""
    if host is None or is_local(host):
        return False
    name = (host.decode() if isinstance(host, bytes) else host).strip("[]")
    try:
        ipaddress.ip_address(name.split("%", 1)[0])
    except ValueError:
        return name != ""
    return False


def _append(path: Path, line: str) -> None:
    """Append one line with a single write, as several processes may append."""
    descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(descriptor, line.encode())
    finally:
        os.close(descriptor)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


@dataclass(slots=True)
class StandIn:
    """Answer the integration's requests from the fake; refuse everything else."""

    folder: Path
    fake: FakeWatercare | None = None
    _hosts: frozenset[str] = field(default_factory=frozenset)

    def scenario(self) -> Scenario:
        """Return the scenario in the folder, or the default one."""
        path = self.folder / SCENARIO
        if not path.is_file():
            return Scenario()
        return Scenario.from_json(json.loads(path.read_text()))

    def refuse(self, kind: str, host: str | bytes | None) -> None:
        """Record a refused request on one line, with its host only."""
        name = host.decode(errors="replace") if isinstance(host, bytes) else host
        at = datetime.now(UTC).isoformat(timespec="seconds")
        _append(self.folder / REFUSED, f"{at} {kind} {name}\n")
        _LOGGER.warning("The journeys stand-in refused %s to %s", kind, name)

    def journal(self, record: dict[str, Any]) -> None:
        """Record one answered request."""
        _append(self.folder / JOURNAL, json.dumps(record, sort_keys=True) + "\n")

    def mark_installed(self) -> None:
        """Mark this process as one the stand-in is installed in."""
        self.folder.mkdir(parents=True, exist_ok=True)
        (self.folder / INSTALLED.format(pid=os.getpid())).write_text("")

    def answers(self, host: str) -> bool:
        """Return whether the fake answers requests to this host."""
        if not self._hosts:
            client = sys.modules.get(CLIENT_MODULE)
            if client is None:
                # The integration is not imported yet, so no request can be
                # its own: nothing goes out meanwhile. Nothing is imported
                # here, so the event loop never waits for an import.
                return False
            self._hosts = frozenset(
                str(urlsplit(url).hostname)
                for url in (client._API_BASE, client._B2C_BASE)
            )
        return host.lower() in self._hosts

    def _load(self) -> tuple[Scenario, Any]:
        """Read the scenario and import the fake; runs in a worker thread."""
        from . import watercare_http  # noqa: PLC0415 - needs the integration

        return self.scenario(), watercare_http

    async def answer(self, method: str, url: str, options: dict[str, Any]) -> Response:
        """Answer one request to the integration's hosts from the fake."""
        scenario, http = await asyncio.to_thread(self._load)
        fake = self._prepare(scenario, http)
        record: dict[str, Any] = {"at": _now()}
        try:
            response: Response = fake.request(method, url, **options)
        except AssertionError as err:
            record["operation"] = http.operation(method, url) or "-"
            record["error"] = str(err)
            await asyncio.to_thread(self.journal, record)
            raise _refusal("the fake could not answer this request") from None
        record.update(fake.log[-1])
        await asyncio.to_thread(self.journal, record)
        return response

    def _prepare(self, scenario: Scenario, http: Any) -> FakeWatercare:
        """Return the fake, set to the scenario.

        One fake serves the process, so a sign-in's steps meet the same
        transaction; tokens outlive it.
        """
        if self.fake is None:
            self.fake = http.FakeWatercare()
        fake = self.fake
        assert fake is not None
        fake.logins = dict(scenario.logins)
        fake.password = scenario.password
        fake.generation = scenario.generation
        fake.api_status = scenario.api_status
        fake.withheld = scenario.withheld
        fake.as_of = scenario.as_of
        return fake


def _refusal(reason: str) -> Exception:
    """Return the connection error a refused aiohttp request raises."""
    import aiohttp  # noqa: PLC0415

    return aiohttp.ClientConnectionError(f"Refused by the journeys stand-in: {reason}")


def _request_patch(standin: StandIn, original: Any) -> Any:
    """Return ``ClientSession._request`` as the stand-in has it."""

    async def request(
        session: Any, method: str, str_or_url: Any, **options: Any
    ) -> Any:
        url = str(str_or_url)
        host = urlsplit(url).hostname or ""
        if is_local(host):
            return await original(session, method, str_or_url, **options)
        if standin.answers(host):
            return await standin.answer(method, url, options)
        standin.refuse("request", host)
        raise _refusal(host)

    return request


def _connect_patches(
    standin: StandIn, connect: Any, connect_ex: Any
) -> tuple[Any, Any]:
    """Return ``socket.connect`` and ``connect_ex`` as the stand-in has them."""

    def guard(sock: socket.socket, address: Any) -> None:
        if sock.family not in {socket.AF_INET, socket.AF_INET6}:
            return
        if sock.type != socket.SOCK_STREAM:
            return
        host = address[0] if isinstance(address, tuple) else address
        if not is_local(host):
            standin.refuse("connection", host)
            raise ConnectionRefusedError(
                errno.ECONNREFUSED, "Refused by the journeys stand-in"
            )

    def guarded_connect(sock: socket.socket, address: Any) -> None:
        guard(sock, address)
        connect(sock, address)

    def guarded_connect_ex(sock: socket.socket, address: Any) -> int:
        try:
            guard(sock, address)
        except ConnectionRefusedError:
            return errno.ECONNREFUSED
        result: int = connect_ex(sock, address)
        return result

    return guarded_connect, guarded_connect_ex


def _lookup_patch(standin: StandIn, original: Any) -> Any:
    """Return a name lookup that refuses any name but this computer's."""

    def lookup(host: Any, *args: Any, **kwargs: Any) -> Any:
        if needs_lookup(host):
            standin.refuse("lookup", host)
            raise socket.gaierror(socket.EAI_NONAME, "Refused by the journeys stand-in")
        return original(host, *args, **kwargs)

    return lookup


def patch(standin: StandIn) -> Callable[[], None]:
    """Patch aiohttp and the socket module; return a function that undoes it."""
    import aiohttp  # noqa: PLC0415

    session = aiohttp.ClientSession
    connect, connect_ex = _connect_patches(
        standin, socket.socket.connect, socket.socket.connect_ex
    )
    replacements: list[tuple[Any, str, Any, Any]] = [
        (
            session,
            "_request",
            session._request,
            _request_patch(standin, session._request),
        ),
        (socket.socket, "connect", socket.socket.connect, connect),
        (socket.socket, "connect_ex", socket.socket.connect_ex, connect_ex),
    ]
    replacements += [
        (
            socket,
            name,
            getattr(socket, name),
            _lookup_patch(standin, getattr(socket, name)),
        )
        for name in ("getaddrinfo", "gethostbyname", "gethostbyname_ex")
    ]
    for owner, name, _original, replacement in replacements:
        setattr(owner, name, replacement)

    def undo() -> None:
        for owner, name, original, _replacement in replacements:
            setattr(owner, name, original)

    return undo


def install(folder: Path | None = None) -> StandIn:
    """Install the stand-in in this process, with the folder from its settings."""
    if folder is None:
        settings = json.loads(Path(__file__).with_name(SETTINGS).read_text())
        folder = Path(settings["folder"])
    standin = StandIn(folder)
    patch(standin)
    standin.mark_installed()
    return standin


def install_or_exit(exit_now: Callable[[int], object] = os._exit) -> None:
    """Install the stand-in, or end the interpreter before anything else runs."""
    try:
        install()
    except BaseException:
        _LOGGER.exception("The journeys stand-in could not be installed")
        exit_now(EXIT_NOT_INSTALLED)
