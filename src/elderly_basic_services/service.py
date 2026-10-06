"""资格网命令侧：所有业务规则在写入事件前校验。

关键约束：
- 探访/预约只在“服务发生当时”有效的政策版本、资格区间与授权范围内成立，
  生效依据被快照进事件，事后政策或授权变化不会改写历史记录。
- 紧急上门与转介可先发生、后补材料，但必须记录法定紧急依据与最小知情范围，
  不绕过隐私边界（无紧急依据仍需有效授权）。
- 设施停业只改派受影响日期的安排；其他设施、其他村与床位计划不动。
- 报送按稳定业务键幂等；内容矛盾转指定复核人，绝不静默合并。
"""

from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Iterable

from .state import NetworkState, apply, to_date, to_dt
from .store import DomainError, EventStore

CN_TZ = timezone(timedelta(hours=8))
_UNSET = object()
SERVICE_PURPOSE = "service_delivery"
ADMIN_PURPOSE = "administration"
EMERGENCY_PURPOSE = "emergency"
AUTH_METHODS = {"self", "written_agent", "verbal_witnessed", "emergency_implied"}


def _default_clock() -> datetime:
    return datetime.now(CN_TZ)


class EligibilityNetwork:
    def __init__(
        self,
        store: EventStore,
        clock: Callable[[], datetime] | None = None,
        reviewers: dict[str, str] | None = None,
    ) -> None:
        self.store = store
        self.clock = clock or _default_clock
        self.reviewers = dict(reviewers or {})
        self.state = NetworkState()
        self._agg_versions: dict[tuple[str, str], int] = {}
        for event in store.events:
            apply(self.state, event)
            key = (event["aggregate_type"], event["aggregate_id"])
            self._agg_versions[key] = self._agg_versions.get(key, 0) + 1

    # ------------------------------------------------------------------ 基础

    def _emit(
        self,
        event_type: str,
        aggregate_type: str,
        aggregate_id: str,
        payload: dict[str, Any],
        at: datetime | None = None,
        event_id: str | None = None,
    ) -> dict[str, Any]:
        at = at or self.clock()
        key = (aggregate_type, aggregate_id)
        self._agg_versions[key] = self._agg_versions.get(key, 0) + 1
        event = {
            "event_id": event_id or f"{event_type.lower()}-{uuid.uuid4().hex[:16]}",
            "event_type": event_type,
            "aggregate_type": aggregate_type,
            "aggregate_id": aggregate_id,
            "occurred_at": at.isoformat(),
            "version": self._agg_versions[key],
            "summary": payload.pop("summary", "") or f"{event_type} {aggregate_id}",
            "payload": payload,
        }
        self.store.append(event)
        apply(self.state, event)
        self._agg_versions[key] = event["version"]
        return event

    def _elder(self, elder_id: str) -> Any:
        elder = self.state.elders.get(elder_id)
        if elder is None:
            raise DomainError("unknown_elder", "老人尚未登记", "elder_id")
        return elder

    def _appointment(self, appointment_id: str) -> Any:
        appt = self.state.appointments.get(appointment_id)
        if appt is None:
            raise DomainError("unknown_appointment", "预约不存在", "appointment_id")
        return appt

    # ------------------------------------------------------------ 目录与政策

    def publish_policy(
        self,
        service_code: str,
        name: str,
        category: str,
        effective_from: date | str,
        *,
        policy_version: int = 1,
        effective_to: date | str | None = None,
        payment_tier: str = "basic",
        cadence_days: int | None = None,
        required_for_categories: Iterable[str] = (),
        responsibilities: dict[str, str] | None = None,
        at: datetime | None = None,
    ) -> dict[str, Any]:
        return self._emit(
            "POLICY_PUBLISHED",
            "policy_catalog",
            f"policy:{service_code}",
            {
                "service_code": service_code,
                "policy_version": policy_version,
                "name": name,
                "category": category,
                "payment_tier": payment_tier,
                "cadence_days": cadence_days,
                "required_for_categories": list(required_for_categories),
                "responsibilities": responsibilities or {},
                "effective_from": str(to_date(effective_from)),
                "effective_to": str(to_date(effective_to)) if effective_to else None,
                "summary": f"政策发布：{name}（v{policy_version}）",
            },
            at,
            event_id=f"policy-{service_code}-v{policy_version}",
        )

    # ---------------------------------------------------------------- 老人

    def register_elder(
        self,
        elder_id: str,
        name: str,
        residence_village: str,
        *,
        identity_number: str | None = None,
        categories: Iterable[str] = (),
        contacts: dict[str, str] | None = None,
        at: datetime | None = None,
    ) -> dict[str, Any]:
        if elder_id in self.state.elders:
            raise DomainError("duplicate_elder", "老人已登记，重复登记请走复核", "elder_id")
        if identity_number and identity_number in self.state.identities:
            raise DomainError(
                "duplicate_identity",
                "同一证件号已登记为其他老人档案，禁止重复建档",
                "identity_number",
            )
        return self._emit(
            "ELDER_REGISTERED",
            "elder_profile",
            elder_id,
            {
                "elder_id": elder_id,
                "name": name,
                "identity_number": identity_number,
                "residence_village": residence_village,
                "categories": list(categories),
                "contacts": contacts or {},
                "summary": f"登记老人 {name}，常住 {residence_village}",
            },
            at,
        )

    def relocate(
        self,
        elder_id: str,
        new_village: str,
        effective_date: date | str,
        *,
        at: datetime | None = None,
    ) -> dict[str, Any]:
        self._elder(elder_id)
        day = to_date(effective_date)
        # 迁居只影响未来：历史事件与已履约记录保留原快照，未来改派/履约按现住地重新快照。
        return self._emit(
            "ELDER_RELOCATED",
            "elder_profile",
            elder_id,
            {
                "elder_id": elder_id,
                "new_village": new_village,
                "effective_date": str(day),
                "summary": f"迁居至 {new_village}，{day} 起生效",
            },
            at,
        )

    # ---------------------------------------------------------------- 授权

    def grant_consent(
        self,
        elder_id: str,
        grant_id: str,
        purpose: str,
        auth_method: str,
        *,
        service_codes: Iterable[str] = (),
        grantee_id: str | None = None,
        grantee_role: str | None = None,
        valid_from: date | str | None = None,
        valid_to: date | str | None = None,
        delegated_by: str | None = None,
        witness: str | None = None,
        at: datetime | None = None,
    ) -> dict[str, Any]:
        self._elder(elder_id)
        if auth_method not in AUTH_METHODS:
            raise DomainError("unknown_auth_method", f"授权方式须为 {sorted(AUTH_METHODS)}", "auth_method")
        if auth_method == "written_agent" and not delegated_by:
            raise DomainError("delegation_required", "书面代办授权必须记录委托人", "delegated_by")
        if auth_method == "verbal_witnessed" and not witness:
            raise DomainError("witness_required", "口头授权必须有见证人", "witness")
        codes = tuple(service_codes)
        payload: dict[str, Any] = {
            "elder_id": elder_id,
            "grant_id": grant_id,
            "purpose": purpose,
            "auth_method": auth_method,
            "service_codes": list(codes),
            "grantee_id": grantee_id,
            "grantee_role": grantee_role,
            "valid_from": str(to_date(valid_from)) if valid_from else None,
            "valid_to": str(to_date(valid_to)) if valid_to else None,
            "delegated_by": delegated_by,
            "witness": witness,
            "summary": f"授予授权 {grant_id}（{purpose}/{auth_method}）",
        }
        return self._emit("CONSENT_GRANTED", "elder_profile", elder_id, payload, at)

    def withdraw_consent(
        self, elder_id: str, grant_id: str, *, reason: str = "", at: datetime | None = None
    ) -> dict[str, Any]:
        elder = self._elder(elder_id)
        grant = elder.grants.get(grant_id)
        if grant is None:
            raise DomainError("unknown_grant", "授权不存在", "grant_id")
        at = at or self.clock()
        if grant.withdrawn_at is not None:
            raise DomainError("grant_inactive", "授权已撤回，撤回只影响未来安排", "grant_id")
        # 已发生的探访记录不做任何处理：撤回自当前时刻起生效。
        return self._emit(
            "CONSENT_WITHDRAWN",
            "elder_profile",
            elder_id,
            {
                "elder_id": elder_id,
                "grant_id": grant_id,
                "reason": reason,
                "summary": f"撤回授权 {grant_id}，未来安排停止",
            },
            at,
        )

    def _effective_grants(self, elder: Any, service_code: str, purpose: str, at: datetime) -> list[Any]:
        return [
            g
            for g in elder.grants.values()
            if g.purpose == purpose and g.effective_at(at) and (not g.service_codes or service_code in g.service_codes)
        ]

    def _grant_matches(self, grant: Any, caregiver_id: str | None, facility_id: str | None) -> bool:
        if grant.grantee_id is not None and grant.grantee_id not in {caregiver_id, facility_id}:
            return False
        if grant.grantee_role == "caregiver" and not caregiver_id:
            return False
        return True

    # ---------------------------------------------------------------- 评估

    def assess_need(
        self,
        elder_id: str,
        assessment_id: str,
        assessor_id: str,
        *,
        service_codes: Iterable[str] = (),
        risk_level: str = "none",
        findings: str = "",
        visit_due_by: date | str | None = None,
        valid_until: date | str | None = None,
        at: datetime | None = None,
    ) -> dict[str, Any]:
        self._elder(elder_id)
        return self._emit(
            "NEED_ASSESSED",
            "elder_profile",
            elder_id,
            {
                "elder_id": elder_id,
                "assessment_id": assessment_id,
                "assessment_by": assessor_id,
                "service_codes": list(service_codes),
                "risk_level": risk_level,
                "findings": findings,
                "visit_due_by": str(to_date(visit_due_by)) if visit_due_by else None,
                "valid_until": str(to_date(valid_until)) if valid_until else None,
                "summary": f"需求与风险评估 {assessment_id}，风险={risk_level}",
            },
            at,
        )

    def escalate_risk(
        self, elder_id: str, risk_level: str, reason: str, *, at: datetime | None = None
    ) -> dict[str, Any]:
        self._elder(elder_id)
        return self._emit(
            "RISK_ESCALATED",
            "elder_profile",
            elder_id,
            {
                "elder_id": elder_id,
                "risk_level": risk_level,
                "reason": reason,
                "summary": f"风险升级为 {risk_level}：{reason}",
            },
            at,
        )

    # ---------------------------------------------------------------- 资格

    def grant_entitlement(
        self,
        elder_id: str,
        service_code: str,
        entitlement_id: str,
        valid_from: date | str,
        *,
        valid_to: date | str | None = None,
        assessment_id: str | None = None,
        granted_by: str | None = None,
        at: datetime | None = None,
    ) -> dict[str, Any]:
        elder = self._elder(elder_id)
        day = to_date(valid_from)
        policy = self.state.policy_on(service_code, day)
        if policy is None:
            raise DomainError("policy_not_effective", f"{day} 没有生效中的 {service_code} 政策", "valid_from")
        if policy.required_for and not set(policy.required_for) & set(elder.categories):
            raise DomainError(
                "category_ineligible",
                f"老人类别 {list(elder.categories)} 不在政策覆盖范围 {list(policy.required_for)}",
                "categories",
            )
        if assessment_id and not any(a.assessment_id == assessment_id for a in elder.assessments):
            raise DomainError("unknown_assessment", "资格所依据的评估不存在", "assessment_id")
        return self._emit(
            "ENTITLEMENT_GRANTED",
            "service_entitlement",
            f"ent:{elder_id}:{service_code}",
            {
                "elder_id": elder_id,
                "service_code": service_code,
                "entitlement_id": entitlement_id,
                "policy_version": policy.policy_version,
                "assessment_id": assessment_id,
                "granted_by": granted_by,
                "valid_from": str(day),
                "valid_to": str(to_date(valid_to)) if valid_to else None,
                "summary": f"授予 {service_code} 资格，依据政策 v{policy.policy_version}",
            },
            at,
        )

    def revoke_entitlement(
        self,
        elder_id: str,
        service_code: str,
        *,
        effective_from: date | str | None = None,
        reason: str = "",
        at: datetime | None = None,
    ) -> dict[str, Any]:
        at = at or self.clock()
        day = to_date(effective_from) if effective_from else at.date()
        if not self.state.entitlement_on(elder_id, service_code, day):
            raise DomainError("entitlement_inactive", "该日期无生效资格可撤回", "service_code")
        return self._emit(
            "ENTITLEMENT_REVOKED",
            "service_entitlement",
            f"ent:{elder_id}:{service_code}",
            {
                "elder_id": elder_id,
                "service_code": service_code,
                "effective_from": str(day),
                "reason": reason,
                "summary": f"撤回 {service_code} 资格（{day} 起）：{reason}",
            },
            at,
        )

    # ------------------------------------------------------------ 设施/护理员

    def register_facility(
        self,
        facility_id: str,
        name: str,
        facility_type: str,
        village_code: str,
        capacity: int,
        *,
        service_codes: Iterable[str] = (),
        at: datetime | None = None,
    ) -> dict[str, Any]:
        if capacity < 0:
            raise DomainError("invalid_capacity", "容量不能为负", "capacity")
        return self._emit(
            "FACILITY_REGISTERED",
            "care_facility",
            facility_id,
            {
                "facility_id": facility_id,
                "name": name,
                "facility_type": facility_type,
                "village_code": village_code,
                "capacity": capacity,
                "service_codes": list(service_codes),
                "summary": f"登记{facility_type}：{name}",
            },
            at,
        )

    def suspend_facility(
        self,
        facility_id: str,
        date_from: date | str,
        *,
        date_to: date | str | None = None,
        reason: str = "",
        reassign: bool = True,
        at: datetime | None = None,
    ) -> list[dict[str, Any]]:
        if facility_id not in self.state.facilities:
            raise DomainError("unknown_facility", "设施不存在", "facility_id")
        at = at or self.clock()
        start = to_date(date_from)
        events = [
            self._emit(
                "FACILITY_SUSPENDED",
                "care_facility",
                facility_id,
                {
                    "facility_id": facility_id,
                    "date_from": str(start),
                    "date_to": str(to_date(date_to)) if date_to else None,
                    "reason": reason,
                    "summary": f"设施停业 {start} 起：{reason}",
                },
                at,
            )
        ]
        if reassign:
            events.extend(self.reassign_suspended(facility_id, start, to_date(date_to), at=at))
        return events

    def resume_facility(
        self, facility_id: str, effective_date: date | str, *, at: datetime | None = None
    ) -> dict[str, Any]:
        if facility_id not in self.state.facilities:
            raise DomainError("unknown_facility", "设施不存在", "facility_id")
        return self._emit(
            "FACILITY_RESUMED",
            "care_facility",
            facility_id,
            {
                "facility_id": facility_id,
                "effective_date": str(to_date(effective_date)),
                "summary": f"设施 {effective_date} 起恢复服务",
            },
            at,
        )

    def register_caregiver(
        self,
        caregiver_id: str,
        name: str,
        village_code: str,
        *,
        facility_id: str | None = None,
        service_codes: Iterable[str] = (),
        qualifications: Iterable[dict[str, Any]] = (),
        at: datetime | None = None,
    ) -> dict[str, Any]:
        return self._emit(
            "CAREGIVER_REGISTERED",
            "care_facility",
            f"caregiver:{caregiver_id}",
            {
                "caregiver_id": caregiver_id,
                "name": name,
                "village_code": village_code,
                "facility_id": facility_id,
                "service_codes": list(service_codes),
                "qualifications": list(qualifications),
                "summary": f"登记护理员 {name}",
            },
            at,
        )

    # ------------------------------------------------------------ 预约与派单

    def request_service(
        self,
        appointment_id: str,
        elder_id: str,
        service_code: str,
        requested_for: datetime | str,
        *,
        requested_by: str | None = None,
        emergency: bool = False,
        referral_id: str | None = None,
        at: datetime | None = None,
    ) -> dict[str, Any]:
        elder = self._elder(elder_id)
        at = at or self.clock()
        target = to_dt(requested_for)
        if appointment_id in self.state.appointments:
            raise DomainError("duplicate_appointment", "预约编号已存在", "appointment_id")
        if not emergency:
            # 非紧急请求：资格 + 服务授权必须在请求时刻有效；家属代办须有事务授权。
            self._require_entitled(elder, service_code, target.date())
            grants = self._effective_grants(elder, service_code, SERVICE_PURPOSE, target)
            if not grants:
                raise DomainError("consent_required", "缺少当时有效的服务授权", "grant")
            if requested_by and requested_by != elder_id:
                admin = [
                    g
                    for g in self._effective_grants(elder, service_code, ADMIN_PURPOSE, at)
                    if g.grantee_id == requested_by
                ]
                if not admin:
                    raise DomainError("agency_required", "家属代办缺少指向该代办人的有效事务授权", "requested_by")
        return self._emit(
            "SERVICE_REQUESTED",
            "appointment",
            appointment_id,
            {
                "appointment_id": appointment_id,
                "elder_id": elder_id,
                "service_code": service_code,
                "requested_by": requested_by or elder_id,
                "requested_for": target.isoformat(),
                "emergency": emergency,
                "referral_id": referral_id,
                "legal_basis": "emergency_life_safety" if emergency else None,
                "summary": f"{'紧急' if emergency else ''}服务请求 {service_code}",
            },
            at,
        )

    def _require_entitled(self, elder: Any, service_code: str, day: date) -> Any:
        policy = self.state.policy_on(service_code, day)
        if policy is None:
            raise DomainError("policy_not_effective", f"{day} 没有生效中的 {service_code} 政策", "service_code")
        ent = self.state.entitlement_on(elder.elder_id, service_code, day)
        if ent is None:
            raise DomainError("entitlement_required", f"{day} 无生效中的 {service_code} 资格", "service_code")
        return policy

    def _booked_count(self, facility_id: str, day: date, exclude: str | None = None) -> int:
        count = 0
        for appt in self.state.appointments.values():
            if appt.appointment_id == exclude or appt.status != "scheduled":
                continue
            if appt.facility_id == facility_id and appt.scheduled_for and appt.scheduled_for.date() == day:
                count += 1
        return count

    def _snapshot(
        self,
        elder: Any,
        service_code: str,
        when: datetime,
        caregiver_id: str | None,
        facility_id: str | None,
    ) -> dict[str, Any]:
        policy = self.state.policy_on(service_code, when.date())
        ent = self.state.entitlement_on(elder.elder_id, service_code, when.date())
        grants = [
            g.grant_id
            for g in self._effective_grants(elder, service_code, SERVICE_PURPOSE, when)
            if self._grant_matches(g, caregiver_id, facility_id)
        ]
        assessment = elder.latest_assessment(when.date())
        snap = {
            "policy_version": policy.policy_version if policy else None,
            "policy_name": policy.name if policy else None,
            "entitlement_id": ent["entitlement_id"] if ent else None,
            "grant_ids": grants,
            "residence_village": elder.residence_on(when.date()),
            "risk_level": elder.risk_level,
            "assessment_id": assessment.assessment_id if assessment else None,
        }
        if caregiver_id:
            caregiver = self.state.caregivers[caregiver_id]
            qual = caregiver.qualified(service_code, when.date())
            snap["caregiver_qualification"] = qual.get("certificate") if qual else None
        return snap

    def _validate_delivery(
        self,
        elder: Any,
        service_code: str,
        when: datetime,
        facility_id: str | None,
        caregiver_id: str | None,
        *,
        emergency: bool = False,
        appointment_id: str | None = None,
    ) -> None:
        day = when.date()
        if not emergency:
            self._require_entitled(elder, service_code, day)
            grants = [
                g
                for g in self._effective_grants(elder, service_code, SERVICE_PURPOSE, when)
                if self._grant_matches(g, caregiver_id, facility_id)
            ]
            if not grants:
                raise DomainError("consent_required", "服务时刻缺少覆盖该执行人/机构的有效授权", "grant")
        if facility_id is not None:
            facility = self.state.facilities.get(facility_id)
            if facility is None:
                raise DomainError("unknown_facility", "设施不存在", "facility_id")
            if service_code not in facility.service_codes:
                raise DomainError("facility_service_mismatch", "该设施不提供此项服务", "facility_id")
            if facility.suspended_on(day):
                raise DomainError("facility_suspended", f"设施在 {day} 停业，请先改派", "facility_id")
            if self._booked_count(facility_id, day, exclude=appointment_id) >= facility.capacity:
                raise DomainError("capacity_exceeded", f"{day} 容量已满", "facility_id")
        if caregiver_id is not None:
            caregiver = self.state.caregivers.get(caregiver_id)
            if caregiver is None:
                raise DomainError("unknown_caregiver", "护理员不存在", "caregiver_id")
            if not caregiver.qualified(service_code, day):
                raise DomainError("qualification_required", "护理员缺少当日有效资质", "caregiver_id")

    def schedule_appointment(
        self,
        appointment_id: str,
        scheduled_for: datetime | str,
        *,
        facility_id: str | None = None,
        caregiver_id: str | None = None,
        delivery_mode: str = "onsite",
        at: datetime | None = None,
    ) -> dict[str, Any]:
        appt = self._appointment(appointment_id)
        if appt.status not in ("requested",):
            raise DomainError("appointment_not_open", "仅待派单预约可排班", "appointment_id")
        when = to_dt(scheduled_for)
        elder = self._elder(appt.elder_id)
        emergency = appt.material_status == "pending"
        self._validate_delivery(
            elder,
            appt.service_code,
            when,
            facility_id,
            caregiver_id,
            emergency=emergency,
            appointment_id=appointment_id,
        )
        snap = self._snapshot(elder, appt.service_code, when, caregiver_id, facility_id)
        if emergency:
            snap["legal_basis"] = "emergency_life_safety"
        return self._emit(
            "APPOINTMENT_SCHEDULED",
            "appointment",
            appointment_id,
            {
                "appointment_id": appointment_id,
                "scheduled_for": when.isoformat(),
                "facility_id": facility_id,
                "caregiver_id": caregiver_id,
                "delivery_mode": delivery_mode,
                "referral_id": appt.referral_id,
                "snapshots": snap,
                "summary": f"排班 {appt.service_code} @ {when:%Y-%m-%d %H:%M}",
            },
            at,
        )

    def assign_visit(
        self, appointment_id: str, caregiver_id: str, *, at: datetime | None = None
    ) -> dict[str, Any]:
        appt = self._appointment(appointment_id)
        if appt.status != "scheduled":
            raise DomainError("appointment_not_scheduled", "仅已排班预约可派探访", "appointment_id")
        when = appt.scheduled_for or self.clock()
        elder = self._elder(appt.elder_id)
        emergency = appt.material_status == "pending"
        self._validate_delivery(
            elder,
            appt.service_code,
            when,
            appt.facility_id,
            caregiver_id,
            emergency=emergency,
            appointment_id=appointment_id,
        )
        snap = self._snapshot(elder, appt.service_code, when, caregiver_id, appt.facility_id)
        return self._emit(
            "VISIT_ASSIGNED",
            "visit_record",
            f"visit:{appointment_id}",
            {
                "appointment_id": appointment_id,
                "caregiver_id": caregiver_id,
                "snapshots": {k: v for k, v in snap.items() if k in ("grant_ids", "residence_village")},
                "summary": f"探访派单给护理员 {caregiver_id}",
            },
            at,
        )

    def reschedule(
        self,
        appointment_id: str,
        *,
        scheduled_for: datetime | str | None = None,
        facility_id: Any = _UNSET,
        caregiver_id: str | None = None,
        delivery_mode: str | None = None,
        reason: str = "",
        at: datetime | None = None,
    ) -> dict[str, Any]:
        appt = self._appointment(appointment_id)
        if appt.status == "fulfilled":
            raise DomainError("record_immutable", "已履约记录不可改派，只能开更正记录", "appointment_id")
        if appt.status == "cancelled":
            raise DomainError("appointment_cancelled", "已取消预约不能改派", "appointment_id")
        when = to_dt(scheduled_for) if scheduled_for else appt.scheduled_for
        if when is None:
            raise DomainError("schedule_required", "改派须给出新的服务时间", "scheduled_for")
        new_facility = appt.facility_id if facility_id is _UNSET else facility_id
        new_caregiver = caregiver_id or appt.caregiver_id
        elder = self._elder(appt.elder_id)
        emergency = appt.material_status == "pending"
        self._validate_delivery(
            elder,
            appt.service_code,
            when,
            new_facility,
            new_caregiver,
            emergency=emergency,
            appointment_id=appointment_id,
        )
        snap = self._snapshot(elder, appt.service_code, when, new_caregiver, new_facility)
        if emergency:
            snap["legal_basis"] = "emergency_life_safety"
        final_mode = delivery_mode or ("home" if new_facility is None else appt.delivery_mode)
        return self._emit(
            "APPOINTMENT_RESCHEDULED",
            "appointment",
            appointment_id,
            {
                "appointment_id": appointment_id,
                "scheduled_for": when.isoformat(),
                "facility_id": new_facility,
                "caregiver_id": new_caregiver,
                "delivery_mode": final_mode,
                "reason": reason,
                "snapshots": snap,
                "summary": f"改派：{reason or '时间/资源调整'}",
            },
            at,
        )

    def cancel_appointment(
        self, appointment_id: str, *, reason: str = "", at: datetime | None = None
    ) -> dict[str, Any]:
        appt = self._appointment(appointment_id)
        if appt.status == "fulfilled":
            raise DomainError("record_immutable", "已履约记录不可取消", "appointment_id")
        if appt.status == "cancelled":
            raise DomainError("appointment_cancelled", "预约已取消", "appointment_id")
        return self._emit(
            "APPOINTMENT_CANCELLED",
            "appointment",
            appointment_id,
            {
                "appointment_id": appointment_id,
                "reason": reason,
                "summary": f"取消预约：{reason}",
            },
            at,
        )

    # ------------------------------------------------------------ 停业改派

    def reassign_suspended(
        self,
        facility_id: str,
        date_from: date | str,
        date_to: date | str | None = None,
        *,
        at: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """只改派受停业影响日期、且原设施正在停业的预约。

        护理床位、其他村与其他设施的计划不在此方法作用域内，继续运行。
        """
        facility = self.state.facilities.get(facility_id)
        if facility is None:
            raise DomainError("unknown_facility", "设施不存在", "facility_id")
        start = to_date(date_from)
        end = to_date(date_to)
        events: list[dict[str, Any]] = []
        for appt in list(self.state.appointments.values()):
            if appt.status != "scheduled" or appt.facility_id != facility_id:
                continue
            when = appt.scheduled_for
            if when is None or when.date() < start or (end and when.date() > end):
                continue  # 非受影响日期不动
            if facility.suspended_on(when.date()) is None:
                continue
            alt_facility, alt_caregiver, _mode = self._alternative_for(appt, when.date())
            events.append(
                self.reschedule(
                    appt.appointment_id,
                    scheduled_for=when,
                    facility_id=alt_facility,
                    caregiver_id=alt_caregiver,
                    reason=f"facility_suspension:{facility_id}",
                    at=at,
                )
            )
        return events

    def _alternative_for(
        self, appt: Any, day: date
    ) -> tuple[str | None, str | None, str]:
        elder = self.state.elders[appt.elder_id]
        residence = elder.residence_on(day)
        candidates = [
            f
            for f in self.state.facilities.values()
            if appt.service_code in f.service_codes
            and f.facility_id != appt.facility_id
            and f.suspended_on(day) is None
            and self._booked_count(f.facility_id, day, exclude=appt.appointment_id) < f.capacity
        ]
        # 优先老人实际居住村，其次同县（跨村居住按现住地承接）。
        candidates.sort(key=lambda f: (f.village != residence, self.state.county_of(f.village) != self.state.county_of(residence), f.village))
        if candidates:
            alt = candidates[0]
            caregiver = self._find_caregiver(appt.service_code, day, alt.village, alt.facility_id)
            return alt.facility_id, caregiver, "onsite"
        caregiver = self._find_caregiver(appt.service_code, day, residence, None)
        return None, caregiver, "home"

    def _find_caregiver(
        self, service_code: str, day: date, village: str, facility_id: str | None
    ) -> str | None:
        pool = sorted(self.state.caregivers.values(), key=lambda c: (c.village != village, c.facility_id != facility_id))
        for caregiver in pool:
            if caregiver.qualified(service_code, day):
                return caregiver.caregiver_id
        return None

    # ------------------------------------------------------------ 紧急与转介

    def trigger_emergency(
        self,
        elder_id: str,
        severity: str,
        description: str,
        reporter_id: str,
        *,
        informed_responders: Iterable[str] = (),
        visit_caregiver_id: str | None = None,
        visit_at: datetime | str | None = None,
        at: datetime | None = None,
    ) -> dict[str, Any]:
        """先上门、先转介：材料可后补，但紧急法定依据与知情范围必须当场记录。"""
        elder = self._elder(elder_id)
        at = at or self.clock()
        when = to_dt(visit_at) if visit_at else at
        referral_id = f"ref-{uuid.uuid4().hex[:12]}"
        responders = tuple(informed_responders)
        if not responders and severity in ("critical", "high"):
            raise DomainError("responder_required", "高风险转介必须记录最小知情响应人", "informed_responders")
        events: dict[str, dict[str, Any]] = {}
        events["referral"] = self._emit(
            "REFERRAL_OPENED",
            "referral_case",
            referral_id,
            {
                "referral_id": referral_id,
                "elder_id": elder_id,
                "severity": severity,
                "description": description,
                "reporter_id": reporter_id,
                "legal_basis": "emergency_life_safety",
                "informed_responders": list(responders),
                "summary": f"紧急转介（{severity}）：{description[:40]}",
            },
            at,
        )
        events["emergency"] = self._emit(
            "EMERGENCY_TRIGGERED",
            "elder_profile",
            elder_id,
            {
                "elder_id": elder_id,
                "referral_id": referral_id,
                "risk_level": severity if severity in ("critical", "high", "medium", "low") else elder.risk_level,
                "reason": description,
                "summary": f"触发紧急响应并转介 {referral_id}",
            },
            at,
        )
        appointment_id = f"emt-{uuid.uuid4().hex[:12]}"
        self.request_service(
            appointment_id,
            elder_id,
            "emergency_visit",
            when,
            requested_by=reporter_id,
            emergency=True,
            referral_id=referral_id,
            at=at,
        )
        if visit_caregiver_id:
            caregiver = self.state.caregivers.get(visit_caregiver_id)
            if caregiver is None or not caregiver.qualified("emergency_visit", when.date()):
                raise DomainError("qualification_required", "紧急上门人须具备应急探访资质", "visit_caregiver_id")
        self.schedule_appointment(
            appointment_id,
            when,
            caregiver_id=visit_caregiver_id,
            delivery_mode="home",
            at=at,
        )
        events["appointment_id"] = appointment_id
        return events

    def complete_paperwork(
        self,
        appointment_id: str,
        *,
        grant_id: str | None = None,
        supervisor_id: str | None = None,
        note: str = "",
        at: datetime | None = None,
    ) -> dict[str, Any]:
        """后补材料：补到有效授权，或由主管按紧急法定依据核签；二者必居其一。"""
        appt = self._appointment(appointment_id)
        if appt.material_status != "pending":
            raise DomainError("paperwork_complete", "该预约材料已齐，无需补录", "appointment_id")
        at = at or self.clock()
        elder = self._elder(appt.elder_id)
        visit_time = appt.scheduled_for or at
        valid_grant = None
        if grant_id:
            valid_grant = elder.grants.get(grant_id)
            if valid_grant is None or not valid_grant.effective_at(visit_time):
                raise DomainError("grant_inactive", "补交授权在服务时刻不成立", "grant_id")
            if valid_grant.service_codes and appt.service_code not in valid_grant.service_codes:
                raise DomainError("grant_scope_mismatch", "补交授权不覆盖本次紧急服务事项", "grant_id")
        if valid_grant is None and not supervisor_id:
            raise DomainError(
                "followup_required",
                "紧急上门后须补有效授权，或由主管按法定紧急依据核签",
                "grant_id",
            )
        return self._emit(
            "PAPERWORK_COMPLETED",
            "appointment",
            appointment_id,
            {
                "appointment_id": appointment_id,
                "grant_id": grant_id,
                "supervisor_id": supervisor_id,
                "legal_basis": None if grant_id else "emergency_life_safety",
                "note": note,
                "summary": "紧急上门材料补齐" + ("（授权补录）" if grant_id else "（主管核签）"),
            },
            at,
        )

    def close_referral(
        self,
        referral_id: str,
        resolution: str,
        *,
        outcome: str = "",
        at: datetime | None = None,
    ) -> dict[str, Any]:
        ref = self.state.referrals.get(referral_id)
        if ref is None:
            raise DomainError("unknown_referral", "转介案件不存在", "referral_id")
        if not ref.is_open:
            raise DomainError("referral_closed", "转介案件已关闭", "referral_id")
        return self._emit(
            "REFERRAL_CLOSED",
            "referral_case",
            referral_id,
            {
                "referral_id": referral_id,
                "resolution": resolution,
                "outcome": outcome,
                "summary": f"关闭转介：{resolution}",
            },
            at,
        )

    # ---------------------------------------------------------------- 履约

    def fulfill(
        self,
        appointment_id: str,
        visit_id: str,
        *,
        fulfilled_at: datetime | str | None = None,
        note: str = "",
        at: datetime | None = None,
    ) -> dict[str, Any]:
        appt = self._appointment(appointment_id)
        if appt.status == "cancelled":
            raise DomainError("appointment_cancelled", "已取消预约不能登记履约", "appointment_id")
        if appt.status == "fulfilled":
            raise DomainError("record_immutable", "该预约已有履约记录，只能开更正记录", "appointment_id")
        at = at or self.clock()
        when = to_dt(fulfilled_at) if fulfilled_at else appt.scheduled_for or at
        if any(v["visit_id"] == visit_id for v in self.state.visits):
            raise DomainError("duplicate_visit", "探访记录编号已存在", "visit_id")
        elder = self._elder(appt.elder_id)
        emergency = appt.material_status == "pending"
        # 履约边界以“服务发生当时”为准：当时生效的政策、资格、授权与现住地。
        self._validate_delivery(
            elder,
            appt.service_code,
            when,
            appt.facility_id,
            appt.caregiver_id,
            emergency=emergency,
            appointment_id=appointment_id,
        )
        snapshots = self._snapshot(elder, appt.service_code, when, appt.caregiver_id, appt.facility_id)
        if emergency:
            snapshots["legal_basis"] = "emergency_life_safety"
        return self._emit(
            "SERVICE_FULFILLED",
            "visit_record",
            f"visit:{visit_id}",
            {
                "visit_id": visit_id,
                "appointment_id": appointment_id,
                "fulfilled_at": when.isoformat(),
                "note": note,
                "snapshots": snapshots,
                "summary": f"履约完成 {appt.service_code} @ {when:%Y-%m-%d}",
            },
            at,
        )

    def correct_visit(
        self, original_visit_id: str, new_visit_id: str, note: str, *, at: datetime | None = None
    ) -> dict[str, Any]:
        """更正不覆盖：原探访保留，新增带 correction_of 链接的记录。"""
        original = next((v for v in self.state.visits if v["visit_id"] == original_visit_id), None)
        if original is None:
            raise DomainError("unknown_visit", "原探访记录不存在", "original_visit_id")
        if any(v["visit_id"] == new_visit_id for v in self.state.visits):
            raise DomainError("duplicate_visit", "探访记录编号已存在", "new_visit_id")
        return self._emit(
            "SERVICE_FULFILLED",
            "visit_record",
            f"visit:{new_visit_id}",
            {
                "visit_id": new_visit_id,
                "appointment_id": original["appointment_id"],
                "fulfilled_at": original["fulfilled_at"],
                "note": note,
                "correction_of": original_visit_id,
                "snapshots": dict(original["snapshots"]),
                "summary": f"更正探访 {original_visit_id}（原记录保留）",
            },
            at,
        )

    # ------------------------------------------------------------ 报送与复核

    _VOLATILE_KEYS = {"submitted_at", "received_at"}

    def _stable_content(self, payload: dict[str, Any]) -> str:
        clean = {k: v for k, v in payload.items() if k not in self._VOLATILE_KEYS}
        return json.dumps(clean, ensure_ascii=False, sort_keys=True, default=str)

    def submit_record(
        self,
        submission_key: str,
        record_type: str,
        provider_id: str,
        payload: dict[str, Any],
        *,
        reviewer_id: str | None = None,
        at: datetime | None = None,
    ) -> dict[str, Any]:
        """机构报送：稳定业务键幂等；晚到原样返回；矛盾转复核，不静默合并。"""
        at = at or self.clock()
        existing = self.state.submissions.get(submission_key)
        if existing is not None:
            if self._stable_content(payload) == self._stable_content(existing.receipt.get("content", {})):
                return self._emit(
                    "SUBMISSION_DUPLICATE_RETURNED",
                    "provider_submission",
                    f"sub:{submission_key}",
                    {
                        "submission_key": submission_key,
                        "record_type": record_type,
                        "provider_id": provider_id,
                        "receipt": existing.receipt,
                        "summary": "重复/晚到报送，返回既有结果",
                    },
                    at,
                    event_id=f"dup-{submission_key}-{uuid.uuid4().hex[:8]}",
                )
            reviewer = reviewer_id or self.reviewers.get(record_type)
            if not reviewer:
                raise DomainError("reviewer_required", "内容矛盾且未指定复核人", "reviewer_id")
            review_id = f"rev-{uuid.uuid4().hex[:12]}"
            self._emit(
                "REVIEW_OPENED",
                "review_case",
                review_id,
                {
                    "review_id": review_id,
                    "submission_key": submission_key,
                    "record_type": record_type,
                    "reviewer_id": reviewer,
                    "reason_code": "content_conflict",
                    "detail": "同一稳定业务键报送内容矛盾，拒绝静默合并",
                    "payload": payload,
                    "existing": existing.receipt,
                    "summary": f"报送矛盾转复核人 {reviewer}",
                },
                at,
            )
            review_event = self._emit(
                "SUBMISSION_ACCEPTED",
                "provider_submission",
                f"sub:{submission_key}",
                {
                    "submission_key": submission_key,
                    "record_type": record_type,
                    "provider_id": provider_id,
                    "status": "referred_for_review",
                    "review_id": review_id,
                    "receipt": existing.receipt,
                    "summary": "矛盾报送挂起待复核，既有结果保持不变",
                },
                at,
            )
            self.state.submissions[submission_key].status = "referred_for_review"
            self.state.submissions[submission_key].review_id = review_id
            return review_event

        event_id = f"sub-{submission_key}"
        receipt = {
            "content": payload,
            "accepted_event_id": event_id,
            "record_type": record_type,
            "provider_id": provider_id,
        }
        return self._emit(
            "SUBMISSION_ACCEPTED",
            "provider_submission",
            f"sub:{submission_key}",
            {
                "submission_key": submission_key,
                "record_type": record_type,
                "provider_id": provider_id,
                "status": "applied",
                "receipt": receipt,
                "summary": f"受理报送 {submission_key}",
            },
            at,
            event_id=event_id,
        )

    def resolve_review(
        self,
        review_id: str,
        decision: str,
        resolved_by: str,
        *,
        note: str = "",
        correction: dict[str, Any] | None = None,
        at: datetime | None = None,
    ) -> dict[str, Any]:
        review = self.state.reviews.get(review_id)
        if review is None:
            raise DomainError("unknown_review", "复核案件不存在", "review_id")
        if review.status != "open":
            raise DomainError("review_closed", "复核案件已处理", "review_id")
        if decision not in ("keep_existing", "replace", "reject_new"):
            raise DomainError("invalid_decision", "决定须为 keep_existing/replace/reject_new", "decision")
        return self._emit(
            "REVIEW_RESOLVED",
            "review_case",
            review_id,
            {
                "review_id": review_id,
                "decision": decision,
                "resolved_by": resolved_by,
                "note": note,
                "correction": correction,
                "summary": f"复核结论：{decision}",
            },
            at,
        )
