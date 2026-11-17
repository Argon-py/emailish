// Screenshot of the UI after running a job: node lab/screenshot.js <url> <xlsx> <out.png>
const { chromium } = require('playwright');
(async () => {
  const [, , url, file, out] = process.argv;
  const b = await chromium.launch();
  const p = await b.newPage({ viewport: { width: 1200, height: 900 } });
  await p.goto(url);
  await p.waitForTimeout(800);
  await p.screenshot({ path: out, fullPage: true });
  await b.close();
})();
