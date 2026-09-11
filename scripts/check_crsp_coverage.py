import wrds

db = wrds.Connection()

for schema in [
    "crsp",
    "crsp_a_stock",
]:
    print(f"\n=== {schema} ===")

    result = db.raw_sql(f"""
        SELECT
            MIN(dlycaldt) AS min_date,
            MAX(dlycaldt) AS max_date,
            COUNT(*) AS total_rows
        FROM {schema}.stkdlysecurityprimarydata
    """)

    print(result)

db.close()