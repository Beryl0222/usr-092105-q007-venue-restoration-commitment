"""赛后场馆恢复：事件流不变量与只读投影。

规则要点：
- 撤场完成（OCCUPANCY_WINDOW_CLOSED）不等于开放，重开必须同时具备安全、功能、资产三类签署。
- 一切以分区为最小粒度：争议分区可以隔离，已恢复分区不得被连带关闭。
- 巡检证据区分 original（当场采集）与 supplementary（后补材料），后补材料不能顶替原始证据。
- 捐赠装置未完成 ASSET_HANDOVER_SIGNED 前，不得计入场馆资产。
- 对外日历只读取已签署的 ZONE_REOPENED；课程改签只能引用已签署的重开决定。
"""

from __future__ import annotations

# 事件类型 → 所属聚合。既有四类聚合保持不变。
EVENT_AGGREGATES: dict[str, str] = {
    "BASELINE_FROZEN": "venue_baseline",
    "BASELINE_AMENDED": "venue_baseline",
    "INSTALLATION_REGISTERED": "temporary_installation",
    "INSTALLATION_ACCEPTED": "temporary_installation",
    "OCCUPANCY_WINDOW_CLOSED": "temporary_installation",
    "INSPECTION_RECORDED": "restoration_case",
    "DAMAGE_REPORTED": "restoration_case",
    "DAMAGE_DISPUTED": "restoration_case",
    "ZONE_QUARANTINED": "restoration_case",
    "REPAIR_ORDER_OPENED": "restoration_case",
    "ZONE_REPAIRED": "restoration_case",
    "REPAIR_ORDER_CLOSED": "restoration_case",
    "COST_GUARANTEE_HELD": "restoration_case",
    "COST_GUARANTEE_SETTLED": "restoration_case",
    "ASSET_DONATION_OFFERED": "restoration_case",
    "ASSET_HANDOVER_SIGNED": "restoration_case",
    "SAFETY_SIGN_OFF": "reopening_decision",
    "FUNCTION_SIGN_OFF": "reopening_decision",
    "ASSET_SIGN_OFF": "reopening_decision",
    "ZONE_REOPENED": "reopening_decision",
    "COURSE_REBOOKED": "reopening_decision",
}

SIGN_OFF_EVENTS = {
    "SAFETY_SIGN_OFF": "safety",
    "FUNCTION_SIGN_OFF": "function",
    "ASSET_SIGN_OFF": "asset",
}

ENVELOPE_FIELDS = (
    "event_id",
    "event_type",
    "aggregate_type",
    "aggregate_id",
    "occurred_at",
    "version",
    "summary",
)


def _details(event: dict) -> dict:
    d = event.get("details")
    return d if isinstance(d, dict) else {}


def _zone(event: dict) -> str | None:
    return event.get("zone_id") or _details(event).get("zone_id")


def validate_event(record: dict) -> list[str]:
    """校验单个事件信封及事件-聚合配对，返回中文错误列表。"""
    errors = [f"缺少字段：{name}" for name in ENVELOPE_FIELDS if name not in record]
    if errors:
        return errors

    event_type = record.get("event_type")
    if event_type not in EVENT_AGGREGATES:
        errors.append(f"未知事件类型：{event_type}")
    aggregate_type = record.get("aggregate_type")
    if aggregate_type not in set(EVENT_AGGREGATES.values()):
        errors.append(f"未知聚合类型：{aggregate_type}")
    if event_type in EVENT_AGGREGATES and aggregate_type in set(EVENT_AGGREGATES.values()):
        expected = EVENT_AGGREGATES[event_type]
        if aggregate_type != expected:
            errors.append(f"{event_type} 必须归属于聚合 {expected}，实际为 {aggregate_type}")

    version = record.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        errors.append("version 必须是正整数")
    if not isinstance(record.get("summary"), str) or len(record["summary"]) < 2:
        errors.append("summary 必须为不少于 2 个字符的中文摘要")
    return errors


def validate_stream(events: list[dict]) -> list[str]:
    """按追加顺序校验整条事件流，返回全部中文错误（空列表表示通过）。"""
    errors: list[str] = []

    seen_event_ids: set[str] = set()
    # aggregate_id -> 下一个期望版本
    next_version: dict[str, int] = {}

    frozen_zones: set[str] = set()
    baseline_event_ids: dict[str, str] = {}
    registered_installations: set[str] = set()
    installation_zone: dict[str, str] = {}
    closed_installations: set[str] = set()
    closed_zones: set[str] = set()

    # 分区 -> 巡检是否含原始证据
    zone_has_original_evidence: dict[str, bool] = {}
    # damage_id -> {zone, event_id, responsible_party, resolved}
    damages: dict[str, dict] = {}
    unresolved_damage_zones: set[str] = set()
    quarantined_zones: set[str] = set()
    reopened_zones: set[str] = set()

    open_orders: dict[str, str] = {}  # order_id -> zone
    zone_open_order: dict[str, str] = {}
    guarantees: dict[str, dict] = {}  # guarantee_id -> {zone, settled}
    assets: dict[str, dict] = {}  # asset_id -> {zone, signed}
    zone_signoffs: dict[str, set[str]] = {}
    signoff_event_ids: dict[tuple[str, str], str] = {}
    reopen_events: dict[str, dict] = {}  # zone -> 重开事件

    def fail(event: dict, message: str) -> None:
        errors.append(f"[{event.get('event_id', '?')}] {message}")

    for index, event in enumerate(events):
        eid = str(event.get("event_id", f"#{index}"))
        errs = validate_event(event)
        if errs:
            errors.extend(f"[{eid}] {m}" for m in errs)
            continue

        if eid in seen_event_ids:
            fail(event, f"事件标识重复：{eid}（重试必须幂等，不得产生第二条）")
        seen_event_ids.add(eid)

        aggregate_id = event["aggregate_id"]
        expected_version = next_version.get(aggregate_id, 1)
        if event["version"] != expected_version:
            fail(event, f"聚合 {aggregate_id} 版本应为 {expected_version}，实际为 {event['version']}")
        next_version[aggregate_id] = expected_version + 1

        et = event["event_type"]
        d = _details(event)
        zone = _zone(event)
        refs = event.get("refs") or []

        if et == "BASELINE_FROZEN":
            if not zone:
                fail(event, "BASELINE_FROZEN 必须指明 zone_id")
            elif zone in frozen_zones:
                fail(event, f"分区 {zone} 已有冻结基线，不得重复冻结；修订请用 BASELINE_AMENDED")
            if not d.get("baseline_snapshot"):
                fail(event, "缺少 baseline_snapshot（草坪承载、地板孔位、消防、无障碍等赛前记录）")
            if zone:
                frozen_zones.add(zone)
                baseline_event_ids[zone] = eid

        elif et == "BASELINE_AMENDED":
            if not zone:
                fail(event, "BASELINE_AMENDED 必须指明 zone_id")
            elif zone not in frozen_zones:
                fail(event, f"分区 {zone} 尚未冻结基线，不能修订")
            if not d.get("amend_reason"):
                fail(event, "基线修订必须给出 amend_reason，且原始冻结记录不得被覆盖")

        elif et == "INSTALLATION_REGISTERED":
            if not zone:
                fail(event, "临时结构登记必须指明 zone_id")
            elif zone not in frozen_zones:
                fail(event, f"分区 {zone} 未完成进场基线冻结，不得登记临时结构占用")
            if not d.get("structure_name"):
                fail(event, "缺少 structure_name")
            if not d.get("responsible_party"):
                fail(event, "缺少 responsible_party（使用方/赛事运营商）")
            permits = d.get("permits")
            if not isinstance(permits, list) or not permits:
                fail(event, "临时结构必须登记 permits 许可清单")
            else:
                for p in permits:
                    if not isinstance(p, dict) or not p.get("permit_no") or not p.get("permit_type"):
                        fail(event, "每条许可必须包含 permit_no 与 permit_type")
                        break
            window = d.get("occupancy_window")
            if not isinstance(window, dict) or not window.get("start") or not window.get("end"):
                fail(event, "必须登记 occupancy_window 占用窗口起止时间")
            registered_installations.add(aggregate_id)
            if zone:
                installation_zone[aggregate_id] = zone

        elif et in ("INSTALLATION_ACCEPTED", "OCCUPANCY_WINDOW_CLOSED"):
            if aggregate_id not in registered_installations:
                fail(event, f"临时结构 {aggregate_id} 尚未登记，不能{'进场核验' if et == 'INSTALLATION_ACCEPTED' else '报撤场'}")
            if et == "OCCUPANCY_WINDOW_CLOSED":
                closed_installations.add(aggregate_id)
                z = installation_zone.get(aggregate_id)
                if z:
                    closed_zones.add(z)

        elif et == "INSPECTION_RECORDED":
            evidence = d.get("evidence")
            if not isinstance(evidence, list) or not evidence:
                fail(event, "巡检必须提交 evidence 证据清单")
            else:
                has_original = False
                for item in evidence:
                    if not isinstance(item, dict) or not item.get("media_ref") or not item.get("record_type") or not item.get("taken_at"):
                        fail(event, "每条证据必须包含 media_ref、record_type、taken_at")
                        continue
                    if item["record_type"] not in ("original", "supplementary"):
                        fail(event, f"证据类型非法：{item['record_type']}")
                    if item["record_type"] == "original":
                        has_original = True
                if zone:
                    zone_has_original_evidence[zone] = zone_has_original_evidence.get(zone, False) or has_original
                    if not zone_has_original_evidence[zone]:
                        fail(event, "仅有 supplementary 后补材料不能作为原始巡检证据；验收前必须补入 original 证据")

        elif et == "DAMAGE_REPORTED":
            damage_id = d.get("damage_id")
            if not damage_id:
                fail(event, "缺少 damage_id")
            elif damage_id in damages:
                fail(event, f"损伤 {damage_id} 已登记，不得重复")
            if not zone:
                fail(event, "损伤登记必须指明 zone_id")
            elif zone not in frozen_zones:
                fail(event, f"分区 {zone} 无赛前冻结基线，不能登记损伤")
            if d.get("aspect") not in ("turf", "floor", "fire_egress", "accessibility", "other"):
                fail(event, "损伤必须标注 aspect（turf/floor/fire_egress/accessibility/other）")
            if not d.get("severity"):
                fail(event, "缺少 severity")
            if zone and baseline_event_ids.get(zone) not in refs:
                fail(event, "损伤事件必须在 refs 中引用该分区的 BASELINE_FROZEN 事件，保证可追溯赛前基线")
            if damage_id and zone:
                damages[damage_id] = {
                    "zone": zone,
                    "event_id": eid,
                    "responsible_party": d.get("responsible_party"),
                    "resolved": False,
                }
                unresolved_damage_zones.add(zone)

        elif et == "DAMAGE_DISPUTED":
            damage_id = d.get("damage_id")
            if not damage_id or damage_id not in damages:
                fail(event, f"争议必须基于已登记损伤，未找到 damage_id={damage_id}")
            if not d.get("dispute_reason"):
                fail(event, "缺少 dispute_reason")

        elif et == "ZONE_QUARANTINED":
            if not zone:
                fail(event, "隔离必须指明 zone_id")
            elif zone in reopened_zones:
                fail(event, f"分区 {zone} 已重开，不能再隔离")
            zone_damages = [dm for dm in damages.values() if dm["zone"] == zone]
            if not zone_damages:
                fail(event, f"分区 {zone} 无已登记损伤，隔离必须针对具体损伤争议")
            if zone:
                quarantined_zones.add(zone)

        elif et == "REPAIR_ORDER_OPENED":
            damage_id = d.get("damage_id")
            order_id = d.get("repair_order_id")
            if not order_id:
                fail(event, "缺少 repair_order_id")
            elif order_id in open_orders:
                fail(event, f"工单 {order_id} 已存在")
            if not damage_id or damage_id not in damages:
                fail(event, "工单必须关联已登记的 damage_id")
            if zone in reopened_zones:
                fail(event, f"分区 {zone} 已重开")
            if order_id and zone:
                open_orders[order_id] = zone
                zone_open_order[zone] = order_id

        elif et == "ZONE_REPAIRED":
            order_id = d.get("repair_order_id") or zone_open_order.get(zone or "")
            if not zone or open_orders.get(order_id) != zone:
                fail(event, "修复完成必须关联本分区处于开启状态的修复工单")
            else:
                del open_orders[order_id]
                zone_open_order.pop(zone, None)
                # 修复完成：解除该分区的未决损伤与隔离状态（效力仅限本分区）
                unresolved_damage_zones.discard(zone)
                quarantined_zones.discard(zone)
                for dm in damages.values():
                    if dm["zone"] == zone:
                        dm["resolved"] = True
            if not d.get("repair_summary"):
                fail(event, "缺少 repair_summary 修复过程记录")

        elif et == "REPAIR_ORDER_CLOSED":
            order_id = d.get("repair_order_id")
            if not order_id or order_id in open_orders:
                fail(event, "工单只能在 ZONE_REPAIRED 之后关闭")

        elif et == "COST_GUARANTEE_HELD":
            guarantee_id = d.get("guarantee_id")
            damage_id = d.get("damage_id")
            if not guarantee_id:
                fail(event, "缺少 guarantee_id")
            elif guarantee_id in guarantees:
                fail(event, f"费用担保 {guarantee_id} 已登记")
            if not damage_id or damage_id not in damages:
                fail(event, "费用担保必须关联已登记损伤 damage_id")
            if not isinstance(d.get("amount"), (int, float)):
                fail(event, "担保必须写明 amount")
            if guarantee_id and damage_id in damages:
                guarantees[guarantee_id] = {"zone": damages[damage_id]["zone"], "settled": False}

        elif et == "COST_GUARANTEE_SETTLED":
            guarantee_id = d.get("guarantee_id")
            if not guarantee_id or guarantee_id not in guarantees:
                fail(event, f"结算前必须先扣留担保：{guarantee_id}")
            elif guarantees.get(guarantee_id, {}).get("settled"):
                fail(event, f"担保 {guarantee_id} 已结算")
            if d.get("guarantee_status") not in ("released", "claimed", "partial_claim"):
                fail(event, "结算必须给出 guarantee_status（released/claimed/partial_claim）")
            if guarantee_id in guarantees:
                guarantees[guarantee_id]["settled"] = True

        elif et == "ASSET_DONATION_OFFERED":
            asset_id = d.get("asset_id")
            if not asset_id:
                fail(event, "缺少 asset_id")
            elif asset_id in assets:
                fail(event, f"资产 {asset_id} 已登记")
            if not d.get("donor_party"):
                fail(event, "缺少 donor_party（赞助方）")
            if asset_id:
                assets[asset_id] = {"zone": zone, "signed": False, "name": d.get("asset_name")}

        elif et == "ASSET_HANDOVER_SIGNED":
            asset_id = d.get("asset_id")
            if not asset_id or asset_id not in assets:
                fail(event, "交接必须针对已要约捐赠的装置")
            elif assets[asset_id]["signed"]:
                fail(event, f"装置 {asset_id} 已完成交接签署")
            else:
                assets[asset_id]["signed"] = True

        elif et in SIGN_OFF_EVENTS:
            kind = SIGN_OFF_EVENTS[et]
            if not zone:
                fail(event, f"{kind} 验收签署必须指明 zone_id")
                continue
            blocked = (
                zone not in closed_zones
                or zone in unresolved_damage_zones
                or not zone_has_original_evidence.get(zone)
                or kind in zone_signoffs.setdefault(zone, set())
                or not d.get("signed_by")
            )
            if zone not in closed_zones:
                fail(event, f"分区 {zone} 尚未报撤场完成，不能进行 {kind} 验收签署")
            if zone in unresolved_damage_zones:
                fail(event, f"分区 {zone} 存在未修复损伤，{kind} 验收不得签署")
            if not zone_has_original_evidence.get(zone):
                fail(event, f"分区 {zone} 缺少 original 原始巡检证据，后补照片不能支撑 {kind} 验收签署")
            if kind == "asset" and any(a["zone"] == zone and not a["signed"] for a in assets.values()):
                pending = [aid for aid, a in assets.items() if a["zone"] == zone and not a["signed"]]
                fail(event, f"分区 {zone} 存在未正式交接的装置 {pending}，资产验收不得签署，且不得计入场馆资产")
                blocked = True
            if kind in zone_signoffs.setdefault(zone, set()):
                fail(event, f"分区 {zone} 的 {kind} 验收已签署，不得重复")
            if not d.get("signed_by"):
                fail(event, "签署必须记录 signed_by 责任人")
            if blocked:
                continue
            zone_signoffs.setdefault(zone, set()).add(kind)
            signoff_event_ids[(zone, kind)] = eid

        elif et == "ZONE_REOPENED":
            if not zone:
                fail(event, "重开必须指明 zone_id")
                continue
            if zone not in frozen_zones:
                fail(event, f"分区 {zone} 无冻结基线")
            if zone not in closed_zones:
                fail(event, "撤场完成只是前提：未报 OCCUPANCY_WINDOW_CLOSED 不得重开")
            missing = {"safety", "function", "asset"} - zone_signoffs.get(zone, set())
            if missing:
                fail(event, f"分区 {zone} 重开缺少验收签署：{sorted(missing)}；撤场完成不等于可以开放")
            for kind in ("safety", "function", "asset"):
                sid = signoff_event_ids.get((zone, kind))
                if sid and sid not in refs:
                    fail(event, f"重开事件必须在 refs 中引用 {kind} 签署事件 {sid}")
            if zone in quarantined_zones:
                fail(event, f"分区 {zone} 仍处隔离状态，不得重开")
            if zone in reopened_zones:
                fail(event, f"分区 {zone} 已重开，重开决定不可重复下达")
            if not d.get("reopen_from"):
                fail(event, "重开必须给出 reopen_from 对外开放时间")
            reopened_zones.add(zone)
            reopen_events[zone] = event

        elif et == "COURSE_REBOOKED":
            course_id = d.get("course_id")
            decision_id = d.get("rebook_from_decision")
            new_slot = d.get("new_slot")
            if not course_id:
                fail(event, "缺少 course_id")
            if not decision_id:
                fail(event, "改签必须在 rebook_from_decision 引用已签署的 ZONE_REOPENED 事件")
            target = next((ev for ev in events[: index + 1] if ev.get("event_id") == decision_id), None)
            if decision_id and (target is None or target.get("event_type") != "ZONE_REOPENED"):
                fail(event, f"改签引用的 {decision_id} 不是已签署的重开决定；对外日历只认签署结果")
            if not isinstance(new_slot, dict) or not new_slot.get("start") or not new_slot.get("zone_id"):
                fail(event, "改签必须给出 new_slot.start 与 new_slot.zone_id")
            if target is not None and isinstance(new_slot, dict):
                target_zone = _zone(target)
                if new_slot.get("zone_id") != target_zone:
                    fail(event, f"改签目标分区必须与重开分区一致：{target_zone}")
                reopen_from = _details(target).get("reopen_from")
                if reopen_from and new_slot.get("start", "") < reopen_from:
                    fail(event, "改签时间不得早于分区重开时间")

    return errors


# ---------------------------------------------------------------------------
# 只读投影
# ---------------------------------------------------------------------------


def build_model(events: list[dict]) -> dict:
    """把事件流折叠为查询模型。投影只读事实，不做放宽解释。"""
    zones: dict[str, dict] = {}
    damages: dict[str, dict] = {}
    assets: dict[str, dict] = {}
    guarantees: dict[str, dict] = {}
    installations: dict[str, dict] = {}
    courses: dict[str, dict] = {}
    reopen_by_event: dict[str, dict] = {}

    def zone(zid: str | None) -> dict:
        if zid is None:
            zid = "_unzoned"
        return zones.setdefault(zid, {
            "zone_id": zid,
            "baseline": None,
            "baseline_event_id": None,
            "amendments": [],
            "responsible_party": None,
            "occupancy_closed": False,
            "inspections": [],
            "damages": [],
            "quarantined": False,
            "repair_orders": [],
            "sign_offs": set(),
            "reopened": False,
            "reopen_event_id": None,
            "reopen_from": None,
        })

    for event in events:
        et = event.get("event_type")
        d = _details(event)
        z = zone(_zone(event))
        eid = event.get("event_id")

        if et == "BASELINE_FROZEN":
            z["baseline"] = d.get("baseline_snapshot")
            z["baseline_event_id"] = eid
        elif et == "BASELINE_AMENDED":
            z["amendments"].append({"event_id": eid, "reason": d.get("amend_reason"), "at": event.get("occurred_at")})
        elif et == "INSTALLATION_REGISTERED":
            z["responsible_party"] = d.get("responsible_party")
            installations[event["aggregate_id"]] = {
                "zone_id": _zone(event),
                "structure_name": d.get("structure_name"),
                "permits": d.get("permits", []),
                "occupancy_window": d.get("occupancy_window"),
                "responsible_party": d.get("responsible_party"),
            }
        elif et == "OCCUPANCY_WINDOW_CLOSED":
            z["occupancy_closed"] = True
        elif et == "INSPECTION_RECORDED":
            for item in d.get("evidence", []):
                z["inspections"].append(item)
        elif et == "DAMAGE_REPORTED":
            damages[d["damage_id"]] = {
                "damage_id": d["damage_id"],
                "zone_id": _zone(event),
                "aspect": d.get("aspect"),
                "severity": d.get("severity"),
                "baseline_event_id": next((r for r in event.get("refs", []) if r), None),
                "responsible_party": d.get("responsible_party") or z["responsible_party"],
                "reported_event_id": eid,
                "disputed": False,
                "dispute_reason": None,
                "repair_orders": [],
                "guarantee": None,
                "resolved": False,
            }
            z["damages"].append(d["damage_id"])
        elif et == "DAMAGE_DISPUTED":
            dm = damages.get(d.get("damage_id"))
            if dm:
                dm["disputed"] = True
                dm["dispute_reason"] = d.get("dispute_reason")
        elif et == "ZONE_QUARANTINED":
            z["quarantined"] = True
        elif et == "REPAIR_ORDER_OPENED":
            order = {
                "repair_order_id": d.get("repair_order_id"),
                "damage_id": d.get("damage_id"),
                "contractor": d.get("contractor"),
                "cost_estimate": d.get("cost_estimate"),
                "status": "open",
                "repair_summary": None,
            }
            z["repair_orders"].append(order)
            dm = damages.get(d.get("damage_id"))
            if dm:
                dm["repair_orders"].append(order["repair_order_id"])
        elif et == "ZONE_REPAIRED":
            z["quarantined"] = False
            for order in reversed(z["repair_orders"]):
                if order["status"] == "open":
                    order["status"] = "repaired"
                    order["repair_summary"] = d.get("repair_summary")
                    break
            for dm in damages.values():
                if dm["zone_id"] == _zone(event):
                    dm["resolved"] = True
        elif et == "REPAIR_ORDER_CLOSED":
            for order in reversed(z["repair_orders"]):
                if order["repair_order_id"] == d.get("repair_order_id") and order["status"] == "repaired":
                    order["status"] = "closed"
                    order["actual_cost"] = d.get("amount")
                    break
        elif et == "COST_GUARANTEE_HELD":
            guarantees[d["guarantee_id"]] = {
                "guarantee_id": d["guarantee_id"],
                "damage_id": d.get("damage_id"),
                "zone_id": _zone(event),
                "amount": d.get("amount"),
                "currency": d.get("currency", "CNY"),
                "status": "held",
            }
            dm = damages.get(d.get("damage_id"))
            if dm:
                dm["guarantee"] = d["guarantee_id"]
        elif et == "COST_GUARANTEE_SETTLED":
            g = guarantees.get(d.get("guarantee_id"))
            if g:
                g["status"] = d.get("guarantee_status")
                g["settled_amount"] = d.get("amount")
        elif et == "ASSET_DONATION_OFFERED":
            assets[d["asset_id"]] = {
                "asset_id": d["asset_id"],
                "asset_name": d.get("asset_name"),
                "zone_id": _zone(event),
                "donor_party": d.get("donor_party"),
                "ownership_status": "offered",
                "handover_event_id": None,
            }
        elif et == "ASSET_HANDOVER_SIGNED":
            a = assets.get(d.get("asset_id"))
            if a:
                a["ownership_status"] = "handover_signed"
                a["handover_event_id"] = eid
        elif et in SIGN_OFF_EVENTS:
            z["sign_offs"].add(SIGN_OFF_EVENTS[et])
        elif et == "ZONE_REOPENED":
            z["reopened"] = True
            z["reopen_event_id"] = eid
            z["reopen_from"] = d.get("reopen_from")
            reopen_by_event[eid] = z["zone_id"]
        elif et == "COURSE_REBOOKED":
            courses[d["course_id"]] = {
                "course_id": d["course_id"],
                "course_name": d.get("course_name"),
                "original_slot": d.get("original_slot"),
                "new_slot": d.get("new_slot"),
                "decision_event_id": d.get("rebook_from_decision"),
                "rebook_event_id": eid,
            }

    for z in zones.values():
        z["sign_offs"] = sorted(z["sign_offs"])

    return {
        "zones": zones,
        "damages": damages,
        "assets": assets,
        "guarantees": guarantees,
        "installations": installations,
        "courses": courses,
        "reopen_by_event": reopen_by_event,
    }


def public_calendar(model: dict) -> list[dict]:
    """对外开放日历：只读取已签署的分区重开结果，并附上已同步的课程改签。"""
    calendar = []
    for zid in sorted(zid for zid, z in model["zones"].items() if z["reopened"]):
        z = model["zones"][zid]
        calendar.append({
            "zone_id": zid,
            "reopen_from": z["reopen_from"],
            "decision_event_id": z["reopen_event_id"],
            "courses": [
                {
                    "course_id": c["course_id"],
                    "course_name": c["course_name"],
                    "start": c["new_slot"]["start"],
                    "end": c["new_slot"].get("end"),
                }
                for c in sorted(model["courses"].values(), key=lambda c: c["rebook_event_id"] or "")
                if c["new_slot"]["zone_id"] == zid
            ],
        })
    return calendar


def damage_trace(model: dict, damage_id: str) -> dict | None:
    """从一处损伤展开：赛前基线 → 使用方 → 修复过程 → 费用担保。"""
    dm = model["damages"].get(damage_id)
    if dm is None:
        return None
    z = model["zones"].get(dm["zone_id"], {})
    return {
        "damage": {
            "damage_id": dm["damage_id"],
            "aspect": dm["aspect"],
            "severity": dm["severity"],
            "reported_event_id": dm["reported_event_id"],
            "disputed": dm["disputed"],
            "dispute_reason": dm["dispute_reason"],
            "resolved": dm["resolved"],
        },
        "zone_id": dm["zone_id"],
        "quarantined": z.get("quarantined", False),
        "baseline": z.get("baseline"),
        "baseline_event_id": dm["baseline_event_id"],
        "responsible_party": dm["responsible_party"],
        "original_evidence": [i for i in z.get("inspections", []) if i.get("record_type") == "original"],
        "supplementary_evidence": [i for i in z.get("inspections", []) if i.get("record_type") == "supplementary"],
        "repair_orders": [o for o in z.get("repair_orders", []) if o["repair_order_id"] in dm["repair_orders"]],
        "cost_guarantee": model["guarantees"].get(dm["guarantee"]),
    }


def venue_assets(model: dict) -> list[dict]:
    """场馆资产清册：未正式签署交接的捐赠装置一律不计入。"""
    return [
        a for a in sorted(model["assets"].values(), key=lambda a: a["asset_id"])
        if a["ownership_status"] == "handover_signed"
    ]


def pending_donations(model: dict) -> list[dict]:
    """已要约但尚未完成交接、不得入账的装置。"""
    return [
        a for a in sorted(model["assets"].values(), key=lambda a: a["asset_id"])
        if a["ownership_status"] == "offered"
    ]


def course_plan(model: dict) -> list[dict]:
    """课程改签计划：仅保留指向已签署重开决定的最新一次改签。"""
    return [
        {
            "course_id": c["course_id"],
            "course_name": c["course_name"],
            "original_slot": c["original_slot"],
            "new_slot": c["new_slot"],
            "decision_event_id": c["decision_event_id"],
        }
        for c in sorted(model["courses"].values(), key=lambda c: c["course_id"])
        if c["decision_event_id"] in model["reopen_by_event"]
    ]
