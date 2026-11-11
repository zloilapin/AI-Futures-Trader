const puppeteer = require('C:/Users/Admin/AppData/Local/npm-cache/_npx/ab5cd9f6d13a2312/node_modules/puppeteer');
const fs = require('fs');
const path = require('path');

const SCREENSHOTS_DIR = path.join(__dirname, '../logs/nado_grant_screenshots');

async function exploreSection3() {
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

  // --- SECTION 2 ---
  console.log('Filling Section 2...');
  const descriptionText = `AI-Futures-Trader is a non-custodial multi-agent algorithmic trading terminal, Telegram Mini App, and Copy-Trading system engineered specifically for Nado DEX CLOB on Ink L2.

Key Features & Production Status:
• Autonomous Consortium: Bull/Bear/OI/Funding specialists debating market regimes (Trending, Choppy, Range) with deterministic anti-chase guards.
• Sentinel Risk Agent: 24/7 on-chain SL/TP trigger placement (Oracle Price SL / Last Price TP), parabolic trailing stop, and breakeven management.
• Non-Custodial LinkSigner: Generates session keys via EIP-712 that can only trade on CLOB, strictly forbidding collateral withdrawals.
• Copy-Trading Engine: Automatically replicates master strategies to connected subscribers via Telegram Mini App while tagging NADO_BUILDER_ID for on-chain fee splitting.`;

  await fillByBlockId('85ad5b0c-23a5-45c5-9fc7-3d1e2a08978a', descriptionText, 'textarea');

  // Helper to select options in a multi-select
  async function selectMultiOptions(blockId, desiredOptions) {
    await page.evaluate((bId) => {
      const input = document.querySelector(`[data-block-id="${bId}"] input`);
      if (input) input.click();
    }, blockId);
    await new Promise(r => setTimeout(r, 400));
    await page.evaluate((desired) => {
      const optionEls = Array.from(document.querySelectorAll('.tally-multi-select-list-option, [role="option"]'));
      for (const optEl of optionEls) {
        const text = optEl.innerText.trim();
        if (desired.includes(text)) {
          optEl.click();
        }
      }
    }, desiredOptions);
    await page.evaluate(() => document.body.click());
    await new Promise(r => setTimeout(r, 300));
  }

  // Type of your build
  await selectMultiOptions('e1fdc97f-4d4c-415b-92da-4a7c18a29fb6', [
    'Trading terminal/frontend',
    'Smart order router/copy trading',
    'Bots',
    'Analytics/risk/portfolio tools'
  ]);

  // Target users
  await selectMultiOptions('5038926d-f73e-4765-a694-7e9f71eb19d1', [
    'Consumer/mainstream',
    'Professional traders'
  ]);

  // Primary impact to Nado
  await selectMultiOptions('169cefcd-8208-4fa3-8592-280f30c9157c', [
    'Trading volume',
    'Users',
    'Open interest/perp depth'
  ]);

  // Has your product been integrated with any other perp DEX besides Nado? -> B No
  await page.evaluate(() => {
    const radioNo = document.querySelector('[data-block-id="09aaaa0e-eb28-4908-925d-12bc3d247aa7"] input');
    if (radioNo) {
      radioNo.click();
      radioNo.checked = true;
      radioNo.dispatchEvent(new Event('change', { bubbles: true }));
    }
  });

  // Current tractions
  const tractionText = `Live on Ink L2 Mainnet (Chain ID 57073):
• Live Subaccount: 0x12d1293CBD98eEA18a2e5906dF5ED3BEf07fB1aa (active positions in SOL-USD & ETH-USD on Nado CLOB).
• 100% open-source codebase with automated SDK test suite (740+ tests).
• Integrated Telegram Bot & Mini App with non-custodial LinkSigner onboarding.
• Ready to tag 100% of generated orders with Nado Builder ID as soon as assigned.`;

  await fillByBlockId('6671c138-b4c5-4ddf-a7d9-2d889935d8d8', tractionText, 'textarea');

  await page.screenshot({ path: path.join(SCREENSHOTS_DIR, '02_section2_filled.png'), fullPage: true });

  // Click Next
  console.log('Clicking Next to Section 3...');
  await page.evaluate(() => {
    const nextBtn = Array.from(document.querySelectorAll('button')).find(b => b.innerText.includes('Next'));
    if (nextBtn) nextBtn.click();
  });

  await new Promise(r => setTimeout(r, 2000));

  // Inspect Section 3
  const sec3Details = await page.evaluate(() => {
    const title = document.querySelector('h1, h2, h3, [role="heading"]')?.innerText || document.title;
    const headings = Array.from(document.querySelectorAll('h1, h2, h3, h4, [role="heading"]')).map(h => h.innerText);
    const blocks = Array.from(document.querySelectorAll('[data-block-id]')).map(b => {
      const blockId = b.getAttribute('data-block-id');
      const text = b.innerText.trim();
      const input = b.querySelector('input');
      const textarea = b.querySelector('textarea');
      const radios = Array.from(b.querySelectorAll('input[type="radio"], input[type="checkbox"]')).map(r => ({
        type: r.type,
        value: r.value,
        id: r.id,
        label: r.parentElement?.innerText?.trim() || ''
      }));
      let type = 'text_or_container';
      if (input && input.type !== 'radio' && input.type !== 'checkbox') type = `input[${input.type}] (placeholder="${input.placeholder}")`;
      else if (textarea) type = `textarea (placeholder="${textarea.placeholder}")`;
      else if (radios.length > 0) type = `options: [${radios.map(r => r.label).join(' | ')}]`;
      return { blockId, text: text.slice(0, 200), type };
    });
    const buttons = Array.from(document.querySelectorAll('button')).map(b => b.innerText.trim());
    return { title, headings, blocks, buttons, bodyText: document.body.innerText };
  });

  console.log('=== SECTION 3 DETAILS ===');
  console.log(JSON.stringify(sec3Details, null, 2));
  fs.writeFileSync(path.join(SCREENSHOTS_DIR, 'section3_details.json'), JSON.stringify(sec3Details, null, 2));
  await page.screenshot({ path: path.join(SCREENSHOTS_DIR, '03_section3.png'), fullPage: true });

  await browser.close();
}

exploreSection3().catch(e => console.error(e));

