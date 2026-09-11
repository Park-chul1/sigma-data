from pathlib import Path

import duckdb


ROOT = Path(__file__).resolve().parents[1]

DAILY = (
    ROOT
    / "data"
    / "normalized"
    / "crsp"
    / "daily"
    / "year=*"
    / "month=*"
    / "part-000.parquet"
)

INFO = (
    ROOT
    / "data"
    / "normalized"
    / "crsp"
    / "security_info"
    / "part-000.parquet"
)

DELIST = (
    ROOT
    / "data"
    / "normalized"
    / "crsp"
    / "delistings"
    / "part-000.parquet"
)


def main():
    con = duckdb.connect()

    try:
        result = con.execute(
            f"""
            WITH unmatched AS (
                SELECT
                    d.permno,
                    d.date,
                    d.price_raw,
                    d.ret_total,
                    d.delist_flag

                FROM read_parquet('{DAILY}') d

                LEFT JOIN read_parquet('{INFO}') h
                  ON d.permno = h.permno
                 AND d.date >= h.valid_from
                 AND d.date <= COALESCE(
                        h.valid_to,
                        DATE '9999-12-31'
                     )

                WHERE h.permno IS NULL
            ),

            candidates AS (
                SELECT
                    u.*,

                    e.event_date,
                    e.delisting_return,
                    e.action_type,
                    e.status_type,
                    e.reason_type,
                    e.payment_type,
                    e.successor_permno,

                    DATE_DIFF(
                        'day',
                        e.event_date,
                        u.date
                    ) AS days_from_event,

                    ROW_NUMBER() OVER (
                        PARTITION BY
                            u.permno,
                            u.date
                        ORDER BY
                            ABS(
                                DATE_DIFF(
                                    'day',
                                    e.event_date,
                                    u.date
                                )
                            )
                    ) AS rn

                FROM unmatched u

                LEFT JOIN read_parquet('{DELIST}') e
                  ON u.permno = e.permno
            )

            SELECT
                permno,
                date AS daily_date,

                price_raw,
                ret_total,
                delist_flag,

                event_date,
                days_from_event,

                delisting_return,

                action_type,
                status_type,
                reason_type,
                payment_type,

                successor_permno

            FROM candidates

            WHERE rn = 1

            ORDER BY
                daily_date,
                permno
            """
        ).df()

        print(
            "=== RETURN-ONLY ROW ↔ NEAREST "
            "DELISTING EVENT ==="
        )

        print(
            result.to_string(index=False)
        )

    finally:
        con.close()


if __name__ == "__main__":
    main()