from pathlib import Path
import duckdb

ROOT = Path(__file__).resolve().parents[1]

DAILY = ROOT / "data/normalized/crsp/daily/year=2025/month=01/part-000.parquet"
INFO = ROOT / "data/normalized/crsp/security_info/part-000.parquet"

OUT = (
    ROOT
    / "data/derived/crsp/universe"
    / "year=2025/month=01/part-000.parquet"
)

OUT.parent.mkdir(parents=True, exist_ok=True)

con = duckdb.connect()

con.execute(
    f"""
    COPY (
        SELECT
            d.permno,
            d.date,

            h.ticker,
            h.cusip,

            h.primary_exchange,
            h.share_type,
            h.security_type,
            h.security_subtype,
            h.us_incorporated_flag,
            h.issuer_type,
            h.trading_status_flag,
            h.conditional_type,

            -- Legacy-style common stock 10/11 equivalent
            (
                h.share_type = 'NS'
                AND h.security_type = 'EQTY'
                AND h.security_subtype = 'COM'
                AND h.us_incorporated_flag = 'Y'
                AND h.issuer_type IN ('ACOR', 'CORP')
            ) AS is_common_stock,

            -- NYSE / NYSE American / NASDAQ
            (
                h.primary_exchange IN ('N', 'A', 'Q')
            ) AS is_primary_us_exchange,

            (
                h.trading_status_flag = 'A'
            ) AS is_active,

            (
                h.conditional_type = 'RW'
            ) AS is_regular_way,

            -- Our baseline investable universe
            (
                h.share_type = 'NS'
                AND h.security_type = 'EQTY'
                AND h.security_subtype = 'COM'
                AND h.us_incorporated_flag = 'Y'
                AND h.issuer_type IN ('ACOR', 'CORP')

                AND h.primary_exchange IN ('N', 'A', 'Q')
                AND h.trading_status_flag = 'A'
                AND h.conditional_type = 'RW'
            ) AS is_investable

        FROM read_parquet('{DAILY}') d

        LEFT JOIN read_parquet('{INFO}') h
            ON d.permno = h.permno
           AND d.date BETWEEN h.valid_from AND h.valid_to
    )
    TO '{OUT}'
    (
        FORMAT PARQUET,
        COMPRESSION ZSTD
    )
    """
)

print(f"Created: {OUT}")

print("\n=== UNIVERSE SUMMARY ===")

print(
    con.execute(
        f"""
        SELECT
            COUNT(*) AS daily_rows,
            COUNT(DISTINCT permno) AS all_securities,

            COUNT(*) FILTER (
                WHERE is_investable
            ) AS investable_rows,

            COUNT(DISTINCT permno) FILTER (
                WHERE is_investable
            ) AS investable_securities

        FROM read_parquet('{OUT}')
        """
    ).df()
)

print("\n=== INVESTABLE SECURITIES BY DATE ===")

print(
    con.execute(
        f"""
        SELECT
            date,
            COUNT(*) FILTER (
                WHERE is_investable
            ) AS n_investable
        FROM read_parquet('{OUT}')
        GROUP BY date
        ORDER BY date
        """
    ).df()
)

con.close()
