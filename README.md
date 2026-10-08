# Watercare for Home Assistant

An unofficial Home Assistant integration for Watercare (Auckland) water accounts. It signs in with your Watercare login, reads your completed bills, and records daily water use and cost for Home Assistant's Energy dashboard.

[![Open your Home Assistant instance and open this repository in HACS.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=lafro&repository=ha-watercare&category=integration)

> [!IMPORTANT]
> This project is not affiliated with, endorsed by or supported by Watercare Services Limited. It uses the endpoints of Watercare's customer app, which may change without notice.

Based on the MIT-licensed [brunsy/ha-watercare](https://github.com/brunsy/ha-watercare); see [NOTICE](NOTICE).

## What it provides

- Water use for the Energy dashboard's **Water** view, one value per day: each bill is spread evenly over the days it covers.
- Water cost for each day at the price Watercare charged that day. Watercare's published residential prices for every financial year since 2018/19 are built in ([sources](docs/tariffs.md)).
- Sensors for the last bill (usage, cost, daily average, reading type, efficiency band) and the account (balance, amount due, payment due date, meter number).
- A repair notice each July if the integration does not yet know the new year's prices.
- Re-authentication, reconfiguration and privacy-safe diagnostics.

## Requirements

- Home Assistant 2026.10.0 or newer.
- A Watercare account with a mechanical (manually read, about monthly) water meter. Smart meters are not supported yet; see [Known limitations](#known-limitations).

## Installation

1. In HACS, open the menu (⋮), choose **Custom repositories**, add `https://github.com/lafro/ha-watercare` with type **Integration**, then download **Watercare**. Or use the button above.
2. Restart Home Assistant.
3. Go to **Settings → Devices & services → Add integration → Watercare**.
4. Sign in with the email address and password you use for the Watercare app or My Account.

The integration is not in the HACS default store; add it as a custom repository as above.

Manual installation: copy `custom_components/watercare` from the latest release into your `config/custom_components` folder and restart.

## Configuration

### Setup

| Field | Meaning |
|---|---|
| Email | Your Watercare login email. |
| Password | Your Watercare password. Home Assistant stores it, with a renewable sign-in token, in its config entry and sends it only to Watercare. |

Each Watercare account can be added once.

### Options

Open **Settings → Devices & services → Watercare → Configure**.

| Option | Meaning |
|---|---|
| Wastewater ratio | Share of metered water billed as wastewater. Most homes are 0.785 (78.5%), apartments 0.95. Your bill shows it as, for example, "@78.50%". |
| Water rate, Wastewater rate, Wastewater fixed charge | Prices for the **current** financial year, GST-inclusive. They are pre-filled from Watercare's published prices. Change them only if your bill shows different prices; earlier years always use the published prices. |

Use **Reconfigure** on the integration to change the email or password for the same account.

## Entities

One service device, **Watercare**, represents the account.

| Entity | Meaning |
|---|---|
| Last bill usage | Litres used in the most recent billing period. Mechanical meters are billed in whole kilolitres. |
| Last bill cost | Cost of that period at the prices in force on each of its days. Unknown while a price is missing. |
| Daily average | Watercare's average daily use for that period. |
| Last billing period end | When the most recent billing period ended (diagnostic). |
| Payment due | When payment is due (diagnostic). |
| Reading type | Estimate or Actual (diagnostic). |
| Household efficiency band | Watercare's usage band for the household (diagnostic). |
| Account balance, Amount due | From the account (diagnostic). |
| Overdue amount | From the account (diagnostic, disabled by default). |
| Meter number | The meter on the account (diagnostic). |

Watercare publishes only completed billing periods, so these describe the last issued bill, not use accruing now. None of the sensors has a state class; the Energy dashboard uses the statistics below.

## Energy dashboard

The integration keeps four long-term statistics:

| Statistic | Contents |
|---|---|
| Watercare Water Consumption (`watercare:water_consumption`) | Litres per day. |
| Watercare Total Cost (`watercare:water_cost`) | Water, wastewater and the fixed charge, NZD per day. |
| Watercare Consumption Cost (`watercare:consumption_cost`) | Water supply charge only. |
| Watercare Wastewater Cost (`watercare:wastewater_cost`) | Wastewater volume charge only. |

In **Settings → Dashboards → Energy → Water consumption**, add *Watercare Water Consumption* and choose *Watercare Total Cost* as the statistic that tracks its cost.

How the numbers are built (details in [docs/statistics.md](docs/statistics.md)):

- Each bill's volume is spread evenly over the days it covers, so day, week and month views show use where it happened rather than one spike when the bill arrives.
- Each day is priced with that day's financial-year prices. A bill that spans 1 July is split between the two years by day.
- A poll only adds days after the stored history and never rewrites it.

## Data updates

The integration polls Watercare every 12 hours. A new bill appears in Home Assistant at the next poll after Watercare issues it. Watercare corrects an estimated reading through the next bill rather than by revising the old one.

## Prices and 1 July

Watercare changes its prices every 1 July and has no API for them. Each release carries the published prices up to its own date. If the integration does not know the current year's prices, it:

- keeps recording water use;
- pauses the cost statistics from 1 July, rather than guessing;
- shows a repair notice under **Settings → System → Repairs** that asks for the prices from your bill.

Entering the prices there (or in the options), or updating to a release that includes them, resumes the cost statistics from 1 July.

## Upgrading from 1.4.x

Version 1.5.0 changes how the statistics are stored (daily rows instead of one row per bill, and each year at its own prices). On its first poll it clears the four `watercare:*` statistics and imports the whole history again in the new format, once. **Back up Home Assistant and export these statistics first.** The steps, checks and rollback are in [docs/migration.md](docs/migration.md). The entities, their history and the Energy dashboard configuration are kept.

The flat prices entered in 1.4.x are replaced by the published price table. If they matched a published year, nothing else changes; if they did not, they are kept as the current year's prices.

## Examples

Notify when a new bill arrives:

```yaml
automation:
  - alias: "Watercare: new bill"
    triggers:
      - trigger: state
        entity_id: sensor.watercare_last_billing_period_end
    conditions:
      - condition: template
        value_template: "{{ trigger.from_state.state not in ['unknown', 'unavailable'] }}"
    actions:
      - action: notify.notify
        data:
          message: >
            New water bill: {{ states('sensor.watercare_last_bill_usage') | int // 1000 }} kL,
            NZD {{ states('sensor.watercare_last_bill_cost') }}.
```

Remind three days before payment is due:

```yaml
automation:
  - alias: "Watercare: payment reminder"
    triggers:
      - trigger: time
        at: "09:00:00"
    conditions:
      - condition: template
        value_template: >
          {{ states('sensor.watercare_amount_due') | float(0) > 0 and
             (as_datetime(states('sensor.watercare_payment_due')) - now()).days == 3 }}
    actions:
      - action: notify.notify
        data:
          message: "Watercare bill of NZD {{ states('sensor.watercare_amount_due') }} is due in 3 days."
```

## Use cases

- Track household water use and cost by day, week, month and year next to electricity in the Energy dashboard.
- Compare years at the prices that actually applied.
- Get told when a bill arrives, when a reading was estimated, or when payment is due.

## Known limitations

- Mechanical meters only. Smart-meter data uses different endpoints whose formats have not been verified; support needs a real sample first.
- Daily values are an even split of each bill, not measured daily use. A mechanical meter is read about monthly in whole kilolitres.
- Costs are calculated from published residential prices; Watercare's API provides no dollar figures. Infrastructure growth charges, trade waste, late fees and credits are not included. Business accounts use different prices and are not supported.
- Watercare's own split of a bill across 1 July may differ from the even per-day split by a few cents.
- History before 1 July 2018 is priced at the 2018/19 prices, the earliest year in the table.

## Troubleshooting

- **Setup says it cannot connect:** Watercare may be down for maintenance. Try again later.
- **Re-authentication requested:** Watercare rejected the stored password. Enter the current one.
- **Costs stopped on 1 July:** the integration does not know the new prices yet. Fix the repair notice or update the integration.
- **A "statistics were not rebuilt" repair notice:** see [docs/statistics.md](docs/statistics.md#when-the-rebuild-is-skipped).
- For a reproducible problem, use the integration's ⋮ menu to **Enable debug logging**, reproduce it, then **Disable debug logging** to download the log. Download diagnostics from the same menu. Logs and diagnostics leave out credentials, account and meter numbers and usage, but review them before sharing and follow the issue form's privacy warning.

## Removal

1. Remove the Watercare sources from **Settings → Dashboards → Energy**.
2. Delete **Watercare** under **Settings → Devices & services**.
3. Remove it in HACS and restart Home Assistant.

The statistics stay in the database after removal. Delete them under **Developer tools → Statistics** if you no longer want the history.

## Security, support and licence

Use [GitHub Issues](https://github.com/lafro/ha-watercare/issues) for support and [private vulnerability reporting](SECURITY.md) for security problems. Development notes are in [CONTRIBUTING.md](CONTRIBUTING.md) and [docs/](docs/).

Licensed under the MIT License. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
