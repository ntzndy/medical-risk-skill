#!/usr/bin/env python3
"""Stage 3: execute explicitly confirmed rules on a derived, scoped analysis set."""
import argparse
import hashlib
import math
from pathlib import Path

import numpy as np
import pandas as pd

from profile_data import read_table
from review_workflow import (
    WORKFLOW_VERSION, emit_summary, fingerprint, load_json, missingness_table,
    quality_template, review_scope, save_json, scan, selection_fingerprint,
    source_identity, validate_quality, validate_research_question, validate_selection,
)

ACTION_OPERATIONS = {
    '设为缺失（须说明规则）': {'set_missing'},
    '修正编码/单位（须提供映射）': {'map_values', 'scale'},
    '排除记录（须说明依据）': {'exclude_rows'},
    '排除变量（须说明依据）': {'exclude_variable'},
}
CONFIRMATION_FILES = {
    'research': 'confirmations/research_question.json',
    'selection': 'confirmations/variable_selection.json',
    'quality': 'confirmations/clinical_review.json',
    'processing': 'confirmations/processing_plan.json',
}


def file_digest(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def require_valid(errors):
    if errors:
        raise ValueError('；'.join(errors))


def text_values(series):
    return series.astype('string').str.strip()


def validate_processing(plan, selection, quality, identity):
    errors = []
    if (plan.get('schema_version') != WORKFLOW_VERSION or plan.get('stage') != 'preprocessing'
            or plan.get('status') != 'confirmed'):
        errors.append('阶段3处理方案须由用户确认，草稿不能执行')
    if any(plan.get(key) != value for key, value in identity.items()):
        errors.append('处理方案来源指纹已失效')
    if plan.get('selection_fingerprint') != selection_fingerprint(selection):
        errors.append('处理方案与变量选择不一致')
    if plan.get('quality_fingerprint') != fingerprint(quality):
        errors.append('处理方案须绑定当前已审核质量决定')
    if plan.get('missing_strategy') != 'complete_case' or plan.get('strip_whitespace') is not True:
        errors.append('本版只支持完整案例，不插补；须确认去除字符串首尾空格的标准化')
    if any(row.get('user_action') == '核对原始病历/数据来源' for row in quality['issues']):
        errors.append('仍有待溯源项目；获得反馈并更新审核后才能结束阶段3')
    operations = plan.get('operations')
    if not isinstance(operations, list) or any(not isinstance(op, dict) for op in operations):
        errors.append('operations 必须是对象列表')
        return errors
    allowed = {selection['outcome']} | {
        row['variable'] for row in selection['variables'] if row['user_selection'] == '纳入'
    }
    issues = {row['issue_id']: row for row in quality['issues']}
    covered = set()
    for op in operations:
        action, variable = op.get('action'), op.get('variable')
        if action not in {'set_missing', 'map_values', 'scale', 'exclude_rows', 'exclude_variable'}:
            errors.append('未知处理操作；不得执行任意代码或静默插补')
        if not isinstance(variable, str) or variable not in allowed:
            errors.append('处理只能作用于已选解释变量和结局，不能清洗未选字段或辅助标识')
        if action == 'exclude_variable' and variable == selection['outcome']:
            errors.append('不能排除结局；更改研究目标须返回阶段1')
        if not isinstance(op.get('reason'), str) or not op['reason'].strip():
            errors.append('每个修改/排除操作必须有具体依据')
        ids = op.get('issue_ids')
        if not isinstance(ids, list) or any(not isinstance(key, str) for key in ids):
            errors.append('每项操作须提供 issue_ids 列表；额外明确授权的操作使用空列表')
            continue
        for key in ids:
            issue = issues.get(key)
            if (issue is None or issue['variable'] != variable
                    or action not in ACTION_OPERATIONS.get(issue['user_action'], set())):
                errors.append(f'{key}: 操作与已确认的问题处置不一致')
            else:
                covered.add(key)
    for key, issue in issues.items():
        if issue['user_action'] in ACTION_OPERATIONS and key not in covered:
            errors.append(f'{key}: 只记录了修改/排除决定，尚未提供可执行规则')
    return errors


def apply_operations(frame, operations, outcome):
    result = frame.copy()
    log = []
    for op in operations:
        action, name = op['action'], op['variable']
        if name not in result:
            raise ValueError(f'{name}: 已被排除，不能再次处理')
        before = len(result)
        values = text_values(result[name])
        if action == 'exclude_variable':
            if name == outcome:
                raise ValueError('不能排除结局')
            affected = int(result[name].notna().sum())
            result = result.drop(columns=name)
        elif action == 'scale':
            factor = op.get('factor')
            if isinstance(factor, bool) or not isinstance(factor, (int, float)) or not math.isfinite(factor) or factor <= 0:
                raise ValueError(f'{name}: 单位转换 factor 必须是有限正数')
            numeric = pd.to_numeric(values, errors='coerce')
            if (values.notna() & numeric.isna()).any():
                raise ValueError(f'{name}: 单位转换前须先处理非数字编码')
            result[name] = numeric * factor
            affected = int(numeric.notna().sum())
        elif action in {'set_missing', 'exclude_rows'}:
            targets = op.get('values')
            if not isinstance(targets, list) or not targets or any(
                    not isinstance(value, (str, int, float)) or isinstance(value, bool)
                    or (isinstance(value, float) and not math.isfinite(value)) for value in targets):
                raise ValueError(f'{name}: values 须明确列出待处理的原始取值')
            mask = values.isin([str(value).strip() for value in targets])
            affected = int(mask.sum())
            if not affected:
                raise ValueError(f'{name}: 规则未匹配任何记录，请核对取值，不能记作已修复')
            if action == 'set_missing':
                result.loc[mask, name] = pd.NA
            else:
                result = result.loc[~mask].copy()
        elif action == 'map_values':
            mapping = op.get('mapping')
            if not isinstance(mapping, dict) or not mapping or any(
                    not isinstance(key, str) or key != key.strip() for key in mapping):
                raise ValueError(f'{name}: mapping 须为明确的原值到新值映射，键不含首尾空格')
            if any(value is not None and (
                    isinstance(value, bool) or not isinstance(value, (str, int, float))
                    or (isinstance(value, float) and not math.isfinite(value))) for value in mapping.values()):
                raise ValueError(f'{name}: 映射值须为字符串、有限数值或 null')
            missing_keys = set(mapping) - set(values.dropna())
            if missing_keys:
                raise ValueError(f'{name}: 部分映射原值不存在，请核对规则')
            mask = values.isin(mapping)
            affected = int(mask.sum())
            result.loc[mask, name] = values.loc[mask].map(mapping)
        else:
            raise ValueError(f'不支持的处理操作：{action}')
        log.append(dict(action=action, variable=name, affected_n=affected,
                        rows_before=before, rows_after=len(result), reason=op['reason']))
    return result, log


def apply_types(frame, specs, outcome, positive):
    if not isinstance(specs, list) or any(not isinstance(spec, dict) for spec in specs):
        raise ValueError('variables 必须为变量类型对象列表')
    names = [spec.get('name') for spec in specs]
    if any(not isinstance(name, str) for name in names) or len(names) != len(set(names)) or set(names) != set(frame.columns):
        raise ValueError('类型确认须完整且唯一地覆盖保留的结局和解释变量；排除变量后移除其类型项')
    result = frame.copy()
    dictionary = []
    for spec in specs:
        name, kind = spec['name'], spec.get('type')
        values = text_values(result[name]).replace('', pd.NA)
        if kind == 'continuous':
            if name == outcome:
                raise ValueError('本版结局须为二分类，不自动二分连续结局')
            numeric = pd.to_numeric(values, errors='coerce').astype(float)
            if (values.notna() & numeric.isna()).any() or not np.isfinite(numeric.dropna()).all():
                raise ValueError(f'{name}: 存在未处理的非数字或无穷值，不能静默设缺失')
            step = spec.get('unit_step', 1)
            if isinstance(step, bool) or not isinstance(step, (int, float)) or not math.isfinite(step) or step <= 0:
                raise ValueError(f'{name}: unit_step 必须是有限正数')
            if numeric.nunique() < 2:
                raise ValueError(f'{name}: 没有足够变异；须返回审核并明确是否排除变量')
            result[name] = numeric
            reference = None
        elif kind in {'binary', 'categorical'}:
            if name == outcome and kind != 'binary':
                raise ValueError('结局必须明确确认为 binary')
            levels = sorted(values.dropna().unique().tolist())
            reference = spec.get('reference')
            if reference is None or str(reference).strip() not in levels:
                raise ValueError(f'{name}: 须确认实际存在的参考组')
            reference = str(reference).strip()
            if len(levels) < 2 or (kind == 'binary' and len(levels) != 2):
                raise ValueError(f'{name}: 分类水平与确认类型不符')
            result[name] = values
            step = 1
        else:
            raise ValueError(f'{name}: 须由用户确认 continuous / binary / categorical，不能只用类型候选')
        dictionary.append(dict(name=name, type=kind, reference=reference,
                               unit=spec.get('unit', ''), unit_step=step))
    if positive is None or str(positive).strip() not in set(result[outcome].dropna()):
        raise ValueError('须确认处理后结局的阳性值')
    outcome_spec = next(spec for spec in dictionary if spec['name'] == outcome)
    if outcome_spec['reference'] == str(positive).strip():
        raise ValueError('结局参考组不能同时是阳性组')
    if len(dictionary) < 2:
        raise ValueError('至少保留一个解释变量')
    return result, dictionary


def load_preprocessing(workflow_dir):
    """Verify stages 1-3 and stale artifacts before preparing or running a model."""
    root = Path(workflow_dir).resolve()
    manifest_path = root / 'preprocessing_manifest.json'
    manifest = load_json(manifest_path)
    if (manifest.get('schema_version') != WORKFLOW_VERSION
            or manifest.get('stage') != 'preprocessing' or manifest.get('status') != 'completed'):
        raise ValueError('阶段3尚未完成，不能准备或执行模型')
    decisions = {}
    for key, relative in CONFIRMATION_FILES.items():
        entry = manifest.get('confirmations', {}).get(key, {})
        if entry.get('file') != relative:
            raise ValueError('前序确认记录缺失')
        path = root / relative
        if file_digest(path) != entry.get('sha256'):
            raise ValueError('前序决定已改变，须重新处理并确认后续阶段')
        if file_digest(entry['input_path']) != entry.get('input_sha256'):
            raise ValueError('用户的前序决定文件已更新，旧处理结果不能直接使用')
        decisions[key] = load_json(path)
    identity = {key: manifest[key] for key in ('source_sha256', 'sheet')}
    if source_identity(manifest['source_path'], manifest['sheet']) != identity:
        raise ValueError('原始数据已改变，旧处理结果和模型确认失效')
    columns = manifest['source_columns']
    research, selection, quality, plan = (decisions[key] for key in ('research', 'selection', 'quality', 'processing'))
    require_valid(validate_research_question(research, columns, identity))
    require_valid(validate_selection(selection, columns, identity, research))
    if (quality.get('status') != 'reviewed'
            or quality.get('selection_fingerprint') != selection_fingerprint(selection)):
        raise ValueError('质量审核尚未完成或与变量选择不一致')
    require_valid(validate_processing(plan, selection, quality, identity))
    data_path = root / 'analysis_data.csv'
    if manifest.get('data_file') != data_path.name or file_digest(data_path) != manifest.get('data_sha256'):
        raise ValueError('分析数据集缺失或已改变，须重新执行阶段3')
    return manifest, file_digest(manifest_path), decisions, data_path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('input', 'research-question', 'selection-decisions', 'quality-decisions', 'processing-plan', 'outdir'):
        parser.add_argument('--' + name, required=True)
    args = parser.parse_args(argv)
    source = Path(args.input)
    research, selection, quality, plan = (
        load_json(path) for path in (args.research_question, args.selection_decisions,
                                    args.quality_decisions, args.processing_plan)
    )
    identity = source_identity(source, research.get('sheet'))
    header = read_table(source, identity['sheet'], nrows=0)
    columns = header.columns.map(str).tolist()
    require_valid(validate_research_question(research, columns, identity))
    require_valid(validate_selection(selection, columns, identity, research))
    scope = review_scope(selection, columns)
    selected = read_table(source, identity['sheet'], usecols=lambda name: str(name) in scope)
    selected.columns = selected.columns.map(str)
    selected = selected.loc[:, scope]
    issues = scan(selected, selection.get('support_variables', []))
    require_valid(validate_quality(quality, quality_template(selection, scope, issues)))
    require_valid(validate_processing(plan, selection, quality, identity))
    model_names = [name for name in scope if name not in selection.get('support_variables', [])]
    original = selected.loc[:, model_names]
    processed, log = apply_operations(original, plan['operations'], selection['outcome'])
    processed, dictionary = apply_types(processed, plan.get('variables'), selection['outcome'], plan.get('positive_value'))
    if not len(processed):
        raise ValueError('处理后没有数据，不能进入模型确认')
    for name in processed:
        log.append(dict(action='confirm_type_and_trim', variable=name,
                        affected_n=int(processed[name].notna().sum()),
                        rows_before=len(processed), rows_after=len(processed),
                        reason='用户确认类型；字符串去首尾空格，空串转缺失；不插补'))
    out = Path(args.outdir)
    outputs = ['analysis_data.csv', 'processing_log.csv', 'variable_dictionary.csv',
               'missingness_before.csv', 'missingness_after.csv', 'postprocessing_issues.csv',
               'preprocessing_manifest.json', 'preprocessing_summary.json', *CONFIRMATION_FILES.values()]
    protected = {Path(path).resolve() for path in
                 (args.input, args.research_question, args.selection_decisions, args.quality_decisions, args.processing_plan)}
    if any((out / name).resolve() in protected for name in outputs):
        raise ValueError('输出会覆盖原始文件或输入决定，已停止')
    out.mkdir(parents=True, exist_ok=True)
    processed.to_csv(out / 'analysis_data.csv', index=False, encoding='utf-8-sig')
    pd.DataFrame(log).to_csv(out / 'processing_log.csv', index=False, encoding='utf-8-sig')
    pd.DataFrame(dictionary).to_csv(out / 'variable_dictionary.csv', index=False, encoding='utf-8-sig')
    missingness_table(original, selection['outcome']).to_csv(out / 'missingness_before.csv', index=False, encoding='utf-8-sig')
    missingness_table(processed, selection['outcome']).to_csv(out / 'missingness_after.csv', index=False, encoding='utf-8-sig')
    residual = scan(processed)
    residual.to_csv(out / 'postprocessing_issues.csv', index=False, encoding='utf-8-sig')
    acceptances = plan.get('postprocessing_acceptances', [])
    if not isinstance(acceptances, list) or any(
            not isinstance(row, dict) or not isinstance(row.get('reason'), str) or not row['reason'].strip()
            for row in acceptances):
        raise ValueError('postprocessing_acceptances 须逐项说明复核残留线索后的保留依据')
    accepted = {(row.get('rule'), row.get('variable')) for row in acceptances}
    retained = {(row['rule'], row['variable']): row['affected_n'] for row in quality['issues']
                if row['user_action'] in {'保留并记录', '不适用/无需处理'}}
    pending = [row for row in residual.to_dict('records')
               if (row['rule'], row['variable']) not in accepted
               and row['affected_n'] > retained.get((row['rule'], row['variable']), -1)]
    confirmations = {}
    inputs = {'research': args.research_question, 'selection': args.selection_decisions,
              'quality': args.quality_decisions, 'processing': args.processing_plan}
    for key, relative in CONFIRMATION_FILES.items():
        path = out / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        save_json(path, {'research': research, 'selection': selection, 'quality': quality, 'processing': plan}[key])
        confirmations[key] = dict(file=relative, sha256=file_digest(path),
                                 input_path=str(Path(inputs[key]).resolve()), input_sha256=file_digest(inputs[key]))
    status = 'awaiting_postprocessing_review' if pending else 'completed'
    manifest = dict(schema_version=WORKFLOW_VERSION, stage='preprocessing', status=status,
                    **identity, source_path=str(source.resolve()), source_columns=columns,
                    data_file='analysis_data.csv', data_sha256=file_digest(out / 'analysis_data.csv'),
                    outcome=selection['outcome'], positive_value=str(plan['positive_value']).strip(),
                    rows_before=len(original), rows_after=len(processed),
                    covariates=[spec for spec in dictionary if spec['name'] != selection['outcome']],
                    missing_strategy='complete_case', confirmations=confirmations,
                    residual_issue_count=len(residual))
    save_json(out / 'preprocessing_manifest.json', manifest)
    emit_summary(out, 'preprocessing_summary.json', dict(stage='preprocessing', phase_number=3,
                 status=status, rows_before=len(original), rows_after=len(processed),
                 excluded_rows=len(original) - len(processed),
                 retained_predictors=[spec['name'] for spec in manifest['covariates']],
                 residual_issue_count=len(residual), source_modified=False,
                 next_action=('仍在阶段3：复核新增/加重或处理后仍存的线索，更新规则或确认保留依据后重跑'
                              if pending else '阶段3处理已完成；展示前后结果后，进入阶段4确认 Logistic 方案'),
                 required_inputs=['postprocessing_review'] if pending else ['stage_4_model_confirmation'],
                 blocking_reasons=[f'{row["rule"]}: {row["variable"]} 尚未复核' for row in pending],
                 artifacts={name: str((out / name).resolve()) for name in outputs if (out / name).is_file()}))


if __name__ == '__main__':
    main()
