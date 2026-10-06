from __future__ import annotations

import copy
import unittest
from datetime import date

from support import T0, add_elder, build_network, dt, qualify

from elderly_basic_services.model import (
    APPT_CANCELLED,
    APPT_FULFILLED,
    APPT_PLANNED,
    APPT_REASSIGNED,
    APPT_UNASSIGNED,
    ESC_OPEN,
    ESC_RESOLVED,
    ORIGIN_EMERGENCY,
    DomainError,
)


class FulfillmentContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.net = build_network()
        add_elder(self.net, "elder-1")
        qualify(self.net, "elder-1", ["meal_service", "home_visit", "nursing_care"], dt(1, 2))

    def test_fulfillment_snapshots_policy_and_consent(self) -> None:
        appointment_id = self.net.schedule_appointment(
            "elder-1", "meal_service", date(2026, 2, 10), dt(2, 1), facility_id="mp-1"
        )
        record_id = self.net.record_fulfillment(appointment_id, "cg-raw", dt(2, 10, 13), "已送达")
        record = self.net.records[record_id]
        self.assertEqual("P2026", record.policy_version)
        self.assertEqual("consent-0001", record.consent_id)
        self.assertFalse(record.emergency)
        self.assertEqual(APPT_FULFILLED, self.net.appointments[appointment_id].status)

    def test_fulfillment_rejected_outside_policy_window(self) -> None:
        self.net.publish_catalog("temp_meal", "临时助餐", "meal", "P2025", T0, T0, effective_to=dt(3, 1))
        qualify(self.net, "elder-1", ["temp_meal"], dt(1, 3), consent_scopes=["meal"])
        appointment_id = self.net.schedule_appointment("elder-1", "temp_meal", date(2026, 2, 20), dt(2, 1))
        with self.assertRaises(DomainError) as ctx:
            self.net.record_fulfillment(appointment_id, "cg-raw", dt(3, 5), "已送达")
        self.assertEqual("policy_inactive", ctx.exception.code)

    def test_fulfillment_rejected_after_consent_expires(self) -> None:
        net = build_network()
        add_elder(net, "elder-2")
        assessment_id = net.assess_need("elder-2", "low", ["meal_service"], "assessor-1", dt(1, 2), dt(12, 31))
        net.grant_entitlement("elder-2", ["meal_service"], dt(1, 2), assessment_id, dt(1, 2))
        net.grant_consent("elder-2", "self", ["meal"], dt(1, 2), dt(1, 2), effective_to=dt(2, 15))
        appointment_id = net.schedule_appointment("elder-2", "meal_service", date(2026, 2, 10), dt(2, 1))
        with self.assertRaises(DomainError) as ctx:
            net.record_fulfillment(appointment_id, "cg-raw", dt(2, 20), "已送达")
        self.assertEqual("consent_missing", ctx.exception.code)

    def test_schedule_requires_entitlement_and_consent(self) -> None:
        net = build_network()
        add_elder(net, "elder-3")
        with self.assertRaises(DomainError) as ctx:
            net.schedule_appointment("elder-3", "meal_service", date(2026, 2, 10), dt(2, 1))
        self.assertEqual("entitlement_missing", ctx.exception.code)

    def test_qualification_checked_at_schedule_and_fulfillment(self) -> None:
        with self.assertRaises(DomainError) as ctx:
            self.net.schedule_appointment(
                "elder-1", "nursing_care", date(2026, 2, 10), dt(2, 1), caregiver_id="cg-raw"
            )
        self.assertEqual("qualification_missing", ctx.exception.code)
        appointment_id = self.net.schedule_appointment("elder-1", "nursing_care", date(2026, 2, 10), dt(2, 1))
        with self.assertRaises(DomainError) as ctx:
            self.net.record_fulfillment(appointment_id, "cg-raw", dt(2, 10, 15), "已照护")
        self.assertEqual("qualification_missing", ctx.exception.code)
        record_id = self.net.record_fulfillment(appointment_id, "cg-1", dt(2, 10, 15), "已照护")
        self.assertIn(record_id, self.net.records)

    def test_facility_capacity_is_enforced(self) -> None:
        self.net.register_facility("mp-small", "village-1", "meal_point", 1, T0)
        add_elder(self.net, "elder-4")
        qualify(self.net, "elder-4", ["meal_service"], dt(1, 2))
        self.net.schedule_appointment("elder-1", "meal_service", date(2026, 2, 10), dt(2, 1), facility_id="mp-small")
        with self.assertRaises(DomainError) as ctx:
            self.net.schedule_appointment("elder-4", "meal_service", date(2026, 2, 10), dt(2, 1), facility_id="mp-small")
        self.assertEqual("capacity_exceeded", ctx.exception.code)

    def test_duplicate_elder_registration_returns_existing(self) -> None:
        again = self.net.register_elder(
            "elder-1-copy", "老人elder-1", "living_alone", "village-1", "village-2", "139elder-1", dt(1, 3)
        )
        self.assertEqual("elder-1", again)
        self.assertEqual(1, len(self.net.elders))


class EmergencyFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.net = build_network()
        add_elder(self.net, "elder-1")

    def test_urgent_escalation_visits_first_and_defers_materials(self) -> None:
        escalation_id = self.net.escalate_risk("elder-1", "urgent", dt(2, 10, 8), "officer-1", referral_org="县医院")
        escalation = self.net.escalations[escalation_id]
        self.assertTrue(escalation.materials_pending)
        self.assertEqual(("assessment", "entitlement", "consent"), escalation.missing_materials)
        self.assertEqual(dt(2, 11, 8), escalation.response_due_at)
        self.assertEqual("县医院", escalation.referral_org)
        (appointment,) = self.net.appointments.values()
        self.assertEqual(ORIGIN_EMERGENCY, appointment.origin)
        self.assertEqual(dt(2, 11, 8), appointment.due_at)
        # 材料未补齐不能结案
        with self.assertRaises(DomainError) as ctx:
            self.net.resolve_escalation(escalation_id, dt(2, 10, 20), "已处置")
        self.assertEqual("materials_pending", ctx.exception.code)
        with self.assertRaises(DomainError) as ctx:
            self.net.complete_materials(escalation_id, dt(2, 10, 20))
        self.assertEqual("materials_incomplete", ctx.exception.code)
        self.assertEqual(["assessment", "entitlement", "consent"], ctx.exception.details["missing"])
        # 补齐材料后结案
        qualify(self.net, "elder-1", ["home_visit"], dt(2, 10, 21))
        self.net.complete_materials(escalation_id, dt(2, 10, 22))
        self.assertFalse(escalation.materials_pending)
        self.net.resolve_escalation(escalation_id, dt(2, 10, 23), "已处置")
        self.assertEqual(ESC_RESOLVED, escalation.status)

    def test_emergency_fulfillment_respects_privacy_boundary(self) -> None:
        self.net.escalate_risk("elder-1", "urgent", dt(2, 10, 8), "officer-1")
        (appointment,) = self.net.appointments.values()
        # 紧急记录不允许夹带评估等敏感明细
        with self.assertRaises(DomainError) as ctx:
            self.net.record_fulfillment(
                appointment.appointment_id, "cg-raw", dt(2, 10, 10), "已上门",
                {"needs_detail": "失能评估细节"},
            )
        self.assertEqual("privacy_boundary", ctx.exception.code)
        self.assertEqual(["needs_detail"], ctx.exception.details["rejected_fields"])
        record_id = self.net.record_fulfillment(
            appointment.appointment_id, "cg-raw", dt(2, 10, 10), "已上门",
            {"condition_note": "意识清醒", "referral_org": "县医院"},
        )
        record = self.net.records[record_id]
        self.assertTrue(record.emergency)
        self.assertIsNone(record.consent_id)

    def test_second_escalation_while_open_is_rejected(self) -> None:
        self.net.escalate_risk("elder-1", "high", dt(2, 10, 8), "officer-1")
        with self.assertRaises(DomainError) as ctx:
            self.net.escalate_risk("elder-1", "urgent", dt(2, 10, 9), "officer-1")
        self.assertEqual("escalation_open", ctx.exception.code)
        escalation = next(iter(self.net.escalations.values()))
        self.assertEqual(ESC_OPEN, escalation.status)
        self.assertEqual(dt(2, 13, 8), escalation.response_due_at)


class FutureOnlyChangeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.net = build_network()
        add_elder(self.net, "elder-1")
        qualify(self.net, "elder-1", ["meal_service"], dt(1, 2))
        past = self.net.schedule_appointment("elder-1", "meal_service", date(2026, 2, 1), dt(1, 20), facility_id="mp-1")
        self.net.record_fulfillment(past, "cg-raw", dt(2, 1, 13), "已送达", {"menu": "米饭"})
        self.past_record_id = f"rec-{past}"
        self.future_id = self.net.schedule_appointment(
            "elder-1", "meal_service", date(2026, 3, 1), dt(2, 5), facility_id="mp-1"
        )
        self.record_before = copy.deepcopy(self.net.records[self.past_record_id].__dict__)

    def test_relocate_cancels_only_future_arrangements(self) -> None:
        cancelled = self.net.relocate_elder("elder-1", "village-3", dt(2, 10))
        self.assertEqual([self.future_id], cancelled)
        appointment = self.net.appointments[self.future_id]
        self.assertEqual(APPT_CANCELLED, appointment.status)
        self.assertEqual("relocated", appointment.cancel_reason)
        elder = self.net.elders["elder-1"]
        self.assertEqual("village-3", elder.residence_village_id)
        self.assertEqual("village-1", elder.residence_history[0][0])
        self.assertEqual("village-3", elder.residence_history[1][0])
        # 已发生的记录不被覆盖
        self.assertEqual(self.record_before, self.net.records[self.past_record_id].__dict__)

    def test_withdraw_consent_cancels_only_future_arrangements(self) -> None:
        cancelled = self.net.withdraw_consent("consent-0001", dt(2, 10))
        self.assertEqual([self.future_id], cancelled)
        self.assertEqual("consent_withdrawn", self.net.appointments[self.future_id].cancel_reason)
        self.assertEqual(self.record_before, self.net.records[self.past_record_id].__dict__)
        with self.assertRaises(DomainError) as ctx:
            self.net.schedule_appointment("elder-1", "meal_service", date(2026, 3, 5), dt(2, 11))
        self.assertEqual("consent_missing", ctx.exception.code)


class FacilityClosureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.net = build_network()
        add_elder(self.net, "elder-1")
        qualify(self.net, "elder-1", ["meal_service", "nursing_care"], dt(1, 2))
        self.a1 = self.net.schedule_appointment("elder-1", "meal_service", date(2026, 2, 10), dt(2, 1), facility_id="mp-1")
        self.a2 = self.net.schedule_appointment("elder-1", "meal_service", date(2026, 2, 11), dt(2, 1), facility_id="mp-1")
        self.a3 = self.net.schedule_appointment("elder-1", "meal_service", date(2026, 2, 15), dt(2, 1), facility_id="mp-1")
        self.a4 = self.net.schedule_appointment("elder-1", "meal_service", date(2026, 2, 10), dt(2, 1), facility_id="mp-3")
        self.a5 = self.net.schedule_appointment(
            "elder-1", "nursing_care", date(2026, 2, 10), dt(2, 1), facility_id="nh-1", caregiver_id="cg-1"
        )

    def test_closure_reassigns_only_affected_dates(self) -> None:
        result = self.net.close_facility("mp-1", date(2026, 2, 10), date(2026, 2, 11), dt(2, 9), "线路检修")
        self.assertEqual(sorted([self.a1, self.a2]), sorted(result["reassigned"]))
        self.assertEqual([], result["unassigned"])
        # 停业区间内的预约改派到同乡其他助餐点
        self.assertEqual("mp-2", self.net.appointments[self.a1].facility_id)
        self.assertEqual("mp-2", self.net.appointments[self.a2].facility_id)
        self.assertEqual(APPT_REASSIGNED, self.net.appointments[self.a1].status)
        # 区间外、其他村、护理床位的计划继续运行
        self.assertEqual("mp-1", self.net.appointments[self.a3].facility_id)
        self.assertEqual(APPT_PLANNED, self.net.appointments[self.a3].status)
        self.assertEqual("mp-3", self.net.appointments[self.a4].facility_id)
        self.assertEqual("nh-1", self.net.appointments[self.a5].facility_id)
        self.assertEqual(APPT_PLANNED, self.net.appointments[self.a5].status)

    def test_closure_without_fallback_marks_unassigned(self) -> None:
        net = build_network()
        add_elder(net, "elder-9")
        qualify(net, "elder-9", ["meal_service"], dt(1, 2))
        a1 = net.schedule_appointment("elder-9", "meal_service", date(2026, 2, 10), dt(2, 1), facility_id="mp-1")
        a2 = net.schedule_appointment("elder-9", "meal_service", date(2026, 2, 11), dt(2, 1), facility_id="mp-1")
        net.close_facility("mp-2", date(2026, 2, 10), date(2026, 2, 11), dt(2, 9), "停业")
        net.close_facility("mp-3", date(2026, 2, 10), date(2026, 2, 11), dt(2, 9), "停业")
        result = net.close_facility("mp-1", date(2026, 2, 10), date(2026, 2, 11), dt(2, 9), "停业")
        self.assertEqual([], result["reassigned"])
        self.assertEqual(sorted([a1, a2]), sorted(result["unassigned"]))
        self.assertEqual(APPT_UNASSIGNED, net.appointments[a1].status)


if __name__ == "__main__":
    unittest.main()
