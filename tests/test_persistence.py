from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path

from support import add_elder, build_network, dt, qualify

from elderly_basic_services.model import ESC_OPEN, ORIGIN_EMERGENCY
from elderly_basic_services.network import ServiceNetwork
from elderly_basic_services.store import JsonlEventStore


class PersistenceTests(unittest.TestCase):
    def test_restart_preserves_visit_deadlines_and_escalation_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            net = build_network(JsonlEventStore(path))
            add_elder(net, "elder-1")
            qualify(net, "elder-1", ["meal_service"], dt(1, 2))
            visit_id = net.schedule_appointment(
                "elder-1", "meal_service", date(2026, 2, 10), dt(2, 1), due_at=dt(2, 12, 18)
            )
            add_elder(net, "elder-2")
            escalation_id = net.escalate_risk("elder-2", "urgent", dt(2, 10, 8), "officer-1", referral_org="县医院")
            seq_before = dict(net._seq)

            restored = ServiceNetwork.restore(JsonlEventStore(path))

            escalation = restored.escalations[escalation_id]
            self.assertEqual(ESC_OPEN, escalation.status)
            self.assertEqual(dt(2, 11, 8), escalation.response_due_at)
            self.assertTrue(escalation.materials_pending)
            self.assertEqual("县医院", escalation.referral_org)
            self.assertEqual(dt(2, 12, 18), restored.appointments[visit_id].due_at)
            emergency = next(
                item for item in restored.appointments.values() if item.origin == ORIGIN_EMERGENCY
            )
            self.assertEqual(dt(2, 11, 8), emergency.due_at)
            self.assertEqual(seq_before, restored._seq)
            # 重启后流程可继续：紧急上门履约、补材料、结案
            record_id = restored.record_fulfillment(
                emergency.appointment_id, "cg-raw", dt(2, 10, 10), "已上门",
                {"condition_note": "意识清醒"},
            )
            self.assertIn(record_id, restored.records)
            qualify(restored, "elder-2", ["home_visit"], dt(2, 10, 21))
            restored.complete_materials(escalation_id, dt(2, 10, 22))
            restored.resolve_escalation(escalation_id, dt(2, 10, 23), "已处置")
            # 新事件序号在重放基础上继续递增
            again = ServiceNetwork.restore(JsonlEventStore(path))
            self.assertEqual(
                restored._seq["elder-2"], again._seq["elder-2"]
            )

    def test_reports_registry_survives_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            net = build_network(JsonlEventStore(path))
            add_elder(net, "elder-1")
            qualify(net, "elder-1", ["meal_service"], dt(1, 2))
            first = net.ingest_report(
                "org-1", "elder-1", "meal_service", date(2026, 2, 10), "已送餐",
                dt(2, 10, 18), performer_id="cg-raw",
            )
            restored = ServiceNetwork.restore(JsonlEventStore(path))
            second = restored.ingest_report(
                "org-1", "elder-1", "meal_service", date(2026, 2, 10), "已送餐",
                dt(2, 12, 9), performer_id="cg-raw",
            )
            self.assertEqual("duplicate", second.status)
            self.assertEqual(first.record_id, second.record_id)
            self.assertEqual(1, len(restored.records))


if __name__ == "__main__":
    unittest.main()
