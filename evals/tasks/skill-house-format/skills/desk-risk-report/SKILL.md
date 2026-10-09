---
name: desk-risk-report
description: House format for the weekly desk risk report that goes to the risk committee (file name, columns, rounding, sign convention, row order, status flags, total row), with a validator to run before handing it over. Use whenever you write or update a desk risk report.
---
# Desk risk report: house format

The risk committee's tooling parses this file, so every rule matters.

1. **File**: `reports/desk_risk_<YYYYMMDD>.csv`, where the date is the positions'
   `as_of` date written without dashes.
2. **Header**, exactly: `desk,gross_usd,net_usd,var99_usd,limit_usd,util_pct,status`
3. **One row per desk** that holds positions:
   - `gross_usd`: the sum of qty x price over the desk's positions;
   - `net_usd`: the same sum with SHORT positions counted negative;
   - `var99_usd`: the desk's 99% VaR from var.csv;
   - `limit_usd`: the desk's gross limit from limits.csv;
   - `util_pct`: gross_usd / limit_usd x 100, with one decimal place;
   - `status`: `BREACH` if util_pct is above 100.0, `WARN` if it is 85.0 or above,
     otherwise `OK`.
4. **Money** is in whole US dollars: rounded to the nearest dollar, with no currency
   symbol and no thousands separators. Negative amounts have a leading minus.
5. **Order**: rows by util_pct, highest first.
6. **Total**: the last row is `TOTAL`, with the sums of the gross_usd and net_usd
   columns. Leave var99_usd, limit_usd, util_pct and status empty: VaR is not additive
   across desks, so never sum it.
7. **Check** before handing it over: run `python3 validate.py <report>` (validate.py is
   in this skill's directory). It must print `OK`.
