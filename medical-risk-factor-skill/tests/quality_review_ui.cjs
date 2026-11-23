// Synthetic-only offline browser and backend validation for the v1.7.2 quality review.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { execFileSync } = require('node:child_process');
const { pathToFileURL } = require('node:url');
const { chromium } = require('playwright');

const python = process.argv[2];
if (!python) throw new Error('Usage: node tests/quality_review_ui.cjs <python-path>');
const root = process.argv[3] ? path.resolve(process.argv[3]) : fs.mkdtempSync(path.join(os.tmpdir(), 'medical-review-v17-'));
fs.mkdirSync(root, { recursive: true });
const source = path.join(root, 'synthetic.csv'), output = path.join(root, 'review');
const cli = path.resolve(__dirname, '../scripts/review_workflow.py');
const csv = ['age,lab,sparse,half,light,outcome,unused,patient_id',
  ...Array.from({ length: 24 }, (_, i) => {
    const age = i === 0 ? '999' : i === 1 ? '-1' : i === 2 ? '' : i === 3 ? '1000' : String(20 + i);
    const lab = i === 0 ? '<0.1' : i === 1 ? '12mg/L' : String(15 + i);
    const sparse = i < 4 ? String(10 + i) : '', half = i < 12 ? String(i + 1) : '';
    const light = i < 3 ? '' : String(100 + i), outcome = i === 0 ? '' : String(i % 2);
    return `${age},${lab},${sparse},${half},${light},${outcome},UNSELECTED_SECRET,PRIVATE_PERSON_${i}`;
  })].join('\n');
fs.writeFileSync(source, '\ufeff' + csv);
const run = (...args) => execFileSync(python, ['-X', 'utf8', cli, '--input', source, '--outdir', output, ...args],
  { encoding: 'utf8', timeout: 30000, windowsHide: true });
run();
const goalPath = path.join(root, 'research.json');
const goal = JSON.parse(fs.readFileSync(path.join(output, 'research_question.template.json'), 'utf8'));
Object.assign(goal, { status: 'confirmed', research_population: 'Synthetic independent individuals',
  research_question: 'Synthetic factors', outcome: 'outcome', outcome_definition: 'Synthetic binary event' });
fs.writeFileSync(goalPath, JSON.stringify(goal));
const withGoal = (...args) => run('--research-question', goalPath, ...args);
withGoal();

(async () => {
  const browser = await chromium.launch({ channel: process.env.PLAYWRIGHT_CHANNEL || 'msedge', headless: true, timeout: 30000 });
  const errors = [], remoteRequests = [];
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 1000 }, acceptDownloads: true });
    page.setDefaultTimeout(10000);
    page.on('pageerror', error => errors.push(error.message));
    page.on('request', request => { if (/^https?:/.test(request.url())) remoteRequests.push(request.url()); });
    await page.goto(pathToFileURL(path.join(output, 'interactive_review.html')).href);
    assert.equal(await page.locator('#outcome').isDisabled(), true);
    assert.equal(await page.locator('input[type=radio]:checked').count(), 0);
    await page.locator('#confirm').click();
    assert.match(await page.locator('#status').innerText(), /未选择/);
    for (let i = 0; i < 8; i++) {
      await page.locator(`input[name=pick_${i}][value="${i < 5 ? '纳入' : '不纳入'}"]`).check();
    }
    const selectionPath = path.join(root, 'variable_selection_decisions.json');
    let downloaded = page.waitForEvent('download');
    await page.locator('#confirm').click();
    await (await downloaded).saveAs(selectionPath);
    withGoal('--selection-decisions', selectionPath);
    await page.goto(pathToFileURL(path.join(output, 'interactive_data_quality.html')).href);
    assert.equal(await page.locator('#missing .missing-row').count(), 6);
    assert.deepEqual(await page.locator('[data-missing-group]').evaluateAll(elements =>
      elements.map(el => el.dataset.missingGroup)), ['below_30', 'from_30_to_50', 'above_50', 'no_missing']);
    assert.equal(await page.locator('[data-missing-group=below_30] .missing-row').count(), 3);
    assert.equal(await page.locator('[data-missing-group=from_30_to_50] .missing-row').count(), 1);
    assert.equal(await page.locator('[data-missing-group=above_50] .missing-row').count(), 1);
    assert.equal(await page.locator('[data-missing-group=no_missing] .missing-row').count(), 1);
    assert.equal(await page.locator('#missing select, #missing button, #missing table').count(), 0);
    assert.equal(await page.locator('#missing textarea').count(), 3);
    for (const key of ['below_30', 'from_30_to_50', 'above_50']) {
      const group = page.locator(`[data-missing-group=${key}]`);
      assert.equal(await group.locator('textarea').count(), 1);
      assert.equal(await group.evaluate(el => Boolean(
        el.querySelector('.missing-list').compareDocumentPosition(el.querySelector('textarea')) & Node.DOCUMENT_POSITION_FOLLOWING)), true);
    }
    assert.equal(await page.locator('[data-missing-group=no_missing] textarea').count(), 0);
    assert.match(await page.locator('[data-missing-group=below_30] h3').innerText(), /默认多重插补/);
    assert.deepEqual(await page.locator('#missing .missing-row').allTextContents(), [
      'age / 缺失比例 / 4.17%', 'light / 缺失比例 / 12.5%', 'outcome / 缺失比例 / 4.17%',
      'half / 缺失比例 / 50%', 'sparse / 缺失比例 / 83.33%', 'lab / 缺失比例 / 0%',
    ]);
    assert.match(await page.locator('[data-missing-group=from_30_to_50]').innerText(), /50%/);
    assert.equal(await page.locator('#sentinels').count(), 0);
    assert.equal((await page.locator('#quality').innerText()).includes('疑似'), false);
    assert.equal((await page.locator('#issues').innerText()).includes('实际空值占'), false);
    assert.doesNotMatch(await page.locator('#missing .missing-list').allTextContents().then(rows => rows.join('\n')),
      /实际空值|解释变量|outcome=|当前方案|删除缺失行/);
    assert.match(await page.locator('#issues').innerText(), /999/);
    assert.match(await page.locator('#issues').innerText(), /12mg\/L/);
    assert.match(await page.locator('#issues').innerText(), /Q1=/);
    assert.equal((await page.content()).includes('PRIVATE_PERSON_'), false);
    assert.equal((await page.content()).includes('UNSELECTED_SECRET'), false);
    await page.locator('#confirm').click();
    assert.match(await page.locator('#status').innerText(), /未决定/);
    await page.locator('#submit-missing').click();
    assert.match(await page.locator('#missing-status').innerText(), /30%至50%.*请填写本组处理意见/);
    assert.match(await page.locator('#missing-status').innerText(), /大于50%.*请填写本组处理意见/);
    const texts = {
      below_30: 'age：改用逐模型完整案例。其余变量沿用默认方案。\nSynthetic exception, pending confirmation.',
      from_30_to_50: 'half：逐模型完整案例；需要确认样本损失。',
      above_50: 'sparse：不纳入解释变量；Synthetic clinical assessment required.',
    };
    await page.locator('#group_feedback_from_30_to_50').fill(texts.from_30_to_50);
    await page.locator('#group_feedback_above_50').fill(texts.above_50);
    const feedbackPath = path.join(root, 'clinical_review_feedback.json');
    downloaded = page.waitForEvent('download');
    await page.locator('#submit-missing').click();
    await (await downloaded).saveAs(feedbackPath);
    let feedback = JSON.parse(fs.readFileSync(feedbackPath, 'utf8'));
    assert.equal(feedback.status, 'missing_feedback_submitted');
    assert.equal(feedback.missing_group_feedback[0].user_text, '');
    assert.equal(feedback.issues.every(row => row.user_action === '待确认'), true);
    await page.locator('#group_feedback_below_30').fill(texts.below_30);
    downloaded = page.waitForEvent('download');
    await page.locator('#submit-missing').click();
    const feedbackDownload = await downloaded;
    assert.equal(feedbackDownload.suggestedFilename(), 'clinical_review_feedback.json');
    await feedbackDownload.saveAs(feedbackPath);
    feedback = JSON.parse(fs.readFileSync(feedbackPath, 'utf8'));
    assert.equal(feedback.review_version, '1.7.2');
    assert.equal(feedback.statistical_plan_confirmed, false);
    for (const row of feedback.missing_group_feedback) {
      assert.equal(row.user_text, texts[row.group_key]);
      assert.equal(row.resolved_text, '');
    }
    for (const variable of ['age', 'light', 'outcome']) {
      assert.equal(feedback.missing_decisions.find(row => row.variable === variable).user_choice, 'multiple_imputation');
    }
    for (const variable of ['half', 'sparse']) {
      assert.equal(feedback.missing_decisions.find(row => row.variable === variable).user_choice, '');
    }
    assert.equal(feedback.missing_decisions.find(row => row.variable === 'lab').user_choice, 'no_missing');
    assert.match(await page.locator('#missing-status').innerText(), /未插补、未改值、未进入模型/);
    for (const select of await page.locator('#issues select').all()) await select.selectOption('保留并记录');
    for (const textarea of await page.locator('#issues textarea').all()) await textarea.fill('Synthetic user-reviewed retention; not a clinical decision.');
    await page.locator('#confirm').click();
    assert.match(await page.locator('#status').innerText(), /分组意见尚待整理/);
    const draftPath = path.join(root, 'clinical_review_draft.json');
    downloaded = page.waitForEvent('download');
    await page.locator('#draft').click();
    await (await downloaded).saveAs(draftPath);
    const draft = JSON.parse(fs.readFileSync(draftPath, 'utf8'));
    assert.equal(draft.status, 'draft');
    assert.deepEqual(draft.missing_group_feedback, feedback.missing_group_feedback);

    const invalid = qualityPath => {
      assert.throws(() => withGoal('--selection-decisions', selectionPath, '--validate-decisions', qualityPath),
        error => error.status === 2);
      const result = JSON.parse(fs.readFileSync(path.join(output, 'review_validation.json'), 'utf8'));
      assert.equal(result.valid, false);
      assert.equal(result.formal_analysis_authorized, false);
      assert.ok(result.errors.some(error => error.includes('分组意见尚待整理')));
    };
    invalid(feedbackPath);
    draft.status = 'reviewed';
    const unresolvedPath = path.join(root, 'unresolved_review.json');
    fs.writeFileSync(unresolvedPath, JSON.stringify(draft));
    invalid(unresolvedPath);

    // Simulate a separate, explicitly confirmed agent resolution on synthetic data.
    const resolved = JSON.parse(JSON.stringify(draft));
    for (const row of resolved.missing_group_feedback) row.resolved_text = row.user_text;
    const age = resolved.missing_decisions.find(row => row.variable === 'age');
    Object.assign(age, { user_choice: 'complete_case', user_reason: 'Synthetic confirmed age exception.' });
    Object.assign(resolved.missing_decisions.find(row => row.variable === 'half'), {
      user_choice: 'complete_case', user_reason: 'Synthetic confirmed complete-case decision with sample-loss review.',
    });
    Object.assign(resolved.missing_decisions.find(row => row.variable === 'sparse'), {
      user_choice: 'exclude_variable', user_reason: 'Synthetic confirmed exclusion, not a threshold-only rule.',
    });
    const resolvedPath = path.join(root, 'resolved_review.json'), resolvedPage = path.join(root, 'resolved_review.html');
    fs.writeFileSync(resolvedPath, JSON.stringify(resolved));
    execFileSync(python, ['-X', 'utf8', '-c',
      'import sys; sys.path.insert(0,sys.argv[1]); import review_workflow as w; r=w.load_json(sys.argv[2]); ' +
      'w.write_html(dict(decision=r,issues=w.pd.read_csv(sys.argv[5]).astype(object).fillna("").to_dict("records"),' +
      'missingness=w.pd.read_csv(sys.argv[3]).to_dict("records"),' +
      'missing_options=w.MISSING_OPTIONS,actions=w.ISSUE_ACTIONS),sys.argv[4])',
      path.resolve(__dirname, '../scripts'), resolvedPath, path.join(output, 'missingness_before.csv'), resolvedPage,
      path.join(output, 'data_quality_issues.csv')],
      { encoding: 'utf8', timeout: 30000, windowsHide: true });
    await page.goto(pathToFileURL(resolvedPage).href);
    assert.match(await page.locator('#issues').innerText(), /Q1=/);
    assert.match(await page.locator('#issues').innerText(), /12mg\/L/);
    assert.equal(await page.locator('#issues textarea').first().inputValue(),
      'Synthetic user-reviewed retention; not a clinical decision.');
    for (const key of Object.keys(texts)) assert.equal(await page.locator(`#group_feedback_${key}`).inputValue(), texts[key]);
    await page.locator('#group_feedback_below_30').fill(texts.below_30 + '\nChanged instruction');
    await page.locator('#confirm').click();
    assert.match(await page.locator('#status').innerText(), /分组意见尚待整理/);
    await page.locator('#group_feedback_below_30').fill(texts.below_30);
    const qualityPath = path.join(root, 'clinical_review_decisions.json');
    downloaded = page.waitForEvent('download');
    await page.locator('#confirm').click();
    await (await downloaded).saveAs(qualityPath);
    const quality = JSON.parse(fs.readFileSync(qualityPath, 'utf8'));
    assert.equal(quality.review_version, '1.7.2');
    assert.equal(quality.status, 'reviewed');
    assert.equal('sentinel_decisions' in quality, false);
    assert.equal(quality.missing_decisions[0].observed_missing_n, 1);
    assert.equal(quality.missing_decisions.find(row => row.variable === 'age').user_choice, 'complete_case');
    assert.equal(quality.missing_decisions.find(row => row.variable === 'light').user_choice, 'multiple_imputation');
    assert.equal(quality.missing_decisions.find(row => row.variable === 'lab').user_choice, 'no_missing');
    assert.equal(quality.statistical_plan_confirmed, false);
    assert.equal(quality.issues.some(r => ['HIGH_MISSING', 'ALL_MISSING', 'SENTINEL_VALUE'].includes(r.rule)), false);
    let validation = JSON.parse(withGoal('--selection-decisions', selectionPath, '--validate-decisions', qualityPath));
    assert.equal(validation.valid, true);
    assert.equal(validation.status, 'awaiting_specialized_missing_plan');
    assert.equal(validation.formal_analysis_authorized, false);
    for (const row of quality.missing_decisions.filter(row => row.user_choice === 'multiple_imputation')) {
      Object.assign(row, { user_choice: 'complete_case', user_reason: 'Separate synthetic confirmed decision, not an MI fallback.' });
    }
    fs.writeFileSync(qualityPath, JSON.stringify(quality));
    validation = JSON.parse(withGoal('--selection-decisions', selectionPath, '--validate-decisions', qualityPath));
    assert.equal(validation.valid, true);
    assert.equal(validation.status, 'awaiting_preprocessing');
    assert.equal(validation.formal_analysis_authorized, false);
    for (const [label, width, height] of [['desktop', 1440, 1000], ['mobile', 390, 844]]) {
      await page.setViewportSize({ width, height });
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
      const clipped = await page.locator('button, h1, h2, h3').evaluateAll(elements =>
        elements.filter(el => el.clientWidth && el.scrollWidth > el.clientWidth + 1).map(el => el.textContent));
      assert.deepEqual(clipped, []);
      await page.screenshot({ path: path.join(root, `quality-${label}.png`), fullPage: true });
    }
    assert.equal(fs.readFileSync(source, 'utf8'), '\ufeff' + csv);
    assert.deepEqual(errors, []);
    assert.deepEqual(remoteRequests, []);
    console.log(JSON.stringify({ passed: true, screenshots: root, review: path.join(output, 'interactive_data_quality.html'),
      pageErrors: errors, remoteRequests }, null, 2));
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
