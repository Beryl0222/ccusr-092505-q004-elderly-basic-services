"""追加型事件存储与命令侧错误类型。

存储只允许追加：任何对历史事实的修改都以新事件（更正事件、撤回事件、
停业事件、改派事件）表达，因此已发生的探访记录在物理上不可被覆盖。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .contracts import validate_event

SCHEMA_PATH = Path(__file__).resolve().parents[2] / "contracts" / "domain.schema.json"


def load_schema() -> dict[str, Any]:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


@dataclass
class DomainError(Exception):
    code: str
    message: str
    field: str = ""

    def __str__(self) -> str:
        return f"[{self.code}] {self.message}"


class EventStore:
    """JSONL 追加日志；进程重启后重新加载并重放即可恢复全部状态。"""

    def __init__(self, path: str | Path | None = None, clock: Callable[[], Any] | None = None) -> None:
        self.path = Path(path) if path else None
        self.schema = load_schema()
        self._events: list[dict[str, Any]] = []
        self._seen_ids: set[str] = set()
        if self.path and self.path.exists():
            for line_no, line in enumerate(self.path.read_text(encoding="utf-8").splitlines(), start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise DomainError("corrupt_log", f"事件日志第 {line_no} 行无法解析") from exc
                issues = validate_event(event, self.schema)
                if issues:
                    raise DomainError(
                        "corrupt_log", f"事件日志第 {line_no} 行违反契约: {issues[0].code}"
                    )
                if event["event_id"] in self._seen_ids:
                    raise DomainError("corrupt_log", f"事件日志第 {line_no} 行事件编号重复")
                self._seen_ids.add(event["event_id"])
                self._events.append(event)

    @property
    def events(self) -> list[dict[str, Any]]:
        return list(self._events)

    def append(self, event: dict[str, Any]) -> dict[str, Any]:
        issues = validate_event(event, self.schema)
        if issues:
            issue = issues[0]
            raise DomainError(issue.code, issue.message, issue.field)
        if event["event_id"] in self._seen_ids:
            raise DomainError("duplicate_event", "事件编号已存在，禁止重复写入", "event_id")
        self._events.append(event)
        self._seen_ids.add(event["event_id"])
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
        return event
