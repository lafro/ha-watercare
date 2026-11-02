# Architecture

How the code in `custom_components/watercare/` is put together, as of 1.6.0. Tags are explained in [README.md](README.md#accuracy-tags). Code claims marked ✅ were checked against 1.5.1 on 9 October 2026, and those about 1.6.0's changes against 1.6.0 on 2 November 2026.

What users see is in the [README](../README.md). The statistics model has its own page, [statistics.md](statistics.md), and so do the prices, [tariffs.md](tariffs.md); this page links to them rather than repeating them.

## Files and responsibilities

| File | Responsibility |
|---|---|
| `manifest.json` | A `service` integration polling the cloud (`cloud_polling`), one config entry per Home Assistant instance (`single_config_entry`). Depends on `recorder`; `requirements` is empty, because Home Assistant already ships everything the code imports. |
| `const.py` | Domain, the Auckland time zone, the 12-hour poll interval, config keys, the statistics-format marker, the option keys 1.4.x wrote, the four statistic ids, the repair issue ids and documentation links. |
| `__init__.py` | Entry setup and unload, the config-entry migration from 1.4.x, the entity-registry fixes carried from earlier releases, and starting the statistics once Home Assistant has started. |
| `api.py` | The customer-app API client: Azure AD B2C sign-in, token refresh, the account record, the billing periods and the bills and payments, and the error types Home Assistant must tell apart. |
| `session.py` | A short-lived session from Home Assistant's own helper for each sign-in, with a cookie jar of its own. |
| `models.py` | Typed, tolerant parsing of the account record, the billing periods and the issued bills, and the match between a bill and its billing period. Raw payloads are not kept, so they cannot reach logs or diagnostics. |
| `coordinator.py` | The `DataUpdateCoordinator`. A poll fetches the account and the bills, prices the latest bill and reads the amount of the latest issued bill; the statistics update follows in a background task. Raises and clears the tariff, smart-meter and newer-version repair issues. |
| `statistics.py` | The statistics model: each bill spread over its days, priced at the tariff of its start date, appended after the stored history; the one-off rebuild from 1.4.x and the check that it would lose nothing. |
| `tariffs.py` | Watercare's published residential prices by financial year, and the schedule that combines them with prices entered in the options. |
| `sensor.py` | Twelve sensors, each a `SensorEntityDescription`, on one service device. |
| `config_flow.py` | Setup, re-authentication, reconfiguration, and the options (wastewater ratio and the current financial year's prices). |
| `repairs.py` | The fix flow that asks for a financial year's prices. |
| `diagnostics.py` | Diagnostics with credentials, identifiers, usage and costs left out. |
| `strings.json`, `translations/en.json`, `icons.json` | Form text, translatable errors and repair notices, entity names and icons. |
| `quality_scale.yaml` | Home Assistant's integration quality scale, rule by rule. |
| `brand/` | The icon and logo images Home Assistant serves for the integration. They are built outside this repository. |

Outside the integration: `tests/` (pytest with Home Assistant's test harness and syrupy snapshots), `scripts/` (version and coverage checks, and the statistics rollback in [migration.md](migration.md)), and the GitHub workflows described in [CONTRIBUTING.md](../CONTRIBUTING.md) and [release.md](release.md).

## Data flow

```
Home Assistant, every 12 hours
  WatercareCoordinator._async_update_data
    ├─ api.async_get_account()            GET v1/account
    │     (first renews the access token if needed: refresh token, else a full sign-in)
    ├─ api.async_get_billing_periods()    GET v1/usage/{account}/mechanicalmonthly
    ├─ models.parse_billing_periods()     skip malformed periods, drop duplicates, sort
    ├─ statistics.bill_cost(latest bill)  at the tariff of the bill's start date
    ├─ api.async_get_transactions()       GET v1/account/{account}/transactions
    │     (the latest issued bill's amount; a failure keeps the last one read since start-up and never fails the poll)
    └─ returns WatercareData ──▶ the sensors (CoordinatorEntity)
          │
          └─ hands the bills to the statistics task, which runs only after Home Assistant has started
                WatercareCoordinator._async_run_statistics
                  ├─ entry already in the 1.5 format:  statistics.async_import (new days only)
                  └─ entry from 1.4.x:                 statistics.async_rebuild (once)
                                                       or, if that would lose history,
                                                       async_import and a repair notice
```

✅ (code)

- The account record is fetched once per poll, and the bills and transactions requests use the account number from it. ✅ (code)
- The transactions list holds bills and payments with their dates and amounts but no billing period. The latest bill is matched to the latest billing period that ended on or up to seven days before the bill's date, read as an Auckland date ([known-unknowns.md](known-unknowns.md#known-unknowns)). A bill with no such period still shows its amount, without a period. An item whose date has no Auckland date (a `9999-12-31` placeholder, say) is skipped. Bills are ordered by their timestamps, then their amounts, so of two bills on the same date the newest does not depend on the order Watercare lists them in. If the transactions request fails, or its answer cannot be read for any reason, the sensor keeps the amount read earlier since start-up, or stays unknown if there is none, and the poll goes on. ✅ (code)
- Parsing is tolerant: unknown keys are ignored, and both timestamp forms Watercare sends are accepted (milliseconds on usage dates, none on the account's due date). A billing period is skipped only when it is not an object, a date is missing, unparseable or has no Auckland date (a `9999-12-31` placeholder, say), the end is before the start or on or after today's Auckland date (the endpoint returns only completed periods: a later end would spread usage over days that have not happened, and an unfinished period would end today and leave its partial usage in the append-only history, so a period read today loads the next day), or `waterUsage` is not a number, negative or non-finite (`NaN`, `Infinity`). A `null` or missing `waterUsage` counts as 0 litres and the period is kept ([known-unknowns.md](known-unknowns.md#assumptions-the-code-acts-on), A8). A `numberOfDays` that is missing, not a finite number or below 1 falls back to counting the days, both ends included, and a `dailyAverage` that is not a finite number becomes empty. A repeated period (same start and end) is kept once. ✅ (code)
- Entities are thin. Each sensor is a description with a `value_fn` over the poll's `WatercareData`; the usage sensor also has an `attributes_fn` that keeps the 1.4.x attribute names for dashboards built on them. Unique ids are `<entry id>_<key>`. ✅ (code)

## Sign-in and tokens

- Sign-in is Azure AD B2C's self-asserted (email and password) flow with PKCE, the flow Watercare's app uses. A login page without B2C's `SETTINGS` object means maintenance, a rate limit or a changed flow, not bad credentials. ✅ (code)
- There is no simpler sign-in to switch to: Watercare's B2C tenant has no password-grant policy. ✅ (API probe, August 2026)
- Each sign-in runs in its own session from `async_create_clientsession`, with its own cookie jar (`quote_cookie=False`, because B2C's cookies hold characters aiohttp would otherwise quote). The session is detached afterwards, never closed, because its connector belongs to Home Assistant. Every other request uses Home Assistant's shared session. ✅ (code)
- The refresh token is stored in the config entry whenever it changes, so a restart renews the access token instead of signing in with the password. The access token lives in memory and is renewed 60 seconds before it expires. If the API answers 401, the client renews the token once and retries. ✅ (code)
- Logs never include credentials, tokens, account or meter numbers, URLs that contain them, or response bodies, and use `%`-style formatting. ✅ (code)

## Errors

| What went wrong | Raised as | The coordinator raises | Home Assistant then |
|---|---|---|---|
| Watercare rejected the email or password, refused the sign-in, returned no authorisation code or access token, or answered 401 to a token it had just issued | `WatercareAuthError` | `ConfigEntryAuthFailed` | starts re-authentication |
| Watercare answers with `"error": "service_maintenance"`, whatever the HTTP status | `WatercareMaintenanceError` | `UpdateFailed` (`service_maintenance`) | marks the sensors unavailable and tries again at the next poll; setup shows "Watercare is down for maintenance" |
| Watercare answers with `"error": "upgrade_required"`, whatever the HTTP status | `WatercareUpgradeRequiredError` | `UpdateFailed` (`upgrade_required`) and the non-fixable repair issue `upgrade_required_<entry id>` | as above; the issue goes once a poll gets the account and the bills again |
| Network error or timeout, any other HTTP error, a maintenance page at sign-in, malformed JSON, no account | `WatercareConnectionError` | `UpdateFailed` (`cannot_connect`) | as above |
| The billing periods are not a list | `WatercarePayloadError` | `UpdateFailed` (`unexpected_response`) | as above |
| No usable billing period | (none) | `UpdateFailed` (`no_billing_periods`) | as above |

✅ (code). The two service errors are subclasses of `WatercareConnectionError`, so anything that handles a connection error handles them too. Telling `WatercareAuthError` apart from the connection errors is what stops a Watercare outage from asking the user for their password. During setup, Home Assistant turns `UpdateFailed` into a setup retry.

A failed statistics update does not fail the poll or the entry: it is logged and the next poll tries again (1.5.1). ✅ (code)

## Long-term statistics

The details and the reasoning are in [statistics.md](statistics.md). In short ✅ (code):

- Four external statistics with source `watercare`, ids unchanged since 1.2.x. All have `has_sum=True` and `mean_type=NONE`. Consumption is in litres with `unit_class="volume"`, which the Energy dashboard's water picker requires; the three cost statistics are in NZD with no unit class.
- One row per Auckland day, stamped at local midnight (a whole hour in UTC all year).
- Each bill is spread evenly over its days and priced at the tariff of its start date (`statistics.pricing_date`). Cost rows stop at the first day whose bill has no known tariff.
- A poll only appends days after the newest stored row, continuing its running sum. Entries without `statistics_version: 2` in their data get a one-off rebuild, unless it would lose history.
- All of it runs in a background task of the config entry, one update at a time, and only after Home Assistant has started. Reads wait for the recorder's queue first. Setup never waits for the recorder: 1.5.0 did, and held start-up for 5 minutes ([statistics.md](statistics.md#start-up-and-the-recorder)).

### Why the usage and cost sensors have no state class

A `state_class` makes Home Assistant's recorder compile long-term statistics for the entity itself, on top of the external ones above. **Last bill usage** and **Last bill cost** hold per-bill totals that go up and down from one bill to the next; they are not counters. With `TOTAL_INCREASING` or `TOTAL`, the recorder compiled corrupt statistics for them (spurious resets and net deltas), which also appeared in the Energy dashboard's water picker as a second, wrongly summed source. Without a state class there are no such statistics, and the four `watercare:*` statistics are the only long-term record. ✅ (review finding and the running installation, 8 August 2026; the state class was removed in 1.4.x)

**Last bill amount** and the account sensors (balance, amount due, overdue amount) have no state class either. **Average daily usage** has `MEASUREMENT`: it is a per-day rate, not a total. ✅ (code)

## Config entry

| Part | Holds |
|---|---|
| `unique_id` | The Watercare account number, so the same account cannot be added twice, and re-authentication and reconfiguration refuse a different account (`wrong_account`). It never appears in diagnostics. |
| `data` | `username` (the email), `password`, `refresh_token`, and `statistics_version` once the statistics are in the 1.5 format. |
| `options` | `wastewater_ratio` (default 0.785), and `tariff_overrides`: prices entered for particular financial years, keyed by the year the financial year starts in. |

✅ (code). The entry is version 1.2. `async_migrate_entry` moves 1.4.x entries (1.1) to it: flat prices that equal a published year are dropped, others are kept as the current financial year's prices, and the keys 1.4.x wrote (the endpoint among them) are removed. A version newer than 1 is refused rather than guessed at. ✅ (code)

## Setup, options, re-authentication and repairs

- **Setup** signs in before creating the entry, sets the account number as the unique id, and shows `invalid_auth`, `service_maintenance`, `upgrade_required`, `cannot_connect` or `unknown` on failure. Only one entry is allowed: the statistic ids are shared, so a second account would overwrite the first one's history. ✅ (code)
- **Options** hold the wastewater ratio and the current financial year's prices, pre-filled from the published table or an earlier entry. For a year with no known prices the fields start empty (the `new_year` step), and all three or none must be entered, so saving the form unchanged can never store last year's prices as a guess. Saving reloads the entry. ✅ (code)
- **Re-authentication** asks for the current password when Watercare rejects the stored one. **Reconfigure** changes the email and password for the same account. ✅ (code)
- **Repairs:** when the cost statistics pause for a year without prices, or the current year has none yet, a fixable repair issue asks for that year's prices; fixing it stores them for that year only and reloads the entry. A second, non-fixable issue explains a skipped rebuild ([statistics.md](statistics.md#when-the-rebuild-is-skipped)), a third says smart meters are not supported yet ([Smart meters](#smart-meters)), and a fourth says Watercare asked for a newer version of its app ([Errors](#errors)). Removing the entry deletes all four and keeps the statistics. ✅ (code)

## Entity-registry fixes from earlier releases (`__init__.py`)

On every setup ✅ (code):

- an entry created before 1.2.2 without a unique id gets the account number;
- the usage sensor's unique id from before 1.2.2 (the bare domain) moves to `<entry id>_usage`, which keeps its entity id and history;
- **Account balance** and **Amount due** are re-enabled only if the integration disabled them (1.2.x shipped them disabled while their data was missing); a sensor the user disabled stays disabled.

## Diagnostics

Diagnostics get pasted into public issues, so they hold only shapes, counts, dates and statuses: the entry version, its data with the username, password, refresh token and identifiers redacted, the wastewater ratio, which years have entered prices, the latest published year, the coordinator's status, the latest bill's dates and reading type, and whether the latest issued bill was read, with its date and whether a billing period matched it. No account or meter number, balance, usage, cost or bill amount. ✅ (code; `tests/snapshots/test_diagnostics.ambr`)

## Smart meters

Only `mechanicalmonthly` is fetched. Up to 1.4.1 the code kept the endpoint constants and an endpoint option, fixed to `mechanicalmonthly`, as a seam for re-adding smart meters. 1.5.0 removed them: the migration drops the option and nothing reads it. Re-adding smart-meter support starts from a real smart-meter response, because their payloads have never been seen ([known-unknowns.md](known-unknowns.md#known-unknowns)). ✅ (code)

Each poll checks the account before it fetches the bills, because a smart-meter account may have none, or get an error for them. If `meterType` is anything but `mechanical` (Watercare's app also knows `smartmeter-single` and `smartmeter-other`), or `transitionedMeterFromMMToSM` is `true` on the account or one of its meters, it raises the non-fixable repair issue `smart_meter_<entry id>`. Otherwise it deletes the issue. A missing `meterType` raises nothing. Polling is otherwise unchanged: the bills from before a change to a smart meter still load. ✅ (code)
