"""Synthetic-only evidence, nonduplicated missingness, and execution safety tests."""
import contextlib
import copy
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import preprocess_data as processing
import review_workflow as workflow
from profile_data import read_table
from quality_review import missing_review_groups, review_tables


class DetailedQualityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.frame = pd.DataFrame({
            'age': ['999', '-1', None, '24', '25', '26', '27', '28', '29', '30', '31', '32'],
            'outcome': ['0', '1'] * 6,
            'lab': ['10', '<0.1', '12mg/L', '阴性'] + ['15'] * 8,
            'unused': ['UNSELECTED_SECRET'] * 12,
            'patient_id': ['PRIVATE_SECRET'] * 12,
        })
        self.source = self.root / 'source.csv'
        self.frame.to_csv(self.source, index=False)
        self.frame = read_table(self.source)
        self.identity = workflow.source_identity(self.source)
        self.research = workflow.research_template(self.identity)
        self.research.update(status='confirmed', research_population='Synthetic population',
                             research_question='Synthetic factors', outcome='outcome',
                             outcome_definition='Synthetic binary event')
        self.selection = workflow.selection_template(workflow.selection_inventory(self.frame), self.identity, self.research)
        self.selection.update(status='confirmed', positive_value='1')
        for row in self.selection['variables']:
            row['user_selection'] = '纳入' if row['variable'] in {'age', 'lab'} else '不纳入'
        self.scope = workflow.review_scope(self.selection, list(self.frame.columns))
        self.scoped = self.frame[self.scope]
        self.issues = workflow.scan(self.scoped)
        self.template = workflow.quality_template(self.selection, self.scope, self.issues, self.scoped)
        self.review = copy.deepcopy(self.template)
        self.review['status'] = 'reviewed'
        for row in self.review['issues']:
            row.update(user_action='保留并记录', user_reason='Synthetic user opinion')
        for row in self.review['missing_decisions']:
            if row['observed_missing_n']:
                row.update(user_choice='complete_case', user_reason='Synthetic complete-case rationale')
            else:
                row['user_choice'] = 'no_missing'

    def validate(self):
        return workflow.validate_quality(self.review, self.template)

    def test_missing_table_covers_every_selected_model_variable_without_codes_counted_as_empty(self):
        rows = {row['variable']: row for row in self.template['missing_decisions']}
        self.assertEqual(set(rows), {'age', 'outcome', 'lab'})
        self.assertEqual(rows['age']['observed_missing_n'], 1)
        self.assertNotIn('suspected_missing_n', rows['age'])
        self.assertNotIn('sentinel_decisions', self.template)
        self.assertEqual(rows['age']['missing_pct'], 8.33)
        self.assertNotIn('HIGH_MISSING', set(self.issues.rule))

    def test_review_does_not_require_a_missing_code_answer(self):
        self.assertFalse(self.validate())
        self.assertNotIn('SENTINEL_VALUE', set(self.issues.rule))

    def test_literal_codes_do_not_generate_missing_decisions_or_inflate_inventory_missingness(self):
        frame = pd.DataFrame({'a': ['NA', 'null', '999'], 'b': ['999', 'NA', '2']})
        result = review_tables(frame, {'outcome': 'b'})
        self.assertNotIn('sentinel_decisions', result)
        self.assertTrue(all(row['observed_missing_n'] == 0 and row['user_choice'] == 'no_missing'
                            for row in result['missing_decisions']))
        inventory = workflow.inventory(frame, detailed=True)
        self.assertEqual(inventory['missing'].tolist(), [0, 0])
        self.assertEqual(frame['a'].tolist(), ['NA', 'null', '999'])
        self.assertEqual(set(workflow.scan(frame).rule), {'MIXED_TYPE'})

    def test_actual_missing_cannot_be_hidden_under_no_missing(self):
        next(r for r in self.review['missing_decisions'] if r['variable'] == 'age')['user_choice'] = 'no_missing'
        self.assertTrue(self.validate())

    def test_missing_decisions_must_be_complete_and_bound_to_actual_counts(self):
        self.review['missing_decisions'][0]['observed_missing_n'] = 99
        self.assertTrue(self.validate())
        self.review = copy.deepcopy(self.template)
        self.review['missing_decisions'] = []
        self.assertTrue(self.validate())

    def test_obsolete_code_decisions_and_old_review_version_are_rejected(self):
        self.review['review_version'] = '1.7'
        self.assertTrue(self.validate())
        self.review['review_version'] = self.template['review_version']
        self.review['sentinel_decisions'] = [
            dict(variable='age', code='999', is_missing='yes', affected_n=1)]
        self.assertTrue(self.validate())

    def test_high_and_all_missing_only_appear_in_missing_table(self):
        frame = pd.DataFrame({'high': [None] * 9 + ['1'], 'empty': [None] * 10, 'outcome': ['0', '1'] * 5})
        before = frame.copy(deep=True)
        issues = workflow.scan(frame)
        self.assertTrue({'HIGH_MISSING', 'ALL_MISSING', 'SENTINEL_VALUE'}.isdisjoint(issues.rule))
        rows = {row['variable']: row for row in review_tables(frame, {'outcome': 'outcome'})['missing_decisions']}
        self.assertEqual((rows['high']['observed_missing_n'], rows['high']['missing_pct']), (9, 90))
        self.assertEqual((rows['empty']['observed_missing_n'], rows['empty']['missing_pct']), (10, 100))
        self.assertEqual(rows['empty']['user_choice'], '')
        self.assertIn('CONSTANT_COLUMN', set(issues.rule))
        pd.testing.assert_frame_equal(frame, before)

    def test_missing_review_groups_cover_each_variable_once_without_changing_decisions(self):
        rows = [
            dict(variable=name, observed_missing_n=count, total_n=100, user_choice='', user_reason='')
            for name, count in [('high', 51), ('zero', 0), ('low', 29), ('mid', 30), ('half', 50), ('all', 100)]
        ]
        before = copy.deepcopy(rows)
        groups = missing_review_groups(rows)
        self.assertEqual([group['key'] for group in groups], ['below_30', 'from_30_to_50', 'above_50', 'no_missing'])
        self.assertEqual({group['key']: group['variables'] for group in groups},
                         dict(below_30=['low'], from_30_to_50=['mid', 'half'], above_50=['high', 'all'], no_missing=['zero']))
        self.assertCountEqual([name for group in groups for name in group['variables']],
                              [row['variable'] for row in rows])
        self.assertEqual(rows, before)

    def test_group_boundaries_use_counts_not_rounded_percent(self):
        rows = [
            dict(variable='below30', observed_missing_n=29999, total_n=100000, missing_pct=30.0),
            dict(variable='equal30', observed_missing_n=30000, total_n=100000, missing_pct=30.0),
            dict(variable='above30', observed_missing_n=30001, total_n=100000, missing_pct=30.0),
            dict(variable='below50', observed_missing_n=10000, total_n=20001, missing_pct=50.0),
            dict(variable='above50', observed_missing_n=10001, total_n=20001, missing_pct=50.0),
            dict(variable='equal50', observed_missing_n=1, total_n=2, missing_pct=50.0),
        ]
        self.assertEqual({group['key']: group['variables'] for group in missing_review_groups(rows)},
                         dict(below_30=['below30'], from_30_to_50=['equal30', 'above30', 'below50', 'equal50'],
                              above_50=['above50']))

    def test_only_nonzero_below_thirty_defaults_to_mi_without_authorizing_execution(self):
        frame = pd.DataFrame({
            name: [None] * count + [str(i % 2) for i in range(100 - count)]
            for name, count in [('zero', 0), ('low', 29), ('thirty', 30), ('half', 50), ('high', 51)]
        })
        before = frame.copy(deep=True)
        result = review_tables(frame, {'outcome': 'low'})
        rows = {row['variable']: row for row in result['missing_decisions']}
        self.assertEqual(rows['low']['user_choice'], 'multiple_imputation')
        self.assertTrue(rows['low']['user_reason'])
        self.assertEqual(rows['zero']['user_choice'], 'no_missing')
        for name in ['thirty', 'half', 'high']:
            self.assertEqual(rows[name]['user_choice'], '')
        self.assertEqual([row['group_key'] for row in result['missing_group_feedback']],
                         ['below_30', 'from_30_to_50', 'above_50'])
        self.assertTrue(all(row['user_text'] == row['resolved_text'] == '' for row in result['missing_group_feedback']))
        self.assertEqual(self.template['status'], 'draft')
        pd.testing.assert_frame_equal(frame, before)

    def test_free_text_must_be_resolved_before_quality_can_be_reviewed(self):
        feedback = self.review['missing_group_feedback'][0]
        feedback['user_text'] = 'age: exclude this variable instead of the default'
        self.assertTrue(any('分组意见尚待整理' in error for error in self.validate()))
        feedback['resolved_text'] = feedback['user_text']
        next(row for row in self.review['missing_decisions'] if row['variable'] == 'age').update(
            user_choice='exclude_variable', user_reason='Synthetic user explicitly requested this exception')
        self.assertFalse(self.validate())
        feedback['user_text'] += '; revised source instruction'
        self.assertTrue(self.validate())

    def test_group_feedback_cannot_be_omitted_reassigned_or_forged_as_reviewed(self):
        for change in ['omit', 'reassign', 'malformed', 'submitted']:
            with self.subTest(change=change):
                review = copy.deepcopy(self.review)
                if change == 'omit':
                    review['missing_group_feedback'] = []
                elif change == 'reassign':
                    review['missing_group_feedback'][0]['variables'] = ['unused']
                elif change == 'malformed':
                    review['missing_group_feedback'][0]['user_text'] = None
                else:
                    review['status'] = 'missing_feedback_submitted'
                self.assertTrue(workflow.validate_quality(review, self.template))

    def test_default_mi_still_blocks_the_complete_case_executor(self):
        review = copy.deepcopy(self.review)
        row = next(row for row in review['missing_decisions'] if row['variable'] == 'age')
        row.update(user_choice=self.template['missing_decisions'][0]['user_choice'],
                   user_reason=self.template['missing_decisions'][0]['user_reason'])
        self.assertEqual(row['user_choice'], 'multiple_imputation')
        plan = self.executable_plan()
        plan['quality_fingerprint'] = workflow.fingerprint(review)
        self.assertFalse(workflow.validate_quality(review, self.template))
        errors = processing.validate_processing(plan, self.selection, review, self.identity)
        self.assertTrue(any('multiple_imputation' in error for error in errors))

    def test_empty_data_is_not_grouped_as_zero_missing_and_empty_groups_are_omitted(self):
        self.assertEqual(missing_review_groups([]), [])
        groups = missing_review_groups([dict(variable='empty', observed_missing_n=0, total_n=0)])
        self.assertEqual([group['key'] for group in groups], ['unavailable'])
        empty = review_tables(pd.DataFrame({'x': []}), {'outcome': 'x'})
        self.assertEqual(empty['missing_decisions'][0]['user_choice'], '')

    def test_999_is_checked_as_numeric_extreme_instead_of_skipped(self):
        frame = pd.DataFrame({'x': list(range(20)) + [999]})
        issues = workflow.scan(frame)
        self.assertNotIn('SENTINEL_VALUE', set(issues.rule))
        details = json.loads(issues.loc[issues.rule == 'EXTREME_IQR'].iloc[0].details)
        self.assertEqual(details['numeric_n'], 21)
        self.assertEqual(details['flagged_values']['values'], [dict(value='999.0', count=1)])

    def test_mixed_values_include_forms_and_actual_tokens(self):
        row = self.issues.loc[self.issues.rule == 'MIXED_TYPE'].iloc[0]
        details = json.loads(row.details)
        self.assertEqual(details['nonnumeric_n'], 3)
        self.assertEqual(details['forms']['检测上/下限表达'], 1)
        self.assertEqual(details['forms']['数值附带单位或文字'], 1)
        self.assertEqual({r['value'] for r in details['nonnumeric_values']['values']}, {'<0.1', '12mg/L', '阴性'})

    def test_one_bad_token_among_many_numbers_is_not_missed(self):
        issues = workflow.scan(pd.DataFrame({'x': ['10'] * 100 + ['bad']}))
        self.assertIn('MIXED_TYPE', set(issues.rule))

    def test_extreme_values_include_thresholds_and_observed_range(self):
        issues = workflow.scan(pd.DataFrame({'x': list(range(20)) + [1000]}))
        details = json.loads(issues.loc[issues.rule == 'EXTREME_IQR'].iloc[0].details)
        self.assertEqual(details['max'], 1000)
        self.assertLess(details['upper_bound'], 1000)
        self.assertEqual(details['flagged_values']['values'], [dict(value='1000.0', count=1)])

    def test_private_values_and_unselected_values_are_not_in_evidence(self):
        frame = pd.DataFrame({'patient_id': ['PRIVATE_SECRET'] * 12})
        text = workflow.scan(frame, support_variables=['patient_id']).to_json(force_ascii=False)
        self.assertNotIn('PRIVATE_SECRET', text)
        self.assertNotIn('UNSELECTED_SECRET', self.issues.to_json())

    def test_nonfinite_values_do_not_break_json_evidence(self):
        issues = workflow.scan(pd.DataFrame({'x': [float('inf')] + list(range(20))}))
        self.assertIn('NONFINITE_VALUES', set(issues.rule))
        for value in issues.details:
            json.loads(value)

    def test_selecting_mi_is_recordable_but_cannot_silently_execute_complete_cases(self):
        next(r for r in self.review['missing_decisions'] if r['variable'] == 'age').update(
            user_choice='multiple_imputation', user_reason='Synthetic MAR assessment; dedicated MI required')
        self.assertFalse(self.validate())
        plan = workflow.processing_template(self.selection, self.identity)
        plan.update(status='confirmed', missing_strategy='complete_case',
                    quality_fingerprint=workflow.fingerprint(self.review))
        errors = processing.validate_processing(plan, self.selection, self.review, self.identity)
        self.assertTrue(any('multiple_imputation' in error for error in errors))

    def test_codes_are_only_converted_by_an_explicit_processing_rule(self):
        quality = dict(missing_decisions=[])
        frame = pd.DataFrame({'x': ['999', 'NA', '2']})
        operations = processing.reviewed_operations(dict(operations=[]), quality)
        preserved, log = processing.apply_operations(frame, operations, 'outcome')
        pd.testing.assert_frame_equal(preserved, frame)
        self.assertEqual(log, [])
        plan = dict(operations=[dict(action='set_missing', variable='x', values=['999'],
                                     issue_ids=[], reason='Synthetic user explicitly confirmed this code')])
        result, log = processing.apply_operations(frame, processing.reviewed_operations(plan, quality), 'outcome')
        self.assertTrue(pd.isna(result.loc[0, 'x']))
        self.assertEqual(result.loc[1, 'x'], 'NA')
        self.assertEqual(log[0]['affected_n'], 1)

    def test_excluding_variable_is_a_real_authorized_operation_not_a_label(self):
        quality = dict(missing_decisions=[
            dict(variable='x', user_choice='exclude_variable', user_reason='Not a required confounder')])
        operations = processing.reviewed_operations(dict(operations=[]), quality)
        result, _ = processing.apply_operations(pd.DataFrame({'x': [1], 'outcome': ['1']}), operations, 'outcome')
        self.assertEqual(list(result), ['outcome'])

    def test_processing_template_does_not_preselect_complete_case(self):
        self.assertEqual(workflow.processing_template(self.selection, self.identity)['missing_strategy'], '')

    def process_fixture(self, plan, review):
        paths = {}
        for key, value in [('research', self.research), ('selection', self.selection), ('quality', review), ('plan', plan)]:
            paths[key] = self.root / (key + '.json')
            workflow.save_json(paths[key], value)
        out = self.root / 'processed'
        with contextlib.redirect_stdout(io.StringIO()):
            processing.main(['--input', str(self.source), '--research-question', str(paths['research']),
                             '--selection-decisions', str(paths['selection']), '--quality-decisions', str(paths['quality']),
                             '--processing-plan', str(paths['plan']), '--outdir', str(out)])
        return out, workflow.load_json(out / 'preprocessing_manifest.json')

    def executable_plan(self):
        plan = workflow.processing_template(self.selection, self.identity)
        plan.update(status='confirmed', missing_strategy='complete_case',
                    quality_fingerprint=workflow.fingerprint(self.review),
                    variables=[dict(name='age', type='continuous', unit='years', unit_step=1),
                               dict(name='lab', type='categorical', reference='15'),
                               dict(name='outcome', type='binary', reference='0')])
        return plan

    def test_new_missing_from_other_repairs_needs_a_new_specific_missing_decision(self):
        mixed = next(row for row in self.review['issues'] if row['rule'] == 'MIXED_TYPE')
        mixed.update(user_action='设为缺失（须说明规则）', user_reason='Synthetic verified invalid text')
        plan = self.executable_plan()
        plan['variables'][1] = dict(name='lab', type='continuous', unit_step=1)
        plan['operations'] = [dict(action='set_missing', variable='lab', values=['<0.1', '12mg/L', '阴性'],
                                   issue_ids=[mixed['issue_id']], reason='Synthetic invalid text, user approved')]
        # The retained numeric lab values must vary to permit a continuous variable.
        self.frame.loc[4:, 'lab'] = [str(i) for i in range(20, 28)]
        self.frame.to_csv(self.source, index=False)
        self.identity = workflow.source_identity(self.source)
        self.research.update(**self.identity)
        self.selection.update(**self.identity, research_question_fingerprint=workflow.fingerprint(self.research))
        self.scoped = read_table(self.source)[self.scope]
        issues = workflow.scan(self.scoped)
        template = workflow.quality_template(self.selection, self.scope, issues, self.scoped)
        self.review.update(source_sha256=self.identity['source_sha256'],
                           selection_fingerprint=template['selection_fingerprint'])
        plan.update(**self.identity, selection_fingerprint=workflow.selection_fingerprint(self.selection),
                    quality_fingerprint=workflow.fingerprint(self.review))
        _, manifest = self.process_fixture(plan, self.review)
        self.assertEqual(manifest['status'], 'awaiting_postprocessing_review')
        plan['postprocessing_acceptances'] = [dict(rule='MISSING_AFTER_PROCESSING', variable='lab', reason='Not enough')]
        _, manifest = self.process_fixture(plan, self.review)
        self.assertEqual(manifest['status'], 'awaiting_postprocessing_review')
        plan['postprocessing_missing_decisions'] = [
            dict(variable='lab', missing_n=3, user_choice='complete_case', reason='Synthetic post-repair decision')]
        _, manifest = self.process_fixture(plan, self.review)
        self.assertEqual(manifest['status'], 'completed')

    def test_recovery_and_structural_choices_also_stop_the_execution_engine(self):
        for choice in ['recover_source', 'structural_review']:
            with self.subTest(choice=choice):
                next(row for row in self.review['missing_decisions'] if row['variable'] == 'age').update(
                    user_choice=choice, user_reason='Synthetic dedicated review required')
                self.assertFalse(self.validate())
                errors = processing.validate_processing(self.executable_plan(), self.selection, self.review, self.identity)
                self.assertTrue(any(choice in error for error in errors))

    def test_page_contains_concrete_reports_but_no_private_values(self):
        target = self.root / 'quality.html'
        workflow.write_html(dict(decision=self.template, issues=self.issues.to_dict('records'),
                                 missingness=workflow.missingness_table(self.scoped, 'outcome').to_dict('records'),
                                 missing_options=workflow.MISSING_OPTIONS, actions=workflow.ISSUE_ACTIONS), target)
        text = target.read_text(encoding='utf-8')
        self.assertIn('multiple_imputation', text)
        self.assertIn('"missing_groups"', text)
        self.assertIn('"below_30"', text)
        self.assertIn('"no_missing"', text)
        self.assertIn('12mg/L', text)
        self.assertNotIn('PRIVATE_SECRET', text)
        self.assertNotIn('UNSELECTED_SECRET', text)


if __name__ == '__main__':
    unittest.main()
