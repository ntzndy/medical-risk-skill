#!/usr/bin/env python3
"""Read-only de-identified clinical table inventory. Requires pandas, openpyxl for XLSX."""
import re
import pandas as pd

PRIVATE = re.compile(r'(身份证|姓名|电话|手机|地址|住址|name|phone|email|address|patient.?id|患者编号|病人编号|病历号|住院号|记录号|^id$)', re.I)
DATE = re.compile(r'日期|时间戳|出生|date|timestamp', re.I)

def read_table(path, sheet=0, *, usecols=None, nrows=None):
    if path.suffix.lower() in ('.xlsx', '.xlsm'):
        return pd.read_excel(path, sheet_name=sheet, dtype=object, usecols=usecols, nrows=nrows,
                             keep_default_na=False, na_values=[''])
    if path.suffix.lower() == '.csv':
        for encoding in ('utf-8-sig','gb18030'):
            try: return pd.read_csv(path, encoding=encoding, dtype=object, usecols=usecols, nrows=nrows,
                                    keep_default_na=False, na_values=[''])
            except UnicodeDecodeError: pass
    raise ValueError('Only .xlsx/.xlsm/.csv supported')

DOMAIN_RULES = [
    ('人口学与社会人口学特征', r'年龄|岁|性别|民族|出生|地区|居住|社会|教育|职业|收入'),
    ('人体测量与生长发育', r'身高|体重|BMI|体表面积|生长|头围|腰围'),
    ('既往史、家族史与合并症', r'既往|家族史|遗传史|围产|合并症|基础病|病史|过敏'),
    ('疾病/解剖学/病因学特征', r'诊断|疾病|病种|分型|亚型|侧别|单侧|双侧|肾|器官|畸形|发育|病因|基因'),
    ('临床表现与体征', r'症状|体征|血压|发热|水肿|感染|疼痛|表现'),
    ('实验室与生理指标', r'检验|实验室|血常规|血红蛋白|肌酐|尿素|eGFR|尿蛋白|蛋白尿|电解质|白蛋白|指标|浓度'),
    ('影像学与辅助检查', r'超声|彩超|CT|MRI|核医学|影像|病理|检查结果'),
    ('治疗与医疗干预', r'治疗|用药|药物|手术|透析|干预|激素|抗生素'),
    ('随访与结局相关变量', r'随访|终点|结局|进展|复发|死亡|事件|末次|复查|时间至|生存'),
    ('医疗服务/研究流程变量', r'入院|出院|科室|转诊|住院次数|就诊|入组|批次|来源'),
    ('标识符、日期及管理字段', r'编号|ID$|病历号|住院号|记录号|姓名|日期|时间戳|序号'),
]

def infer_domain(name):
    for domain, pattern in DOMAIN_RULES:
        if re.search(pattern, str(name), re.I):
            return domain
    return '待确认/其他（需人工归类）'

def column_profile(series):
    """One per-column conversion, reusable by inventory and scoped QA."""
    text=series.astype('string').str.strip()
    observed=text.notna() & ~text.isin(['','NA','N/A','null','NULL'])
    return dict(text=text, lower=text.str.lower(), values=text[observed],
                numeric=pd.to_numeric(text, errors='coerce'))


def inventory_row(name, profile=None):
    privacy=bool(PRIVATE.search(str(name)))
    domain='标识符、日期及管理字段' if privacy else infer_domain(name)
    administrative=privacy or domain=='标识符、日期及管理字段'
    row=dict(variable=str(name),domain=domain,nonmissing=None,missing=None,
             missing_pct=None,unique=None,
             suggested_type='identifier/private (exclude)' if privacy else '待选定后核查',
             suggested_role='仅用于标识/审计，不建议直接入模' if administrative else '候选解释变量/需结合研究设计确认',
             recommendation='暂不建议作为预测变量' if administrative else ('待确认' if domain.startswith('待确认') else '可考虑；需结合临床意义、时间顺序及混杂结构判断'),
             user_selection='',example_frequency='',numeric_fraction=None)
    if profile is None:
        return row
    non=profile['values']
    missing=len(profile['text'])-len(non)
    unique=int(non.nunique())
    numbers=profile['numeric'].loc[non.index]
    numeric_fraction=float(numbers.notna().mean()) if len(non) else 0
    if privacy: kind='identifier/private (exclude)'
    elif numeric_fraction>.95 and unique>10: kind='continuous_candidate'
    elif unique<=2: kind='binary_candidate'
    elif unique<=12: kind='categorical_candidate'
    else: kind='review_needed'
    summary=''
    if not privacy and not DATE.search(str(name)):
        counts=non.value_counts()
        summary='; '.join(f'{str(k)[:35]}:{v}' for k,v in counts[counts>=5].head(5).items())
    row.update(nonmissing=len(non),missing=missing,
               missing_pct=round(100*missing/max(len(profile['text']),1),2),
               unique=unique,suggested_type=kind,example_frequency=summary,
               numeric_fraction=round(numeric_fraction,3))
    return row


def inventory(df, detailed=False):
    """Metadata only unless passed an explicitly selected frame for detailed review."""
    return pd.DataFrame([inventory_row(name, column_profile(df[name]) if detailed else None)
                         for name in df.columns])

def main(argv=None):
    # The legacy entry point follows the same gates instead of bypassing stage 1.
    from review_workflow import main as workflow_main
    workflow_main(argv)
if __name__=='__main__':main()
