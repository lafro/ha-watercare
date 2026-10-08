# Watercare for Home Assistant: agent notes

Public HACS integration (`custom_components/watercare`, domain `watercare`). Read `README.md`, `docs/statistics.md` and `docs/tariffs.md` before changing behaviour.

## Rules

- **Synthetic data only.** Never put a real Watercare account or meter number, email, name, address, token, balance or usage figure in code, tests, docs, commits, PRs or issues. Use the values in `tests/common.py`.
- **No Home Assistant access from this repository's sessions.** Do not call Home Assistant tools or deploy by hand (no SSH, Samba or copying into `custom_components`). Deploy = release, then a HACS download of that version, then a restart, done from the Home Assistant operations repository.
- **Releases** come only from the Release workflow, dispatched by the maintainer. Never create, move or delete tags or releases. Never release a CI-only or docs-only change. See `docs/release.md`.
- **Statistics continuity matters more than tidiness.** Statistic ids, entity unique ids and the config-entry migration path must keep existing installations working. Never rewrite stored statistics except through the documented one-off rebuild, and never run SQL against the recorder.
- **Prices** change every 1 July. Add new years to `tariffs.py` with a cited source in `docs/tariffs.md`.
- Do not open PRs or issues upstream (`brunsy/ha-watercare`) or post on forums without the maintainer's explicit approval.

## Working

- Set up with `uv sync --locked` (install uv first in a cloud session). Keep caches out of the repo.
- Checks: `uv run ruff format --check .`, `uv run ruff check .`, `uv run mypy custom_components/watercare`, `uv run pytest`, `uv run python scripts/check_module_coverage.py`.
- Branches `<type>/<slug>`, squash-merge PRs into `main`; all GitHub Actions pinned to full commit SHAs.
- Upstream guidance for HA integrations: `home-assistant/core/.claude/skills` (adapt paths to `custom_components/`).

## Facts that are easy to get wrong

- Watercare's mobile API only returns completed billing periods; dates are Auckland midnight in UTC; usage is in litres, in whole kilolitres for mechanical meters.
- Sign-in is Azure AD B2C self-asserted (email and password); there is no password-grant policy. A missing `SETTINGS` object on the login page means maintenance, not bad credentials.
- No API exposes prices. The Energy dashboard's water picker needs `unit_class="volume"`.
- The usage and cost sensors deliberately have no state class; the external statistics feed the Energy dashboard.
