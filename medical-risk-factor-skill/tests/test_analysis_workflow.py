"""Synthetic-only tests for confirmed processing and the five-stage Logistic workflow."""
import contextlib
import copy
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import preprocess_data as processing
import review_workflow as workflow
import run_logistic as logistic
from profile_data import read_table


class AnalysisWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.out = self.root / 'workflow'
        self.source = self.root / 'synthetic.csv'
        rng = np.random.default_rng(84)
        age = rng.normal(50, 10, 240).round(1)
        male = rng.integers(0, 2, 240)
        probability = 1 / (1 + np.exp(-(-.6 + .03 * (age - 50) + .3 * male)))
        self.frame = pd.DataFrame({
            'age': age.astype(object), 'sex': np.where(male, 'M', 'F'),
            'outcome': (rng.random(240) < probability).astype(int),
            'unused': ['UNSELECTED_SECRET'] * 240,
            'patient_id': ['PRIVATE_SECRET'] * 240,
        })
        self.frame.loc[0, 'age'] = '999'
        self.frame.loc[1, 'age'] = '-1'
        self.frame.to_csv(self.source, index=False, encoding='utf-8-sig')
        self.identity = workflow.source_identity(self.source)
        self.research = workflow.research_template(self.identity)
        self.research.update(status='confirmed', research_population='Synthetic independent individuals',
                             research_question='Factors associated with the synthetic event',
                             outcome='outcome', outcome_definition='Event during the synthetic observation window',
                             study_design='Synthetic retrospective cohort')
        self.selection = workflow.selection_template(workflow.selection_inventory(self.frame), self.identity, self.research)
        self.selection.update(status='confirmed', positive_value='1')
        for row in self.selection['variables']:
            row['user_selection'] = '纳入' if row['variable'] in {'age', 'sex'} else '不纳入'
        self.scope = workflow.review_scope(self.selection, list(self.frame.columns))
        source_frame = read_table(self.source)
        issues = workflow.scan(source_frame[self.scope])
        self.quality = workflow.quality_template(self.selection, self.scope, issues, source_frame[self.scope])
        self.quality['status'] = 'reviewed'
        operations = []
        for issue in self.quality['issues']:
            issue.update(user_action='保留并记录', user_reason='Synthetic retained clue')
            if issue['rule'] == 'NEGATIVE_MEASUREMENT':
                issue.update(user_action='修正编码/单位（须提供映射）', user_reason='Synthetic source verified: -1 means 45')
                operations.append(dict(action='map_values', variable='age', mapping={'-1': '45'},
                                       issue_ids=[issue['issue_id']], reason='Confirmed synthetic source correction'))
        operations.append(dict(action='set_missing', variable='age', values=['999'], issue_ids=[],
                               reason='Synthetic user explicitly confirmed this age code as missing'))
        for row in self.quality['missing_decisions']:
            row.update(user_choice='complete_case', user_reason='Synthetic confirmed per-model complete cases')
        self.plan = workflow.processing_template(self.selection, self.identity)
        self.plan.update(status='confirmed', missing_strategy='complete_case', quality_fingerprint=workflow.fingerprint(self.quality),
                         operations=operations,
                         postprocessing_missing_decisions=[
                             dict(variable='age', missing_n=1, user_choice='complete_case',
                                  reason='Synthetic user reviewed the new missing value after explicit conversion')],
                         variables=[dict(name='outcome', type='binary', reference='0', unit='', unit_step=1),
                                    dict(name='age', type='continuous', reference=None, unit='years', unit_step=10),
                                    dict(name='sex', type='binary', reference='F', unit='', unit_step=1)])
        self.paths = {name: self.root / f'{name}.json' for name in ('research', 'selection', 'quality', 'plan')}
        self.save_decisions()

    def save_decisions(self):
        for key, value in (('research', self.research), ('selection', self.selection),
                           ('quality', self.quality), ('plan', self.plan)):
            workflow.save_json(self.paths[key], value)

    def process(self):
        self.save_decisions()
        with contextlib.redirect_stdout(io.StringIO()):
            processing.main(['--input', str(self.source), '--research-question', str(self.paths['research']),
                             '--selection-decisions', str(self.paths['selection']),
                             '--quality-decisions', str(self.paths['quality']),
                             '--processing-plan', str(self.paths['plan']), '--outdir', str(self.out)])
        return workflow.load_json(self.out / 'preprocessing_manifest.json')

    def confirm_model(self):
        manifest, binding, decisions, _ = processing.load_preprocessing(self.out)
        cfg = logistic.plan_template(manifest, binding, decisions['research'])
        cfg.update(confirmed=True, independent_observations=True)
        path = self.root / 'config.json'
        workflow.save_json(path, cfg)
        return cfg, path

    def test_processing_really_repairs_and_never_changes_source_or_unselected_fields(self):
        before = self.source.read_bytes()
        manifest = self.process()
        data = read_table(self.out / 'analysis_data.csv')
        self.assertEqual(manifest['status'], 'completed')
        self.assertEqual(list(data.columns), ['age', 'sex', 'outcome'])
        self.assertTrue(pd.isna(data.loc[0, 'age']))
        self.assertEqual(float(data.loc[1, 'age']), 45)
        self.assertEqual(self.source.read_bytes(), before)
        self.assertEqual(manifest['rows_after'], 240)
        self.assertNotIn('PRIVATE_SECRET', (self.out / 'processing_log.csv').read_text(encoding='utf-8-sig'))
        self.assertNotIn('UNSELECTED_SECRET', (self.out / 'analysis_data.csv').read_text(encoding='utf-8-sig'))
        before_missing = pd.read_csv(self.out / 'missingness_before.csv')
        after_missing = pd.read_csv(self.out / 'missingness_after.csv')
        self.assertEqual(int(before_missing.query("variable == 'age' and group == 'overall'").iloc[0]['missing']), 0)
        self.assertEqual(int(after_missing.query("variable == 'age' and group == 'overall'").iloc[0]['missing']), 1)

    def test_draft_processing_does_not_execute(self):
        self.plan['status'] = 'draft'
        with self.assertRaises(ValueError):
            self.process()
        self.assertFalse((self.out / 'analysis_data.csv').exists())

    def test_review_choice_without_executable_rule_is_rejected(self):
        self.plan['operations'] = []
        with self.assertRaises(ValueError):
            self.process()

    def test_pending_source_verification_cannot_be_called_completed(self):
        self.quality['issues'][0]['user_action'] = '核对原始病历/数据来源'
        self.plan['quality_fingerprint'] = workflow.fingerprint(self.quality)
        with self.assertRaises(ValueError):
            self.process()

    def test_unselected_field_cannot_drive_processing(self):
        self.plan['operations'].append(dict(action='exclude_rows', variable='unused',
                                            values=['UNSELECTED_SECRET'], issue_ids=[], reason='Invalid scope'))
        with self.assertRaises(ValueError):
            self.process()

    def test_unmatched_rule_does_not_claim_a_fix(self):
        self.plan['operations'][0]['mapping'] = {'NONEXISTENT': '45'}
        with self.assertRaises(ValueError):
            self.process()

    def test_explicit_row_exclusion_and_unit_conversion(self):
        self.plan['operations'].extend([
            dict(action='exclude_rows', variable='age', values=['45'], issue_ids=[], reason='Synthetic approved exclusion'),
            dict(action='scale', variable='age', factor=.1, issue_ids=[], reason='Synthetic approved scale'),
        ])
        manifest = self.process()
        self.assertEqual(manifest['rows_after'], 239)
        data = read_table(self.out / 'analysis_data.csv')
        self.assertLess(pd.to_numeric(data.age).max(), 10)

    def test_exclude_variable_requires_removing_its_type_item(self):
        self.plan['operations'].append(dict(action='exclude_variable', variable='sex', issue_ids=[],
                                            reason='Synthetic approved exclusion'))
        with self.assertRaises(ValueError):
            self.process()
        self.plan['variables'] = [spec for spec in self.plan['variables'] if spec['name'] != 'sex']
        manifest = self.process()
        self.assertEqual([spec['name'] for spec in manifest['covariates']], ['age'])

    def test_type_confirmation_cannot_be_inferred_or_silently_coerced(self):
        self.plan['variables'][1]['type'] = ''
        with self.assertRaises(ValueError):
            self.process()
        frame = pd.DataFrame({'age': ['42', 'bad', '51'], 'outcome': ['0', '1', '0']})
        with self.assertRaises(ValueError):
            processing.apply_types(frame, self.plan['variables'][:2], 'outcome', '1')

    def test_positive_value_must_differ_from_outcome_reference(self):
        self.plan['positive_value'] = '0'
        with self.assertRaises(ValueError):
            self.process()

    def test_residual_or_new_issue_stays_in_stage_three_until_reviewed(self):
        mapping_op = next(op for op in self.plan['operations'] if op['action'] == 'map_values')
        mapping_op['mapping'] = {'-1': '-2'}
        manifest = self.process()
        self.assertEqual(manifest['status'], 'awaiting_postprocessing_review')
        with self.assertRaises(ValueError):
            processing.load_preprocessing(self.out)
        self.plan['postprocessing_acceptances'] = [
            dict(rule='NEGATIVE_MEASUREMENT', variable='age', reason='Synthetic test-only retained clue after review'),
            dict(rule='EXTREME_IQR', variable='age', reason='Synthetic changed extreme-value evidence also reviewed'),
        ]
        manifest = self.process()
        self.assertEqual(manifest['status'], 'completed')

    def test_changed_raw_source_invalidates_model_preparation(self):
        self.process()
        changed = self.frame.copy()
        changed.loc[2, 'age'] = 33
        changed.to_csv(self.source, index=False, encoding='utf-8-sig')
        with self.assertRaises(ValueError):
            processing.load_preprocessing(self.out)

    def test_changed_user_decision_invalidates_old_processing(self):
        self.process()
        changed = copy.deepcopy(self.research)
        changed['research_question'] = 'Updated question'
        workflow.save_json(self.paths['research'], changed)
        with self.assertRaises(ValueError):
            processing.load_preprocessing(self.out)

    def test_changed_derived_dataset_is_rejected_before_fit(self):
        self.process()
        _, path = self.confirm_model()
        data = read_table(self.out / 'analysis_data.csv')
        data.loc[2, 'age'] = '1'
        data.to_csv(self.out / 'analysis_data.csv', index=False, encoding='utf-8-sig')
        with patch.object(logistic, 'fit') as fit:
            with self.assertRaises(ValueError):
                logistic.main(['--workflow-dir', str(self.out), '--config', str(path)])
            fit.assert_not_called()

    def test_prepare_model_is_a_separate_stage_and_never_fits(self):
        self.process()
        with patch.object(logistic, 'fit', side_effect=AssertionError('premature fit')):
            with contextlib.redirect_stdout(io.StringIO()):
                logistic.main(['--workflow-dir', str(self.out), '--prepare-plan'])
        cfg = workflow.load_json(self.out / 'config.template.json')
        self.assertFalse(cfg['confirmed'])
        self.assertIsNone(cfg['independent_observations'])
        self.assertFalse((self.out / 'results').exists())
        summary = workflow.load_json(self.out / 'model_plan_summary.json')
        self.assertEqual(summary['phase_number'], 4)
        self.assertEqual(summary['univariable_samples']['age']['N'], 239)
        self.assertEqual(summary['univariable_samples']['sex']['N'], 240)
        self.assertFalse(summary['fitting_performed'])

    def test_only_setting_confirmed_true_cannot_bypass_preceding_stages(self):
        path = self.root / 'bypass.json'
        workflow.save_json(path, {'confirmed': True})
        with patch.object(logistic, 'fit') as fit:
            with self.assertRaises(FileNotFoundError):
                logistic.main(['--workflow-dir', str(self.out), '--config', str(path)])
            fit.assert_not_called()

    def test_model_confirmation_rejects_drafts_stale_bindings_and_unsupported_models(self):
        manifest = self.process()
        cfg, _ = self.confirm_model()
        _, binding, _, _ = processing.load_preprocessing(self.out)
        for key, value in [('confirmed', False), ('preprocessing_fingerprint', 'stale'),
                           ('model', 'cox'), ('independent_observations', False),
                           ('missing_strategy', 'mean_imputation'), ('multivariable_covariates', ['unused']),
                           ('multivariable_covariates', ['age'])]:
            with self.subTest(key=key):
                changed = copy.deepcopy(cfg)
                changed[key] = value
                with self.assertRaises(ValueError):
                    logistic.validate_model_plan(changed, manifest, binding)

    def test_full_pipeline_generates_both_models_and_result_interpretation(self):
        self.process()
        _, path = self.confirm_model()
        with contextlib.redirect_stdout(io.StringIO()):
            logistic.main(['--workflow-dir', str(self.out), '--config', str(path)])
        results = self.out / 'results'
        audit = workflow.load_json(results / 'audit.json')
        self.assertTrue(audit['success'])
        self.assertEqual(audit['phase_number'], 5)
        self.assertEqual(audit['model']['N'], 239)
        self.assertEqual(audit['univariable_models']['age']['N'], 239)
        self.assertEqual(audit['univariable_models']['sex']['N'], 240)
        self.assertGreater((results / 'forest_plot.png').stat().st_size, 1000)
        report = (results / 'analysis_report.md').read_text(encoding='utf-8')
        self.assertIn(self.research['research_question'], report)
        self.assertIn('aOR=', report)
        self.assertIn('粗 OR=', report)
        self.assertIn('age [per 10 years]', report)
        dictionary = pd.read_csv(results / 'variable_dictionary.csv')
        self.assertEqual(set(dictionary['role']), {'outcome', 'covariate'})
        self.assertEqual(audit['interpretation_status'], 'draft_requires_contextual_review')
        self.assertEqual(workflow.load_json(results / 'analysis_summary.json')['status'],
                         'results_generated_interpretation_pending')

    def test_only_univariable_is_a_supported_confirmed_choice(self):
        self.process()
        cfg, path = self.confirm_model()
        cfg['analyses'] = ['univariable']
        workflow.save_json(path, cfg)
        with contextlib.redirect_stdout(io.StringIO()):
            logistic.main(['--workflow-dir', str(self.out), '--config', str(path)])
        audit = workflow.load_json(self.out / 'results' / 'audit.json')
        self.assertTrue(audit['success'])
        self.assertFalse(audit['multivariable_success'])
        self.assertFalse((self.out / 'results' / 'forest_plot.png').exists())

    def test_literal_special_codes_are_preserved_until_explicit_processing(self):
        source = self.root / 'literal.csv'
        pd.DataFrame({'code': ['NA', 'null', '999', None]}).to_csv(source, index=False)
        read = read_table(source)
        self.assertEqual(read['code'].iloc[:3].tolist(), ['NA', 'null', '999'])
        self.assertTrue(pd.isna(read['code'].iloc[3]))
        issues = workflow.scan(read)
        self.assertNotIn('SENTINEL_VALUE', set(issues.rule))
        self.assertIn('MIXED_TYPE', set(issues.rule))
        self.assertEqual(int(workflow.missingness_table(read, 'code').query("group == 'overall'").iloc[0]['missing']), 1)

    def test_continuous_effect_matches_confirmed_comparison_scale(self):
        frame = self.frame[['age', 'outcome']].iloc[2:].copy()
        per_year, _ = logistic.fit(frame, 'outcome', [dict(name='age', type='continuous', unit_step=1)], '1')
        per_decade, _ = logistic.fit(frame, 'outcome', [dict(name='age', type='continuous', unit_step=10)], '1')
        self.assertAlmostEqual(per_decade.iloc[0].beta, 10 * per_year.iloc[0].beta, places=8)
        self.assertIn('per 10 units', per_decade.iloc[0].term)

    def test_perfect_separation_is_not_reported_as_reliable_success(self):
        frame = pd.DataFrame({'outcome': ['0', '1'] * 40, 'x': [0, 1] * 40})
        with self.assertRaises(ValueError):
            logistic.fit(frame, 'outcome', [dict(name='x', type='continuous')], '1')


if __name__ == '__main__':
    unittest.main()
