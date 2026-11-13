#!/usr/bin/env python3
"""Researcher-confirmed independent-observation binary logistic association analysis.
Requires pandas, numpy, statsmodels, matplotlib, openpyxl for Excel.
No imputation, matching, survival, clustering, repeated measures or automatic variable selection.
"""
import argparse, warnings
from pathlib import Path
import numpy as np
import pandas as pd
import statsmodels.api as sm
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from profile_data import read_table
from preprocess_data import load_preprocessing
from review_workflow import emit_summary, load_json, missingness_table, save_json

plt.rcParams['font.sans-serif'] = ['Microsoft YaHei', 'SimHei', 'Noto Sans CJK SC', 'DejaVu Sans']
plt.rcParams['axes.unicode_minus'] = False


def design(df, covars):
    pieces=[]
    for spec in covars:
        name=spec['name']; typ=spec['type']; s=df[name]
        if typ=='continuous':
            x=pd.to_numeric(s, errors='coerce')
            invalid=(s.notna() & x.isna())
            if invalid.any(): raise ValueError(f'{name}: non-numeric values found; fix units/codes before analysis')
            step=float(spec.get('unit_step',1))
            if not np.isfinite(step) or step<=0: raise ValueError(f'{name}: unit_step must be finite and >0')
            label=f'{name} [per {step:g} {spec.get("unit") or "units"}]'
            pieces.append(pd.DataFrame({label:x/step},index=df.index))
        elif typ in ('binary','categorical'):
            if 'reference' not in spec:raise ValueError(f'{name}: reference is mandatory')
            ref=str(spec['reference']);v=s.astype('string').str.strip();levels=sorted(v.dropna().unique().tolist())
            if ref not in levels:raise ValueError(f'{name}: reference {ref!r} not found: {levels}')
            if len(levels)<2:raise ValueError(f'{name}: fewer than two levels')
            if typ=='binary' and len(levels)!=2:raise ValueError(f'{name}: binary variable has {len(levels)} levels')
            d=pd.DataFrame(index=df.index)
            for level in levels:
                if level != ref:
                    lab=f'{name}: {level} vs {ref}'
                    d[lab]=(v==level).astype(float)
                    d.loc[v.isna(),lab]=np.nan
            pieces.append(d)
        else:raise ValueError(f'{name}: unsupported type {typ}')
    X=pd.concat(pieces,axis=1) if pieces else pd.DataFrame(index=df.index)
    if X.columns.duplicated().any():raise ValueError('Duplicate generated design column')
    return X

def fit(df, outcome, covars, positive):
    yraw=df[outcome].astype('string').str.strip()
    levels=yraw.dropna().unique().tolist()
    if len(levels)!=2 or str(positive) not in levels:raise ValueError(f'Outcome must have exactly two categories, including positive {positive!r}; got {levels}')
    y=(yraw==str(positive)).astype(float);y[yraw.isna()]=np.nan
    X=design(df,covars)
    merged=pd.concat([y.rename('outcome'),X],axis=1).dropna()
    if not len(merged):raise ValueError('No complete observations')
    Y=merged['outcome'].astype(float);XX=sm.add_constant(merged.drop(columns='outcome').astype(float),has_constant='add')
    n=int(len(Y));events=int(Y.sum());nonevents=n-events
    if not events or not nonevents:raise ValueError('No event/non-event contrast in model sample')
    if XX.shape[1]>n or np.linalg.matrix_rank(XX.to_numpy())<XX.shape[1]:raise ValueError('Singular/collinear design; reconsider variables or sparse categories')
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter('always')
        res=sm.GLM(Y,XX,family=sm.families.Binomial()).fit(maxiter=200)
    if any('PerfectSeparation' in warning.category.__name__ for warning in caught):
        raise ValueError('Perfect separation detected; do not interpret this model')
    if not res.converged:raise ValueError('Model did not converge; do not interpret coefficients')
    b=res.params;se=res.bse;ci=res.conf_int(); p=res.pvalues
    rows=[]
    for term in XX.columns:
        if term=='const':continue
        vals=[b[term],se[term],ci.loc[term,0],ci.loc[term,1]]
        if not all(np.isfinite(vals)):raise ValueError(f'Nonfinite estimates for {term}; possible separation')
        rows.append(dict(term=term,beta=float(b[term]),SE=float(se[term]),OR=float(np.exp(np.clip(b[term],-700,700))),CI_lower=float(np.exp(np.clip(ci.loc[term,0],-700,700))),CI_upper=float(np.exp(np.clip(ci.loc[term,1],-700,700))),p_value=float(p[term]),N=n,events=events,non_events=nonevents))
    flags=[str(w.message) for w in caught]
    if len(XX.columns)>1 and min(events,nonevents)/max(XX.shape[1]-1,1)<10:flags.append('Low events/non-events per model parameter: possible unstable estimates (not a fixed threshold).')
    if any(abs(r['beta'])>10 or r['SE']>5 for r in rows):flags.append('Extreme beta/SE: possible quasi-complete separation or unstable estimates.')
    return pd.DataFrame(rows),dict(N=n,events=events,non_events=nonevents,parameters=XX.shape[1]-1,flags=flags)

def describe(df, outcome, positive, covars):
    rows=[];ys=df[outcome].astype('string').str.strip()
    groups={'overall':pd.Series(True,index=df.index),'outcome_negative':ys.notna()&(ys!=str(positive)),'outcome_positive':ys==str(positive)}
    for spec in covars:
        name=spec['name'];s=df[name]
        for group,mask in groups.items():
            sub=s[mask]; non=sub.dropna(); missing=int(sub.isna().sum())
            if spec['type']=='continuous':
                z=pd.to_numeric(non,errors='coerce')
                if z.isna().any():raise ValueError(f'{name}: invalid numeric values')
                desc=f'{z.median():.3g} [{z.quantile(.25):.3g}, {z.quantile(.75):.3g}]' if len(z) else 'NA'
                rows.append(dict(variable=name,group=group,category='',summary=desc,nonmissing=len(non),missing=missing))
            else:
                for level,count in non.astype('string').value_counts().items():
                    rows.append(dict(variable=name,group=group,category=str(level),summary=f'{count} ({100*count/max(len(non),1):.1f}%)',nonmissing=len(non),missing=missing))
    return pd.DataFrame(rows)

def sample_counts(df, outcome, positive, names):
    sample = df.loc[:, [outcome] + names].dropna()
    events = int((sample[outcome].astype('string').str.strip() == str(positive)).sum())
    return dict(N=len(sample), events=events, non_events=len(sample) - events)


def plan_template(manifest, binding, research):
    return dict(confirmed=False, stage='model_plan', model='binary_logistic',
                analyses=['univariable', 'multivariable'], preprocessing_fingerprint=binding,
                outcome=manifest['outcome'], positive_value=manifest['positive_value'],
                missing_strategy='complete_case', independent_observations=None,
                study_design=research.get('study_design', ''),
                covariates=manifest['covariates'],
                multivariable_covariates=[spec['name'] for spec in manifest['covariates']], notes='')


def validate_model_plan(cfg, manifest, binding):
    if cfg.get('confirmed') is not True or cfg.get('stage') != 'model_plan':
        raise ValueError('阶段4模型方案尚未获用户确认')
    if cfg.get('preprocessing_fingerprint') != binding:
        raise ValueError('数据处理或前序决定已改变，须重新确认阶段4')
    if cfg.get('model') != 'binary_logistic':
        raise ValueError('本版仅支持单因素/多因素二分类 Logistic')
    analyses = cfg.get('analyses')
    if (not isinstance(analyses, list) or not analyses or any(not isinstance(item, str) for item in analyses)
            or len(analyses) != len(set(analyses)) or not set(analyses) <= {'univariable', 'multivariable'}):
        raise ValueError('analyses 仅可选择 univariable 和/或 multivariable')
    if cfg.get('independent_observations') is not True or not str(cfg.get('study_design') or '').strip():
        raise ValueError('须确认研究设计和独立观测；匹配/重复测量等不能按普通 Logistic 执行')
    for key in ('outcome', 'positive_value', 'missing_strategy', 'covariates'):
        if cfg.get(key) != manifest[key]:
            raise ValueError(f'{key} 超出已确认处理范围；更改编码或缺失策略须返回阶段3')
    subset = cfg.get('multivariable_covariates')
    names = {spec['name'] for spec in manifest['covariates']}
    if (not isinstance(subset, list) or any(not isinstance(name, str) for name in subset)
            or len(subset) != len(set(subset)) or not set(subset) <= names):
        raise ValueError('多因素调整集只能从已核查并保留的解释变量中明确选择')
    if 'multivariable' in analyses and len(subset) < 2:
        raise ValueError('多因素分析至少需要两个解释变量；可返回阶段2选择变量或仅确认单因素分析')


def effect_lines(result, adjusted=False):
    lines = []
    for _, row in result.iterrows():
        direction = '较高' if row.OR > 1 else '较低' if row.OR < 1 else '相同'
        precision = ('95% CI 包含1，不能据此确定关联方向'
                     if row.CI_lower <= 1 <= row.CI_upper else '95% CI 未包含1，仍需结合偏倚及模型警告判断')
        lines.append(f'- {row.term}: {"aOR" if adjusted else "OR"}={row.OR:.3g} '
                     f'(95% CI {row.CI_lower:.3g}~{row.CI_upper:.3g}), P={row.p_value:.3g}；'
                     f'N={row.N}，事件={row.events}；相对参考组/所列增量，结局优势点估计{direction}；{precision}。')
    return lines


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workflow-dir', required=True, help='已完成阶段3的处理目录')
    parser.add_argument('--prepare-plan', action='store_true', help='只准备阶段4待确认方案，不拟合')
    parser.add_argument('--config', help='阶段4用户确认的正式配置')
    parser.add_argument('--input', help='可省略；提供时必须是已绑定的派生分析集')
    parser.add_argument('--outdir')
    args = parser.parse_args(argv)
    if args.prepare_plan and args.config:
        parser.error('准备方案和正式分析是两个阶段，不能同次执行')
    if not args.prepare_plan and not args.config:
        parser.error('阶段5须提供 --config，阶段4使用 --prepare-plan')
    manifest, binding, decisions, data_path = load_preprocessing(args.workflow_dir)
    if args.input and Path(args.input).resolve() != data_path.resolve():
        raise ValueError('只能使用阶段3生成且已核验指纹的分析集，不能直接输入原始表')
    df = read_table(data_path)
    df.columns = df.columns.map(str)
    outcome, positive, covars = manifest['outcome'], manifest['positive_value'], manifest['covariates']
    names = [outcome] + [spec['name'] for spec in covars]
    if len(names) != len(set(names)) or set(df.columns) != set(names):
        raise ValueError('分析集字段与已确认范围不一致')
    out = Path(args.outdir) if args.outdir else Path(args.workflow_dir) / ('' if args.prepare_plan else 'results')
    if not args.prepare_plan and out.resolve() == Path(args.workflow_dir).resolve():
        raise ValueError('正式分析请使用独立结果子目录，避免覆盖阶段3产物')
    if args.prepare_plan:
        out.mkdir(parents=True, exist_ok=True)
        save_json(out / 'config.template.json', plan_template(manifest, binding, decisions['research']))
        counts = {spec['name']: sample_counts(df, outcome, positive, [spec['name']]) for spec in covars}
        emit_summary(out, 'model_plan_summary.json', dict(stage='model_plan', phase_number=4,
                     status='awaiting_model_confirmation', model='binary_logistic',
                     analyses=['univariable', 'multivariable'],
                     univariable_samples=counts,
                     proposed_multivariable_sample=sample_counts(df, outcome, positive, [spec['name'] for spec in covars]),
                     fitting_performed=False, next_action='展示调整集、编码、预计样本数与局限，等待用户确认方案',
                     required_inputs=['config.json'], blocking_reasons=['模型方案尚未确认'],
                     artifacts={'config.template.json': str((out / 'config.template.json').resolve())}))
        return
    cfg = load_json(args.config)
    validate_model_plan(cfg, manifest, binding)
    targets = ('forest_plot.png', 'missingness.csv', 'table1.csv', 'variable_dictionary.csv',
               'univariable.csv', 'multivariable.csv', 'analysis_plan.md', 'analysis_report.md',
               'audit.json', 'analysis_summary.json')
    protected = {data_path.resolve(), Path(args.config).resolve(), Path(manifest['source_path']).resolve()}
    protected.update(Path(entry['input_path']).resolve() for entry in manifest['confirmations'].values())
    if any((out / name).resolve() in protected for name in targets):
        raise ValueError('结果输出会覆盖原始文件、分析集或确认记录，已停止')
    out.mkdir(parents=True, exist_ok=True)
    forest = out / 'forest_plot.png'
    if forest.exists():
        forest.unlink()
    missingness_table(df, outcome).to_csv(out / 'missingness.csv', index=False, encoding='utf-8-sig')
    describe(df, outcome, positive, covars).to_csv(out / 'table1.csv', index=False, encoding='utf-8-sig')
    outcome_spec = next(spec for spec in decisions['processing']['variables'] if spec['name'] == outcome)
    pd.DataFrame([dict(**spec, role='covariate') for spec in covars] +
                 [dict(**outcome_spec, role='outcome')]).to_csv(
        out / 'variable_dictionary.csv', index=False, encoding='utf-8-sig')
    uni, allflags, univariable_models = [], [], {}
    success = True
    if 'univariable' in cfg['analyses']:
        for spec in covars:
            try:
                result, meta = fit(df, outcome, [spec], positive)
                result.insert(0, 'variable', spec['name'])
                uni.append(result)
                univariable_models[spec['name']] = meta
                allflags.extend(f'univariable {spec["name"]}: {flag}' for flag in meta['flags'])
            except Exception as error:
                success = False
                univariable_models[spec['name']] = {'success': False, 'error': str(error)}
                allflags.append(f'UNIVARIABLE FAILED {spec["name"]}: {error}')
    uni_result = pd.concat(uni, ignore_index=True) if uni else pd.DataFrame(columns=['variable', 'term', 'OR', 'CI_lower', 'CI_upper', 'p_value', 'N', 'events'])
    uni_result.to_csv(out / 'univariable.csv', index=False, encoding='utf-8-sig')
    multi, meta, multi_success = pd.DataFrame(), {}, False
    if 'multivariable' in cfg['analyses']:
        chosen = [spec for spec in covars if spec['name'] in cfg['multivariable_covariates']]
        try:
            multi, meta = fit(df, outcome, chosen, positive)
            allflags.extend(meta['flags'])
            multi_success = True
            fig, ax = plt.subplots(figsize=(9, max(3, .45 * len(multi) + 1.5)))
            positions = np.arange(len(multi))
            ax.errorbar(multi['OR'], positions, xerr=[multi['OR'] - multi['CI_lower'],
                        multi['CI_upper'] - multi['OR']], fmt='o', capsize=3)
            ax.axvline(1, linestyle='--', linewidth=1, color='gray')
            ax.set_xscale('log')
            ax.set_yticks(positions, multi['term'])
            ax.invert_yaxis()
            ax.set_xlabel('Adjusted odds ratio (95% CI; log scale)')
            fig.tight_layout()
            fig.savefig(forest, dpi=180)
            plt.close(fig)
        except Exception as error:
            success = False
            allflags.append(f'MULTIVARIABLE FAILED: {error}')
    if multi.empty:
        multi = pd.DataFrame(columns=['term', 'OR', 'CI_lower', 'CI_upper', 'p_value', 'N', 'events'])
    multi.to_csv(out / 'multivariable.csv', index=False, encoding='utf-8-sig')
    research = decisions['research']
    plan = ['# Locked Logistic Analysis Plan', f'- Research question: {research["research_question"]}',
            f'- Outcome: {outcome}; positive: {positive}', f'- Study design: {cfg["study_design"]}',
            f'- Analyses: {", ".join(cfg["analyses"])}',
            f'- Univariable variables: {", ".join(spec["name"] for spec in covars)}',
            f'- Multivariable variables: {", ".join(cfg["multivariable_covariates"])}',
            '- Missing strategy: per-model complete cases; no imputation',
            f'- Preprocessing fingerprint: {binding}', '- User confirmation: true']
    (out / 'analysis_plan.md').write_text('\n'.join(plan) + '\n', encoding='utf-8')
    report = ['# 危险因素分析与结果解读（自动草稿，须结合研究背景复核）', '',
              '## 研究目标与样本', f'- 研究问题：{research["research_question"]}',
              f'- 研究对象：{research["research_population"]}',
              f'- 结局：{outcome}；定义/时间窗：{research["outcome_definition"]}；阳性值：{positive}',
              f'- 原始记录：{manifest["rows_before"]}；处理后：{len(df)}；每个模型再按自身所需变量取完整案例。',
              f'- 单因素状态：{"已执行" if "univariable" in cfg["analyses"] else "未选择"}；'
              f'多因素状态：{"成功" if multi_success else "失败" if "multivariable" in cfg["analyses"] else "未选择"}。',
              '', '## 单因素结果与方向解释', *effect_lines(uni_result),
              '', '## 多因素结果与方向解释', *effect_lines(multi, adjusted=True),
              '', '## 粗估计与调整后估计']
    for _, row in multi.iterrows():
        match = uni_result.loc[uni_result['term'] == row.term] if 'term' in uni_result else pd.DataFrame()
        if len(match):
            crude = match.iloc[0]
            report.append(f'- {row.term}：粗 OR={crude.OR:.3g} → aOR={row.OR:.3g}；'
                          f'对应 N={crude.N}/{row.N}，样本可能不同；变化不能单独证明混杂或因果。')
    report.extend(['', '## 警告与解释边界', *[f'- {flag}' for flag in allflags],
                   '- 所有效应须按变量参考组或连续变量增量解释；有分离、极端估计等警告时不得写成可靠危险因素。',
                   '- OR 为结局优势比，不是风险倍数；观察性关联不能直接解释为因果。',
                   '- 区间包含1不等于证明没有关联；应结合区间宽度、样本量和临床意义。',
                   '- 尚未自动执行多重插补、RCS、匹配/聚类校正、影响点检验或敏感性分析。',
                   '- 代理须在本阶段阅读结果和警告，结合研究目标完善临床解释、混杂/选择偏倚和诊断局限，不能只交此草稿。'])
    (out / 'analysis_report.md').write_text('\n'.join(report) + '\n', encoding='utf-8')
    save_json(out / 'audit.json', dict(success=success, stage='analysis_and_interpretation',
              phase_number=5, original_rows=manifest['rows_before'], processed_rows=len(df),
              model=meta, univariable_models=univariable_models, multivariable_success=multi_success,
              warnings=allflags, configuration=cfg, preprocessing_fingerprint=binding,
              interpretation_status='draft_requires_contextual_review'))
    emit_summary(out, 'analysis_summary.json', dict(stage='analysis_and_interpretation', phase_number=5,
                 status='results_generated_interpretation_pending' if success else 'analysis_failed_review_required',
                 success=success, next_action='读取实际结果和警告，在阶段5完成针对研究目标的解释与最终报告',
                 required_inputs=['contextual_result_interpretation'], blocking_reasons=[] if success else allflags,
                 artifacts={name: str((out / name).resolve()) for name in
                            ('analysis_report.md', 'univariable.csv', 'multivariable.csv', 'table1.csv',
                             'missingness.csv', 'variable_dictionary.csv', 'forest_plot.png', 'audit.json')
                            if (out / name).is_file()}))
    if not success:
        raise SystemExit(2)


if __name__ == '__main__':
    main()
