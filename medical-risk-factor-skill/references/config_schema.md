# 五阶段配置与执行约束

每次命令只推进当前阶段；由代理记录用户实际反馈，不要求用户编辑 JSON。模板的 draft / false 不能自行改作已确认；用户确认只适用于呈现的当前阶段。

## 阶段1：research_question.json

基础命令 `python scripts/review_workflow.py --input data.xlsx --sheet Sheet1 --outdir output` 仅生成表头摘要和 `research_question.template.json`，不生成变量选择任务或质量报告。必须向用户提问研究目标并等待反馈。

- `schema_version`: `"1.7"`；`stage`: `"research_question"`；`status`: 用户明确反馈后才为 `"confirmed"`。
- `source_sha256`、`sheet`: 模板记录的原始来源指纹和实际工作表；CSV 为 `null`。
- `research_population`、`research_question`、`outcome_definition`: 用户确认的研究人群、问题、结局定义/时间窗，均非空；时间窗不适用时如实记录。
- `outcome`: 原始非身份字段，不依据文件名推定。需要派生结局时先单独确认定义与实现，不擅自替换原始结局。
- `study_design`、`analysis_unit`: 已知则记录，阶段4须补齐设计及独立观测确认。

## 阶段2：variable_selection_decisions.json

在基础命令后加 `--research-question output/research_question.json`，验证目标后生成全量 `variable_inventory.csv`、选择模板及 `interactive_review.html`。此时不读取行值或核查质量。

- `schema_version`: `"1.4"`；`stage`: `"variable_selection"`；`status`: `"confirmed"`。
- `source_sha256`、`sheet`、`research_question_fingerprint`: 绑定阶段1；目标变化后旧选择无效。
- `variables`: 唯一完整覆盖所有原始字段，每项 `variable`、`user_selection`、`user_note`；只能选 `纳入` 或 `不纳入`。完整分类、角色、AI 建议及理由保留在 CSV/HTML，不在决策中重复。
- `outcome`: 与阶段1完全一致，页面锁定；对应解释变量行选 `不纳入`。
- `positive_value`: 可暂空，阶段3必须明确处理后编码。
- `support_variables`: 默认为 `[]`；仅授权辅助字段，且须非空 `support_purpose`；不重复解释变量或结局，不入模。

旧版带额外展示元数据的 v1.4 文件可读取，但须添加当前目标指纹并重新确认，不能跳过阶段1。核查范围仅为解释变量、结局和授权辅助字段的并集。

## 阶段3：审核、处理、复核

再加 `--selection-decisions output/variable_selection_decisions.json`，生成质量页面、问题 CSV、`missingness_before.csv` 及 `processing_plan.template.json`。缺失表只统计实际空值，高缺失或全缺失不在其他异常中重复。`NA`、`null`、`999` 等文字/编码保留原值，参加常规类型及极端值核查，不自动识别“疑似缺失码”或静默设缺失。

页面与对话分为 `0% < 缺失比例 < 30%`、`30% ≤ 缺失比例 ≤ 50%`、`> 50%` 三组，0%和无记录另列。每行只显示“变量名 / 缺失比例 / xx%”，数量与结局分布仍保留在审核数据及CSV中。分组由实际数量计算，页面元数据 `missing_groups` 和摘要 `missing_group_counts` 不代表处理授权。低组默认拟采用多重插补，中高组待定；每组列表后仅一个意见框，保存用户整组意见或变量例外。

### clinical_review_decisions.json

- `schema_version`: `"1.4"`；`stage`: `"data_quality"`；`status`: `"reviewed"`。
- `review_version`: `"1.7.2"`；旧质量审核须重新生成，阶段1目标及阶段2变量选择的结构版本不变。
- `source_sha256`、`sheet`、`selection_fingerprint`、`outcome`、`scope_variables`: 从已确认选择生成并锁定。
- `issues`: 唯一完整覆盖本轮问题；每项 `issue_id`、`rule`、`variable`、`affected_n`、`user_action`、`user_reason`。零异常时 `[]`，仍须确认。
- `user_action`: 选择核对来源、保留、设缺失、修正、排除或无需处理，均须用户意见与理由，修改/排除须具体规则。
- 不生成 `sentinel_decisions` 或疑似编码计数；旧版带此决定的审核须重新核查，不能静默沿用自动转换规则。用户明确认定某编码表示缺失时，通过确认后的 `operations` 精确处理。
- `missing_decisions`: 完整覆盖结局及已选解释变量，锁定 `variable`、`role`、`total_n`、`observed_missing_n`、`missing_pct`。用户填 `user_choice`、`user_reason`；可选 `complete_case`、`exclude_variable`、`multiple_imputation`、`recover_source`、`structural_review`；无实际缺失时可用 `no_missing`。非no_missing须理由；结局禁止exclude_variable。缺失数量不把特殊码自动算空。
- `missing_group_feedback`: 完整覆盖实际存在的三类缺失分组，每项锁定 `group_key`、`label`、`variables`。低/中/高组键分别为 `below_30`、`from_30_to_50`、`above_50`；`user_text` 保存原文，`resolved_text` 初始为空。仅代理据原文整理成逐变量方案、展示并获用户确认后才把已确认原文写入 `resolved_text`。两者不一致时正式审核不能通过，不能忽略文字而沿用默认决定。
- “提交缺失处理意见”导出 `clinical_review_feedback.json`，`status: "missing_feedback_submitted"`，仅为待整理反馈，不是 `reviewed`；其他异常可尚未决定。低组的预填 `user_choice: "multiple_imputation"` 及默认理由仍为草稿。中高组没有意见且没有已确认逐变量方案时，不能提交空白代替决定。
- `statistical_plan_confirmed`: 保持 `false`，质量审核不授权模型。

可再加 `--validate-decisions output/clinical_review_decisions.json`，校验并生成 `quality_binding.json`。`valid: true` 只代表决定结构完整，不代表已处理或模型已授权。

### processing_plan.json

- `schema_version`: `"1.7"`；`stage`: `"preprocessing"`；`status`: 用户确认规则与类型后为 `"confirmed"`。
- `source_sha256`、`sheet`、`selection_fingerprint`: 沿用模板。
- `quality_fingerprint`: 当前审核 JSON 的规范指纹，取验证输出的 `quality_binding.json`。
- `missing_strategy`: 模板为空，禁止默认替用户选择。当前执行器仅接受明确确认的 `"complete_case"`，且质量决定不能有MI/补录/专项处理待办；逐模型取完整案例，不统一删行。审核可选MI，但当前不执行MI，不能用此字段覆盖用户选择。
- 缺失决定exclude_variable会真正排除解释变量；其类型项须从variables移除。
- `postprocessing_missing_decisions`: 若处理后某保留变量的缺失数超过原实际空值数，须复核后逐项填 `variable`、当前 `missing_n`、`user_choice: "complete_case"`、`reason`；未匹配当前数量或未确认时停在阶段3。若选MI等方法，回专项方案，不能填完整案例冒充。此确认不由通用postprocessing_acceptances替代。
- `strip_whitespace`: 须确认 `true`；类型处理时去首尾空格，空串转缺失。
- `positive_value`: 处理后结局阳性值，明确指定并与参考组不同。
- `variables`: 完整且唯一覆盖处理后保留的结局与解释变量；每项 `name`、`type`、`reference`、`unit`、`unit_step`。类型由用户确认，不直接采纳候选；仅 continuous / binary / categorical。结局须 binary；分类参考组须实际存在；连续变量 `unit_step` 为有限正数。排除变量后删除其类型项。
- `operations`: 按顺序执行的明确规则。每项都有 `action`、`variable`、`issue_ids`、`reason`，不能使用自由文本表达式/任意代码。只能修改解释变量或结局，不能以未选/辅助字段清洗或删病例。修改类问题须绑定匹配的可执行规则；额外明确授权操作的 `issue_ids` 可为 `[]`。

| action | 额外字段 | 含义 |
| --- | --- | --- |
| `set_missing` | `values: ["999"]` | 将精确匹配取值设为缺失 |
| `map_values` | `mapping: {"男": "1", "女": "0"}` | 明确重编码；映射到 `null` 代表设缺失 |
| `scale` | `factor: 0.1` | 全列非缺失数值乘有限正系数，转换单位 |
| `exclude_rows` | `values: ["明确错误码"]` | 排除该变量精确匹配的记录，不自动去重 |
| `exclude_variable` | 无 | 排除解释变量，不得排除结局 |

映射/匹配按去首尾空格的字符串，数值型原码也用数据中实际字符串，例如 `"0"` 与 `"0.0"` 不自动视为相同。未命中规则或未处理非数字值会报错，不能记录为已修复。

执行：
```bash
python scripts/preprocess_data.py --input data.xlsx --research-question output/research_question.json --selection-decisions output/variable_selection_decisions.json --quality-decisions output/clinical_review_decisions.json --processing-plan output/processing_plan.json --outdir output
```

输出 `analysis_data.csv`（仅保留建模字段）、前后缺失表、`processing_log.csv`、`variable_dictionary.csv`、`postprocessing_issues.csv`、确认快照和 `preprocessing_manifest.json`。原始文件不变；记录前后人数与每项处理影响数，不输出标识明细。

新增/加重、具体取值/阈值等证据改变或修改后仍存且未获接受的线索会使状态停在 `awaiting_postprocessing_review`。用户复核后应修正规则；确需保留时，在计划中加入 `postprocessing_acceptances: [{"rule": "...", "variable": "...", "reason": "复核后依据"}]` 并重新确认处理。旧审核已明确保留、证据未变且未加重的线索无需重复询问。新增缺失仍须专门的postprocessing_missing_decisions，不能以通用接受项代替。只有 `status: completed` 才能进入阶段4。

## 阶段4：config.json

```bash
python scripts/run_logistic.py --workflow-dir output --prepare-plan
```

核验阶段1–3及分析集，生成 `config.template.json` 和样本数摘要，**不拟合**。展示方案并等待用户确认：

- `stage`: `"model_plan"`；`confirmed`: 确认后才为 `true`。
- `model`: 仅 `"binary_logistic"`；`analyses`: 默认 `["univariable", "multivariable"]`，允许明确选择其中之一。
- `preprocessing_fingerprint`: 处理清单文件 SHA256；前序变化后配置失效。
- `outcome`、`positive_value`、`missing_strategy`、`covariates`: 与已确认处理完全一致。每个保留解释变量分别做单因素，不按单因素 P 自动筛选。
- `multivariable_covariates`: 用户明确确认的多因素变量名列表，须来自保留变量且至少两个。调整集变化后用 `sample_counts` 重新报告该集合的 N/事件数，再确认。
- `study_design`: 非空研究设计；`independent_observations`: 用户确认独立观测后为 `true`。

配置样例仅说明结构，不能以示例或默认值代替用户确认。编码、参考组或缺失策略改变须回阶段3；添加新变量须回阶段2。

## 阶段5：执行并解释

```bash
python scripts/run_logistic.py --workflow-dir output --config output/config.json --outdir output/results
```

执行脚本要求完整前序记录和指纹匹配；不能只设置 `confirmed: true` 绕过。可省略 `--input`；若提供，只能是该目录绑定的 `analysis_data.csv`。来源文件、用户决定、分析集或前序确认发生变化须回到对应阶段。

结果包括 Table 1、单/多因素结果、实际 N/事件数、森林图（仅多因素成功时）、带数值解释的报告草稿和审计。未选择的模型输出空表并明确状态；失败不生成有效系数。生成后仍在阶段5：代理须读结果与警告、回应研究目标、比较粗/调整估计并完善最终报告，未完成解读不能宣称全流程结束。

本版不支持插补、Cox、条件 Logistic、权重、群组相关、重复测量、非线性或自动筛选。需要这些方法时停止说明，不偷换为已实现路线。
