import json
import unittest
from pathlib import Path

from src.validator import validate_event

ROOT = Path(__file__).parents[1]


class ContractTest(unittest.TestCase):
    def test_sample_matches_envelope(self) -> None:
        sample = json.loads((ROOT / "data" / "sample.json").read_text(encoding="utf-8"))
        self.assertEqual(validate_event(sample), [])

    def test_missing_fields_reported_in_chinese(self) -> None:
        errors = validate_event({"event_id": "evt-abcd-0001"})
        self.assertTrue(any("缺少字段" in e for e in errors))

    def test_version_must_be_positive_integer(self) -> None:
        errors = validate_event({"version": 0})
        self.assertIn("version 必须是正整数", errors)

    def test_event_type_belongs_to_declared_aggregate(self) -> None:
        errors = validate_event({
            "event_id": "evt-xxxx-0001",
            "event_type": "ZONE_REOPENED",
            "aggregate_type": "venue_baseline",
            "aggregate_id": "x",
            "occurred_at": "2026-10-01T00:00:00+08:00",
            "version": 1,
            "summary": "错误归属的事件",
            "payload": {},
        })
        self.assertTrue(any("必须归属聚合" in e for e in errors))

    def test_occurred_at_requires_timezone(self) -> None:
        errors = validate_event({"occurred_at": "2026-10-01T00:00:00"})
        self.assertTrue(any("带时区" in e for e in errors))


if __name__ == "__main__":
    unittest.main()
