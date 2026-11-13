"""Synthetic-only regression tests for selection-before-QA and scoped review."""
import contextlib
import copy
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import profile_data
import review_workflow as workflow


class ReviewWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'synthetic.csv'
        self.out = self.root / 'review'
        self.frame = pd.DataFrame({
            '年龄': [-1] + list(range(21, 32)),
            '结局': [0, 1] * 6,
            '未选异常列': ['UNSELECTED_SECRET', '999'] * 6,
            '患者编号': ['PRIVATE_ALPHA'] * 6 + ['PRIVATE_BETA'] * 6,
        })
        self.frame.to_csv(self.source, index=False, encoding='utf-8-sig')
        self.identity = workflow.source_identity(self.source)
        self.columns = list(self.frame.columns)
        self.research = workflow.research_template(self.identity)
        self.research.update(status='confirmed', research_population='合成研究对象',
                             research_question='合成结局的相关因素', outcome='结局',
                             outcome_definition='合成二分类结局，观察期内发生记为1')
        self.research_path = self.root / 'research.json'
        workflow.save_json(self.research_path, self.research)
        self.selection = workflow.selection_template(workflow.selection_inventory(self.frame), self.identity, self.research)
        self.selection['status'] = 'confirmed'
        self.selection['outcome'] = '结局'
        for row in self.selection['variables']:
            row['user_selection'] = '纳入' if row['variable'] == '年龄' else '不纳入'
        self.selection_path = self.root / 'selection.json'
        workflow.save_json(self.selection_path, self.selection)

    def run_cli(self, *args, with_research=True):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            research_args = ['--research-question', str(self.research_path)] if with_research else []
            workflow.main(['--input', str(self.source), '--outdir', str(self.out), *research_args, *args])

    def test_first_stage_asks_target_and_stops_before_variable_selection(self):
        with patch.object(workflow, 'scan', side_effect=AssertionError('premature scan')):
            self.run_cli(with_research=False)
        summary = workflow.load_json(self.out / 'research_summary.json')
        self.assertEqual(summary['phase_number'], 1)
        self.assertEqual(summary['status'], 'awaiting_research_question')
        self.assertIn('研究对象', summary['question'])
        self.assertFalse((self.out / 'interactive_review.html').exists())
        self.assertFalse((self.out / 'variable_selection_decisions.template.json').exists())
        template = workflow.load_json(self.out / 'research_question.template.json')
        self.assertEqual(template['status'], 'draft')
        self.assertEqual(template['outcome'], '')

    def test_selection_cannot_skip_target_question(self):
        with patch.object(workflow, 'scan') as scanner:
            with self.assertRaises(SystemExit):
                self.run_cli('--selection-decisions', str(self.selection_path), with_research=False)
            scanner.assert_not_called()

    def test_legacy_cli_also_stops_at_target_question(self):
        with contextlib.redirect_stdout(io.StringIO()):
            profile_data.main(['--input', str(self.source), '--outdir', str(self.out)])
        self.assertEqual(workflow.load_json(self.out / 'research_summary.json')['phase_number'], 1)
        self.assertFalse((self.out / 'interactive_review.html').exists())

    def test_unconfirmed_or_changed_target_blocks_progression(self):
        draft = copy.deepcopy(self.research)
        draft['status'] = 'draft'
        self.assertTrue(workflow.validate_research_question(draft, self.columns, self.identity))
        changed = copy.deepcopy(self.research)
        changed['research_question'] = '用户修改了研究问题'
        self.assertTrue(workflow.validate_selection(self.selection, self.columns, self.identity, changed))
        changed = copy.deepcopy(self.selection)
        changed['outcome'] = '年龄'
        self.assertTrue(workflow.validate_selection(changed, self.columns, self.identity, self.research))

    def stage_two(self):
        self.run_cli('--selection-decisions', str(self.selection_path))
        return workflow.load_json(self.out / 'clinical_review_decisions.template.json')

    def completed_quality(self):
        quality = self.stage_two()
        quality['status'] = 'reviewed'
        for row in quality['issues']:
            row['user_action'] = '保留并记录'
        return quality

    def test_initial_phase_does_not_scan_or_inspect_values(self):
        with patch.object(workflow, 'scan', side_effect=AssertionError('premature scan')):
            with patch.object(pd.Series, 'value_counts', side_effect=AssertionError('premature frequency')):
                with patch.object(pd.DataFrame, 'duplicated', side_effect=AssertionError('premature dedup')):
                    self.run_cli()
        self.assertFalse((self.out / 'data_quality_issues.csv').exists())
        self.assertFalse((self.out / 'interactive_data_quality.html').exists())
        summary = workflow.load_json(self.out / 'selection_summary.json')
        self.assertFalse(summary['data_quality_scan_performed'])
        template = workflow.load_json(self.out / 'variable_selection_decisions.template.json')
        self.assertEqual(template['status'], 'draft')
        self.assertEqual([row['variable'] for row in template['variables']], self.columns)
        self.assertTrue(all(row['user_selection'] == '' for row in template['variables']))
        self.assertTrue(all(set(row) == {'variable', 'user_selection', 'user_note'}
                            for row in template['variables']))
        self.assertTrue(all(key in template['variables'][0] for key in ('variable', 'user_selection', 'user_note')))
        page = (self.out / 'interactive_review.html').read_text(encoding='utf-8')
        self.assertNotIn('PRIVATE_ALPHA', page)
        self.assertNotIn('UNSELECTED_SECRET', page)

    def test_decision_json_is_compact_but_display_csv_keeps_recommendations(self):
        self.run_cli()
        template = workflow.load_json(self.out / 'variable_selection_decisions.template.json')
        self.assertNotIn('ai_reason', template['variables'][0])
        inventory = pd.read_csv(self.out / 'variable_inventory.csv')
        self.assertIn('ai_reason', inventory.columns)
        self.assertIn('suggested_role', inventory.columns)

    def test_legacy_profile_is_also_metadata_only(self):
        with patch.object(pd.DataFrame, 'duplicated', side_effect=AssertionError('premature dedup')):
            with patch.object(pd.Series, 'value_counts', side_effect=AssertionError('premature frequency')):
                basic = profile_data.inventory(self.frame)
        self.assertTrue(basic['missing_pct'].isna().all())
        self.assertTrue((basic['example_frequency'] == '').all())

    def test_quality_validation_without_selection_is_rejected_before_scan(self):
        with patch.object(workflow, 'scan') as scanner:
            with self.assertRaises(SystemExit):
                self.run_cli('--validate-decisions', str(self.root / 'missing.json'))
            scanner.assert_not_called()

    def test_invalid_selections_are_rejected_before_scan(self):
        variants = []
        draft = copy.deepcopy(self.selection)
        draft['status'] = 'draft'
        variants.append(draft)
        omitted = copy.deepcopy(self.selection)
        omitted['variables'].pop()
        variants.append(omitted)
        blank = copy.deepcopy(self.selection)
        blank['variables'][0]['user_selection'] = ''
        variants.append(blank)
        stale = copy.deepcopy(self.selection)
        stale['source_sha256'] = 'old'
        variants.append(stale)
        malformed = copy.deepcopy(self.selection)
        malformed['variables'] = [1]
        variants.append(malformed)
        for variant in variants:
            with self.subTest(variant=variant):
                workflow.save_json(self.selection_path, variant)
                with patch.object(workflow, 'scan') as scanner:
                    with self.assertRaises(SystemExit):
                        self.run_cli('--selection-decisions', str(self.selection_path))
                    scanner.assert_not_called()

    def test_scan_is_limited_to_predictors_and_outcome(self):
        before = self.source.read_bytes()
        with patch.object(workflow, 'scan', wraps=workflow.scan) as scanner:
            quality = self.stage_two()
        self.assertEqual(scanner.call_args.args[0].columns.tolist(), ['年龄', '结局'])
        self.assertEqual(quality['scope_variables'], ['年龄', '结局'])
        self.assertTrue(all(row['variable'] in {'年龄', '结局'} for row in quality['issues']))
        self.assertIn('NEGATIVE_MEASUREMENT', {row['rule'] for row in quality['issues']})
        summary = workflow.load_json(self.out / 'data_quality_summary.json')
        self.assertEqual(summary['excluded_not_reviewed'], ['未选异常列', '患者编号'])
        details = pd.read_csv(self.out / 'selected_variable_inventory.csv')
        self.assertEqual(details['review_role'].tolist(), ['predictor', 'outcome'])
        self.assertEqual(details['user_selection'].tolist(), ['纳入', '不纳入'])
        self.assertEqual(self.source.read_bytes(), before)
        self.assertNotIn('PRIVATE_ALPHA', (self.out / 'interactive_data_quality.html').read_text(encoding='utf-8'))
        pd.testing.assert_frame_equal(self.frame, pd.DataFrame({
            '年龄': [-1] + list(range(21, 32)), '结局': [0, 1] * 6,
            '未选异常列': ['UNSELECTED_SECRET', '999'] * 6,
            '患者编号': ['PRIVATE_ALPHA'] * 6 + ['PRIVATE_BETA'] * 6,
        }))

    def test_auxiliary_identifiers_require_explicit_authorization_and_purpose(self):
        dec = copy.deepcopy(self.selection)
        dec['support_variables'] = ['患者编号']
        self.assertTrue(workflow.validate_selection(dec, self.columns, self.identity))
        dec['support_purpose'] = '仅在本地核对重复患者'
        self.assertEqual(workflow.validate_selection(dec, self.columns, self.identity), [])
        workflow.save_json(self.selection_path, dec)
        quality = self.stage_two()
        self.assertIn('患者编号', quality['scope_variables'])
        repeated = [q for q in quality['issues'] if q['rule'] == 'REPEATED_IDENTIFIER']
        self.assertEqual(repeated[0]['affected_n'], 12)
        detail = pd.read_csv(self.out / 'selected_variable_inventory.csv').fillna('')
        identifier = detail.loc[detail.variable == '患者编号'].iloc[0]
        self.assertEqual(identifier['review_role'], 'support_only')
        self.assertEqual(identifier['example_frequency'], '')

    def test_identifier_and_outcome_cannot_be_predictors(self):
        for name in ('患者编号', '结局'):
            dec = copy.deepcopy(self.selection)
            next(row for row in dec['variables'] if row['variable'] == name)['user_selection'] = '纳入'
            self.assertTrue(workflow.validate_selection(dec, self.columns, self.identity))
        dec['outcome'] = '患者编号'
        self.assertTrue(workflow.validate_selection(dec, self.columns, self.identity))

    def test_empty_issue_list_can_be_reviewed_without_false_patient_dedup(self):
        frame = pd.DataFrame({'暴露': [1, 2] * 6, '结局': [0, 1] * 6})
        issues = workflow.scan(frame)
        self.assertTrue(issues.empty)
        dec = copy.deepcopy(self.selection)
        template = workflow.quality_template(dec, list(frame.columns), issues)
        reviewed = copy.deepcopy(template)
        reviewed['status'] = 'reviewed'
        self.assertEqual(workflow.validate_quality(reviewed, template), [])

    def test_changed_selection_invalidates_previous_quality_review(self):
        quality = self.completed_quality()
        dec = copy.deepcopy(self.selection)
        dec['variables'][2]['user_selection'] = '纳入'
        scope = workflow.review_scope(dec, self.columns)
        new_template = workflow.quality_template(dec, scope, workflow.scan(self.frame[scope]))
        self.assertTrue(workflow.validate_quality(quality, new_template))

    def test_incomplete_or_tampered_quality_review_fails(self):
        quality = self.completed_quality()
        template = workflow.load_json(self.out / 'clinical_review_decisions.template.json')
        self.assertEqual(workflow.validate_quality(quality, template), [])
        for key, value in [('rule', 'INVENTED'), ('affected_n', 999), ('variable', '未选异常列'),
                           ('user_action', '待确认')]:
            tampered = copy.deepcopy(quality)
            tampered['issues'][0][key] = value
            self.assertTrue(workflow.validate_quality(tampered, template))
        duplicate = copy.deepcopy(quality)
        duplicate['issues'].append(duplicate['issues'][0])
        self.assertTrue(workflow.validate_quality(duplicate, template))
        omitted = copy.deepcopy(quality)
        omitted['issues'] = []
        self.assertTrue(workflow.validate_quality(omitted, template))
        for action in workflow.REASON_ACTIONS:
            changed = copy.deepcopy(quality)
            changed['issues'][0]['user_action'] = action
            self.assertTrue(workflow.validate_quality(changed, template))
            changed['issues'][0]['user_reason'] = '合成测试规则，未实际改值'
            self.assertEqual(workflow.validate_quality(changed, template), [])

    def test_end_to_end_validation_does_not_authorize_formal_analysis(self):
        quality = self.completed_quality()
        quality_path = self.root / 'quality.json'
        workflow.save_json(quality_path, quality)
        self.run_cli('--selection-decisions', str(self.selection_path), '--validate-decisions', str(quality_path))
        result = workflow.load_json(self.out / 'review_validation.json')
        self.assertTrue(result['valid'])
        self.assertFalse(result['formal_analysis_authorized'])
        self.assertEqual(result['status'], 'awaiting_preprocessing')

    def test_excel_source_identity_uses_actual_sheet_and_requires_multisheet_choice(self):
        workbook = self.root / 'synthetic.xlsx'
        with pd.ExcelWriter(workbook) as writer:
            self.frame.to_excel(writer, sheet_name='Clinical', index=False)
        self.assertEqual(workflow.source_identity(workbook)['sheet'], 'Clinical')
        with contextlib.redirect_stdout(io.StringIO()):
            workflow.main(['--input', str(workbook), '--outdir', str(self.out)])
        goal = workflow.load_json(self.out / 'research_question.template.json')
        goal.update(status='confirmed', research_population='合成研究对象',
                    research_question='合成结局相关因素', outcome='结局', outcome_definition='合成观察期结局')
        workflow.save_json(self.research_path, goal)
        with contextlib.redirect_stdout(io.StringIO()):
            workflow.main(['--input', str(workbook), '--outdir', str(self.out),
                           '--research-question', str(self.research_path)])
        dec = workflow.load_json(self.out / 'variable_selection_decisions.template.json')
        self.assertEqual(len(dec['variables']), len(self.columns))
        with pd.ExcelWriter(workbook) as writer:
            self.frame.to_excel(writer, sheet_name='Clinical', index=False)
            self.frame.to_excel(writer, sheet_name='Other', index=False)
        with self.assertRaises(ValueError):
            workflow.source_identity(workbook)
        self.assertEqual(workflow.source_identity(workbook, '1')['sheet'], 'Other')
        self.assertTrue(workflow.validate_selection(self.selection, self.columns, workflow.source_identity(workbook, 'Clinical')))

    def test_rare_and_date_frequency_examples_are_suppressed(self):
        frame = pd.DataFrame({'类别': ['常见'] * 6 + ['RARE_SECRET'],
                              '入院日期': ['2030-01-01'] * 7})
        result = profile_data.inventory(frame, detailed=True)
        self.assertNotIn('RARE_SECRET', result.loc[0, 'example_frequency'])
        self.assertEqual(result.loc[1, 'example_frequency'], '')

    def test_embedded_json_cannot_terminate_script(self):
        target = self.root / 'safe.html'
        payload = {'decision': {'notes': '</script><script>alert(1)</script>'}}
        workflow.write_html(payload, target)
        page = target.read_text(encoding='utf-8')
        self.assertNotIn('</script><script>alert(1)</script>', page)
        self.assertIn('\\u003c/script\\u003e', page)


if __name__ == '__main__':
    unittest.main()
