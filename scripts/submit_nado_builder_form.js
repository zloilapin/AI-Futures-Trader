const puppeteer = require('C:/Users/Admin/AppData/Local/npm-cache/_npx/ab5cd9f6d13a2312/node_modules/puppeteer');
const fs = require('fs');
const path = require('path');

const SCREENSHOTS_DIR = path.join(__dirname, '../logs/nado_grant_screenshots');
if (!fs.existsSync(SCREENSHOTS_DIR)) {
  fs.mkdirSync(SCREENSHOTS_DIR, { recursive: true });
}

async function submitNadoBuilderForm() {
  console.log('🚀 Launching Puppeteer to submit Nado Builder Program form (https://tally.so/r/0QO4oy)...');
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

  // ==========================================
  // SECTION 1: Project Basics
  // ==========================================
  console.log('\n📝 [1/3] Filling Section 1: Project Basics...');

  // Project name
  await fillByBlockId('e20a3c9a-145e-4754-bd17-cecda6880083', 'AI-Futures-Trader on Nado DEX (Ink L2)');

  // Website URL
  await fillByBlockId('3b579226-d444-43bb-909d-6c3278d30230', 'https://github.com/zloilapin/AI-Futures-Trader');

  // Github/repo
  await fillByBlockId('80a0de6e-2fb6-4b72-b1e4-046143faaba4', 'https://github.com/zloilapin/AI-Futures-Trader', 'textarea');

  // Primary contact name
  await fillByBlockId('3c3e5853-5c7f-4da6-a21b-4e77b9e9759a', 'Maxim Lapin');

  // Primary contact email
  await fillByBlockId('df4f607c-89db-420f-9bbf-ec0c5a64fc5f', 'zloilapinbs@gmail.com');

  // Telegram handle + Discord handle
  await fillByBlockId('a5748407-be09-4ebc-a315-31a7c1b041f3', 'Tg: @lazerniyyyeth ; DC: lapinzloyyy');

  // Do you already have a group chat with the Nado team? -> B: No, not yet
  await page.evaluate(() => {
    const radioB = document.querySelector('[data-block-id="9ff2397f-abe2-4d5e-8e33-c614c16498ed"] input');
    if (radioB) {
      radioB.click();
      radioB.checked = true;
      radioB.dispatchEvent(new Event('change', { bubbles: true }));
    }
  });

  await new Promise(r => setTimeout(r, 500));
  await page.screenshot({ path: path.join(SCREENSHOTS_DIR, '01_section1_filled.png'), fullPage: true });
  console.log('✅ Section 1 filled. Screenshot saved.');

  // Click Next
  await page.evaluate(() => {
    const nextBtn = Array.from(document.querySelectorAll('button')).find(b => b.innerText.includes('Next'));
    if (nextBtn) nextBtn.click();
  });
  await new Promise(r => setTimeout(r, 2000));

  // ==========================================
  // SECTION 2: Product & Use Case
  // ==========================================
  console.log('\n📝 [2/3] Filling Section 2: Product & Use Case...');

  const productDescription = `AI-Futures-Trader is an open-source, non-custodial multi-agent algorithmic trading terminal, Telegram Mini App, and Copy-Trading system engineered specifically for the Nado DEX central limit order book (CLOB) on Ink L2.

Core Architecture & Innovation:
1. Multi-Agent Consensus Consortium:
   A decentralized syndicate of specialized LLM/heuristic agents (Bull, Bear, OI/Funding, Price Action, Indicators) running real-time market regime classification (Trending vs. Choppy/Range) with deterministic anti-chase guards.
2. 24/7 Autonomous Sentinel Protection:
   Natively places and manages hybrid on-chain triggers (Oracle Price Stop-Loss / Last Price Take-Profit) directly on Nado smart contracts, with dynamic ATR trailing stops, breakeven thresholds, and micro-dust order guards.
3. 100% Non-Custodial LinkSigner Architecture:
   Utilizes Nado's EIP-712 LinkSigner mechanism where an ephemeral session key can only execute trades, but is cryptographically prevented from withdrawing collateral.
4. Telegram Mini App & Multi-Trader Copy Engine:
   Enables retail users to link subaccounts in 1-click and mirror master trades, with automated NADO_BUILDER_ID tagging on every order for on-chain builder fee distribution.`;

  await fillByBlockId('85ad5b0c-23a5-45c5-9fc7-3d1e2a08978a', productDescription, 'textarea');

  // Type of your build (Multi-select)
  await selectMultiOptions('e1fdc97f-4d4c-415b-92da-4a7c18a29fb6', [
    'Trading terminal/frontend',
    'Smart order router/copy trading',
    'Bots',
    'Analytics/risk/portfolio tools'
  ]);

  // Target users (Multi-select)
  await selectMultiOptions('5038926d-f73e-4765-a694-7e9f71eb19d1', [
    'Consumer/mainstream',
    'Professional traders'
  ]);

  // Primary impact to Nado (Multi-select)
  await selectMultiOptions('169cefcd-8208-4fa3-8592-280f30c9157c', [
    'Trading volume',
    'Users',
    'Open interest/perp depth'
  ]);

  // Has your product been integrated with any other perp DEX besides Nado? -> B: No
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
• Live Subaccount: 0x12d1293CBD98eEA18a2e5906dF5ED3BEf07fB1aa actively trading SOL-USD and ETH-USD with live on-chain SL/TP triggers.
• 100% test pass rate across 740+ unit and integration tests (including EIP-712 LinkSigner and builder fee attribution logic).
• Fully integrated Telegram Bot & Mini App (TMA) for zero-barrier retail onboarding.
• Active referral program integration (Code: ptn3l1u) bringing net new traders to Nado DEX.
• Production SDK built on official nado-protocol Python library, ready to stamp 100% of order flow with NADO_BUILDER_ID.`;

  await fillByBlockId('6671c138-b4c5-4ddf-a7d9-2d889935d8d8', tractionText, 'textarea');

  await new Promise(r => setTimeout(r, 500));
  await page.screenshot({ path: path.join(SCREENSHOTS_DIR, '02_section2_filled.png'), fullPage: true });
  console.log('✅ Section 2 filled. Screenshot saved.');

  // Click Next
  await page.evaluate(() => {
    const nextBtn = Array.from(document.querySelectorAll('button')).find(b => b.innerText.includes('Next'));
    if (nextBtn) nextBtn.click();
  });
  await new Promise(r => setTimeout(r, 2000));

  // ==========================================
  // SECTION 3: Nado Integration Plan
  // ==========================================
  console.log('\n📝 [3/3] Filling Section 3: Nado Integration Plan...');

  // Current product status -> E: Already live on Ink
  await page.evaluate(() => {
    const radioE = document.querySelector('[data-block-id="4d75f204-bbdd-47d2-b822-3c755cbf5ed2"] input');
    if (radioE) {
      radioE.click();
      radioE.checked = true;
      radioE.dispatchEvent(new Event('change', { bubbles: true }));
    }
  });

  // Expected launch timeline on Nado -> A: <1 month
  await page.evaluate(() => {
    const radioA = document.querySelector('[data-block-id="9210bf2c-62c4-4e9c-8665-dcaf68b568d8"] input');
    if (radioA) {
      radioA.click();
      radioA.checked = true;
      radioA.dispatchEvent(new Event('change', { bubbles: true }));
    }
  });

  // How do you want to integrate with Nado? (Multi-select)
  await selectMultiOptions('986e6318-24a8-48a9-be04-2c890cb1214a', [
    'Use Nado perps via API/SDK',
    'Route order flow into Nado',
    'Use Nado as backend for my own UI'
  ]);

  // How we can best support you? (Multi-select)
  await selectMultiOptions('f6912c20-8eed-4cd8-9ced-b19a71af3ed8', [
    'Fee rebates/incentives',
    'Technical support',
    'GTM/co-marketing'
  ]);

  // Builder code recipient address
  await fillByBlockId('11dbdae5-9299-4518-92e7-5b48682efef7', '0x12d1293CBD98eEA18a2e5906dF5ED3BEf07fB1aa');

  // Minimum builder fee
  await fillByBlockId('3d1055c6-cde5-4129-9cb8-5be8bdb12b6f', '0 bps');

  // Maximum builder fee
  await fillByBlockId('2e060550-730a-4211-a6a2-508dbbd30b6c', '1 bps (0.01%)');

  // Anything else you’d like us to know or request?
  const anythingElseText = `We are deeply committed to growing the Nado DEX ecosystem on Ink L2.
1. Registration & Builder ID:
   Our system already has full technical support for NADO_BUILDER_ID and build_order_appendix() in production. As soon as you assign our official builder_id, we will instantly plug it into our config and stamp all mainnet and copy-trader order volume with it.
2. Live Demonstration:
   You are warmly invited to test our live bot in Public Showcase Mode (0 LLM token cost) via Telegram: @AI_Futures_Trader_Bot.
3. Ink Grant Alignment:
   We have also formally applied to the Ink Spark Builder Program (Priority Nado Integrations Track).
4. Direct Communication:
   Looking forward to connecting on Telegram (@lazerniyyyeth) or Discord (lapinzloyyy) to coordinate technical onboarding and GTM!`;

  await fillByBlockId('ea4dd79b-845c-489a-9691-577b2283f477', anythingElseText, 'textarea');

  await new Promise(r => setTimeout(r, 500));
  await page.screenshot({ path: path.join(SCREENSHOTS_DIR, '03_section3_filled.png'), fullPage: true });
  console.log('✅ Section 3 filled. Screenshot saved.');

  // ==========================================
  // SUBMISSION
  // ==========================================
  console.log('\n🚀 Submitting Nado Builder Program Application...');
  await page.evaluate(() => {
    const submitBtn = Array.from(document.querySelectorAll('button')).find(b => b.innerText.includes('Submit'));
    if (!submitBtn) throw new Error('Submit button not found!');
    submitBtn.click();
  });

  // Wait for confirmation page
  console.log('⏳ Waiting for submission confirmation...');
  await page.waitForNavigation({ waitUntil: 'networkidle2', timeout: 30000 }).catch(() => {});
  await new Promise(r => setTimeout(r, 3000));

  await page.screenshot({ path: path.join(SCREENSHOTS_DIR, '04_submitted_confirmation.png'), fullPage: true });

  const finalPageText = await page.evaluate(() => document.body.innerText);
  console.log('\n📄 Final Page Text Preview:\n' + finalPageText.slice(0, 500));

  await browser.close();
  console.log('\n🎉 ALL DONE! Submission confirmed and screenshots saved to logs/nado_grant_screenshots/');
}

submitNadoBuilderForm().catch(err => {
  console.error('❌ Submission failed:', err);
  process.exit(1);
});

