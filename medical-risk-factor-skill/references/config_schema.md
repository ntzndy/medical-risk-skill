# 五阶段配置与执行约束

每次命令只推进当前阶段；由代理记录用户实际反馈，不要求用户编辑 JSON。模板的 draft / false 不能自行改作已确认；用户确认只适用于呈现的当前阶段。

## 阶段1：research_question.json

基础命令 `python scripts/review_workflow.py --input data.xlsx --sheet Sheet1 --outdir output` 仅生成表头摘要和 `research_question.template.json`，不生成变量选择任务或质量报告。必须向用户提问研究目标并等待反馈。

- `schema_version`: `"1.6"`；`stage`: `"research_question"`；`status`: 用户明确反馈后才为 `"confirmed"`。
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

再加 `--selection-decisions output/variable_selection_decisions.json`，生成质量页面、问题 CSV、`missingness_before.csv` 及 `processing_plan.template.json`。缺失表只统计实际空值；`NA`、`null`、`999` 等文字/编码保留原值并作为核查线索，不静默设缺失。

### clinical_review_decisions.json

- `schema_version`: `"1.4"`；`stage`: `"data_quality"`；`status`: `"reviewed"`。
- `source_sha256`、`sheet`、`selection_fingerprint`、`outcome`、`scope_variables`: 从已确认选择生成并锁定。
- `issues`: 唯一完整覆盖本轮问题；每项 `issue_id`、`rule`、`variable`、`affected_n`、`user_action`、`user_reason`。零异常时 `[]`，仍须确认。
- `user_action`: 核对原始病历/数据来源、保留并记录、设为缺失（须说明规则）、修正编码/单位（须提供映射）、排除记录（须说明依据）、排除变量（须说明依据）、不适用/无需处理。修改/排除须说明理由；仍为“核对来源”时不得完成阶段3。
- `statistical_plan_confirmed`: 保持 `false`，质量审核不授权模型。

可再加 `--validate-decisions output/clinical_review_decisions.json`，校验并生成 `quality_binding.json`。`valid: true` 只代表决定结构完整，不代表已处理或模型已授权。

### processing_plan.json

- `schema_version`: `"1.6"`；`stage`: `"preprocessing"`；`status`: 用户确认规则与类型后为 `"confirmed"`。
- `source_sha256`、`sheet`、`selection_fingerprint`: 沿用模板。
- `quality_fingerprint`: 当前审核 JSON 的规范指纹，取验证输出的 `quality_binding.json`。
- `missing_strategy`: 仅 `"complete_case"`，逐模型完整案例，不提前按所有选定变量统一删行；不插补。
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

新增/加重或修改后仍存的线索会使状态停在 `awaiting_postprocessing_review`。用户复核后应修正规则；确需保留时，在计划中加入 `postprocessing_acceptances: [{"rule": "...", "variable": "...", "reason": "复核后依据"}]` 并重新确认处理。旧审核已明确保留且未加重的线索无需重复询问。只有 `status: completed` 才能进入阶段4。

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
