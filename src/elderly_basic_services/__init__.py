"""基本养老服务资格网领域契约与业务核心。"""

from .access import Principal, visible_conflicts, visible_profile, visible_records
from .contracts import ContractIssue, validate_event
from .model import DomainError
from .network import IngestResult, ServiceNetwork
from .projections import coverage_report
from .store import InMemoryEventStore, JsonlEventStore

__all__ = [
    "ContractIssue",
    "DomainError",
    "InMemoryEventStore",
    "IngestResult",
    "JsonlEventStore",
    "Principal",
    "ServiceNetwork",
    "coverage_report",
    "validate_event",
    "visible_conflicts",
    "visible_profile",
    "visible_records",
]
