#!/usr/bin/env python3
"""Five-stage workflow: ask the research question before variable selection and QA."""
import argparse
import hashlib
import json
import re
from collections import Counter
from pathlib import Path

import numpy as np
import pandas as pd

from profile_data import DATE, PRIVATE, column_profile, inventory, inventory_row, read_table
from quality_review import (
    MISSING_OPTIONS, REVIEW_VERSION, details_text, missing_mask, missing_review_groups,
    mixed_details, review_tables, validate_detailed_review, value_summary,
)

VERSION = '1.4'
WORKFLOW_VERSION = '1.7'
RESEARCH_QUESTION = '你想分析哪个疾病或临床事件的危险因素？研究对象是谁，表中哪一列表示这个结局？'
SELECTIONS = ['纳入', '不纳入']
ISSUE_ACTIONS = ['待确认', '核对原始病历/数据来源', '保留并记录', '设为缺失（须说明规则）',
                 '修正编码/单位（须提供映射）', '排除记录（须说明依据）', '排除变量（须说明依据）', '不适用/无需处理']
REASON_ACTIONS = set(ISSUE_ACTIONS[3:7])
ISSUE_COLUMNS = ['issue_id', 'rule', 'severity', 'variable', 'affected_n', 'description',
                 'suggestion', 'details', 'user_action', 'user_reason']
VARIABLE_DECISION_COLUMNS = ['variable', 'user_selection', 'user_note']
ISSUE_DECISION_COLUMNS = ['issue_id', 'rule', 'variable', 'affected_n', 'user_action', 'user_reason']


def save_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


def emit_summary(out, name, summary):
    summary['workflow_version'] = WORKFLOW_VERSION
    save_json(out / name, summary)
    print(json.dumps(summary, ensure_ascii=False, separators=(',', ':')))


def load_json(path):
    value = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    if not isinstance(value, dict):
        raise ValueError('决策文件必须为 JSON 对象')
    return value


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':')).encode('utf-8')).hexdigest()


def research_template(identity):
    return dict(schema_version=WORKFLOW_VERSION, stage='research_question', status='draft',
                **identity, research_population='', research_question='', outcome='',
                outcome_definition='', study_design='', analysis_unit='', notes='')


def validate_research_question(research, columns, identity):
    errors = []
    if (research.get('schema_version') != WORKFLOW_VERSION
            or research.get('stage') != 'research_question' or research.get('status') != 'confirmed'):
        errors.append('须先提问并获得用户反馈，确认 research_question.json 后才能选择变量')
    if any(research.get(key) != value for key, value in identity.items()):
        errors.append('研究目标对应的数据来源或工作表已改变')
    for key in ('research_population', 'research_question', 'outcome_definition'):
        if not isinstance(research.get(key), str) or not research[key].strip():
            errors.append(f'研究目标缺少用户确认的 {key}')
    outcome = research.get('outcome')
    if not isinstance(outcome, str) or outcome not in columns or PRIVATE.search(outcome):
        errors.append('须确认原始数据中非身份标识的结局字段；派生结局须先另行确认')
    return errors


def source_identity(path, sheet=None):
    path = Path(path)
    actual_sheet = None
    if path.suffix.lower() in {'.xlsx', '.xlsm'}:
        with pd.ExcelFile(path) as book:
            names = book.sheet_names
        if sheet is None:
            if len(names) != 1:
                raise ValueError(f'有多个工作表，请先指定 --sheet；可选：{names}')
            actual_sheet = names[0]
        elif str(sheet) in names:
            actual_sheet = str(sheet)
        elif str(sheet).isdigit() and int(sheet) < len(names):
            actual_sheet = names[int(sheet)]
        else:
            raise ValueError(f'工作表不存在；可选：{names}')
    digest = hashlib.sha256()
    with path.open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(chunk)
    return {'source_sha256': digest.hexdigest(), 'sheet': actual_sheet}


def recommended_action(row):
    domain = str(row['domain'])
    if PRIVATE.search(row['variable']) or domain == '标识符、日期及管理字段':
        return '建议排除', '标识/日期/管理字段不直接入模；如需核对，可另行授权为辅助核查字段'
    if domain.startswith('待确认'):
        return '待确认', '需用户说明字段含义及分析角色；尚未进行数据质量核查'
    if domain in {'随访与结局相关变量', '治疗与医疗干预'}:
        return '待确认', '需确认是否为结局、结局后信息或治疗暴露，以及时间顺序'
    return '建议纳入', '仅为临床角色候选建议；选择后再检查数据可用性，不代表最终入模'


def selection_inventory(df):
    result = inventory(df)[['variable', 'domain', 'suggested_type', 'suggested_role']].copy()
    recommendations = [recommended_action(row) for row in result.to_dict('records')]
    result['ai_suggestion'] = [r[0] for r in recommendations]
    result['ai_reason'] = [r[1] for r in recommendations]
    result['user_selection'] = ''
    result['user_note'] = ''
    return result


def decision_rows(value, key, errors):
    rows = value.get(key)
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        errors.append(f'{key} 必须是对象列表')
        return []
    return rows


def validate_selection(dec, columns, identity, research=None):
    errors = []
    if dec.get('schema_version') != VERSION or dec.get('stage') != 'variable_selection':
        errors.append('须使用 v1.4 独立变量选择文件；旧版合并审核文件不能代替')
    if dec.get('status') != 'confirmed':
        errors.append('变量选择尚未确认')
    if any(dec.get(key) != value for key, value in identity.items()):
        errors.append('数据文件或工作表已改变，请重新确认变量')
    if research is not None:
        if dec.get('research_question_fingerprint') != fingerprint(research):
            errors.append('研究目标已改变或尚未绑定，请重新确认变量')
        if dec.get('outcome') != research.get('outcome'):
            errors.append('结局与阶段1用户确认的目标不一致')
    rows = decision_rows(dec, 'variables', errors)
    names = [r.get('variable') for r in rows]
    if any(not isinstance(name, str) for name in names):
        errors.append('变量名必须是字符串')
        return errors
    if len(names) != len(columns) or set(names) != set(columns):
        errors.append('必须覆盖全部原始字段，不得遗漏或增加字段')
    if len(names) != len(set(names)):
        errors.append('存在重复变量决策')
    if any(r.get('user_selection') not in SELECTIONS for r in rows):
        errors.append('每个字段必须选择纳入或不纳入')
    outcome = dec.get('outcome')
    if not isinstance(outcome, str) or outcome not in columns:
        errors.append('须指定原始结局字段')
        outcome = ''
    included = {r['variable'] for r in rows if r.get('user_selection') == '纳入'}
    if outcome in included:
        errors.append('结局字段单独指定，不得同时作为解释变量纳入')
    if any(PRIVATE.search(name) for name in included | {outcome}):
        errors.append('身份标识字段不得作为解释变量或结局')
    support = dec.get('support_variables', [])
    if not isinstance(support, list) or any(not isinstance(name, str) for name in support):
        errors.append('辅助核查字段必须是字段名列表')
    elif len(support) != len(set(support)) or not set(support).issubset(columns):
        errors.append('辅助核查字段有重复或不属于原始数据')
    elif set(support) & (included | {outcome}):
        errors.append('辅助核查字段不得重复指定结局或已纳入解释变量')
    if support and not str(dec.get('support_purpose') or '').strip():
        errors.append('授权辅助核查字段时必须填写用途')
    return errors


def selection_fingerprint(dec):
    relevant = {key: dec.get(key) for key in ('source_sha256', 'sheet', 'outcome', 'support_purpose',
                                            'positive_value', 'research_question_fingerprint')}
    relevant['variables'] = sorted((r['variable'], r['user_selection']) for r in dec['variables'])
    relevant['support_variables'] = sorted(dec.get('support_variables', []))
    return hashlib.sha256(json.dumps(relevant, sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()


def review_scope(dec, columns):
    selected = {r['variable'] for r in dec['variables'] if r['user_selection'] == '纳入'}
    selected.update(dec.get('support_variables', []))
    selected.add(dec['outcome'])
    return [name for name in columns if name in selected]


def scan(df, support_variables=(), inventory_rows=None):
    """The caller must pass only outcome, confirmed predictors and authorized support."""
    issues = []

    def add(rule, severity, variable, count, description, suggestion, details=None):
        issues.append(dict(issue_id=f'Q{len(issues)+1:04d}', rule=rule, severity=severity,
                           variable=variable, affected_n=int(count), description=description,
                           suggestion=suggestion, details=details_text(details or {}),
                           user_action='待确认', user_reason=''))

    n = len(df)
    if n == 0:
        add('EMPTY_DATA', '阻断', '（已选范围）', 0, '没有数据行', '检查数据来源')
    # Repeated clinical values do not establish duplicate patients or visits.
    for col in df.columns:
        key = str(col)
        profile = column_profile(df[col])
        if inventory_rows is not None:
            inventory_rows.append(inventory_row(key, profile))
        s = profile['text']
        null = missing_mask(s)
        valid = s[~null]
        m = int(null.sum())
        if valid.size and valid.nunique() == 1:
            add('CONSTANT_COLUMN', '提示', key, valid.size, '有效记录仅有一个取值', '请给出处置意见；可仅描述或明确排除',
                value_summary(valid, private=bool(PRIVATE.search(key) or DATE.search(key))))
        if PRIVATE.search(key):
            add('IDENTIFIER', '隐私', key, n, '经用户授权，仅用于辅助核查', '用户确认用途；不导出标识值，不进入回归',
                dict(total_n=n, missing_n=m, unique_n=int(valid.nunique()), values_redacted=True))
            if key in support_variables:
                repeated = int(valid.duplicated(keep=False).sum())
                if repeated:
                    add('REPEATED_IDENTIFIER', '需核对', key, repeated, '授权标识字段存在重复值', '用户核对重复患者或合法多访视并提供意见，不自动去重',
                        dict(repeated_records_n=repeated, repeated_groups_n=int(valid[valid.duplicated(keep=False)].nunique()),
                             values_redacted=True))
            continue
        if DATE.search(key):
            continue
        nums = profile['numeric'][~null]
        fraction = float(nums.notna().mean()) if len(valid) else 0
        if nums.notna().any() and nums.isna().any():
            add('MIXED_TYPE', '需核对', key, nums.isna().sum(), '数字与文字/编码混杂', '查看具体混杂形式及频数，由用户给出类型、映射或处理规则',
                mixed_details(valid, nums))
        if fraction >= 0.95 and len(valid):
            x = nums.dropna().astype(float)
            nonfinite = ~np.isfinite(x)
            if nonfinite.any():
                add('NONFINITE_VALUES', '需核对', key, nonfinite.sum(), '存在正负无穷等非有限数值',
                    '查看具体取值，由用户核对来源并指定处理规则', value_summary(x[nonfinite]))
                x = x[~nonfinite]
            if re.search(r'年龄|^age$|身高|^height$|体重|^weight$|^bmi$', key, re.I):
                negative = int((x < 0).sum())
                if negative:
                    add('NEGATIVE_MEASUREMENT', '需核对', key, negative, '年龄或人体测量出现负数', '列出负值及频数，由用户提供核实结果与处理意见',
                        value_summary(x[x < 0]))
            if len(x) >= 8 and x.nunique() > 5:
                q1, q3 = x.quantile([0.25, 0.75])
                iqr = q3 - q1
                extreme = int(((x < q1 - 3 * iqr) | (x > q3 + 3 * iqr)).sum()) if iqr > 0 else 0
                if extreme:
                    flagged = x[(x < q1 - 3*iqr) | (x > q3 + 3*iqr)]
                    add('EXTREME_IQR', '提示', key, extreme, '超出 Q1-3*IQR 或 Q3+3*IQR，仅为极端值线索', '查看阈值、范围及极端值，由用户确认保留、核对或具体处理规则',
                        dict(q1=float(q1), q3=float(q3), iqr=float(iqr),
                             lower_bound=float(q1-3*iqr), upper_bound=float(q3+3*iqr),
                             min=float(x.min()), max=float(x.max()), numeric_n=len(x),
                             flagged_values=value_summary(flagged)))
    return pd.DataFrame(issues, columns=ISSUE_COLUMNS)


def selection_template(selection, identity, research=None):
    return dict(schema_version=VERSION, stage='variable_selection', status='draft', **identity,
                research_question_fingerprint=fingerprint(research) if research is not None else '',
                outcome=research['outcome'] if research is not None else '',
                positive_value='', support_variables=[], support_purpose='', notes='',
                variables=selection[VARIABLE_DECISION_COLUMNS].to_dict('records'))


def quality_template(dec, scope, issues, frame=None):
    rows = issues[ISSUE_DECISION_COLUMNS].to_dict('records')
    detailed = review_tables(frame, dec) if frame is not None else dict(
        review_version=REVIEW_VERSION, missing_decisions=[], missing_group_feedback=[])
    return dict(schema_version=VERSION, stage='data_quality', status='draft', **detailed,
                source_sha256=dec['source_sha256'], sheet=dec['sheet'],
                selection_fingerprint=selection_fingerprint(dec), outcome=dec['outcome'],
                scope_variables=scope, issues=rows, notes='',
                statistical_plan_confirmed=False)


def validate_quality(review, template):
    errors = validate_detailed_review(review, template)
    for key in ('schema_version', 'stage', 'source_sha256', 'sheet', 'selection_fingerprint',
                'outcome', 'scope_variables'):
        if review.get(key) != template[key]:
            errors.append(f'{key} 与当前数据/变量选择不一致，请重新核查')
    if review.get('status') != 'reviewed':
        errors.append('数据质量审核尚未完成')
    rows = decision_rows(review, 'issues', errors)
    ids = [row.get('issue_id') for row in rows]
    expected = {row['issue_id']: row for row in template['issues']}
    if any(not isinstance(key, str) for key in ids):
        errors.append('异常编号必须是字符串')
        return errors
    if len(ids) != len(set(ids)) or set(ids) != set(expected):
        errors.append('异常记录必须完整且唯一，不得遗漏或增加')
    for row in rows:
        original = expected.get(row.get('issue_id'))
        if original and any(row.get(key) != original[key] for key in ('rule', 'variable', 'affected_n')):
            errors.append(f'{row["issue_id"]} 规则、字段或影响数与重新扫描不符')
        action = row.get('user_action')
        if action not in ISSUE_ACTIONS[1:]:
            errors.append(f'{row.get("issue_id")} 仍未选择有效处理方式')
        if not str(row.get('user_reason') or '').strip():
            errors.append(f'{row.get("issue_id")} 须填写用户处理意见、理由；修改/排除还须具体规则')
    return errors


def write_html(payload, target):
    if payload.get('decision', {}).get('stage') == 'data_quality':
        payload = dict(payload, missing_groups=missing_review_groups(payload['decision'].get('missing_decisions', [])))
    safe = json.dumps(payload, ensure_ascii=False, default=str).replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026')
    template = Path(__file__).resolve().parents[1] / 'assets' / 'review_workflow.html'
    Path(target).write_text(template.read_text(encoding='utf-8').replace('__REVIEW_DATA__', safe), encoding='utf-8')


def missingness_table(df, outcome):
    """Count actual blanks only; literal codes remain untouched."""
    y = df[outcome].astype('string').str.strip()
    groups = [('overall', pd.Series(True, index=df.index))]
    levels = sorted(y.dropna().unique().tolist())
    if len(levels) <= 20:
        groups.extend((f'outcome={level}', y.eq(level).fillna(False)) for level in levels)
    groups.append(('outcome_missing', y.isna()))
    rows = []
    for name in df.columns:
        if PRIVATE.search(str(name)):
            continue
        for group, mask in groups:
            values = df.loc[mask, name]
            rows.append(dict(variable=name, group=group, n=len(values),
                             missing=int(missing_mask(values).sum()),
                             missing_pct=round(100 * missing_mask(values).mean(), 2) if len(values) else None))
    return pd.DataFrame(rows)


def processing_template(dec, identity):
    predictors = [row['variable'] for row in dec['variables'] if row['user_selection'] == '纳入']
    return dict(schema_version=WORKFLOW_VERSION, stage='preprocessing', status='draft', **identity,
                selection_fingerprint=selection_fingerprint(dec), quality_fingerprint='',
                positive_value=dec.get('positive_value', ''), missing_strategy='',
                strip_whitespace=True, operations=[], postprocessing_acceptances=[],
                variables=[dict(name=name, type='', reference=None, unit='', unit_step=1)
                           for name in [dec['outcome']] + predictors], notes='')


def validation_result(out, errors, stage, review=None):
    specialized = [row for row in (review or {}).get('missing_decisions', [])
                   if row.get('user_choice') in {'multiple_imputation', 'recover_source', 'structural_review'}]
    result = dict(valid=not errors, errors=errors, stage=stage,
                  phase_number=3 if stage == 'data_quality' else 2,
                  status='blocked' if errors else 'awaiting_specialized_missing_plan' if specialized else 'awaiting_preprocessing',
                  formal_analysis_authorized=False,
                  blocking_reasons=errors or [f'{row["variable"]}: {row["user_choice"]} 尚待专项实现/核实' for row in specialized],
                  next_action=('修正决策后重新验证' if errors else
                               '记录用户选择，完成专项缺失方案；不得改用完整案例' if specialized else
                               '阶段3尚未完成：确认类型和处理规则，执行修复并复核'),
                  required_inputs=['corrected_decisions'] if errors else
                                  ['specialized_missing_plan'] if specialized else ['processing_plan.json'])
    emit_summary(out, 'review_validation.json', result)
    if errors:
        raise SystemExit(2)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', required=True)
    parser.add_argument('--sheet', help='多工作表文件必须先指定工作表')
    parser.add_argument('--outdir', required=True)
    parser.add_argument('--research-question', help='阶段1已获用户反馈并确认的研究目标 JSON')
    parser.add_argument('--selection-decisions', help='已确认的变量选择 JSON；未提供时仅生成选择页面')
    parser.add_argument('--validate-decisions', help='核验阶段3质量审核 JSON；须同时提供研究目标和变量选择文件')
    args = parser.parse_args(argv)
    if args.selection_decisions and not args.research_question:
        parser.error('须先提供 --research-question；不允许跳过目标提问阶段')
    if args.validate_decisions and not args.selection_decisions:
        parser.error('--validate-decisions 须同时提供 --selection-decisions；不会先执行全表扫描')
    out = Path(args.outdir)
    out.mkdir(parents=True, exist_ok=True)
    identity = source_identity(args.input, args.sheet)
    header = read_table(Path(args.input), identity['sheet'], nrows=0)
    header.columns = header.columns.map(str)
    if header.columns.duplicated().any():
        raise ValueError('重复字段名，无法建立唯一变量清单')
    columns = header.columns.tolist()
    if not args.research_question:
        save_json(out / 'research_question.template.json', research_template(identity))
        emit_summary(out, 'research_summary.json', dict(**identity, stage='research_question',
                     phase_number=1, rows=None, columns=len(columns), source_columns=columns,
                     read_scope='header_only', data_quality_scan_performed=False,
                     status='awaiting_research_question', question=RESEARCH_QUESTION,
                     next_action='向用户提问，等待研究对象、研究问题、结局字段及定义/时间窗反馈',
                     required_inputs=['research_question.json'], blocking_reasons=['研究目标尚未获用户确认'],
                     artifacts={'research_question.template.json':
                                str((out / 'research_question.template.json').resolve())}))
        return
    research = load_json(args.research_question)
    errors = validate_research_question(research, columns, identity)
    if errors:
        emit_summary(out, 'research_validation.json', dict(stage='research_question', phase_number=1,
                     status='blocked', valid=False, blocking_reasons=errors,
                     next_action='重新询问用户并确认研究目标', required_inputs=['research_question.json']))
        raise SystemExit(2)
    if not args.selection_decisions:
        selection = selection_inventory(header)
        selection.to_csv(out / 'variable_inventory.csv', index=False, encoding='utf-8-sig')
        selection.to_csv(out / 'variable_selection_template.csv', index=False, encoding='utf-8-sig')
        template = selection_template(selection, identity, research)
        save_json(out / 'variable_selection_decisions.template.json', template)
        write_html(dict(decision=template, variables=selection.to_dict('records'),
                        options=SELECTIONS, research_question=research['research_question'],
                        outcome_locked=True), out / 'interactive_review.html')
        emit_summary(out, 'selection_summary.json', dict(**identity, stage='variable_selection',
                  phase_number=2, research_question=research['research_question'], outcome=research['outcome'],
                  rows=None, row_count_status='deferred_until_selected_read', columns=len(columns),
                  read_scope='header_only', domain_counts=dict(Counter(selection['domain'].tolist())),
                  all_columns_covered=True, data_quality_scan_performed=False,
                  status='awaiting_variable_selection', unconfirmed_variables=len(columns),
                  next_action='沿用已确认结局，用户确认全部字段的纳入/不纳入',
                  required_inputs=['variable_selection_decisions.json'], blocking_reasons=['变量选择尚未确认'],
                  artifacts={name: str((out / name).resolve()) for name in
                             ('variable_inventory.csv', 'variable_selection_template.csv', 'interactive_review.html',
                              'variable_selection_decisions.template.json')}))
        return
    dec = load_json(args.selection_decisions)
    errors = validate_selection(dec, columns, identity, research)
    if errors:
        validation_result(out, errors, 'variable_selection')
    scope = review_scope(dec, columns)
    scope_set = set(scope)
    selected = read_table(Path(args.input), identity['sheet'], usecols=lambda name: str(name) in scope_set)
    selected.columns = selected.columns.map(str)
    selected = selected.loc[:, scope]
    detail_rows = [] if not args.validate_decisions else None
    issues = scan(selected, dec.get('support_variables', []), inventory_rows=detail_rows)
    template = quality_template(dec, scope, issues, selected)
    if args.validate_decisions:
        quality = load_json(args.validate_decisions)
        errors = validate_quality(quality, template)
        if not errors:
            save_json(out / 'quality_binding.json', {'quality_fingerprint': fingerprint(quality)})
        validation_result(out, errors, 'data_quality', quality)
        return
    detail = pd.DataFrame(detail_rows)
    choices = {row['variable']: row['user_selection'] for row in dec['variables']}
    detail['user_selection'] = [choices[name] for name in detail['variable']]
    detail['review_role'] = ['outcome' if name == dec['outcome'] else
                             'support_only' if name in dec.get('support_variables', []) else 'predictor'
                             for name in detail['variable']]
    detail.to_csv(out / 'selected_variable_inventory.csv', index=False, encoding='utf-8-sig')
    issues.to_csv(out / 'data_quality_issues.csv', index=False, encoding='utf-8-sig')
    missingness_table(selected, dec['outcome']).to_csv(out / 'missingness_before.csv', index=False, encoding='utf-8-sig')
    save_json(out / 'processing_plan.template.json', processing_template(dec, identity))
    roles = dict(zip(detail['variable'], detail['review_role']))
    pd.DataFrame([dict(variable=name, review_role=roles.get(name, 'not_reviewed')) for name in columns]
                 ).to_csv(out / 'review_scope.csv', index=False, encoding='utf-8-sig')
    save_json(out / 'clinical_review_decisions.template.json', template)
    write_html(dict(decision=template, issues=issues.to_dict('records'), actions=ISSUE_ACTIONS,
                    missingness=missingness_table(selected, dec['outcome']).to_dict('records'),
                    missing_options=MISSING_OPTIONS), out / 'interactive_data_quality.html')
    emit_summary(out, 'data_quality_summary.json', dict(**identity, stage='data_quality', rows=len(selected),
              phase_number=3, review_version=REVIEW_VERSION,
              row_count_status='confirmed_after_selected_read', read_scope='selected_columns_only',
              selection_fingerprint=selection_fingerprint(dec), outcome=dec['outcome'],
              reviewed_columns=len(scope), role_counts=dict(Counter(detail['review_role'].tolist())),
              missing_group_counts={group['key']: len(group['variables'])
                                    for group in missing_review_groups(template['missing_decisions'])},
              excluded_not_reviewed_count=len(columns)-len(scope),
              excluded_not_reviewed=[name for name in columns if name not in scope],
              issue_count=len(issues), issue_counts=dict(Counter(issues['severity'].tolist())),
              source_modified=False, data_quality_scan_performed=True, status='awaiting_data_quality_review',
              next_action='用户审核问题与处置；无异常也须确认审核',
              required_inputs=['clinical_review_decisions.json'], blocking_reasons=['质量审核尚未确认'],
              artifacts={name: str((out / name).resolve()) for name in
                         ('review_scope.csv', 'selected_variable_inventory.csv', 'data_quality_issues.csv',
                          'missingness_before.csv', 'processing_plan.template.json',
                          'interactive_data_quality.html', 'clinical_review_decisions.template.json')}))


if __name__ == '__main__':
    main()
