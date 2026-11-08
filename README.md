# Watercare for Home Assistant

An unofficial Home Assistant integration for Watercare (Auckland) water accounts. It signs in with your Watercare login, reads your completed bills, and records daily water use and cost for Home Assistant's Energy dashboard.

[![Open your Home Assistant instance and open this repository in the Marketplace, or in HACS before Home Assistant 2026.11.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=lafro&repository=ha-watercare&category=integration)

> [!IMPORTANT]
> This project is not affiliated with, endorsed by or supported by Watercare Services Limited. It uses the endpoints of Watercare's customer app, which may change without notice.

Based on the MIT-licensed [brunsy/ha-watercare](https://github.com/brunsy/ha-watercare); see [NOTICE](NOTICE).

## What it provides

- Daily water use and cost in the Energy dashboard's **Water** view, from your bills, priced with Watercare's published residential prices for every year since 2018/19 ([sources](docs/tariffs.md)).
- Sensors for your latest bill (usage, calculated cost, the amount Watercare billed, average daily usage) and your account (balance, amount due, due date).
- Repair notices when the integration needs a new year's prices, when Watercare reports a smart meter, and when Watercare asks for a newer version of its app.

## Requirements

- Home Assistant 2026.10.0 or newer. On 2026.11 or newer you also need a GitHub account, because Watercare isn't in the Marketplace catalog.
- A Watercare account with a mechanical (manually read) water meter. Smart meters are not supported; see [Known limitations](#known-limitations).

## Installation

1. Install Watercare, or use the button above:
   - **Home Assistant 2026.11 or newer:** go to **Settings → Marketplace → Add from link**, enter `https://github.com/lafro/ha-watercare` and select **Add**, connecting GitHub when asked. Then open **Watercare** and select **Install**. No restart is normally needed.
   - **Home Assistant 2026.10:** in HACS, open the menu (⋮), choose **Custom repositories**, add `https://github.com/lafro/ha-watercare` with type **Integration**, download **Watercare**, and restart Home Assistant.
2. Go to **Settings → Devices & services → Add integration → Watercare**.
3. Sign in with the email address and password you use for the Watercare app or My Account. Home Assistant keeps the sign-in, with a renewable token, and sends it only to Watercare.

The first update imports every bill Watercare still lists. Upgrading from 1.4.x? Install 1.5.1 first (in the Marketplace: **Watercare → ⋮ → Install another version**), then this version.

Beta versions are GitHub pre-releases, offered only if you opt in: in the Marketplace, **Watercare → ⋮ → Include pre-releases**; in HACS on 2026.10, turn on beta versions for Watercare.

## Options

**Settings → Devices & services → Watercare → Configure**:

| Option | Meaning |
|---|---|
| Wastewater ratio | The share of your water billed as wastewater: 0.785, Watercare's standard for homes, unless your bill shows another percentage (apartments are 0.95). Days already recorded keep their cost; to reprice them, change it, then [start again](docs/statistics.md#starting-again). |
| Water rate, Wastewater rate, Wastewater fixed charge | This financial year's prices, including GST. They are filled in from Watercare's published prices when the integration has them; otherwise enter all three from Watercare's price list or your bill, or leave them empty. Earlier years use the published prices, or prices you entered for them, and a change never reprices days that are already recorded. |

**Reconfigure** changes the email or password for the same account.

### Switching accounts

Only one Watercare account can be added. To switch, remove Watercare, delete its four statistics under **Settings → Tools → Statistics**, and add it again with the other account. Removing Watercare keeps the statistics, and adding it again continues them: if you keep them, the new account's bills are added only after the newest recorded day.

## Entities

One service device, **Watercare**, represents the account.

| Entity | Meaning |
|---|---|
| Last bill usage | Litres used in the latest billing period; a mechanical meter reads whole kilolitres, so it moves in 1,000 L steps. |
| Last bill cost | That period's cost at the prices in force when it started. Unknown while a price is missing. |
| Last bill amount | Your latest bill's total, as Watercare issued it. Its attributes are the bill's date and, when it can be matched, the billing period it charges for. |
| Average daily usage | Watercare's average daily use for that period, in litres. |
| Last billing period end | When the latest billing period ended (diagnostic). |
| Due date | When payment is due (diagnostic). |
| Reading type | Estimate or Actual (diagnostic). |
| Household efficiency band | Watercare's usage band for the household (diagnostic). |
| Account balance, Amount due | From the account (diagnostic). |
| Overdue amount | From the account (diagnostic, disabled by default). |
| Meter number | The meter on the account (diagnostic). |

Watercare publishes only completed bills, so these sensors describe your latest bill, not use so far. For the Energy dashboard, use the statistics below.

## Energy dashboard

| Statistic | Contents |
|---|---|
| Watercare Water Consumption (`watercare:water_consumption`) | Litres per day. |
| Watercare Total Cost (`watercare:water_cost`) | Water, wastewater and the fixed charge, NZD per day. |
| Watercare Consumption Cost (`watercare:consumption_cost`) | Water supply charge only. |
| Watercare Wastewater Cost (`watercare:wastewater_cost`) | Wastewater volume charge only. |

In **Settings → Dashboards → Energy → Water consumption**, add *Watercare Water Consumption* and choose *Watercare Total Cost* as its cost.

- The first update imports every completed bill Watercare has, which can go back several years.
- Each bill's water use and cost are spread evenly over the days it covers.
- Each bill is priced at the prices in force when its billing period started, as Watercare charges it, so a bill that spans 1 July keeps the earlier year's prices.
- Later updates only add new days. Days already recorded are not changed.

Watercare's app shows one bar per bill, from one meter reading to the next, so its "months" are not calendar months. Its yearly view shows calendar years, like the Energy dashboard.

## Data updates

The integration checks Watercare every 12 hours. A bill's water use and cost appear at the first check after Watercare publishes it, and no earlier than the day after its billing period ends.

## Prices and 1 July

Watercare changes its prices every 1 July and has no API for them, so each release carries the published prices up to its date. If the integration does not have a year's prices, it keeps recording water use, pauses the cost statistics from the first bill that starts in that year, and shows a repair notice that asks for the new prices, from Watercare's published price list or your bill. Entering them there or in the options, or updating the integration, resumes the costs from where they paused.

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
            NZD {{ states('sensor.watercare_last_bill_amount') }}.
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
             (as_datetime(states('sensor.watercare_due_date')) - now()).days == 3 }}
    actions:
      - action: notify.notify
        data:
          message: "Watercare bill of NZD {{ states('sensor.watercare_amount_due') }} is due in 3 days."
```

## Known limitations

- **Mechanical meters only.** If Watercare reports a smart meter at your property, or that your meter was replaced with one, a **Watercare smart meters are not supported yet** repair notice appears. The integration reads only billing periods, not a smart meter's own readings; water use already recorded is kept. While Watercare reports a smart meter, a billing period that overlaps days already recorded or the period before it, or has no water use figure, is skipped with a warning in the log. If Watercare has no bills from a manually read meter, setup keeps retrying. The notice goes away by itself if Watercare reports a manually read meter again.
- Daily values are each bill spread evenly over its days, not measured daily use.
- Costs are worked out from Watercare's published residential prices, so they leave out infrastructure growth charges, trade waste, late fees and credits. **Last bill amount** shows what Watercare actually billed. Business accounts are not supported.
- Bills that start before 1 July 2018 are priced at the 2018/19 prices, the earliest the integration has.
- One Watercare property per Home Assistant instance. If your login has several, the integration uses the first one Watercare returns when you add it (its meter is on the Meter number sensor) and stays with it while Watercare lists it; you can't choose another.

## Troubleshooting

- **Setup says Watercare is down for maintenance:** try again later. After setup, the log says so, the sensors show as unavailable, and the next update, within 12 hours, tries again.
- **Setup says it cannot connect:** Watercare did not answer, answered with an error, or its sign-in page was unavailable. Try again later.
- **"Watercare asks for a newer version" repair notice:** Watercare's service has probably changed. Check the Marketplace (or HACS on 2026.10) for an update to Watercare, and open an issue if there is none. The notice goes away by itself once Watercare answers normally again.
- **Re-authentication requested:** Watercare rejected the stored password. Enter the current one. If you changed your Watercare email, use **Reconfigure** instead.
- **Costs stopped after 1 July:** the integration does not have the new year's prices yet. Fix the repair notice or update the integration.
- **Reporting a problem:** in the integration's ⋮ menu, **Enable debug logging**, reproduce the problem, then **Disable debug logging** to download the log. Download diagnostics from the same menu. Both leave out credentials, account and meter numbers, usage and amounts, but check them before sharing.

## Removal

1. Remove the Watercare sources from **Settings → Dashboards → Energy**.
2. Delete **Watercare** under **Settings → Devices & services**.
3. Uninstall it in the Marketplace (**Watercare → ⋮ → Uninstall**), or remove it in HACS on 2026.10. Then restart Home Assistant.

The statistics stay in the database. Delete them under **Settings → Tools → Statistics** if you no longer want the history.

## Security, support and licence

Use [GitHub Issues](https://github.com/lafro/ha-watercare/issues) for support and [private vulnerability reporting](SECURITY.md) for security problems. Development notes are in [CONTRIBUTING.md](CONTRIBUTING.md) and [docs/](docs/).

Licensed under the MIT License. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
