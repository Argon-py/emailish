// Browser-level QA of the emailish UI (headless Chromium via Playwright).
// Usage: node test_data/qa_ui.js http://127.0.0.1:PORT <output-dir>
// The server must be started with EMAILISH_SMTP_ENABLED=false.
const { chromium } = require('playwright');
const path = require('path');
const fs = require('fs');

(async () => {
  const [, , url, out] = process.argv;
  const td = __dirname;
  const results = [];
  const check = (name, ok, detail = '') => { results.push({ name, ok, detail }); console.log(`[${ok ? 'PASS' : 'FAIL'}] ${name}${detail ? ' -- ' + detail : ''}`); };
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1200, height: 900 } });
  await page.goto(url);

  // banner tells the user SMTP is off
  const banner = await page.textContent('#banner');
  check('UI: banner explains that mailbox checks are not running', /cannot run|disabled/i.test(banner), banner.trim().slice(0, 90));

  // 1) 1,000-row XLSX: automatic detection + progress + downloads
  await page.setInputFiles('#file', path.join(td, 'synthetic_1000.xlsx'));
  await page.waitForSelector('#column-card:not(.hidden)');
  const detected = await page.$eval('#column', s => s.value);
  check('UI: XLSX upload shows auto-detected column', detected === 'Email Address', detected);
  const info = await page.textContent('#file-info');
  check('UI: row count shown after upload', info.includes('1,000 rows'), info);
  const previewRows = await page.$$eval('#preview tr', r => r.length);
  check('UI: preview table rendered', previewRows === 6, `${previewRows - 1} preview rows`);
  await page.click('#start');
  const phases = new Set();
  const widths = [];
  const t0 = Date.now();
  while (!(await page.isVisible('#result'))) {
    phases.add((await page.textContent('#phase')).replace(/[\d,]+ \/ [\d,]+/, 'N / M'));
    widths.push(await page.$eval('#bar', b => parseFloat(b.style.width) || 0));
    if (Date.now() - t0 > 120000) break;
    await page.waitForTimeout(100);
  }
  const elapsed = (Date.now() - t0) / 1000;
  check('UI: job completes and result panel appears', await page.isVisible('#result'), `${elapsed.toFixed(1)} s`);
  check('UI: progress text shown while running', phases.size >= 1, [...phases].slice(0, 4).join(' | '));
  check('UI: progress bar never moves backwards', widths.every((w, i) => i === 0 || w >= widths[i - 1]),
        `${widths.length} samples, last ${widths[widths.length - 1]}%`);
  const chips = await page.textContent('#chips');
  check('UI: result chips show status counts', /INVALID/.test(chips) && /UNKNOWN/.test(chips), chips.replace(/\s+/g, ' '));
  check('UI: no VALID chip without SMTP', !/VALID\d/.test(chips.replace(/INVALID/g, '')), chips);
  const [d1] = await Promise.all([page.waitForEvent('download'), page.click('#dl-xlsx')]);
  await d1.saveAs(path.join(out, 'ui_' + d1.suggestedFilename()));
  const [d2] = await Promise.all([page.waitForEvent('download'), page.click('#dl-csv')]);
  await d2.saveAs(path.join(out, 'ui_' + d2.suggestedFilename()));
  check('UI: Excel download button works', fs.statSync(path.join(out, 'ui_' + d1.suggestedFilename())).size > 10000, d1.suggestedFilename());
  check('UI: CSV download button works', fs.statSync(path.join(out, 'ui_' + d2.suggestedFilename())).size > 10000, d2.suggestedFilename());
  await page.screenshot({ path: path.join(out, 'ui_1000_rows.png'), fullPage: true });

  // 2) manual column selection through the dropdown
  await page.setInputFiles('#file', path.join(td, 'two_email_columns.csv'));
  await page.waitForFunction(() => document.querySelector('#file-info').textContent.includes('two_email_columns'));
  const auto = await page.$eval('#column', s => s.value);
  await page.selectOption('#column', 'Backup Email');
  const highlighted = await page.$eval('#preview th.sel', th => th.textContent);
  check('UI: manual column change highlights the chosen column', auto === 'Email' && highlighted === 'Backup Email',
        `auto=${auto}, highlighted=${highlighted}`);
  await page.click('#start');
  await page.waitForFunction(() => document.querySelector('#phase').textContent.startsWith('Done · 30'), null, { timeout: 60000 });
  const chips2 = await page.textContent('#chips');
  check('UI: manually selected column was verified (30 INVALID Null-MX)', chips2.replace(/\s+/g, '') === 'INVALID30', chips2);

  // 3) history table
  const hist = await page.$$eval('#history tr', rows => rows.slice(1).map(r => r.textContent));
  check('UI: history lists both jobs as done', hist.length >= 2 && hist.slice(0, 2).every(t => t.includes('done')),
        `${hist.length} rows`);
  const links = await page.$$eval('#history a', as => as.map(a => a.textContent));
  check('UI: history offers xlsx/csv downloads', links.filter(t => t === 'xlsx').length >= 2 && links.filter(t => t === 'csv').length >= 2);

  // 4) phone width: no horizontal scroll
  const m = await browser.newPage({ viewport: { width: 390, height: 800 } });
  await m.goto(url);
  await m.waitForTimeout(500);
  const sw = await m.evaluate(() => document.documentElement.scrollWidth);
  check('UI: no horizontal page scroll at 390px width', sw <= 390, `scrollWidth ${sw}`);

  fs.writeFileSync(path.join(out, 'ui_checks.json'), JSON.stringify(results, null, 1));
  await browser.close();
  process.exit(results.every(r => r.ok) ? 0 : 1);
})();
