# Known unknowns, assumptions and checks

The honest register, as of 1.5.1. If you are about to rely on something load-bearing, check here first. Tags are explained in [README.md](README.md#accuracy-tags).

## Assumptions the code acts on

| # | Assumption | Basis | Risk if wrong |
|---|---|---|---|
| A1 | `mechanicalmonthly` returns the **full** billing history, not a sliding window. | Observed on a real account on 7 August 2026: one response held several years of bills. ⚠️ | Low since 1.5.0. A poll only appends after the stored rows, so a shorter response loses nothing and the sums never step down; the one-off rebuild checks first and is skipped with a repair notice if stored history reaches further back than the response ([statistics.md](statistics.md#when-the-rebuild-is-skipped)). ✅ (code). Up to 1.4.x the sums were recomputed from zero each poll and would have stepped down. |
| A2 | The `id` of the first entry in `meters` in `v1/account` is the meter number printed on the bill. | It matched the bill (`<METER_NUMBER>`) for the account checked. ⚠️ | Cosmetic: only the **Meter number** diagnostic sensor uses it. An account with several meters shows only the first. |
| A3 | `readingType` is only ever `E` (estimate) or `A` (actual). | Every period observed. ⚠️ | Low: an unknown code is shown as received, because the mapping falls back to the raw value. ✅ (code) |
| A4 | Prices entered by the user include GST. | Bills and Watercare's schedules print GST-inclusive prices, the published table is GST-inclusive ([tariffs.md](tariffs.md)), and every price field in the options and the repair form says "including GST". ✅ (code) | Costs from entered prices would be off by GST if someone entered prices without it. |
| A5 | Watercare charges a whole bill at the prices in force when its period **starts**. | One real bill spanning 1 July 2026 matched this to the cent; a split by day was about 3.7% too high ([tariffs.md](tariffs.md#how-a-bill-is-priced)). ⚠️ (one bill) | Bills spanning 1 July would be mispriced. The rule is one function, `pricing_date`, but correcting stored history needs another rebuild. |
| A6 | The fixed charge accrues as the yearly charge ÷ 365 per day, in every year. | Watercare invoices it "at a daily rate"; leap years have not been checked against a bill ([tariffs.md](tariffs.md#how-a-bill-is-priced)). ⚠️ | A bill in a leap year would be off by a fraction of one day's fixed charge. |
| A7 | Watercare corrects an estimated reading through the next bill, not by revising a bill it has already issued. | [statistics.md](statistics.md#anchoring-to-stored-history) and the README rely on it, but the evidence is not recorded in this repository. Up to 1.4.x the recompute-from-zero design was valued partly because a revision would flow through all later sums. ⚠️ | A revised bill would not reach days already stored, because polls only append; the stored totals would stay as first imported. A one-off manual rebuild ([statistics.md](statistics.md#when-the-rebuild-is-skipped)) would pick the revision up. |
| A8 | An explicit `null` for `waterUsage` means no water was used. | Carried over from 1.4.1, which treated it the same way for consistency with its own code, not from an observed response. ⚠️ | A bill recorded as zero, and kept as zero by the append-only polls. Low. |
| A9 | Home Assistant 2026.10.0 is the minimum. | Up to 1.4.x the README named 2026.3 (for locally served brand images) without testing older releases. From 1.5.0, `hacs.json` requires 2026.10.0, the release the tests run against, and the weekly compatibility workflow tests the newest release and test harness. ✅ (code) | None known. |

## Known unknowns

- **Smart meters.** The smart-meter endpoints (the original project offered `dailywithstats`, `monthly` and `halfhourly`) return nothing for a mechanical account, so their real payloads have never been seen, and nothing about a smart-meter account can be tested without one. Any re-add starts by inspecting a real response. ❓
- **Revisions to issued bills.** See A7. ❓
- **Another bill spanning 1 July.** Only one has been compared with `pricing_date` (A5). [release.md](release.md#before-releasing-150) asked for a second, for example one issued in July 2025; no second comparison is recorded. ❓
- **APIs the integration does not use.** Watercare's app has other endpoints, and the My Account portal has its own API (invoice history, bill PDFs). Their payloads, and whether a token from one API works on the other, were never tested. ❓

## Deliberate behaviours that can surprise (not bugs)

- **A price change never reprices stored days.** Correcting a price entered in the options or the repair form fixes only days recorded afterwards. Days already stored at a wrong price need the manual rebuild in [statistics.md](statistics.md#when-the-rebuild-is-skipped). Up to 1.4.x it was the reverse: any price change repriced all history. ✅ (code)
- **"Last bill", not "current".** The sensors describe the most recent completed bill, because that is all the API returns, not use accruing now. ✅
- **Daily values are an even split of each bill**, not measured daily use; a mechanical meter is read about monthly, in whole kilolitres, so a bill's usage moves in 1,000 L steps. ✅
- **Cost is calculated, not billed.** It uses Watercare's published residential prices and leaves out infrastructure growth charges, trade waste, late fees and credits. It is not Watercare's invoice figure. ✅
- **Costs pause rather than guess.** With no prices for a bill's year, the cost statistics stop at that bill and a repair notice asks for the prices; water use keeps recording. ✅ (code)
- **History before 1 July 2018 uses the 2018/19 prices**, the earliest year in the table, so it probably comes out a little high: the prices in the table rise every year. ✅ (code)
- **One Watercare account per Home Assistant instance.** ✅ (code)
- **Removing the integration keeps the statistics.** Delete them under **Developer tools → Statistics** if the history is no longer wanted. ✅ (code)

## Things to re-check on a schedule

- **Every 1 July:** Watercare publishes new prices. Add the year to `tariffs.py` with its source and release a new version ([tariffs.md](tariffs.md#adding-a-new-year)). Until then, users see a repair notice and cost statistics pause from the first bill that starts in the new year.
- **Each Home Assistant release:** bump `pytest-homeassistant-custom-component`, which pins Home Assistant, and drop the `homeassistant` override in `pyproject.toml` once the harness pins a final release ([CLAUDE.md](../CLAUDE.md), "Working").
- **Weekly:** the Compatibility workflow tests against the newest Home Assistant release and harness and opens an issue when it fails. GitHub disables scheduled workflows in a public repository after 60 days without activity; check with `gh run list --workflow compat.yml`.
- **When a bill spanning 1 July is available:** compare its charge lines with the integration's figure (A5).

## The Python-version question

It recurs, so here is the answer. ✅ (code and package metadata, 9 October 2026)

- The shipped integration has **no Python pin**: `requirements` in `manifest.json` is empty, and the code runs on whatever Python Home Assistant runs.
- Development and tests need Python 3.14.2 or later (`requires-python` in `pyproject.toml`), because the test harness pins Home Assistant exactly and Home Assistant 2026.10.0 itself requires Python 3.14.2 or later.
- That floor follows Home Assistant; it is not a choice made here. Move it by bumping the harness.

## How these pages were checked

- First written on 8 August 2026 for 1.4.1, from the code and its git history, the manifest, PyPI metadata and the running installation, not from memory. An independent reviewer then cross-read every ✅ claim against the code, the CI configuration, the fork point and PyPI, and re-computed a cost example from a real bill. All checked claims matched except one, which was corrected: the coordinator did not branch on the endpoint; that was a documented seam, not live code (and 1.5.0 removed it).
- Moved into this repository and updated for 1.5.1 on 9 October 2026: code claims were re-checked against 1.5.1, the claims about the original project against `brunsy/ha-watercare` at `ef7e258`, and the Python floor against Home Assistant's package metadata. Claims about Watercare's API and a real account keep their original date and could not be re-checked from this repository. Account and meter identifiers, and figures from real bills, were removed.
- If a ✅ claim no longer matches the code, downgrade it to ⚠️ with the date rather than silently editing it, so drift stays visible.

## What these pages do not cover

- Line-by-line code detail: read the source, which is commented where the behaviour is not obvious.
- The brand-artwork pipeline, which lives outside this repository.
- Watercare's APIs beyond what the code uses.
- Secrets: none are kept in this repository.
