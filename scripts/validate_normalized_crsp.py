from pathlib import Path
import duckdb

ROOT = Path(__file__).resolve().parents[1]

DAILY = ROOT / "data/normalized/crsp/daily/year=2025/month=01/part-000.parquet"
INFO = ROOT / "data/normalized/crsp/security_info/part-000.parquet"

con = duckdb.connect()

print("=== DAILY SCHEMA ===")
print(con.execute(f"""
DESCRIBE
SELECT * FROM read_parquet('{DAILY}')
""").df())

print("\n=== INFO SCHEMA ===")
print(con.execute(f"""
DESCRIBE
SELECT * FROM read_parquet('{INFO}')
""").df())

print("\n=== PIT JOIN ===")
print(con.execute(f"""
SELECT
    COUNT(*) AS joined_rows,

    COUNT(*) FILTER (
        WHERE h.permno IS NULL
    ) AS unmatched_rows

FROM read_parquet('{DAILY}') d

LEFT JOIN read_parquet('{INFO}') h
    ON d.permno = h.permno
   AND d.date BETWEEN h.valid_from AND h.valid_to
""").df())

con.close()
