import wrds

db = wrds.Connection()

libraries = db.list_libraries()

targets = [
    lib for lib in libraries
    if ("crsp" in lib.lower() or "comp" in lib.lower())
]

print("\n=== Relevant libraries ===")
for lib in targets:
    print(lib)

print("\n=== Candidate tables ===")

for lib in targets:
    try:
        tables = db.list_tables(library=lib)

        interesting = [
            t for t in tables
            if any(
                key in t.lower()
                for key in [
                    "dsf",
                    "daily",
                    "stock",
                    "security",
                    "names",
                    "delist",
                    "link",
                    "funda",
                    "fundq",
                ]
            )
        ]

        if interesting:
            print(f"\n[{lib}]")
            for table in interesting[:50]:
                print("  ", table)

    except Exception as e:
        print(f"[{lib}] ERROR: {e}")

db.close()
