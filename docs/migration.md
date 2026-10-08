# Upgrading from 1.4.x to 1.5.0

1.5.0 rebuilds the four `watercare:*` statistics once, in a new format (daily rows, each bill at the prices of the year it starts in; see [statistics.md](statistics.md)). This page is the operator runbook: what to do before, how to check the result, and how to roll back.

The entities, their recorded history and the Energy dashboard configuration are kept: the domain, the entity unique ids and the statistic ids are unchanged.

## Before upgrading

1. **Make a full Home Assistant backup** that includes the database.
2. **Export the four statistics.** Home Assistant has no button for this. Send this command to the WebSocket API (for example from a short script that uses a long-lived access token) and save the response as `export.json`:

   ```json
   {
     "type": "recorder/statistics_during_period",
     "start_time": "2000-01-01T00:00:00Z",
     "statistic_ids": [
       "watercare:water_consumption",
       "watercare:water_cost",
       "watercare:consumption_cost",
       "watercare:wastewater_cost"
     ],
     "period": "hour",
     "types": ["state", "sum"]
   }
   ```

   Also save the response to `{"type": "recorder/get_statistics_metadata", "statistic_ids": [...the same four...]}` as `metadata.json`. Keep both with the backup; together they are the quick rollback.
3. **Pre-flight (read-only): will the rebuild run?** 1.5.0 rebuilds only if that loses nothing (see [statistics.md](statistics.md#when-the-rebuild-is-skipped)). Check `watercare:water_consumption` in `export.json`:
   - it holds **one row per bill**, each starting at an Auckland midnight (`start` is in epoch milliseconds: 12:00 UTC in winter, 11:00 UTC in summer), at the end date of its bill, with a `sum` that only rises;
   - its **first row** is at the end date of the oldest bill the Watercare app still shows, and its **last `sum`** equals the total usage of the bills the app shows, in litres.

   If the rows start earlier than that oldest bill, or the last `sum` is larger, 1.5.0 will **skip** the rebuild, keep the stored rows (one per bill) and only add new days as daily rows, with a repair notice. Daily history for the past then needs the manual rebuild in [statistics.md](statistics.md#when-the-rebuild-is-skipped). Decide which you want before upgrading. Rows that are not at bill ends (for example from an older release or a hand edit) are a reason to look closer before upgrading.
4. **Rehearse the rollback once** on a copy restored from the backup (a test instance, never production): clear the four statistics there, re-import them as in [Rolling back](#rolling-back), then export again and compare with `export.json`. They must match.

## Upgrading

1. If the integration came from a different HACS repository, remove that custom repository in HACS first, then add `https://github.com/lafro/ha-watercare` and download the new version. Otherwise update as usual.
2. Restart Home Assistant.
3. The config entry migrates (1.1 to 1.2): flat 1.4.x prices that match a published year are dropped in favour of the published table; others are kept as the current year's prices. The log says which.
4. On the first poll the log shows `Rebuilding the Watercare statistics in the 1.5.0 format …` and then `Watercare statistics rebuilt: …`. If the clear or the import fails, the poll fails and the next attempt rebuilds again (the entry only records `statistics_version: 2` once the import is queued).

## Checking the result

- **Settings → Dashboards → Energy → Water** shows daily bars instead of one bar per bill.
- **Developer tools → Statistics** shows no issues for the four statistics.
- The total of the four statistics over a bill's dates matches the bill: see [tariffs.md](tariffs.md#checking-against-a-bill). A bill that spans 1 July is charged at the prices of the year it starts in.
- The **Watercare** device's diagnostics show `statistics_status: rebuilt` (or `current` after a restart) and `statistics_version: 2`.
- If a repair notice says the statistics were **not** rebuilt, read [statistics.md](statistics.md#when-the-rebuild-is-skipped).

## Rolling back

Choose one:

- **Restore the backup** taken before the upgrade, then install the previous release from HACS. This restores everything, including the old statistics.
- **Re-import the export:**
  1. Reinstall the previous release from HACS and restart.
  2. Convert the export into import commands with [`scripts/statistics_rollback.py`](../scripts/statistics_rollback.py) (Python 3.11 or newer, standard library only):

     ```bash
     python3 statistics_rollback.py export.json metadata.json > import.json
     ```

  3. Clear the four statistics: `{"type": "recorder/clear_statistics", "statistic_ids": [...the same four...]}`. Wait for its result.
  4. Send each message in `import.json` (a list, one `recorder/import_statistics` command per statistic) with an `id` added. Each must return `success: true`.
  5. Export again and compare with `export.json`.

  The previous release then recomputes its own format on its next poll; the re-import keeps any history older than the bills Watercare still returns.

Rolling back to 1.4.x also brings back its flat-price costs.

### Why the export needs converting

Home Assistant 2026.10 rejects the export as it is: `recorder/import_statistics` wants `unit_of_measurement` where `recorder/get_statistics_metadata` returns `statistics_unit_of_measurement` (with `display_unit_of_measurement` and `has_mean` besides), an ISO-8601 `start` where `recorder/statistics_during_period` returns epoch milliseconds (plus an `end`), and no `null` values, where rows written by 1.4.x have `state: null`. The script converts `start` to ISO-8601 UTC, drops `end` and null values, renames the unit, drops `display_unit_of_measurement` and `has_mean`, and keeps `mean_type`, `unit_class`, `has_sum`, `name`, `source` and `statistic_id`. `tests/test_statistics_rollback.py` runs this whole round trip (export, clear, convert, import, compare) through Home Assistant's own WebSocket API.
