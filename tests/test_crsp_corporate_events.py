from datetime import date
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import duckdb

from src.derive.crsp_corporate_events import build_period


class CorporateEventPeriodTest(unittest.TestCase):
    def _write_fixture(self, root: Path) -> tuple[Path, Path, Path]:
        con = duckdb.connect()
        daily = root / "daily.parquet"
        info = root / "info.parquet"
        events = root / "events.parquet"
        con.execute(
            f"""COPY (SELECT * FROM (VALUES
                (10001, DATE '2020-01-02', 10.0, 1000.0, 100.0, 0.10, 0.08, '', '', ''),
                (10001, DATE '2020-01-03', 5.0, 500.0, 50.0, -0.50, -0.50, 'Y', '', ''),
                (10002, DATE '2020-01-03', 8.0, 800.0, 80.0, NULL, NULL, 'Y', 'M', '')
            ) t(permno,date,price_raw,market_cap_kusd,volume_raw_shares,ret_total,
                ret_ex_div,delist_flag,ret_missing_flag,distribution_return_flag))
            TO '{daily}' (FORMAT PARQUET)"""
        )
        con.execute(
            f"""COPY (SELECT * FROM (VALUES
                (10001, DATE '2019-01-01', DATE '2020-01-03', 'AAA', 'N', 'EQTY', 'A'),
                (10002, DATE '2019-01-01', DATE '2020-01-03', 'BBB', 'Q', 'EQTY', 'A')
            ) t(permno,valid_from,valid_to,ticker,primary_exchange,security_type,trading_status_flag))
            TO '{info}' (FORMAT PARQUET)"""
        )
        con.execute(
            f"""COPY (SELECT * FROM (VALUES
                (10001, DATE '2020-01-03', -0.75, 'DLST', 'V', 'P', 'CASH', NULL, NULL),
                (10002, DATE '2020-01-03', NULL, 'DLST', 'V', 'P', NULL, NULL, NULL)
            ) t(permno,event_date,delisting_return,action_type,status_type,reason_type,
                payment_type,successor_permno,successor_permco))
            TO '{events}' (FORMAT PARQUET)"""
        )
        con.close()
        return daily, info, events

    def test_ciz_return_is_not_compounded_with_delisting_return(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            daily, info, events = self._write_fixture(root)
            output = root / "derived/part.parquet"
            manifest = build_period(
                daily_pattern=daily,
                security_info_file=info,
                delistings_file=events,
                output_file=output,
                requested_start=date(2020, 1, 3),
                requested_end=date(2020, 1, 3),
            )

            rows = duckdb.connect().execute(
                f"""SELECT permno, ret_total_backtest, delisting_return,
                           return_source, requires_review
                    FROM read_parquet('{output}') ORDER BY permno"""
            ).fetchall()
            self.assertEqual(rows[0], (10001, -0.5, -0.75, "CRSP_CIZ_DAILY", False))
            self.assertEqual(rows[1], (10002, None, None, "MISSING", True))
            self.assertEqual(manifest["rows"], 2)
            self.assertEqual(manifest["requires_review_rows"], 1)
            self.assertEqual(
                json.loads((output.parent / "_SUCCESS.json").read_text())["return_policy"],
                "CRSP_CIZ_DAILY",
            )

    def test_rejects_invalid_period(self) -> None:
        with self.assertRaisesRegex(ValueError, "on or before"):
            build_period(
                daily_pattern=Path("unused"),
                security_info_file=Path("unused"),
                delistings_file=Path("unused"),
                output_file=Path("unused"),
                requested_start=date(2020, 1, 2),
                requested_end=date(2020, 1, 1),
            )


if __name__ == "__main__":
    unittest.main()
