from __future__ import annotations

import unittest
from datetime import date

from support import add_elder, build_network, dt, qualify

from elderly_basic_services.projections import coverage_report


class CoverageReportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.net = build_network()
        add_elder(self.net, "elder-a", village="village-1")
        add_elder(self.net, "elder-b", village="village-1")  # 未获资格：覆盖缺口
        add_elder(self.net, "elder-c", village="village-3")
        qualify(self.net, "elder-a", ["meal_service", "home_visit"], dt(1, 2))
        qualify(self.net, "elder-c", ["meal_service"], dt(1, 2))
        meal = self.net.schedule_appointment("elder-a", "meal_service", date(2026, 2, 10), dt(2, 1), facility_id="mp-1")
        self.net.record_fulfillment(meal, "cg-raw", dt(2, 10, 13), "已送达")
        self.overdue_visit = self.net.schedule_appointment(
            "elder-a", "home_visit", date(2026, 2, 4), dt(2, 1), due_at=dt(2, 5)
        )
        self.net.escalate_risk("elder-c", "high", dt(2, 18, 9), "officer-1")
        self.now = dt(2, 20)

    def test_county_report_rolls_up_villages(self) -> None:
        report = coverage_report(self.net, "county-1", date(2026, 2, 1), date(2026, 2, 28), self.now)
        self.assertEqual(3, report["elders_total"])
        self.assertEqual(2, report["entitled"])
        self.assertEqual(["elder-b"], report["coverage_gap"])
        self.assertEqual(1, report["fulfillments_real"])  # 以履约记录为准
        self.assertEqual([self.overdue_visit], report["overdue_visits"])
        self.assertEqual(1, len(report["open_escalations"]))
        self.assertEqual(["town-1", "town-2"], [child["node_id"] for child in report["children"]])

    def test_village_report_shows_local_gap_and_inherited_responsibility(self) -> None:
        report = coverage_report(self.net, "village-1", date(2026, 2, 1), date(2026, 2, 28), self.now)
        self.assertEqual(2, report["elders_total"])
        self.assertEqual(1, report["entitled"])
        self.assertEqual(["elder-b"], report["coverage_gap"])
        # 责任分工沿层级向上兜底：村里没有助餐责任人时取乡级
        self.assertEqual("officer-town1-meal", report["responsibility"]["meal_service"])
        self.assertEqual("officer-v1-visit", report["responsibility"]["home_visit"])
        self.assertEqual(1, report["appointments"]["fulfilled"])

    def test_completion_counts_come_from_records_not_appointment_status(self) -> None:
        report = coverage_report(self.net, "village-3", date(2026, 2, 1), date(2026, 2, 28), self.now)
        self.assertEqual(1, report["elders_total"])
        self.assertEqual(0, report["fulfillments_real"])
        self.assertEqual(0, report["appointments"]["scheduled"])


if __name__ == "__main__":
    unittest.main()
