from pathlib import Path

import wrds


ROOT = Path(__file__).resolve().parents[1]

DAILY_OUT = (
    ROOT
    / "data/raw/crsp/ciz/stkdlysecurityprimarydata"
    / "year=2025/month=01/part-000.parquet"
)

INFO_OUT = (
    ROOT
    / "data/raw/crsp/ciz/stksecurityinfohist"
    / "sample/2025-01.parquet"
)

DELIST_OUT = (
    ROOT
    / "data/raw/crsp/ciz/stkdelists"
    / "sample/2025-01.parquet"
)

for path in [DAILY_OUT, INFO_OUT, DELIST_OUT]:
    path.parent.mkdir(parents=True, exist_ok=True)


db = wrds.Connection()


# --------------------------------------------------
# 1. Daily security data
# --------------------------------------------------

daily_sql = """
SELECT
    permno,
    dlycaldt,
    dlydelflg,
    dlyprc,
    dlyprcflg,
    dlycap,
    dlycapflg,
    dlyret,
    dlyretx,
    dlyretmissflg,
    dlydistretflg,
    dlyvol
FROM crsp.stkdlysecurityprimarydata
WHERE dlycaldt >= '2025-01-01'
  AND dlycaldt <  '2025-02-01'
ORDER BY dlycaldt, permno
"""

print("Downloading CRSP daily sample...")
daily = db.raw_sql(daily_sql)

print("daily rows:", len(daily))
print(daily.head())
print(daily.dtypes)

daily.to_parquet(DAILY_OUT, index=False)


# --------------------------------------------------
# 2. Point-in-time security information
# --------------------------------------------------

info_sql = """
SELECT
    permno,
    secinfostartdt,
    secinfoenddt,
    ticker,
    cusip,
    primaryexch,
    sharetype,
    securitytype,
    securitysubtype,
    usincflg,
    issuertype,
    tradingstatusflg,
    conditionaltype,
    shareclass
FROM crsp.stksecurityinfohist
WHERE secinfostartdt < '2025-02-01'
  AND COALESCE(secinfoenddt, DATE '9999-12-31') >= '2025-01-01'
ORDER BY permno, secinfostartdt
"""

print("\nDownloading security history sample...")
info = db.raw_sql(info_sql)

print("security info rows:", len(info))
print(info.head())

info.to_parquet(INFO_OUT, index=False)


# --------------------------------------------------
# 3. Delistings
# --------------------------------------------------

delist_sql = """
SELECT
    permno,
    delistingdt,
    delret,
    delactiontype,
    delstatustype,
    delreasontype,
    delpaymenttype,
    delpermno,
    delpermco
FROM crsp.stkdelists
WHERE delistingdt >= '2025-01-01'
  AND delistingdt <  '2025-02-01'
ORDER BY delistingdt, permno
"""

print("\nDownloading delisting sample...")
delist = db.raw_sql(delist_sql)

print("delisting rows:", len(delist))
print(delist.head())

delist.to_parquet(DELIST_OUT, index=False)


db.close()

print("\nDone.")
print("daily  :", DAILY_OUT)
print("info   :", INFO_OUT)
print("delist :", DELIST_OUT)
