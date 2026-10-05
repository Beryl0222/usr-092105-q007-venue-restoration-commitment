"""校验领域事件信封与赛后恢复业务不变量。

信封校验（validate_event）沿用历史约定：缺字段、version 正整数。
事件流校验（validate_stream）在信封之上裁决领域规则，核心立场：
撤场完成不等于开放；安全、功能、资产分别签署；争议区可隔离但不连带邻区；
后补照片不得冒充原始证据；捐赠装置未正式接收不算场馆资产；
对外开放（含课程改签）只能引用已签署的分区重开结果。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .index import StreamIndex, parse_time
from .registry import AGGREGATE_TYPES, EVENT_REGISTRY

REQUIRED = ("event_id", "event_type", "aggregate_type", "aggregate_id", "occurred_at", "version", "summary")

# 损伤类别 -> 被哪一类验收签署“卡住”
CATEGORY_SIGNOFF = {
    "草坪承载": "safety",
    "地板孔位": "safety",
    "消防通道": "safety",
    "结构承载": "safety",
    "无障碍设施": "function",
    "面层外观": "function",
    "设备设施": "function",
    "资产装置": "asset",
}

RESOLUTIONS = ("成立", "不成立")
DISPOSITIONS = ("撤场清运", "拟捐赠")
EVIDENCE_KINDS = ("original", "supplementary")
FINDING_RESULTS = ("一致", "差异")


def validate_event(record: dict) -> list[str]:
    """返回可以直接展示给接入方的中文错误（单条信封校验，向后兼容）。"""
    errors = [f"缺少字段：{name}" for name in REQUIRED if name not in record]
    if "version" in record and (not isinstance(record["version"], int) or record["version"] < 1):
        errors.append("version 必须是正整数")
    if "event_type" in record and record["event_type"] not in EVENT_REGISTRY:
        errors.append(f"未知事件类型：{record['event_type']}")
    if "aggregate_type" in record and record["aggregate_type"] not in AGGREGATE_TYPES:
        errors.append(f"未知聚合类型：{record['aggregate_type']}")
    reg = EVENT_REGISTRY.get(record.get("event_type", ""))
    if reg and record.get("aggregate_type") and record["aggregate_type"] != reg["aggregate"]:
        errors.append(
            f"事件 {record['event_type']} 必须归属聚合 {reg['aggregate']}，"
            f"实际为 {record['aggregate_type']}"
        )
    if reg:
        payload = record.get("payload")
        if not isinstance(payload, dict):
            errors.append("payload 必须是对象")
        else:
            for key in reg["required_payload"]:
                if key not in payload or payload[key] in (None, ""):
                    errors.append(f"payload 缺少字段：{key}")
    if "occurred_at" in record:
        try:
            parse_time(record["occurred_at"])
        except (ValueError, TypeError):
            errors.append("occurred_at 必须是带时区的时间串")
    return errors


def validate_stream(events: list[dict[str, Any]]) -> list[str]:
    """校验整条事件流，返回全部错误（不短路），错误以 [事件/区域 标识] 起头。"""
    errors: list[str] = []
    index = StreamIndex(events)

    # 分区单事件不得携带 zone_ids 批量字段——隔离与重开只允许逐区裁决
    single_zone_events = {
        "ZONE_SAFETY_SIGNED", "ZONE_FUNCTION_SIGNED", "ZONE_ASSET_SIGNED",
        "ZONE_QUARANTINED", "ZONE_REOPENED", "DAMAGE_REPORTED",
    }

    # ---------- 一、信封与流序 ----------
    seen_event_ids: set[str] = set()
    for event in events:
        eid = event.get("event_id", "?")
        for problem in validate_event(event):
            errors.append(f"[事件 {eid}] {problem}")
        if eid in seen_event_ids:
            errors.append(f"[事件 {eid}] event_id 重复")
        seen_event_ids.add(eid)
        if event.get("event_type") in single_zone_events and (event.get("payload") or {}).get("zone_ids"):
            errors.append(f"[事件 {eid}] {event['event_type']} 只能逐区裁决，不得使用 zone_ids")

    for agg_id, agg_events in index.by_aggregate.items():
        for expected_version, event in enumerate(agg_events, start=1):
            if event["version"] != expected_version:
                errors.append(
                    f"[事件 {event['event_id']}] 聚合 {agg_id} 版本应为 {expected_version}，"
                    f"实际 {event['version']}（版本须从 1 连续递增）"
                )
        times = [parse_time(e["occurred_at"]) for e in agg_events]
        for earlier, later in zip(times, times[1:]):
            if earlier > later:
                errors.append(f"[聚合 {agg_id}] 事件发生时间倒序，追加事实不得早于既有事实")

    if errors and not _is_well_formed(index):
        # 结构不完整时后续规则可能误报，先返回结构性错误
        return errors

    freeze = next((e for e in events if e["event_type"] == "BASELINE_FROZEN"), None)
    freeze_at = parse_time(freeze["occurred_at"]) if freeze else None

    # ---------- 二、基线、占用窗口与巡检 ----------
    windows = [e for e in events if e["event_type"] == "OCCUPANCY_WINDOW_DECLARED"]
    for win in windows:
        p = win["payload"]
        load_in, load_out = parse_time(p["load_in_at"]), parse_time(p["load_out_at"])
        if load_in >= load_out:
            errors.append(f"[事件 {win['event_id']}] 占用窗口进场时间必须早于撤场时间")
    for i, a in enumerate(windows):
        for b in windows[i + 1:]:
            pa, pb = a["payload"], b["payload"]
            overlap = set(pa["zone_ids"]) & set(pb["zone_ids"])
            if overlap and parse_time(pa["load_in_at"]) < parse_time(pb["load_out_at"]) and \
                    parse_time(pb["load_in_at"]) < parse_time(pa["load_out_at"]) and \
                    pa["occupant"] != pb["occupant"]:
                errors.append(
                    f"[事件 {b['event_id']}] 区域 {sorted(overlap)} 与窗口 {pa['window_id']} "
                    f"占用期冲突且使用方不同"
                )

    def window_covering(zone_id: str, when: datetime) -> dict | None:
        for win in windows:
            p = win["payload"]
            if zone_id in p["zone_ids"] and parse_time(p["load_in_at"]) <= when <= parse_time(p["load_out_at"]):
                return win
        return None

    inspections: dict[str, dict] = {}
    for event in events:
        p = event.get("payload", {})
        if event["event_type"] == "INSPECTION_OPENED":
            inspections[p["inspection_id"]] = event
            if freeze is None or p["baseline_id"] != freeze["aggregate_id"]:
                errors.append(
                    f"[事件 {event['event_id']}] 巡检 {p['inspection_id']} "
                    f"必须引用已冻结的赛前基线 {freeze['aggregate_id'] if freeze else ''}"
                )
            if parse_time(event["occurred_at"]) <= freeze_at:
                errors.append(f"[事件 {event['event_id']}] 赛后巡检不得早于基线冻结时间")
        if event["event_type"] == "INSPECTION_COMPARED":
            opened = inspections.get(p["inspection_id"])
            if not opened:
                errors.append(f"[事件 {event['event_id']}] 比对前必须先有巡检立案 {p['inspection_id']}")
                continue
            if parse_time(event["occurred_at"]) < parse_time(opened["occurred_at"]):
                errors.append(f"[事件 {event['event_id']}] 比对时间早于巡检立案时间")
            findings = p.get("findings", [])
            if not findings:
                errors.append(f"[事件 {event['event_id']}] 比对结果 findings 不能为空")
            for finding in findings:
                zid = finding.get("zone_id")
                if zid not in opened["payload"]["zone_ids"]:
                    errors.append(
                        f"[事件 {event['event_id']}] 比对发现的区域 {zid} 不在巡检范围 "
                        f"{opened['payload']['zone_ids']} 内"
                    )
                if finding.get("result") not in FINDING_RESULTS:
                    errors.append(
                        f"[事件 {event['event_id']}] 比对结论只能是 {FINDING_RESULTS}，"
                        f"实际 {finding.get('result')}"
                    )

    # ---------- 三、证据：原始 vs 后补，严禁冒充 ----------
    compared_at: dict[str, datetime] = {}
    for event in events:
        if event["event_type"] == "INSPECTION_COMPARED":
            compared_at[event["payload"]["inspection_id"]] = parse_time(event["occurred_at"])

    for event in events:
        if event["event_type"] != "INSPECTION_EVIDENCE_SUBMITTED":
            continue
        p = event["payload"]
        eid, kind = p["evidence_id"], p["evidence_kind"]
        if kind not in EVIDENCE_KINDS:
            errors.append(f"[事件 {event['event_id']}] 证据类型只能是 {EVIDENCE_KINDS}")
            continue
        captured = parse_time(p["captured_at"])
        submitted = parse_time(event["occurred_at"])
        if captured > submitted:
            errors.append(f"[事件 {event['event_id']}] 证据 {eid} 拍摄时间晚于提交时间")
        if kind == "original":
            if freeze_at is None or captured > freeze_at:
                errors.append(
                    f"[事件 {event['event_id']}] 证据 {eid} 标注为原始证据，但拍摄于基线冻结之后；"
                    f"后补照片只能标注 supplementary，不得冒充原始证据"
                )
        elif freeze_at is not None and captured <= freeze_at:
            errors.append(
                f"[事件 {event['event_id']}] 证据 {eid} 拍摄于冻结之前，应作为 original 归档，"
                f"不得降级为后补证据"
            )

    # ---------- 四、临时结构、许可与资产归属 ----------
    installs: dict[str, dict] = {}
    declarations: dict[str, list[dict]] = {}
    transfers: dict[str, list[dict]] = {}
    removals: dict[str, list[dict]] = {}

    for event in events:
        p = event.get("payload", {})
        etype = event["event_type"]
        iid = p.get("installation_id")
        if etype == "INSTALLATION_ACCEPTED":
            installs[iid] = event
            for zid in p["zone_ids"]:
                if not window_covering(zid, parse_time(event["occurred_at"])):
                    errors.append(
                        f"[事件 {event['event_id']}] 临时结构 {iid} 在区域 {zid} "
                        f"进场时没有与之时间匹配的占用窗口申报"
                    )
        elif etype == "ASSET_DISPOSITION_DECLARED":
            accepted = installs.get(iid)
            if not accepted or parse_time(event["occurred_at"]) < parse_time(accepted["occurred_at"]):
                errors.append(f"[事件 {event['event_id']}] 声明资产归属前，临时结构 {iid} 必须已凭许可进场")
            if p["disposition"] not in DISPOSITIONS:
                errors.append(f"[事件 {event['event_id']}] 资产归属只能是 {DISPOSITIONS}")
            prior = [d for d in declarations.get(iid, []) if d["payload"]["asset_id"] == p["asset_id"]]
            if prior:
                errors.append(f"[事件 {event['event_id']}] 资产 {p['asset_id']} 归属声明重复")
            declarations.setdefault(iid, []).append(event)
        elif etype == "ASSET_TRANSFERRED":
            decl = next((d for d in declarations.get(iid, [])
                         if d["payload"]["asset_id"] == p["asset_id"]), None)
            if not decl:
                errors.append(
                    f"[事件 {event['event_id']}] 资产 {p['asset_id']} 未经归属声明不得办理接收"
                )
            elif decl["payload"]["disposition"] != "拟捐赠":
                errors.append(
                    f"[事件 {event['event_id']}] 资产 {p['asset_id']} 归属为"
                    f"“{decl['payload']['disposition']}”，不能按捐赠接收"
                )
            already = [t for t in transfers.get(iid, []) if t["payload"]["asset_id"] == p["asset_id"]]
            if already:
                errors.append(f"[事件 {event['event_id']}] 资产 {p['asset_id']} 已正式接收，不得重复交接")
            removed_before = [r for r in removals.get(iid, [])
                              if parse_time(r["occurred_at"]) < parse_time(event["occurred_at"])]
            if removed_before:
                errors.append(
                    f"[事件 {event['event_id']}] 装置 {iid} 已撤场，无法再向场馆捐赠交接"
                )
            transfers.setdefault(iid, []).append(event)
        elif etype == "INSTALLATION_REMOVED":
            accepted = installs.get(iid)
            if not accepted:
                errors.append(f"[事件 {event['event_id']}] 未经进场许可的结构 {iid} 不能报撤场")
            else:
                extra = set(p["zone_ids"]) - set(accepted["payload"]["zone_ids"])
                if extra:
                    errors.append(f"[事件 {event['event_id']}] 撤场区域 {sorted(extra)} 不在进场登记范围内")
            removals.setdefault(iid, []).append(event)

    def asset_ready_for_zone(zone_id: str, when: datetime) -> list[str]:
        """区域资产验收前，每个进入该区域的装置/资产都必须归属清晰。"""
        blockers = []
        for iid, accepted in installs.items():
            if zone_id not in accepted["payload"]["zone_ids"]:
                continue
            decls = [d for d in declarations.get(iid, [])
                     if parse_time(d["occurred_at"]) <= when]
            if not decls:
                blockers.append(f"装置 {iid} 未声明任何资产归属")
                continue
            for decl in decls:
                asset_id, disposition = decl["payload"]["asset_id"], decl["payload"]["disposition"]
                if disposition == "撤场清运":
                    gone = any(parse_time(r["occurred_at"]) <= when for r in removals.get(iid, []))
                    if not gone:
                        blockers.append(f"运营方资产 {asset_id} 尚未撤场")
                elif disposition == "拟捐赠":
                    received = any(
                        t["payload"]["asset_id"] == asset_id and parse_time(t["occurred_at"]) <= when
                        for t in transfers.get(iid, [])
                    )
                    if not received:
                        blockers.append(f"捐赠装置 {asset_id} 仅有捐赠意向、未正式签署接收")
        return blockers

    # ---------- 五、损伤、争议、工单与修复 ----------
    damages: dict[str, list[dict]] = {}
    work_orders: dict[str, dict] = {}
    repairs: list[dict] = []

    def window_covering_at_any(zone_id: str) -> bool:
        return any(zone_id in w["payload"]["zone_ids"] for w in windows)

    def _resolved_at(damage_id: str, when: datetime) -> dict | None:
        return next(
            (e for e in damages.get(damage_id, [])
             if e["event_type"] == "DAMAGE_RESOLVED" and parse_time(e["occurred_at"]) <= when),
            None,
        )

    for event in events:
        p = event.get("payload", {})
        etype, when = event["event_type"], parse_time(event["occurred_at"])
        if etype == "DAMAGE_REPORTED":
            did = p["damage_id"]
            if did in damages:
                errors.append(f"[事件 {event['event_id']}] 损伤 {did} 重复登记")
            if p["element_category"] not in CATEGORY_SIGNOFF:
                errors.append(
                    f"[事件 {event['event_id']}] 损伤类别只能是 "
                    f"{tuple(CATEGORY_SIGNOFF)}，实际 {p['element_category']}"
                )
            if not window_covering_at_any(p["zone_id"]):
                errors.append(f"[事件 {event['event_id']}] 区域 {p['zone_id']} 从未申报占用窗口")
            if freeze and p["baseline_id"] != freeze["aggregate_id"]:
                errors.append(f"[事件 {event['event_id']}] 损伤 {did} 未引用冻结基线")
            refs = p.get("evidence_ids", [])
            if not refs:
                errors.append(f"[事件 {event['event_id']}] 损伤 {did} 登记必须附带证据")
            originals = 0
            for ref in refs:
                ev = index.evidence.get(ref)
                if not ev:
                    errors.append(f"[事件 {event['event_id']}] 损伤 {did} 引用了不存在的证据 {ref}")
                    continue
                if ev["payload"]["zone_id"] != p["zone_id"]:
                    errors.append(f"[事件 {event['event_id']}] 证据 {ref} 不属于区域 {p['zone_id']}")
                if ev["payload"]["evidence_kind"] == "original":
                    originals += 1
                if parse_time(ev["occurred_at"]) > when:
                    errors.append(f"[事件 {event['event_id']}] 损伤 {did} 引用了事后才提交的证据 {ref}")
            if refs and originals == 0:
                errors.append(
                    f"[事件 {event['event_id']}] 损伤 {did} 只有后补照片，"
                    f"缺少赛前原始证据，不能仅凭后补材料立案"
                )
            damages.setdefault(did, []).append(event)

        elif etype == "DAMAGE_DISPUTED":
            history = damages.get(p["damage_id"], [])
            report = history[0] if history else None
            if not report:
                errors.append(f"[事件 {event['event_id']}] 争议必须针对已登记的损伤 {p['damage_id']}")
            elif _resolved_at(p["damage_id"], when):
                errors.append(f"[事件 {event['event_id']}] 损伤 {p['damage_id']} 已裁定，不能再提争议")
            damages.setdefault(p["damage_id"], []).append(event)

        elif etype == "DAMAGE_RESOLVED":
            did = p["damage_id"]
            report = damages.get(did, [None])[0]
            if not report:
                errors.append(f"[事件 {event['event_id']}] 裁定的损伤 {did} 未登记")
            if _resolved_at(did, when):
                errors.append(f"[事件 {event['event_id']}] 损伤 {did} 已有生效裁定")
            if p["resolution"] not in RESOLUTIONS:
                errors.append(f"[事件 {event['event_id']}] 裁定结论只能是 {RESOLUTIONS}")
            damages.setdefault(did, []).append(event)

        elif etype == "REPAIR_WORK_ORDER_ISSUED":
            woid = p["work_order_id"]
            if woid in work_orders:
                errors.append(f"[事件 {event['event_id']}] 工单 {woid} 重复开具")
            did = p["damage_id"]
            report = damages.get(did, [None])[0]
            if not report:
                errors.append(f"[事件 {event['event_id']}] 工单 {woid} 对应损伤 {did} 未登记")
            elif report["payload"]["zone_id"] != p["zone_id"]:
                errors.append(f"[事件 {event['event_id']}] 工单 {woid} 区域与损伤区域不一致")
            if not isinstance(p["estimated_cost"], (int, float)) or p["estimated_cost"] < 0:
                errors.append(f"[事件 {event['event_id']}] 工单 {woid} 估价必须是非负数字")
            work_orders[woid] = event

        elif etype == "ZONE_REPAIRED":
            repairs.append(event)
            for woid in p["work_order_ids"]:
                wo = work_orders.get(woid)
                if not wo:
                    errors.append(f"[事件 {event['event_id']}] 完工引用了不存在的工单 {woid}")
                elif wo["payload"]["zone_id"] != p["zone_id"]:
                    errors.append(f"[事件 {event['event_id']}] 工单 {woid} 不属于区域 {p['zone_id']}")
                elif parse_time(wo["occurred_at"]) > when:
                    errors.append(f"[事件 {event['event_id']}] 工单 {woid} 开具时间晚于完工时间")
            for ref in p.get("evidence_ids", []):
                ev = index.evidence.get(ref)
                if not ev:
                    errors.append(f"[事件 {event['event_id']}] 完工引用了不存在的证据 {ref}")
                elif parse_time(ev["occurred_at"]) > when:
                    errors.append(f"[事件 {event['event_id']}] 证据 {ref} 提交晚于完工记录")

    def closed_at(damage_id: str, when: datetime) -> bool:
        """损伤在某时点已闭环：裁定不成立，或认账且对应工单全部完工。"""
        resolution = _resolved_at(damage_id, when)
        if not resolution:
            return False
        if resolution["payload"]["resolution"] == "不成立":
            return True
        related = [wo for wo in work_orders.values() if wo["payload"]["damage_id"] == damage_id]
        if not related:
            return False
        done_ids = {woid for rep in repairs if parse_time(rep["occurred_at"]) <= when
                    for woid in rep["payload"]["work_order_ids"]}
        return all(wo["payload"]["work_order_id"] in done_ids for wo in related)

    # ---------- 六、费用担保 ----------
    guarantees: dict[str, list[dict]] = {}
    for event in events:
        p, when = event.get("payload", {}), parse_time(event["occurred_at"])
        if event["event_type"] == "COST_GUARANTEE_HELD":
            gid = p["guarantee_id"]
            if gid in guarantees:
                errors.append(f"[事件 {event['event_id']}] 担保 {gid} 重复冻结")
            if not isinstance(p["amount"], (int, float)) or p["amount"] < 0:
                errors.append(f"[事件 {event['event_id']}] 担保金额必须是非负数字")
            for did in p["covers_damage_ids"]:
                report = damages.get(did, [None])[0]
                if not report:
                    errors.append(f"[事件 {event['event_id']}] 担保 {gid} 覆盖的损伤 {did} 未登记")
                elif parse_time(report["occurred_at"]) > when:
                    errors.append(f"[事件 {event['event_id']}] 担保 {gid} 先于损伤 {did} 登记")
            guarantees.setdefault(gid, []).append(event)
        elif event["event_type"] == "COST_GUARANTEE_SETTLED":
            gid = p["guarantee_id"]
            held = next((e for e in guarantees.get(gid, [])
                         if e["event_type"] == "COST_GUARANTEE_HELD"
                         and parse_time(e["occurred_at"]) <= when), None)
            if not held:
                errors.append(f"[事件 {event['event_id']}] 结算前必须先冻结担保 {gid}")
                continue
            if any(e["event_type"] == "COST_GUARANTEE_SETTLED" for e in guarantees[gid]):
                errors.append(f"[事件 {event['event_id']}] 担保 {gid} 已结算，不得重复结算")
            if not isinstance(p["settled_amount"], (int, float)) or p["settled_amount"] < 0:
                errors.append(f"[事件 {event['event_id']}] 结算金额必须是非负数字")
            elif p["settled_amount"] > held["payload"]["amount"] + 1e-9:
                errors.append(
                    f"[事件 {event['event_id']}] 担保 {gid} 结算 {p['settled_amount']} "
                    f"超过冻结额度 {held['payload']['amount']}"
                )
            covered = set(held["payload"]["covers_damage_ids"])
            for woid in p["against_work_order_ids"]:
                wo = work_orders.get(woid)
                if not wo:
                    errors.append(f"[事件 {event['event_id']}] 结算引用了不存在的工单 {woid}")
                    continue
                if wo["payload"]["damage_id"] not in covered:
                    errors.append(
                        f"[事件 {event['event_id']}] 工单 {woid} 的损伤不在担保 {gid} 覆盖范围内"
                    )
                resolution = _resolved_at(wo["payload"]["damage_id"], when)
                if not resolution or resolution["payload"]["resolution"] != "成立":
                    errors.append(f"[事件 {event['event_id']}] 工单 {woid} 对应损伤未裁定认账，不能扣款")
                done = any(woid in rep["payload"]["work_order_ids"]
                           and parse_time(rep["occurred_at"]) <= when for rep in repairs)
                if not done:
                    errors.append(f"[事件 {event['event_id']}] 工单 {woid} 尚未完工，不能结算担保")
            guarantees.setdefault(gid, []).append(event)

    # ---------- 七、三类验收签署、隔离与分区重开 ----------
    signoffs: dict[str, dict[str, dict]] = {}  # zone -> kind -> event
    quarantine_at: dict[str, datetime] = {}
    reopened: dict[str, dict] = {}
    compared_zones: set[str] = set()
    for event in events:
        if event["event_type"] == "INSPECTION_COMPARED":
            for finding in event["payload"].get("findings", []):
                compared_zones.add(finding.get("zone_id"))

    for event in events:
        p, when = event.get("payload", {}), parse_time(event["occurred_at"])
        etype = event["event_type"]

        if etype in ("ZONE_SAFETY_SIGNED", "ZONE_FUNCTION_SIGNED", "ZONE_ASSET_SIGNED"):
            kind = etype[len("ZONE_"):-len("_SIGNED")].lower()
            zid = p["zone_id"]
            if zid in signoffs and kind in signoffs[zid]:
                errors.append(f"[事件 {event['event_id']}] 区域 {zid} 的{kind}验收已签署，不得重复签署")
            if zid not in compared_zones:
                errors.append(
                    f"[事件 {event['event_id']}] 区域 {zid} 尚未完成与赛前基线的逐项比对，不能验收签署"
                )
            if kind in ("safety", "function"):
                need = {k for k, v in CATEGORY_SIGNOFF.items() if v == kind}
                for did, history in damages.items():
                    report = history[0]
                    if report["payload"]["zone_id"] != zid:
                        continue
                    if report["payload"]["element_category"] in need and not closed_at(did, when):
                        errors.append(
                            f"[事件 {event['event_id']}] 区域 {zid} 损伤 {did} 未闭环，"
                            f"{kind} 验收不得签署"
                        )
            if kind == "asset":
                doc = p.get("transfer_document_id")
                if doc:
                    transfer = next(
                        (e for e in events
                         if e["event_type"] == "ASSET_TRANSFERRED"
                         and e["payload"].get("transfer_document_id") == doc),
                        None,
                    )
                    if not transfer or parse_time(transfer["occurred_at"]) > when:
                        errors.append(
                            f"[事件 {event['event_id']}] 区域 {zid} 资产签署引用的交接单 "
                            f"{doc} 在签署时尚不存在，不得凭纸面交接入账"
                        )
                for blocker in asset_ready_for_zone(zid, when):
                    errors.append(f"[事件 {event['event_id']}] 区域 {zid} 资产验收受阻：{blocker}")
            signoffs.setdefault(zid, {})[kind] = event

        elif etype == "ZONE_QUARANTINED":
            zid = p["zone_id"]
            open_damage = [
                did for did, history in damages.items()
                if history[0]["payload"]["zone_id"] == zid and not closed_at(did, when)
            ]
            if not open_damage:
                errors.append(
                    f"[事件 {event['event_id']}] 区域 {zid} 没有未闭环的损伤，不得随意隔离"
                )
            if zid in reopened and parse_time(reopened[zid]["occurred_at"]) <= when:
                errors.append(f"[事件 {event['event_id']}] 区域 {zid} 已重开，隔离须另案处理")
            quarantine_at.setdefault(zid, when)

        elif etype == "ZONE_REOPENED":
            zid = p["zone_id"]
            if zid in reopened:
                errors.append(f"[事件 {event['event_id']}] 区域 {zid} 重复重开")
            kinds = signoffs.get(zid, {})
            missing = [k for k in ("safety", "function", "asset") if k not in kinds]
            if missing:
                names = {"safety": "安全", "function": "功能", "asset": "资产"}
                errors.append(
                    f"[事件 {event['event_id']}] 区域 {zid} 缺少{[names[k] for k in missing]}"
                    f"验收签署；撤场完成不等于开放，三类签署齐备后方可重开"
                )
            declared = set(p.get("signoff_ids", []))
            actual = {e["payload"]["signoff_id"] for e in kinds.values()}
            if declared != actual:
                errors.append(
                    f"[事件 {event['event_id']}] 区域 {zid} 重开申报签署 {sorted(declared)} "
                    f"与实际签署 {sorted(actual)} 不一致"
                )
            if any(parse_time(e["occurred_at"]) > when for e in kinds.values()):
                errors.append(f"[事件 {event['event_id']}] 区域 {zid} 引用了尚未完成的签署")
            if zid in quarantine_at:
                unclosed = [
                    did for did, history in damages.items()
                    if history[0]["payload"]["zone_id"] == zid and not closed_at(did, when)
                ]
                if unclosed:
                    errors.append(
                        f"[事件 {event['event_id']}] 隔离区域 {zid} 仍有未闭环损伤 {unclosed}，"
                        f"修复验收完成前不得重开"
                    )
            reopened[zid] = event

    # ---------- 八、课程改签：只能读取已签署的分区结果 ----------
    for event in events:
        if event["event_type"] != "COURSE_RESCHEDULED":
            continue
        p, when = event["payload"], parse_time(event["occurred_at"])
        if p["from_zone_id"] == p["to_zone_id"]:
            errors.append(f"[事件 {event['event_id']}] 改签原区域与目标区域相同")
        targets = []
        for did in p["based_on_decision_ids"]:
            decision = index.by_id.get(did)
            if not decision or decision["event_type"] != "ZONE_REOPENED":
                errors.append(
                    f"[事件 {event['event_id']}] 改签依据 {did} 不是已签署的分区重开决定；"
                    f"对外开放日历只能读取重开结果"
                )
                continue
            targets.append(decision)
        target = next((d for d in targets if d["payload"]["zone_id"] == p["to_zone_id"]), None)
        if targets and not target:
            errors.append(
                f"[事件 {event['event_id']}] 改签目标区域 {p['to_zone_id']} "
                f"不在所引用的重开决定范围内"
            )
        if target:
            if parse_time(target["occurred_at"]) > parse_time(p["new_start"]):
                errors.append(
                    f"[事件 {event['event_id']}] 新课程时间早于目标区域 {p['to_zone_id']} 重开时间"
                )
            if parse_time(target["occurred_at"]) > when:
                errors.append(f"[事件 {event['event_id']}] 改签通知先于重开签署，不能对外发布")
        if parse_time(p["notified_at"]) > when:
            errors.append(f"[事件 {event['event_id']}] 通知时间晚于改签记录时间")
        if parse_time(p["notified_at"]) >= parse_time(p["original_start"]):
            errors.append(f"[事件 {event['event_id']}] 必须在原课程开始前完成改签通知")

    return errors


def _is_well_formed(index: StreamIndex) -> bool:
    """结构完整到足以运行语义规则（必需载荷齐备、时间可解析）。"""
    for event in index.events:
        if not isinstance(event.get("payload"), dict):
            return False
        try:
            parse_time(event["occurred_at"])
        except (ValueError, TypeError):
            return False
        reg = EVENT_REGISTRY.get(event["event_type"])
        if reg and any(k not in event["payload"] for k in reg["required_payload"]):
            return False
    return True
