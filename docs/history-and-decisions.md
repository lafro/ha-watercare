# History and decisions

Where the code came from, what changed and why, and the problems already solved, as of 1.5.1. Tags are explained in [README.md](README.md#accuracy-tags). The detail behind each step is in the other pages; this one is the "why".

## Where this started

The code is based on [brunsy/ha-watercare](https://github.com/brunsy/ha-watercare) at commit `ef7e258`, which is still that repository's `main` ✅ (GitHub, 9 October 2026). The original ✅ (source at `ef7e258`, 9 October 2026):

- was version **1.1.0**, with `integration_type: "hub"` and `requirements: ["aiohttp", "pytz", "voluptuous"]`;
- had **no coordinator**: one 582-line `sensor.py` fetched, parsed, priced and imported statistics inside a single sensor, and exposed almost everything as attributes of that one entity;
- defaulted to the smart-meter `halfhourly` endpoint and a $310 annual line charge;
- offered four endpoints, `mechanicalmonthly` and three smart-meter ones (`dailywithstats`, `monthly`, `halfhourly`), none of the smart ones tested;
- consisted of `__init__.py`, `api.py`, `config_flow.py`, `const.py`, `manifest.json` and `sensor.py`.

The first change was a fix to refresh expired Watercare access tokens: the original stopped working once its token aged out, and that fix is why this project exists. ✅ (8 August 2026)

## 1.x up to 1.4.1: the fork

Releases up to 1.4.1 came from a GitHub fork of the original. That history is not carried into this repository ([CHANGELOG](../CHANGELOG.md)), so the steps below have no commits here. ✅ (8 August 2026)

1. **Brand images.** The app icon was rebuilt as clean vector art from a low-resolution image with a repeatable render pipeline, and the integration started serving its own brand images (Home Assistant 2026.3 and later). The pipeline lives outside this repository; `custom_components/watercare/brand/` holds its output.
2. **Modernisation.** A `DataUpdateCoordinator`; the monolithic sensor split into dedicated entities; credentials checked at setup; duplicate-account protection and a re-authentication flow; Home Assistant's shared aiohttp session; a device; `integration_type` changed from `hub` to `service`; friendly form labels and translations.
3. **Entity naming and account data.** Entities named for the period they describe ("Last bill …", because the API only returns completed bills); the balance, amount due and due date from the account record, which had been fetched at sign-in and thrown away; Watercare's own `numberOfDays`; a fix for the due date's timestamp format (no milliseconds); and the missing `unit_class="volume"` that hid the consumption statistic from the Energy dashboard's water picker.
4. **Mechanical meters only.** The untested smart-meter processing and the endpoint chooser were removed, keeping a seam to re-add them; a pytest suite; `state_class` dropped from the usage and cost sensors ([architecture.md](architecture.md#why-the-usage-and-cost-sensors-have-no-state-class)); hardening against malformed payloads; connection failures told apart from credential failures; a **Meter number** sensor and a service device; `pytz` replaced with `zoneinfo`; display precision fixed.
5. **CI and 1.4.1.** Lint CI on Python 3.14 with a pinned Ruff, and the 1.4.1 release.

## 1.5.0 and 1.5.1: this repository

Both were released on 8 October 2026. ✅ (GitHub releases)

- **1.5.0** is the first release from this standalone repository (not a GitHub fork), whose first commit imports 1.4.1's code with synthetic test data. The domain, entity unique ids and statistic ids are unchanged, so existing installations keep their entities, history and Energy dashboard configuration. It changed the statistics model (daily rows, each bill at its own year's prices, appended rather than recomputed, a one-off rebuild), replaced the flat price options with Watercare's published prices, and brought the project to the house standard: typed models, a reconfigure step, a stored refresh token, privacy-safe diagnostics and logs, strict typing, a 95% per-module branch-coverage gate, snapshot tests, actions pinned to commits, and a gated Release workflow. It also removed the smart-meter seam. Details: [CHANGELOG](../CHANGELOG.md), [statistics.md](statistics.md), [tariffs.md](tariffs.md).
- **1.5.1** stopped setup from waiting for the recorder. 1.5.0's rebuild held Home Assistant's start-up for 5 minutes and could leave the statistics empty; the statistics now run in the background after start-up. Details: [statistics.md](statistics.md#start-up-and-the-recorder).

## Decisions

The ones worth remembering, with their status in 1.5.1.

**Mechanical meters only; smart meters removed.** Only the mechanical path can be tested against a real account. The smart-meter paths were inherited, unverified, and had defects: wrong period naming, an assumed payload shape and a time-zone bug in the daily path. Shipping confident-looking untested code was judged worse than not shipping it, so a smart-meter user gets nothing rather than something that might be wrong. 1.4.x kept a seam to re-add them; 1.5.0 removed it, and re-adding starts from a real smart-meter response. Still in force. ✅

**No state class on the usage and cost sensors.** They hold per-bill totals, not counters, and a state class made Home Assistant compile corrupt duplicate statistics that shadowed the external ones in the Energy dashboard. This was the most valuable finding of the August 2026 review. Still in force. ✅

**Costs are calculated, from published prices.** Neither of Watercare's APIs exposes prices ✅ (August 2026). Up to 1.4.x the user entered one flat set of prices; from 1.5.0 each release carries Watercare's published prices for every financial year since 2018/19, with sources ([tariffs.md](tariffs.md)), and the user enters prices only for a year the release does not know yet. Still in force. ✅ (code)

**Watercare's web portal is not used for dollar totals.** The My Account portal's own API has an invoice history with real totals, but using it would add a second sign-in flow against an undocumented API to get a number the calculation already matched to the cent on the bills checked. Revisit only if calculated costs drift from the bills. Still in force. ✅ (decision)

**The meter number is a sensor, not the device's serial number.** It identifies a water meter, not the account the device represents. A review flagged it and the maintainer had questioned the "Serial number" label independently, so the device is a service with no serial number and the meter number has its own diagnostic sensor. From 1.5.0 it is no longer copied into the usage sensor's attributes. Still in force. ✅ (code)

**Reversed in 1.5.0: recomputing the cost history when a price changes.** Up to 1.4.x every poll recomputed every sum from zero at the one flat price in the options. That let a wrong price be corrected, but it put today's prices on past bills, and a shorter response from Watercare would have stepped the sums down. From 1.5.0 a poll only appends days after the stored history, each bill is priced at the prices in force when it started, and a price change never reprices stored days. ✅ (code; [statistics.md](statistics.md#anchoring-to-stored-history))

**Spread each bill over its days.** Before 1.5.0 the whole bill landed on the hour after its period ended, which showed nothing for weeks and then a spike, and booked most of a bill to the next month. A mechanical meter is read about monthly in whole kilolitres, so an even daily split adds no false precision. ✅ ([statistics.md](statistics.md#spreading-each-bill-over-its-days))

**Price a whole bill at the prices in force when it starts.** That is how Watercare charged the one bill spanning 1 July that has been checked; a split by day was about 3.7% too high. The rule is one function, `pricing_date`, so it can change if another bill shows otherwise. ⚠️ (one bill; [tariffs.md](tariffs.md#how-a-bill-is-priced))

**Pause costs rather than guess.** A year without known prices stops the cost statistics at its first bill and raises a repair notice; the forms never pre-fill an unknown year's prices, because a price saved by accident would resume the costs and stay in them. ✅ (code)

**One Watercare account per Home Assistant instance.** The statistic ids are shared, so a second account would overwrite the first one's history (earlier releases allowed it, and the two collided). ✅ (code)

**Never wait for the recorder in setup.** The recorder only works through its queue once Home Assistant has started, and start-up waits for config entries being set up. From 1.5.1 the statistics run in a background task after start-up. ✅ (code; [statistics.md](statistics.md#start-up-and-the-recorder))

**Releases are immutable; defects are fixed forward.** On the fork, early iteration produced several same-day releases, which were consolidated into one current release. From 1.5.0, tags and releases cannot be moved, replaced or deleted, and a defect gets a new patch version ([release.md](release.md)). ✅

**No pull requests or issues upstream without the maintainer's approval.** The maintainer declined to send changes to `brunsy/ha-watercare` for now. Still in force ([CLAUDE.md](../CLAUDE.md)). ✅

## Problems already solved

So that future work does not learn them again.

- **HACS compares version strings.** Releasing the same version again does not offer an update. Once a HACS record pointed its installed version at a tag that no longer existed and showed no information until a clean download of the current version and a restart fixed it. Every shippable change gets a new version ([release.md](release.md)). ✅ (August 2026)
- **No GitHub Actions on the fork at first.** Early releases were zipped by hand until Actions was enabled. From 1.5.0 there is no zip: HACS installs the integration from the release tag, and a test keeps `zip_release` out of `hacs.json`. ✅ (code)
- **The Python floor follows Home Assistant.** The test harness pins Home Assistant exactly, and Home Assistant 2026.10.0 needs Python 3.14.2 or later, so the tests do too. See [known-unknowns.md](known-unknowns.md#the-python-version-question). ✅ (package metadata, 9 October 2026)
- **Guardrails for agents.** Agent sessions cannot merge, tag, release, force-push or call Home Assistant tools; some steps need the maintainer to run one command. This is by design ([CLAUDE.md](../CLAUDE.md)). ✅
- **"Current bill" or "last bill".** The API only returns completed periods, so a sensor called "current bill" invited reading a whole past bill as spending so far. Naming the sensors "Last bill …" fixed the meaning. ✅
- **Two timestamp formats.** Usage dates carry milliseconds and the account's due date does not, which silently broke a timestamp sensor until the parser accepted both. ✅ (code)
- **The 1.4.x default prices were a year out.** They were labelled 2026/27 because they matched a bill issued in July 2026, but that bill's period began in June, so they are the 2025/26 prices ([tariffs.md](tariffs.md#checking-against-a-bill)). ✅
- **Waiting for the recorder during start-up.** 1.5.0's rebuild deadlocked with start-up until Home Assistant cancelled the setup after 5 minutes ([CHANGELOG](../CHANGELOG.md), 1.5.1). ✅
- **HACS reads the default branch's licence.** The import commit's LICENSE was detected as `NOASSERTION`, so the required `hacs` check failed on every pull request until a fixed LICENSE reached `main` ([release.md](release.md#merging-the-first-pull-request)). ✅
