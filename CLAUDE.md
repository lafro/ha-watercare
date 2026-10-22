# CLAUDE.md: ha-watercare

Public HACS integration (`custom_components/watercare`, domain `watercare`). Read `README.md`, `docs/statistics.md` and `docs/tariffs.md` before changing behaviour. This repository is public: everything committed here, and every PR or issue text, is world-readable.

## Rules

- **Synthetic data only.** Never put a real Watercare account or meter number, email, name, address, token, balance, usage or cost figure, or the dates of a real bill, in code, tests, docs, commits, PRs or issues. Use the values in `tests/common.py`.
- **No secrets.** CI runs a secret scan on every pull request; you can run the same locally with `gitleaks git -c .gitleaks.toml .` and `gitleaks dir -c .gitleaks.toml .`. The contract tests also reject account-number and meter-id shapes and non-example emails, but they cannot recognise a real usage figure.
- **Pull requests only.** Never push to `main`, force-push, or create, move or delete tags or releases. Tags and releases are immutable by ruleset and come only from the Release workflow, which the maintainer runs (`docs/release.md`). Never release a CI-only or docs-only change, and never deploy by hand: users install a release through HACS.
- **Statistics continuity matters more than tidiness.** Statistic ids, entity unique ids and the config-entry migration path must keep existing installations working. Never rewrite stored statistics except through the documented one-off rebuild, and never run SQL against the recorder.
- **Prices** change every 1 July. Add new years to `tariffs.py` with a cited source in `docs/tariffs.md`.

## Working

- Set up with `uv sync --locked` (install uv first in a cloud session). Keep caches out of the repo.
- Checks: `uv run ruff format --check .`, `uv run ruff check .`, `uv run mypy custom_components/watercare`, `uv run pytest`, `uv run python scripts/check_module_coverage.py`, `uv run python scripts/check_versions.py`.
- Snapshot tests (syrupy) cover entity states and diagnostics: `tests/snapshots/`. After an intended change, run `uv run pytest --snapshot-update`, then review the `.ambr` diff before committing it.
- Home Assistant version under test: `pyproject.toml` overrides the harness's Home Assistant beta with its release (2026.10.0). Bump `pytest-homeassistant-custom-component` to move it, and drop the override (and the `homeassistant` dev pin) once a harness release pins a final release; `test_home_assistant_under_test_matches_the_harness` fails if they drift apart. Dependabot ignores `homeassistant` on purpose.
- `.github/workflows/compat.yml` runs weekly against the newest Home Assistant release and harness, unpinned, and opens an issue when it fails. GitHub disables scheduled workflows in a public repository after 60 days without activity; check with `gh run list --workflow compat.yml` and re-enable with `gh workflow enable compat.yml` (and `validate.yml`, `codeql.yml`).
- Branches `<type>/<slug>`, squash-merge PRs into `main`; all GitHub Actions pinned to full commit SHAs, and every checkout sets `persist-credentials: false`.
- Upstream guidance for HA integrations: `home-assistant/core/.claude/skills` (adapt paths to `custom_components/`).
- Question inherited behaviour. Code carried over from 1.4.1 or the original project is not right just because it exists: check it against Home Assistant's current guidance, the documented API behaviour or a test before keeping it. Prefer a test or a read-only probe to argument, and write the reasoning in `docs/` or the PR, not only the outcome.

## Facts that are easy to get wrong

- Watercare's mobile API only returns completed billing periods; dates are Auckland midnight in UTC; usage is in litres, in whole kilolitres for mechanical meters.
- Watercare charges a whole bill at the prices in force when its billing period **starts** (`statistics.pricing_date`), so a bill spanning 1 July keeps the earlier year's prices. Checked against one real bill; see `docs/tariffs.md`.
- Sign-in is Azure AD B2C self-asserted (email and password); there is no password-grant policy. A missing `SETTINGS` object on the login page means maintenance, not bad credentials. Each sign-in gets its own session from `async_create_clientsession` (own cookie jar, `quote_cookie=False`), detached afterwards, never closed.
- No API exposes prices. The Energy dashboard's water picker needs `unit_class="volume"`.
- The usage and cost sensors deliberately have no state class; the external statistics feed the Energy dashboard.
- The recorder does not work through its queue until Home Assistant has started, and start-up waits for config entries being set up. Never await a recorder task (or its `on_done`) in setup or a poll: 1.5.0 did, and held start-up for 5 minutes until the setup was cancelled between the clear and the import. Statistics run in a background task after start-up; see `docs/statistics.md#start-up-and-the-recorder`. `Recorder.async_block_till_done` covers the tasks still queued, not one the recorder is already running; that gap is harmless only because a statistic with no stored rows is always imported in full, so keep that rule.
- This repository is `lafro/ha-watercare`, not a fork, and the old name `lafro/ha-watercare-next` only redirects here. The Release workflow publishes only from `lafro/ha-watercare`.
