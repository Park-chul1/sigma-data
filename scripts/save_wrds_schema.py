from pathlib import Path
import wrds

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "metadata" / "schema"
OUT.mkdir(parents=True, exist_ok=True)

targets = [
    ("crsp", "stkdlysecurityprimarydata"),
    ("crsp", "stkdlysecuritydata"),
    ("crsp", "stksecurityinfohist"),
    ("crsp", "stkdelists"),
    ("crsp", "ccmxpf_linktable"),
    ("comp", "funda"),
    ("comp", "fundq"),
]

db = wrds.Connection()

for library, table in targets:
    print(f"\n=== {library}.{table} ===")

    schema = db.describe_table(
        library=library,
        table=table,
    )

    print(schema)

    path = OUT / f"{library}__{table}.csv"
    schema.to_csv(path, index=False)

    print(f"saved -> {path}")

db.close()
