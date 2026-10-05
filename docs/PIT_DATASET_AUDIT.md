# PIT dataset audit and completion plan

Audit date: 2026-09-27. Repository commit inspected: `f495c9e`.

Implementation update (2026-10-03): the bounded approximate-PIT research builder,
event/return policies, trading-calendar warmup, separated features/labels/display,
content-addressed caches, and synthetic tests are now implemented. See
[current pipeline documentation](PIT_RESEARCH_PIPELINE.md) and
[source evidence](CRSP_SOURCE_EVIDENCE.md). The latter also identifies **132
numeric terminal returns with MV warnings**, in addition to the 703 NULLs below.
The rest of this document preserves the original audit and proposed work;
strict historical PIT, detailed settlement terms, and next-open execution remain unfinished.

The CRSP raw and normalized foundation is in place for 2000–2025. The dataset is **not yet ready for a survivorship-controlled, point-in-time backtest**: the full-period derived layer, terminal-return policy, information-availability rules, and release checks remain unfinished.

Scope assumed from the stored data and `principles.md`: CRSP-covered US equities, with the sample's US-incorporated common-stock universe as an initial research configuration. This is not a claim to cover every US security, OTC issue, or foreign-incorporated US listing. The existing files stop in December 2025; no 2026 data is stored. A later endpoint or a different universe needs an explicit configuration and source-coverage check.

“Zero survivorship bias” is a design requirement to retain historically eligible securities and all subsequent holding outcomes. It is not something these files can certify absolutely: missing terminal outcomes, vendor coverage, and historical revisions remain measurable limitations. Historical effective dates also do not establish when a value was actually available to an investor.

**What is actually present**

| Folder or component | Verified contents | Remaining work |
| --- | --- | --- |
| `data/raw/crsp/stkdlysecurityprimarydata/` | 312 monthly partitions; 49,875,213 rows; 2000-01-03 through 2025-12-31 | Reconcile against an identified vendor release; add execution/event fields in a versioned extract |
| `data/normalized/crsp/daily/` | Same 312 partitions and rows; 25,306 PERMNOs; 6,539 observed dates | Build the full-period derived layer |
| `data/raw/crsp/stksecurityinfohist/`, `data/normalized/crsp/security_info/` | 121,149 historical intervals; 25,331 PERMNOs | Classify terminal rows outside metadata validity; preserve information availability separately |
| `data/raw/crsp/stkdelists/`, `data/normalized/crsp/delistings/` | 14,618 events; 2000-01-03 through 2025-12-30 | Expand timing/missingness fields and reconcile boundary events |
| `data/derived/crsp/universe/` | January 2025 only: 195,703 rows, 75,794 flagged investable | Replace the sample with a configurable full-period builder |
| `metadata/schema/` | Seven schema descriptions, including full daily CRSP, Compustat, and CCM | Schemas are not ingested financial/link data |
| `data/raw/crsp/ciz/`, `data/normalized/crsp/delists/`, `data/normalized/demo_crsp/` | Earlier samples/demo data | Isolate from production input discovery |
| `src/ingest/`, `src/normalize/` | Three production source pipelines | Add coverage-aware reuse and versioned provenance |
| `src/validate/`, `tests/`, `logs/`, `notebooks/` | Empty | Add automated assertions and release reports |
| `db/research.duckdb` | No tables | Optional catalog only; Parquet is the actual store |

The audit compared every daily raw partition with its normalized counterpart using the production casts and bidirectional `EXCEPT ALL`: **zero value/row differences across all 312 partitions**. Security history and delistings also matched their raw inputs exactly after the intended casts. All canonical manifests agreed with actual row counts. Daily raw file sizes agreed with their manifests, and all requested monthly intervals were complete calendar-month queries.

Other full-data checks passed: no duplicate daily `(permno, date)` keys, null daily keys, dates in the wrong monthly partition, invalid security intervals, overlapping security intervals, or duplicate delisting `(permno, event_date)` keys. No non-null total return was below -1 or nonfinite.

These establish local consistency, not completeness relative to WRDS. No live WRDS query or re-download was performed. The broken local `.venv/bin/python*` entries are zero-byte files; scans used Python 3.14 with DuckDB 1.5.5 in an isolated temporary environment. Production scripts and data were not changed by this audit.

**The critical findings**

1. **Do not filter holding returns by same-day investability.** Applying the rule in [build_universe_sample.py](../scripts/build_universe_sample.py) across the complete dataset leaves **zero of 14,621 daily delisting-return rows eligible**. That is appropriate for selecting new positions, but would remove every terminal-return row if used to select portfolio P&L observations. The sample currently stores flags; this is a dangerous downstream usage, not a claim that it already deleted rows. Preserve the complete return stream for securities held before they stop being investable. Loss of eligibility is not an executable liquidation.

2. **Delisting returns are already present in this daily extract.** Each of the 14,618 stored events matches exactly one daily `delist_flag = 'Y'` row by PERMNO. All occur on the next observed market session after `event_date`; all return values agree, including matching NULLs. The matching non-null returns must not be compounded with `delisting_return` again. Daily rows should identify their economic source, for example `CRSP_DAILY` or `CRSP_DELIST_EMBEDDED`, while retaining the original fields. CRSP documents the daily flag and separate return-storage date in its [CIZ guide](https://indexes.morningstar.com/docs/guide/crsp-us-stock-databases-guide-for-flat-file-format-2-0?isRdp=true). This finding applies to the audited extract; validate again after a source/version change.

3. **703 delisting outcomes are missing.** All 703 appear as missing returns in both the event table and daily delisting rows. Of those, **384 meet the sample's full baseline eligibility rule on their event date**. There are also 649,811 missing daily total returns overall. Preserve the vendor reason flags. Do not fill these with zero, remove the securities retrospectively, or let a portfolio aggregation silently skip held missing returns. A strict run should stop/report an unresolved held outcome; alternative return assumptions belong in explicitly named sensitivity runs. Reconstruct only from evidence and preserve its timing and provenance.

4. **Nine daily rows do not match security history.** The full left join preserves all 49,875,213 daily rows, but nine have no valid metadata interval. All nine are return-bearing delisting rows just after the historical interval ends. Preserve them as terminal accounting records. Historical descriptive metadata may be attached from the verified event/last valid interval with an explicit provenance flag; that must not extend trading eligibility. The current [PIT validator](../scripts/validate_crsp_pit.py) reports unmatched rows without requiring an exception classification.

5. **Three delisting rows lack events in the stored event window.** They are PERMNOs `53938`, `80139`, and `86564`, all dated 2000-01-03. Earlier event dates are a likely boundary explanation, but were not verified against WRDS. Reconcile them using `deldlydt` and earlier event coverage. Filtering events only by `delistingdt >= requested_start` is insufficient for return-date coverage.

6. **Next-open execution is not yet supported.** `principles.md` specifies signals after the close and execution at the next open. The ingested primary daily table lacks `dlyopen`. Obtain suitable opening prices and document the execution assumption; a first reported trade is not a guaranteed fill. Close-to-close daily total return cannot automatically be used as the realized return of a position first bought at the next open, because it includes the prior overnight movement.

7. **Historical joins are not complete PIT availability controls.** The source files were downloaded in August/September 2026. Effective intervals reconstruct historical classification, but there are no implemented `available_at` checks or historical revision vintages. A later-known delisting payout may be useful as an eventual outcome without being usable as an earlier feature or immediately available cash. A download timestamp is provenance, not the original public-availability timestamp.

**Complete the work in this order**

1. **Freeze the scope, repair the runtime, and make reuse safe.**

   Keep the existing snapshot. Add a dependency specification/lock and a documented entrypoint. Define requested start/end, source release, decision time, execution time, universe policy, lookback, and missing-outcome policy in a run configuration. Do not assume the source's current maximum date equals the locally stored endpoint.

   Fix existence-only skip logic in [daily ingest](../src/ingest/crsp_daily.py), [security-history ingest](../src/ingest/crsp_security_info.py), and [delisting ingest](../src/ingest/crsp_delistings.py): reuse must verify requested coverage, columns/schema, and source identity. A partial month must be extendable. The single-file history/event extracts must not silently skip an expanded date range. Normalized reuse must detect changed raw input. Stage and validate replacements before publishing them; `--force` currently deletes committed files before downloading their replacements.

   Give datasets snapshot IDs, content hashes, query/column fingerprints, schema versions, and repository-relative paths. Existing normalized manifests point to an old absolute checkout path. Isolate [normalize_crsp_sample.py](../scripts/normalize_crsp_sample.py), which targets production daily/security paths with sample contents and an older daily schema.

2. **Acquire the missing CRSP fields and source metadata.**

   Add a versioned extract from `crsp.stkdlysecuritydata`, whose schema is already saved locally. Prioritize `dlyopen`, `dlyclose`, `dlybid`, `dlyask`, `dlyprevdt`, `dlyprevprc`, `dlyprevprcflg`, `dlyretdurflg`, `dlyfacprc`, `dlyorddivamt`, and `dlynonorddivamt`, retaining existing flags. The previous-price date and duration flag help distinguish returns spanning gaps from ordinary session returns. Keep source values intact during normalization.

   Expand `stkdelists` to include `deldlydt`, `delretmisstype`, `delamtdt`, `delnextdt`, `delnextprc`, `delnextprcflg`, `deldtprc`, `deldtprcflg`, `deldivamt`, and `deldistype`. Preserve the separate event, return-storage, and amount dates. CRSP's [CIZ field definitions](https://indexes.morningstar.com/docs/guide/crsp-us-stock-databases-guide-for-flat-file-format-2-0?isRdp=true) distinguish these dates; the daily return-storage convention does not establish cash availability.

   Ingest the vendor trading calendar and flag dictionaries, and discover/save the schema of `stkdistributions` before adding its event extract. Include declaration, ex, record, payment, and security/ratio fields where provided. Expand source coverage for lookback and event boundaries. Reconcile counts and missing dates by month against the identified source release; aggregate counts alone cannot establish membership completeness.

3. **Build the full-period security panel and a separate universe.**

   Implement `src/derive/security_daily_pit.py` plus a date-range CLI. These are proposed new files, not existing commands. Start with a row-preserving daily left join on PERMNO and the inclusive validity interval, using an open-end convention for NULL `valid_to`. Reject multiple matching intervals. Keep unmatched terminal rows with classified exceptions.

   Store historical classification, `is_listed`, `is_trading`, `can_enter`, `can_exit`, `has_required_history`, `price_is_stale`, and reason codes. Use source trade/price flags and the calendar; an active security-history flag alone does not prove an executable trade. The snapshot contains 1,573,216 bid/ask-average price rows and explicit halt/suspension/missing-price states. Keep observational state separate from the strategy's trading policy.

   Construct `universe_daily` from attributes available at each decision time. Keep US-incorporation, exchange, security type, minimum price, trailing liquidity, and factor-history rules configurable. Use only backward-looking windows. Do not require survival, a future return, future fundamental coverage, or full-period history. A company-level strategy also needs an explicit as-of share-class selection/aggregation rule.

4. **Build returns and event accounting without dropping holdings.**

   Implement `src/derive/returns_daily.py` and a corporate-event ledger. Preserve `ret_total_vendor`, `ret_total_backtest`, `return_source`, reconstruction flags, missing-reason flags, and review status. In the audited source, retain embedded delisting returns exactly once. Match new event extracts through verified return-storage dates, rather than adopting the nearest-event heuristic in the existing diagnostic script as production logic.

   Keep event outcomes and holdings accounting available after `can_enter` becomes false. Track outstanding claims, actual payment timing, and verified successor terms. Do not manufacture a sale during a halt or automatically splice successor PERMNO histories. Prevent dividend/event cash flows from being counted again when total return already includes them. A return-based research mode and a share/cash-ledger execution mode need distinct accounting conventions.

   Resolve the nine metadata exceptions and three boundary events. Classify the 703 missing outcomes with the expanded fields. Keep unresolved cases visible even if exact payoff recovery proves impossible; excluding those securities using hindsight would create the selection problem the dataset is meant to prevent.

5. **Build a run exporter with enforceable timing.**

   Implement `scripts/build_backtest_dataset.py` with `requested_start`, `process_start`, and `requested_end`. Derive `process_start` from trading sessions, maximum lookback, and information lags. If performance must start in January 2000, download the required pre-2000 history or explicitly delay eligibility until history is sufficient.

   Separate decision-time features from future outcome labels. Enforce `available_at <= decision_time`; store whether availability is observed or conservatively assumed. Unknown publication times need a documented session lag. Avoid treating later source corrections or eventual payouts as earlier information. Preserve historical vintages where the source provides them.

   Generate optional total-return and vendor price-return indices per run. Missing returns must not silently disappear from compounding, and a -100% return must reduce the relevant wealth path to zero. The field currently named `ret_ex_div` corresponds to CRSP `DlyRetx`, which excludes ordinary dividends; document that definition rather than assuming it excludes every distribution. Write a run manifest with source hashes, code version, configuration, timing/event policies, coverage, and unresolved exposures.

6. **Add fundamentals only with a version/availability design.**

   No Compustat or CCM observations are currently ingested. For valuation, profitability, investment, or accounting factors, ingest historical company/security links and fundamentals including inactive companies. Join PERMNO/GVKEY through link validity, with explicit link-type/primary-link rules and ambiguity checks. Avoid inner joins that remove market-history rows merely because accounting data is unavailable.

   `datadate` is not a publication date. `rdq` is an earnings-report date, not proof that every later-populated field was available then. A fixed lag also does not undo restatements. For strict PIT accounting features, use a suitable historical-vintage product or original filings with publication/acceptance times and field-level availability. Ordinary Compustat can overwrite earlier values; [WRDS/S&P's restatement guidance](https://wrds-www.wharton.upenn.edu/documents/2180/WRDS_SP_Webinar_Feb_002.pdf) distinguishes those data from products retaining versions. Verify subscription and historical coverage before choosing a source. This step can follow the CRSP milestone for a price/volume-only strategy.

7. **Require these release checks before calling the dataset finished.**

   - Every expected partition/session is covered or explicitly explained; source reconciliation and hashes are recorded.
   - Raw-to-normalized values and rows are preserved; keys are unique; historical joins do not multiply rows.
   - Every unmatched metadata row and every terminal event has a disposition. Terminal returns remain usable after new-entry eligibility ends.
   - Delisting returns and distributions are included exactly once. Regression cases cover an IPO, a delisting, a -100% loss, a missing terminal outcome, a halt/reopening, a split, a dividend, and year/month boundaries.
   - A held missing return fails or reports unresolved P&L under the strict policy; neither aggregation nor compounding silently ignores it.
   - Eligibility/features computed with data cut off at a decision date match those computed from the full snapshot for that date. Perturbing future data cannot change earlier decisions. This tests code causality, not the historical correctness of a modern vendor vintage.
   - Signal/fill/return timing agrees with the chosen execution model. Missing opens and untradeable holdings have explicit handling.
   - Re-running the same source snapshot, code, and configuration produces identical logical outputs. Dependency versions and exceptions are included in the release report.

The first deliverable should be a complete CRSP panel, universe, and return/event layer with these checks. The second should be accounting PIT features if the strategy needs them. Factor research and a transaction-cost backtest can then consume those validated outputs, as required by `principles.md`.

**A reproducible check for the most consequential finding**

Run this read-only SQL from the repository root with DuckDB:

```sql
WITH terminal AS (
    SELECT *
    FROM read_parquet(
        'data/normalized/crsp/daily/year=*/month=*/part-000.parquet'
    )
    WHERE delist_flag = 'Y'
), classified AS (
    SELECT d.*, h.share_type, h.security_type, h.security_subtype,
           h.us_incorporated_flag, h.issuer_type, h.primary_exchange,
           h.trading_status_flag, h.conditional_type
    FROM terminal d
    LEFT JOIN read_parquet(
        'data/normalized/crsp/security_info/part-000.parquet'
    ) h
      ON d.permno = h.permno
     AND d.date BETWEEN h.valid_from
                    AND COALESCE(h.valid_to, DATE '9999-12-31')
)
SELECT COUNT(*) AS terminal_rows,
       COUNT(*) FILTER (WHERE
           share_type = 'NS' AND security_type = 'EQTY'
           AND security_subtype = 'COM' AND us_incorporated_flag = 'Y'
           AND issuer_type IN ('ACOR', 'CORP')
           AND primary_exchange IN ('N', 'A', 'Q')
           AND trading_status_flag = 'A' AND conditional_type = 'RW'
       ) AS terminal_rows_passing_entry_filter
FROM classified;
```

Audited result: `terminal_rows = 14621`, `terminal_rows_passing_entry_filter = 0`.
