from pathlib import Path
import duckdb


ROOT = Path(__file__).resolve().parents[1]

DAILY = (
    ROOT
    / "data/raw/crsp/ciz/stkdlysecurityprimarydata"
    / "year=2025/month=01/part-000.parquet"
)

INFO = (
    ROOT
    / "data/raw/crsp/ciz/stksecurityinfohist"
    / "sample/2025-01.parquet"
)

con = duckdb.connect()


print("=== DAILY ===")

print(
    con.execute(
        f"""
        SELECT
            COUNT(*) AS rows,
            COUNT(DISTINCT permno) AS securities,
            MIN(dlycaldt) AS first_date,
            MAX(dlycaldt) AS last_date
        FROM read_parquet('{DAILY}')
        """
    ).df()
)


print("\n=== DUPLICATE DAILY KEYS ===")

print(
    con.execute(
        f"""
        SELECT COUNT(*) AS duplicate_keys
        FROM (
            SELECT
                permno,
                dlycaldt,
                COUNT(*) AS n
            FROM read_parquet('{DAILY}')
            GROUP BY permno, dlycaldt
            HAVING COUNT(*) > 1
        )
        """
    ).df()
)


print("\n=== NULL / FLAGS ===")

print(
    con.execute(
        f"""
        SELECT
            COUNT(*) AS rows,
            COUNT(*) FILTER (WHERE dlyret IS NULL) AS null_ret,
            COUNT(*) FILTER (WHERE dlyprc IS NULL) AS null_price,
            COUNT(*) FILTER (WHERE dlycap IS NULL) AS null_cap,
            COUNT(*) FILTER (WHERE dlydelflg = 'Y') AS delist_rows
        FROM read_parquet('{DAILY}')
        """
    ).df()
)


print("\n=== PIT JOIN ===")

print(
    con.execute(
        f"""
        SELECT
            COUNT(*) AS joined_rows,
            COUNT(*) FILTER (
                WHERE h.permno IS NULL
            ) AS unmatched_rows
        FROM read_parquet('{DAILY}') d

        LEFT JOIN read_parquet('{INFO}') h

          ON d.permno = h.permno

         AND TRY_CAST(d.dlycaldt AS DATE)
             >= TRY_CAST(h.secinfostartdt AS DATE)

         AND TRY_CAST(d.dlycaldt AS DATE)
             <= COALESCE(
                    TRY_CAST(h.secinfoenddt AS DATE),
                    DATE '9999-12-31'
                )
        """
    ).df()
)

con.close()
