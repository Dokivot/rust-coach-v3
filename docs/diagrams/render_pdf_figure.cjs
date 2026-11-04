const fs = require('node:fs');
const path = require('node:path');

async function main() {
  const [moduleRoot, chromePath] = process.argv.slice(2);
  const puppeteer = require(path.join(moduleRoot, 'puppeteer'));
  const root = path.resolve(__dirname, '../..');
  const browser = await puppeteer.launch({ executablePath: chromePath, headless: true, args: ['--no-sandbox'] });
  try {
    const page = await browser.newPage();
    const svg = fs.readFileSync(path.join(root, 'docs/technical-architecture-target.svg'), 'utf8');
    await page.setContent('<meta charset="utf-8"><style>@page{size:3000px 2440px;margin:0}body{margin:0}svg{display:block}</style>' + svg);
    await page.evaluate(() => document.fonts.ready);
    await page.pdf({ path: path.join(root, 'tmp/pdfs/architecture-vector.pdf'), width: '3000px', height: '2440px', preferCSSPageSize: true, printBackground: true });
  } finally {
    await browser.close();
  }
}

main().catch(error => { console.error(error); process.exitCode = 1; });
