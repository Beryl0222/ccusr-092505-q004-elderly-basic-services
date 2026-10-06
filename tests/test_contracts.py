from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from elderly_basic_services.contracts import validate_event


class ContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.schema = json.loads((ROOT / "contracts" / "domain.schema.json").read_text(encoding="utf-8"))
        cls.sample = json.loads((ROOT / "data" / "sample.json").read_text(encoding="utf-8"))

    def test_sample_is_valid(self) -> None:
        self.assertEqual([], validate_event(self.sample, self.schema))

    def test_missing_fields_are_reported_in_stable_order(self) -> None:
        issues = validate_event({}, self.schema)
        self.assertEqual(sorted(issue.field for issue in issues), [issue.field for issue in issues])
        self.assertIn("event_id", {issue.field for issue in issues})

    def test_naive_time_and_zero_version_are_rejected(self) -> None:
        payload = dict(self.sample, occurred_at="2026-09-24T12:00:00", version=0)
        codes = {(issue.field, issue.code) for issue in validate_event(payload, self.schema)}
        self.assertIn(("occurred_at", "timezone_required"), codes)
        self.assertIn(("version", "positive_integer"), codes)

    def test_unknown_event_type_is_rejected(self) -> None:
        payload = dict(self.sample, event_type="UNKNOWN")
        issues = validate_event(payload, self.schema)
        self.assertEqual([("event_type", "unsupported_value")], [(item.field, item.code) for item in issues])

    def test_emitted_events_satisfy_contract(self) -> None:
        from datetime import date

        from support import add_elder, build_network, dt, qualify

        net = build_network()
        add_elder(net, "elder-1")
        qualify(net, "elder-1", ["meal_service", "home_visit"], dt(1, 2))
        appointment_id = net.schedule_appointment("elder-1", "meal_service", date(2026, 2, 10), dt(2, 1))
        net.record_fulfillment(appointment_id, "cg-raw", dt(2, 10, 13), "已送达")
        net.escalate_risk("elder-1", "urgent", dt(2, 11, 8), "officer-1", referral_org="县医院")
        net.close_facility("mp-1", date(2026, 2, 12), date(2026, 2, 13), dt(2, 11, 9), "检修")
        net.ingest_report("org-1", "elder-1", "meal_service", date(2026, 2, 10), "已送餐", dt(2, 10, 18), performer_id="cg-raw")
        events = list(net.store.iter())
        self.assertGreater(len(events), 0)
        for event in events:
            self.assertEqual([], validate_event(event, self.schema), event.get("event_type"))


if __name__ == "__main__":
    unittest.main()
