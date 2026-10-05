# 赛后场馆恢复承诺

本项目保存“赛后场馆恢复承诺”领域中跨机构交换记录的基础约定，覆盖**从进场冻结到分区重开**的完整链路：
区域基线、临时结构及许可、占用窗口、资产归属、巡检证据、损伤争议、修复工单、费用担保、分区隔离/重开、课程改签与各类签署。

核心立场：**撤场完成不等于开放**；安全、功能、资产须分别验收；有争议的区域可隔离但不连带邻区；
后补照片不得冒充原始证据；捐赠装置未正式接收不算场馆资产；对外开放日历只读取已签署的分区结果。

## 领域资料

- `contracts/domain.schema.json`：事件信封、事件/聚合枚举与各事件类型的载荷条件（JSON Schema 2020-12）。
- `docs/domain.md`：领域说明——生命周期、三类签署矩阵、证据/资产/担保规则、读模型约束。
- `data/sample.json`：一条可用于本地联调的中文样例（信封最小样例）。
- `data/sample_stream.json`：完整故事流（草坪承载、地板孔位、消防通道、无障碍差异、赞助装置捐赠、争议隔离、邻区重开、保证金结算、课程改签）。
- `src/registry.py`：事件目录（事件→聚合、必填载荷），公共标识语义的唯一事实来源。
- `src/index.py`：事件流加载与索引。
- `src/validator.py`：信封校验（`validate_event`）与事件流语义校验（`validate_stream`）。
- `src/projections.py`：只读投影——公众开放日历、课程改签、损伤展开、资产台账、分区看板。
- `tests/`：信封回归测试与全部语义不变量的正反用例。

## 约定

事件由 `event_id` 唯一标识，`aggregate_id` 指向业务对象，同一聚合内 `version` 从 1 开始连续递增，
`occurred_at` 保留带时区的真实发生时间。来源系统重试时必须沿用原事件标识。

## 本地检查

```bash
python3 -m unittest discover -s tests
```

快速试用校验器与投影：

```python
from src.index import load_stream
from src.validator import validate_stream
from src.projections import public_opening_calendar, damage_trace, venue_asset_register

events = load_stream("data/sample_stream.json")
assert validate_stream(events) == []
print(public_opening_calendar(events))                 # 只有已签署重开的分区
print(damage_trace(events, "DMG-2026-001"))            # 基线→使用方→修复→担保
print(venue_asset_register(events))                    # 已接收资产 / 待交接捐赠
```
