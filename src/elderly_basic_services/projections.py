"""按县-乡-村层级的覆盖缺口、服务责任与真实完成情况投影。"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from .model import (
    APPT_CANCELLED,
    APPT_FULFILLED,
    APPT_OPEN_STATES,
    ESC_OPEN,
    SCOPE_VISIT,
    DomainError,
)
from .network import ServiceNetwork


def coverage_report(
    network: ServiceNetwork,
    node_id: str,
    window_start: date,
    window_end: date,
    now: datetime,
) -> dict[str, Any]:
    """生成某节点的覆盖报告，并递归汇总下级节点。

    - 覆盖缺口：已登记但没有有效资格的老人；
    - 服务责任：各服务码当时有效的责任人（沿层级向上兜底）；
    - 真实完成情况：以履约记录为准，而不是预约状态。
    """
    node = network.jurisdictions.get(node_id)
    if node is None:
        raise DomainError("unknown_node", f"层级节点不存在: {node_id}")
    subtree = network.subtree_ids(node_id)
    elders = [
        elder
        for elder in network.elders.values()
        if elder.residence_village_id in subtree and elder.status == "active"
    ]
    elder_ids = {elder.elder_id for elder in elders}
    entitled = {
        elder.elder_id
        for elder in elders
        if any(
            item.elder_id == elder.elder_id and item.valid_from <= now
            and (item.valid_to is None or now < item.valid_to)
            for item in network.entitlements.values()
        )
    }
    appointments = [
        item
        for item in network.appointments.values()
        if item.elder_id in elder_ids and window_start <= item.scheduled_date <= window_end
    ]
    fulfillments = [
        record
        for record in network.records.values()
        if record.elder_id in elder_ids
        and window_start <= record.performed_at.date() <= window_end
    ]
    overdue_visits = [
        item.appointment_id
        for item in network.appointments.values()
        if item.elder_id in elder_ids
        and item.kind == SCOPE_VISIT
        and item.status in APPT_OPEN_STATES
        and item.due_at is not None
        and item.due_at < now
    ]
    open_escalations = [
        item.escalation_id
        for item in network.escalations.values()
        if item.elder_id in elder_ids and item.status == ESC_OPEN
    ]
    service_codes = sorted({entry.service_code for entry in network.catalog.values()})
    responsibility = {
        code: network.responsible_officer(node_id, code, now) for code in service_codes
    }
    children = [
        coverage_report(network, child.node_id, window_start, window_end, now)
        for child in sorted(network.jurisdictions.values(), key=lambda item: item.node_id)
        if child.parent_id == node_id
    ]
    return {
        "node_id": node_id,
        "level": node.level,
        "name": node.name,
        "elders_total": len(elders),
        "entitled": len(entitled),
        "coverage_gap": sorted(elder_ids - entitled),
        "appointments": {
            "scheduled": sum(1 for item in appointments if item.status in APPT_OPEN_STATES),
            "fulfilled": sum(1 for item in appointments if item.status == APPT_FULFILLED),
            "cancelled": sum(1 for item in appointments if item.status == APPT_CANCELLED),
        },
        "fulfillments_real": len(fulfillments),
        "overdue_visits": sorted(overdue_visits),
        "open_escalations": sorted(open_escalations),
        "responsibility": responsibility,
        "children": children,
    }
