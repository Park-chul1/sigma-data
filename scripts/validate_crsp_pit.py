from pathlib import Path

import duckdb


# --------------------------------------------------
# Project paths
# --------------------------------------------------

# scripts/validate_crsp_pit.py
#          ↓ parent
# scripts/
#          ↓ parent
# sigma-data/
ROOT = Path(__file__).resolve().parents[1]

DAILY_ROOT = (
    ROOT
    / "data"
    / "normalized"
    / "crsp"
    / "daily"
)

INFO_FILE = (
    ROOT
    / "data"
    / "normalized"
    / "crsp"
    / "security_info"
    / "part-000.parquet"
)

DAILY_PATTERN = (
    DAILY_ROOT
    / "year=*"
    / "month=*"
    / "part-000.parquet"
)


def main():
    # --------------------------------------------------
    # Filesystem validation
    # --------------------------------------------------

    daily_files = sorted(
        DAILY_ROOT.glob(
            "year=*/month=*/part-000.parquet"
        )
    )

    if not daily_files:
        raise RuntimeError(
            f"No normalized daily parquet files found "
            f"under {DAILY_ROOT}"
        )

    if not INFO_FILE.exists():
        raise RuntimeError(
            f"Security info parquet not found: "
            f"{INFO_FILE}"
        )

    print("=== FILES ===")
    print(f"project root     : {ROOT}")
    print(f"daily partitions : {len(daily_files)}")
    print(f"daily pattern    : {DAILY_PATTERN}")
    print(f"security info    : {INFO_FILE}")

    con = duckdb.connect()

    try:
        # --------------------------------------------------
        # Daily row count
        # --------------------------------------------------

        daily_rows = con.execute(
            f"""
            SELECT COUNT(*)
            FROM read_parquet('{DAILY_PATTERN}')
            """
        ).fetchone()[0]

        print("\n=== DAILY ===")
        print(f"daily_rows: {daily_rows:,}")

        # --------------------------------------------------
        # PIT join validation
        # --------------------------------------------------

        result = con.execute(
            f"""
            SELECT
                COUNT(*) AS joined_rows,

                COUNT(*) FILTER (
                    WHERE h.permno IS NULL
                ) AS unmatched_rows

            FROM read_parquet('{DAILY_PATTERN}') d

            LEFT JOIN read_parquet('{INFO_FILE}') h

              ON d.permno = h.permno

             AND d.date >= h.valid_from

             AND d.date <= COALESCE(
                    h.valid_to,
                    DATE '9999-12-31'
                 )
            """
        ).fetchone()

        joined_rows, unmatched_rows = result

        print("\n=== PIT JOIN ===")
        print(f"joined_rows   : {joined_rows:,}")
        print(f"unmatched_rows: {unmatched_rows:,}")

        # --------------------------------------------------
        # Critical invariant
        #
        # LEFT JOIN preserves unmatched daily rows.
        # Therefore joined_rows > daily_rows means that
        # at least one daily row matched multiple history
        # intervals.
        # --------------------------------------------------

        if joined_rows != daily_rows:
            raise RuntimeError(
                "PIT join changed row count: "
                f"daily={daily_rows:,}, "
                f"joined={joined_rows:,}. "
                "Possible overlapping security-info intervals."
            )

        print(
            "\n[PASS] PIT join produced no row explosion."
        )

        # --------------------------------------------------
        # Inspect unmatched observations
        # --------------------------------------------------

        if unmatched_rows > 0:
            print(
                "\n=== UNMATCHED DAILY OBSERVATIONS ==="
            )

            unmatched = con.execute(
                f"""
                WITH unmatched AS (
                    SELECT
                        d.permno,
                        d.date,
                        d.price_raw,
                        d.ret_total

                    FROM read_parquet(
                        '{DAILY_PATTERN}'
                    ) d

                    LEFT JOIN read_parquet(
                        '{INFO_FILE}'
                    ) h

                      ON d.permno = h.permno

                     AND d.date >= h.valid_from

                     AND d.date <= COALESCE(
                            h.valid_to,
                            DATE '9999-12-31'
                         )

                    WHERE h.permno IS NULL
                )

                SELECT
                    u.permno,
                    u.date,
                    u.price_raw,
                    u.ret_total,

                    COUNT(h.permno)
                        AS history_rows_for_permno,

                    MIN(h.valid_from)
                        AS earliest_history,

                    MAX(h.valid_to)
                        AS latest_history

                FROM unmatched u

                LEFT JOIN read_parquet(
                    '{INFO_FILE}'
                ) h
                  ON u.permno = h.permno

                GROUP BY
                    u.permno,
                    u.date,
                    u.price_raw,
                    u.ret_total

                ORDER BY
                    u.date,
                    u.permno
                """
            ).df()

            print(
                unmatched.to_string(index=False)
            )

            rate = (
                unmatched_rows
                / daily_rows
                * 100
            )

            print(
                f"\nunmatched rate: "
                f"{rate:.8f}%"
            )

        else:
            print(
                "\n[PASS] Every daily observation "
                "has PIT security metadata."
            )

    finally:
        con.close()


if __name__ == "__main__":
    main()
