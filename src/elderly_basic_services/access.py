"""按角色的可见性控制。

- 老人本人：查看自己的档案、预约与履约记录；
- 家属代办等代理人：只看授权范围内的服务记录，评估明细需单独授权；
- 村 / 乡 / 县主管：按层级查看管辖子树（服务责任随居住村）；
- 紧急响应人：仅在有未结案的紧急升级时看到最小必要信息；
- 复核人：只看分派给自己的报送冲突。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

from .model import (
    ESC_OPEN,
    RISK_URGENT,
    SCOPE_ASSESSMENT_VIEW,
    DomainError,
    FulfillmentRecord,
)
from .network import ServiceNetwork

ROLE_ELDER = "elder"
ROLE_AGENT = "agent"
ROLE_VILLAGE_OFFICER = "village_officer"
ROLE_TOWNSHIP_OFFICER = "township_officer"
ROLE_COUNTY_OFFICER = "county_officer"
ROLE_EMERGENCY_RESPONDER = "emergency_responder"
ROLE_REVIEWER = "reviewer"

_OFFICER_LEVEL = {
    ROLE_VILLAGE_OFFICER: "village",
    ROLE_TOWNSHIP_OFFICER: "township",
    ROLE_COUNTY_OFFICER: "county",
}


@dataclass(frozen=True)
class Principal:
    """访问主体：角色加必要的身份锚点。"""

    role: str
    node_id: Optional[str] = None  # 主管所辖节点
    elder_id: Optional[str] = None  # 老人本人
    agent_id: Optional[str] = None  # 代理人 / 复核人


def _deny(message: str = "无权访问") -> DomainError:
    return DomainError("access_denied", message)


def _officer_covers(network: ServiceNetwork, principal: Principal, elder_id: str) -> bool:
    expected_level = _OFFICER_LEVEL.get(principal.role)
    if expected_level is None or principal.node_id is None:
        return False
    node = network.jurisdictions.get(principal.node_id)
    if node is None or node.level != expected_level:
        return False
    elder = network.elders.get(elder_id)
    if elder is None:
        return False
    return elder.residence_village_id in network.subtree_ids(principal.node_id)


def _agent_scopes(network: ServiceNetwork, principal: Principal, elder_id: str, at: datetime) -> frozenset[str]:
    scopes: set[str] = set()
    for consent in network.consents.values():
        if consent.elder_id != elder_id or consent.agent_id != principal.agent_id:
            continue
        if consent.withdrawn_at is not None and consent.withdrawn_at <= at:
            continue
        if consent.effective_from > at:
            continue
        if consent.effective_to is not None and consent.effective_to <= at:
            continue
        scopes.update(consent.scopes)
    return frozenset(scopes)


def _open_urgent_escalation(network: ServiceNetwork, elder_id: str) -> bool:
    return any(
        item.elder_id == elder_id and item.status == ESC_OPEN and item.level == RISK_URGENT
        for item in network.escalations.values()
    )


def visible_profile(
    network: ServiceNetwork, principal: Principal, elder_id: str, at: datetime
) -> dict[str, Any]:
    """老人档案的授权视图。"""
    elder = network.elders.get(elder_id)
    if elder is None:
        raise DomainError("unknown_elder", f"老人不存在: {elder_id}")
    if principal.role == ROLE_ELDER and principal.elder_id == elder_id:
        return {
            "elder_id": elder.elder_id,
            "name": elder.name,
            "living_situation": elder.living_situation,
            "residence_village_id": elder.residence_village_id,
            "registered_village_id": elder.registered_village_id,
            "contact": elder.contact,
        }
    if principal.role in _OFFICER_LEVEL and _officer_covers(network, principal, elder_id):
        return {
            "elder_id": elder.elder_id,
            "name": elder.name,
            "living_situation": elder.living_situation,
            "residence_village_id": elder.residence_village_id,
            "registered_village_id": elder.registered_village_id,
            "contact": elder.contact,
        }
    if principal.role == ROLE_AGENT:
        scopes = _agent_scopes(network, principal, elder_id, at)
        if not scopes:
            raise _deny("代理人没有有效授权")
        view: dict[str, Any] = {
            "elder_id": elder.elder_id,
            "name": elder.name,
            "residence_village_id": elder.residence_village_id,
        }
        if SCOPE_ASSESSMENT_VIEW in scopes:
            latest = max(
                (item for item in network.assessments.values() if item.elder_id == elder_id),
                key=lambda item: item.assessed_at,
                default=None,
            )
            if latest is not None:
                view["risk_level"] = latest.risk_level
                view["needs"] = list(latest.needs)
        return view
    if principal.role == ROLE_EMERGENCY_RESPONDER:
        if not _open_urgent_escalation(network, elder_id):
            raise _deny("无进行中的紧急升级")
        return {
            "elder_id": elder.elder_id,
            "name": elder.name,
            "contact": elder.contact,
            "residence_village_id": elder.residence_village_id,
        }
    raise _deny()


def _record_view(
    network: ServiceNetwork, record: FulfillmentRecord, at: datetime, *, full: bool
) -> dict[str, Any]:
    view = {
        "record_id": record.record_id,
        "service_code": record.service_code,
        "performed_at": record.performed_at.isoformat(),
        "outcome": record.outcome,
        "emergency": record.emergency,
    }
    if full:
        view["details"] = dict(record.details)
        view["policy_version"] = record.policy_version
        view["consent_id"] = record.consent_id
    return view


def visible_records(
    network: ServiceNetwork, principal: Principal, elder_id: str, at: datetime
) -> list[dict[str, Any]]:
    """履约记录的授权视图，按发生时间排序。"""
    if elder_id not in network.elders:
        raise DomainError("unknown_elder", f"老人不存在: {elder_id}")
    own = principal.role == ROLE_ELDER and principal.elder_id == elder_id
    officer = principal.role in _OFFICER_LEVEL and _officer_covers(network, principal, elder_id)
    records = sorted(
        (item for item in network.records.values() if item.elder_id == elder_id),
        key=lambda item: item.performed_at,
    )
    if own or officer:
        return [_record_view(network, item, at, full=True) for item in records]
    if principal.role == ROLE_AGENT:
        scopes = _agent_scopes(network, principal, elder_id, at)
        if not scopes:
            raise _deny("代理人没有有效授权")
        visible = []
        for record in records:
            scope = network._scope_of(record.service_code, record.performed_at)
            if scope is None or scope not in scopes:
                continue
            visible.append(
                _record_view(
                    network, record, at, full=SCOPE_ASSESSMENT_VIEW in scopes
                )
            )
        return visible
    raise _deny()


def visible_conflicts(network: ServiceNetwork, principal: Principal) -> list[dict[str, Any]]:
    """复核人只看分派给自己的报送冲突。"""
    if principal.role != ROLE_REVIEWER or principal.agent_id is None:
        raise _deny("仅复核人可查看报送冲突")
    return [
        {
            "conflict_id": item.conflict_id,
            "business_key": item.business_key,
            "org_id": item.org_id,
            "existing_record_id": item.existing_record_id,
            "opened_at": item.opened_at.isoformat(),
            "status": item.status,
        }
        for item in sorted(network.conflicts.values(), key=lambda item: item.opened_at)
        if item.reviewer_id == principal.agent_id
    ]
