from __future__ import annotations

import unittest
from datetime import date

from support import T0, add_elder, build_network, dt, qualify

from elderly_basic_services.model import DomainError


class IngestionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.net = build_network()
        add_elder(self.net, "elder-1")
        qualify(self.net, "elder-1", ["meal_service"], dt(1, 2))
        self.net.assign_responsibility("org-1", "report_review", "reviewer-1", T0, T0)

    def ingest(self, org="org-1", outcome="已送餐", service_date=date(2026, 2, 10), received=None, **kw):
        return self.net.ingest_report(
            org, "elder-1", "meal_service", service_date, outcome,
            received or dt(2, 10, 18), performer_id="cg-raw", **kw,
        )

    def test_duplicate_returns_existing_result(self) -> None:
        first = self.ingest()
        self.assertEqual("accepted", first.status)
        self.assertFalse(first.late)
        second = self.ingest(received=dt(2, 12, 9))
        self.assertEqual("duplicate", second.status)
        self.assertEqual(first.record_id, second.record_id)
        self.assertTrue(second.late)  # 晚到的重复报送同样按业务键返回既有结果
        self.assertEqual(1, len(self.net.records))

    def test_conflict_goes_to_designated_reviewer_without_merging(self) -> None:
        first = self.ingest()
        conflicted = self.ingest(outcome="未送达")
        self.assertEqual("conflict", conflicted.status)
        conflict = self.net.conflicts[conflicted.conflict_id]
        self.assertEqual("reviewer-1", conflict.reviewer_id)
        self.assertEqual(first.record_id, conflict.existing_record_id)
        # 原记录不被静默合并
        self.assertEqual("已送餐", self.net.records[first.record_id].outcome)
        self.assertEqual(1, len(self.net.records))

    def test_explicit_report_key_is_used_as_business_key(self) -> None:
        first = self.ingest(report_key="org-1-report-0007")
        again = self.ingest(report_key="org-1-report-0007", service_date=date(2026, 2, 11))
        # 业务键相同、内容摘要不同（日期不同）应判为矛盾而非新记录
        self.assertEqual("conflict", again.status)
        self.assertEqual(1, len(self.net.records))
        self.assertIsNotNone(first.record_id)

    def test_report_without_consent_is_rejected(self) -> None:
        add_elder(self.net, "elder-2")
        self.net.assess_need("elder-2", "low", ["meal_service"], "assessor-1", dt(1, 2), dt(12, 31))
        with self.assertRaises(DomainError) as ctx:
            self.net.ingest_report(
                "org-1", "elder-2", "meal_service", date(2026, 2, 10), "已送餐",
                dt(2, 10, 18), performer_id="cg-raw",
            )
        self.assertIn(ctx.exception.code, {"entitlement_missing", "consent_missing"})

    def test_conflict_without_designated_reviewer_is_rejected(self) -> None:
        self.net.ingest_report(
            "org-2", "elder-1", "meal_service", date(2026, 2, 10), "已送餐",
            dt(2, 10, 18), performer_id="cg-raw",
        )
        with self.assertRaises(DomainError) as ctx:
            self.net.ingest_report(
                "org-2", "elder-1", "meal_service", date(2026, 2, 10), "未送达",
                dt(2, 10, 19), performer_id="cg-raw",
            )
        self.assertEqual("reviewer_missing", ctx.exception.code)


if __name__ == "__main__":
    unittest.main()
