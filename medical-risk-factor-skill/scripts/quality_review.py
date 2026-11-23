"""Concrete, privacy-aware evidence and explicit user decisions for stage 3."""
import json
import re

import pandas as pd

from profile_data import PRIVATE

REVIEW_VERSION = '1.7.2'
DEFAULT_MI_REASON = '按用户设定的低于30%组默认方案，拟采用多重插补；尚需确认适用性、插补模型及实现。'
MISSING_OPTIONS = [
    dict(value='complete_case', label='删除缺失行（逐模型完整案例）', executable=True,
         basis='仅按该模型所需字段取完整记录；需评估样本损失、选择偏倚和缺失机制，不能只凭缺失率决定。[S1,S2]'),
    dict(value='exclude_variable', label='该解释变量不纳入分析', executable=True,
         basis='适用于非必要且数据不可用的解释变量；删除重要混杂因素可能引入偏倚，不能机械按比例删变量。结局不可选。[S1,S2]'),
    dict(value='multiple_imputation', label='多重插补（需另行实现与确认）', executable=False,
         basis='适当缺失机制及相容插补模型下可考虑；须确认变量类型、辅助变量、插补次数、诊断及Rubin合并。当前引擎未实现，选择后暂停，绝不改用完整案例。[S1,S3]'),
    dict(value='recover_source', label='核对来源并补录后再分析', executable=False,
         basis='仅据可核实来源补录，不猜测或凭均值填充；更新数据后重新核查。'),
    dict(value='structural_review', label='结构性不适用／检测界限，另定方案', executable=False,
         basis='不把不适用或检测上下限当作普通随机缺失；先确认含义、研究人群及可执行分析方案。[S1,S3]'),
]


def missing_mask(series):
    text = series.astype('string').str.strip()
    return text.isna() | text.eq('').fillna(False)


def missing_review_groups(rows):
    """Use exact counts, not rounded percentages, at the 30% and 50% boundaries."""
    labels = [
        ('below_30', '缺失比例小于30%（默认多重插补）'),
        ('from_30_to_50', '缺失比例30%至50%（含30%和50%）'),
        ('above_50', '缺失比例大于50%'),
        ('no_missing', '无缺失（0%）'),
        ('unavailable', '无记录，缺失比例不可计算'),
    ]
    grouped = {key: [] for key, _ in labels}
    for row in rows:
        count, total = row['observed_missing_n'], row['total_n']
        if not total:
            key = 'unavailable'
        elif not count:
            key = 'no_missing'
        else:
            key = 'below_30' if 10 * count < 3 * total else 'from_30_to_50' if 2 * count <= total else 'above_50'
        grouped[key].append(row['variable'])
    return [dict(key=key, label=label, variables=grouped[key]) for key, label in labels if grouped[key]]


def value_summary(series, private=False, limit=20):
    if private:
        return dict(redacted=True, note='标识值不导出')
    counts = series.dropna().astype('string').str.strip().value_counts()
    items = []
    for value, count in counts.head(limit).items():
        # Do not expose contact details or narrative text embedded in clinical cells.
        hidden = (len(value) > 60 or re.search(
            r'@|\d{15,}|\b1[3-9]\d{9}\b|\d{4}[-/]\d{1,2}[-/]\d{1,2}|姓名|电话|住址', value))
        items.append(dict(value='[敏感或长文本已隐藏]' if hidden else value, count=int(count)))
    return dict(values=items, distinct_n=int(len(counts)), shown_n=len(items),
                omitted_distinct_n=max(0, len(counts) - len(items)))


def mixed_details(text, numeric):
    nonnumeric = text[numeric.isna()]
    forms = {}
    for value in nonnumeric.dropna():
        if re.match(r'^[<>≤≥]', value):
            kind = '检测上/下限表达'
        elif re.search(r'\d', value) and re.search(r'[A-Za-z\u4e00-\u9fff%/]', value):
            kind = '数值附带单位或文字'
        else:
            kind = '分类文字或其他非数字'
        forms[kind] = forms.get(kind, 0) + 1
    return dict(numeric_n=int(numeric.notna().sum()), nonnumeric_n=int(numeric.isna().sum()),
                forms=forms, nonnumeric_values=value_summary(nonnumeric))


def review_tables(frame, selection):
    missing = []
    support = set(selection.get('support_variables', []))
    for name in frame.columns:
        if PRIVATE.search(str(name)):
            continue
        text = frame[name].astype('string').str.strip()
        if name not in support:
            count = int(missing_mask(text).sum())
            default_mi = count > 0 and 10 * count < 3 * len(frame)
            missing.append(dict(variable=name, role='outcome' if name == selection['outcome'] else 'predictor',
                                total_n=len(frame), observed_missing_n=count,
                                missing_pct=round(100 * count / len(frame), 2) if len(frame) else None,
                                user_choice='multiple_imputation' if default_mi else '' if count or not len(frame) else 'no_missing',
                                user_reason=DEFAULT_MI_REASON if default_mi else ''))
    feedback = [dict(group_key=group['key'], label=group['label'], variables=group['variables'],
                     user_text='', resolved_text='')
                for group in missing_review_groups(missing) if group['key'] not in {'no_missing', 'unavailable'}]
    return dict(review_version=REVIEW_VERSION, missing_decisions=missing, missing_group_feedback=feedback)


def validate_detailed_review(review, template):
    errors = []
    if review.get('review_version') != REVIEW_VERSION:
        errors.append(f'须重新生成并完成 v{REVIEW_VERSION} 逐项质量审核，旧审核不能代替')
    if 'sentinel_decisions' in review:
        errors.append('本版已取消疑似缺失码决定；如需将具体编码设为缺失，须另行确认明确处理规则')
    feedback, expected_feedback = review.get('missing_group_feedback'), template.get('missing_group_feedback', [])
    if not isinstance(feedback, list) or any(not isinstance(row, dict) for row in feedback):
        errors.append('missing_group_feedback 必须完整覆盖本轮缺失分组')
    else:
        fields = ('group_key', 'label', 'variables')
        if ([{field: row.get(field) for field in fields} for row in feedback]
                != [{field: row.get(field) for field in fields} for row in expected_feedback]):
            errors.append('缺失分组意见与当前变量范围不符，请重新核查')
        for row in feedback:
            text, resolved = row.get('user_text'), row.get('resolved_text')
            if not isinstance(text, str) or not isinstance(resolved, str):
                errors.append('缺失分组意见及其已确认原文须为文字')
            elif text != resolved:
                errors.append(f'{row.get("label")}: 分组意见尚待整理为逐变量处理决定并确认，不能直接执行自由文字')
    for key, fields in (
        ('missing_decisions', ('variable', 'role', 'total_n', 'observed_missing_n', 'missing_pct')),
    ):
        rows, expected = review.get(key), template.get(key, [])
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            errors.append(f'{key} 必须完整覆盖本轮核查项')
            continue
        actual_binding = [{field: row.get(field) for field in fields} for row in rows]
        expected_binding = [{field: row.get(field) for field in fields} for row in expected]
        if actual_binding != expected_binding:
            errors.append(f'{key} 与当前变量、取值及数量不符，请重新核查')
            continue
        for row in rows:
            name = row['variable']
            choices = {option['value'] for option in MISSING_OPTIONS} | {'no_missing'}
            choice = row.get('user_choice')
            if not isinstance(choice, str) or choice not in choices:
                errors.append(f'{name}: 须选择缺失处理方式')
                continue
            if choice == 'no_missing' and row['observed_missing_n']:
                errors.append(f'{name}: 已确认存在缺失，不能选择无缺失')
            if not row['total_n']:
                errors.append(f'{name}: 没有数据记录，不能确认缺失处理方案')
            if choice != 'no_missing' and not str(row.get('user_reason') or '').strip():
                errors.append(f'{name}: 须说明缺失处理依据')
            if choice == 'exclude_variable' and row['role'] == 'outcome':
                errors.append('不能因缺失直接排除结局；须返回研究目标确认')
    return errors


def details_text(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)
