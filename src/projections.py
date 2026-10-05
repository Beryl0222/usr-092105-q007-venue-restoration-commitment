"""赛后恢复领域的只读投影。

投影不产生新事实，只把事件流整理成对外视图，并强制执行两条读侧纪律：
- 公众开放日历只能读取“已签署的分区重开决定”，未重开或被隔离的区域不出现；
- 场馆资产台账只登记经 ASSET_TRANSFERRED 正式接收的捐赠装置，
  仅有捐赠意向、未完成交接的装置不得计入场馆资产。
"""

from __future__ import annotations

from typing import Any

from .index import StreamIndex, parse_time

SIGNOFF_KINDS = (
    ("ZONE_SAFETY_SIGNED", "safety", "安全"),
    ("ZONE_FUNCTION_SIGNED", "function", "功能"),
    ("ZONE_ASSET_SIGNED", "asset", "资产"),
)


def public_opening_calendar(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """对外日历：仅含已签署重开的分区；隔离区、未完成签署的区域一律不出现。

    课程改签视图从本日历取数——没有重开决定，就没有可售/可迁的名额。
    """
    index = StreamIndex(events)
    index = StreamIndex(events)
    # 有效的资产签署：若签署引用了捐赠交接单，则该交接事件必须真实存在，
    # 否则只是“纸面接收”，不得让分区进入对外日历。
    transfer_docs = {
        e["payload"]["transfer_document_id"]
        for e in events if e["event_type"] == "ASSET_TRANSFERRED"
    }
    signoffs: dict[str, dict[str, str]] = {}
    for event in events:
        if event["event_type"].endswith("_SIGNED") and event["event_type"].startswith("ZONE_"):
            kind = event["event_type"][len("ZONE_"):-len("_SIGNED")].lower()
            p = event["payload"]
            if kind == "asset" and p.get("transfer_document_id") and \
                    p["transfer_document_id"] not in transfer_docs:
                continue  # 交接单不存在，资产签署无效
            signoffs.setdefault(p["zone_id"], {})[kind] = p["signoff_id"]

    calendar: list[dict[str, Any]] = []
    for event in events:
        if event["event_type"] != "ZONE_REOPENED":
            continue
        zid = event["payload"]["zone_id"]
        kinds = signoffs.get(zid, {})
        if not all(k in kinds for k in ("safety", "function", "asset")):
            # 历史脏数据或违规事件：不得进入对外日历
            continue
        calendar.append({
            "zone_id": zid,
            "reopened_at": event["occurred_at"],
            "decision_id": event["event_id"],
            "signoffs": {
                "safety": kinds["safety"],
                "function": kinds["function"],
                "asset": kinds["asset"],
            },
            "open_to_public": True,
        })
    calendar.sort(key=lambda row: row["reopened_at"])
    return calendar


def course_rebooking_view(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """课程改签台账：每条改签必须能回溯到一个已开放分区。"""
    open_zones = {row["zone_id"]: row for row in public_opening_calendar(events)}
    view: list[dict[str, Any]] = []
    for event in events:
        if event["event_type"] != "COURSE_RESCHEDULED":
            continue
        p = event["payload"]
        target = open_zones.get(p["to_zone_id"])
        view.append({
            "course_id": p["course_id"],
            "session_id": p["session_id"],
            "from_zone_id": p["from_zone_id"],
            "to_zone_id": p["to_zone_id"],
            "original_start": p["original_start"],
            "new_start": p["new_start"],
            "status": "已同步开放日历" if target else "改签目标未开放，不得对外发布",
            "based_on_decision_ids": p["based_on_decision_ids"],
            "notified_at": p["notified_at"],
        })
    return view


def damage_trace(events: list[dict[str, Any]], damage_id: str) -> dict[str, Any]:
    """从一处损伤向下展开：赛前基线 → 使用方 → 修复过程 → 费用担保。

    任何一环缺失都如实标注，不做拼接猜测。
    """
    index = StreamIndex(events)
    history = index.damages.get(damage_id, [])
    report = next((e for e in history if e["event_type"] == "DAMAGE_REPORTED"), None)
    if report is None:
        return {"damage_id": damage_id, "found": False}

    p = report["payload"]
    baseline_id = p.get("baseline_id")
    freeze = next((e for e in events if e["event_type"] == "BASELINE_FROZEN"
                   and e["aggregate_id"] == baseline_id), None)

    # 使用方：取该区域在损伤登记前最近一个已进场的占用窗口
    # （损伤常在撤场申报后的联合巡检中发现，因此不要求仍在窗口内）
    occupant = None
    latest_load_in = None
    report_at = parse_time(report["occurred_at"])
    for win in events:
        if win["event_type"] != "OCCUPANCY_WINDOW_DECLARED":
            continue
        wp = win["payload"]
        load_in = parse_time(wp["load_in_at"])
        if p["zone_id"] in wp["zone_ids"] and load_in <= report_at:
            if latest_load_in is None or load_in > latest_load_in:
                latest_load_in = load_in
                occupant = {"occupant": wp["occupant"], "window_id": wp["window_id"]}

    resolution = next((e for e in history if e["event_type"] == "DAMAGE_RESOLVED"), None)
    disputes = [
        {"event_id": e["event_id"], "disputed_by": e["payload"]["disputed_by"],
         "reason": e["payload"]["reason"], "at": e["occurred_at"]}
        for e in history if e["event_type"] == "DAMAGE_DISPUTED"
    ]

    work_orders = []
    for wo in events:
        if wo["event_type"] == "REPAIR_WORK_ORDER_ISSUED" and wo["payload"]["damage_id"] == damage_id:
            woid = wo["payload"]["work_order_id"]
            repaired_by = next(
                (rep for rep in events
                 if rep["event_type"] == "ZONE_REPAIRED" and woid in rep["payload"]["work_order_ids"]),
                None,
            )
            work_orders.append({
                "work_order_id": woid,
                "estimated_cost": wo["payload"]["estimated_cost"],
                "issued_at": wo["occurred_at"],
                "repaired_at": repaired_by["occurred_at"] if repaired_by else None,
                "status": "已完工" if repaired_by else "待修复",
            })

    guarantees = []
    for held in events:
        if held["event_type"] != "COST_GUARANTEE_HELD":
            continue
        if damage_id not in held["payload"]["covers_damage_ids"]:
            continue
        gid = held["payload"]["guarantee_id"]
        settlement = next(
            (e for e in index.guarantees.get(gid, [])
             if e["event_type"] == "COST_GUARANTEE_SETTLED"),
            None,
        )
        guarantees.append({
            "guarantee_id": gid,
            "guarantor": held["payload"]["guarantor"],
            "amount": held["payload"]["amount"],
            "held_at": held["occurred_at"],
            "settled_at": settlement["occurred_at"] if settlement else None,
            "settled_amount": settlement["payload"]["settled_amount"] if settlement else None,
        })

    return {
        "found": True,
        "damage_id": damage_id,
        "zone_id": p["zone_id"],
        "element": f'{p["element_category"]} / {p["element"]}',
        "reported_by": p["reported_by"],
        "reported_at": report["occurred_at"],
        "baseline": {
            "baseline_id": baseline_id,
            "frozen_at": freeze["occurred_at"] if freeze else None,
        },
        "occupant": occupant,
        "evidence_ids": p.get("evidence_ids", []),
        "disputes": disputes,
        "resolution": (
            {"result": resolution["payload"]["resolution"],
             "resolved_by": resolution["payload"]["resolved_by"],
             "at": resolution["occurred_at"]}
            if resolution else None
        ),
        "work_orders": work_orders,
        "guarantees": guarantees,
    }


def venue_asset_register(events: list[dict[str, Any]]) -> dict[str, Any]:
    """场馆资产台账。

    只有 ASSET_TRANSFERRED（双方签署接收）之后的装置才计入场馆资产；
    “拟捐赠”但未交接的装置单列为 pending_donations，禁止入账。
    """
    assets: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
    dispositions: dict[tuple[str, str], dict] = {}

    for event in events:
        etype = event["event_type"]
        p = event.get("payload", {})
        if etype == "ASSET_DISPOSITION_DECLARED" and p["disposition"] == "拟捐赠":
            dispositions[(p["installation_id"], p["asset_id"])] = event
        elif etype == "ASSET_TRANSFERRED":
            key = (p["installation_id"], p["asset_id"])
            declaration = dispositions.get(key)
            assets.append({
                "asset_id": p["asset_id"],
                "installation_id": p["installation_id"],
                "received_at": event["occurred_at"],
                "accepted_by": p["accepted_by"],
                "transfer_document_id": p["transfer_document_id"],
                "declared_at": declaration["occurred_at"] if declaration else None,
            })

    received_ids = {(a["installation_id"], a["asset_id"]) for a in assets}
    for (iid, asset_id), declaration in dispositions.items():
        if (iid, asset_id) not in received_ids:
            pending.append({
                "asset_id": asset_id,
                "installation_id": iid,
                "declared_at": declaration["occurred_at"],
                "note": "仅有捐赠意向，未正式接收，不得计为场馆资产",
            })

    return {"assets": assets, "pending_donations": pending}


def zone_restoration_status(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """分区恢复看板：安全/功能/资产三轨独立，隔离不影响邻区。"""
    rows: dict[str, dict[str, Any]] = {}

    def ensure(zid: str) -> dict[str, Any]:
        return rows.setdefault(zid, {
            "zone_id": zid,
            "safety_signed": False,
            "function_signed": False,
            "asset_signed": False,
            "quarantined": False,
            "reopened": False,
        })

    compared: set[str] = set()
    for event in events:
        etype, p = event["event_type"], event.get("payload", {})
        if etype == "INSPECTION_COMPARED":
            for finding in p.get("findings", []):
                compared.add(finding.get("zone_id"))
                ensure(finding.get("zone_id"))
        elif etype == "ZONE_SAFETY_SIGNED":
            ensure(p["zone_id"])["safety_signed"] = True
        elif etype == "ZONE_FUNCTION_SIGNED":
            ensure(p["zone_id"])["function_signed"] = True
        elif etype == "ZONE_ASSET_SIGNED":
            ensure(p["zone_id"])["asset_signed"] = True
        elif etype == "ZONE_QUARANTINED":
            ensure(p["zone_id"])["quarantined"] = True
        elif etype == "ZONE_REOPENED":
            row = ensure(p["zone_id"])
            row["reopened"] = True
            row["quarantined"] = False

    return sorted(rows.values(), key=lambda r: r["zone_id"])
