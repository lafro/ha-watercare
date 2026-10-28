# Release process

HACS offers an update only when the version string changes, so every shippable change needs a new version. Changes that only touch CI, tests or docs do not get a release of their own.

1. In a pull request, set the same version in `custom_components/watercare/manifest.json` and `pyproject.toml` (no `v` prefix) and add a non-empty `## X.Y.Z` section to `CHANGELOG.md`; `scripts/check_versions.py` fails without it.
2. Run the [journeys](../CONTRIBUTING.md#journeys) on the pull request's branch, for every release; all seven must pass.
3. Merge the pull request once all required checks pass.
4. A release that changes sign-in, setup or the first import is first tried as a new installation, with the live test in [CONTRIBUTING](../CONTRIBUTING.md#live-test) (`--ref` with the merge commit).
5. Run the **Release** workflow from `main` with the tag `vX.Y.Z` (Actions → Release → Run workflow). After the gates pass, the publishing job waits in the `release` environment until the maintainer approves it (the run's **Review deployments** button). It re-runs every gate from a clean install, checks that the manifest's links name the repository it runs in, creates the tag on the exact commit that passed, checks it, then publishes the release with that version's CHANGELOG section as its notes (HACS shows them in the update dialog). It never edits files.
6. Tags matching `v*` cannot be moved or deleted (tag ruleset), and releases are immutable. A defect found after release is fixed with a new patch version, never by replacing a release.
7. Install the release through HACS on a test or production instance and check it (see [migration.md](migration.md) for 1.5.0).

## Each July

Watercare publishes new prices for 1 July. Follow [tariffs.md](tariffs.md#adding-a-new-year) and release a minor version.
