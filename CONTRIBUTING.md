# Contributing

## Requirements

- Python 3.14.2 or newer
- [uv](https://docs.astral.sh/uv/)
- A disposable Home Assistant instance for manual testing

Never use real credentials in automated tests and never commit captured Watercare responses. Fixtures, examples and docs must be synthetic: no real account or meter numbers, names, emails, addresses, tokens, balances, usage or costs, and no figures or dates copied from a real bill (made-up bills here fall on the 3rd and 4th of the month). `tests/test_repository_contracts.py` rejects anything shaped like a Watercare account number or meter id except the synthetic ones, and email addresses outside the example domains; it cannot recognise a real usage figure, so check those yourself.

## Setup and checks

```bash
uv sync --locked
uv run ruff format --check .
uv run ruff check .
uv run mypy custom_components/watercare
uv run pytest
uv run python scripts/check_module_coverage.py
gitleaks git --log-opts="origin/main..HEAD"   # before every push
gitleaks dir .
```

Keep tool caches out of the repository (uv uses its global cache by default). Hassfest and HACS validation run in GitHub Actions.

## Pull requests

- Add tests for every behaviour change; every integration module needs at least 95% line and branch coverage.
- Preserve the config-entry migration, re-authentication and statistics continuity paths.
- Treat API responses as untrusted input.
- Never log request payloads, response bodies, headers or identifiers. Use `%`-style logging.
- Update the README, CHANGELOG and `docs/` for user-visible changes.
- Pull requests must pass Python, Hassfest, HACS, dependency review and CodeQL. `main` takes squash merges only.

## Releases

See [docs/release.md](docs/release.md).
