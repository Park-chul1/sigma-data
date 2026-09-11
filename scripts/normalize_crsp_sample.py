from pathlib import Path
import duckdb

ROOT = Path(__file__).resolve().parents[1]

RAW_DAILY = ROOT / "data/raw/crsp/ciz/stkdlysecurityprimarydata/year=2025/month=01/part-000.parquet"
RAW_INFO = ROOT / "data/raw/crsp/ciz/stksecurityinfohist/sample/2025-01.parquet"
RAW_DELIST = ROOT / "data/raw/crsp/ciz/stkdelists/sample/2025-01.parquet"

NORM_DAILY = ROOT / "data/normalized/crsp/daily/year=2025/month=01/part-000.parquet"
NORM_INFO = ROOT / "data/normalized/crsp/security_info/part-000.parquet"
NORM_DELIST = ROOT / "data/normalized/crsp/delists/year=2025/month=01/part-000.parquet"

for p in [NORM_DAILY, NORM_INFO, NORM_DELIST]:
    p.parent.mkdir(parents=True, exist_ok=True)

con = duckdb.connect()

# --------------------------------------------------
# Daily
# --------------------------------------------------

con.execute(f"""
COPY (
    SELECT
        CAST(permno AS BIGINT)              AS permno,
        CAST(dlycaldt AS DATE)              AS date,

        CAST(dlyprc AS DOUBLE)              AS price,
        CAST(dlycap AS DOUBLE)              AS market_cap,
        CAST(dlyret AS DOUBLE)              AS ret,
        CAST(dlyretx AS DOUBLE)             AS ret_ex_div,
        CAST(dlyvol AS DOUBLE)              AS volume,

        CAST(dlydelflg AS VARCHAR)          AS delist_flag,
        CAST(dlyprcflg AS VARCHAR)          AS price_flag,
        CAST(dlycapflg AS VARCHAR)          AS market_cap_flag,
        CAST(dlyretmissflg AS VARCHAR)      AS ret_missing_flag,
        CAST(dlydistretflg AS VARCHAR)      AS distribution_return_flag

    FROM read_parquet('{RAW_DAILY}')
)
TO '{NORM_DAILY}'
(
    FORMAT PARQUET,
    COMPRESSION ZSTD
)
""")

# --------------------------------------------------
# Security history
# --------------------------------------------------

con.execute(f"""
COPY (
    SELECT
        CAST(permno AS BIGINT)                  AS permno,
        CAST(secinfostartdt AS DATE)            AS valid_from,
        CAST(secinfoenddt AS DATE)              AS valid_to,

        CAST(ticker AS VARCHAR)                 AS ticker,
        CAST(cusip AS VARCHAR)                  AS cusip,
        CAST(primaryexch AS VARCHAR)            AS primary_exchange,

        CAST(sharetype AS VARCHAR)              AS share_type,
        CAST(securitytype AS VARCHAR)           AS security_type,
        CAST(securitysubtype AS VARCHAR)        AS security_subtype,

        CAST(usincflg AS VARCHAR)               AS us_incorporated_flag,
        CAST(issuertype AS VARCHAR)             AS issuer_type,
        CAST(tradingstatusflg AS VARCHAR)       AS trading_status_flag,
        CAST(conditionaltype AS VARCHAR)        AS conditional_type,
        CAST(shareclass AS VARCHAR)             AS share_class

    FROM read_parquet('{RAW_INFO}')
)
TO '{NORM_INFO}'
(
    FORMAT PARQUET,
    COMPRESSION ZSTD
)
""")

# --------------------------------------------------
# Delistings
# --------------------------------------------------

con.execute(f"""
COPY (
    SELECT
        CAST(permno AS BIGINT)              AS permno,
        CAST(delistingdt AS DATE)           AS delisting_date,
        CAST(delret AS DOUBLE)              AS delisting_return,

        CAST(delactiontype AS VARCHAR)      AS action_type,
        CAST(delstatustype AS VARCHAR)      AS status_type,
        CAST(delreasontype AS VARCHAR)      AS reason_type,
        CAST(delpaymenttype AS VARCHAR)     AS payment_type,

        CAST(delpermno AS BIGINT)           AS successor_permno,
        CAST(delpermco AS BIGINT)           AS successor_permco

    FROM read_parquet('{RAW_DELIST}')
)
TO '{NORM_DELIST}'
(
    FORMAT PARQUET,
    COMPRESSION ZSTD
)
""")

con.close()

print("Normalized CRSP sample created.")
print("daily :", NORM_DAILY)
print("info  :", NORM_INFO)
print("delist:", NORM_DELIST)
