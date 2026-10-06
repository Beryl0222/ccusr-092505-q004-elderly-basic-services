"""事件存储：内存实现与 JSONL 文件实现。

JsonlEventStore 追加写入、整段重放，系统重启后由 ServiceNetwork.restore
重新应用全部事件，探访期限与升级状态随之恢复。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterator, Mapping, Protocol


class EventStore(Protocol):
    def append(self, event: Mapping[str, Any]) -> None: ...

    def iter(self) -> Iterator[dict[str, Any]]: ...


class InMemoryEventStore:
    def __init__(self) -> None:
        self._events: list[dict[str, Any]] = []

    def append(self, event: Mapping[str, Any]) -> None:
        self._events.append(dict(event))

    def iter(self) -> Iterator[dict[str, Any]]:
        return iter(list(self._events))


class JsonlEventStore:
    """每行一个 JSON 事件的追加式文件存储。"""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)

    def append(self, event: Mapping[str, Any]) -> None:
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True))
            handle.write("\n")

    def iter(self) -> Iterator[dict[str, Any]]:
        if not self._path.exists():
            return iter(())
        with self._path.open("r", encoding="utf-8") as handle:
            return iter([json.loads(line) for line in handle if line.strip()])
