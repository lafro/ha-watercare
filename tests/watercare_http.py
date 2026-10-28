"""A synthetic Watercare at the HTTP level, for tests of the real client.

It answers what the integration's client (``custom_components/watercare/
api.py``) sends, the way the services do: Watercare's Azure AD B2C sign-in
(the login page with its ``SETTINGS`` object, the email and password check,
the confirmation's redirect to the app with an authorisation code, and the
PKCE code exchange), the token refresh, and the customer-app API's account
and billing-period calls. ``FakeWatercare`` stands in for an aiohttp
session (``request``, ``get`` and ``post`` return a response to use with
``async with``), so the real client, coordinator and config flow run
unchanged; ``tests/watercare_standin.py`` puts it behind aiohttp in a
disposable Home Assistant for the journeys. It checks what it is sent: a
request the integration must never send is an ``AssertionError``.

It keeps no state that outlives a sign-in: an access or refresh token names
its synthetic account and the generation of sign-ins it was issued in, so a
restarted Home Assistant resumes with its stored refresh token. Raising
``generation`` ends every earlier sign-in, as a password change does.

The bills are a function of the date alone (``bills``): the meter is read
once a month, from 3 June 2024, and a bill is issued a few days after its
reading. Neighbouring bills differ in length and volume, and one spans
1 July, so a day shifted, counted twice or priced in the wrong year shows in
the statistics. Every value is synthetic, the payload builders the tests
share (``account_payload``, ``api_period``) included.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Final, Self

from multidict import CIMultiDict
from yarl import URL

from custom_components.watercare import api as client
from custom_components.watercare.const import NZ_TIMEZONE

ACCOUNT_NUMBER: Final = "1000001-01"
SECOND_ACCOUNT_NUMBER: Final = "2000002-02"
METER_NUMBER: Final = "TEST-METER-0001"
SECOND_METER_NUMBER: Final = "TEST-METER-0002"
EMAIL: Final = "someone@example.com"
PASSWORD: Final = "synthetic-password"

# The first meter reading; the first bill starts the day after it.
FIRST_READING: Final = date(2024, 6, 3)
# A bill is issued, and the API returns it, this many days after its reading.
ISSUE_DAYS: Final = 5
ACCESS_LIFETIME_S: Final = 3600
_ONE_DAY: Final = timedelta(days=1)
_ACCOUNTS: Final = {
    ACCOUNT_NUMBER: METER_NUMBER,
    SECOND_ACCOUNT_NUMBER: SECOND_METER_NUMBER,
}
_HTTP_OK: Final = 200
_HTTP_FOUND: Final = 302
_HTTP_BAD_REQUEST: Final = 400
_HTTP_UNAUTHORIZED: Final = 401
_MAINTENANCE_PAGE: Final = (
    b"<html><head><title>Service unavailable</title></head>"
    b"<body><h1>Synthetic maintenance page</h1></body></html>"
)
_REJECTED_CREDENTIALS: Final = {
    "status": "400",
    "message": "The username or password provided in the request are invalid.",
}
_EXPIRED_GRANT: Final = {
    "error": "invalid_grant",
    "error_description": "The provided grant has expired.",
}


# ------------------------------------------------------------------ payloads


def account_payload(
    number: str = ACCOUNT_NUMBER,
    meter: str = METER_NUMBER,
    *,
    user_account_id: int = 1,
) -> list[dict[str, Any]]:
    """Return a v1/account response: a list holding one synthetic account."""
    return [
        {
            "userAccountId": user_account_id,
            "accountNumber": number,
            "accountName": "Test Account",
            "amountDue": 123.45,
            "accountBalance": 123.45,
            "overdueAmount": 0,
            "dueDate": "2026-09-25T23:59:59Z",
            "hasDueDate": True,
            "meterType": "mechanical",
            "meters": [{"id": meter}],
        }
    ]


def api_date(day: date) -> str:
    """Return a date the way Watercare sends it: Auckland midnight in UTC."""
    instant = datetime.combine(day, time.min, tzinfo=NZ_TIMEZONE).astimezone(UTC)
    return instant.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def api_period(
    start: date,
    end: date,
    usage: int,
    *,
    days: int | None = None,
    reading: str = "A",
) -> dict[str, Any]:
    """Return one mechanicalmonthly item."""
    number_of_days = days if days is not None else (end - start).days + 1
    return {
        "billingPeriodFromDate": api_date(start),
        "billingPeriodToDate": api_date(end),
        "waterUsage": usage,
        "readingType": reading,
        "statistics": {
            "dailyAverage": round(usage / number_of_days),
            "numberOfDays": number_of_days,
            "efficiency": {"currentHouseholdBand": 2, "usageToLowerBand": 25},
        },
    }


# --------------------------------------------------------------------- bills


@dataclass(frozen=True, slots=True)
class Bill:
    """One synthetic bill: both ends included, whole kilolitres."""

    start: date
    end: date
    litres: int
    reading: str

    @property
    def days(self) -> int:
        """Return the days the bill covers, both ends included."""
        return (self.end - self.start).days + 1

    def payload(self) -> dict[str, Any]:
        """Return the bill as a mechanicalmonthly item."""
        return api_period(self.start, self.end, self.litres, reading=self.reading)


def reading_day(year: int, month: int) -> date:
    """Return the day the meter is read in a month: the 2nd to the 5th."""
    return date(year, month, 2 + (year * 12 + month - 1) % 4)


def bills(today: date) -> list[Bill]:
    """Return every bill issued by ``today`` (in Auckland), oldest first.

    A function of the date alone. Bill ``n`` runs from the day after one
    reading to the next reading; it holds 6 to 14 kL, never the same as the
    bill before, and every fourth reading is an estimate.
    """
    issued: list[Bill] = []
    year, month = FIRST_READING.year, FIRST_READING.month
    previous = reading_day(year, month)
    while True:
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
        reading = reading_day(year, month)
        if reading + timedelta(days=ISSUE_DAYS) > today:
            return issued
        kilolitres = 6 + (year * 12 + month) * 5 % 9
        estimate = len(issued) % 4 == 3
        issued.append(
            Bill(
                previous + _ONE_DAY,
                reading,
                kilolitres * 1000,
                "E" if estimate else "A",
            )
        )
        previous = reading


def auckland_today() -> date:
    """Return today's date in Auckland."""
    return datetime.now(NZ_TIMEZONE).date()


# ------------------------------------------------------------------ response


class Response:
    """One response, read the way the client reads aiohttp's."""

    def __init__(
        self,
        body: bytes,
        *,
        status: int = _HTTP_OK,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status = status
        self.headers: CIMultiDict[str] = CIMultiDict(
            {"Content-Type": "application/json"} if headers is None else headers
        )
        self._body = body

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    def release(self) -> None:
        """Release the connection: there is none."""

    async def read(self) -> bytes:
        """Return the body."""
        return self._body

    async def text(self, encoding: str | None = None) -> str:
        """Return the body as text."""
        return self._body.decode(encoding or "utf-8")

    async def json(self, *, content_type: str | None = "application/json") -> Any:
        """Return the body decoded as JSON; a body that is not raises ValueError."""
        del content_type  # The client always passes None: no type check.
        return json.loads(self._body)


def json_response(payload: object, *, status: int = _HTTP_OK) -> Response:
    """Return a JSON response."""
    return Response(json.dumps(payload).encode(), status=status)


def html_response(page: bytes, *, status: int = _HTTP_OK) -> Response:
    """Return an HTML page."""
    return Response(page, status=status, headers={"Content-Type": "text/html"})


# ---------------------------------------------------------------- operations


def _b2c_prefix() -> str:
    return URL(f"{client._B2C_BASE}/{client._POLICY}/").path.lower()


def operation(method: str, url: str) -> str | None:
    """Return the name of the request the client sends to ``url``, or None.

    ``authorize``, ``self_asserted``, ``confirmed`` and ``token`` are the
    sign-in's four steps, ``refresh`` the token refresh, and ``account`` and
    ``billing_periods`` the API's calls.
    """
    target = URL(url)
    path = target.path.lower()
    method = method.upper()
    if target.host == URL(client._B2C_BASE).host:
        if not path.startswith(_b2c_prefix()):
            return None
        steps = {
            ("GET", "oauth2/v2.0/authorize"): "authorize",
            ("POST", "selfasserted"): "self_asserted",
            ("GET", "api/combinedsigninandsignup/confirmed"): "confirmed",
            ("GET", "oauth2/v2.0/token"): "token",
            ("POST", "oauth2/v2.0/token"): "refresh",
        }
        return steps.get((method, path.removeprefix(_b2c_prefix())))
    if target.host == URL(client._API_BASE).host and method == "GET":
        parts = target.path.strip("/").split("/")
        if parts == ["v1", "account"]:
            return "account"
        if (
            len(parts) == 4
            and parts[:2] == ["v1", "usage"]
            and parts[3] == ("mechanicalmonthly")
        ):
            return "billing_periods"
    return None


# --------------------------------------------------------------------- fake


@dataclass(slots=True)
class _Transaction:
    """One sign-in in progress: what the login page gave out and who signed in."""

    challenge: str
    csrf: str
    account: str | None = None


@dataclass(slots=True)
class _Grant:
    """An authorisation code not yet exchanged."""

    challenge: str
    account: str


@dataclass(frozen=True, slots=True)
class Token:
    """What a synthetic token stands for."""

    kind: str
    account: str
    generation: int


def _encode(data: dict[str, Any]) -> str:
    raw = json.dumps(data, sort_keys=True).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def read_token(value: str, kind: str) -> Token | None:
    """Return what a synthetic token of ``kind`` stands for, or None."""
    prefix = f"synthetic-{kind}."
    if not value.startswith(prefix):
        return None
    encoded = value.removeprefix(prefix)
    try:
        data = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
        return Token(kind, str(data["account"]), int(data["generation"]))
    except binascii.Error, ValueError, KeyError, TypeError:
        return None


def challenge_of(verifier: str) -> str:
    """Return the S256 code challenge of a PKCE code verifier."""
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


@dataclass(slots=True)
class FakeWatercare:
    """Answer the client's requests as Watercare and its B2C sign-in do.

    ``logins`` maps each email Watercare knows to its account, all with the
    one ``password``. Tokens of an earlier ``generation`` are refused: the
    API answers 401 and the refresh 400. ``api_status``, when set, is the
    status every API request gets (a maintenance page), as when Watercare is
    down; the sign-in still works. The newest ``withheld`` bills are not
    issued yet. ``as_of`` is the date the bills are issued up to (today in
    Auckland if None). All of them may change between requests.

    ``log`` records each answered request: its operation, status, and the
    synthetic account or whether the email and password were accepted.
    Never a credential or a token.
    """

    logins: dict[str, str] = field(default_factory=lambda: {EMAIL: ACCOUNT_NUMBER})
    password: str = PASSWORD
    generation: int = 1
    api_status: int | None = None
    withheld: int = 0
    as_of: date | None = None
    log: list[dict[str, Any]] = field(default_factory=list)
    _transactions: dict[str, _Transaction] = field(default_factory=dict)
    _grants: dict[str, _Grant] = field(default_factory=dict)
    _serial: int = 0

    # --------------------------------------------------------------- session

    def get(self, url: str, **options: Any) -> Response:
        """Answer a GET, as ``aiohttp.ClientSession.get`` would send it."""
        return self.request("GET", url, **options)

    def post(self, url: str, **options: Any) -> Response:
        """Answer a POST, as ``aiohttp.ClientSession.post`` would send it."""
        return self.request("POST", url, **options)

    def request(self, method: str, url: str, **options: Any) -> Response:
        """Answer one request; raise AssertionError for one never to be sent."""
        name = operation(method, url)
        if name is None:
            raise AssertionError(f"unexpected request {method.upper()}")
        query = URL(url).extend_query(options.get("params") or {}).query
        headers = CIMultiDict(options.get("headers") or {})
        form = dict(options.get("data") or {})
        note: dict[str, Any] = {}
        if name in {"account", "billing_periods"}:
            response = self._api(name, url, headers, note)
        elif name == "authorize":
            response = self._authorize(query)
        elif name == "self_asserted":
            response = self._self_asserted(query, headers, form, note)
        elif name == "confirmed":
            response = self._confirmed(query, options)
        elif name == "token":
            response = self._exchange(query, note)
        else:
            response = self._refresh(form, note)
        self.log.append({"operation": name, "status": response.status, **note})
        return response

    # ---------------------------------------------------------------- bills

    def bills(self) -> list[Bill]:
        """Return the bills issued now, oldest first."""
        issued = bills(self.as_of or auckland_today())
        return issued[: len(issued) - self.withheld] if self.withheld else issued

    # --------------------------------------------------------------- sign-in

    def _next(self) -> int:
        self._serial += 1
        return self._serial

    def _authorize(self, query: Any) -> Response:
        if query.get("client_id") != client._CLIENT_ID:
            raise AssertionError("the login page was asked for another client")
        if (query.get("response_type"), query.get("code_challenge_method")) != (
            "code",
            "S256",
        ):
            raise AssertionError("the login page was asked for no PKCE code")
        if "offline_access" not in str(query.get("scope", "")).split():
            raise AssertionError("no offline_access scope: no refresh token")
        challenge = str(query.get("code_challenge") or "")
        if not challenge:
            raise AssertionError("no code challenge")
        serial = self._next()
        transaction = "StateProperties=" + _encode({"TID": f"synthetic-{serial}"})
        csrf = f"synthetic-csrf-{serial}"
        self._transactions[transaction] = _Transaction(challenge, csrf)
        settings = {"transId": transaction, "csrf": csrf, "api": "SelfAsserted"}
        page = (
            "<!DOCTYPE html><html><head><title>Sign in</title></head><body>\n"
            "<script>\n"
            f"var SETTINGS = {json.dumps(settings)};\n"
            "</script>\n</body></html>\n"
        )
        return html_response(page.encode())

    def _transaction(self, query: Any) -> _Transaction:
        if query.get("p") != client._POLICY:
            raise AssertionError("another policy")
        transaction = self._transactions.get(str(query.get("tx")))
        if transaction is None:
            raise AssertionError("a transaction the login page never gave out")
        return transaction

    def _self_asserted(
        self, query: Any, headers: Any, form: dict[str, Any], note: dict[str, Any]
    ) -> Response:
        transaction = self._transaction(query)
        if headers.get("X-CSRF-TOKEN") != transaction.csrf:
            raise AssertionError("the sign-in's CSRF token is not the login page's")
        if form.get("request_type") != "RESPONSE":
            raise AssertionError("the sign-in form has no request_type")
        account = self.logins.get(str(form.get("email")))
        accepted = account is not None and form.get("password") == self.password
        note["accepted"] = accepted
        if not accepted:
            # B2C answers 200 and puts its own status in the body.
            return json_response(_REJECTED_CREDENTIALS)
        transaction.account = account
        return json_response({"status": "200"})

    def _confirmed(self, query: Any, options: dict[str, Any]) -> Response:
        if options.get("allow_redirects") is not False:
            raise AssertionError("the redirect to the app must not be followed")
        transaction = self._transaction(query)
        if query.get("csrf_token") != transaction.csrf:
            raise AssertionError("the confirmation's CSRF token is not the page's")
        if transaction.account is None:
            raise AssertionError(
                "confirmed before the email and password were accepted"
            )
        del self._transactions[str(query.get("tx"))]
        code = f"synthetic-code-{self._next()}"
        self._grants[code] = _Grant(transaction.challenge, transaction.account)
        location = f"{client._REDIRECT_URI}?code={code}"
        return Response(b"", status=_HTTP_FOUND, headers={"Location": location})

    def _exchange(self, query: Any, note: dict[str, Any]) -> Response:
        if query.get("grant_type") != "authorization_code":
            raise AssertionError("a code exchange without its grant type")
        if query.get("client_id") != client._CLIENT_ID:
            raise AssertionError("a code exchange for another client")
        grant = self._grants.pop(str(query.get("code")), None)
        if grant is None:
            raise AssertionError("a code that was never issued, or used twice")
        if challenge_of(str(query.get("code_verifier"))) != grant.challenge:
            raise AssertionError("the code verifier does not match the challenge")
        note["account"] = grant.account
        return json_response(self._tokens(grant.account))

    def _refresh(self, form: dict[str, Any], note: dict[str, Any]) -> Response:
        if form.get("grant_type") != "refresh_token":
            raise AssertionError("a token request that is neither code nor refresh")
        if form.get("client_id") != client._CLIENT_ID:
            raise AssertionError("a refresh for another client")
        token = read_token(str(form.get("refresh_token")), "refresh")
        if token is None or token.generation != self.generation:
            return json_response(_EXPIRED_GRANT, status=_HTTP_BAD_REQUEST)
        note["account"] = token.account
        return json_response(self._tokens(token.account))

    def _token(self, kind: str, account: str) -> str:
        data = {"account": account, "generation": self.generation, "n": self._next()}
        return f"synthetic-{kind}.{_encode(data)}"

    def _tokens(self, account: str) -> dict[str, Any]:
        return {
            "access_token": self._token("access", account),
            "token_type": "Bearer",
            "expires_in": ACCESS_LIFETIME_S,
            "refresh_token": self._token("refresh", account),
            "refresh_token_expires_in": 1209600,
        }

    # ------------------------------------------------------------------- API

    def _api(self, name: str, url: str, headers: Any, note: dict[str, Any]) -> Response:
        if self.api_status is not None:
            return html_response(_MAINTENANCE_PAGE, status=self.api_status)
        bearer = str(headers.get("authorization", ""))
        token = read_token(bearer.removeprefix("Bearer "), "access")
        if (
            not bearer.startswith("Bearer ")
            or token is None
            or token.generation != self.generation
        ):
            return json_response({"message": "Unauthorized"}, status=_HTTP_UNAUTHORIZED)
        note["account"] = token.account
        if token.account not in _ACCOUNTS:
            raise AssertionError("a token for an account Watercare does not have")
        if name == "account":
            number = token.account
            meter = _ACCOUNTS[number]
            position = list(_ACCOUNTS).index(number) + 1
            return json_response(
                account_payload(number, meter, user_account_id=position)
            )
        asked = URL(url).path.strip("/").split("/")[2]
        if asked != token.account:
            raise AssertionError("bills of an account the token does not hold")
        return json_response([bill.payload() for bill in reversed(self.bills())])
