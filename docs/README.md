# Maintainer documentation

How the Watercare integration works, why it works that way, and how it is released. User documentation is the [README](../README.md).

| Page | Read it when you want |
|---|---|
| [architecture.md](architecture.md) | How the code is put together: modules, data flow, sign-in, errors, configuration. |
| [statistics.md](statistics.md) | How bills become long-term statistics, the one-off rebuild, and start-up and the recorder. |
| [tariffs.md](tariffs.md) | The published prices by financial year, their sources, and how a bill is priced. |
| [history-and-decisions.md](history-and-decisions.md) | Where the code came from, what changed and why, and the problems already solved. |
| [known-unknowns.md](known-unknowns.md) | The assumptions the code relies on, what is not known, and what to re-check. |
| [migration.md](migration.md) | Upgrading from 1.4.x to 1.5: backup, pre-flight, checks and rollback. |
| [release.md](release.md) | Versions, the Release workflow, and the yearly price update. |

Reading paths:

- **Changing behaviour:** architecture, then statistics and tariffs, then known-unknowns, then release.
- **"Why does it work this way?":** history-and-decisions and known-unknowns.

## Accuracy tags

Documentation drifts from the code. [architecture.md](architecture.md), [history-and-decisions.md](history-and-decisions.md) and [known-unknowns.md](known-unknowns.md) tag their non-obvious claims so a reader can tell how far to trust them:

- ✅ **Verified**: checked against the code, the API, the upstream repository or a real installation, with the method and date.
- ⚠️ **Assumption**: believed true and acted on, but not confirmed; the basis is stated.
- ❓ **Unknown**: not known, or not tested.

A claim without a tag counts as ⚠️. When you change the code, update the page and its tag. When a ✅ claim no longer matches the code, downgrade it to ⚠️ with the date rather than deleting it, so drift stays visible.
