/* Run against synthetic review pages: node tests/verify_review_ui.cjs <workflow-dir>. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { pathToFileURL } = require('node:url');
const { chromium } = require('playwright');

async function main() {
  const root = path.resolve(process.argv[2]);
  const browser = await chromium.launch({ headless: true, channel: process.env.PLAYWRIGHT_CHANNEL || undefined });
  try {
    for (const [name, width, height] of [['desktop', 1440, 1000], ['mobile', 390, 844]]) {
      const page = await browser.newPage({ viewport: { width, height }, acceptDownloads: true });
      const errors = [];
      page.on('pageerror', error => errors.push(error.message));
      await page.goto(pathToFileURL(path.join(root, 'interactive_review.html')).href);
      assert.match(await page.title(), /2\/5/);
      assert.equal(await page.locator('#outcome').inputValue(), 'outcome');
      assert.equal(await page.locator('#outcome').isDisabled(), true);
      await page.locator('#confirm').click();
      assert.match(await page.locator('#status').textContent(), /未选择/);
      for (const radio of await page.locator('input[type=radio][value="不纳入"]').all()) {
        await radio.check();
      }
      const selectedNames = await page.evaluate(() => variables.filter(row =>
        row.variable !== decision.outcome && !String(row.suggested_type).startsWith('identifier'))
        .slice(0, 2).map(row => row.variable));
      assert.equal(selectedNames.length, 2);
      for (const variable of selectedNames) {
        const row = page.locator('tr').filter({ has: page.getByText(variable, { exact: true }) });
        await row.getByRole('radio', { name: '纳入', exact: true }).check();
      }
      await page.locator('#positive').fill('1');
      const selectedDownload = page.waitForEvent('download');
      await page.locator('#confirm').click();
      const selectionPath = path.join(root, `browser_selection_${name}.json`);
      await (await selectedDownload).saveAs(selectionPath);
      const selection = JSON.parse(fs.readFileSync(selectionPath, 'utf8'));
      assert.equal(selection.status, 'confirmed');
      assert.equal(selection.outcome, 'outcome');
      assert.ok(selection.research_question_fingerprint);
      assert.equal(selection.variables.filter(row => row.user_selection === '纳入').length, 2);
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
      await page.screenshot({ path: path.join(root, `selection_${name}.png`), fullPage: true });

      await page.goto(pathToFileURL(path.join(root, 'interactive_data_quality.html')).href);
      assert.match(await page.title(), /3\/5/);
      for (const select of await page.locator('#issues select').all()) {
        await select.selectOption({ label: '保留并记录' });
      }
      assert.equal(await page.locator('#sentinels').count(), 0);
      assert.equal(await page.locator('#missing select').count(), 0);
      for (const group of await page.locator('[data-missing-group]').all()) {
        const key = await group.getAttribute('data-missing-group');
        const input = group.locator('textarea');
        if (['below_30', 'from_30_to_50', 'above_50'].includes(key)) {
          assert.equal(await input.count(), 1);
          await input.fill('Synthetic request: review complete-case handling for this group; not execution authorization.');
        } else {
          assert.equal(await input.count(), 0);
        }
      }
      for (const textarea of await page.locator('#issues textarea').all()) {
        await textarea.fill('Synthetic user-reviewed retention');
      }
      if (await page.locator('#issues select').count()) {
        await page.locator('#issues select').first().selectOption({ label: '设为缺失（须说明规则）' });
        await page.locator('#issues textarea').first().fill('');
        await page.locator('#confirm').click();
        assert.match(await page.locator('#status').textContent(), /理由/);
        await page.locator('#issues textarea').first().fill('Synthetic browser test only; requires confirmed execution rules');
      }
      const hasFeedback = await page.locator('#missing textarea').count() > 0;
      const qualityDownload = page.waitForEvent('download');
      await page.locator(hasFeedback ? '#submit-missing' : '#confirm').click();
      const qualityPath = path.join(root, `browser_quality_${name}.json`);
      await (await qualityDownload).saveAs(qualityPath);
      const quality = JSON.parse(fs.readFileSync(qualityPath, 'utf8'));
      assert.equal(quality.status, hasFeedback ? 'missing_feedback_submitted' : 'reviewed');
      assert.equal(quality.statistical_plan_confirmed, false);
      assert.ok(quality.missing_group_feedback.every(row => row.user_text && row.resolved_text === ''));
      if (hasFeedback) {
        await page.locator('#confirm').click();
        assert.match(await page.locator('#status').textContent(), /分组意见尚待整理/);
      }
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
      await page.screenshot({ path: path.join(root, `quality_${name}.png`), fullPage: true });
      assert.deepEqual(errors, []);
      await page.close();
    }
    console.log('Desktop/mobile review UI checks passed; screenshots and synthetic exports:', root);
  } finally {
    await browser.close();
  }
}

main().catch(error => { console.error(error); process.exitCode = 1; });
