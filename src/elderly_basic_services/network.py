"""基本养老服务资格网的业务核心。

ServiceNetwork 以事件溯源方式运行：每个命令先校验业务规则，再追加事件并应用；
restore 通过重放事件恢复全部状态，包括探访期限与升级状态。

核心不变量：
- 履约记录只在当时有效的政策窗口与授权范围内生成，并快照两者；
- 紧急风险可先触发上门与转介、后补材料，但紧急记录只允许最小字段集；
- 迁居与撤回同意只影响生效日之后的安排，已发生的记录不可改写；
- 机构报送按稳定业务键幂等，内容矛盾交指定复核人，不静默合并；
- 设施停业只改派停业日期区间内的预约，其余计划不受影响。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any, Mapping, Optional

from .model import (
    APPT_CANCELLED,
    APPT_FULFILLED,
    APPT_OPEN_STATES,
    APPT_REASSIGNED,
    APPT_UNASSIGNED,
    CHANNEL_FAMILY_PROXY,
    CHANNEL_WITNESSED,
    CHILD_LEVEL,
    EMERGENCY_DETAIL_FIELDS,
    ESC_OPEN,
    ESC_RESOLVED,
    ESCALATION_RESPONSE_HOURS,
    FACILITY_KINDS,
    KNOWN_CHANNELS,
    KNOWN_LEVELS,
    KNOWN_LIVING_SITUATIONS,
    KNOWN_RISK_LEVELS,
    KNOWN_SCOPES,
    LEVEL_COUNTY,
    LEVEL_VILLAGE,
    ORIGIN_EMERGENCY,
    ORIGIN_MANUAL,
    ORIGIN_REPORT,
    RISK_URGENT,
    SCOPE_VISIT,
    SERVICE_FACILITY_KIND,
    SERVICE_REPORT_REVIEW,
    Appointment,
    Assessment,
    Caregiver,
    CatalogEntry,
    ConsentGrant,
    DomainError,
    ElderProfile,
    Entitlement,
    Escalation,
    Facility,
    FulfillmentRecord,
    Jurisdiction,
    Qualification,
    ReportConflict,
    ReportReceipt,
    Responsibility,
)
from .store import EventStore


def _parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _require_aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise DomainError("timezone_required", f"{field_name} 必须包含时区")


def _noon(day: date, like: datetime) -> datetime:
    """把预约日期展开为当日正午（沿用参照时间的时区），用于时间窗校验。"""
    return datetime.combine(day, time(12, 0), tzinfo=like.tzinfo)


@dataclass(frozen=True)
class IngestResult:
    """机构报送结果：accepted / duplicate / conflict。"""

    status: str
    record_id: Optional[str] = None
    conflict_id: Optional[str] = None
    late: bool = False


class ServiceNetwork:
    """基本养老服务资格网。"""

    def __init__(self, store: EventStore) -> None:
        self.store = store
        self.jurisdictions: dict[str, Jurisdiction] = {}
        self.catalog: dict[str, CatalogEntry] = {}
        self.responsibilities: list[Responsibility] = []
        self.facilities: dict[str, Facility] = {}
        self.caregivers: dict[str, Caregiver] = {}
        self.elders: dict[str, ElderProfile] = {}
        self.consents: dict[str, ConsentGrant] = {}
        self.assessments: dict[str, Assessment] = {}
        self.entitlements: dict[str, Entitlement] = {}
        self.appointments: dict[str, Appointment] = {}
        self.records: dict[str, FulfillmentRecord] = {}
        self.escalations: dict[str, Escalation] = {}
        self.reports: dict[str, ReportReceipt] = {}
        self.conflicts: dict[str, ReportConflict] = {}
        self._seq: dict[str, int] = {}
        self._handlers = {
            "JURISDICTION_REGISTERED": self._apply_jurisdiction,
            "CATALOG_PUBLISHED": self._apply_catalog,
            "RESPONSIBILITY_ASSIGNED": self._apply_responsibility,
            "FACILITY_REGISTERED": self._apply_facility,
            "FACILITY_CLOSED": self._apply_closure,
            "CAREGIVER_REGISTERED": self._apply_caregiver,
            "ELDER_REGISTERED": self._apply_elder,
            "CONSENT_GRANTED": self._apply_consent,
            "CONSENT_WITHDRAWN": self._apply_withdrawal,
            "ELDER_RELOCATED": self._apply_relocation,
            "NEED_ASSESSED": self._apply_assessment,
            "ENTITLEMENT_GRANTED": self._apply_entitlement,
            "APPOINTMENT_SCHEDULED": self._apply_appointment,
            "VISIT_ASSIGNED": self._apply_appointment,
            "APPOINTMENT_REASSIGNED": self._apply_reassignment,
            "APPOINTMENT_CANCELLED": self._apply_cancellation,
            "SERVICE_FULFILLED": self._apply_fulfillment,
            "RISK_ESCALATED": self._apply_escalation,
            "EMERGENCY_REFERRAL": self._apply_referral,
            "MATERIALS_COMPLETED": self._apply_materials_completed,
            "ESCALATION_RESOLVED": self._apply_escalation_resolved,
            "REPORT_CONFLICT": self._apply_conflict,
        }

    @classmethod
    def restore(cls, store: EventStore) -> "ServiceNetwork":
        """重放事件恢复全部状态；重启后探访期限与升级状态保持不变。"""
        network = cls(store)
        for event in store.iter():
            network._apply(event)
        return network

    # ------------------------------------------------------------------
    # 事件骨架
    # ------------------------------------------------------------------

    def _emit(
        self,
        event_type: str,
        aggregate_type: str,
        aggregate_id: str,
        occurred_at: datetime,
        summary: str,
        data: Mapping[str, Any],
    ) -> dict[str, Any]:
        seq = self._seq.get(aggregate_id, 0) + 1
        event = {
            "event_id": f"{aggregate_id}-{seq}",
            "event_type": event_type,
            "aggregate_type": aggregate_type,
            "aggregate_id": aggregate_id,
            "occurred_at": _iso(occurred_at),
            "version": seq,
            "summary": summary,
            "data": dict(data),
        }
        self.store.append(event)
        self._apply(event)
        return event

    def _apply(self, event: Mapping[str, Any]) -> None:
        aggregate_id = str(event.get("aggregate_id", ""))
        version = event.get("version", 0)
        if isinstance(version, int):
            self._seq[aggregate_id] = max(self._seq.get(aggregate_id, 0), version)
        handler = self._handlers.get(str(event.get("event_type", "")))
        if handler is not None:
            handler(event)

    # ------------------------------------------------------------------
    # 主数据登记
    # ------------------------------------------------------------------

    def register_jurisdiction(
        self,
        node_id: str,
        level: str,
        name: str,
        at: datetime,
        parent_id: Optional[str] = None,
    ) -> None:
        _require_aware(at, "at")
        if level not in KNOWN_LEVELS:
            raise DomainError("unknown_level", f"未登记的层级: {level}")
        if level == LEVEL_COUNTY:
            if parent_id is not None:
                raise DomainError("invalid_hierarchy", "县级节点不应有上级")
        else:
            parent = self.jurisdictions.get(parent_id or "")
            if parent is None:
                raise DomainError("invalid_hierarchy", "上级节点不存在")
            if CHILD_LEVEL.get(parent.level) != level:
                raise DomainError("invalid_hierarchy", "层级关系必须是县-乡-村")
        self._emit(
            "JURISDICTION_REGISTERED",
            "jurisdiction",
            node_id,
            at,
            f"登记层级节点 {name}",
            {"node_id": node_id, "level": level, "name": name, "parent_id": parent_id},
        )

    def publish_catalog(
        self,
        service_code: str,
        name: str,
        required_scope: str,
        policy_version: str,
        effective_from: datetime,
        at: datetime,
        effective_to: Optional[datetime] = None,
        requires_qualification: Optional[str] = None,
    ) -> str:
        _require_aware(effective_from, "effective_from")
        if effective_to is not None:
            _require_aware(effective_to, "effective_to")
            if effective_to <= effective_from:
                raise DomainError("invalid_window", "政策失效时间必须晚于生效时间")
        if required_scope not in KNOWN_SCOPES - {"assessment_view"}:
            raise DomainError("unknown_scope", f"未登记的服务范围: {required_scope}")
        catalog_id = f"{service_code}@{policy_version}"
        if catalog_id in self.catalog:
            raise DomainError("duplicate_catalog", f"目录条目已存在: {catalog_id}")
        self._emit(
            "CATALOG_PUBLISHED",
            "service_catalog",
            catalog_id,
            at,
            f"发布服务目录 {name}（政策 {policy_version}）",
            {
                "catalog_id": catalog_id,
                "service_code": service_code,
                "name": name,
                "required_scope": required_scope,
                "policy_version": policy_version,
                "effective_from": _iso(effective_from),
                "effective_to": _iso(effective_to) if effective_to else None,
                "requires_qualification": requires_qualification,
            },
        )
        return catalog_id

    def assign_responsibility(
        self,
        node_id: str,
        service_code: str,
        officer_id: str,
        effective_from: datetime,
        at: datetime,
        effective_to: Optional[datetime] = None,
    ) -> None:
        """登记责任分工；node_id 也可以是机构标识（用于指定报送复核人）。"""
        _require_aware(effective_from, "effective_from")
        self._emit(
            "RESPONSIBILITY_ASSIGNED",
            "jurisdiction",
            node_id,
            at,
            f"{node_id} 的 {service_code} 责任人为 {officer_id}",
            {
                "node_id": node_id,
                "service_code": service_code,
                "officer_id": officer_id,
                "effective_from": _iso(effective_from),
                "effective_to": _iso(effective_to) if effective_to else None,
            },
        )

    def responsible_officer(
        self, node_id: str, service_code: str, at: datetime
    ) -> Optional[str]:
        """沿层级向上查找当时有效的责任人；村级缺省时由乡、县兜底。"""
        current: Optional[str] = node_id
        while current is not None:
            matches = [
                item
                for item in self.responsibilities
                if item.node_id == current
                and item.service_code == service_code
                and item.effective_from <= at
                and (item.effective_to is None or at < item.effective_to)
            ]
            if matches:
                return max(matches, key=lambda item: item.effective_from).officer_id
            node = self.jurisdictions.get(current)
            current = node.parent_id if node else None
        return None

    def register_facility(
        self,
        facility_id: str,
        village_id: str,
        kind: str,
        daily_capacity: int,
        at: datetime,
    ) -> None:
        _require_aware(at, "at")
        if kind not in FACILITY_KINDS:
            raise DomainError("unknown_facility_kind", f"未登记的设施类型: {kind}")
        self._require_village(village_id)
        if daily_capacity < 1:
            raise DomainError("invalid_capacity", "设施容量必须为正数")
        self._emit(
            "FACILITY_REGISTERED",
            "care_facility",
            facility_id,
            at,
            f"登记设施 {facility_id}",
            {
                "facility_id": facility_id,
                "village_id": village_id,
                "kind": kind,
                "daily_capacity": daily_capacity,
            },
        )

    def register_caregiver(
        self,
        caregiver_id: str,
        org_id: str,
        qualifications: list[dict[str, str]],
        at: datetime,
    ) -> None:
        """qualifications 形如 [{"cert": "nursing", "level": "中级", "valid_until": "..."}]。"""
        _require_aware(at, "at")
        normalized = []
        for item in qualifications:
            valid_until = _parse_dt(item["valid_until"])
            _require_aware(valid_until, "valid_until")
            normalized.append(
                {"cert": item["cert"], "level": item.get("level", ""), "valid_until": _iso(valid_until)}
            )
        self._emit(
            "CAREGIVER_REGISTERED",
            "caregiver",
            caregiver_id,
            at,
            f"登记护理人员 {caregiver_id}",
            {"caregiver_id": caregiver_id, "org_id": org_id, "qualifications": normalized},
        )

    # ------------------------------------------------------------------
    # 老人、授权、评估、资格
    # ------------------------------------------------------------------

    def register_elder(
        self,
        elder_id: str,
        name: str,
        living_situation: str,
        residence_village_id: str,
        registered_village_id: str,
        contact: str,
        at: datetime,
        id_number: Optional[str] = None,
    ) -> str:
        """登记老人；同一证件号（或姓名+联系方式）重复登记时返回既有档案。"""
        _require_aware(at, "at")
        if living_situation not in KNOWN_LIVING_SITUATIONS:
            raise DomainError("unknown_living_situation", f"未登记的居住情形: {living_situation}")
        self._require_village(residence_village_id)
        self._require_village(registered_village_id)
        existing = self._find_elder_by_identity(id_number, name, contact)
        if existing is not None:
            return existing.elder_id
        self._emit(
            "ELDER_REGISTERED",
            "elder_profile",
            elder_id,
            at,
            f"登记老人 {name}",
            {
                "elder_id": elder_id,
                "name": name,
                "living_situation": living_situation,
                "residence_village_id": residence_village_id,
                "registered_village_id": registered_village_id,
                "contact": contact,
                "id_number": id_number,
            },
        )
        return elder_id

    def _find_elder_by_identity(
        self, id_number: Optional[str], name: str, contact: str
    ) -> Optional[ElderProfile]:
        for elder in self.elders.values():
            if id_number and elder.id_number == id_number:
                return elder
            if not id_number and elder.name == name and elder.contact == contact:
                return elder
        return None

    def grant_consent(
        self,
        elder_id: str,
        channel: str,
        scopes: list[str],
        effective_from: datetime,
        at: datetime,
        effective_to: Optional[datetime] = None,
        agent_id: Optional[str] = None,
        agent_relation: Optional[str] = None,
        witness_ids: Optional[list[str]] = None,
    ) -> str:
        """登记授权。家属代办必须登记代理人；见证协助必须登记见证人。"""
        _require_aware(effective_from, "effective_from")
        self._require_elder(elder_id)
        if channel not in KNOWN_CHANNELS:
            raise DomainError("unknown_channel", f"未登记的授权方式: {channel}")
        scope_set = frozenset(scopes)
        if not scope_set or not scope_set <= KNOWN_SCOPES:
            raise DomainError("unknown_scope", "授权范围为空或包含未登记项")
        if channel == CHANNEL_FAMILY_PROXY and not (agent_id and agent_relation):
            raise DomainError("agent_required", "家属代办必须登记代理人与关系")
        if channel == CHANNEL_WITNESSED and not witness_ids:
            raise DomainError("witness_required", "见证协助必须登记见证人")
        consent_id = f"consent-{len(self.consents) + 1:04d}"
        self._emit(
            "CONSENT_GRANTED",
            "elder_profile",
            elder_id,
            at,
            f"登记授权 {consent_id}（{channel}）",
            {
                "consent_id": consent_id,
                "elder_id": elder_id,
                "channel": channel,
                "scopes": sorted(scope_set),
                "effective_from": _iso(effective_from),
                "effective_to": _iso(effective_to) if effective_to else None,
                "agent_id": agent_id,
                "agent_relation": agent_relation,
                "witness_ids": list(witness_ids or ()),
            },
        )
        return consent_id

    def withdraw_consent(self, consent_id: str, effective_at: datetime) -> list[str]:
        """撤回授权：只影响生效时间之后的安排，已发生的记录保持不变。

        返回因此取消的预约 id 列表。
        """
        _require_aware(effective_at, "effective_at")
        consent = self.consents.get(consent_id)
        if consent is None:
            raise DomainError("unknown_consent", f"授权不存在: {consent_id}")
        if consent.withdrawn_at is not None:
            raise DomainError("already_withdrawn", "授权已撤回")
        self._emit(
            "CONSENT_WITHDRAWN",
            "elder_profile",
            consent.elder_id,
            effective_at,
            f"撤回授权 {consent_id}",
            {"consent_id": consent_id, "elder_id": consent.elder_id, "effective_at": _iso(effective_at)},
        )
        cancelled = []
        for appointment in self._future_appointments(consent.elder_id, effective_at):
            scope = self._scope_of(appointment.service_code, effective_at)
            if scope and not self._consent_covering(appointment.elder_id, scope, effective_at):
                self._cancel_appointment(appointment, effective_at, "consent_withdrawn")
                cancelled.append(appointment.appointment_id)
        return cancelled

    def assess_need(
        self,
        elder_id: str,
        risk_level: str,
        needs: list[str],
        assessor_id: str,
        assessed_at: datetime,
        valid_until: datetime,
    ) -> str:
        _require_aware(assessed_at, "assessed_at")
        _require_aware(valid_until, "valid_until")
        self._require_elder(elder_id)
        if risk_level not in KNOWN_RISK_LEVELS:
            raise DomainError("unknown_risk_level", f"未登记的风险等级: {risk_level}")
        if valid_until <= assessed_at:
            raise DomainError("invalid_window", "评估有效期必须晚于评估时间")
        unknown = [code for code in needs if code not in self._known_service_codes()]
        if unknown:
            raise DomainError("unknown_service", f"需求包含未登记服务: {unknown}")
        assessment_id = f"assess-{len(self.assessments) + 1:04d}"
        self._emit(
            "NEED_ASSESSED",
            "elder_profile",
            elder_id,
            assessed_at,
            f"完成风险与需求评估 {assessment_id}（{risk_level}）",
            {
                "assessment_id": assessment_id,
                "elder_id": elder_id,
                "risk_level": risk_level,
                "needs": list(needs),
                "assessor_id": assessor_id,
                "assessed_at": _iso(assessed_at),
                "valid_until": _iso(valid_until),
            },
        )
        return assessment_id

    def grant_entitlement(
        self,
        elder_id: str,
        service_codes: list[str],
        valid_from: datetime,
        basis_assessment_id: str,
        at: datetime,
        valid_to: Optional[datetime] = None,
    ) -> str:
        _require_aware(valid_from, "valid_from")
        self._require_elder(elder_id)
        assessment = self.assessments.get(basis_assessment_id)
        if assessment is None or assessment.elder_id != elder_id:
            raise DomainError("unknown_assessment", "资格必须基于本人的有效评估")
        if assessment.valid_until < valid_from:
            raise DomainError("assessment_expired", "评估已过期，不能作为资格依据")
        if not set(service_codes) <= set(assessment.needs):
            raise DomainError("beyond_assessment", "资格范围不能超出评估需求")
        entitlement_id = f"entitle-{len(self.entitlements) + 1:04d}"
        self._emit(
            "ENTITLEMENT_GRANTED",
            "service_entitlement",
            entitlement_id,
            at,
            f"授予服务资格 {entitlement_id}",
            {
                "entitlement_id": entitlement_id,
                "elder_id": elder_id,
                "service_codes": sorted(set(service_codes)),
                "valid_from": _iso(valid_from),
                "valid_to": _iso(valid_to) if valid_to else None,
                "basis_assessment_id": basis_assessment_id,
            },
        )
        return entitlement_id

    # ------------------------------------------------------------------
    # 预约与履约
    # ------------------------------------------------------------------

    def schedule_appointment(
        self,
        elder_id: str,
        service_code: str,
        scheduled_date: date,
        at: datetime,
        facility_id: Optional[str] = None,
        caregiver_id: Optional[str] = None,
        due_at: Optional[datetime] = None,
    ) -> str:
        """常规预约：要求当时有效的资格、授权与政策窗口。"""
        _require_aware(at, "at")
        if due_at is not None:
            _require_aware(due_at, "due_at")
        self._require_elder(elder_id)
        scheduled_dt = _noon(scheduled_date, at)
        entry = self._catalog_entry_at(service_code, scheduled_dt)
        if entry is None:
            raise DomainError("policy_inactive", f"{service_code} 在预约日期无有效政策")
        if not self._entitlement_covering(elder_id, service_code, scheduled_dt):
            raise DomainError("entitlement_missing", "老人在该日期无此服务资格")
        if not self._consent_covering(elder_id, entry.required_scope, scheduled_dt):
            raise DomainError("consent_missing", "缺少覆盖该服务范围的有效授权")
        if facility_id is not None:
            self._check_facility_usable(facility_id, entry.required_scope, scheduled_date)
        if caregiver_id is not None:
            self._check_qualification(caregiver_id, entry, scheduled_dt)
        return self._create_appointment(
            elder_id=elder_id,
            service_code=service_code,
            kind=entry.required_scope,
            scheduled_date=scheduled_date,
            facility_id=facility_id,
            caregiver_id=caregiver_id,
            origin=ORIGIN_MANUAL,
            due_at=due_at,
            at=at,
        )

    def record_fulfillment(
        self,
        appointment_id: str,
        performer_id: str,
        performed_at: datetime,
        outcome: str,
        details: Optional[dict] = None,
        _report: Optional[dict] = None,
    ) -> str:
        """生成履约记录：只在当时有效的政策与授权范围内生成，并快照两者。

        紧急来源的预约允许在授权补齐前履约，但明细只允许最小字段集，
        不因此打开隐私边界。
        """
        _require_aware(performed_at, "performed_at")
        appointment = self.appointments.get(appointment_id)
        if appointment is None:
            raise DomainError("unknown_appointment", f"预约不存在: {appointment_id}")
        if appointment.status not in APPT_OPEN_STATES:
            raise DomainError("invalid_state", f"预约状态不允许履约: {appointment.status}")
        entry = self._catalog_entry_at(appointment.service_code, performed_at)
        if entry is None:
            raise DomainError("policy_inactive", "履约时点无有效政策，不能生成记录")
        details = dict(details or {})
        consent = self._consent_covering(
            appointment.elder_id, entry.required_scope, performed_at
        )
        emergency = appointment.origin == ORIGIN_EMERGENCY
        if consent is None:
            if not emergency:
                raise DomainError("consent_missing", "履约时点缺少有效授权")
            extra = set(details) - EMERGENCY_DETAIL_FIELDS
            if extra:
                raise DomainError(
                    "privacy_boundary",
                    "紧急记录只允许最小字段集",
                    {"rejected_fields": sorted(extra)},
                )
        if entry.requires_qualification:
            self._check_qualification(performer_id, entry, performed_at)
        record_id = f"rec-{appointment_id}"
        data = {
            "record_id": record_id,
            "appointment_id": appointment_id,
            "elder_id": appointment.elder_id,
            "service_code": appointment.service_code,
            "performed_at": _iso(performed_at),
            "performer_id": performer_id,
            "outcome": outcome,
            "policy_version": entry.policy_version,
            "consent_id": consent.consent_id if consent else None,
            "emergency": consent is None,
            "details": details,
        }
        if _report:
            data.update(_report)
        aggregate_type = "visit_record" if appointment.kind == SCOPE_VISIT else "appointment"
        self._emit(
            "SERVICE_FULFILLED",
            aggregate_type,
            appointment_id,
            performed_at,
            f"生成履约记录 {record_id}",
            data,
        )
        return record_id

    # ------------------------------------------------------------------
    # 紧急风险：先上门与转介，后补材料
    # ------------------------------------------------------------------

    def escalate_risk(
        self,
        elder_id: str,
        level: str,
        at: datetime,
        opened_by: str,
        referral_org: Optional[str] = None,
    ) -> str:
        """登记风险升级。urgent 立即生成上门探访（24 小时内）并记录转介，
        材料（评估/资格/授权）允许后补，但补齐前不能结案。"""
        _require_aware(at, "at")
        self._require_elder(elder_id)
        if level not in ESCALATION_RESPONSE_HOURS:
            raise DomainError("unknown_level", "仅 high / urgent 需要升级响应")
        if any(
            item.elder_id == elder_id and item.status == ESC_OPEN
            for item in self.escalations.values()
        ):
            raise DomainError("escalation_open", "该老人已有未结案的升级")
        missing = self._missing_materials(elder_id, at)
        escalation_id = f"esc-{len(self.escalations) + 1:04d}"
        response_due = at + timedelta(hours=ESCALATION_RESPONSE_HOURS[level])
        self._emit(
            "RISK_ESCALATED",
            "elder_profile",
            elder_id,
            at,
            f"风险升级 {escalation_id}（{level}）",
            {
                "escalation_id": escalation_id,
                "elder_id": elder_id,
                "level": level,
                "opened_at": _iso(at),
                "opened_by": opened_by,
                "response_due_at": _iso(response_due),
                "materials_pending": bool(missing),
                "missing_materials": list(missing),
                "materials_due_at": _iso(at + timedelta(hours=72)),
                "referral_org": referral_org,
            },
        )
        if level == RISK_URGENT:
            self._create_appointment(
                elder_id=elder_id,
                service_code="home_visit",
                kind=SCOPE_VISIT,
                scheduled_date=at.date(),
                facility_id=None,
                caregiver_id=None,
                origin=ORIGIN_EMERGENCY,
                due_at=response_due,
                at=at,
            )
            if referral_org:
                self._emit(
                    "EMERGENCY_REFERRAL",
                    "elder_profile",
                    elder_id,
                    at,
                    f"紧急转介至 {referral_org}",
                    {"escalation_id": escalation_id, "elder_id": elder_id, "referral_org": referral_org},
                )
        return escalation_id

    def complete_materials(self, escalation_id: str, at: datetime) -> None:
        """补齐材料：评估、资格、授权齐备后才能解除待补状态。"""
        _require_aware(at, "at")
        escalation = self._require_escalation(escalation_id)
        if not escalation.materials_pending:
            raise DomainError("invalid_state", "该升级没有待补材料")
        missing = self._missing_materials(escalation.elder_id, at)
        if missing:
            raise DomainError("materials_incomplete", "材料未补齐", {"missing": list(missing)})
        self._emit(
            "MATERIALS_COMPLETED",
            "elder_profile",
            escalation.elder_id,
            at,
            f"升级 {escalation_id} 材料补齐",
            {"escalation_id": escalation_id, "elder_id": escalation.elder_id},
        )

    def resolve_escalation(self, escalation_id: str, at: datetime, resolution: str) -> None:
        _require_aware(at, "at")
        escalation = self._require_escalation(escalation_id)
        if escalation.materials_pending:
            raise DomainError("materials_pending", "材料未补齐，不能结案")
        self._emit(
            "ESCALATION_RESOLVED",
            "elder_profile",
            escalation.elder_id,
            at,
            f"升级 {escalation_id} 结案",
            {"escalation_id": escalation_id, "elder_id": escalation.elder_id, "resolution": resolution},
        )

    def _missing_materials(self, elder_id: str, at: datetime) -> tuple[str, ...]:
        missing = []
        if not any(
            item.elder_id == elder_id and item.valid_until > at
            for item in self.assessments.values()
        ):
            missing.append("assessment")
        if not self._entitlement_covering(elder_id, "home_visit", at):
            missing.append("entitlement")
        if not self._consent_covering(elder_id, SCOPE_VISIT, at):
            missing.append("consent")
        return tuple(missing)

    # ------------------------------------------------------------------
    # 迁居：只影响未来安排
    # ------------------------------------------------------------------

    def relocate_elder(self, elder_id: str, new_village_id: str, effective_at: datetime) -> list[str]:
        """老人迁居：居住村自生效时间起变更，生效日之后的预约取消，
        已发生的履约记录保持不变。返回取消的预约 id。"""
        _require_aware(effective_at, "effective_at")
        self._require_elder(elder_id)
        self._require_village(new_village_id)
        self._emit(
            "ELDER_RELOCATED",
            "elder_profile",
            elder_id,
            effective_at,
            f"老人迁居至 {new_village_id}",
            {"elder_id": elder_id, "new_village_id": new_village_id, "effective_at": _iso(effective_at)},
        )
        cancelled = []
        for appointment in self._future_appointments(elder_id, effective_at):
            self._cancel_appointment(appointment, effective_at, "relocated")
            cancelled.append(appointment.appointment_id)
        return cancelled

    # ------------------------------------------------------------------
    # 设施停业：只改派受影响日期
    # ------------------------------------------------------------------

    def close_facility(
        self, facility_id: str, closure_start: date, closure_end: date, at: datetime, reason: str
    ) -> dict[str, list[str]]:
        """临时停业：仅改派停业区间内的预约；区间外、其他设施、其他村的
        计划继续运行。改派目标需同类设施、当日未停业且容量未满。"""
        _require_aware(at, "at")
        facility = self.facilities.get(facility_id)
        if facility is None:
            raise DomainError("unknown_facility", f"设施不存在: {facility_id}")
        if closure_end < closure_start:
            raise DomainError("invalid_window", "停业结束日期不能早于开始日期")
        self._emit(
            "FACILITY_CLOSED",
            "care_facility",
            facility_id,
            at,
            f"设施 {facility_id} 停业 {closure_start} 至 {closure_end}",
            {
                "facility_id": facility_id,
                "closure_start": closure_start.isoformat(),
                "closure_end": closure_end.isoformat(),
                "reason": reason,
            },
        )
        reassigned: list[str] = []
        unassigned: list[str] = []
        affected = [
            item
            for item in self.appointments.values()
            if item.facility_id == facility_id
            and item.status in APPT_OPEN_STATES
            and closure_start <= item.scheduled_date <= closure_end
        ]
        for appointment in sorted(affected, key=lambda item: (item.scheduled_date, item.appointment_id)):
            target = self._find_reassignment(facility, appointment)
            self._emit(
                "APPOINTMENT_REASSIGNED",
                "appointment",
                appointment.appointment_id,
                at,
                f"预约 {appointment.appointment_id} 改派",
                {
                    "appointment_id": appointment.appointment_id,
                    "new_facility_id": target.facility_id if target else None,
                    "reason": f"facility_closed:{facility_id}",
                },
            )
            (reassigned if target else unassigned).append(appointment.appointment_id)
        return {"reassigned": reassigned, "unassigned": unassigned}

    def _find_reassignment(
        self, closed: Facility, appointment: Appointment
    ) -> Optional[Facility]:
        closed_village = self.jurisdictions.get(closed.village_id)
        township_id = closed_village.parent_id if closed_village else None

        def rank(candidate: Facility) -> tuple[int, str]:
            if candidate.village_id == closed.village_id:
                return (0, candidate.facility_id)
            village = self.jurisdictions.get(candidate.village_id)
            if village and village.parent_id == township_id:
                return (1, candidate.facility_id)
            return (2, candidate.facility_id)

        candidates = [
            item
            for item in self.facilities.values()
            if item.facility_id != closed.facility_id
            and item.kind == closed.kind
            and not item.closed_on(appointment.scheduled_date)
            and self._facility_load(item.facility_id, appointment.scheduled_date)
            < item.daily_capacity
        ]
        return min(candidates, key=rank, default=None)

    # ------------------------------------------------------------------
    # 机构报送：稳定业务键幂等，矛盾交复核
    # ------------------------------------------------------------------

    def ingest_report(
        self,
        org_id: str,
        elder_id: str,
        service_code: str,
        service_date: date,
        outcome: str,
        received_at: datetime,
        performer_id: str,
        report_key: Optional[str] = None,
        details: Optional[dict] = None,
    ) -> IngestResult:
        """机构报送履约事实。

        - 同一稳定业务键、内容一致：返回既有记录（含晚到的重复报送）；
        - 同一业务键、内容矛盾：登记冲突交指定复核人，原记录不变；
        - 新报送：按常规履约校验（政策、授权、资质）后生成记录。
        """
        _require_aware(received_at, "received_at")
        business_key = report_key or f"{org_id}|{elder_id}|{service_code}|{service_date.isoformat()}"
        payload_hash = hashlib.sha256(
            json.dumps(
                {
                    "elder_id": elder_id,
                    "service_code": service_code,
                    "service_date": service_date.isoformat(),
                    "outcome": outcome,
                    "details": details or {},
                },
                ensure_ascii=False,
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        late = service_date < received_at.date()
        receipt = self.reports.get(business_key)
        if receipt is not None:
            if receipt.payload_hash == payload_hash:
                return IngestResult(status="duplicate", record_id=receipt.record_id, late=late)
            reviewer = self.responsible_officer(org_id, SERVICE_REPORT_REVIEW, received_at)
            if reviewer is None:
                raise DomainError("reviewer_missing", f"机构 {org_id} 未指定报送复核人")
            conflict_id = f"conflict-{len(self.conflicts) + 1:04d}"
            self._emit(
                "REPORT_CONFLICT",
                "report_conflict",
                conflict_id,
                received_at,
                f"报送内容矛盾，交复核人 {reviewer}",
                {
                    "conflict_id": conflict_id,
                    "business_key": business_key,
                    "org_id": org_id,
                    "reviewer_id": reviewer,
                    "existing_record_id": receipt.record_id,
                    "existing_hash": receipt.payload_hash,
                    "received_hash": payload_hash,
                },
            )
            return IngestResult(status="conflict", conflict_id=conflict_id, late=late)
        appointment_id = self._create_appointment(
            elder_id=elder_id,
            service_code=service_code,
            kind=self._scope_of(service_code, received_at) or service_code,
            scheduled_date=service_date,
            facility_id=None,
            caregiver_id=None,
            origin=ORIGIN_REPORT,
            due_at=None,
            at=received_at,
        )
        record_id = self.record_fulfillment(
            appointment_id,
            performer_id=performer_id,
            performed_at=_noon(service_date, received_at),
            outcome=outcome,
            details=details,
            _report={
                "business_key": business_key,
                "payload_hash": payload_hash,
                "org_id": org_id,
                "received_at": _iso(received_at),
            },
        )
        return IngestResult(status="accepted", record_id=record_id, late=late)

    # ------------------------------------------------------------------
    # 内部命令助手
    # ------------------------------------------------------------------

    def _create_appointment(
        self,
        elder_id: str,
        service_code: str,
        kind: str,
        scheduled_date: date,
        facility_id: Optional[str],
        caregiver_id: Optional[str],
        origin: str,
        due_at: Optional[datetime],
        at: datetime,
    ) -> str:
        appointment_id = f"apt-{len(self.appointments) + 1:04d}"
        event_type = "VISIT_ASSIGNED" if kind == SCOPE_VISIT else "APPOINTMENT_SCHEDULED"
        aggregate_type = "visit_record" if kind == SCOPE_VISIT else "appointment"
        self._emit(
            event_type,
            aggregate_type,
            appointment_id,
            at,
            f"安排服务 {appointment_id}（{service_code} {scheduled_date}）",
            {
                "appointment_id": appointment_id,
                "elder_id": elder_id,
                "service_code": service_code,
                "kind": kind,
                "scheduled_date": scheduled_date.isoformat(),
                "facility_id": facility_id,
                "caregiver_id": caregiver_id,
                "origin": origin,
                "due_at": _iso(due_at) if due_at else None,
            },
        )
        return appointment_id

    def _cancel_appointment(self, appointment: Appointment, at: datetime, reason: str) -> None:
        aggregate_type = "visit_record" if appointment.kind == SCOPE_VISIT else "appointment"
        self._emit(
            "APPOINTMENT_CANCELLED",
            aggregate_type,
            appointment.appointment_id,
            at,
            f"取消预约 {appointment.appointment_id}（{reason}）",
            {"appointment_id": appointment.appointment_id, "reason": reason},
        )

    def _future_appointments(self, elder_id: str, effective_at: datetime) -> list[Appointment]:
        return [
            item
            for item in self.appointments.values()
            if item.elder_id == elder_id
            and item.status in APPT_OPEN_STATES
            and item.scheduled_date >= effective_at.date()
        ]

    # ------------------------------------------------------------------
    # 内部查询
    # ------------------------------------------------------------------

    def _require_elder(self, elder_id: str) -> ElderProfile:
        elder = self.elders.get(elder_id)
        if elder is None:
            raise DomainError("unknown_elder", f"老人不存在: {elder_id}")
        return elder

    def _require_village(self, village_id: str) -> None:
        node = self.jurisdictions.get(village_id)
        if node is None or node.level != LEVEL_VILLAGE:
            raise DomainError("unknown_village", f"村不存在: {village_id}")

    def _require_escalation(self, escalation_id: str) -> Escalation:
        escalation = self.escalations.get(escalation_id)
        if escalation is None:
            raise DomainError("unknown_escalation", f"升级不存在: {escalation_id}")
        if escalation.status != ESC_OPEN:
            raise DomainError("invalid_state", "升级已结案")
        return escalation

    def _known_service_codes(self) -> set[str]:
        return {entry.service_code for entry in self.catalog.values()}

    def _scope_of(self, service_code: str, at: datetime) -> Optional[str]:
        entry = self._catalog_entry_at(service_code, at)
        if entry is not None:
            return entry.required_scope
        latest = max(
            (item for item in self.catalog.values() if item.service_code == service_code),
            key=lambda item: item.effective_from,
            default=None,
        )
        return latest.required_scope if latest else None

    def _catalog_entry_at(self, service_code: str, at: datetime) -> Optional[CatalogEntry]:
        matches = [
            entry
            for entry in self.catalog.values()
            if entry.service_code == service_code
            and entry.effective_from <= at
            and (entry.effective_to is None or at < entry.effective_to)
        ]
        return max(matches, key=lambda entry: entry.effective_from, default=None)

    def _consent_covering(
        self, elder_id: str, scope: str, at: datetime
    ) -> Optional[ConsentGrant]:
        for consent in self.consents.values():
            if consent.elder_id == elder_id and consent.covers(scope, at):
                return consent
        return None

    def _entitlement_covering(self, elder_id: str, service_code: str, at: datetime) -> bool:
        return any(
            item.elder_id == elder_id and item.covers(service_code, at)
            for item in self.entitlements.values()
        )

    def _facility_load(self, facility_id: str, day: date) -> int:
        return sum(
            1
            for item in self.appointments.values()
            if item.facility_id == facility_id
            and item.scheduled_date == day
            and item.status in APPT_OPEN_STATES
        )

    def _check_facility_usable(self, facility_id: str, required_scope: str, day: date) -> None:
        facility = self.facilities.get(facility_id)
        if facility is None:
            raise DomainError("unknown_facility", f"设施不存在: {facility_id}")
        expected_kind = SERVICE_FACILITY_KIND.get(required_scope)
        if expected_kind is None:
            raise DomainError("facility_not_applicable", f"{required_scope} 类服务不使用设施")
        if facility.kind != expected_kind:
            raise DomainError("facility_kind_mismatch", "设施类型与服务不匹配")
        if facility.closed_on(day):
            raise DomainError("facility_closed", "设施当日停业")
        if self._facility_load(facility_id, day) >= facility.daily_capacity:
            raise DomainError("capacity_exceeded", "设施当日容量已满")

    def _check_qualification(
        self, caregiver_id: str, entry: CatalogEntry, at: datetime
    ) -> None:
        cert = entry.requires_qualification
        if cert is None:
            return
        caregiver = self.caregivers.get(caregiver_id)
        qualification = caregiver.qualifications.get(cert) if caregiver else None
        if qualification is None or qualification.valid_until < at:
            raise DomainError("qualification_missing", f"护理人员缺少有效资质: {cert}")

    def subtree_ids(self, node_id: str) -> set[str]:
        """节点及其全部下级节点 id。"""
        result = {node_id}
        frontier = [node_id]
        while frontier:
            current = frontier.pop()
            children = [
                node.node_id
                for node in self.jurisdictions.values()
                if node.parent_id == current
            ]
            result.update(children)
            frontier.extend(children)
        return result

    # ------------------------------------------------------------------
    # 事件应用（只依据事件数据，保证重放一致）
    # ------------------------------------------------------------------

    @staticmethod
    def _data(event: Mapping[str, Any]) -> Mapping[str, Any]:
        return event.get("data", {})

    def _apply_jurisdiction(self, event: Mapping[str, Any]) -> None:
        data = self._data(event)
        self.jurisdictions[data["node_id"]] = Jurisdiction(
            node_id=data["node_id"],
            level=data["level"],
            name=data["name"],
            parent_id=data["parent_id"],
        )

    def _apply_catalog(self, event: Mapping[str, Any]) -> None:
        data = self._data(event)
        self.catalog[data["catalog_id"]] = CatalogEntry(
            catalog_id=data["catalog_id"],
            service_code=data["service_code"],
            name=data["name"],
            required_scope=data["required_scope"],
            policy_version=data["policy_version"],
            effective_from=_parse_dt(data["effective_from"]),
            effective_to=_parse_dt(data["effective_to"]) if data["effective_to"] else None,
            requires_qualification=data["requires_qualification"],
        )

    def _apply_responsibility(self, event: Mapping[str, Any]) -> None:
        data = self._data(event)
        self.responsibilities.append(
            Responsibility(
                node_id=data["node_id"],
                service_code=data["service_code"],
                officer_id=data["officer_id"],
                effective_from=_parse_dt(data["effective_from"]),
                effective_to=_parse_dt(data["effective_to"]) if data["effective_to"] else None,
            )
        )

    def _apply_facility(self, event: Mapping[str, Any]) -> None:
        data = self._data(event)
        self.facilities[data["facility_id"]] = Facility(
            facility_id=data["facility_id"],
            village_id=data["village_id"],
            kind=data["kind"],
            daily_capacity=data["daily_capacity"],
        )

    def _apply_closure(self, event: Mapping[str, Any]) -> None:
        data = self._data(event)
        facility = self.facilities[data["facility_id"]]
        facility.closures.append(
            (date.fromisoformat(data["closure_start"]), date.fromisoformat(data["closure_end"]))
        )

    def _apply_caregiver(self, event: Mapping[str, Any]) -> None:
        data = self._data(event)
        qualifications = {
            item["cert"]: Qualification(
                cert=item["cert"], level=item["level"], valid_until=_parse_dt(item["valid_until"])
            )
            for item in data["qualifications"]
        }
        self.caregivers[data["caregiver_id"]] = Caregiver(
            caregiver_id=data["caregiver_id"], org_id=data["org_id"], qualifications=qualifications
        )

    def _apply_elder(self, event: Mapping[str, Any]) -> None:
        data = self._data(event)
        registered_at = _parse_dt(str(event["occurred_at"]))
        self.elders[data["elder_id"]] = ElderProfile(
            elder_id=data["elder_id"],
            name=data["name"],
            living_situation=data["living_situation"],
            residence_village_id=data["residence_village_id"],
            registered_village_id=data["registered_village_id"],
            contact=data["contact"],
            id_number=data.get("id_number"),
            residence_history=[(data["residence_village_id"], registered_at)],
        )

    def _apply_consent(self, event: Mapping[str, Any]) -> None:
        data = self._data(event)
        self.consents[data["consent_id"]] = ConsentGrant(
            consent_id=data["consent_id"],
            elder_id=data["elder_id"],
            channel=data["channel"],
            scopes=frozenset(data["scopes"]),
            effective_from=_parse_dt(data["effective_from"]),
            effective_to=_parse_dt(data["effective_to"]) if data["effective_to"] else None,
            agent_id=data["agent_id"],
            agent_relation=data["agent_relation"],
            witness_ids=tuple(data["witness_ids"]),
        )

    def _apply_withdrawal(self, event: Mapping[str, Any]) -> None:
        data = self._data(event)
        consent = self.consents[data["consent_id"]]
        consent.withdrawn_at = _parse_dt(data["effective_at"])

    def _apply_relocation(self, event: Mapping[str, Any]) -> None:
        data = self._data(event)
        elder = self.elders[data["elder_id"]]
        effective_at = _parse_dt(data["effective_at"])
        elder.residence_village_id = data["new_village_id"]
        elder.residence_history.append((data["new_village_id"], effective_at))

    def _apply_assessment(self, event: Mapping[str, Any]) -> None:
        data = self._data(event)
        self.assessments[data["assessment_id"]] = Assessment(
            assessment_id=data["assessment_id"],
            elder_id=data["elder_id"],
            risk_level=data["risk_level"],
            needs=tuple(data["needs"]),
            assessor_id=data["assessor_id"],
            assessed_at=_parse_dt(data["assessed_at"]),
            valid_until=_parse_dt(data["valid_until"]),
        )

    def _apply_entitlement(self, event: Mapping[str, Any]) -> None:
        data = self._data(event)
        self.entitlements[data["entitlement_id"]] = Entitlement(
            entitlement_id=data["entitlement_id"],
            elder_id=data["elder_id"],
            service_codes=frozenset(data["service_codes"]),
            valid_from=_parse_dt(data["valid_from"]),
            valid_to=_parse_dt(data["valid_to"]) if data["valid_to"] else None,
            basis_assessment_id=data["basis_assessment_id"],
        )

    def _apply_appointment(self, event: Mapping[str, Any]) -> None:
        data = self._data(event)
        self.appointments[data["appointment_id"]] = Appointment(
            appointment_id=data["appointment_id"],
            elder_id=data["elder_id"],
            service_code=data["service_code"],
            kind=data["kind"],
            scheduled_date=date.fromisoformat(data["scheduled_date"]),
            facility_id=data["facility_id"],
            caregiver_id=data["caregiver_id"],
            origin=data["origin"],
            due_at=_parse_dt(data["due_at"]) if data["due_at"] else None,
        )

    def _apply_reassignment(self, event: Mapping[str, Any]) -> None:
        data = self._data(event)
        appointment = self.appointments[data["appointment_id"]]
        appointment.facility_id = data["new_facility_id"]
        appointment.status = APPT_REASSIGNED if data["new_facility_id"] else APPT_UNASSIGNED

    def _apply_cancellation(self, event: Mapping[str, Any]) -> None:
        data = self._data(event)
        appointment = self.appointments[data["appointment_id"]]
        appointment.status = APPT_CANCELLED
        appointment.cancel_reason = data["reason"]

    def _apply_fulfillment(self, event: Mapping[str, Any]) -> None:
        data = self._data(event)
        record = FulfillmentRecord(
            record_id=data["record_id"],
            appointment_id=data["appointment_id"],
            elder_id=data["elder_id"],
            service_code=data["service_code"],
            performed_at=_parse_dt(data["performed_at"]),
            performer_id=data["performer_id"],
            outcome=data["outcome"],
            policy_version=data["policy_version"],
            consent_id=data["consent_id"],
            emergency=data["emergency"],
            details=dict(data["details"]),
        )
        self.records[record.record_id] = record
        appointment = self.appointments.get(record.appointment_id)
        if appointment is not None:
            appointment.status = APPT_FULFILLED
        if data.get("business_key"):
            self.reports[data["business_key"]] = ReportReceipt(
                business_key=data["business_key"],
                payload_hash=data["payload_hash"],
                record_id=record.record_id,
                org_id=data["org_id"],
                received_at=_parse_dt(data["received_at"]),
            )

    def _apply_escalation(self, event: Mapping[str, Any]) -> None:
        data = self._data(event)
        self.escalations[data["escalation_id"]] = Escalation(
            escalation_id=data["escalation_id"],
            elder_id=data["elder_id"],
            level=data["level"],
            opened_at=_parse_dt(data["opened_at"]),
            opened_by=data["opened_by"],
            response_due_at=_parse_dt(data["response_due_at"]),
            materials_pending=data["materials_pending"],
            missing_materials=tuple(data["missing_materials"]),
            materials_due_at=_parse_dt(data["materials_due_at"]) if data["materials_due_at"] else None,
            referral_org=data["referral_org"],
        )

    def _apply_referral(self, event: Mapping[str, Any]) -> None:
        data = self._data(event)
        escalation = self.escalations.get(data["escalation_id"])
        if escalation is not None:
            escalation.referral_org = data["referral_org"]

    def _apply_materials_completed(self, event: Mapping[str, Any]) -> None:
        data = self._data(event)
        escalation = self.escalations[data["escalation_id"]]
        escalation.materials_pending = False
        escalation.missing_materials = ()

    def _apply_escalation_resolved(self, event: Mapping[str, Any]) -> None:
        data = self._data(event)
        escalation = self.escalations[data["escalation_id"]]
        escalation.status = ESC_RESOLVED
        escalation.resolved_at = _parse_dt(str(event["occurred_at"]))
        escalation.resolution = data["resolution"]

    def _apply_conflict(self, event: Mapping[str, Any]) -> None:
        data = self._data(event)
        self.conflicts[data["conflict_id"]] = ReportConflict(
            conflict_id=data["conflict_id"],
            business_key=data["business_key"],
            org_id=data["org_id"],
            reviewer_id=data["reviewer_id"],
            existing_record_id=data["existing_record_id"],
            existing_hash=data["existing_hash"],
            received_hash=data["received_hash"],
            opened_at=_parse_dt(str(event["occurred_at"])),
        )
