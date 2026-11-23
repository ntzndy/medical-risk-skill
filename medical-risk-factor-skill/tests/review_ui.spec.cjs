// Synthetic-only offline UI smoke test. Pass the bundled Python path as argv[2].
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { execFileSync } = require('node:child_process');
const { pathToFileURL } = require('node:url');
const { chromium } = require('playwright');

const python = process.argv[2];
if (!python) throw new Error('Usage: node tests/review_ui.spec.cjs <python-path>');
const root = fs.mkdtempSync(path.join(os.tmpdir(), 'medical-review-v14-'));
const source = path.join(root, 'synthetic.csv');
const output = path.join(root, 'review');
const cli = path.resolve(__dirname, '../scripts/review_workflow.py');
const csv = ['年龄,结局,未选异常列,患者编号',
  ...Array.from({ length: 12 }, (_, i) => `${i === 0 ? -1 : 20 + i},${i % 2},UNSELECTED_SECRET,PRIVATE_PERSON_${i}`)].join('\n');
fs.writeFileSync(source, '\ufeff' + csv);
const run = (...args) => execFileSync(python, ['-X', 'utf8', cli, '--input', source, '--outdir', output, ...args],
  { encoding: 'utf8', timeout: 30000, windowsHide: true });
run();
assert.equal(fs.existsSync(path.join(output, 'interactive_review.html')), false);
const researchPath = path.join(root, 'research_question.json');
const research = JSON.parse(fs.readFileSync(path.join(output, 'research_question.template.json'), 'utf8'));
Object.assign(research, { status: 'confirmed', research_population: 'Synthetic independent individuals',
  research_question: 'Synthetic outcome factors', outcome: '结局', outcome_definition: 'Synthetic binary event' });
fs.writeFileSync(researchPath, JSON.stringify(research));
const withResearch = (...args) => run('--research-question', researchPath, ...args);
withResearch();

(async () => {
  const browser = await chromium.launch({ channel: process.env.PLAYWRIGHT_CHANNEL || 'msedge',
    headless: true, timeout: 15000, args: ['--disable-gpu'] });
  const errors = [], remoteRequests = [];
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 1000 }, acceptDownloads: true });
    page.setDefaultTimeout(10000);
    page.on('pageerror', error => errors.push(error.message));
    page.on('request', request => { if (/^https?:/.test(request.url())) remoteRequests.push(request.url()); });
    await page.goto(pathToFileURL(path.join(output, 'interactive_review.html')).href);
    assert.equal(await page.locator('#selection').isVisible(), true);
    assert.equal(await page.locator('#quality').isVisible(), false);
    assert.equal(await page.locator('input[type=radio]').count(), 8);
    assert.equal(await page.locator('input[type=radio]:checked').count(), 0);
    assert.equal((await page.content()).includes('PRIVATE_PERSON_'), false);
    assert.equal((await page.content()).includes('UNSELECTED_SECRET'), false);
    await page.locator('#confirm').click();
    assert.match(await page.locator('#status').innerText(), /未选择/);
    assert.equal(await page.locator('#outcome').inputValue(), '结局');
    assert.equal(await page.locator('#outcome').isDisabled(), true);
    for (let index = 0; index < 4; index++) {
      await page.locator(`input[name=pick_${index}][value="${index === 0 ? '纳入' : '不纳入'}"]`).check();
    }
    assert.equal(await page.locator('#status').innerText(), '');
    for (const [label, width, height] of [['desktop', 1440, 1000], ['mobile', 390, 844]]) {
      await page.setViewportSize({ width, height });
      await page.evaluate(() => window.scrollTo(0, 0));
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), true);
      await page.screenshot({ path: path.join(root, `selection-${label}.png`), fullPage: true, timeout: 15000 });
    }
    const selectionPath = path.join(root, 'variable_selection_decisions.json');
    let downloadEvent = page.waitForEvent('download');
    await page.locator('#confirm').click();
    await (await downloadEvent).saveAs(selectionPath);
    const selection = JSON.parse(fs.readFileSync(selectionPath, 'utf8'));
    assert.equal(selection.status, 'confirmed');
    assert.equal(selection.stage, 'variable_selection');
    assert.equal(selection.variables.length, 4);
    assert.deepEqual(selection.support_variables, []);
    withResearch('--selection-decisions', selectionPath);
    await page.goto(pathToFileURL(path.join(output, 'interactive_data_quality.html')).href);
    assert.equal(await page.locator('#quality').isVisible(), true);
    assert.equal(await page.locator('#selection').isVisible(), false);
    assert.match(await page.locator('#scope').innerText(), /年龄/);
    assert.doesNotMatch(await page.locator('#scope').innerText(), /未选异常列|患者编号/);
    await page.locator('#confirm').click();
    assert.match(await page.locator('#status').innerText(), /未决定/);
    const actions = page.locator('#issues select');
    assert.ok(await actions.count() >= 1);
    for (let index = 0; index < await actions.count(); index++) {
      await actions.nth(index).selectOption('保留并记录');
    }
    await actions.first().selectOption('设为缺失（须说明规则）');
    await page.locator('#confirm').click();
    assert.match(await page.locator('#status').innerText(), /理由/);
    for (const textarea of await page.locator('#issues textarea').all()) {
      await textarea.fill('合成测试规则；此流程不执行改值');
    }
    assert.equal(await page.locator('#status').innerText(), '');
    for (const [label, width, height] of [['desktop', 1440, 1000], ['mobile', 390, 844]]) {
      await page.setViewportSize({ width, height });
      await page.evaluate(() => window.scrollTo(0, 0));
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), true);
      await page.screenshot({ path: path.join(root, `quality-${label}.png`), fullPage: true, timeout: 15000 });
    }
    const qualityPath = path.join(root, 'clinical_review_decisions.json');
    downloadEvent = page.waitForEvent('download');
    await page.locator('#confirm').click();
    await (await downloadEvent).saveAs(qualityPath);
    const quality = JSON.parse(fs.readFileSync(qualityPath, 'utf8'));
    assert.equal(quality.status, 'reviewed');
    assert.equal(quality.statistical_plan_confirmed, false);
    const validation = JSON.parse(withResearch('--selection-decisions', selectionPath, '--validate-decisions', qualityPath));
    assert.equal(validation.valid, true);
    assert.equal(validation.formal_analysis_authorized, false);
    assert.equal(fs.readFileSync(source, 'utf8'), '\ufeff' + csv);
    assert.deepEqual(errors, []);
    assert.deepEqual(remoteRequests, []);
    console.log(JSON.stringify({ passed: true, screenshots: root, pageErrors: errors, remoteRequests }, null, 2));
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
