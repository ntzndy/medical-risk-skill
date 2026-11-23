# 医学危险因素分析 Skill v1.7.2

严格逐层推进：**1 读取文件并提问确认目标 → 2 选择变量 → 3 核查并执行确认修复 → 4 确认 Logistic 方案 → 5 分析与解释**。未完成当前阶段不进入下一阶段，一次确认不代表后续授权。

## 使用

安装依赖：`python -m pip install -r requirements.txt`。以下命令均在 skill 目录执行。

### 阶段1：读取并提问
```bash
python scripts/review_workflow.py --input data.xlsx --sheet Sheet1 --outdir output
```
只读表头并计算来源指纹，生成 `research_summary.json`、`research_question.template.json`。向用户提问：“你想分析哪个疾病或临床事件的危险因素？研究对象是谁，表中哪一列表示这个结局？”等待反馈，记录并确认 `research_question.json`，不凭文件名代答，不提前核查。

### 阶段2：全量变量选择
```bash
python scripts/review_workflow.py --input data.xlsx --sheet Sheet1 --outdir output --research-question output/research_question.json
```
生成全量字段 CSV 和离线 `interactive_review.html`。结局沿用阶段1并锁定，所有字段由用户选择纳入/不纳入；结局行不作为解释变量。AI 建议不是用户选择。辅助核查字段另行授权及说明用途。确认后导出 `variable_selection_decisions.json`。

### 阶段3：仅核查选定范围，确认后执行修复
```bash
python scripts/review_workflow.py --input data.xlsx --sheet Sheet1 --outdir output --research-question output/research_question.json --selection-decisions output/variable_selection_decisions.json
```
生成缺失核查、异常具体情况、类型候选、处理模板和 `interactive_data_quality.html`。缺失按 `0%<缺失比例<30%`、`30%≤缺失比例≤50%`、`>50%` 分组，无缺失另列。页面逐变量仅显示“变量名 / 缺失比例 / xx%”，每组列表后提供一个意见框；详细人数和结局分布保留在核查文件中。低组默认拟采用多重插补，用户可填写变量例外；中高组不预设方法，由用户填写整组或逐变量处理意见。恰好30%和50%归入中组。

“提交缺失处理意见”导出 `clinical_review_feedback.json`，仅保存待整理的原文，不执行处理。代理据此整理成逐变量方案、展示并获确认后，才能完成质量审核。可选完整案例、排除解释变量、MI、补录及专项处理；MI尚无执行引擎，默认或选择MI后仍须暂停，不能替换为完整案例。

缺失不在其他异常中重复，也不再自动识别或单列疑似缺失码；非空编码保留原值，参加常规类型和极端值核查。混杂显示具体形式及频数，极端值显示阈值、范围和取值，其他问题也须用户意见与依据。未选字段不核查。详细方法及文献见 `references/quality_review.md`，决定结构见 `references/config_schema.md`。

可用 `--validate-decisions output/clinical_review_decisions.json` 在上一命令上验证审核完整性，获取当前 `quality_fingerprint`；验证通过不等于修复完成。随后执行：
```bash
python scripts/preprocess_data.py --input data.xlsx --research-question output/research_question.json --selection-decisions output/variable_selection_decisions.json --quality-decisions output/clinical_review_decisions.json --processing-plan output/processing_plan.json --outdir output
```
另存 `analysis_data.csv`、处理前后缺失表、处理日志和确认清单，不改原文件。不插补，不按未选字段删病例。新出现或处理后仍存的问题需再次复核，清单 `status: completed` 后才能进入阶段4。

### 阶段4：确认单因素与多因素 Logistic
```bash
python scripts/run_logistic.py --workflow-dir output --prepare-plan
```
只生成待确认 `config.template.json` 和预计样本数，不拟合。默认单因素和多因素均做，也可明确仅选择其中之一；展示单因素范围、多因素调整集、编码、完整案例策略、预计 N/事件数、研究设计与独立观测假设。用户确认后由代理记录 `config.json`，不自动按单因素 P 值选变量。

### 阶段5：分析并解释结果
```bash
python scripts/run_logistic.py --workflow-dir output --config output/config.json --outdir output/results
```
程序核验所有前序确认和数据指纹，再生成描述表、单/多因素结果、OR/CI/P、模型样本数、森林图、报告草稿和审计。代理在同一阶段继续阅读实际结果、解释研究问题并完善最终报告；只交结果文件不算完成。

## 边界与验证
- 原始文件只读；所有改值与排除必须有用户确认的具体规则。
- 独立观测、二分类结局、逐模型完整案例是本版执行范围；不支持插补、Cox、匹配/聚类、重复测量等，遇到不适用时停下说明。
- 目标、选择、处理或数据变化会使对应下游确认失效；旧版审核须绑定新目标并重新确认。
- 正常使用优先读当前阶段摘要和产物，不默认读全部源码/参考资料，不重复输出完整清单或 JSON。
- 测试：`python -m unittest discover -s tests -v`，全部使用合成数据。
