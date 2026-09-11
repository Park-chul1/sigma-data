from pathlib import Path
import duckdb

ROOT = Path(__file__).resolve().parents[1]

INFO = ROOT / "data/normalized/crsp/security_info/part-000.parquet"

con = duckdb.connect()

print("=== SECURITY CLASSIFICATION COMBINATIONS ===")

print(
    con.execute(
        f"""
        SELECT
            share_type,
            security_type,
            security_subtype,
            us_incorporated_flag,
            issuer_type,
            COUNT(*) AS rows,
            COUNT(DISTINCT permno) AS securities
        FROM read_parquet('{INFO}')
        GROUP BY ALL
        ORDER BY securities DESC
        LIMIT 40
        """
    ).df().to_string(index=False)
)

print("\n=== EXCHANGE / TRADING STATUS ===")

print(
    con.execute(
        f"""
        SELECT
            primary_exchange,
            conditional_type,
            trading_status_flag,
            COUNT(*) AS rows,
            COUNT(DISTINCT permno) AS securities
        FROM read_parquet('{INFO}')
        GROUP BY ALL
        ORDER BY securities DESC
        """
    ).df().to_string(index=False)
)

con.close()
