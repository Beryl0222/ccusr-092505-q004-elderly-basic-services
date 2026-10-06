from __future__ import annotations

import unittest
from datetime import date

from support import T0, add_elder, build_network, dt, qualify

from elderly_basic_services.access import Principal, visible_conflicts, visible_profile, visible_records
from elderly_basic_services.model import DomainError


class AccessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.net = build_network()
        add_elder(self.net, "elder-1", village="village-1", registered="village-2")
        qualify(self.net, "elder-1", ["meal_service", "home_visit"], dt(1, 2))
        meal = self.net.schedule_appointment("elder-1", "meal_service", date(2026, 2, 1), dt(1, 20), facility_id="mp-1")
        self.net.record_fulfillment(meal, "cg-raw", dt(2, 1, 13), "已送达", {"menu": "米饭"})
        visit = self.net.schedule_appointment("elder-1", "home_visit", date(2026, 2, 3), dt(1, 20))
        self.net.record_fulfillment(visit, "cg-raw", dt(2, 3, 10), "已探访", {"note": "状态平稳"})
        self.now = dt(2, 20)

    def test_elder_sees_own_records_in_full(self) -> None:
        principal = Principal(role="elder", elder_id="elder-1")
        records = visible_records(self.net, principal, "elder-1", self.now)
        self.assertEqual(2, len(records))
        self.assertEqual({"menu": "米饭"}, records[0]["details"])
        profile = visible_profile(self.net, principal, "elder-1", self.now)
        self.assertEqual("living_alone", profile["living_situation"])

    def test_agent_sees_only_granted_scopes(self) -> None:
        self.net.grant_consent(
            "elder-1", "family_proxy", ["meal"], dt(1, 5), dt(1, 5),
            agent_id="agent-1", agent_relation="子女",
        )
        principal = Principal(role="agent", agent_id="agent-1")
        records = visible_records(self.net, principal, "elder-1", self.now)
        self.assertEqual(["meal_service"], [item["service_code"] for item in records])
        self.assertNotIn("details", records[0])
        profile = visible_profile(self.net, principal, "elder-1", self.now)
        self.assertNotIn("risk_level", profile)

    def test_agent_with_assessment_scope_sees_assessment(self) -> None:
        self.net.grant_consent(
            "elder-1", "family_proxy", ["meal", "assessment_view"], dt(1, 5), dt(1, 5),
            agent_id="agent-2", agent_relation="子女",
        )
        principal = Principal(role="agent", agent_id="agent-2")
        profile = visible_profile(self.net, principal, "elder-1", self.now)
        self.assertEqual("medium", profile["risk_level"])
        records = visible_records(self.net, principal, "elder-1", self.now)
        self.assertEqual({"menu": "米饭"}, records[0]["details"])

    def test_agent_without_grant_is_denied(self) -> None:
        with self.assertRaises(DomainError) as ctx:
            visible_records(self.net, Principal(role="agent", agent_id="stranger"), "elder-1", self.now)
        self.assertEqual("access_denied", ctx.exception.code)

    def test_officer_visibility_follows_residence_hierarchy(self) -> None:
        village = Principal(role="village_officer", node_id="village-1")
        self.assertEqual(2, len(visible_records(self.net, village, "elder-1", self.now)))
        township = Principal(role="township_officer", node_id="town-1")
        self.assertEqual(2, len(visible_records(self.net, township, "elder-1", self.now)))
        county = Principal(role="county_officer", node_id="county-1")
        self.assertEqual(2, len(visible_records(self.net, county, "elder-1", self.now)))
        # 户籍村不等于服务责任村：老人居住在 village-1，village-2 主管无权查看
        registered_village = Principal(role="village_officer", node_id="village-2")
        with self.assertRaises(DomainError):
            visible_records(self.net, registered_village, "elder-1", self.now)
        other_township = Principal(role="township_officer", node_id="town-2")
        with self.assertRaises(DomainError):
            visible_records(self.net, other_township, "elder-1", self.now)
        # 角色与节点层级不一致同样拒绝
        mismatched = Principal(role="village_officer", node_id="town-1")
        with self.assertRaises(DomainError):
            visible_records(self.net, mismatched, "elder-1", self.now)

    def test_emergency_responder_sees_minimum_only_during_urgent_escalation(self) -> None:
        responder = Principal(role="emergency_responder")
        with self.assertRaises(DomainError):
            visible_profile(self.net, responder, "elder-1", self.now)
        self.net.escalate_risk("elder-1", "urgent", dt(2, 21, 8), "officer-1")
        profile = visible_profile(self.net, responder, "elder-1", dt(2, 21, 9))
        self.assertEqual("139elder-1", profile["contact"])
        self.assertNotIn("living_situation", profile)
        with self.assertRaises(DomainError):
            visible_records(self.net, responder, "elder-1", dt(2, 21, 9))

    def test_reviewer_sees_only_assigned_conflicts(self) -> None:
        self.net.assign_responsibility("org-1", "report_review", "reviewer-1", T0, T0)
        self.net.ingest_report("org-1", "elder-1", "meal_service", date(2026, 2, 10), "已送餐", dt(2, 10, 18), performer_id="cg-raw")
        self.net.ingest_report("org-1", "elder-1", "meal_service", date(2026, 2, 10), "未送达", dt(2, 10, 19), performer_id="cg-raw")
        mine = visible_conflicts(self.net, Principal(role="reviewer", agent_id="reviewer-1"))
        self.assertEqual(1, len(mine))
        self.assertEqual("org-1", mine[0]["org_id"])
        others = visible_conflicts(self.net, Principal(role="reviewer", agent_id="reviewer-2"))
        self.assertEqual([], others)
        with self.assertRaises(DomainError):
            visible_conflicts(self.net, Principal(role="elder", elder_id="elder-1"))


if __name__ == "__main__":
    unittest.main()
