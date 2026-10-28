# Contributing

## Requirements

- Python 3.14.2 or newer
- [uv](https://docs.astral.sh/uv/)
- `lsof`, for the live test's and the journeys' checks (macOS and most Linux systems have it)

Never use real credentials in automated tests and never commit captured Watercare responses. Fixtures, examples and docs must be synthetic: no real account or meter numbers, names, emails, addresses, tokens, balances, usage or costs, and no figures or dates copied from a real bill. `tests/test_repository_contracts.py` rejects anything shaped like a Watercare account number or meter id except the synthetic ones, and email addresses outside the example domains; it cannot recognise a real usage figure, so check those yourself.

## Setup and checks

```bash
uv sync --locked
uv run ruff format --check .
uv run ruff check .
uv run mypy custom_components/watercare
uv run pytest
uv run python scripts/check_module_coverage.py
```

Keep tool caches out of the repository (uv uses its global cache by default). Hassfest and HACS validation run in GitHub Actions.

## Live test

The tests use a fake Watercare. To try the integration against the real service, run a disposable Home Assistant:

```bash
uv run python scripts/live_ha.py start                 # the working tree
uv run python scripts/live_ha.py start --ref v1.5.1    # or a release tag or commit
uv run python scripts/live_ha.py stop                  # remove it all
```

`start` builds a new Home Assistant (the newest release, or `--ha-version`) in a folder of its own in the system's temporary directory, completes onboarding, and checks before it reports ready that the instance listens on 127.0.0.1 only and loads nothing that discovers or announces devices (no `default_config`). Open the printed link, add the integration and sign in with your own Watercare email and password: the browser on the same computer is signed in to the instance without a password, as is any other program on it (and, in principle, a web page open in that browser), so run `stop` as soon as you are done. The integration logs at debug level to the instance's log. `stop` removes the integration's entries, stops Home Assistant and deletes the folder with its database, log and stored password. Never commit anything from a live test.

## Journeys

The journeys run the integration end to end in a real Home Assistant against a stand-in Watercare, with no real sign-in:

```bash
uv run python scripts/journeys.py                       # the working tree, all seven
uv run python scripts/journeys.py --ref v1.5.1          # or a release tag or commit
uv run python scripts/journeys.py --only reauth options # some of them
uv run python scripts/journeys.py --clean-up            # delete a left-over instance
```

It builds a disposable Home Assistant as the live test does (the newest release, or `--ha-version`), but in a folder of its own (`journeys-watercare`) on port 8126, so the two never meet. A run does not start while that folder exists, as another run may be using it; `--clean-up` deletes one a killed run left behind. The stand-in (`tests/watercare_standin.py`, with the fake Watercare the tests use, `tests/watercare_http.py`) goes into that instance's Python through a `.pth` file. It answers the integration's requests at the HTTP level, the Azure AD B2C sign-in and the token refresh included, so the integration's own client runs unchanged. Its answers follow the scenario the journey sets, with synthetic bills that depend only on the date. In Home Assistant's process it refuses and records every other aiohttp request, TCP connection and name lookup that would leave the computer; Home Assistant does not start without it. It does not cover UDP, reverse lookups, resolvers written in C, or other programs: building the instance, and Home Assistant's first start, download Home Assistant and the requirements it pins from PyPI (through uv), as for the live test.

| Journey | What it does |
| --- | --- |
| `first_setup` | Email and password, entry; the setup signs in once, and the entry's first update uses the refresh token; every bill imported as daily rows |
| `wrong_password` | Watercare rejects the password (`invalid_auth`) and the form keeps the email; the right one creates the entry |
| `watercare_down` | Watercare answers 503 (`cannot_connect`); the same form works once it is back |
| `reauth` | The password changes at Watercare, ending every sign-in; the next update asks to sign in again; the new password, and the reloaded entry uses its refresh token; history unbroken |
| `reconfigure` | Another account's login is refused (`wrong_account`); a new email for the same account is saved; history unbroken |
| `options` | A new wastewater ratio and this financial year's prices (the `new_year` step when no release has them); stored days keep their prices, later days take the new ones |
| `restart` | Reload the entry, then restart Home Assistant; each resumes with the refresh token and adds only the new bill's days; history unbroken |

Every journey starts with no Watercare statistics. It also checks that its entry is loaded and its sensors exist, none unavailable, and show the newest bill; that each of the four statistics has one row per Auckland day from the first bill to the newest, with no day missing or twice, each day holding the value [statistics.md](docs/statistics.md) gives it, each bill's days adding up to the bill and each sum step equal to its day; that the repair notices are those the prices call for; that every screen it met has its words (no raw keys); and that the stand-in refused nothing; the run checks that once more after Home Assistant has stopped. The run prints PASS or FAIL for each check and journey, writes a report with every check and every screen as the dialog shows it to the system's temporary directory, deletes the instance whatever happens, and exits non-zero if anything failed. The first run downloads Home Assistant; the restart journey is the slowest, as it restarts Home Assistant.

Run the journeys before every release, and in every pull request that changes sign-in, setup, the options or the statistics; say in the pull request that they passed. They are not part of CI.

## Pull requests

- Add tests for every behaviour change; every integration module needs at least 95% line and branch coverage.
- Preserve the config-entry migration, re-authentication and statistics continuity paths.
- Treat API responses as untrusted input.
- Never log request payloads, response bodies, headers or identifiers. Use `%`-style logging.
- Update the README, CHANGELOG and `docs/` for user-visible changes.
- Pull requests must pass Python, Hassfest, HACS, dependency review, CodeQL and the content checks: a secret scan, and a check against a private list of terms that must never be published (pull requests from forks and Dependabot skip it, because they get no repository secrets). `main` takes squash merges only.

## Releases

See [docs/release.md](docs/release.md).
