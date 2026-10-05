"""事件信封校验的兼容入口。

实现已随领域扩展迁移至 src.domain，此处保留原导入路径，
既有接入方 `from src.validator import validate_event` 无需修改。
"""

from src.domain import validate_event

REQUIRED = (
    "event_id",
    "event_type",
    "aggregate_type",
    "aggregate_id",
    "occurred_at",
    "version",
    "summary",
)

__all__ = ["validate_event", "REQUIRED"]
