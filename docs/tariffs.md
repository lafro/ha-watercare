# Watercare residential prices by financial year

The integration prices each bill with the prices in force when its billing period started, as Watercare does, and spreads that cost over the bill's days. This page lists the prices it carries and where each one comes from. The table in code is `PUBLISHED_TARIFFS` in [`custom_components/watercare/tariffs.py`](../custom_components/watercare/tariffs.py); a test checks it against the spot values below.

Watercare's financial year runs from 1 July to 30 June, and prices change on 1 July. All figures are **GST-inclusive** residential prices for customers with a water meter in the Metropolitan network, as printed on bills and in Watercare's annual price schedules.

| Financial year | Water, per 1,000 L | Wastewater, per 1,000 L | Wastewater fixed charge, per year | Source |
|---|---|---|---|---|
| 2018/19 | $1.517 | $2.618 | $218 | [1] |
| 2019/20 | $1.555 | $2.704 | $225 | [2] |
| 2020/21 | $1.594 | $2.772 | $231 | [3] |
| 2021/22 | $1.706 | $2.966 | $247 | [4] |
| 2022/23 | $1.825 | $3.174 | $264 | [5] |
| 2023/24 | $1.998 | $3.476 | $289.00 | [6] |
| 2024/25 | $2.142 | $3.726 | $310.00 | [7] |
| 2025/26 | $2.296 | $3.994 | $332.00 | [8] |
| 2026/27 | $2.46 | $4.28 | $355.90 | [9] |

Every year in the table comes from a Watercare publication; no year is estimated. Every schedule also states the same wastewater basis: residential wastewater volume is 78.5% of metered water (apartments are among the exceptions at 95%). The integration's **Wastewater ratio** option defaults to 0.785.

## Sources

Retrieved 8 October 2026. The SHA-256 prefix identifies the exact file that was read.

1. Watercare, *Domestic water and wastewater charges and IGC 2018-2019* (prices effective 1 July 2018). Archived copy: <https://web.archive.org/web/20190206090237/https://www.watercare.co.nz/CMSPages/GetAzureFile.aspx?path=~%5Cwatercarepublicweb%5Cmedia%5Cwatercare-media-library%5Cfees-charges%5Cdomestic_water_ww_igc_charges_2018_2019.pdf&hash=e2d716151960d61c5c7926ed8501ce627c12da17a4eeded983854433db63489b> (SHA-256 `36db576a261bf5ad…`).
2. Watercare news release, *Our water and wastewater prices will change from 1 July 2019*: water rises from $1.517 to $1.555 per 1,000 litres, the domestic fixed wastewater charge rises by $7 to $225 a year, and the wastewater charge rises from $2.618 to $2.704 per 1,000 litres, all including GST. <https://www.watercare.co.nz/home/about-us/latest-news-and-media/our-water-and-wastewater-prices-will-change-from-1-july-2019>. Watercare's 2019/20 price PDF is no longer published or archived; the release is Watercare's own statement of the 2019/20 prices and agrees with the 2018/19 schedule above for the earlier figures.
3. Watercare, *Domestic water and wastewater charges and IGC 2020-2021*. <https://assets.watercare.co.nz/media/domestic_water_ww_igc_charges_2020_2021_a44f00f24e.pdf> (`048396cb22a34acf…`).
4. Watercare, *Domestic water services and wastewater charges and IGC 2021-2022*. <https://assets.watercare.co.nz/media/domestic_waterww_other_charges_2021_2022_81e6c1b88a.pdf> (`966f6bb9b248e3f5…`).
5. Watercare, *Domestic water services and wastewater charges and IGC 2022-2023*. <https://assets.watercare.co.nz/media/domestic_charges_2022_2023_a44c780198.pdf> (`e97b78804e458ac1…`).
6. Watercare, *Domestic water services and wastewater charges and IGC 2023–2024*. <https://assets.watercare.co.nz/media/domestic_charges_2023_2024_ac9880f2db.pdf> (`30cac8aa40d030a1…`).
7. Watercare, *Residential water services and wastewater charges and IGC 2024–2025*. <https://assets.watercare.co.nz/media/Residential_Water_Ww_IGC_Charges_2024_2025_fa98271f45.pdf> (`cb585654c48aef3a…`).
8. Watercare, *Residential water services and wastewater charges and IGC 2025–2026*. <https://assets.watercare.co.nz/media/residential_other_charges_2025_2026_pdf_cf73059f20.pdf> (`638ee97fee353919…`).
9. Watercare, *Residential water services and wastewater charges and IGC 2026–2027* (prices effective 1 July 2026). <https://assets.watercare.co.nz/media/Residential_Water_Ww_IGC_Charges_Other_Charges_2026_2027_rev_0aa42cb042.pdf> (`cabc6ae20d929e72…`).

## How a bill is priced

Watercare charges a whole bill at the prices in force when its billing period **starts**. A bill that runs from June into July is charged entirely at the earlier financial year's prices, and the new prices apply from the first bill that starts on or after 1 July. This was checked against a real bill whose period spanned 1 July 2026: pricing the whole bill at 2025/26 prices reproduced its total to the cent, while splitting it by day between 2025/26 and 2026/27 prices overstated it by about 3.7%, several dollars on one bill. Only that one bill has been checked; if a bill spanning 1 July shows otherwise, please open an issue. The rule is one function, `pricing_date` in [`statistics.py`](../custom_components/watercare/statistics.py).

For each bill, at its start date's prices:

- water cost = litres ÷ 1,000 × water price;
- wastewater cost = litres ÷ 1,000 × wastewater ratio × wastewater price;
- fixed charge = yearly fixed charge ÷ 365 × Watercare's day count for the bill.

Each day of the bill gets an equal share of the bill's litres and fixed-charge days, priced the same way, so the daily rows add up to the bill.

Assumptions to know about:

- **Daily fixed charge.** Watercare invoices the fixed charge "at a daily rate". The integration divides the yearly charge by 365 in every year, including leap years.
- **Rounding.** Watercare rounds each bill line to the cent; the statistics keep unrounded daily values.

## Checking against a bill

A bill's total should equal water + wastewater + fixed charge as above, using the row for the financial year in which the bill **starts**. For example, a made-up bill of 12 kL over a 30-day period starting in August 2025 (2025/26) gives 12 × 2.296 + 12 × 0.785 × 3.994 + 332 ÷ 365 × 30 = NZD 92.46.

Releases up to 1.4.1 defaulted to 2.296 / 3.994 / 332 and labelled them as the 2026/27 prices, because they matched a bill issued in July 2026. They are the **2025/26** prices: that bill's period began in June, so Watercare charged it at 2025/26 prices. 1.4.x priced every past bill with them. From 1.5.0 each bill uses the prices of the year it starts in, so bills that start before 1 July 2025 come out lower, and bills that start on or after 1 July 2026 higher, than 1.4.x showed.

## Adding a new year

Each July, after Watercare publishes the new schedule:

1. Add the year to `PUBLISHED_TARIFFS` and a row and source to this page, with the retrieval date and file hash.
2. Update `test_published_values_match_the_cited_schedules`.
3. Release a new version. Installs that already entered the year's prices in the options keep their entry; matching figures can be removed from the options.

Until a release includes the new year, users see a repair notice and can enter the prices themselves; cost statistics pause from the first bill that starts on or after 1 July in the meantime. Prices entered or released later apply only to days not yet recorded. That is why the options and the repair form leave a year's price fields empty until its prices are known: a guess saved by accident would resume the cost statistics and stay in them.
