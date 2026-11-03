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
- While Watercare reports a smart meter, whose bills have never been seen, such a bill is skipped with a warning instead of moved, and so is one that starts before the previous bill ends (starting on its end day is allowed) or has no water use figure. Moving a bill with its whole volume suits a mechanical meter's re-dated bill, but could count a smart meter's days twice.
- Watercare corrects an estimated reading through the next bill, not by revising the old one, so nothing needs rewriting.

## Costs at the tariff in force

Watercare charges each bill at the prices in force when its billing period starts, so the integration prices every day of a bill with the financial-year tariff of the bill's **start date** ([tariffs.md](tariffs.md#how-a-bill-is-priced)). A bill that spans 1 July keeps the earlier year's prices on all of its days, including the July ones, and a bill's daily cost rows add up to what Watercare charged for it. The **Last bill cost** sensor uses the same rows.

If no tariff is known for the year a bill starts in (a new financial year the release does not yet carry and the user has not entered), the cost statistics stop before that bill's first day, and resume from that day once a tariff exists. The consumption statistic is unaffected. A repair notice asks for the prices of the earliest year that blocks the costs, or of the current year if it has none yet. Days already recorded are never repriced, so the options and the repair form never pre-fill prices for a year the integration does not know.

## The first import

A new config entry's first statistics update (after Home Assistant has started; see below) is a normal poll. A statistic with no stored rows is imported in full, from the first bill Watercare returns, at the entry's options; one with stored rows continues after its newest row. So removing Watercare and adding it again continues its statistics: the new entry adds only the days after the newest stored one. New entries record `statistics_version: 2` in their data, which 1.5.x reads as "already in the new format", so going back to 1.5.1 continues the statistics too, rather than rebuilding them.

Entries created by 1.4.x or earlier (config entry minor version 1) are no longer upgraded: setup refuses them with a migration error and logs how to go on (install 1.5.1 first, or remove Watercare, delete its four statistics and add it again). 1.5.x rebuilt such an entry's statistics once, from every bill; 1.6.0 removed that rebuild.

### Starting again

To import the whole history afresh, at the current options (after changing the wastewater ratio, say):

1. delete the four statistics under **Settings → Tools → Statistics**;
2. reload Watercare.

Its next statistics update finds them empty and imports every bill Watercare returns. To switch accounts, remove Watercare instead of reloading it, delete the statistics, and add it again with the other account ([Switching accounts](../README.md#switching-accounts)).

### Start-up and the recorder

Home Assistant's recorder writes statistics from a queue on its own thread, and it does not start working through that queue until Home Assistant has started. Home Assistant in turn does not finish starting while a config entry set up during start-up is still setting up. So an integration must never wait for the recorder during setup.

1.5.0 broke this rule: it rebuilt the statistics inside the first poll of setup and waited (up to 5 minutes) for the recorder to confirm, so on a restart with the rebuild pending, setup and start-up waited for each other until Home Assistant's 5-minute start-up timeout cancelled the setup. A normal poll was affected less: it never waited for the queue, but it read the stored statistics during setup, so a slow database could hold start-up for as long as the read took. The rebuild is gone, but a first import still writes every bill Watercare returns, so the rules below stay.

From 1.5.1:

- A poll only fetches the bills and the account. The statistics update runs afterwards as a background task of the config entry, and only once Home Assistant has started (`async_at_started`). Setup never waits for it, and unloading the entry or stopping Home Assistant cancels it. A poll still fetching bills when the entry unloads starts no update when it finishes, and an entry unloaded (or reloaded) before Home Assistant has started stops waiting for the start, so the unloaded entry's statistics never start.
- One update runs at a time. A poll that finishes while an update is still running leaves its bills for that update to take next.
- Before every read of the stored statistics, an update waits for the recorder's queue (below).

`tests/test_startup.py` holds the recorder's thread, as start-up does, and checks that setup finishes at once and the first import runs afterwards. It also checks that no update runs before Home Assistant has started or after the entry unloads (an entry unloaded or reloaded during start-up included), that a first import a shutdown drops runs again on the next start, and that an import the recorder retries or drops is completed later.

#### What the wait before a read guarantees

Reads go straight to the database, past the recorder's queue, so before every read an update awaits the recorder's `async_block_till_done`, as Home Assistant's history and logbook do before theirs, and plans from rows already written rather than ones still queued. Exactly what that guarantees:

- If anything is queued, it queues a task of its own behind it and returns once the recorder thread reaches that task, so every write that was waiting in the queue has run.
- If the queue is empty, it returns at once, even while the recorder thread is still running the last task it took off the queue.

So a read never misses a write that was still waiting in the queue, but it can miss one: the last one queued, while the recorder is still committing it. That is harmless because of one rule: **a statistic with no stored rows is always imported in full, from zero.**

- After a first import, the write a read can miss is that import. The read finds the statistic empty, so from the same bills the next import queues the same rows for it again, and the recorder, which keeps one row per statistic and hour, replaces the identical rows with them.
- After a normal import, the write a read can miss continues its statistic from that statistic's newest stored row, which the read still sees, so the next import plans the same days again from the same row.

`test_a_statistic_with_no_stored_rows_is_reimported_with_the_same_rows` in `tests/test_statistics_recorder.py` pins the rule.

Taken off the queue is not always written, and the same rule covers that too. An import that meets a database error the recorder retries (a MySQL or MariaDB lock timeout or deadlock) queues itself again, behind the wait's own task; one that meets any other error is logged and dropped. A retried import writes its rows shortly afterwards. A dropped one leaves the statistic at its newest written row, or empty, so the next update plans the same days again. `test_an_import_the_recorder_retries_or_drops_is_completed_later` in `tests/test_startup.py` covers both.
