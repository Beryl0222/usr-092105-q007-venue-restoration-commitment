"""赛后恢复领域的事件目录。

登记每个事件归属的聚合类型与载荷必填项；validator 与 projections 共用，
保证“公共标识语义”只有一处定义。既有四个聚合与五个历史事件保持原名原义。
"""

from __future__ import annotations

from typing import Any

# 聚合类型（前四个为既有标识，course_session 随课程改签场景新增）
AGGREGATE_TYPES = (
    "venue_baseline",
    "temporary_installation",
    "restoration_case",
    "reopening_decision",
    "course_session",
)

# event_type -> 允许的 aggregate_type、中文说明、payload 必填字段
EVENT_REGISTRY: dict[str, dict[str, Any]] = {
    # ---- venue_baseline：进场冻结、占用窗口、巡检与证据 ----
    "BASELINE_FROZEN": {
        "aggregate": "venue_baseline",
        "summary": "赛前区域基线冻结",
        "required_payload": ["venue_id", "recorded_by"],
    },
    "OCCUPANCY_WINDOW_DECLARED": {
        "aggregate": "venue_baseline",
        "summary": "申报占用窗口与使用方",
        "required_payload": ["window_id", "zone_ids", "load_in_at", "load_out_at", "occupant"],
    },
    "INSPECTION_OPENED": {
        "aggregate": "venue_baseline",
        "summary": "赛后联合巡检立案",
        "required_payload": ["inspection_id", "baseline_id", "zone_ids"],
    },
    "INSPECTION_COMPARED": {
        "aggregate": "venue_baseline",
        "summary": "巡检逐项比对赛前基线",
        "required_payload": ["inspection_id", "findings"],
    },
    "INSPECTION_EVIDENCE_SUBMITTED": {
        "aggregate": "venue_baseline",
        "summary": "提交巡检证据（原始/补充）",
        "required_payload": ["evidence_id", "zone_id", "evidence_kind", "captured_at"],
    },
    # ---- temporary_installation：许可、资产归属、撤场 ----
    "INSTALLATION_ACCEPTED": {
        "aggregate": "temporary_installation",
        "summary": "临时结构凭许可进场",
        "required_payload": ["installation_id", "zone_ids", "permit_id", "approved_plan"],
    },
    "ASSET_DISPOSITION_DECLARED": {
        "aggregate": "temporary_installation",
        "summary": "声明资产归属（运营方所有/拟捐赠）",
        "required_payload": ["installation_id", "asset_id", "disposition"],
    },
    "ASSET_TRANSFERRED": {
        "aggregate": "temporary_installation",
        "summary": "捐赠装置经正式签署接收为场馆资产",
        "required_payload": ["installation_id", "asset_id", "transfer_document_id", "accepted_by"],
    },
    "INSTALLATION_REMOVED": {
        "aggregate": "temporary_installation",
        "summary": "临时设施撤场完成（不等于分区开放）",
        "required_payload": ["installation_id", "zone_ids"],
    },
    # ---- restoration_case：损伤争议、工单修复、费用担保 ----
    "DAMAGE_REPORTED": {
        "aggregate": "restoration_case",
        "summary": "登记与基线不符的损伤",
        "required_payload": ["damage_id", "zone_id", "element_category", "element", "reported_by", "evidence_ids", "baseline_id"],
    },
    "DAMAGE_DISPUTED": {
        "aggregate": "restoration_case",
        "summary": "使用方对损伤提出争议",
        "required_payload": ["damage_id", "disputed_by", "reason"],
    },
    "DAMAGE_RESOLVED": {
        "aggregate": "restoration_case",
        "summary": "损伤争议裁定（认账/不成立）",
        "required_payload": ["damage_id", "resolution", "resolved_by"],
    },
    "REPAIR_WORK_ORDER_ISSUED": {
        "aggregate": "restoration_case",
        "summary": "开具修复工单",
        "required_payload": ["work_order_id", "damage_id", "zone_id", "estimated_cost"],
    },
    "ZONE_REPAIRED": {
        "aggregate": "restoration_case",
        "summary": "分区修复完工待验",
        "required_payload": ["zone_id", "work_order_ids", "evidence_ids"],
    },
    "COST_GUARANTEE_HELD": {
        "aggregate": "restoration_case",
        "summary": "冻结费用担保",
        "required_payload": ["guarantee_id", "guarantor", "amount", "covers_damage_ids"],
    },
    "COST_GUARANTEE_SETTLED": {
        "aggregate": "restoration_case",
        "summary": "按完工工单结算担保、释放余款",
        "required_payload": ["guarantee_id", "against_work_order_ids", "settled_amount"],
    },
    # ---- reopening_decision：三类签署、隔离、重开 ----
    "ZONE_SAFETY_SIGNED": {
        "aggregate": "reopening_decision",
        "summary": "安全验收签署（结构/消防/承载/疏散）",
        "required_payload": ["zone_id", "inspection_id", "signoff_id", "signer"],
    },
    "ZONE_FUNCTION_SIGNED": {
        "aggregate": "reopening_decision",
        "summary": "功能验收签署（场地用途/无障碍/设备）",
        "required_payload": ["zone_id", "signoff_id", "signer"],
    },
    "ZONE_ASSET_SIGNED": {
        "aggregate": "reopening_decision",
        "summary": "资产验收签署（归属清晰、交接齐备）",
        "required_payload": ["zone_id", "signoff_id", "signer"],
    },
    "ZONE_QUARANTINED": {
        "aggregate": "reopening_decision",
        "summary": "因未决损伤或争议隔离分区",
        "required_payload": ["zone_id", "reason"],
    },
    "ZONE_REOPENED": {
        "aggregate": "reopening_decision",
        "summary": "分区签署齐备后对公众重开",
        "required_payload": ["zone_id", "decision_id", "signoff_ids"],
    },
    # ---- course_session：对外开放与课程改签 ----
    "COURSE_RESCHEDULED": {
        "aggregate": "course_session",
        "summary": "已售社区课程按分区结果改签",
        "required_payload": [
            "course_id", "session_id", "from_zone_id", "to_zone_id",
            "original_start", "new_start", "based_on_decision_ids", "notified_at",
        ],
    },
}

DISPUTED_SIGN_KINDS = ("safety", "function", "asset")
