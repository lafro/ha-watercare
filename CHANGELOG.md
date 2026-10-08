# Changelog

## 1.5.0

First release from the standalone `lafro/ha-watercare` repository. The domain, entities and statistic ids are unchanged, so an existing installation keeps its configuration.

### Statistics: what changes in the Energy dashboard

- **Daily rows instead of one spike per bill.** Each bill's volume and cost are spread evenly over the days the bill covers. Day, week and month views now show water use where it happened. Before, the whole bill landed on the hour after the period ended.
- **Each bill priced at the prices of its own year.** The integration carries Watercare's published residential prices for every financial year from 2018/19 to 2026/27, with sources in [docs/tariffs.md](docs/tariffs.md). Each day is priced with that day's prices, so a bill that spans 1 July is split between the two years. Before, every past bill was recalculated at the one flat price in the options on every poll.
- **History is no longer rewritten.** Each poll only adds days after the stored history and continues its running total. A changed price, or a shorter response from Watercare, can no longer change or step down past values.
- **One-off rebuild on upgrade.** On its first poll, 1.5.0 clears the four `watercare:*` statistics and imports the full history in the new format, once. Back up Home Assistant and export those statistics first: see [docs/migration.md](docs/migration.md). If stored history reaches further back than Watercare now returns, the rebuild is skipped and a repair notice explains why.
- **Costs pause rather than guess.** If the prices for the current financial year are not known, cost statistics stop from 1 July and a repair notice asks for the prices from the bill. Water use keeps recording.

Expect different cost totals from 1.4.x: the 1.4.x defaults were the 2025/26 prices, so older bills now cost less and bills from 1 July 2026 more.

### Configuration

- The four flat price options are replaced by the published price table. The options now hold the wastewater ratio and, if needed, the current year's prices. Flat prices from 1.4.x that match a published year are dropped; others are kept as the current year's prices.
- New **Reconfigure** step to change the email or password for the same account.
- Re-authentication refuses credentials for a different account.
- Setup errors now distinguish an unexpected error (`unknown`) from a connection problem.
- Home Assistant 2026.10.0 or newer is required.

### Reliability and privacy

- The sign-in token is stored, so restarts renew it instead of signing in with the password each time.
- A Watercare maintenance page no longer triggers re-authentication.
- New diagnostics download with credentials, account and meter numbers, usage and costs left out.
- Logs no longer include account records, sign-in settings or response bodies.
- The usage sensor's attributes no longer include the meter number (it remains available as the **Meter number** sensor) or the endpoint. The rate attributes now show the prices of the bill's own year, and a `tariff_year` attribute is added.

### Project

- Fresh, standalone repository (not a GitHub fork) with synthetic test data only.
- Tests run on every change with a 95% per-module branch-coverage gate, strict typing, Hassfest and HACS validation. Releases are created only by a gated workflow and never edit the manifest.
- Entity names, icons, errors and repair notices are translatable.

## 1.4.1 and earlier

Released from `lafro/ha-watercare`, a fork of [brunsy/ha-watercare](https://github.com/brunsy/ha-watercare). That history is not carried into this repository; the first commit here imports the 1.4.1 code.
