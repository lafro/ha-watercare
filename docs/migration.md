# Upgrading from 1.4.x to 1.5.0

1.5.0 rebuilds the four `watercare:*` statistics once, in a new format (daily rows, each year at its own prices; see [statistics.md](statistics.md)). This page is the operator runbook: what to do before, how to check the result, and how to roll back.

The entities, their recorded history and the Energy dashboard configuration are kept: the domain, the entity unique ids and the statistic ids are unchanged.

## Before upgrading

1. **Make a full Home Assistant backup** that includes the database.
2. **Export the four statistics.** Home Assistant has no button for this. Send this command to the WebSocket API (for example from a short script that uses a long-lived access token) and save the result as JSON:

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

   Also save the metadata from `{"type": "recorder/get_statistics_metadata", "statistic_ids": [...the same four...]}`. Keep the export with the backup; it is the quick rollback.

## Upgrading

1. If the integration came from a different HACS repository, remove that custom repository in HACS first, then add `https://github.com/lafro/ha-watercare` and download the new version. Otherwise update as usual.
2. Restart Home Assistant.
3. The config entry migrates (1.1 to 1.2): flat 1.4.x prices that match a published year are dropped in favour of the published table; others are kept as the current year's prices. The log says which.
4. On the first poll the log shows `Rebuilding the Watercare statistics in the 1.5.0 format …` and then `Watercare statistics rebuilt: …`.

## Checking the result

- **Settings → Dashboards → Energy → Water** shows daily bars instead of one bar per bill.
- **Developer tools → Statistics** shows no issues for the four statistics.
- The total of the four statistics over a bill's dates matches the bill: pick a bill inside one financial year and compare with [tariffs.md](tariffs.md#checking-against-a-bill).
- The **Watercare** device's diagnostics show `statistics_status: rebuilt` (or `current` after a restart) and `statistics_version: 2`.
- If a repair notice says the statistics were **not** rebuilt, read [statistics.md](statistics.md#when-the-rebuild-is-skipped).

## Rolling back

Choose one:

- **Restore the backup** taken before the upgrade, then install the previous release from HACS. This restores everything, including the old statistics.
- **Re-import the export:** reinstall the previous release, clear the four statistics (`{"type": "recorder/clear_statistics", "statistic_ids": [...]}`), then import each exported series with `recorder/import_statistics`, passing its saved metadata and rows. The previous release then recomputes its own format on its next poll.

Rolling back to 1.4.x also brings back its flat-price costs.
