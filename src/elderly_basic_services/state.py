"""事件流的纯函数重放：把领域事件归约为当前状态。

状态只由事件派生，网络重启后重放事件日志即可恢复探访期限、
升级状态、授权区间、设施停业区间等全部运行状态。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any


def to_dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def to_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value)
    if "T" in text:
        return to_dt(text).date()
    return date.fromisoformat(text)


@dataclass
class Grant:
    grant_id: str
    service_codes: tuple[str, ...]
    purpose: str
    grantee_id: str | None
    grantee_role: str | None
    auth_method: str
    valid_from: date | None
    valid_to: date | None
    withdrawn_at: datetime | None = None
    delegated_by: str | None = None
    witness: str | None = None

    def covers(self, service_code: str, purpose: str, at: date) -> bool:
        if self.purpose != purpose:
            return False
        if self.service_codes and service_code not in self.service_codes:
            return False
        if self.valid_from and at < self.valid_from:
            return False
        if self.valid_to and at > self.valid_to:
            return False
        return True

    def effective_at(self, at: datetime) -> bool:
        day = at.date()
        if self.valid_from and day < self.valid_from:
            return False
        if self.valid_to and day > self.valid_to:
            return False
        if self.withdrawn_at is not None and self.withdrawn_at <= at:
            return False
        return True


@dataclass
class Assessment:
    assessment_id: str
    assessed_at: datetime
    assessor_id: str
    service_codes: tuple[str, ...]
    risk_level: str
    findings: str
    visit_due_by: date | None
    valid_until: date | None

    def active_on(self, day: date) -> bool:
        if self.valid_until is not None and day > self.valid_until:
            return False
        return self.assessed_at.date() <= day


@dataclass
class Elder:
    elder_id: str
    name: str
    identity_number: str | None
    categories: tuple[str, ...]
    contacts: dict[str, str]
    home_village: str
    residence_history: list[tuple[date, str]] = field(default_factory=list)
    assessments: list[Assessment] = field(default_factory=list)
    grants: dict[str, Grant] = field(default_factory=dict)
    risk_level: str = "none"
    escalated: bool = False
    open_emergency: str | None = None
    registered_at: datetime | None = None

    def residence_on(self, day: date) -> str:
        current = self.home_village
        for effective, village in self.residence_history:
            if effective <= day:
                current = village
        return current

    @property
    def residence(self) -> str:
        current = self.home_village
        for effective, village in self.residence_history:
            current = village
        return current

    def latest_assessment(self, day: date | None = None) -> Assessment | None:
        active = [a for a in self.assessments if day is None or a.active_on(day)]
        return active[-1] if active else None


@dataclass
class PolicyVersion:
    service_code: str
    policy_version: int
    name: str
    category: str
    payment_tier: str
    cadence_days: int | None
    required_for: tuple[str, ...]
    effective_from: date
    effective_to: date | None
    published_at: datetime
    responsibilities: dict[str, str] = field(default_factory=dict)


@dataclass
class Entitlement:
    elder_id: str
    service_code: str
    grants: list[dict[str, Any]] = field(default_factory=list)
    revocations: list[dict[str, Any]] = field(default_factory=list)

    def active_on(self, day: date) -> dict[str, Any] | None:
        current = None
        for item in sorted(self.grants, key=lambda g: g["valid_from"]):
            if item["valid_from"] <= day and (item["valid_to"] is None or day <= item["valid_to"]):
                current = item
        if current is None:
            return None
        for rev in self.revocations:
            if rev["effective_from"] <= day and current["valid_from"] <= rev["effective_from"]:
                return None
        return current


@dataclass
class Interval:
    start: date
    end: date | None
    reason: str = ""


@dataclass
class Facility:
    facility_id: str
    name: str
    facility_type: str
    village: str
    capacity: int
    service_codes: tuple[str, ...]
    suspensions: list[Interval] = field(default_factory=list)

    def suspended_on(self, day: date) -> Interval | None:
        for interval in self.suspensions:
            if interval.start <= day and (interval.end is None or day < interval.end):
                return interval
        return None


@dataclass
class Caregiver:
    caregiver_id: str
    name: str
    facility_id: str | None
    village: str
    service_codes: tuple[str, ...]
    qualifications: list[dict[str, Any]] = field(default_factory=list)

    def qualified(self, service_code: str, day: date) -> dict[str, Any] | None:
        for qual in self.qualifications:
            codes = qual.get("service_codes") or ()
            if codes and service_code not in codes:
                continue
            start = to_date(qual.get("valid_from"))
            end = to_date(qual.get("valid_to"))
            if start and day < start:
                continue
            if end and day > end:
                continue
            return qual
        return None


@dataclass
class Appointment:
    appointment_id: str
    elder_id: str
    service_code: str
    status: str = "requested"
    requested_by: str | None = None
    requested_for: datetime | None = None
    scheduled_for: datetime | None = None
    facility_id: str | None = None
    caregiver_id: str | None = None
    delivery_mode: str = "onsite"
    snapshots: dict[str, Any] = field(default_factory=dict)
    reschedules: list[dict[str, Any]] = field(default_factory=list)
    cancel_reason: str | None = None
    visit_id: str | None = None
    referral_id: str | None = None
    material_status: str = "complete"
    paper_completed_at: datetime | None = None


@dataclass
class Referral:
    referral_id: str
    elder_id: str
    opened_at: datetime
    severity: str
    description: str
    reporter_id: str
    legal_basis: str
    responders: tuple[str, ...]
    open_visits: list[str] = field(default_factory=list)
    closed_at: datetime | None = None
    resolution: str | None = None
    outcome: str | None = None

    @property
    def is_open(self) -> bool:
        return self.closed_at is None


@dataclass
class Submission:
    submission_key: str
    status: str  # applied / referred_for_review
    record_type: str
    provider_id: str
    receipt: dict[str, Any] = field(default_factory=dict)
    review_id: str | None = None


@dataclass
class ReviewCase:
    review_id: str
    submission_key: str
    record_type: str
    reviewer_id: str
    reason_code: str
    detail: str
    payload: dict[str, Any]
    existing: dict[str, Any]
    opened_at: datetime
    status: str = "open"
    resolution: dict[str, Any] | None = None


@dataclass
class NetworkState:
    elders: dict[str, Elder] = field(default_factory=dict)
    identities: dict[str, str] = field(default_factory=dict)
    policies: dict[str, list[PolicyVersion]] = field(default_factory=dict)
    entitlements: dict[str, Entitlement] = field(default_factory=dict)
    facilities: dict[str, Facility] = field(default_factory=dict)
    caregivers: dict[str, Caregiver] = field(default_factory=dict)
    appointments: dict[str, Appointment] = field(default_factory=dict)
    referrals: dict[str, Referral] = field(default_factory=dict)
    visits: list[dict[str, Any]] = field(default_factory=list)
    submissions: dict[str, Submission] = field(default_factory=dict)
    reviews: dict[str, ReviewCase] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)

    def policy_on(self, service_code: str, day: date) -> PolicyVersion | None:
        current = None
        for policy in self.policies.get(service_code, []):
            if policy.effective_from <= day and (policy.effective_to is None or day <= policy.effective_to):
                current = policy
        return current

    def entitlement_on(self, elder_id: str, service_code: str, day: date) -> dict[str, Any] | None:
        ent = self.entitlements.get(f"{elder_id}:{service_code}")
        return ent.active_on(day) if ent else None

    def county_of(self, village: str) -> str:
        return village.split("/", 1)[0]


def apply(state: NetworkState, event: dict[str, Any]) -> NetworkState:  # noqa: C901 - 事件归约
    state.events.append(event)
    etype = event["event_type"]
    p = event.get("payload", {})
    at = to_dt(event["occurred_at"])

    if etype == "POLICY_PUBLISHED":
        state.policies.setdefault(p["service_code"], []).append(
            PolicyVersion(
                service_code=p["service_code"],
                policy_version=p["policy_version"],
                name=p["name"],
                category=p["category"],
                payment_tier=p.get("payment_tier", "basic"),
                cadence_days=p.get("cadence_days"),
                required_for=tuple(p.get("required_for_categories", [])),
                effective_from=to_date(p["effective_from"]) or at.date(),
                effective_to=to_date(p.get("effective_to")),
                published_at=at,
                responsibilities=dict(p.get("responsibilities", {})),
            )
        )
    elif etype == "ELDER_REGISTERED":
        elder = Elder(
            elder_id=p["elder_id"],
            name=p["name"],
            identity_number=p.get("identity_number"),
            categories=tuple(p.get("categories", [])),
            contacts=dict(p.get("contacts", {})),
            home_village=p["residence_village"],
            registered_at=at,
        )
        state.elders[p["elder_id"]] = elder
        if elder.identity_number:
            state.identities[elder.identity_number] = elder.elder_id
    elif etype == "ELDER_RELOCATED":
        state.elders[p["elder_id"]].residence_history.append((to_date(p["effective_date"]) or at.date(), p["new_village"]))
    elif etype == "CONSENT_GRANTED":
        grant = Grant(
            grant_id=p["grant_id"],
            service_codes=tuple(p.get("service_codes", [])),
            purpose=p["purpose"],
            grantee_id=p.get("grantee_id"),
            grantee_role=p.get("grantee_role"),
            auth_method=p["auth_method"],
            valid_from=to_date(p.get("valid_from")),
            valid_to=to_date(p.get("valid_to")),
            delegated_by=p.get("delegated_by"),
            witness=p.get("witness"),
        )
        state.elders[p["elder_id"]].grants[grant.grant_id] = grant
    elif etype == "CONSENT_WITHDRAWN":
        grant = state.elders[p["elder_id"]].grants.get(p["grant_id"])
        if grant is not None:
            grant.withdrawn_at = at
    elif etype == "NEED_ASSESSED":
        elder = state.elders[p["elder_id"]]
        elder.assessments.append(
            Assessment(
                assessment_id=p["assessment_id"],
                assessed_at=at,
                assessor_id=p["assessment_by"],
                service_codes=tuple(p.get("service_codes", [])),
                risk_level=p.get("risk_level", "none"),
                findings=p.get("findings", ""),
                visit_due_by=to_date(p.get("visit_due_by")),
                valid_until=to_date(p.get("valid_until")),
            )
        )
        elder.risk_level = p.get("risk_level", "none")
    elif etype == "RISK_ESCALATED":
        state.elders[p["elder_id"]].escalated = True
        state.elders[p["elder_id"]].risk_level = p.get("risk_level", state.elders[p["elder_id"]].risk_level)
    elif etype == "EMERGENCY_TRIGGERED":
        state.elders[p["elder_id"]].escalated = True
        state.elders[p["elder_id"]].open_emergency = p["referral_id"]
    elif etype == "REFERRAL_OPENED":
        state.referrals[p["referral_id"]] = Referral(
            referral_id=p["referral_id"],
            elder_id=p["elder_id"],
            opened_at=at,
            severity=p["severity"],
            description=p["description"],
            reporter_id=p["reporter_id"],
            legal_basis=p.get("legal_basis", "emergency_life_safety"),
            responders=tuple(p.get("informed_responders", [])),
            open_visits=list(p.get("visit_ids", [])),
        )
    elif etype == "REFERRAL_CLOSED":
        ref = state.referrals[p["referral_id"]]
        ref.closed_at = at
        ref.resolution = p["resolution"]
        ref.outcome = p.get("outcome", "")
        elder = state.elders[ref.elder_id]
        if elder.open_emergency == ref.referral_id:
            elder.open_emergency = None
            elder.escalated = False
    elif etype == "ENTITLEMENT_GRANTED":
        key = f"{p['elder_id']}:{p['service_code']}"
        ent = state.entitlements.setdefault(key, Entitlement(p["elder_id"], p["service_code"]))
        ent.grants.append(
            {
                "entitlement_id": p["entitlement_id"],
                "valid_from": to_date(p["valid_from"]),
                "valid_to": to_date(p.get("valid_to")),
                "policy_version": p.get("policy_version"),
                "assessment_id": p.get("assessment_id"),
                "granted_by": p.get("granted_by"),
                "granted_at": at,
            }
        )
    elif etype == "ENTITLEMENT_REVOKED":
        key = f"{p['elder_id']}:{p['service_code']}"
        state.entitlements[key].revocations.append(
            {"effective_from": to_date(p["effective_from"]) or at.date(), "reason": p.get("reason", "")}
        )
    elif etype == "FACILITY_REGISTERED":
        state.facilities[p["facility_id"]] = Facility(
            facility_id=p["facility_id"],
            name=p["name"],
            facility_type=p["facility_type"],
            village=p["village_code"],
            capacity=int(p["capacity"]),
            service_codes=tuple(p.get("service_codes", [])),
        )
    elif etype == "FACILITY_SUSPENDED":
        state.facilities[p["facility_id"]].suspensions.append(
            Interval(to_date(p["date_from"]) or at.date(), to_date(p.get("date_to")), p.get("reason", ""))
        )
    elif etype == "FACILITY_RESUMED":
        facility = state.facilities[p["facility_id"]]
        day = to_date(p["effective_date"]) or at.date()
        for interval in facility.suspensions:
            if interval.end is None and interval.start <= day:
                interval.end = day
    elif etype == "CAREGIVER_REGISTERED":
        state.caregivers[p["caregiver_id"]] = Caregiver(
            caregiver_id=p["caregiver_id"],
            name=p["name"],
            facility_id=p.get("facility_id"),
            village=p["village_code"],
            service_codes=tuple(p.get("service_codes", [])),
            qualifications=list(p.get("qualifications", [])),
        )
    elif etype in ("SERVICE_REQUESTED", "APPOINTMENT_SCHEDULED", "APPOINTMENT_RESCHEDULED",
                   "APPOINTMENT_CANCELLED", "VISIT_ASSIGNED", "SERVICE_FULFILLED",
                   "PAPERWORK_COMPLETED"):
        _apply_appointment_event(state, event, at)
    elif etype == "SUBMISSION_ACCEPTED":
        state.submissions[p["submission_key"]] = Submission(
            submission_key=p["submission_key"],
            status=p.get("status", "applied"),
            record_type=p["record_type"],
            provider_id=p["provider_id"],
            receipt=dict(p.get("receipt", {})),
            review_id=p.get("review_id"),
        )
    elif etype == "SUBMISSION_DUPLICATE_RETURNED":
        # 回执事件不改变既有业务状态，仅留下可追溯的报送记录。
        pass
    elif etype == "REVIEW_OPENED":
        state.reviews[p["review_id"]] = ReviewCase(
            review_id=p["review_id"],
            submission_key=p["submission_key"],
            record_type=p["record_type"],
            reviewer_id=p["reviewer_id"],
            reason_code=p["reason_code"],
            detail=p.get("detail", ""),
            payload=dict(p.get("payload", {})),
            existing=dict(p.get("existing", {})),
            opened_at=at,
        )
    elif etype == "REVIEW_RESOLVED":
        state.reviews[p["review_id"]].status = "resolved"
        state.reviews[p["review_id"]].resolution = {
            "decision": p["decision"],
            "note": p.get("note", ""),
            "resolved_by": p["resolved_by"],
            "resolved_at": at.isoformat(),
            "correction": p.get("correction"),
        }
    return state


def _apply_appointment_event(state: NetworkState, event: dict[str, Any], at: datetime) -> None:
    etype = event["event_type"]
    p = event.get("payload", {})
    appt = state.appointments.get(p.get("appointment_id", ""))
    if etype == "SERVICE_REQUESTED":
        state.appointments[p["appointment_id"]] = Appointment(
            appointment_id=p["appointment_id"],
            elder_id=p["elder_id"],
            service_code=p["service_code"],
            requested_by=p.get("requested_by"),
            requested_for=to_dt(p["requested_for"]) if p.get("requested_for") else at,
            referral_id=p.get("referral_id"),
            material_status="pending" if p.get("emergency") else "complete",
        )
        return
    if appt is None:
        return
    if etype == "APPOINTMENT_SCHEDULED":
        appt.status = "scheduled"
        appt.scheduled_for = to_dt(p["scheduled_for"])
        appt.facility_id = p.get("facility_id")
        appt.caregiver_id = p.get("caregiver_id")
        appt.delivery_mode = p.get("delivery_mode", "onsite")
        appt.snapshots = dict(p.get("snapshots", {}))
        if p.get("referral_id"):
            appt.referral_id = p["referral_id"]
            appt.material_status = "pending"
    elif etype == "VISIT_ASSIGNED":
        appt.caregiver_id = p["caregiver_id"]
        appt.snapshots = {**appt.snapshots, **dict(p.get("snapshots", {}))}
    elif etype == "APPOINTMENT_RESCHEDULED":
        appt.reschedules.append(
            {
                "from_time": appt.scheduled_for.isoformat() if appt.scheduled_for else None,
                "from_facility": appt.facility_id,
                "to_time": p.get("scheduled_for"),
                "to_facility": p.get("facility_id", appt.facility_id),
                "reason": p.get("reason", ""),
                "at": at.isoformat(),
            }
        )
        if p.get("scheduled_for"):
            appt.scheduled_for = to_dt(p["scheduled_for"])
        if "facility_id" in p:
            appt.facility_id = p["facility_id"]
        if p.get("caregiver_id"):
            appt.caregiver_id = p["caregiver_id"]
        if p.get("delivery_mode"):
            appt.delivery_mode = p["delivery_mode"]
        if p.get("snapshots"):
            appt.snapshots = dict(p["snapshots"])
        appt.status = "scheduled"
        appt.cancel_reason = None
    elif etype == "APPOINTMENT_CANCELLED":
        appt.status = "cancelled"
        appt.cancel_reason = p.get("reason", "")
    elif etype == "SERVICE_FULFILLED":
        visit = {
            "visit_id": p["visit_id"],
            "appointment_id": appt.appointment_id,
            "elder_id": appt.elder_id,
            "service_code": appt.service_code,
            "caregiver_id": p.get("caregiver_id", appt.caregiver_id),
            "facility_id": p.get("facility_id", appt.facility_id),
            "fulfilled_at": p["fulfilled_at"],
            "service_date": to_date(p["fulfilled_at"]).isoformat(),
            "note": p.get("note", ""),
            "snapshots": dict(p.get("snapshots", appt.snapshots)),
            "correction_of": p.get("correction_of"),
            "event_id": event["event_id"],
        }
        state.visits.append(visit)
        if not p.get("correction_of"):
            appt.status = "fulfilled"
            appt.visit_id = visit["visit_id"]
    elif etype == "PAPERWORK_COMPLETED":
        appt.material_status = "complete"
        appt.paper_completed_at = at
        appt.snapshots = {**appt.snapshots, "paperwork": p.get("snapshots", {})}
