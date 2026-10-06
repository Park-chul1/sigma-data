# CRSP source evidence and bounded input audit

Checked 2026-10-03; field and policy refresh 2026-10-06. This records facts about the local snapshot and source
semantics; it does not certify the original public availability of each value.
No WRDS connection or download was made. Licensed sample values are written only
to the ignored local report, never to this document or synthetic fixtures.

## Source identity and normalization

The ingest query and raw manifests identify `crsp.stkdlysecurityprimarydata`,
`crsp.stksecurityinfohist`, and `crsp.stkdelists`. The saved source-schema files
match those queried columns. This identifies the extract as **CIZ / Flat File
Format 2.0**, not legacy SIZ. Identification uses source provenance and CRSP's
published table definitions, not the renamed normalized columns alone.

The normalizers only cast and rename values. In particular:

| Source | Normalized | Meaning / consequence |
| --- | --- | --- |
| `DlyPrc` | `price_raw` | Daily last trade or price of another type identified by `price_flag`; not a split-adjusted history or necessarily an executable close |
| `DlyRet` | `ret_total` | Vendor total investment return, already incorporating applicable distributions; terminal return rows are explicitly flagged |
| `DlyRetx` | `ret_ex_div` | Excludes **ordinary** dividends; the normalized name does not mean all distributions are excluded |
| `DlyVol` | `volume_raw_shares` | Raw shares traded, without retrospective split adjustment |
| `DlyDelFlg` | `delist_flag` | Distinguishes terminal-return records from regular returns |
| `DlyRetMissFlg` | `ret_missing_flag` | Vendor reason/quality code, independent of the numeric column's nullness |
| `DlyDistRetFlg` | `distribution_return_flag` | Summary of corporate effects in the vendor calculation, not full event terms |

The [CRSP CIZ database guide](https://www.crsp.org/wp-content/uploads/guides/CRSP_US_Stock_%26_Indexes_Database_Guide_Flat_File_Format_2.0.pdf)
defines the daily primary table and these fields. The
[official SIZ-to-CIZ comparison](https://www.crsp.org/crsp_pdf/crsp-us-stock-indexes-databases-siz-to-ciz-cross-reference-guide/)
identifies the primary daily file as the 12-column subset of the full daily
file. Some old CRSP guide links now redirect to Morningstar's guide viewer;
its PDF frame was not readable with the browser tool. The official indexed
guide extracts supplied the table definitions; directly accessible CRSP flag
appendices below supplied the code definitions. The old comparison URL also
returned 404 on direct open. No inaccessible section was assumed verified.

`DelDlyDt` is a return **storage** date, conventionally the trading session after
`DelistingDt`. `DelAmtDt` refers to the amount used for the terminal return and
is a separate field. Neither is automatically the time cash became available.
See the [official CRSP10 guide, delistings section](https://www.crsp.org/wp-content/uploads/guides/CRSP10_Year_US_Stock_Database_Guide.pdf).
The local extract did not ingest these fields; event/storage matching therefore
uses the documented convention and is checked against the current snapshot.

### 2026-10-06 field and policy refresh

Read-only Parquet schema inspection again confirmed that **both** the existing
raw `stkdelists` and normalized delistings omit `DelDlyDt` and `DelRetMissType`.
The saved WRDS schema lists these available source fields; that listing is not
evidence that their values were collected. No RAW/NORMALIZED file was changed.
The official CIZ guide's indexed primary-table definition was readable and
distinguishes `DlyDelFlg=Y` (terminal return) from `N` (ordinary return). The
official indexed CRSP10 delisting definition remained readable, but direct
opening of its PDF returned 404. These indexed official extracts support the
storage convention; unavailable PDF content has not been treated as inspected.
The live distribution-impact and return-missing dictionaries linked below were
also rechecked. The CIZ guide associates both `DelRetMissType` and
`DlyRetMissFlg` with the same RM dictionary.

The canonical builder now accepts an actually present normalized
`deldlydt` or `return_storage_date` field as source evidence. If the field is
absent, `storage_date_basis=NEXT_SESSION_FALLBACK_NOT_SOURCE_DATE` explicitly
labels the derived next-XNYS-session candidate and `source_return_storage_date`
stays NULL. A present-but-NULL source date stays unknown. No nearest-date
matching is allowed. Exact PERMNO/storage keys, return values and NULL states
are checked independently from daily numeric/quality validation. When an RM
event flag is supplied, its presence, code and agreement are also checked.
`terminal_reconciliation.parquet` retains reverse event-to-daily failures,
including absent rows and nonterminal flags. Its dates and future event details
are audit evidence only, never decision inputs or publication/payment times.

The record API verifies the same exact storage key even when the caller passes
`reconciled=True`; that argument cannot authorize arbitrary later dates. Both
paths share the numeric/source-flag gate: finite return at least -1, `NA`
missing flag and known distribution/delisting flags. `DelRet` corroborates the
stored CIZ terminal return and is never added again. This strengthens internal
consistency and approximate PIT; it does not establish historical vintages or
the availability of terminal proceeds.

## Event and missing-value code evidence

The [CRSP distribution-impact dictionary](https://www.crsp.org/wp-content/uploads/appendix/FlagType_CI.html)
supports these mappings: C1/C2 ordinary cash distributions; S1/S2 splits or stock
dividends; CS cash plus split; D1/D2 terminal actions; F1/P1 non-split share/price
factors; M2/MU multiple actions; N1 non-ordinary; O1 other; T1 tender; NO no action;
NA not applicable. These summaries do not identify every split ratio, spin-off,
right, or successor consideration. Unknown codes must remain unknown.

The [CRSP return-missing dictionary](https://www.crsp.org/wp-content/uploads/appendix/FlagType_RM.html)
defines NA as not applicable, NS as new security, NT as not tracked, RA as return
after an untracked interval, MP as missing price, GP as excessive price gap,
DG/DM/DP as terminal-return problems, and MV as a missing corporate-action value.
**A non-null return with MV is not an unqualified valid outcome.**

The [CRSP delisting code crosswalk](https://www.crsp.org/wp-content/uploads/DelistCode.html)
distinguishes MER (merger), GEX (exchange), GLI (liquidation), GDR (dropped), and
LOS (lost source). Successor identity does not provide the exchange ratio or
prove continuity of the same security. The
[return-duration dictionary](https://www.crsp.org/wp-content/uploads/appendix/FlagType_RD.html)
also distinguishes single-session, multiple-session, missing, and terminal
returns. The local primary extract omits the duration field, previous-price
date, opening price, adjustment factors, distribution dates/amounts/ratios,
publication timestamps, and historical revision vintages.

## Local checks and results

Run from the repository root in the documented research environment:

```sh
python scripts/inspect_research_inputs.py --calendar --full-terminal-check
```

The default report is `data/derived/research_validation/input_audit.json` and
contains the actual bounded event samples. `--data-root` and `--output` are
configurable. Without the optional flags, the script reads manifests/Parquet
footers plus January 2019, August 2020, and June 2022 values. The optional checks
scan only the necessary full-period columns. No input is modified.

Results for this snapshot, with DuckDB 1.5.6 and exchange_calendars 4.13.2:

- Canonical daily raw and normalized: 312 monthly partitions each, 49,875,213
  rows, 2000-01-03 through 2025-12-31; every manifest count agrees with its footer.
- Security history: 121,149 intervals / 25,331 PERMNOs; no invalid or overlapping
  validity intervals. Some intervals start before the daily extract.
- Event table: 14,618 unique PERMNO/date events, 703 missing returns, 966 true zero
  returns, and eight total losses. No numeric return outside the valid domain.
- The three sampled months contain 520,586 rows in total, no duplicate/null
  daily keys, and zero raw-to-normalized value differences under the existing
  cast/rename definitions. This run does not recheck every daily value/key.
- All 6,539 observed daily dates match the independently constructed XNYS
  session calendar exactly. The 2025-01-09 Carter funeral closure is absent in
  both. This checks session coverage, not vendor security membership.
- All 14,618 stored events match daily terminal values and the next-session
  storage convention. Three terminal rows at the left boundary have no stored
  event. There are 703 null terminal outcomes and **132 additional numeric
  terminal returns with MV warnings**. These 132 are seven D1, 117 D2, and eight
  MU distribution-impact records. Treating every numeric return as complete
  would miss this condition.
- Twenty-four assertions passed. Known deficiencies above are reported as
  deficiencies; passing assertions do not certify complete or strict PIT data.

The source release is not identified in old manifests. Their timestamps record
2026 collection/normalization, not historical information availability. The
audit's metadata identity hashes paths, sizes, mtimes, and manifest bytes; it
is explicitly not a content hash or vendor release identifier.

## Actual event cases and limits

The report checks Apple's August 2020 four-for-one split and ordinary cash
dividend against the issuer's [2020-07-30 announcement](https://www.apple.com/uk/newsroom/2020/07/apple-reports-third-quarter-results/).
That announcement states the split-adjusted trading date of August 31 and a
$0.82 dividend payable August 13 to August 10 record holders. The observed
August 31 return matches the four-for-one price relation; the August 7 vendor
total return matches the cash-inclusive relation, both within six-decimal
source rounding. Thus these effects are already in the local return series.
The announcement is external validation evidence, not an event feed added to
the pipeline; the sample does not validate all distribution terms.

Other bounded cases verify stable PERMNO across FB → META on 2022-06-09, a null
terminal return remaining null, and a numeric terminal value carrying MV.
Full-period terminal reconciliation checks that appending `DelRet` again would
double count those outcomes. It does **not** verify that any terminal amount
was public on the stored return date, available to trade then, or paid then.

Further source work needs the full daily return-duration/previous-price fields,
opening prices for next-open execution, full distributions and terminal
amount/payment timing, complete boundary events, source release identifiers,
and historical availability/vintage data. Strict PIT and share/cash-ledger
reconstruction remain unverified with this input snapshot.

## Re-executed local validation, 2026-10-06 (Asia/Seoul)

The existing read-only inspector was rerun with `--calendar --full-terminal-check`:
24 assertions passed, zero failed. Report:
`data/derived/research_validation/input_audit_20261005.json`. Its full-period
terminal diagnostic joins on PERMNO and then checks the storage convention; the
new canonical run audit uses exact PERMNO/storage-date keys in both directions.
Known boundary/missing/quality counts above remain unresolved, not repaired.

The unified builder was run for 2020-08-03 through 2020-09-04, with preparation
from 2020-05-29, and separately at the left boundary 2000-01-04 through 2000-01-07
with preparation from 2000-01-03. Each passed 80 independent export checks. The
representative run retains 3,615 unresolved return rows; the boundary run retains
876, including all three unmatched terminal rows. Both are `INCOMPLETE_RETURNS`.
See [current pipeline validation](PIT_RESEARCH_PIPELINE.md#검증과-남은-한계) for
commands, run identifiers, sample-dropout counts and precise verification scope.
