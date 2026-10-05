"""加载并索引赛后恢复领域事件流。

事件流是“只追加”的事实记录：同一聚合的版本从 1 递增，全流按 occurred_at 排序。
索引函数只做结构化整理，不做业务裁决；业务不变量在 validator.py，读模型在 projections.py。
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any


def parse_time(value: str) -> datetime:
    """解析 RFC3339 时间；拒绝无时区信息的时间串。"""
    text = value.replace("Z", "+00:00") if value.endswith("Z") else value
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        raise ValueError("occurred_at 必须带时区")
    return parsed


def load_stream(path: str | Path) -> list[dict[str, Any]]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, dict) and "events" in data:
        data = data["events"]
    if not isinstance(data, list):
        raise ValueError("事件流必须是事件数组，或带 events 数组的对象")
    return data


class StreamIndex:
    """按聚合、区域、业务标识建立的只读索引。"""

    def __init__(self, events: list[dict[str, Any]]):
        self.events = sorted(events, key=lambda e: e["occurred_at"])
        self.by_id: dict[str, dict] = {}
        self.by_aggregate: dict[str, list[dict]] = defaultdict(list)
        self.agg_type: dict[str, str] = {}
        # 业务键 -> 事件（载荷中的标识，与聚合标识独立）
        self.evidence: dict[str, dict] = {}
        self.damages: dict[str, list[dict]] = defaultdict(list)
        self.work_orders: dict[str, dict] = {}
        self.guarantees: dict[str, list[dict]] = defaultdict(list)
        self.inspections: dict[str, list[dict]] = defaultdict(list)
        self.zones: dict[str, list[dict]] = defaultdict(list)
        self.courses: dict[str, list[dict]] = defaultdict(list)

        for event in self.events:
            self.by_id[event["event_id"]] = event
            agg_id = event["aggregate_id"]
            self.by_aggregate[agg_id].append(event)
            self.agg_type.setdefault(agg_id, event["aggregate_type"])
            payload = event.get("payload", {}) or {}
            etype = event["event_type"]

            if etype == "INSPECTION_EVIDENCE_SUBMITTED":
                self.evidence[payload["evidence_id"]] = event
                self.inspections[payload.get("inspection_id", "")].append(event)
            if etype in ("DAMAGE_REPORTED", "DAMAGE_DISPUTED", "DAMAGE_RESOLVED"):
                self.damages[payload["damage_id"]].append(event)
            if etype == "REPAIR_WORK_ORDER_ISSUED":
                self.work_orders[payload["work_order_id"]] = event
            if etype in ("COST_GUARANTEE_HELD", "COST_GUARANTEE_SETTLED"):
                self.guarantees[payload["guarantee_id"]].append(event)
            if etype in ("INSPECTION_OPENED", "INSPECTION_COMPARED"):
                self.inspections[payload["inspection_id"]].append(event)

            zone = payload.get("zone_id")
            if zone:
                self.zones[zone].append(event)
            for zid in payload.get("zone_ids", []) or []:
                self.zones[zid].append(event)
            if "course_id" in payload:
                self.courses[payload["course_id"]].append(event)

    # ---- 时间相关的小工具 -------------------------------------------------

    def happened_before(self, events: list[dict], when: datetime) -> list[dict]:
        return [e for e in events if parse_time(e["occurred_at"]) <= when]

    def first(self, events: list[dict], etype: str) -> dict | None:
        return next((e for e in events if e["event_type"] == etype), None)

    def latest(self, events: list[dict], etype: str) -> dict | None:
        return next((e for e in reversed(events) if e["event_type"] == etype), None)
