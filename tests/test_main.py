
import sys
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from main import (
    allocate_proportionally,
    build_report,
    get_payment_amount_as_of,
)


class AllocationTests(unittest.TestCase):

    def test_proportional_allocation(self):
        works = [
            {"work_id": "W-1", "price": Decimal("200.00")},
            {"work_id": "W-2", "price": Decimal("300.00")},
        ]

        result = allocate_proportionally(
            Decimal("100.00"), works
        )

        self.assertEqual(result["W-1"], Decimal("40.00"))
        self.assertEqual(result["W-2"], Decimal("60.00"))

    def test_rounding_to_cents(self):
        works = [
            {"work_id": "W-1", "price": Decimal("1.00")},
            {"work_id": "W-2", "price": Decimal("1.00")},
            {"work_id": "W-3", "price": Decimal("1.00")},
        ]

        result = allocate_proportionally(
            Decimal("10.00"), works
        )

        self.assertEqual(
            sum(result.values()),
            Decimal("10.00")
        )

        self.assertEqual(
            sorted(result.values()),
            [
                Decimal("3.33"),
                Decimal("3.33"),
                Decimal("3.34"),
            ]
        )


class PaymentDateTests(unittest.TestCase):

    def test_paid_payment_after_report_date(self):
        payment = {
            "payment_id": "P-1",
            "status": "paid",
            "amount": Decimal("100.00"),
            "paid_at": date(2026, 11, 12),
            "refunded_at": None,
        }

        result = get_payment_amount_as_of(
            payment, date(2026, 11, 10)
        )

        self.assertEqual(result, Decimal("0.00"))

    def test_refunded_payment_before_report_date(self):
        payment = {
            "payment_id": "P-2",
            "status": "refunded",
            "amount": Decimal("50.00"),
            "paid_at": date(2026, 9, 2),
            "refunded_at": date(2026, 9, 10),
        }

        result = get_payment_amount_as_of(
            payment, date(2026, 11, 10)
        )

        self.assertEqual(result, Decimal("0.00"))

    def test_refund_after_report_date(self):
        payment = {
            "payment_id": "P-3",
            "status": "refunded",
            "amount": Decimal("50.00"),
            "paid_at": date(2026, 11, 5),
            "refunded_at": date(2026, 11, 12),
        }

        result = get_payment_amount_as_of(
            payment, date(2026, 11, 10)
        )

        self.assertEqual(result, Decimal("50.00"))

    def test_queued_payment(self):
        payment = {
            "payment_id": "P-4",
            "status": "queued",
            "amount": Decimal("150.00"),
            "paid_at": None,
            "refunded_at": None,
        }

        result = get_payment_amount_as_of(
            payment, date(2026, 11, 10)
        )

        self.assertEqual(result, Decimal("0.00"))


class ReportIntegrationTests(unittest.TestCase):

    def test_report_totals(self):
        work_report, contract_report, warnings = build_report(
            date(2026, 11, 10)
        )

        contracts = {
            row["contract_id"]: row
            for row in contract_report
        }

        self.assertEqual(
            contracts["D-1"]["paid_total"],
            Decimal("315000.00")
        )

        self.assertEqual(
            contracts["D-2"]["paid_total"],
            Decimal("75000.00")
        )

        self.assertEqual(
            contracts["D-1"]["work_remaining"],
            Decimal("185000.00")
        )

        self.assertEqual(
            contracts["D-2"]["work_remaining"],
            Decimal("15000.00")
        )

        self.assertEqual(
            contracts["D-1"]["cancelled_work_price"],
            Decimal("100000.00")
        )

        works = {
            row["work_id"]: row
            for row in work_report
        }

        self.assertEqual(
            works["W-23"]["status"],
            "planned"
        )

        self.assertEqual(
            works["W-13"]["remaining"],
            Decimal("0.00")
        )

        self.assertTrue(warnings)


if __name__ == "__main__":
    unittest.main()
