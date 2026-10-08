# Statistics model

This page explains how the integration turns Watercare bills into Home Assistant long-term statistics, and why. The code is [`custom_components/watercare/statistics.py`](../custom_components/watercare/statistics.py).

## The four statistics

| Statistic id | Unit | Unit class | Contents per day |
|---|---|---|---|
| `watercare:water_consumption` | L | volume | Litres used. |
| `watercare:water_cost` | NZD | none | Water + wastewater + fixed charge. |
| `watercare:consumption_cost` | NZD | none | Water supply charge. |
| `watercare:wastewater_cost` | NZD | none | Wastewater volume charge (no fixed charge). |

They are external statistics (source `watercare`), so they are not tied to an entity and survive entity renames and removing the integration. The ids have not changed since 1.2.x, so the Energy dashboard keeps its configuration across upgrades. The consumption statistic has the `volume` unit class, which the Energy dashboard's water picker requires.

Each row is one Auckland calendar day, stamped at local midnight (always a whole hour in UTC, across daylight saving). Rows carry `state` (that day's amount) and `sum` (the running total).

## What Watercare provides

The customer-app API returns completed billing periods for mechanical meters: start date, end date, litres (whole kilolitres), Watercare's day count, reading type (estimate or actual) and some statistics. There is no in-progress period and no price data. Dates arrive as Auckland midnight expressed in UTC, so `2026-07-15T12:00:00.000Z` is 16 July in Auckland.

## Spreading each bill over its days

Before 1.5.0 the whole bill was stamped on the hour after the period ended. In the Energy dashboard that showed nothing for weeks, then one spike, and month views booked most of a bill to the following month.

From 1.5.0 each bill is spread evenly over the days it covers:

- A bill covers its start date to its end date, inclusive. Watercare counts both ends too (16 June to 16 July is 31 days).
- Bills never share a day. If a bill starts on or before the previous bill's end date, it starts the day after instead. This holds whichever way Watercare dates consecutive bills (the next bill starting the day after, or on the same day as, the previous one ended), and keeps every litre counted once.
- A gap between bills (for example a meter change) gets no rows.
- The split is exact: each bill's daily rows add up to the bill, using shares rounded to a billionth of a litre so rounding never accumulates.

A mechanical meter is read about monthly in whole kilolitres, so a flat daily profile adds no false precision. Watercare's own app presents the same per-period daily average.

## Anchoring to stored history

A normal poll:

1. reads the newest stored row of each statistic;
2. computes daily rows from all bills Watercare returned;
3. writes only the days after the newest stored row, continuing its running sum.

Consequences:

- Stored history is never rewritten by a poll. Changing a price for a year that is already stored does not reprice it.
- If Watercare ever returns fewer bills than before, nothing is lost and the sum never steps down.
- A new bill that would reach back into stored days is spread over the days after them instead, so its whole volume is still recorded.
- Watercare corrects an estimated reading through the next bill, not by revising the old one, so nothing needs rewriting.

## Costs at the tariff in force

Each day is priced with that day's financial-year tariff ([tariffs.md](tariffs.md)). If no tariff is known for a day (a new financial year the release does not yet carry and the user has not entered), the cost statistics stop at the day before, and resume from that day once a tariff exists. The consumption statistic is unaffected. A repair notice asks for the prices.

## The one-off rebuild (upgrade from 1.4.x)

Statistics written by 1.4.x have one row per bill and are priced at one flat tariff, so they cannot be continued. Each config entry records `statistics_version: 2` once its statistics are in the new format. On the first successful poll of an entry without that marker, the integration:

1. checks that the stored consumption history does not start before the oldest bill Watercare now returns (otherwise a rebuild would lose history; see below);
2. clears the four statistics through the recorder's own queue (`Recorder.async_clear_statistics`) and waits for the recorder to confirm;
3. imports the full history as daily rows from zero (`async_add_external_statistics`, the recorder API for external statistics; `async_import_statistics` is the equivalent for entity statistics);
4. records the marker.

Both steps run in the event loop and are queued on the recorder thread in order, so the import always follows the clear. Nothing runs SQL directly. The log shows a warning before and after the rebuild. It only touches the four `watercare:*` statistics and only runs after Watercare has returned at least one valid bill.

A new config entry also has no marker, so its first poll clears and re-imports these statistics too. That is harmless: the result is the same rows.

### When the rebuild is skipped

If the stored history starts earlier than the oldest bill Watercare returns, the rebuild would delete history that cannot be recreated. The integration then keeps the stored rows, adds new days after them in the new format, records the marker and raises a repair notice, **Watercare statistics were not rebuilt**.

To rebuild by hand in that case, after exporting the statistics (see [migration.md](migration.md)):

1. delete the four statistics under **Developer tools → Statistics**;
2. delete the Watercare integration entry and add it again.

The new entry's first poll imports everything Watercare returns.
