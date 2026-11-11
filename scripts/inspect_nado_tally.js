const puppeteer = require('C:/Users/Admin/AppData/Local/npm-cache/_npx/ab5cd9f6d13a2312/node_modules/puppeteer');
const fs = require('fs');
const path = require('path');

const SCREENSHOTS_DIR = path.join(__dirname, '../logs/nado_grant_screenshots');
if (!fs.existsSync(SCREENSHOTS_DIR)) {
  fs.mkdirSync(SCREENSHOTS_DIR, { recursive: true });
}

async function inspect() {
  console.log('🚀 Navigating to Nado Tally Form: https://tally.so/r/0QO4oy ...');
  const browser = await puppeteer.launch({
    headless: "new",
    args: ['--no-sandbox', '--disable-setuid-sandbox']
  });

  const page = await browser.newPage();
  await page.setViewport({ width: 1280, height: 1200 });

  await page.goto('https://tally.so/r/0QO4oy', {
    waitUntil: 'networkidle2',
    timeout: 60000
  });

  console.log('📄 Title:', await page.title());
  await page.screenshot({ path: path.join(SCREENSHOTS_DIR, '01_nado_form_start.png'), fullPage: true });

  const formDetails = await page.evaluate(() => {
    const title = document.querySelector('h1')?.innerText || document.title;
    const blocks = Array.from(document.querySelectorAll('[data-block-id]')).map(b => {
      const blockId = b.getAttribute('data-block-id');
      const label = b.querySelector('label, [role="heading"], h2, h3, h4, p, span')?.innerText || '';
      const input = b.querySelector('input');
      const textarea = b.querySelector('textarea');
      const select = b.querySelector('select');
      const radios = Array.from(b.querySelectorAll('input[type="radio"], input[type="checkbox"]')).map(r => ({
        type: r.type,
        value: r.value,
        id: r.id,
        label: r.parentElement?.innerText || ''
      }));
      const buttons = Array.from(b.querySelectorAll('button')).map(btn => btn.innerText);
      
      let type = 'unknown';
      if (input) type = `input[${input.type}] (name=${input.name}, placeholder=${input.placeholder})`;
      else if (textarea) type = `textarea (placeholder=${textarea.placeholder})`;
      else if (select) type = 'select';
      else if (radios.length > 0) type = `options [${radios.map(r => r.label.trim()).join(' | ')}]`;
      else if (buttons.length > 0) type = `buttons [${buttons.join(' | ')}]`;

      return {
        blockId,
        text: b.innerText.trim().slice(0, 300),
        type,
        required: b.querySelector('[aria-required="true"], .required') !== null
      };
    });

    const allButtons = Array.from(document.querySelectorAll('button')).map(btn => ({
      text: btn.innerText.trim(),
      type: btn.type,
      className: btn.className
    }));

    return {
      title,
      blocks,
      allButtons,
      fullBodyText: document.body.innerText.slice(0, 2000)
    };
  });

  console.log('=== FORM DETAILS ===');
  console.log(JSON.stringify(formDetails, null, 2));

  fs.writeFileSync(path.join(SCREENSHOTS_DIR, 'form_structure.json'), JSON.stringify(formDetails, null, 2));
  await browser.close();
  console.log('✅ Inspection finished. Screenshot saved to logs/nado_grant_screenshots/01_nado_form_start.png');
}

inspect().catch(err => {
  console.error('❌ Inspection error:', err);
  process.exit(1);
});

