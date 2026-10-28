# Release process

HACS offers an update only when the version string changes, so every shippable change needs a new version. Changes that only touch CI, tests or docs do not get a release of their own.

1. In a pull request, set the same version in `custom_components/watercare/manifest.json` and `pyproject.toml` (no `v` prefix) and add a non-empty `## X.Y.Z` section to `CHANGELOG.md`; `scripts/check_versions.py` fails without it.
2. Run the [journeys](../CONTRIBUTING.md#journeys) on the pull request's branch, for every release; all seven must pass.
3. Merge the pull request once all required checks pass.
4. A release that changes sign-in, setup or the first import is first tried as a new installation, with the live test in [CONTRIBUTING](../CONTRIBUTING.md#live-test) (`--ref` with the merge commit).
5. Run the **Release** workflow from `main` with the tag `vX.Y.Z` (Actions → Release → Run workflow). After the gates pass, the publishing job waits in the `release` environment until the maintainer approves it (the run's **Review deployments** button). It re-runs every gate from a clean install, checks that the manifest's links name the repository it runs in, creates the tag on the exact commit that passed, checks it, then publishes the release with that version's CHANGELOG section as its notes (HACS shows them in the update dialog). It never edits files.
6. Tags matching `v*` cannot be moved or deleted (tag ruleset), and releases are immutable. A defect found after release is fixed with a new patch version, never by replacing a release.
7. Install the release through HACS on a test or production instance and check it (see [migration.md](migration.md) for 1.5.0).

## Compatibility runs

- `validate.yml` tests the harness pinned in `pyproject.toml` on every pull request, on `main` and weekly. Every run fails on a deprecation Home Assistant reports for this integration (see Deprecations below).
- `compat.yml` tests each new Home Assistant version, beta, release and patch alike, within hours of its harness (`pytest-homeassistant-custom-component` publishes one a day, 12 to 18 hours after each Home Assistant version). Every 6 hours `scripts/compat_plan.py` works out the targets:
  - `main` and the newest published release (what HACS installs), each on the newest harness for a final Home Assistant release (`stable`) and, while the newest harness pins a beta, on that one too (`beta`);
  - `main` on exactly the minimum Home Assistant in `hacs.json` (`minimum`), with the harness uv matches to it.

  A scheduled run tests only when the targets changed since a run that tested them: it leaves the artifact `compat-done-<fingerprint>` (kept 30 days), and the next scheduled run with the same targets stops after the plan. A manual run, or a pull request that changes the workflow or `scripts/compat_*.py`, always tests.
- Each target runs its own test suite on its harness, unpinned from `uv.lock` and ignoring any override in `pyproject.toml`, without the coverage gates and without `tests/test_repository_contracts.py`, which checks the pinned harness on purpose. `scripts/compat_pytest_plugin.py` sorts the failures:
  - `passed`;
  - `snapshot-differences`: only syrupy snapshots differ, as when Home Assistant adds a field to what a snapshot stores. Not a break; the snapshots move with the harness pin (`uv run pytest --snapshot-update`, then review the `.ambr` diff);
  - `broken`: any other failure, an error in setup or collection, or no record at all. A broken target runs its failed tests once more, and only the second outcome counts.
- Each target uploads `compat-result-<target>-<channel>` (kept 90 days) holding `result.json`, schema 1: `integration`, `integration_version`, `target` (`main` or `release`), `tag`, `ref`, `channel` (`stable`, `beta` or `minimum`), `homeassistant`, `harness`, `python`, `outcome`, `retried`, `broken_tests`, `snapshot_tests` (at most 20 each), `deprecations` (a list, or `null` for a release older than the detector) and `run_url`.
- The report keeps one issue labelled `compat` open while any target is broken and one labelled `deprecation` while any target reports a deprecation. It comments only when what the issue lists changes, and closes the issue when a run finds it clear. Pull requests never touch the issues.
- Targets: a break on a beta is fixed and released before that Home Assistant version ships; a break in the newest release on a final Home Assistant release is fixed in a patch release.

### Deprecations

`tests/ha_deprecations.py`, a pytest plugin loaded by `tests/conftest.py`, collects what Home Assistant reports for this integration: its "Detected that custom integration 'watercare' …" and "The deprecated … from watercare." log messages, and the `DeprecationWarning`, `PendingDeprecationWarning` and `FutureWarning` raised from `custom_components/watercare/`. Any of them fails a run that would otherwise pass and is listed under "Home Assistant deprecations" in the summary, so a harness bump that brings one in cannot be merged until the use is fixed. The fix is integration code and ships as a normal patch release, well before the version Home Assistant names. In `compat.yml` the plugin writes the list to the result instead (`HA_DEPRECATIONS_REPORT`). A test that triggers a report on purpose wraps it in `COLLECTOR.expected()`.

### Versions under test

- Pull requests test the release the pinned harness pins (`test_home_assistant_under_test_matches_the_harness`, and `CLAUDE.md` for the override that may swap a beta for its release).
- Dependabot's uv updates run once a month, on the 12th. The harness pins the newest Home Assistant, betas included, and from about the 23rd until the day after the release (by the 8th) that is a beta; by the 12th it is the month's release. `compat.yml` tests the patches between. Any change to `.github/dependabot.yml` makes Dependabot run at once: merged during a beta week, that opens a pull request pinned to a beta, which fails its checks and is closed.
- The minimum in `hacs.json` is raised only when the code needs something newer, in the pull request that needs it, never just because a new Home Assistant came out. The `minimum` channel keeps it true: when it breaks, the fix raises the minimum. The one other reason to raise it is a Python version: the `minimum` channel runs on the oldest Python that version of Home Assistant supports, and the minimum moves up when Home Assistant moves to a Python that version cannot run on.
- Security alerts for packages only the harness brings in (Home Assistant's own pins, such as Pillow) need no action: HACS does not install them and the integration does not use them. They clear when a harness bump reaches a Home Assistant that moves on.
- GitHub disables scheduled workflows in a public repository after 60 days without activity. Check with `gh run list --workflow compat.yml` and `gh run list --workflow validate.yml`, and re-enable with `gh workflow enable <file>`.

## Each July

Watercare publishes new prices for 1 July. Follow [tariffs.md](tariffs.md#adding-a-new-year) and release a minor version.
