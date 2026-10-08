# Release process

HACS offers an update only when the version string changes, so every shippable change needs a new version. Changes that only touch CI, tests or docs do not get a release of their own.

1. In a pull request, set the same version in `custom_components/watercare/manifest.json` and `pyproject.toml` (no `v` prefix) and add a non-empty `## X.Y.Z` section to `CHANGELOG.md`; `scripts/check_versions.py` fails without it.
2. Merge the pull request once all required checks pass.
3. Run the **Release** workflow from `main` with the tag `vX.Y.Z` (GitHub web or GitHub Mobile: Actions → Release → Run workflow). It re-runs every gate from a clean install, checks that the manifest's links name the repository it runs in, creates the tag on the exact commit that passed, checks it, then publishes the release with that version's CHANGELOG section as its notes (HACS shows them in the update dialog). It never edits files.
4. Tags matching `v*` cannot be moved or deleted (tag ruleset), and releases are immutable. A defect found after release is fixed with a new patch version, never by replacing a release.
5. Install the release through HACS on a test or production instance and check it (see [migration.md](migration.md) for 1.5.0).

Agents working on this repository never create, move or delete tags or releases by hand; the maintainer dispatches the workflow.

## Cut-over to `lafro/ha-watercare` (once, before 1.5.0)

This repository starts life as `lafro/ha-watercare-next`, while `lafro/ha-watercare` is still the old fork (1.4.1, issues and private vulnerability reporting off). Every public link (manifest documentation and issue tracker, the repair notices' "learn more" links, README, SECURITY.md, the issue template, HACS) names `lafro/ha-watercare`, so the names must swap before anything is released or installed. The maintainer does these steps; agents do not rename repositories.

1. Rename `lafro/ha-watercare` (the fork) to `lafro/ha-watercare-legacy`.
2. Rename `lafro/ha-watercare-next` to `lafro/ha-watercare`.
3. Check: `gh repo view lafro/ha-watercare --json isFork,hasIssuesEnabled` returns `false` and `true`, and `gh api repos/lafro/ha-watercare/private-vulnerability-reporting` returns `"enabled": true`.
4. Only then dispatch the Release workflow, then switch HACS: remove the old custom repository, add `https://github.com/lafro/ha-watercare`, download.

Step 4 is enforced: the Release workflow's `python` job fails unless the manifest's documentation URL is the repository the workflow runs in (`check_versions.py --repository`), and its `release` job runs only in `lafro/ha-watercare`.

## Merging the first pull request

`main` was created by the import commit, whose LICENSE GitHub reports as `NOASSERTION`. HACS validation reads the licence of the **default branch**, so the required `hacs` check fails on every pull request until a fixed LICENSE is on `main`, and the "Protect main" ruleset has no bypass actors. The maintainer, once:

1. Saves the ruleset (`gh api repos/lafro/<current name>/rulesets/24698290 > before.json`), then temporarily adds Repository admin as a pull-request bypass actor, or removes `hacs` from its required checks.
2. Squash-merges the pull request.
3. Checks that the push-to-`main` Validate run's `hacs` job passes and that `gh api repos/lafro/ha-watercare/license --jq .license.spdx_id` returns `MIT`.
4. Restores the ruleset exactly and compares its JSON with `before.json`.

## Before releasing 1.5.0

- The cost of a bill that spans 1 July follows one checked bill (see [tariffs.md](tariffs.md#how-a-bill-is-priced)). Compare the charge lines of another such bill, for example one issued in July 2025, with the integration's figure for it before releasing. If Watercare split it differently, change `pricing_date` (or the model) first: the one-off rebuild stores whatever this release calculates, and correcting it later needs another rebuild.
- Follow the pre-flight in [migration.md](migration.md#before-upgrading), including the rollback rehearsal on a restored copy.

## Each July

Watercare publishes new prices for 1 July. Follow [tariffs.md](tariffs.md#adding-a-new-year) and release a minor version.
