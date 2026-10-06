"""测试共用的组网夹具。"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from elderly_basic_services.network import ServiceNetwork  # noqa: E402
from elderly_basic_services.store import InMemoryEventStore  # noqa: E402

TZ = timezone(timedelta(hours=8))
T0 = datetime(2026, 1, 1, 9, 0, tzinfo=TZ)


def dt(month: int, day: int, hour: int = 12) -> datetime:
    return datetime(2026, month, day, hour, 0, tzinfo=TZ)


def build_network(store=None) -> ServiceNetwork:
    """两乡三村的县，含目录、责任、设施与护理员。"""
    net = ServiceNetwork(store or InMemoryEventStore())
    net.register_jurisdiction("county-1", "county", "青川县", T0)
    net.register_jurisdiction("town-1", "township", "石桥乡", T0, parent_id="county-1")
    net.register_jurisdiction("town-2", "township", "柳林乡", T0, parent_id="county-1")
    net.register_jurisdiction("village-1", "village", "石桥村", T0, parent_id="town-1")
    net.register_jurisdiction("village-2", "village", "马鞍村", T0, parent_id="town-1")
    net.register_jurisdiction("village-3", "village", "柳林村", T0, parent_id="town-2")
    net.publish_catalog("home_visit", "上门探访", "visit", "P2026", T0, T0)
    net.publish_catalog("meal_service", "老年助餐", "meal", "P2026", T0, T0)
    net.publish_catalog("nursing_care", "居家照护", "care", "P2026", T0, T0, requires_qualification="nursing")
    net.publish_catalog("daily_help", "基本生活协助", "basic_living", "P2026", T0, T0)
    net.assign_responsibility("town-1", "meal_service", "officer-town1-meal", T0, T0)
    net.assign_responsibility("village-1", "home_visit", "officer-v1-visit", T0, T0)
    net.register_facility("mp-1", "village-1", "meal_point", 5, T0)
    net.register_facility("mp-2", "village-2", "meal_point", 5, T0)
    net.register_facility("mp-3", "village-3", "meal_point", 5, T0)
    net.register_facility("nh-1", "village-1", "nursing_home", 3, T0)
    net.register_caregiver(
        "cg-1", "org-1", [{"cert": "nursing", "level": "中级", "valid_until": "2027-01-01T00:00:00+08:00"}], T0
    )
    net.register_caregiver("cg-raw", "org-1", [], T0)
    return net


SCOPE_OF_SERVICE = {
    "home_visit": "visit",
    "meal_service": "meal",
    "nursing_care": "care",
    "daily_help": "basic_living",
}


def add_elder(net: ServiceNetwork, elder_id: str, village: str = "village-1", registered: str = "village-2") -> str:
    return net.register_elder(
        elder_id, f"老人{elder_id}", "living_alone", village, registered, f"139{elder_id}", dt(1, 2)
    )


def qualify(
    net: ServiceNetwork,
    elder_id: str,
    services: list[str],
    at: datetime,
    consent_scopes: list[str] | None = None,
) -> None:
    """走完评估、资格、授权三步，让老人可以预约给定服务。"""
    assessment_id = net.assess_need(elder_id, "medium", services, "assessor-1", at, dt(12, 31))
    net.grant_entitlement(elder_id, services, at, assessment_id, at)
    scopes = consent_scopes or [SCOPE_OF_SERVICE[code] for code in services]
    net.grant_consent(elder_id, "self", scopes, at, at)
