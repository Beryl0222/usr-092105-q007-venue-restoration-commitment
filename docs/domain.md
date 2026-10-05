# 赛后场馆恢复承诺 · 领域约定

本文档定义“从进场冻结到分区重开”的赛后恢复领域事件、业务不变量与读模型约束。
公共契约见 `contracts/domain.schema.json`，事实样例见 `data/sample_stream.json`，
规则实现见 `src/validator.py`，读模型见 `src/projections.py`。

## 1. 核心立场

1. **撤场完成 ≠ 开放。** `INSTALLATION_REMOVED` 只证明运营方自报设施撤走；
   一个分区只有在安全、功能、资产三类验收**分别签署**齐备后，经 `ZONE_REOPENED` 才能对公众开放。
2. **安全、功能、资产分别验收。** 三条签署轨道互不替代：
   草坪承载、地板孔位、消防通道由**安全**签署把关；无障碍、面层、设备由**功能**签署把关；
   装置归属与交接由**资产**签署把关。
3. **争议区隔离，不连带邻区。** 有未闭环损伤的分区可 `ZONE_QUARANTINED`；
   已恢复的邻区照常重开，对外开放按**分区**逐条发布。
4. **后补照片不能冒充原始证据。** `original` 证据必须拍摄于基线冻结之前；
   冻结之后拍摄的只能是 `supplementary`。损伤登记必须同时引用原始证据，不能只凭后补照片立案。
5. **捐赠装置未正式接收，不算场馆资产。** `ASSET_DISPOSITION_DECLARED("拟捐赠")` 只是意向，
   只有 `ASSET_TRANSFERRED`（凭交接单、双方签署）之后才进入场馆资产台账，资产签署才能放行。
6. **对外开放日历只读取已签署的分区结果。** 课程改签（`COURSE_RESCHEDULED`）
   只能引用 `ZONE_REOPENED` 事件，且新课程时间不得早于目标区域重开时间。

## 2. 聚合

| aggregate_type | 含义 | 关键标识 |
| --- | --- | --- |
| `venue_baseline` | 场馆赛前基线、占用窗口、联合巡检与证据 | 一个场馆恢复承诺一份基线（样例：`venue-restoration-commitment-001`） |
| `temporary_installation` | 临时结构、许可、资产归属与撤场/捐赠交接 | 每个进场装置一个聚合 |
| `restoration_case` | 损伤、争议、裁定、工单、修复与费用担保 | 一次赛事一个恢复案卷 |
| `reopening_decision` | 三类签署、隔离与分区重开决定 | 一轮重开一个决定聚合 |
| `course_session` | 已售社区课程的改签与通知同步 | 每门课一个聚合（随课程场景新增） |

前四个聚合与既有五个事件类型（`BASELINE_FROZEN`、`INSTALLATION_ACCEPTED`、
`DAMAGE_REPORTED`、`ZONE_REPAIRED`、`ZONE_REOPENED`）保持原名原义，未做破坏性修改。

## 3. 事件目录与生命周期

```
BASELINE_FROZEN 冻结赛前基线（草坪/地板/消防/无障碍等分区记录）
  └─ INSPECTION_EVIDENCE_SUBMITTED(original) 冻结前原始证据归档
OCCUPANCY_WINDOW_DECLARED 占用窗口（分区、进出场时间、使用方）
INSTALLATION_ACCEPTED 临时结构凭许可进场（时间须落在占用窗口内）
ASSET_DISPOSITION_DECLARED 声明资产归属（撤场清运 / 拟捐赠）
INSTALLATION_REMOVED 撤场完成 ── 仅事实，不产生任何开放效力
INSPECTION_OPENED → INSPECTION_COMPARED 赛后联合巡检、逐项比对基线
  └─ INSPECTION_EVIDENCE_SUBMITTED(supplementary) 赛后后补证据
DAMAGE_REPORTED 差异登记（须附至少一份 original 证据并引用冻结基线）
  ├─ DAMAGE_DISPUTED → DAMAGE_RESOLVED（成立 / 不成立）
  ├─ COST_GUARANTEE_HELD 费用担保冻结（覆盖指定损伤）
  └─ REPAIR_WORK_ORDER_ISSUED → ZONE_REPAIRED 工单与完工
ZONE_QUARANTINED 未闭环分区隔离（不影响邻区）
ZONE_SAFETY_SIGNED / ZONE_FUNCTION_SIGNED / ZONE_ASSET_SIGNED 三类签署
ASSET_TRANSFERRED 捐赠装置凭交接单正式接收（资产签署的前置条件）
ZONE_REOPENED 三签齐备 → 分区对公众重开
COURSE_RESCHEDULED 已售课程按重开决定改签并通知
COST_GUARANTEE_SETTLED 按完工工单结算担保、释放余款
```

事件→聚合与必填载荷以 `src/registry.py` 为唯一事实来源，并同步到 JSON Schema 的 `allOf` 条件。

## 4. 三类验收签署矩阵

| 损伤类别（element_category） | 卡住的签署 | 闭环条件 |
| --- | --- | --- |
| 草坪承载 / 地板孔位 / 消防通道 / 结构承载 | 安全 `ZONE_SAFETY_SIGNED` | 裁定成立且关联工单全部完工；裁定不成立即闭环 |
| 无障碍设施 / 面层外观 / 设备设施 | 功能 `ZONE_FUNCTION_SIGNED` | 同上 |
| 资产装置 | 资产 `ZONE_ASSET_SIGNED` | 归属清晰：运营方资产已撤场，或拟捐赠资产已 `ASSET_TRANSFERRED` |

此外，任何签署都要求该分区先出现在 `INSPECTION_COMPARED` 的比对范围内；
`ZONE_REOPENED` 申报的 `signoff_ids` 必须与三类实际签署逐一一致。

## 5. 证据规则

- `evidence_kind` 仅允许 `original` / `supplementary`；
  `captured_at` 必须不晚于证据提交时间。
- `original`：`captured_at <= BASELINE_FROZEN.occurred_at`。
  冻结后拍摄的照片若标成 original，判“后补照片冒充原始证据”。
- `supplementary`：`captured_at >` 冻结时间；冻结前素材不得降级为后补证据。
- 损伤登记的每份证据须属于同一分区、在登记前已提交，且至少引用一份 original。

## 6. 资产归属规则

- 进场装置必须先 `INSTALLATION_ACCEPTED`（凭 `permit_id` 与批准方案，且进场时点有占用窗口覆盖），
  之后才能声明归属、撤场或捐赠。
- `撤场清运`：区域资产签署前必须已有该装置的 `INSTALLATION_REMOVED`。
- `拟捐赠`：仅有意向时在台账中列为 `pending_donations`；
  须经 `ASSET_TRANSFERRED`（`transfer_document_id` + `accepted_by`）后才进入 `assets`。
  已撤场的装置不得再补办捐赠交接；同一资产不得重复交接。
- 资产签署若引用交接单号，该交接事件必须在签署时点之前已经存在。

## 7. 费用担保规则

- `COST_GUARANTEE_HELD` 只能覆盖已登记的损伤，金额非负。
- `COST_GUARANTEE_SETTLED`：
  结算额不得超过冻结额；每张扣款工单的损伤必须在担保覆盖范围内、已裁定“成立”、
  且工单已完工；同一担保不得重复结算。

## 8. 读模型（只读视图）

`src/projections.py` 不产生事实，只做投影：

- `public_opening_calendar`：**只**包含三签齐备且交接单真实存在的 `ZONE_REOPENED`；
  隔离区、签署不全区、纸面接收区一律不出现。
- `course_rebooking_view`：改签逐条回溯目标分区是否已在开放日历中。
- `damage_trace(damage_id)`：从一处损伤展开
  **赛前基线 → 使用方（占用窗口）→ 争议/裁定 → 修复工单 → 费用担保结算**。
- `venue_asset_register`：`assets` 与 `pending_donations` 严格分账。
- `zone_restoration_status`：分区看板，三轨布尔位 + 隔离/重开状态，直观展示隔离不连带。

## 9. 流序、版本与幂等

- `event_id` 全流唯一；来源系统重试必须沿用原标识。
- 同一 `aggregate_id` 内 `version` 从 1 连续递增，事件 `occurred_at` 不得倒序。
- 所有时间必须带时区（RFC3339，如 `2026-09-29T13:00:00+08:00`）。
- 裁决、重开、担保结算、资产交接均为一次性事实：重复签署/重复重开/重复结算都会被校验拒绝。
