# Watercare for Home Assistant: agent notes

Public HACS integration (`custom_components/watercare`, domain `watercare`). Read `README.md`, `docs/statistics.md` and `docs/tariffs.md` before changing behaviour. This repository is public: everything committed here, and every PR or issue text, is world-readable.

## Rules

- **Synthetic data only.** Never put a real Watercare account or meter number, email, name, address, token, balance, usage or cost figure, or the dates of a real bill, in code, tests, docs, commits, PRs or issues, nor details of the maintainer's own installation. Use the values in `tests/common.py` (made-up bills on the 3rd and 4th of the month).
- **Secret scan before every push**: `gitleaks git --log-opts="origin/main..HEAD"` and `gitleaks dir .` (or a grep for the same patterns if gitleaks is missing). Anything found blocks the push. The contract tests also reject account-number and meter-id shapes and non-example emails, but they cannot recognise a real usage figure.
- **No Home Assistant access from this repository's sessions.** Never call Home Assistant tools, and never deploy by hand (no SSH, Samba or copying into `custom_components`). Deploy = release, then a HACS download of that version, then a restart, done from the Home Assistant operations repository.
- **GitHub only.** Use no connector other than GitHub from this repository (no issue trackers, email, chat, calendars, website or marketing tools).
- **The maintainer merges and releases.** Open a pull request, wait for CI (`gh pr checks --watch`), and stop. Never create, move or delete tags or releases, never dispatch the Release workflow, never rename or delete the repository, never force-push. Never release a CI-only or docs-only change. See `docs/release.md`.
- **Statistics continuity matters more than tidiness.** Statistic ids, entity unique ids and the config-entry migration path must keep existing installations working. Never rewrite stored statistics except through the documented one-off rebuild, and never run SQL against the recorder.
- **Prices** change every 1 July. Add new years to `tariffs.py` with a cited source in `docs/tariffs.md`.
- Do not open PRs or issues upstream (`brunsy/ha-watercare`) or post on forums without the maintainer's explicit approval.

## How the rules are enforced

`.claude/settings.json` has three layers; none of them is relied on alone. It is a copy of the guard in `lafro/ha-meridian-energy`; keep the two in step.

1. **PreToolUse hooks** (the main layer):
   - any tool whose name matches `mcp__.*__ha_.*` is refused with exit 2, whatever the connector's server name;
   - every Bash call goes through `.claude/hooks/guard.sh` → `guard.py`, which blocks pushes to `main`, tag pushes and tag writes, force pushes and remote-ref deletion, merges, release writes and Release workflow dispatches, including `gh api`/curl spellings and `git -C`, `sh -c`, `$(...)` forms. The cases are in `tests/test_agent_guard.py`; extend them with every rule change.
2. **Deny rules** for the same commands as written, and for Home Assistant connectors by their cloud and local tool prefixes (`mcp__claude_ai_Home_Assistant`, `mcp__ha-mcp`, `mcp__home-assistant`). Deny rules match a tool or command only as spelled, and in a desktop session a connector's tools carry a per-installation ID prefix that these do not match. Keep such IDs out of this public repository; a per-machine deny belongs in the gitignored `.claude/settings.local.json` or in user settings.
3. **`deniedMcpServers`** by connector display name. Whether repository-scope entries reach a session's connectors has not been verified.

The hooks match on tool names, so they work on any surface, but the guard has only been tested by feeding it events, not yet in a live session. Treat this file's rules as binding either way, and still start sessions here without a Home Assistant connector attached.

## Working

- Set up with `uv sync --locked` (install uv first in a cloud session). Keep caches out of the repo.
- Checks: `uv run ruff format --check .`, `uv run ruff check .`, `uv run mypy custom_components/watercare`, `uv run pytest`, `uv run python scripts/check_module_coverage.py`, `uv run python scripts/check_versions.py`.
- Snapshot tests (syrupy) cover entity states and diagnostics: `tests/snapshots/`. After an intended change, run `uv run pytest --snapshot-update`, then review the `.ambr` diff before committing it.
- Home Assistant version under test: `pyproject.toml` overrides the harness's Home Assistant beta with its release (2026.10.0). Bump `pytest-homeassistant-custom-component` to move it, and drop the override (and the `homeassistant` dev pin) once a harness release pins a final release; `test_home_assistant_under_test_matches_the_harness` fails if they drift apart. Dependabot ignores `homeassistant` on purpose.
- `.github/workflows/compat.yml` runs weekly against the newest Home Assistant release and harness, unpinned, and opens an issue when it fails. GitHub disables scheduled workflows in a public repository after 60 days without activity; check with `gh run list --workflow compat.yml` and re-enable with `gh workflow enable compat.yml` (and `validate.yml`, `codeql.yml`).
- `.claude/hooks/guard.py` must stay runnable on Python 3.9 with the standard library only (ruff checks it with a py39 target; a test parses it with the 3.9 grammar).
- Branches `<type>/<slug>`, squash-merge PRs into `main`; all GitHub Actions pinned to full commit SHAs, and every checkout sets `persist-credentials: false`.
- Upstream guidance for HA integrations: `home-assistant/core/.claude/skills` (adapt paths to `custom_components/`).
- Question inherited behaviour. Code carried over from 1.4.1 or the original project is not right just because it exists: check it against Home Assistant's current guidance, the documented API behaviour or a test before keeping it. Prefer a test or a read-only probe to argument, and write the reasoning in `docs/` or the PR, not only the outcome.

## Facts that are easy to get wrong

- Watercare's mobile API only returns completed billing periods; dates are Auckland midnight in UTC; usage is in litres, in whole kilolitres for mechanical meters.
- Watercare charges a whole bill at the prices in force when its billing period **starts** (`statistics.pricing_date`), so a bill spanning 1 July keeps the earlier year's prices. Checked against one real bill; see `docs/tariffs.md`.
- Sign-in is Azure AD B2C self-asserted (email and password); there is no password-grant policy. A missing `SETTINGS` object on the login page means maintenance, not bad credentials. Each sign-in gets its own session from `async_create_clientsession` (own cookie jar, `quote_cookie=False`), detached afterwards, never closed.
- No API exposes prices. The Energy dashboard's water picker needs `unit_class="volume"`.
- The usage and cost sensors deliberately have no state class; the external statistics feed the Energy dashboard.
- Until the cut-over in `docs/release.md`, this repository is `lafro/ha-watercare-next` while every public link names `lafro/ha-watercare`; the Release workflow refuses to publish before the rename.
