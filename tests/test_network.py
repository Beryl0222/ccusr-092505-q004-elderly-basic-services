from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from elderly_basic_services import (
    DomainError,
    EligibilityNetwork,
    EventStore,
    Viewer,
    caregiver_view,
    elder_view,
    hierarchy_view,
    responder_view,
)

CST = datetime.fromisoformat("2026-01-01T00:00:00+08:00").tzinfo


class NetworkTestBase(unittest.TestCase):
    now = datetime(2026, 10, 6, 9, 0, tzinfo=CST)

    def _build(self, store: EventStore) -> EligibilityNetwork:
        clock = lambda: self.now
        return EligibilityNetwork(store, clock=clock, reviewers={"visit": "reviewer-zhao"})

    def _bootstrap_basic_services(self, net: EligibilityNetwork) -> None:
        """政策、老人、授权、评估、资格、设施、护理员的标准联调数据。"""
        net.publish_policy(
            "meal_dining", "老年助餐", "basic_living", date(2026, 1, 1),
            cadence_days=7, required_for_categories=("empty_nest", "living_alone", "left_behind"),
            responsibilities={"village": "村集体排摸与下单", "township": "乡镇督导助餐点", "county": "县民政资金与考核"},
        )
        net.publish_policy(
            "visit_care", "探访关爱", "visit", date(2026, 1, 1),
            cadence_days=30, required_for_categories=("living_alone", "left_behind"),
            responsibilities={"village": "村级周探访", "township": "乡镇月复核", "county": "县民政统筹"},
        )
        net.register_elder(
            "e-001", "王兰", "甲县/东乡/河东村", identity_number="ID-001",
            categories=("living_alone", "empty_nest"), contacts={"phone": "13800000001"},
        )
        net.register_elder(
            "e-002", "李桂", "甲县/西乡/河西村", identity_number="ID-002",
            categories=("left_behind",), contacts={"phone": "13800000002"},
        )
        net.grant_consent(
            "e-001", "g-self-meal", "service_delivery", "self",
            service_codes=("meal_dining", "visit_care"), valid_from=date(2026, 1, 1),
        )
        net.grant_consent(
            "e-001", "g-agent-admin", "administration", "written_agent",
            service_codes=("meal_dining",), grantee_id="son-chen", grantee_role="family_agent",
            delegated_by="e-001", valid_from=date(2026, 1, 1),
        )
        net.assess_need(
            "e-001", "a-001", "assessor-li", service_codes=("meal_dining", "visit_care"),
            risk_level="medium", visit_due_by=date(2026, 10, 31), valid_until=date(2027, 10, 1),
        )
        net.grant_entitlement("e-001", "meal_dining", "ent-001", date(2026, 1, 1), assessment_id="a-001")
        net.grant_entitlement("e-001", "visit_care", "ent-002", date(2026, 1, 1), assessment_id="a-001")
        net.register_facility("f-meal-east", "河东助餐点", "meal_station", "甲县/东乡/河东村", 2,
                              service_codes=("meal_dining",))
        net.register_facility("f-meal-west", "河西助餐点", "meal_station", "甲县/西乡/河西村", 3,
                              service_codes=("meal_dining",))
        net.register_facility("f-bed-central", "县中心护理院", "care_bed", "甲县/城关镇/中心社区", 5,
                              service_codes=("care_bed",))
        net.register_caregiver(
            "cg-01", "护工周敏", "甲县/东乡/河东村", facility_id="f-meal-east",
            service_codes=("meal_dining", "visit_care", "emergency_visit"),
            qualifications=[{"certificate": "养老护理初级", "service_codes": ["meal_dining", "visit_care", "emergency_visit"],
                             "valid_from": "2026-01-01"}],
        )
        net.register_caregiver(
            "cg-02", "护工吴强", "甲县/西乡/河西村", facility_id="f-meal-west",
            service_codes=("meal_dining", "emergency_visit"),
            qualifications=[{"certificate": "养老护理初级", "service_codes": ["meal_dining", "emergency_visit"],
                             "valid_from": "2026-01-01"}],
        )

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "events.jsonl"
        self.network = self._build(EventStore(self.path))
        self.today = date(2026, 10, 6)
        self._bootstrap_basic_services(self.network)


class LifecycleTests(NetworkTestBase):
    def test_full_delivery_snapshots_policy_and_consent_in_effect(self) -> None:
        day = datetime(2026, 10, 10, 11, 30, tzinfo=CST)
        self.network.request_service("apt-1", "e-001", "meal_dining", day, requested_by="son-chen")
        self.network.schedule_appointment("apt-1", day, facility_id="f-meal-east", caregiver_id="cg-01")
        self.network.assign_visit("apt-1", "cg-01")
        self.network.fulfill("apt-1", "v-1", fulfilled_at=day, note="送餐到户")
        visit = self.network.state.visits[-1]
        snap = visit["snapshots"]
        self.assertEqual(1, snap["policy_version"])
        self.assertEqual("ent-001", snap["entitlement_id"])
        self.assertIn("g-self-meal", snap["grant_ids"])
        self.assertEqual("甲县/东乡/河东村", snap["residence_village"])
        self.assertEqual("养老护理初级", snap["caregiver_qualification"])

    def test_request_without_consent_is_rejected(self) -> None:
        net = EligibilityNetwork(EventStore(), clock=lambda: self.now)
        self._bootstrap_basic_services(net)
        net.register_elder("e-009", "无授权老人", "甲县/东乡/河东村", categories=("living_alone",))
        net.grant_entitlement("e-009", "visit_care", "ent-009", date(2026, 10, 1))
        with self.assertRaises(DomainError) as caught:
            net.request_service("apt-x", "e-009", "visit_care", datetime(2026, 10, 10, tzinfo=CST))
        self.assertEqual("consent_required", caught.exception.code)

    def test_family_agent_requires_delegation(self) -> None:
        day = datetime(2026, 10, 10, 11, 30, tzinfo=CST)
        with self.assertRaises(DomainError) as caught:
            self.network.request_service("apt-bad", "e-001", "meal_dining", day, requested_by="stranger")
        self.assertEqual("agency_required", caught.exception.code)
        # 有书面代办授权的家属可以代下单。
        self.network.request_service("apt-ok", "e-001", "meal_dining", day, requested_by="son-chen")
        self.assertEqual("requested", self.network.state.appointments["apt-ok"].status)

    def test_written_agent_grant_must_record_principal(self) -> None:
        with self.assertRaises(DomainError) as caught:
            self.network.grant_consent(
                "e-002", "g-x", "administration", "written_agent", grantee_id="nephew",
                service_codes=("meal_dining",),
            )
        self.assertEqual("delegation_required", caught.exception.code)

    def test_verbal_consent_requires_witness(self) -> None:
        with self.assertRaises(DomainError) as caught:
            self.network.grant_consent("e-002", "g-v", "service_delivery", "verbal_witnessed")
        self.assertEqual("witness_required", caught.exception.code)


class TemporalBoundaryTests(NetworkTestBase):
    def test_visit_uses_policy_version_effective_at_service_time(self) -> None:
        # 10-01 履约按 v1；11-01 新版政策生效，11-02 履约按 v2。
        self.network.publish_policy(
            "meal_dining", "老年助餐（提标）", "basic_living", date(2026, 11, 1),
            policy_version=2, cadence_days=7, required_for_categories=("empty_nest", "living_alone", "left_behind"),
        )
        d1 = datetime(2026, 10, 20, 11, 0, tzinfo=CST)
        d2 = datetime(2026, 11, 2, 11, 0, tzinfo=CST)
        self.network.request_service("apt-v1", "e-001", "meal_dining", d1)
        self.network.schedule_appointment("apt-v1", d1, facility_id="f-meal-east", caregiver_id="cg-01")
        self.network.fulfill("apt-v1", "v-v1", fulfilled_at=d1)
        self.network.request_service("apt-v2", "e-001", "meal_dining", d2)
        self.network.schedule_appointment("apt-v2", d2, facility_id="f-meal-east", caregiver_id="cg-01")
        self.network.fulfill("apt-v2", "v-v2", fulfilled_at=d2)
        self.assertEqual(1, self.network.state.visits[0]["snapshots"]["policy_version"])
        self.assertEqual(2, self.network.state.visits[1]["snapshots"]["policy_version"])

    def test_relocation_only_affects_future_arrangements(self) -> None:
        past = datetime(2026, 10, 1, 11, 0, tzinfo=CST)
        future = datetime(2026, 10, 20, 11, 0, tzinfo=CST)
        self.network.request_service("apt-old", "e-001", "meal_dining", past)
        self.network.schedule_appointment("apt-old", past, facility_id="f-meal-east", caregiver_id="cg-01")
        self.network.fulfill("apt-old", "v-old", fulfilled_at=past)
        self.network.request_service("apt-future", "e-001", "meal_dining", future)
        self.network.schedule_appointment("apt-future", future, facility_id="f-meal-east", caregiver_id="cg-01")
        # 10-15 起跨村居住到西乡河西村。
        self.network.relocate("e-001", "甲县/西乡/河西村", date(2026, 10, 15))
        self.network.fulfill("apt-future", "v-new", fulfilled_at=future)
        self.assertEqual("甲县/东乡/河东村", self.network.state.visits[0]["snapshots"]["residence_village"])
        self.assertEqual("甲县/西乡/河西村", self.network.state.visits[1]["snapshots"]["residence_village"])
        # 改派候选取现住村设施。
        alt, caregiver, mode = self.network._alternative_for(
            self.network.state.appointments["apt-future"], date(2026, 10, 20)
        )
        self.assertEqual("f-meal-west", alt)
        self.assertEqual("onsite", mode)

    def test_withdrawn_consent_blocks_future_but_keeps_history(self) -> None:
        past = datetime(2026, 10, 1, 11, 0, tzinfo=CST)
        future = datetime(2026, 10, 20, 11, 0, tzinfo=CST)
        self.network.request_service("apt-done", "e-001", "meal_dining", past)
        self.network.schedule_appointment("apt-done", past, facility_id="f-meal-east", caregiver_id="cg-01")
        self.network.fulfill("apt-done", "v-done", fulfilled_at=past)
        # 未来安排先排好；撤回后履约被拦截（撤回只影响未来）。
        self.network.request_service("apt-blocked", "e-001", "meal_dining", future)
        self.network.schedule_appointment("apt-blocked", future, facility_id="f-meal-east", caregiver_id="cg-01")
        self.network.withdraw_consent("e-001", "g-self-meal", reason="本人要求停止")
        with self.assertRaises(DomainError) as caught:
            self.network.fulfill("apt-blocked", "v-blocked", fulfilled_at=future)
        self.assertEqual("consent_required", caught.exception.code)
        # 历史探访记录原样保留。
        self.assertEqual("v-done", self.network.state.visits[0]["visit_id"])

    def test_revoked_entitlement_only_affects_future(self) -> None:
        past = datetime(2026, 10, 1, 11, 0, tzinfo=CST)
        self.network.request_service("apt-hist", "e-001", "meal_dining", past)
        self.network.schedule_appointment("apt-hist", past, facility_id="f-meal-east", caregiver_id="cg-01")
        self.network.fulfill("apt-hist", "v-hist", fulfilled_at=past)
        self.network.revoke_entitlement("e-001", "meal_dining", effective_from=date(2026, 10, 15), reason="迁出保障")
        self.assertIsNotNone(self.network.state.entitlement_on("e-001", "meal_dining", date(2026, 10, 1)))
        self.assertIsNone(self.network.state.entitlement_on("e-001", "meal_dining", date(2026, 10, 16)))
        self.assertEqual(1, len(self.network.state.visits))


class FacilityTests(NetworkTestBase):
    def _bed_entitlement(self) -> None:
        self.network.publish_policy(
            "care_bed", "机构照护床位", "care", date(2026, 1, 1),
            required_for_categories=("living_alone",),
        )
        self.network.grant_consent(
            "e-001", "g-bed", "service_delivery", "self",
            service_codes=("care_bed",), valid_from=date(2026, 1, 1),
        )
        self.network.grant_entitlement("e-001", "care_bed", "ent-bed", date(2026, 10, 1))
        self.network.register_caregiver(
            "cg-03", "护工郑梅", "甲县/城关镇/中心社区", facility_id="f-bed-central",
            service_codes=("care_bed",),
            qualifications=[{"certificate": "养老护理中级", "service_codes": ["care_bed"], "valid_from": "2026-01-01"}],
        )

    def test_suspension_reassigns_only_affected_dates(self) -> None:
        inside = datetime(2026, 10, 11, 11, 0, tzinfo=CST)
        outside = datetime(2026, 10, 20, 11, 0, tzinfo=CST)
        self.network.request_service("apt-in", "e-001", "meal_dining", inside)
        self.network.schedule_appointment("apt-in", inside, facility_id="f-meal-east", caregiver_id="cg-01")
        self.network.request_service("apt-out", "e-001", "meal_dining", outside)
        self.network.schedule_appointment("apt-out", outside, facility_id="f-meal-east", caregiver_id="cg-01")
        self.network.suspend_facility(
            "f-meal-east", date(2026, 10, 10), date_to=date(2026, 10, 12), reason="厨房检修"
        )
        self.assertEqual("f-meal-west", self.network.state.appointments["apt-in"].facility_id)
        self.assertEqual("f-meal-east", self.network.state.appointments["apt-out"].facility_id)
        self.assertEqual("cg-02", self.network.state.appointments["apt-in"].caregiver_id)

    def test_bed_plan_and_other_village_plan_continue_during_meal_suspension(self) -> None:
        self._bed_entitlement()
        meal_day = datetime(2026, 10, 11, 11, 0, tzinfo=CST)
        bed_day = datetime(2026, 10, 11, 15, 0, tzinfo=CST)
        self.network.request_service("apt-bed", "e-001", "care_bed", bed_day)
        self.network.schedule_appointment("apt-bed", bed_day, facility_id="f-bed-central", caregiver_id="cg-03")
        # 河西村自己的助餐计划（设施在西乡）不受河东助餐点停业影响。
        self.network.grant_consent(
            "e-002", "g-2", "service_delivery", "verbal_witnessed",
            service_codes=("meal_dining",), witness="village-head", valid_from=date(2026, 1, 1),
        )
        self.network.assess_need("e-002", "a-002", "assessor-li", service_codes=("meal_dining",), risk_level="low")
        self.network.grant_entitlement("e-002", "meal_dining", "ent-2", date(2026, 10, 1), assessment_id="a-002")
        self.network.request_service("apt-west", "e-002", "meal_dining", meal_day)
        self.network.schedule_appointment("apt-west", meal_day, facility_id="f-meal-west", caregiver_id="cg-02")
        self.network.suspend_facility("f-meal-east", date(2026, 10, 10), date_to=date(2026, 10, 12), reason="厨房检修")
        self.assertEqual("scheduled", self.network.state.appointments["apt-bed"].status)
        self.assertEqual("f-bed-central", self.network.state.appointments["apt-bed"].facility_id)
        self.assertEqual("f-meal-west", self.network.state.appointments["apt-west"].facility_id)

    def test_no_capacity_anywhere_falls_back_to_home_visit(self) -> None:
        # 两个助餐点同日停业，无法改派到点上 → 安排上门，床位等其他计划不受影响。
        self.network.suspend_facility("f-meal-west", date(2026, 10, 10), date_to=date(2026, 10, 12),
                                      reason="停业", reassign=False)
        inside = datetime(2026, 10, 11, 11, 0, tzinfo=CST)
        self.network.request_service("apt-home", "e-001", "meal_dining", inside)
        self.network.schedule_appointment("apt-home", inside, facility_id="f-meal-east", caregiver_id="cg-01")
        self.network.suspend_facility("f-meal-east", date(2026, 10, 10), date_to=date(2026, 10, 12), reason="厨房检修")
        appt = self.network.state.appointments["apt-home"]
        self.assertIsNone(appt.facility_id)
        self.assertEqual("cg-01", appt.caregiver_id)
        self.assertEqual("home", appt.delivery_mode)

    def test_resume_closes_suspension_window(self) -> None:
        self.network.suspend_facility("f-meal-east", date(2026, 10, 10), reason="临时检查")
        self.assertIsNotNone(self.network.state.facilities["f-meal-east"].suspended_on(date(2026, 10, 11)))
        self.network.resume_facility("f-meal-east", date(2026, 10, 11))
        self.assertIsNone(self.network.state.facilities["f-meal-east"].suspended_on(date(2026, 10, 11)))

    def test_capacity_is_enforced_per_day(self) -> None:
        d = datetime(2026, 10, 11, 11, 0, tzinfo=CST)
        # 容量 2，需要第三位老人。
        self.network.register_elder("e-003", "赵婆婆", "甲县/东乡/河东村", categories=("empty_nest",))
        self.network.grant_consent("e-003", "g-3", "service_delivery", "self",
                                   service_codes=("meal_dining",), valid_from=date(2026, 1, 1))
        self.network.grant_entitlement("e-003", "meal_dining", "ent-3", date(2026, 10, 1))
        self.network.register_elder("e-004", "钱爷爷", "甲县/东乡/河东村", categories=("empty_nest",))
        self.network.grant_consent("e-004", "g-4", "service_delivery", "self",
                                   service_codes=("meal_dining",), valid_from=date(2026, 1, 1))
        self.network.grant_entitlement("e-004", "meal_dining", "ent-4", date(2026, 10, 1))
        self.network.request_service("apt-c1", "e-001", "meal_dining", d)
        self.network.schedule_appointment("apt-c1", d, facility_id="f-meal-east", caregiver_id="cg-01")
        self.network.request_service("apt-c2", "e-003", "meal_dining", d)
        self.network.schedule_appointment("apt-c2", d, facility_id="f-meal-east", caregiver_id="cg-01")
        self.network.request_service("apt-c3", "e-004", "meal_dining", d)
        with self.assertRaises(DomainError) as caught:
            self.network.schedule_appointment("apt-c3", d, facility_id="f-meal-east", caregiver_id="cg-01")
        self.assertEqual("capacity_exceeded", caught.exception.code)

    def test_expired_caregiver_qualification_blocks_scheduling(self) -> None:
        self.network.register_caregiver(
            "cg-exp", "过期资质护工", "甲县/东乡/河东村", service_codes=("meal_dining",),
            qualifications=[{"certificate": "旧证", "service_codes": ["meal_dining"],
                             "valid_from": "2025-01-01", "valid_to": "2026-09-30"}],
        )
        d = datetime(2026, 10, 11, 11, 0, tzinfo=CST)
        self.network.request_service("apt-q", "e-001", "meal_dining", d)
        with self.assertRaises(DomainError) as caught:
            self.network.schedule_appointment("apt-q", d, facility_id="f-meal-east", caregiver_id="cg-exp")
        self.assertEqual("qualification_required", caught.exception.code)


class EmergencyTests(NetworkTestBase):
    def test_high_risk_emergency_requires_minimum_need_to_know_responders(self) -> None:
        with self.assertRaises(DomainError) as caught:
            self.network.trigger_emergency(
                "e-001", "critical", "电话无人接听且门前有血迹", "cg-01",
                visit_at=datetime(2026, 10, 6, 7, 30, tzinfo=CST),
            )
        self.assertEqual("responder_required", caught.exception.code)

    def test_emergency_visit_first_paperwork_after_within_privacy_boundary(self) -> None:
        at = datetime(2026, 10, 6, 7, 30, tzinfo=CST)
        # e-002 只有（可视为无任何服务授权），紧急仍可先上门。
        result = self.network.trigger_emergency(
            "e-002", "critical", "邻居报跌倒无法起身", "neighbor-ma",
            informed_responders=("responder-120", "village-head"),
            visit_caregiver_id="cg-02", visit_at=at,
        )
        appt_id = result["appointment_id"]
        appt = self.network.state.appointments[appt_id]
        self.assertEqual("pending", appt.material_status)
        self.assertEqual("emergency_life_safety", appt.snapshots["legal_basis"])
        # 无紧急依据的普通服务仍不能借道：紧急事件的知情范围不含其他服务信息。
        self.network.fulfill(appt_id, "v-emg", fulfilled_at=at, note="破门急救送医")
        # 材料未补且无主管核签 → 拒绝关闭。
        with self.assertRaises(DomainError) as caught:
            self.network.complete_paperwork(appt_id)
        self.assertEqual("followup_required", caught.exception.code)
        self.network.complete_paperwork(appt_id, supervisor_id="supervisor-sun", note="核实生命危急")
        self.assertEqual("complete", self.network.state.appointments[appt_id].material_status)
        self.assertTrue(self.network.state.elders["e-002"].escalated)
        self.network.close_referral(result["referral"]["payload"]["referral_id"], "送医后体征平稳", outcome="已住院观察")
        self.assertFalse(self.network.state.elders["e-002"].escalated)

    def test_emergency_paperwork_can_be_cured_with_retroactive_valid_grant(self) -> None:
        at = datetime(2026, 10, 6, 6, 0, tzinfo=CST)
        result = self.network.trigger_emergency(
            "e-001", "high", "老人自述胸闷", "cg-01",
            informed_responders=("village-head",), visit_at=at,
        )
        appt_id = result["appointment_id"]
        # g-self-meal 不覆盖 emergency_visit（service_codes 限定），须补覆盖该服务的授权。
        with self.assertRaises(DomainError):
            self.network.complete_paperwork(appt_id, grant_id="g-self-meal")
        self.network.grant_consent(
            "e-001", "g-emg", "service_delivery", "verbal_witnessed",
            service_codes=("emergency_visit",), witness="village-head", valid_from=date(2026, 10, 6),
            at=datetime(2026, 10, 6, 9, 0, tzinfo=CST),
        )
        self.network.complete_paperwork(appt_id, grant_id="g-emg", note="本人确认上门")
        self.assertEqual("complete", self.network.state.appointments[appt_id].material_status)


class SubmissionTests(NetworkTestBase):
    def test_duplicate_and_late_submission_return_existing_receipt(self) -> None:
        payload = {"visit_id": "v-1", "elder_id": "e-001", "status": "done", "submitted_at": "10:00"}
        first = self.network.submit_record("KEY-1", "visit", "org-east", payload)
        receipt = first["payload"]["receipt"]["accepted_event_id"]
        # 同键晚到、报送时间不同（易变字段），返回同一受理编号。
        again = self.network.submit_record(
            "KEY-1", "visit", "org-east", {**payload, "submitted_at": "12:30"},
        )
        self.assertEqual("SUBMISSION_DUPLICATE_RETURNED", again["event_type"])
        self.assertEqual(receipt, again["payload"]["receipt"]["accepted_event_id"])
        self.assertEqual("applied", self.network.state.submissions["KEY-1"].status)

    def test_conflicting_submission_goes_to_reviewer_and_never_silent_merges(self) -> None:
        self.network.submit_record("KEY-2", "visit", "org-east", {"status": "done", "duration": 30})
        event = self.network.submit_record(
            "KEY-2", "visit", "org-east", {"status": "done", "duration": 99}
        )
        self.assertEqual("referred_for_review", event["payload"]["status"])
        review_id = event["payload"]["review_id"]
        review = self.network.state.reviews[review_id]
        self.assertEqual("content_conflict", review.reason_code)
        self.assertEqual(99, review.payload["duration"])
        self.assertEqual(30, review.existing["content"]["duration"])
        self.network.resolve_review(review_id, "keep_existing", "reviewer-zhao", note="以签到时间为准")
        self.assertEqual("resolved", self.network.state.reviews[review_id].status)
        self.assertEqual(30, self.network.state.submissions["KEY-2"].receipt["content"]["duration"])

    def test_duplicate_identity_registration_is_rejected(self) -> None:
        with self.assertRaises(DomainError) as caught:
            self.network.register_elder("e-dup", "重复档案", "甲县/东乡/河东村", identity_number="ID-001")
        self.assertEqual("duplicate_identity", caught.exception.code)


class ImmutabilityTests(NetworkTestBase):
    def test_fulfilled_record_cannot_be_cancelled_or_overwritten(self) -> None:
        d = datetime(2026, 10, 10, 11, 0, tzinfo=CST)
        self.network.request_service("apt-imm", "e-001", "meal_dining", d)
        self.network.schedule_appointment("apt-imm", d, facility_id="f-meal-east", caregiver_id="cg-01")
        self.network.fulfill("apt-imm", "v-imm", fulfilled_at=d)
        with self.assertRaises(DomainError) as caught:
            self.network.cancel_appointment("apt-imm")
        self.assertEqual("record_immutable", caught.exception.code)
        with self.assertRaises(DomainError):
            self.network.fulfill("apt-imm", "v-again")

    def test_correction_creates_linked_record_without_touching_original(self) -> None:
        d = datetime(2026, 10, 10, 11, 0, tzinfo=CST)
        self.network.request_service("apt-corr", "e-001", "meal_dining", d)
        self.network.schedule_appointment("apt-corr", d, facility_id="f-meal-east", caregiver_id="cg-01")
        self.network.fulfill("apt-corr", "v-orig", fulfilled_at=d, note="误记为本人签收")
        corr = self.network.correct_visit("v-orig", "v-corr", "实为家属代签")
        self.assertEqual("v-orig", corr["payload"]["correction_of"])
        notes = {v["visit_id"]: v["note"] for v in self.network.state.visits}
        self.assertEqual("误记为本人签收", notes["v-orig"])
        self.assertEqual("实为家属代签", notes["v-corr"])


class RestartTests(NetworkTestBase):
    def test_state_survives_restart_including_escalation_and_suspension(self) -> None:
        d = datetime(2026, 10, 11, 11, 0, tzinfo=CST)
        self.network.request_service("apt-r", "e-001", "meal_dining", d)
        self.network.schedule_appointment("apt-r", d, facility_id="f-meal-east", caregiver_id="cg-01")
        self.network.suspend_facility("f-meal-east", date(2026, 10, 10), date_to=date(2026, 10, 12), reason="检修")
        emergency = self.network.trigger_emergency(
            "e-002", "high", "失联两日", "cg-02",
            informed_responders=("village-head",), visit_caregiver_id="cg-02",
            visit_at=datetime(2026, 10, 6, 8, 0, tzinfo=CST),
        )
        self.network.withdraw_consent("e-001", "g-self-meal", reason="停服")

        rebuilt = self._build(EventStore(self.path))
        # 停业窗口与改派结果恢复。
        self.assertIsNotNone(rebuilt.state.facilities["f-meal-east"].suspended_on(date(2026, 10, 11)))
        self.assertEqual("f-meal-west", rebuilt.state.appointments["apt-r"].facility_id)
        self.assertEqual("cg-02", rebuilt.state.appointments["apt-r"].caregiver_id)
        self.assertEqual("onsite", rebuilt.state.appointments["apt-r"].delivery_mode)
        # 升级状态与开放转介恢复。
        self.assertTrue(rebuilt.state.elders["e-002"].escalated)
        self.assertEqual(emergency["referral"]["payload"]["referral_id"], rebuilt.state.elders["e-002"].open_emergency)
        # 撤回状态恢复。
        self.assertIsNotNone(rebuilt.state.elders["e-001"].grants["g-self-meal"].withdrawn_at)
        # 探访期限恢复。
        self.assertEqual(date(2026, 10, 31), rebuilt.state.elders["e-001"].latest_assessment(date(2026, 10, 6)).visit_due_by)
        # 事件编号去重仍生效。
        with self.assertRaises(DomainError) as caught:
            rebuilt.store.append(rebuilt.store.events[0])
        self.assertEqual("duplicate_event", caught.exception.code)


class ReadModelTests(NetworkTestBase):
    def _deliver(self, apt: str, visit: str, day: datetime, elder: str = "e-001") -> None:
        self.network.request_service(apt, elder, "meal_dining", day)
        self.network.schedule_appointment(apt, day, facility_id="f-meal-east", caregiver_id="cg-01")
        self.network.fulfill(apt, visit, fulfilled_at=day)

    def test_hierarchy_reports_gaps_completion_and_responsibilities(self) -> None:
        # e-002 的探访期限已过且从未探访 → visit_overdue 缺口。
        self.network.grant_consent(
            "e-002", "g-2-visit", "service_delivery", "verbal_witnessed",
            service_codes=("visit_care",), witness="village-head", valid_from=date(2026, 1, 1),
        )
        self.network.assess_need(
            "e-002", "a-002-late", "assessor-li", service_codes=("visit_care",),
            risk_level="high", visit_due_by=date(2026, 9, 30),
        )
        self.network.grant_entitlement("e-002", "visit_care", "ent-2-visit", date(2026, 10, 1),
                                       assessment_id="a-002-late")
        # e-001 上次助餐在 9 月中旬，按 7 天节奏存在缺口。
        old = datetime(2026, 9, 15, 11, 0, tzinfo=CST)
        self.network.request_service("apt-old", "e-001", "meal_dining", old)
        self.network.schedule_appointment("apt-old", old, facility_id="f-meal-east", caregiver_id="cg-01")
        self.network.fulfill("apt-old", "v-old", fulfilled_at=old)
        view = hierarchy_view(self.network.state, Viewer("boss", "supervisor", scope="甲县"), today=self.today)
        county = view["nodes"]["甲县"]
        east = view["nodes"]["甲县/东乡/河东村"]
        self.assertGreaterEqual(county["elders"], 2)
        self.assertEqual(1, county["fulfilled"].get("meal_dining", 0))
        gap_services = {(g["elder_id"], g["reason"]) for g in county["gaps"]}
        self.assertIn(("e-001", "cadence_gap"), gap_services)
        self.assertIn("meal_dining", east["responsibilities"])
        self.assertIn("visit_care", east["responsibilities"])
        self.assertIn("visit_overdue", {g["reason"] for g in county["gaps"]})

    def test_township_scope_cannot_see_other_township(self) -> None:
        view = hierarchy_view(self.network.state, Viewer("east-boss", "supervisor", scope="甲县/东乡"), today=self.today)
        elders = {n: item["elders"] for n, item in view["nodes"].items()}
        self.assertEqual(1, elders["甲县/东乡/河东村"])
        self.assertNotIn("甲县/西乡/河西村", elders)

    def test_open_emergency_appears_as_coverage_gap(self) -> None:
        self.network.trigger_emergency(
            "e-002", "high", "失联", "cg-02", informed_responders=("village-head",),
            visit_caregiver_id="cg-02", visit_at=datetime(2026, 10, 6, 8, 0, tzinfo=CST),
        )
        view = hierarchy_view(self.network.state, Viewer("boss", "supervisor", scope="甲县"), today=self.today)
        self.assertTrue(any(g["reason"] == "open_emergency" and g["elder_id"] == "e-002" for g in view["nodes"]["甲县"]["gaps"]))

    def test_elder_and_agent_see_only_their_authorized_content(self) -> None:
        d = datetime(2026, 10, 10, 11, 0, tzinfo=CST)
        self.network.request_service("apt-meal", "e-001", "meal_dining", d, requested_by="son-chen")
        self.network.schedule_appointment("apt-meal", d, facility_id="f-meal-east", caregiver_id="cg-01")
        self.network.fulfill("apt-meal", "v-meal", fulfilled_at=d)
        self.network.request_service("apt-visit", "e-001", "visit_care", d)
        self.network.schedule_appointment("apt-visit", d, caregiver_id="cg-01")
        self.network.fulfill("apt-visit", "v-visit", fulfilled_at=d)

        mine = elder_view(self.network.state, Viewer("e-001", "elder"))
        self.assertEqual({"meal_dining", "visit_care"}, set(mine["visible_service_codes"]))
        self.assertEqual(2, len(mine["visits"]))

        agent = elder_view(self.network.state, Viewer("son-chen", "family_agent", scope="e-001"))
        self.assertEqual(["meal_dining"], agent["visible_service_codes"])
        self.assertEqual(["v-meal"], [v["visit_id"] for v in agent["visits"]])
        self.assertNotIn("risk findings", json.dumps(agent, ensure_ascii=False))

        with self.assertRaises(PermissionError):
            elder_view(self.network.state, Viewer("cg-01", "caregiver"))

    def test_caregiver_sees_only_assigned_work(self) -> None:
        d = datetime(2026, 10, 10, 11, 0, tzinfo=CST)
        self.network.request_service("apt-cg1", "e-001", "meal_dining", d)
        self.network.schedule_appointment("apt-cg1", d, facility_id="f-meal-east", caregiver_id="cg-01")
        self.network.fulfill("apt-cg1", "v-cg1", fulfilled_at=d)
        view = caregiver_view(self.network.state, Viewer("cg-02", "caregiver"))
        self.assertEqual([], view["appointments"])
        view1 = caregiver_view(self.network.state, Viewer("cg-01", "caregiver"))
        self.assertEqual(["apt-cg1"], [a["appointment_id"] for a in view1["appointments"]])
        self.assertEqual("13800000001", view1["appointments"][0]["contact_phone"])

    def test_responder_gets_minimum_need_to_know_payload(self) -> None:
        result = self.network.trigger_emergency(
            "e-001", "critical", "昏迷", "neighbor", informed_responders=("resp-120",),
            visit_caregiver_id="cg-01", visit_at=datetime(2026, 10, 6, 7, 0, tzinfo=CST),
        )
        visible = responder_view(self.network.state, Viewer("resp-120", "emergency_responder"))
        self.assertEqual(1, len(visible))
        self.assertNotIn("assessments", json.dumps(visible, ensure_ascii=False))
        # 未列入知情范围的响应人看不到案件。
        self.assertEqual([], responder_view(self.network.state, Viewer("other-unit", "emergency_responder")))
        referral_id = result["referral"]["payload"]["referral_id"]
        self.network.close_referral(referral_id, "送医")
        self.assertEqual([], responder_view(self.network.state, Viewer("resp-120", "emergency_responder")))


if __name__ == "__main__":
    unittest.main()
