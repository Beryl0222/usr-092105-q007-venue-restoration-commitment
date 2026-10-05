import copy
import json
import unittest
from pathlib import Path

from src.domain import (
    build_model,
    course_plan,
    damage_trace,
    pending_donations,
    public_calendar,
    validate_event,
    validate_stream,
    venue_assets,
)

ROOT = Path(__file__).parents[1]


def load_scenario() -> list[dict]:
    return json.loads((ROOT / "data" / "scenario.json").read_text(encoding="utf-8"))["events"]


_seq = [0]


def ev(event_id: str, event_type: str, aggregate_type: str, aggregate_id: str, **kw) -> dict:
    """构造测试事件的最小帮助函数，默认字段可被关键字覆盖。"""
    _seq[0] += 1
    base = {
        "event_id": event_id,
        "event_type": event_type,
        "aggregate_type": aggregate_type,
        "aggregate_id": aggregate_id,
        "occurred_at": f"2026-09-2{_seq[0] % 9}T10:00:00+08:00",
        "version": kw.pop("version", 1),
        "summary": kw.pop("summary", "测试事件"),
    }
    base.update(kw)
    return base


def baseline(zid="Z1", version=1, eid=None) -> dict:
    return ev(
        eid or f"base-{zid}-{version}",
        "BASELINE_FROZEN",
        "venue_baseline",
        f"base-{zid}",
        version=version,
        zone_id=zid,
        summary=f"{zid} 基线冻结",
        details={"baseline_snapshot": {"turf_load_limit": "0.35MPa"}},
    )


class EnvelopeTest(unittest.TestCase):
    def test_sample_matches_envelope(self) -> None:
        sample = json.loads((ROOT / "data" / "sample.json").read_text(encoding="utf-8"))
        self.assertEqual(validate_event(sample), [])

    def test_legacy_required_fields(self) -> None:
        self.assertTrue(validate_event({}))
        bad = ev("x-0001", "UNKNOWN", "restoration_case", "c1")
        self.assertTrue(any("未知事件类型" in e for e in validate_event(bad)))

    def test_event_aggregate_pairing(self) -> None:
        wrong = ev("evt-pairing-1", "ZONE_REOPENED", "venue_baseline", "b1", zone_id="Z1")
        self.assertTrue(any("必须归属于聚合 reopening_decision" in e for e in validate_event(wrong)))
        right = ev("evt-pairing-2", "ZONE_REOPENED", "reopening_decision", "d1", zone_id="Z1")
        self.assertFalse(validate_event(right))


class ScenarioTest(unittest.TestCase):
    def setUp(self) -> None:
        self.events = load_scenario()

    def test_full_scenario_is_valid(self) -> None:
        self.assertEqual(validate_stream(self.events), [])

    def test_version_must_be_continuous_per_aggregate(self) -> None:
        events = copy.deepcopy(self.events)
        events[0]["version"] = 2  # baseline-z1 首事件版本被改成 2
        errs = validate_stream(events)
        self.assertTrue(any("版本应为 1" in e for e in errs))

    def test_duplicate_event_id_rejected(self) -> None:
        events = copy.deepcopy(self.events)
        events[1]["event_id"] = events[0]["event_id"]
        self.assertTrue(any("事件标识重复" in e for e in validate_stream(events)))


class LifecycleTest(unittest.TestCase):
    def _registered_installation(self, zid="Z1") -> list[dict]:
        return [
            baseline(zid),
            ev(
                f"inst-{zid}-reg", "INSTALLATION_REGISTERED", "temporary_installation", f"inst-{zid}",
                zone_id=zid, version=1, summary="登记临时结构",
                details={
                    "structure_name": "临时看台",
                    "responsible_party": "某运营商",
                    "permits": [{"permit_no": "P1", "permit_type": "临建"}],
                    "occupancy_window": {"start": "2026-09-21T10:00:00+08:00", "end": "2026-09-28T18:00:00+08:00"},
                },
            ),
        ]

    def _inspection(self, zid, agg, version, record_type="original"):
        return ev(
            f"insp-{zid}-{version}", "INSPECTION_RECORDED", "restoration_case", agg,
            zone_id=zid, version=version, summary="巡检",
            details={"evidence": [
                {"media_ref": f"m-{zid}-{version}", "record_type": record_type,
                 "taken_at": "2026-09-29T09:00:00+08:00"}
            ]},
        )

    def _three_signoffs_and_reopen(self, zid, agg="dec", versions=(1, 2, 3, 4)):
        s = []
        for i, (et, kind) in enumerate([
            ("SAFETY_SIGN_OFF", "safety"),
            ("FUNCTION_SIGN_OFF", "function"),
            ("ASSET_SIGN_OFF", "asset"),
        ], start=1):
            s.append(ev(f"so-{zid}-{kind}", et, "reopening_decision", agg,
                        zone_id=zid, version=versions[i - 1], summary=f"{kind}签署",
                        details={"sign_off_type": kind, "signed_by": "责任人"}))
        s.append(ev(f"ro-{zid}", "ZONE_REOPENED", "reopening_decision", agg,
                    zone_id=zid, version=versions[3], summary="重开",
                    refs=[f"so-{zid}-safety", f"so-{zid}-function", f"so-{zid}-asset"],
                    details={"reopen_from": "2026-10-04T08:00:00+08:00"}))
        return s

    def _closed_zone(self, zid="Z1", with_evidence=True) -> list[dict]:
        events = self._registered_installation(zid)
        events.append(ev(f"inst-{zid}-acc", "INSTALLATION_ACCEPTED", "temporary_installation",
                         f"inst-{zid}", zone_id=zid, version=2, summary="进场核验"))
        events.append(ev(f"inst-{zid}-close", "OCCUPANCY_WINDOW_CLOSED", "temporary_installation",
                         f"inst-{zid}", zone_id=zid, version=3, summary="撤场完成"))
        if with_evidence:
            events.append(self._inspection(zid, f"case-{zid}", 1))
        return events

    def test_installation_requires_frozen_baseline(self) -> None:
        events = self._registered_installation("Z9")
        del events[0]  # 没有冻结基线
        errs = validate_stream(events)
        self.assertTrue(any("未完成进场基线冻结" in e for e in errs))

    def test_installation_requires_permits_and_window(self) -> None:
        bad = self._registered_installation("Z1")
        bad[1]["details"].pop("permits")
        bad[1]["details"].pop("occupancy_window")
        errs = validate_stream(bad)
        self.assertTrue(any("permits" in e for e in errs))
        self.assertTrue(any("occupancy_window" in e for e in errs))

    def test_clearance_is_not_reopening(self) -> None:
        events = self._closed_zone()
        # 撤场后直接重开，没有任何验收签署
        events.append(ev("ro-Z1", "ZONE_REOPENED", "reopening_decision", "dec",
                         zone_id="Z1", version=1, summary="试图直接重开",
                         refs=[], details={"reopen_from": "2026-10-04T08:00:00+08:00"}))
        errs = validate_stream(events)
        self.assertTrue(any("撤场完成不等于可以开放" in e for e in errs))

    def test_reopen_requires_three_independent_signoffs(self) -> None:
        for missing in ("safety", "function", "asset"):
            with self.subTest(missing=missing):
                events = self._closed_zone()
                steps = self._three_signoffs_and_reopen("Z1")
                et = {"safety": "SAFETY_SIGN_OFF", "function": "FUNCTION_SIGN_OFF",
                      "asset": "ASSET_SIGN_OFF"}[missing]
                steps = [s for s in steps if s["event_type"] != et]
                # 重开 refs 中去掉对应引用
                steps[-1]["refs"] = [r for r in steps[-1]["refs"] if f"-{missing}" not in r]
                events.extend(steps)
                errs = validate_stream(events)
                self.assertTrue(
                    any(f"重开缺少验收签署：['{missing}']" in e for e in errs),
                    f"缺少 {missing} 签署时必须拒绝重开：{errs}",
                )

    def test_reopen_must_reference_signoff_events(self) -> None:
        events = self._closed_zone()
        steps = self._three_signoffs_and_reopen("Z1")
        steps[-1]["refs"] = []  # 三类签署存在，但重开未引用
        events.extend(steps)
        errs = validate_stream(events)
        self.assertTrue(any("必须在 refs 中引用" in e for e in errs))

    def test_signoff_requires_original_evidence(self) -> None:
        events = self._closed_zone(with_evidence=False)
        events.append(self._inspection("Z1", "case-Z1", 1, record_type="supplementary"))
        events.extend(self._three_signoffs_and_reopen("Z1"))
        errs = validate_stream(events)
        # 仅有后补材料：巡检本身告警，三类签署也都不得通过
        self.assertTrue(any("不能作为原始巡检证据" in e for e in errs))
        self.assertTrue(any("缺少 original 原始巡检证据" in e for e in errs))
        self.assertTrue(any("重开缺少验收签署" in e for e in errs))

    def test_supplementary_after_original_is_allowed(self) -> None:
        events = self._closed_zone()
        events.append(self._inspection("Z1", "case-Z1", 2, record_type="supplementary"))
        events.extend(self._three_signoffs_and_reopen("Z1"))
        self.assertEqual(validate_stream(events), [])

    def test_quarantine_is_zone_scoped(self) -> None:
        """Z1 隔离不连累 Z2：Z2 仍可独立完成重开。"""
        scenario = load_scenario()
        model = build_model(scenario)
        z1 = model["zones"]["Z1"]
        z2 = model["zones"]["Z2"]
        z3 = model["zones"]["Z3"]
        # Z1 在争议期间被隔离过（事件流含 ZONE_QUARANTINED），最终重开
        self.assertTrue(any(e["event_type"] == "ZONE_QUARANTINED" and e["zone_id"] == "Z1"
                            for e in scenario))
        self.assertTrue(z1["reopened"] and z2["reopened"] and z3["reopened"])
        # Z4 有未交接装置，未重开，不影响其他三个分区
        self.assertFalse(model["zones"]["Z4"]["reopened"])

    def test_quarantine_requires_damage_and_blocks_reopen(self) -> None:
        events = self._closed_zone("Z5")
        events.append(ev("q-Z5", "ZONE_QUARANTINED", "restoration_case", "case-Z5",
                         zone_id="Z5", version=2, summary="无损伤却隔离"))
        self.assertTrue(any("无已登记损伤" in e for e in validate_stream(events)))

    def test_damage_must_reference_baseline(self) -> None:
        events = self._closed_zone()
        events.append(ev("dmg-1", "DAMAGE_REPORTED", "restoration_case", "case-Z1",
                         zone_id="Z1", version=2, summary="登记损伤",
                         details={"damage_id": "d1", "aspect": "turf", "severity": "major"}))
        errs = validate_stream(events)
        self.assertTrue(any("引用该分区的 BASELINE_FROZEN" in e for e in errs))

    def test_signoff_blocked_while_damage_unresolved(self) -> None:
        events = self._closed_zone()
        events.append(ev("dmg-1", "DAMAGE_REPORTED", "restoration_case", "case-Z1",
                         zone_id="Z1", version=2, summary="登记损伤", refs=["base-Z1-1"],
                         details={"damage_id": "d1", "aspect": "turf", "severity": "major"}))
        events.append(ev("so-1", "SAFETY_SIGN_OFF", "reopening_decision", "dec",
                         zone_id="Z1", version=1, summary="带伤签署",
                         details={"sign_off_type": "safety", "signed_by": "甲"}))
        errs = validate_stream(events)
        self.assertTrue(any("存在未修复损伤" in e for e in errs))


class AssetTest(unittest.TestCase):
    def test_donation_without_handover_is_not_venue_asset(self) -> None:
        events = [
            ev("don-1", "ASSET_DONATION_OFFERED", "restoration_case", "c1",
               zone_id="Z4", version=1, summary="捐赠要约",
               details={"asset_id": "a1", "asset_name": "互动装置", "donor_party": "某赞助商"}),
        ]
        model = build_model(events)
        self.assertEqual(venue_assets(model), [])
        self.assertEqual(len(pending_donations(model)), 1)

    def test_asset_signoff_blocked_until_handover_signed(self) -> None:
        events = [
            baseline("Z4"),
            ev("inst-reg", "INSTALLATION_REGISTERED", "temporary_installation", "i1",
               zone_id="Z4", version=1, summary="登记",
               details={"structure_name": "装置", "responsible_party": "运营商",
                        "permits": [{"permit_no": "P1", "permit_type": "临电"}],
                        "occupancy_window": {"start": "2026-09-21T10:00:00+08:00",
                                             "end": "2026-09-28T18:00:00+08:00"}}),
            ev("inst-close", "OCCUPANCY_WINDOW_CLOSED", "temporary_installation", "i1",
               zone_id="Z4", version=2, summary="撤场"),
            ev("insp-1", "INSPECTION_RECORDED", "restoration_case", "c1",
               zone_id="Z4", version=1, summary="巡检",
               details={"evidence": [{"media_ref": "m1", "record_type": "original",
                                      "taken_at": "2026-09-29T09:00:00+08:00"}]}),
            ev("don-1", "ASSET_DONATION_OFFERED", "restoration_case", "c1",
               zone_id="Z4", version=2, summary="捐赠要约未签署",
               details={"asset_id": "a1", "donor_party": "赞助商"}),
            ev("so-asset", "ASSET_SIGN_OFF", "reopening_decision", "d1",
               zone_id="Z4", version=1, summary="试图资产签署",
               details={"sign_off_type": "asset", "signed_by": "资产管理员"}),
        ]
        errs = validate_stream(events)
        self.assertTrue(any("未正式交接" in e and "资产验收不得签署" in e for e in errs))

    def test_handover_then_asset_counts(self) -> None:
        events = [
            ev("don-1", "ASSET_DONATION_OFFERED", "restoration_case", "c1",
               zone_id="Z3", version=1, summary="要约",
               details={"asset_id": "a1", "donor_party": "赞助商"}),
            ev("don-2", "ASSET_HANDOVER_SIGNED", "restoration_case", "c1",
               zone_id="Z3", version=2, summary="交接签署",
               details={"asset_id": "a1", "signed_by": "双方"}),
        ]
        model = build_model(events)
        self.assertEqual([a["asset_id"] for a in venue_assets(model)], ["a1"])


class GuaranteeTest(unittest.TestCase):
    def test_settle_without_hold_rejected(self) -> None:
        events = [
            ev("g1", "COST_GUARANTEE_SETTLED", "restoration_case", "c1",
               version=1, summary="未扣留先结算",
               details={"guarantee_id": "g1", "guarantee_status": "claimed"}),
        ]
        self.assertTrue(any("结算前必须先扣留担保" in e for e in validate_stream(events)))

    def test_scenario_guarantee_chain(self) -> None:
        model = build_model(load_scenario())
        g1 = model["guarantees"]["grt-z1-001"]
        self.assertEqual(g1["status"], "partial_claim")
        self.assertEqual(g1["settled_amount"], 76000)
        g2 = model["guarantees"]["grt-z2-001"]
        self.assertEqual(g2["status"], "claimed")


class CourseRebookTest(unittest.TestCase):
    def _reopened(self) -> list[dict]:
        events = [
            baseline("Z2"),
            ev("inst-reg", "INSTALLATION_REGISTERED", "temporary_installation", "i2",
               zone_id="Z2", version=1, summary="登记",
               details={"structure_name": "看台", "responsible_party": "运营商",
                        "permits": [{"permit_no": "P1", "permit_type": "临建"}],
                        "occupancy_window": {"start": "2026-09-21T10:00:00+08:00",
                                             "end": "2026-09-28T18:00:00+08:00"}}),
            ev("inst-close", "OCCUPANCY_WINDOW_CLOSED", "temporary_installation", "i2",
               zone_id="Z2", version=2, summary="撤场"),
            ev("insp-1", "INSPECTION_RECORDED", "restoration_case", "c2",
               zone_id="Z2", version=1, summary="巡检",
               details={"evidence": [{"media_ref": "m1", "record_type": "original",
                                      "taken_at": "2026-09-29T09:00:00+08:00"}]}),
        ]
        events.extend([
            ev("s1", "SAFETY_SIGN_OFF", "reopening_decision", "d2", zone_id="Z2",
               version=1, summary="安全", details={"sign_off_type": "safety", "signed_by": "甲"}),
            ev("s2", "FUNCTION_SIGN_OFF", "reopening_decision", "d2", zone_id="Z2",
               version=2, summary="功能", details={"sign_off_type": "function", "signed_by": "乙"}),
            ev("s3", "ASSET_SIGN_OFF", "reopening_decision", "d2", zone_id="Z2",
               version=3, summary="资产", details={"sign_off_type": "asset", "signed_by": "丙"}),
            ev("reopen-1", "ZONE_REOPENED", "reopening_decision", "d2", zone_id="Z2",
               version=4, summary="重开", refs=["s1", "s2", "s3"],
               details={"reopen_from": "2026-10-04T08:00:00+08:00"}),
        ])
        return events

    def test_rebook_must_reference_signed_reopen(self) -> None:
        events = self._reopened()
        events.append(ev("rb1", "COURSE_REBOOKED", "reopening_decision", "d2",
                         zone_id="Z2", version=5, summary="改签引用了不存在的决定",
                         details={"course_id": "C1", "rebook_from_decision": "reopen-x",
                                  "new_slot": {"zone_id": "Z2", "start": "2026-10-05T10:00:00+08:00"}}))
        errs = validate_stream(events)
        self.assertTrue(any("不是已签署的重开决定" in e for e in errs))

    def test_rebook_zone_and_time_constraints(self) -> None:
        events = self._reopened()
        events.append(ev("rb2", "COURSE_REBOOKED", "reopening_decision", "d2",
                         zone_id="Z2", version=5, summary="改签错分区且早于重开",
                         details={"course_id": "C1", "rebook_from_decision": "reopen-1",
                                  "new_slot": {"zone_id": "Z1", "start": "2026-10-01T10:00:00+08:00"}}))
        errs = validate_stream(events)
        self.assertTrue(any("改签目标分区必须与重开分区一致" in e for e in errs))
        self.assertTrue(any("改签时间不得早于分区重开时间" in e for e in errs))

    def test_valid_rebook(self) -> None:
        events = self._reopened()
        events.append(ev("rb3", "COURSE_REBOOKED", "reopening_decision", "d2",
                         zone_id="Z2", version=5, summary="正常改签",
                         details={"course_id": "C1", "course_name": "舞蹈课",
                                  "rebook_from_decision": "reopen-1",
                                  "original_slot": {"zone_id": "Z2", "start": "2026-10-06T19:00:00+08:00"},
                                  "new_slot": {"zone_id": "Z2", "start": "2026-10-08T18:00:00+08:00"}}))
        self.assertEqual(validate_stream(events), [])


class ProjectionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.model = build_model(load_scenario())

    def test_calendar_only_contains_signed_reopened_zones(self) -> None:
        calendar = public_calendar(self.model)
        by_zone = {c["zone_id"]: c for c in calendar}
        self.assertEqual(set(by_zone), {"Z1", "Z2", "Z3"})  # Z4 未重开，绝不出现在日历
        # 足球课两次改签，日历只展示最新一次（10月10日）
        self.assertEqual(by_zone["Z1"]["courses"][0]["start"], "2026-10-10T10:00:00+08:00")
        self.assertEqual(by_zone["Z2"]["reopen_from"], "2026-10-04T08:00:00+08:00")

    def test_damage_trace_links_baseline_party_repair_guarantee(self) -> None:
        trace = damage_trace(self.model, "dmg-z1-turf-01")
        self.assertIsNotNone(trace)
        self.assertEqual(trace["baseline_event_id"], "evt-base-z1-001")
        self.assertIn("0.35MPa", trace["baseline"]["turf_load_limit"])
        self.assertEqual(trace["responsible_party"], "峰汇赛事运营有限公司")
        self.assertTrue(trace["damage"]["disputed"])
        self.assertEqual(len(trace["repair_orders"]), 1)
        self.assertEqual(trace["repair_orders"][0]["status"], "closed")
        self.assertEqual(trace["repair_orders"][0]["actual_cost"], 94000)
        self.assertEqual(trace["cost_guarantee"]["status"], "partial_claim")
        # 后补材料与原始证据严格分列
        self.assertTrue(all(i["record_type"] == "original" for i in trace["original_evidence"]))
        self.assertTrue(all(i["record_type"] == "supplementary" for i in trace["supplementary_evidence"]))
        self.assertEqual(len(trace["supplementary_evidence"]), 1)

    def test_venue_assets_excludes_pending_donation(self) -> None:
        accepted = {a["asset_id"] for a in venue_assets(self.model)}
        pending = {a["asset_id"] for a in pending_donations(self.model)}
        self.assertEqual(accepted, {"asset-screen-77"})
        self.assertEqual(pending, {"asset-interactive-88"})

    def test_course_plan_only_after_signed_decision(self) -> None:
        plan = course_plan(self.model)
        ids = {c["course_id"] for c in plan}
        self.assertEqual(ids, {"C-101", "C-201"})
        football = next(c for c in plan if c["course_id"] == "C-101")
        self.assertEqual(football["new_slot"]["start"], "2026-10-10T10:00:00+08:00")

    def test_unknown_damage_returns_none(self) -> None:
        self.assertIsNone(damage_trace(self.model, "nope"))


if __name__ == "__main__":
    unittest.main()
