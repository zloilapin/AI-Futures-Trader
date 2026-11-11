const puppeteer = require('C:/Users/Admin/AppData/Local/npm-cache/_npx/ab5cd9f6d13a2312/node_modules/puppeteer');
const fs = require('fs');
const path = require('path');

const SCREENSHOTS_DIR = path.join(__dirname, '../logs/nado_grant_screenshots');

async function explore() {
  const browser = await puppeteer.launch({ headless: "new", args: ['--no-sandbox', '--disable-setuid-sandbox'] });
  const page = await browser.newPage();
  await page.setViewport({ width: 1280, height: 1200 });

  await page.goto('https://tally.so/r/0QO4oy', { waitUntil: 'networkidle2', timeout: 60000 });

  async function fillByBlockId(blockId, value, tag = 'input') {
    await page.evaluate((bId, val, t) => {
      const block = document.querySelector(`[data-block-id="${bId}"]`);
      if (!block) throw new Error(`Block not found: ${bId}`);
      const el = block.querySelector(t);
      if (!el) throw new Error(`Tag ${t} not found in block: ${bId}`);
      const proto = Object.getPrototypeOf(el);
      const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
      setter.call(el, val);
      el.dispatchEvent(new Event('input', { bubbles: true }));
      el.dispatchEvent(new Event('change', { bubbles: true }));
    }, blockId, value, tag);
  }

  // --- SECTION 1 ---
  await fillByBlockId('e20a3c9a-145e-4754-bd17-cecda6880083', 'AI-Futures-Trader on Nado DEX (Ink L2)');
  await fillByBlockId('3b579226-d444-43bb-909d-6c3278d30230', 'https://github.com/zloilapin/AI-Futures-Trader');
  await fillByBlockId('80a0de6e-2fb6-4b72-b1e4-046143faaba4', 'https://github.com/zloilapin/AI-Futures-Trader', 'textarea');
  await fillByBlockId('3c3e5853-5c7f-4da6-a21b-4e77b9e9759a', 'Maxim Lapin');
  await fillByBlockId('df4f607c-89db-420f-9bbf-ec0c5a64fc5f', 'zloilapinbs@gmail.com');
  await fillByBlockId('a5748407-be09-4ebc-a315-31a7c1b041f3', 'Tg: @lazerniyyyeth ; DC: lapinzloyyy');

  await page.evaluate(() => {
    const radioB = document.querySelector('[data-block-id="9ff2397f-abe2-4d5e-8e33-c614c16498ed"] input');
    if (radioB) {
      radioB.click();
      radioB.checked = true;
      radioB.dispatchEvent(new Event('change', { bubbles: true }));
    }
    const nextBtn = Array.from(document.querySelectorAll('button')).find(b => b.innerText.includes('Next'));
    if (nextBtn) nextBtn.click();
  });

  await new Promise(r => setTimeout(r, 2000));

  // Helper to click multi-select and get all options
  async function getMultiSelectOptions(blockId) {
    await page.evaluate((bId) => {
      const input = document.querySelector(`[data-block-id="${bId}"] input`);
      if (input) input.click();
    }, blockId);
    await new Promise(r => setTimeout(r, 400));
    const opts = await page.evaluate(() => {
      const optionEls = Array.from(document.querySelectorAll('.tally-multi-select-list-option, [role="option"]'));
      return optionEls.map(el => el.innerText.trim());
    });
    // Click outside to close
    await page.evaluate(() => document.body.click());
    await new Promise(r => setTimeout(r, 200));
    return opts;
  }

  const buildTypeOptions = await getMultiSelectOptions('e1fdc97f-4d4c-415b-92da-4a7c18a29fb6');
  console.log('buildTypeOptions:', buildTypeOptions);

  const targetUsersOptions = await getMultiSelectOptions('5038926d-f73e-4765-a694-7e9f71eb19d1');
  console.log('targetUsersOptions:', targetUsersOptions);

  const impactOptions = await getMultiSelectOptions('169cefcd-8208-4fa3-8592-280f30c9157c');
  console.log('impactOptions:', impactOptions);

  await browser.close();
}

explore().catch(e => console.error(e));
