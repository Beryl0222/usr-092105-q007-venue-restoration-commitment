"""语义不变量测试：以完整样例流为基准做定点变异，每条规则至少一个反例。"""

import copy
import json
import unittest
from pathlib import Path

from src.index import load_stream
from src.projections import (
    course_rebooking_view,
    damage_trace,
    public_opening_calendar,
    venue_asset_register,
    zone_restoration_status,
)
from src.validator import validate_stream

ROOT = Path(__file__).parents[1]


def stream() -> list[dict]:
    return copy.deepcopy(load_stream(ROOT / "data" / "sample_stream.json"))


def find(events: list[dict], event_id: str) -> dict:
    return next(e for e in events if e["event_id"] == event_id)


def without(events: list[dict], *event_ids: str) -> list[dict]:
    return [e for e in events if e["event_id"] not in event_ids]


class SampleStreamTest(unittest.TestCase):
    def test_full_stream_is_valid(self) -> None:
        self.assertEqual(validate_stream(stream()), [])


class RemovalIsNotReopeningTest(unittest.TestCase):
    def test_reopen_without_safety_signoff_rejected(self) -> None:
        # 撤场事件（INSTALLATION_REMOVED）保留，只抽掉环廊安全签署
        events = without(stream(), "evt-1001-sc-c-01")
        errors = validate_stream(events)
        self.assertTrue(any("安全" in e and "撤场完成不等于开放" in e for e in errors), errors)

    def test_reopen_without_asset_signoff_rejected(self) -> None:
        events = without(stream(), "evt-1001-ac-c-01")
        errors = validate_stream(events)
        self.assertTrue(any("资产" in e and "撤场完成不等于开放" in e for e in errors), errors)

    def test_removal_event_alone_never_opens_zone(self) -> None:
        events = without(
            stream(),
            "evt-1001-sc-c-01", "evt-1001-fc-c-01", "evt-1001-ac-c-01", "evt-1001-ro-c-01",
        )
        calendar = {r["zone_id"] for r in public_opening_calendar(events)}
        self.assertNotIn("Z-C", calendar)  # 有撤场申报、无签署重开，不得对外开放


class ThreeTrackSignoffTest(unittest.TestCase):
    def test_open_damage_blocks_safety_signoff(self) -> None:
        # 抽掉草坪完工记录：损伤认账但未修复，安全签署不应成立
        events = without(stream(), "evt-1004-eva-002", "evt-1004-rep-001")
        errors = validate_stream(events)
        self.assertTrue(any("DMG-2026-001" in e and "safety" in e for e in errors), errors)

    def test_signoff_requires_baseline_comparison(self) -> None:
        events = without(stream(), "evt-0929-cmp-001")
        errors = validate_stream(events)
        self.assertTrue(any("逐项比对" in e for e in errors), errors)

    def test_signoff_ids_must_match_actual_signatures(self) -> None:
        events = stream()
        find(events, "evt-1001-ro-c-01")["payload"]["signoff_ids"] = ["SO-C-SAFE-01"]
        errors = validate_stream(events)
        self.assertTrue(any("与实际签署" in e for e in errors), errors)


class QuarantineTest(unittest.TestCase):
    def test_quarantined_zone_does_not_close_neighbors(self) -> None:
        # 10 月 2 日：A/B 仍在隔离，C/D/F 已重开
        events = [e for e in stream() if e["occurred_at"] <= "2026-10-02T23:59:59+08:00"]
        self.assertEqual(validate_stream(events), [])
        calendar = {r["zone_id"] for r in public_opening_calendar(events)}
        self.assertEqual(calendar, {"Z-C", "Z-D", "Z-F"})
        board = {r["zone_id"]: r for r in zone_restoration_status(events)}
        self.assertTrue(board["Z-A"]["quarantined"])
        self.assertFalse(board["Z-F"]["quarantined"])

    def test_quarantine_requires_unclosed_damage(self) -> None:
        event = copy.deepcopy(find(stream(), "evt-0929-qua-001"))
        event["event_id"] = "evt-x-qua-999"
        event["occurred_at"] = "2026-10-05T12:00:00+08:00"
        event["version"] = 27
        event["payload"] = {"zone_id": "Z-F", "reason": "没有损伤也想封"}
        events = stream() + [event]
        errors = validate_stream(events)
        self.assertTrue(any("不得随意隔离" in e for e in errors), errors)


class EvidenceTest(unittest.TestCase):
    def test_supplementary_photo_cannot_impersonate_original(self) -> None:
        events = stream()
        find(events, "evt-0929-eva-001")["payload"]["evidence_kind"] = "original"
        errors = validate_stream(events)
        self.assertTrue(any("不得冒充原始证据" in e for e in errors), errors)

    def test_original_photo_cannot_be_relabelled_supplementary(self) -> None:
        events = stream()
        find(events, "evt-0920-evoa-001")["payload"]["evidence_kind"] = "supplementary"
        errors = validate_stream(events)
        self.assertTrue(any("不得降级为后补证据" in e for e in errors), errors)

    def test_damage_needs_original_evidence(self) -> None:
        events = stream()
        find(events, "evt-0929-dmg-001")["payload"]["evidence_ids"] = ["EV-SUP-A01"]
        errors = validate_stream(events)
        self.assertTrue(any("缺少赛前原始证据" in e for e in errors), errors)


class AssetTest(unittest.TestCase):
    def test_donation_not_counted_before_formal_acceptance(self) -> None:
        events = without(stream(), "evt-1005-tr-100")
        errors = validate_stream(events)
        # 资产验收必须被“未正式接收”的捐赠装置卡住，重开也连带失败
        self.assertTrue(any("未正式签署接收" in e for e in errors), errors)
        register = venue_asset_register(events)
        self.assertEqual(register["assets"], [])
        self.assertEqual([a["asset_id"] for a in register["pending_donations"]], ["AST-BOOTH-100"])
        self.assertNotIn("Z-E", {r["zone_id"] for r in public_opening_calendar(events)})

    def test_donation_enters_register_after_transfer(self) -> None:
        register = venue_asset_register(stream())
        self.assertEqual([a["asset_id"] for a in register["assets"]], ["AST-BOOTH-100"])
        self.assertEqual(register["pending_donations"], [])

    def test_cannot_transfer_without_donation_intent(self) -> None:
        events = stream()
        # 把互动装置的归属意向改成“撤场清运”，却仍试图按捐赠办理接收
        find(events, "evt-0921-ad02-001")["payload"]["disposition"] = "撤场清运"
        errors = validate_stream(events)
        self.assertTrue(any("不能按捐赠接收" in e for e in errors), errors)


class GuaranteeTest(unittest.TestCase):
    def test_settlement_cannot_exceed_held_amount(self) -> None:
        events = stream()
        find(events, "evt-1005-stl-001")["payload"]["settled_amount"] = 999999
        errors = validate_stream(events)
        self.assertTrue(any("超过冻结额度" in e for e in errors), errors)

    def test_settlement_only_covers_guaranteed_damages(self) -> None:
        events = stream()
        find(events, "evt-0929-gua-001")["payload"]["covers_damage_ids"].remove("DMG-2026-001")
        errors = validate_stream(events)
        self.assertTrue(any("WO-2026-001" in e and "覆盖范围" in e for e in errors), errors)

    def test_settlement_requires_completed_work_orders(self) -> None:
        events = without(stream(), "evt-1004-rep-002")
        errors = validate_stream(events)
        self.assertTrue(any("WO-2026-002" in e and "尚未完工" in e for e in errors), errors)


class DisputeTest(unittest.TestCase):
    def test_dispute_after_resolution_rejected(self) -> None:
        late_dispute = {
            "event_id": "evt-1006-dis-999",
            "event_type": "DAMAGE_DISPUTED",
            "aggregate_type": "restoration_case",
            "aggregate_id": "restoration-case-2026-0929",
            "occurred_at": "2026-10-06T09:00:00+08:00",
            "version": 21,
            "summary": "裁定后再提争议",
            "payload": {"damage_id": "DMG-2026-001", "disputed_by": "运营方-祁磊", "reason": "翻供"},
        }
        errors = validate_stream(stream() + [late_dispute])
        self.assertTrue(any("已裁定，不能再提争议" in e for e in errors), errors)


class CourseRebookingTest(unittest.TestCase):
    def test_rebooking_must_reference_signed_reopening(self) -> None:
        events = stream()
        find(events, "evt-1002-crs-101")["payload"]["based_on_decision_ids"] = ["evt-0929-qua-001"]
        errors = validate_stream(events)
        self.assertTrue(any("不是已签署的分区重开决定" in e for e in errors), errors)

    def test_rebooking_view_reflects_open_calendar(self) -> None:
        rows = course_rebooking_view(stream())
        self.assertTrue(all(r["status"] == "已同步开放日历" for r in rows))

    def test_new_session_cannot_start_before_zone_reopens(self) -> None:
        events = stream()
        find(events, "evt-1002-crs-101")["payload"]["new_start"] = "2026-09-30T09:00:00+08:00"
        errors = validate_stream(events)
        self.assertTrue(any("早于目标区域" in e for e in errors), errors)


class StreamOrderTest(unittest.TestCase):
    def test_aggregate_versions_must_be_continuous(self) -> None:
        events = stream()
        find(events, "evt-0929-dmg-002")["version"] = 99
        errors = validate_stream(events)
        self.assertTrue(any("版本应为" in e for e in errors), errors)

    def test_event_ids_must_be_unique(self) -> None:
        duplicate = copy.deepcopy(find(stream(), "evt-0929-dmg-001"))
        duplicate["aggregate_id"] = "restoration-case-dup"
        errors = validate_stream(stream() + [duplicate])
        self.assertTrue(any("event_id 重复" in e for e in errors), errors)


class DamageTraceTest(unittest.TestCase):
    def test_trace_expands_baseline_occupant_repair_guarantee(self) -> None:
        trace = damage_trace(stream(), "DMG-2026-002")
        self.assertTrue(trace["found"])
        self.assertEqual(trace["baseline"]["baseline_id"], "venue-restoration-commitment-001")
        self.assertEqual(trace["occupant"]["occupant"], "星环赛事运营（上海）有限公司")
        self.assertEqual(trace["resolution"]["result"], "成立")
        self.assertEqual([w["work_order_id"] for w in trace["work_orders"]], ["WO-2026-002"])
        self.assertTrue(all(w["status"] == "已完工" for w in trace["work_orders"]))
        self.assertEqual([g["guarantee_id"] for g in trace["guarantees"]], ["GUA-2026-001"])
        self.assertEqual(trace["guarantees"][0]["settled_amount"], 106000)

    def test_trace_missing_damage_marked_not_found(self) -> None:
        self.assertFalse(damage_trace(stream(), "DMG-NOPE")["found"])


if __name__ == "__main__":
    unittest.main()
