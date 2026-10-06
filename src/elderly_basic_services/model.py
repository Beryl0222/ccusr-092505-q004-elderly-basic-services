"""基本养老服务资格网的核心数据模型与错误类型。

所有时间均为带时区的 datetime；日期为 date。实体状态只能由事件应用产生，
履约记录一旦写入不提供任何修改路径。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional


class DomainError(Exception):
    """业务规则冲突。code 为稳定机器可读码，details 携带上下文。"""

    def __init__(self, code: str, message: str, details: Optional[dict] = None) -> None:
        super().__init__(message)
        self.code = code
        self.details = details or {}


# 授权范围：服务范围即授权范围，一一对应。
SCOPE_BASIC_LIVING = "basic_living"  # 基本生活
SCOPE_MEAL = "meal"  # 助餐
SCOPE_VISIT = "visit"  # 探访
SCOPE_CARE = "care"  # 照护
SCOPE_ASSESSMENT_VIEW = "assessment_view"  # 查看评估结果

KNOWN_SCOPES = frozenset(
    {SCOPE_BASIC_LIVING, SCOPE_MEAL, SCOPE_VISIT, SCOPE_CARE, SCOPE_ASSESSMENT_VIEW}
)

# 授权方式
CHANNEL_SELF = "self"  # 本人办理
CHANNEL_FAMILY_PROXY = "family_proxy"  # 家属代办
CHANNEL_WITNESSED = "witnessed"  # 见证协助（老人口头表达、见证人确认）

KNOWN_CHANNELS = frozenset({CHANNEL_SELF, CHANNEL_FAMILY_PROXY, CHANNEL_WITNESSED})

# 居住情形
LIVING_EMPTY_NEST = "empty_nest"  # 空巢
LIVING_ALONE = "living_alone"  # 独居
LIVING_LEFT_BEHIND = "left_behind"  # 留守

KNOWN_LIVING_SITUATIONS = frozenset({LIVING_EMPTY_NEST, LIVING_ALONE, LIVING_LEFT_BEHIND})

# 层级
LEVEL_COUNTY = "county"
LEVEL_TOWNSHIP = "township"
LEVEL_VILLAGE = "village"

KNOWN_LEVELS = frozenset({LEVEL_COUNTY, LEVEL_TOWNSHIP, LEVEL_VILLAGE})
CHILD_LEVEL = {LEVEL_COUNTY: LEVEL_TOWNSHIP, LEVEL_TOWNSHIP: LEVEL_VILLAGE}

# 风险等级
RISK_LOW = "low"
RISK_MEDIUM = "medium"
RISK_HIGH = "high"
RISK_URGENT = "urgent"

KNOWN_RISK_LEVELS = frozenset({RISK_LOW, RISK_MEDIUM, RISK_HIGH, RISK_URGENT})

# 需要升级响应的风险等级及其响应时限（小时）
ESCALATION_RESPONSE_HOURS = {RISK_HIGH: 72, RISK_URGENT: 24}

# 紧急履约记录允许携带的明细字段：紧急处置不得夹带评估等敏感内容。
EMERGENCY_DETAIL_FIELDS = frozenset({"condition_note", "referral_org"})

# 服务所需的设施类型；探访在老人家中进行，不占用设施。
SERVICE_FACILITY_KIND = {
    SCOPE_MEAL: "meal_point",
    SCOPE_CARE: "nursing_home",
}

FACILITY_KINDS = frozenset({"meal_point", "nursing_home"})

# 预约状态
APPT_PLANNED = "planned"
APPT_REASSIGNED = "reassigned"
APPT_UNASSIGNED = "unassigned"  # 改派无可用资源，待人工处理
APPT_CANCELLED = "cancelled"
APPT_FULFILLED = "fulfilled"

APPT_OPEN_STATES = frozenset({APPT_PLANNED, APPT_REASSIGNED})

# 预约来源
ORIGIN_MANUAL = "manual"
ORIGIN_EMERGENCY = "emergency"
ORIGIN_REPORT = "report"  # 机构报送补登

# 升级状态
ESC_OPEN = "open"
ESC_RESOLVED = "resolved"

# 责任分工中的特殊服务码：机构报送矛盾的指定复核人
SERVICE_REPORT_REVIEW = "report_review"


@dataclass
class Jurisdiction:
    """县 / 乡 / 村层级节点。"""

    node_id: str
    level: str
    name: str
    parent_id: Optional[str] = None


@dataclass
class CatalogEntry:
    """服务目录条目，同时承载政策有效期：只有在有效期内的条目才允许履约。"""

    catalog_id: str
    service_code: str
    name: str
    required_scope: str
    policy_version: str
    effective_from: datetime
    effective_to: Optional[datetime]  # None 表示长期有效
    requires_qualification: Optional[str] = None  # 要求的护理资质证书代码


@dataclass
class Responsibility:
    """某节点对某服务（或报送复核）的责任人分工。"""

    node_id: str
    service_code: str
    officer_id: str
    effective_from: datetime
    effective_to: Optional[datetime]


@dataclass
class Facility:
    facility_id: str
    village_id: str
    kind: str  # meal_point / nursing_home
    daily_capacity: int
    closures: list[tuple[date, date]] = field(default_factory=list)  # 停业区间，含端点

    def closed_on(self, day: date) -> bool:
        return any(start <= day <= end for start, end in self.closures)


@dataclass
class Qualification:
    cert: str
    level: str
    valid_until: datetime


@dataclass
class Caregiver:
    caregiver_id: str
    org_id: str
    qualifications: dict[str, Qualification] = field(default_factory=dict)


@dataclass
class ElderProfile:
    elder_id: str
    name: str
    living_situation: str
    residence_village_id: str  # 居住村，服务责任随居住地
    registered_village_id: str  # 户籍村，可与居住村不同（跨村居住）
    contact: str
    id_number: Optional[str] = None
    status: str = "active"
    # 迁居轨迹：(村, 生效时间)，仅追加，不改写历史
    residence_history: list[tuple[str, datetime]] = field(default_factory=list)


@dataclass
class ConsentGrant:
    consent_id: str
    elder_id: str
    channel: str
    scopes: frozenset[str]
    effective_from: datetime
    effective_to: Optional[datetime]
    agent_id: Optional[str] = None
    agent_relation: Optional[str] = None
    witness_ids: tuple[str, ...] = ()
    withdrawn_at: Optional[datetime] = None  # 撤回生效时间，只影响其后的安排

    def covers(self, scope: str, at: datetime) -> bool:
        if scope not in self.scopes:
            return False
        if self.effective_from > at:
            return False
        if self.effective_to is not None and at >= self.effective_to:
            return False
        if self.withdrawn_at is not None and at >= self.withdrawn_at:
            return False
        return True


@dataclass
class Assessment:
    assessment_id: str
    elder_id: str
    risk_level: str
    needs: tuple[str, ...]  # 需要的服务码
    assessor_id: str
    assessed_at: datetime
    valid_until: datetime


@dataclass
class Entitlement:
    entitlement_id: str
    elder_id: str
    service_codes: frozenset[str]
    valid_from: datetime
    valid_to: Optional[datetime]
    basis_assessment_id: str

    def covers(self, service_code: str, at: datetime) -> bool:
        if service_code not in self.service_codes:
            return False
        if self.valid_from > at:
            return False
        if self.valid_to is not None and at >= self.valid_to:
            return False
        return True


@dataclass
class Appointment:
    appointment_id: str
    elder_id: str
    service_code: str
    kind: str  # 取授权范围值：basic_living / meal / visit / care
    scheduled_date: date
    facility_id: Optional[str]
    caregiver_id: Optional[str]
    origin: str  # manual / emergency / report
    due_at: Optional[datetime] = None  # 探访期限
    status: str = APPT_PLANNED
    cancel_reason: Optional[str] = None


@dataclass
class FulfillmentRecord:
    """履约记录：写入即冻结，快照当时有效的政策版本与授权。"""

    record_id: str
    appointment_id: str
    elder_id: str
    service_code: str
    performed_at: datetime
    performer_id: str
    outcome: str
    policy_version: str
    consent_id: Optional[str]  # 紧急记录为 None
    emergency: bool
    details: dict = field(default_factory=dict)


@dataclass
class Escalation:
    escalation_id: str
    elder_id: str
    level: str
    opened_at: datetime
    opened_by: str
    response_due_at: datetime
    materials_pending: bool
    missing_materials: tuple[str, ...] = ()
    materials_due_at: Optional[datetime] = None
    referral_org: Optional[str] = None
    status: str = ESC_OPEN
    resolved_at: Optional[datetime] = None
    resolution: Optional[str] = None


@dataclass
class ReportReceipt:
    """机构报送回执：稳定业务键 -> 既有结果，用于幂等返回。"""

    business_key: str
    payload_hash: str
    record_id: str
    org_id: str
    received_at: datetime


@dataclass
class ReportConflict:
    """同一业务键内容矛盾，交指定复核人，原记录保持不变。"""

    conflict_id: str
    business_key: str
    org_id: str
    reviewer_id: str
    existing_record_id: str
    existing_hash: str
    received_hash: str
    opened_at: datetime
    status: str = "open"
