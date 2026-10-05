# 赛后场馆恢复承诺

本项目保存"赛后场馆恢复承诺"领域中跨机构交换记录的基础约定，覆盖从**进场冻结**到**分区重开**的完整恢复项目：区域基线、临时结构及许可、占用窗口、资产归属、巡检证据、损伤争议、修复工单、费用担保、课程迁移与各类签署。

核心原则：**撤走设施不等于开放**。撤场完成只是前提，安全、功能、资产须分别验收签署；有争议的区域可以隔离，已经恢复的空间不被连带关闭；后补照片不能冒充原始证据；捐赠装置未正式交接不得算作场馆资产。

## 领域资料

- `contracts/domain.schema.json`：事件信封、事件-聚合配对目录（`x-event_catalog`）与 `details` 载荷字段。
- `data/sample.json`：单条最小中文样例（既有，持续有效）。
- `data/scenario.json`：完整中文场景，四个分区并行推进，共 50+ 条事件。
- `src/domain.py`：事件流不变量校验与只读投影。
- `src/validator.py`：兼容入口，`from src.validator import validate_event` 不变。
- `tests/`：信封约定与全部业务不变量的正反例。

事件由 `event_id` 唯一标识，`aggregate_id` 指向业务对象，`version` 在同一聚合内从 1 开始严格递增，`occurred_at` 保留真实发生时间。来源系统重试时必须沿用原事件标识（重复标识会被拒绝）。

## 四个聚合（保持不变）

| 聚合 | 承载内容 |
| --- | --- |
| `venue_baseline` | 进场冻结的分区基线（草坪承载、地板孔位、消防通道、无障碍设施）及修订 |
| `temporary_installation` | 临时结构登记/进场核验、许可清单、占用窗口、撤场 |
| `restoration_case` | 巡检证据、损伤与争议、隔离、修复工单、费用担保、捐赠装置交接 |
| `reopening_decision` | 安全/功能/资产三类签署、分区重开决定、课程改签 |

## 生命周期

```
BASELINE_FROZEN（按分区冻结，可 BASELINE_AMENDED 修订但不覆盖原件）
  └─ INSTALLATION_REGISTERED（结构+permits+occupancy_window+使用方）
       └─ INSTALLATION_ACCEPTED
            └─ OCCUPANCY_WINDOW_CLOSED（撤场完成 ≠ 可开放）
                 └─ INSPECTION_RECORDED（original / supplementary 严格区分）
                      ├─ 无异常 ───────────────┐
                      └─ DAMAGE_REPORTED       │
                           ├─ DAMAGE_DISPUTED  │
                           ├─ ZONE_QUARANTINED（仅封闭本分区）
                           ├─ REPAIR_ORDER_OPENED
                           ├─ COST_GUARANTEE_HELD
                           ├─ ZONE_REPAIRED → REPAIR_ORDER_CLOSED
                           └─ COST_GUARANTEE_SETTLED
                                               ▼
                 SAFETY_SIGN_OFF / FUNCTION_SIGN_OFF / ASSET_SIGN_OFF（分别签署）
                      └─ ZONE_REOPENED（refs 必须引用三签署事件）
                           └─ COURSE_REBOOKED（只能引用已签署重开决定）
```

捐赠装置另走 `ASSET_DONATION_OFFERED → ASSET_HANDOVER_SIGNED`；交接书签署前既不计入场馆资产，也会卡住本分区的资产验收。

## 强制不变量（`validate_stream`）

1. **基线先行**：未冻结基线不得登记临时结构；无冻结基线不得登记损伤，且损伤事件必须在 `refs` 引用该分区的 `BASELINE_FROZEN`。
2. **撤场≠开放**：未报 `OCCUPANCY_WINDOW_CLOSED` 不能验收；重开必须同时具备安全、功能、资产三类签署，且 `refs` 逐一引用三签署事件。
3. **分区最小粒度**：隔离必须针对已登记损伤，只封锁该分区；其他分区验收、重开、上日历不受影响。未修复损伤未清前不得签署、不得重开。
4. **证据不可冒充**：证据分 `original`（当场采集）与 `supplementary`（后补材料）。只有后补材料时，巡检告警且三类签署全部不得进行；后补材料可在已有原始证据后作为补充。
5. **资产归属**：捐赠装置在 `ASSET_HANDOVER_SIGNED` 前不算场馆资产；分区内存在未交接装置时资产验收不得签署。
6. **费用闭环**：先 `COST_GUARANTEE_HELD` 后 `COST_GUARANTEE_SETTLED`，结算状态为 released / claimed / partial_claim；担保必须挂在已登记损伤上。
7. **工单顺序**：修复完成须关联本分区开启中的工单，工单只能在 `ZONE_REPAIRED` 后关闭。
8. **课程改签**：`rebook_from_decision` 必须指向一条已签署的 `ZONE_REOPENED`；目标分区须与重开分区一致，时间不得早于 `reopen_from`。同一课程可多次改签，对外取最新。

## 只读投影

```python
import json
from src.domain import (
    validate_stream, build_model,
    public_calendar, damage_trace, venue_assets, pending_donations, course_plan,
)

events = json.load(open("data/scenario.json", encoding="utf-8"))["events"]
assert validate_stream(events) == []

model = build_model(events)
public_calendar(model)   # 对外日历：只有已签署重开的分区 + 已同步的最新改签
damage_trace(model, "dmg-z1-turf-01")
# 一处损伤展开：赛前基线 → 使用方 → 原始/后补证据 → 修复工单 → 费用担保
venue_assets(model)      # 场馆资产：只含 handover_signed 的装置
pending_donations(model) # 已要约但未交接、不得入账的装置
course_plan(model)       # 课程改签计划（同一课程多次改签取最新）
```

## 场景说明（data/scenario.json）

城东区体育中心 2026 城市田径公开赛撤场后，下周社区课程名额已售出：

- **Z1 主场草坪区**：220㎡ 草坪承载低于基线且消防通道被占用；运营商争议 → 分区隔离（不连累其他区）→ 后补照片仅登记为 supplementary → 先行修复 → 保函部分赔付（76000/120000）→ 三签署 → 10月8日重开；足球课两次改签，对外日历取 10月10日。
- **Z2 综合训练馆**：47 个地板锚固孔未封孔 → 工单 + 保函 → 修复结算 → 重开；舞蹈课改签 10月8日。
- **Z3 南侧通道**：无损伤，信息大屏完成捐赠交接 → 最早重开（10月1日）。
- **Z4 器材存放区**：赞助商互动装置仅口头表示留馆、未签交接书 → 不计入资产、不上日历、不影响其他分区。

## 本地检查

运行 `python3 -m unittest discover -s tests`（30 项，含信封、全场景与各不变量反例）。
