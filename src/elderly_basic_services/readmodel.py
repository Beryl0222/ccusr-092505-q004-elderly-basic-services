"""读模型：县乡村层级汇总与按授权范围的可见性过滤。

- 主管视图：按县/乡/村汇总覆盖缺口、服务责任与真实完成情况（只统计履约事件）。
- 老人/代理人视图：仅返回各自被授权的服务事项，不含他人信息。
- 护理员视图：仅返回派给自己的安排与履约。
- 紧急响应人视图：按最小知情原则返回开放中的转介，不返回评估与授权明细。
所有视图都是重放状态的纯函数，不在此处暴露未授权字段。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from .state import NetworkState, to_date


def _levels(village: str) -> tuple[str, str | None, str | None]:
    parts = village.split("/")
    county = parts[0]
    township = f"{parts[0]}/{parts[1]}" if len(parts) > 1 else None
    full = village if len(parts) > 2 else township
    return county, township, full


@dataclass(frozen=True)
class Viewer:
    viewer_id: str
    role: str  # supervisor / elder / family_agent / caregiver / emergency_responder
    scope: str | None = None  # 主管的行政范围，如 "甲县" 或 "甲县/东乡"


# ------------------------------------------------------------------ 层级视图

def _village_bookings(state: NetworkState, village: str, day: date) -> dict[str, Any]:
    facilities = []
    for facility in state.facilities.values():
        if facility.village != village:
            continue
        booked = 0
        scheduled_dates: list[str] = []
        for appt in state.appointments.values():
            if appt.status != "scheduled" or appt.facility_id != facility.facility_id:
                continue
            if appt.scheduled_for:
                scheduled_dates.append(appt.scheduled_for.date().isoformat())
                if appt.scheduled_for.date() == day:
                    booked += 1
        suspension = facility.suspended_on(day)
        facilities.append(
            {
                "facility_id": facility.facility_id,
                "name": facility.name,
                "type": facility.facility_type,
                "capacity": facility.capacity,
                "booked_today": booked,
                "available_today": 0 if suspension else max(0, facility.capacity - booked),
                "status": "suspended" if suspension else "open",
                "suspension_reason": suspension.reason if suspension else None,
            }
        )
    return {"facilities": facilities}


def _elder_gaps(state: NetworkState, elder: Any, today: date) -> list[dict[str, Any]]:
    gaps: list[dict[str, Any]] = []
    residence = elder.residence_on(today)
    for ent in state.entitlements.values():
        if ent.elder_id != elder.elder_id:
            continue
        active = ent.active_on(today)
        if active is None:
            continue
        code = ent.service_code
        policy = state.policy_on(code, today)
        visits = sorted(
            (v for v in state.visits if v["elder_id"] == elder.elder_id and v["service_code"] == code),
            key=lambda v: v["service_date"],
        )
        last = visits[-1]["service_date"] if visits else None
        assessment = elder.latest_assessment(today)
        due_by = assessment.visit_due_by if assessment and code in assessment.service_codes else None
        if due_by and today > due_by and (last is None or to_date(last) < assessment.assessed_at.date()):
            gaps.append(
                {
                    "elder_id": elder.elder_id,
                    "service_code": code,
                    "reason": "visit_overdue",
                    "due_by": str(due_by),
                    "last_visit": last,
                    "village": residence,
                }
            )
        elif policy and policy.cadence_days:
            expected = active["valid_from"] + timedelta(days=policy.cadence_days)
            if today >= expected and (last is None or to_date(last) < today - timedelta(days=policy.cadence_days)):
                gaps.append(
                    {
                        "elder_id": elder.elder_id,
                        "service_code": code,
                        "reason": "cadence_gap",
                        "due_by": str(expected),
                        "last_visit": last,
                        "village": residence,
                    }
                )
    if elder.open_emergency:
        gaps.append(
            {
                "elder_id": elder.elder_id,
                "service_code": "emergency_visit",
                "reason": "open_emergency",
                "due_by": None,
                "last_visit": None,
                "village": residence,
            }
        )
    return gaps


def hierarchy_view(state: NetworkState, viewer: Viewer, today: date | None = None) -> dict[str, Any]:
    if viewer.role != "supervisor":
        raise PermissionError("仅主管可查看层级汇总")
    today = today or date.today()
    scope = viewer.scope
    nodes: dict[str, dict[str, Any]] = {}

    def node(code: str) -> dict[str, Any]:
        return nodes.setdefault(
            code,
            {
                "node": code,
                "elders": 0,
                "entitled": {},
                "fulfilled": {},
                "scheduled": 0,
                "gaps": [],
                "open_emergencies": [],
                "facilities": [],
                "responsibilities": {},
            },
        )

    for elder in state.elders.values():
        village = elder.residence_on(today)
        county, township, full = _levels(village)
        path = [p for p in (county, township, full) if p]
        if scope and not village.startswith(scope):
            continue
        for code in path:
            node(code)["elders"] += 1
        for key, ent in state.entitlements.items():
            if ent.elder_id != elder.elder_id or ent.active_on(today) is None:
                continue
            for code in path:
                node(code)["entitled"][ent.service_code] = node(code)["entitled"].get(ent.service_code, 0) + 1
        for gap in _elder_gaps(state, elder, today):
            for code in path:
                node(code)["gaps"].append(gap)
        if elder.open_emergency:
            for code in path:
                node(code)["open_emergencies"].append(
                    {"elder_id": elder.elder_id, "referral_id": elder.open_emergency, "village": village}
                )

    for visit in state.visits:
        if visit.get("correction_of"):
            continue  # 更正记录不重复计入完成量
        elder = state.elders.get(visit["elder_id"])
        if elder is None:
            continue
        village = elder.residence_on(to_date(visit["service_date"]))
        if scope and not village.startswith(scope):
            continue
        for code in [p for p in _levels(village) if p]:
            bucket = node(code)["fulfilled"]
            bucket[visit["service_code"]] = bucket.get(visit["service_code"], 0) + 1

    for appt in state.appointments.values():
        if appt.status != "scheduled":
            continue
        elder = state.elders.get(appt.elder_id)
        if elder is None or not appt.scheduled_for:
            continue
        village = elder.residence_on(appt.scheduled_for.date())
        if scope and not village.startswith(scope):
            continue
        for code in [p for p in _levels(village) if p]:
            node(code)["scheduled"] += 1

    for code, item in list(nodes.items()):
        deepest = code
        item.update(_village_bookings(state, deepest, today))
    for service_code, versions in state.policies.items():
        policy = next((v for v in versions if v.effective_from <= today and (v.effective_to is None or today <= v.effective_to)), None)
        if policy and policy.responsibilities:
            for code in nodes:
                node(code)["responsibilities"].setdefault(service_code, policy.responsibilities)

    return {"as_of": str(today), "scope": scope, "nodes": nodes}


# ------------------------------------------------------------------ 个人视图

def _authorized_services(elder: Any, viewer_id: str) -> list[dict[str, Any]]:
    visible = []
    for grant in elder.grants.values():
        if grant.grantee_id != viewer_id:
            continue
        visible.append(
            {
                "grant_id": grant.grant_id,
                "purpose": grant.purpose,
                "service_codes": list(grant.service_codes),
                "auth_method": grant.auth_method,
                "valid_to": str(grant.valid_to) if grant.valid_to else None,
                "active": grant.withdrawn_at is None,
            }
        )
    return visible


def elder_view(state: NetworkState, viewer: Viewer) -> dict[str, Any]:
    if viewer.role == "elder":
        elder = state.elders.get(viewer.viewer_id)
        if elder is None:
            raise PermissionError("未找到本人档案")
        target = elder
        is_self = True
    elif viewer.role == "family_agent":
        targets = [
            e
            for e in state.elders.values()
            if any(g.grantee_id == viewer.viewer_id and g.withdrawn_at is None for g in e.grants.values())
        ]
        if not targets:
            raise PermissionError("没有任何老人向该代理人有效授权")
        target = next((e for e in targets if e.elder_id == viewer.scope), targets[0])
        is_self = False
    else:
        raise PermissionError("该角色不能访问老人视图")

    services = []
    if is_self:
        services = sorted(
            {
                ent.service_code
                for ent in state.entitlements.values()
                if ent.elder_id == target.elder_id and ent.active_on(date.today()) is not None
            }
        )
    else:
        services = sorted(
            {
                code
                for g in target.grants.values()
                if g.grantee_id == viewer.viewer_id and g.withdrawn_at is None
                for code in g.service_codes
            }
        )
    return {
        "elder_id": target.elder_id,
        "name": target.name,
        "residence_village": target.residence,
        "visible_service_codes": services,
        "grants": [] if is_self else _authorized_services(target, viewer.viewer_id),
        "appointments": [
            {
                "appointment_id": a.appointment_id,
                "service_code": a.service_code,
                "status": a.status,
                "scheduled_for": a.scheduled_for.isoformat() if a.scheduled_for else None,
            }
            for a in state.appointments.values()
            if a.elder_id == target.elder_id
            and (is_self or a.service_code in services)
        ],
        "visits": [
            {
                "visit_id": v["visit_id"],
                "service_code": v["service_code"],
                "service_date": v["service_date"],
                "correction_of": v.get("correction_of"),
            }
            for v in state.visits
            if v["elder_id"] == target.elder_id and (is_self or v["service_code"] in services)
        ],
    }


def caregiver_view(state: NetworkState, viewer: Viewer) -> dict[str, Any]:
    if viewer.role != "caregiver":
        raise PermissionError("仅护理员可访问该视图")
    appointments = []
    for appt in state.appointments.values():
        if appt.caregiver_id != viewer.viewer_id:
            continue
        elder = state.elders[appt.elder_id]
        # 上门联系所必需的最小信息：姓名、服务地址村、联系电话（若登记）。
        appointments.append(
            {
                "appointment_id": appt.appointment_id,
                "elder_id": elder.elder_id,
                "elder_name": elder.name,
                "village": elder.residence_on(appt.scheduled_for.date()) if appt.scheduled_for else elder.residence,
                "contact_phone": elder.contacts.get("phone"),
                "service_code": appt.service_code,
                "status": appt.status,
                "scheduled_for": appt.scheduled_for.isoformat() if appt.scheduled_for else None,
                "delivery_mode": appt.delivery_mode,
            }
        )
    visits = [
        {
            "visit_id": v["visit_id"],
            "appointment_id": v["appointment_id"],
            "elder_id": v["elder_id"],
            "service_date": v["service_date"],
            "service_code": v["service_code"],
        }
        for v in state.visits
        if v["caregiver_id"] == viewer.viewer_id
    ]
    return {"caregiver_id": viewer.viewer_id, "appointments": appointments, "visits": visits}


def responder_view(state: NetworkState, viewer: Viewer) -> list[dict[str, Any]]:
    if viewer.role != "emergency_responder":
        raise PermissionError("仅紧急响应人可访问该视图")
    result = []
    for ref in state.referrals.values():
        if not ref.is_open or viewer.viewer_id not in ref.responders:
            continue
        elder = state.elders[ref.elder_id]
        result.append(
            {
                "referral_id": ref.referral_id,
                "elder_id": elder.elder_id,
                "elder_name": elder.name,
                "village": elder.residence,
                "contact_phone": elder.contacts.get("phone"),
                "severity": ref.severity,
                "description": ref.description,
                "reporter_id": ref.reporter_id,
                "opened_at": ref.opened_at.isoformat(),
            }
        )
    return result
