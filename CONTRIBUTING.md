# Contributing

## Requirements

- Python 3.14.2 or newer
- [uv](https://docs.astral.sh/uv/)
- `lsof`, for the live test's checks (macOS and most Linux systems have it)

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

`start` builds a new Home Assistant (the newest release, or `--ha-version`) in a folder of its own in the system's temporary directory, completes onboarding, and checks before it reports ready that the instance listens on 127.0.0.1 only and loads nothing that discovers or announces devices (no `default_config`). Open the printed link, add the integration and sign in with your own Watercare email and password: the browser on the same computer is signed in to the instance without a password, as is any other program on it, so run `stop` as soon as you are done. The integration logs at debug level to the instance's log. `stop` removes the integration's entries, stops Home Assistant and deletes the folder with its database, log and stored password. Never commit anything from a live test.

## Pull requests

- Add tests for every behaviour change; every integration module needs at least 95% line and branch coverage.
- Preserve the config-entry migration, re-authentication and statistics continuity paths.
- Treat API responses as untrusted input.
- Never log request payloads, response bodies, headers or identifiers. Use `%`-style logging.
- Update the README, CHANGELOG and `docs/` for user-visible changes.
- Pull requests must pass Python, Hassfest, HACS, dependency review, CodeQL and the content checks: a secret scan, and a check against a private list of terms that must never be published (pull requests from forks and Dependabot skip it, because they get no repository secrets). `main` takes squash merges only.

## Releases

See [docs/release.md](docs/release.md).
