# Watercare for Home Assistant

An unofficial Home Assistant integration for Watercare (Auckland) water accounts. It signs in with your Watercare credentials, fetches your usage, and calculates costs from the rates on your bill.

Forked from [brunsy/ha-watercare](https://github.com/brunsy/ha-watercare).

> [!IMPORTANT]
> This project is not affiliated with, endorsed by or supported by Watercare Services Limited. It uses the endpoints of Watercare's customer app, which may change without notice.

## Installation

### Method 1: HACS

[![Open your Home Assistant instance and open this repository in HACS.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=lafro&repository=ha-watercare&category=integration)

Or add it in HACS yourself: open the menu (⋮), select **Custom repositories**, add `https://github.com/lafro/ha-watercare` with type **Integration**, then download **Watercare**.

### Method 2: Manually

Download the latest release and copy the `custom_components/watercare` folder into your `config/custom_components` folder.

After either method, restart Home Assistant, then add the integration under **Settings → Devices & services** and sign in with your Watercare account.

Requires Home Assistant 2026.3 or newer.

## Configuration

This integration supports mechanical (monthly-billed) meters only — the ones read roughly monthly, with readings on your bill marked *Estimate* or *Actual*. There is no data-source choice to make; it's fixed to the monthly billing-period source.

The rate fields correspond to the *Charge details* section of a Watercare bill. Costs are calculated locally from these rates — Watercare doesn't provide cost data — so they're only as accurate as the rates you enter. Prices change on 1 July each year; the rates can be updated in the integration's options at any time.

### Smart meters

Smart-meter support (daily, monthly and half-hourly usage) was removed. It was inherited from the original project, untested, and had known defects, and the maintainer doesn't have a smart meter to test against. The architecture keeps room to re-add it — the relevant API endpoints and statistic names are still documented in the code — so it can come back without a rewrite. If you have a smart meter and want to help test that path, contributions are welcome.

## Entities

| Entity | |
|---|---|
| Last bill usage | Litres used in the most recent billing period. Mechanical meters are billed in whole kilolitres, so this changes in 1,000 L steps. |
| Last bill cost | Cost of that period, from the configured rates. |
| Daily average | Average daily use over that period. |
| Last billing period end | When the most recent billing period ended. |
| Payment due | When payment is due. |
| Reading type | Whether the reading was an estimate or an actual read. |
| Household efficiency band | Watercare's usage band for the household size. |
| Account balance | Balance on the account. |
| Amount due | Amount owing. |
| Overdue amount | Amount past its due date. Disabled by default. |

Watercare only publishes completed billing periods, so these describe the last issued
bill rather than usage accruing now.

**Last billing period end**, **Payment due**, **Reading type**, **Household efficiency
band**, **Account balance**, **Amount due** and **Overdue amount** are diagnostic
entities.

## Energy dashboard

The integration imports long-term statistics. In **Settings → Dashboards → Energy**, add *Watercare Water Consumption* as a water source, with *Watercare Total Cost* as the entity tracking its cost.

## Licence

MIT.
