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

The customer-app API returns completed billing periods for mechanical meters: start date, end date, litres (whole kilolitres), Watercare's day count, reading type (estimate or actual) and some statistics. There is no in-progress period and no price data. Dates arrive as Auckland midnight expressed in UTC, so `2026-07-02T12:00:00.000Z` is 3 July in Auckland.

## Spreading each bill over its days

Before 1.5.0 the whole bill was stamped on the hour after the period ended. In the Energy dashboard that showed nothing for weeks, then one spike, and month views booked most of a bill to the following month.

From 1.5.0 each bill is spread evenly over the days it covers:

- A bill covers its start date to its end date, inclusive. Watercare counts both ends too (3 June to 3 July is 31 days).
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

Watercare charges each bill at the prices in force when its billing period starts, so the integration prices every day of a bill with the financial-year tariff of the bill's **start date** ([tariffs.md](tariffs.md#how-a-bill-is-priced)). A bill that spans 1 July keeps the earlier year's prices on all of its days, including the July ones, and a bill's daily cost rows add up to what Watercare charged for it. The **Last bill cost** sensor uses the same rows.

If no tariff is known for the year a bill starts in (a new financial year the release does not yet carry and the user has not entered), the cost statistics stop before that bill's first day, and resume from that day once a tariff exists. The consumption statistic is unaffected. A repair notice asks for the prices of the earliest year that blocks the costs, or of the current year if it has none yet. Days already recorded are never repriced, so the options and the repair form never pre-fill prices for a year the integration does not know.

## The one-off rebuild (upgrade from 1.4.x)

Statistics written by 1.4.x have one row per bill and are priced at one flat tariff, so they cannot be continued. Each config entry records `statistics_version: 2` once its statistics are in the new format. On the first statistics update of an entry without that marker (after Home Assistant has started; see below), the integration:

1. checks that a rebuild would not lose history: the stored consumption must not start before the oldest bill Watercare now returns, and its running total must not exceed what a rebuild reaches by the same day (which would mean a bill Watercare no longer returns; see below);
2. works out the daily rows for the full history, from zero;
3. queues a clear of the four statistics (`Recorder.async_clear_statistics`) and then the import of those rows (`async_add_external_statistics`, the recorder API for external statistics; `async_import_statistics` is the equivalent for entity statistics) on the recorder's queue;
4. waits until the recorder has taken them off its queue;
5. records the marker.

Step 3 happens in one go in the event loop, without awaiting anything, so the recorder always runs the import straight after the clear, and no cancellation, timeout or shutdown can come between them. Nothing runs SQL directly. Step 4 is there because queued is not written: at shutdown the recorder drops whatever is still in its queue (`Recorder._async_close`), though it finishes the task it is running. Recording the marker before then could leave the 1.4.x rows in place under a marker that says they were rebuilt. If a read fails or the import is refused, the update logs `Could not update the Watercare statistics`. If the update is cancelled during step 4 (an unload or a shutdown), the queued clear and import are left to the recorder. In every case the marker stays unset and the next update checks and rebuilds again. A partly imported history never holds more than the bills Watercare returns, so that check lets the rebuild run. `tests/test_init.py` covers both failures and the full upgrade from a 1.4.x entry. The log shows a warning before and after the rebuild. It only touches the four `watercare:*` statistics and only runs after Watercare has returned at least one valid bill.

A new config entry also has no marker, so its first statistics update clears and re-imports these statistics too. That is harmless: the result is the same rows.

### Start-up and the recorder

Home Assistant's recorder writes statistics from a queue on its own thread, and it does not start working through that queue until Home Assistant has started. Home Assistant in turn does not finish starting while a config entry set up during start-up is still setting up. So an integration must never wait for the recorder during setup.

1.5.0 broke this rule: it rebuilt inside the first poll of setup and waited (up to 5 minutes) for the recorder to confirm the clear. On a restart with the rebuild pending, setup and start-up waited for each other until Home Assistant's 5-minute start-up timeout cancelled the setup, after the clear was queued and before the import was. Start-up was held all that time, and the statistics were left empty. A reload rebuilt them, because the marker had not been recorded. A normal poll was affected less: it never waited for the queue, but it read the stored statistics during setup, so a slow database could hold start-up for as long as the read took.

From 1.5.1:

- A poll only fetches the bills and the account. The statistics update, the one-off rebuild included, runs afterwards as a background task of the config entry, and only once Home Assistant has started (`async_at_started`). Setup never waits for it, and unloading the entry or stopping Home Assistant cancels it. A poll still fetching bills when the entry unloads starts no update when it finishes.
- One update runs at a time. A poll that finishes while an update is still running leaves its bills for that update to take next.
- Before every read of the stored statistics, an update waits for the recorder's queue (below).
- The rebuild queues the clear and the import back to back (above), so a cancellation can only arrive before the clear or after both are queued. One that arrives before the marker leaves the rebuild to run, and the next update rebuilds again, to the same rows.

`tests/test_startup.py` holds the recorder's thread, as start-up does, and checks that setup finishes at once and the statistics are rebuilt afterwards, including when the task is cancelled the moment the clear is queued (the first five tests there fail on 1.5.0). It also checks that no update runs before Home Assistant has started or after the entry unloads, and that the marker waits for the recorder.

#### What the wait before a read guarantees

Reads go straight to the database, past the recorder's queue. Without a wait, a poll that follows a rebuild while the recorder is busy could read the 1.4.x rows and continue their sums. So before every read an update awaits the recorder's `async_block_till_done`, as Home Assistant's history and logbook do before theirs. Exactly what that guarantees:

- If anything is queued, it queues a task of its own behind it and returns once the recorder thread reaches that task, so every write that was waiting in the queue has run.
- If the queue is empty, it returns at once, even while the recorder thread is still running the last task it took off the queue.

So a read never misses a write that was still waiting in the queue, but it can miss one: the last one queued, while the recorder is still committing it. That is harmless because of one rule: **a statistic with no stored rows is always imported in full, from zero.**

- After a rebuild, the write a read can miss is the import of a statistic the rebuild has just cleared. The read finds that statistic empty, so from the same bills the next import queues the same rows for it again, and the recorder, which keeps one row per statistic and hour, replaces the identical rows with them.
- After a normal import, the write a read can miss continues its statistic from that statistic's newest stored row, which the read still sees, so the next import plans the same days again from the same row.

`test_a_statistic_with_no_stored_rows_is_reimported_with_the_same_rows` in `tests/test_statistics_recorder.py` pins the rule. The same wait comes before the marker (step 4 above), where it means the recorder has taken the whole rebuild off its queue.

### When the rebuild is skipped

If the stored history holds bills Watercare no longer returns (it starts earlier than the oldest returned bill, or its running total is larger than the returned bills add up to by the same day), the rebuild would delete history that cannot be recreated. The integration then keeps the stored rows, adds new days after them in the new format, records the marker and raises a repair notice, **Watercare statistics were not rebuilt**.

To rebuild by hand in that case, after exporting the statistics (see [migration.md](migration.md)):

1. delete the four statistics under **Developer tools → Statistics**;
2. delete the Watercare integration entry and add it again.

The new entry's first poll imports everything Watercare returns.
