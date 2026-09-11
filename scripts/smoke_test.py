from pathlib import Path

import duckdb
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]

PARQUET_DIR = ROOT / "data" / "normalized" / "demo_crsp" / "year=2025"
PARQUET_FILE = PARQUET_DIR / "part-000.parquet"
DB_FILE = ROOT / "db" / "research.duckdb"

PARQUET_DIR.mkdir(parents=True, exist_ok=True)


# 1. 가짜 CRSP 데이터
df = pd.DataFrame(
    {
        "permno": [10001, 10002, 10001, 10002, 10003],
        "date": pd.to_datetime(
            [
                "2025-01-02",
                "2025-01-02",
                "2025-01-03",
                "2025-01-03",
                "2025-01-03",
            ]
        ),
        "ret": [0.01, -0.02, 0.005, 0.012, -0.004],
        "prc": [20.1, 31.2, 20.2, 31.5, 44.0],
    }
)

# 2. Parquet에 저장
df.to_parquet(PARQUET_FILE, index=False)

print(f"Parquet written: {PARQUET_FILE}")


# 3. DuckDB 생성
con = duckdb.connect(str(DB_FILE))


# 4. Parquet를 직접 query
result = con.execute(
    f"""
    SELECT
        permno,
        date,
        ret
    FROM read_parquet('{PARQUET_FILE}')
    WHERE ret > 0
    ORDER BY date, permno
    """
).df()

print()
print("DuckDB query result:")
print(result)


# 5. schema 확인
print()
print("Parquet schema:")
print(
    con.execute(
        f"""
        DESCRIBE
        SELECT *
        FROM read_parquet('{PARQUET_FILE}')
        """
    ).df()
)

con.close()
