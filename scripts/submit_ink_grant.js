const puppeteer = require('C:/Users/Admin/AppData/Local/npm-cache/_npx/ab5cd9f6d13a2312/node_modules/puppeteer');
const fs = require('fs');
const path = require('path');

const SCREENSHOTS_DIR = path.join(__dirname, '../logs/grant_submission_screenshots');
if (!fs.existsSync(SCREENSHOTS_DIR)) {
  fs.mkdirSync(SCREENSHOTS_DIR, { recursive: true });
}

async function run() {
  console.log('🚀 Launching headless browser for Ink Spark Builder Grant submission...');
  const browser = await puppeteer.launch({
    headless: "new",
    args: ['--no-sandbox', '--disable-setuid-sandbox']
  });

  const page = await browser.newPage();
  await page.setViewport({ width: 1280, height: 1200 });

  console.log('🌐 Navigating to https://forms.inkonchain.com/spark-builder-program ...');
  await page.goto('https://forms.inkonchain.com/spark-builder-program', {
    waitUntil: 'networkidle2',
    timeout: 60000
  });

  console.log('📄 Page loaded:', await page.title());

  // Helper to fill input / textarea by block id using native property setter
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

  // ==========================================
  // SECTION 1: Project Overview
  // ==========================================
  console.log('\n📝 [1/3] Filling Section 1: Project Overview...');

  // Project Name
  await fillByBlockId('8a6eecf7-c05c-44f8-bb96-17daf72a0268', 'AI-Futures-Trader on Nado DEX (Ink L2)');

  // Project Website
  await fillByBlockId('bea67010-176f-42cb-a92f-d494521e3b3e', 'https://explorer.inkonchain.com/address/0x12d1293CBD98eEA18a2e5906dF5ED3BEf07fB1aa');

  // Project Twitter
  await fillByBlockId('1d29697f-6cb5-4a45-8908-1f3503854457', 'https://x.com/LapkaMax64244');

  // Project Description + Ink Impact
  const descriptionText = `AI-Futures-Trader is an open-source, non-custodial multi-agent algorithmic trading terminal and execution SDK purpose-built for the Nado DEX central limit order book (CLOB) on Ink L2.

What Problem It Solves:
Retail traders and quantitative developers on Ink face high friction participating in Nado's CLOB perps markets: they either trade manually (subject to fatigue and false breakouts) or use risky custodial bots requiring full private key custody. AI-Futures-Trader solves this with a non-custodial session architecture (Nado LinkSigner) where delegated session keys can place and manage orders with ZERO withdrawal privilege—guaranteeing complete asset safety.

Why Ink is the Ideal Home:
Ink L2 provides ultra-low fees and sub-second settlement essential for high-frequency maker quoting and 24/7 autonomous trailing stop protection. AI-Futures-Trader enriches orderbook liquidity on Nado via POST_ONLY maker limit orders and dual-trigger execution (Oracle SL to prevent wick-outs and Last Price TP).

Measurable Impact for Ink Ecosystem:
1. Deepens Nado CLOB liquidity and daily volume across BTC, ETH, SOL, BNB perps.
2. Natively tags every order on-chain using official Nado Builder ID appendix bitmask.
3. Provides an open-source Python SDK and public Telegram Intelligence Hub for the Ink community.`;

  await fillByBlockId('163b68ab-3086-45d4-9998-938baf8c4c09', descriptionText, 'textarea');

  // Show Us What You're Building
  await fillByBlockId('6f17c195-d170-4786-9383-c9f04413df10', 'https://explorer.inkonchain.com/address/0x12d1293CBD98eEA18a2e5906dF5ED3BEf07fB1aa');

  // Mainnet Contracts Deployed
  const contractsText = `Ink L2 Mainnet (Chain ID 57073):
- Active Trading Sub-Account: 0x12d1293CBD98eEA18a2e5906dF5ED3BEf07fB1aa (https://explorer.inkonchain.com/address/0x12d1293CBD98eEA18a2e5906dF5ED3BEf07fB1aa)
- Nado Endpoint Contract: 0x05ec92D78ED421f3D3Ada77FFdE167106565974E
- Nado Clearinghouse Contract: 0xd218Dbe127C76dbF61e7123A16A1926b42b68875`;

  await fillByBlockId('82183786-8b9d-4dea-87a5-a5f2aa7a45f1', contractsText, 'textarea');

  // Project Category (Multi-select 1)
  await page.evaluate(() => {
    const ms1 = document.querySelector('[data-block-id="9b1e22f0-979a-4c88-95cd-6baf423b6402"] input');
    if (ms1) {
      ms1.focus();
      ms1.click();
    }
  });
  await new Promise(r => setTimeout(r, 200));
  await page.evaluate(() => {
    const opt = document.getElementById('c19c1fdf-ff1f-4484-88ff-49f3a71f7f5f'); // Built on Nado with builder code integration
    if (opt) opt.click();
  });
  await new Promise(r => setTimeout(r, 200));

  // Which Ink priority areas (Multi-select 2)
  await page.evaluate(() => {
    const ms2 = document.querySelector('[data-block-id="ed0251d4-a86d-42f7-843a-845c69242006"] input');
    if (ms2) {
      ms2.focus();
      ms2.click();
    }
  });
  await new Promise(r => setTimeout(r, 200));
  await page.evaluate(() => {
    const opt = document.getElementById('5d269d9b-38c7-4467-91d1-749b18355c12'); // Nado
    if (opt) opt.click();
  });
  await new Promise(r => setTimeout(r, 200));

  // What proof of traction or demand can you share?
  const tractionText = `Live Proof of Traction on Ink Mainnet:
1. Active Live Trading Sub-Account: Verified on-chain at 0x12d1293CBD98eEA18a2e5906dF5ED3BEf07fB1aa on Nado DEX CLOB.
2. Execution Record: Over 1,300 live market evaluation cycles with 48+ executed on-chain orders.
3. Risk & Safety Benchmark: 0 margin call incidents, 0 liquidation events. Autonomous Sentinel trailing stop protections successfully locking profits (e.g. +0.87R average on trending pairs) and cutting risk.
4. Non-Custodial Security: Completed LinkSigner onboarding tool allowing users to delegate trading without giving up withdrawal rights.
5. Telemetry & Auditability: Complete append-only JSONL decision logs and open-source Python SDK pipeline ready for community distribution.`;

  await fillByBlockId('4b0e949b-e276-4e51-a130-459eb5d4ab55', tractionText, 'textarea');

  await page.screenshot({ path: path.join(SCREENSHOTS_DIR, '01_section1_filled.png'), fullPage: true });
  console.log('📸 Saved screenshot: 01_section1_filled.png');

  // Click Next -> Section 2
  await page.evaluate(() => {
    const btn = document.querySelector('button[type="submit"]');
    if (btn) btn.click();
  });
  await new Promise(r => setTimeout(r, 2000));

  // ==========================================
  // SECTION 2: Builder Info
  // ==========================================
  console.log('\n📝 [2/3] Filling Section 2: Builder Info...');

  // Name or Alias: Maxim Lapin
  await fillByBlockId('7fd649f4-7e16-45d8-9016-e27a34bdfa79', 'Maxim Lapin');

  // Email Address: zloilapinbs@gmail.com
  await fillByBlockId('b2a38a26-eacc-49a2-a0fe-7003e9ec4865', 'zloilapinbs@gmail.com');

  // Twitter/X Profile: https://x.com/LapkaMax64244
  await fillByBlockId('d47e03d3-3db3-4fed-9032-79923e3a2483', 'https://x.com/LapkaMax64244');

  // TG Handle: @lazerniyyyeth
  await fillByBlockId('60073eb8-7d45-49ae-8088-b2b0093b4d16', '@lazerniyyyeth');

  // Discord Handle: lapinzloyyy
  await fillByBlockId('2ea704d3-d10e-48fd-aa97-7b9dcae8e1bd', 'lapinzloyyy');

  // Have You Previously Built on Ink? -> No
  await page.evaluate(() => {
    const rNo = document.getElementById('choice_006b0c08-7851-4d2e-8622-eb2e2c8bc3f5');
    if (rNo) {
      rNo.click();
      rNo.dispatchEvent(new Event('change', { bubbles: true }));
    }
  });
  await new Promise(r => setTimeout(r, 200));

  // Have You Built on Other Chains Before? -> Ethereum, Optimism, Base, Arbitrum
  await page.evaluate(() => {
    const ms = document.querySelector('[data-block-id="eda80256-8cdd-44a1-9181-ec55a4681813"] input');
    if (ms) {
      ms.focus();
      ms.click();
    }
  });
  await new Promise(r => setTimeout(r, 200));

  await page.evaluate(() => {
    document.getElementById('95719466-240a-4f0d-aec4-8602af6dde7a')?.click(); // Ethereum
    document.getElementById('8ceb9a37-3819-4a9a-af75-cb6bbff629ac')?.click(); // Optimism
    document.getElementById('7d4d0e56-6689-418f-8624-d0246c59dc47')?.click(); // Base
    document.getElementById('f0dce5d2-fbef-4807-b8f6-e5b28fe25c72')?.click(); // Arbitrum
  });
  await new Promise(r => setTimeout(r, 200));

  await page.screenshot({ path: path.join(SCREENSHOTS_DIR, '02_section2_filled.png'), fullPage: true });
  console.log('📸 Saved screenshot: 02_section2_filled.png');

  // Click Next -> Section 3
  await page.evaluate(() => {
    const btn = document.querySelector('button[type="submit"]');
    if (btn) btn.click();
  });
  await new Promise(r => setTimeout(r, 2000));

  // ==========================================
  // SECTION 3: Support Request
  // ==========================================
  console.log('\n📝 [3/3] Filling Section 3: Support Request...');

  // Requested Grant Amount: 12000
  await fillByBlockId('c442a13c-8452-466e-907a-bda867ab1ced', '12000');

  // What Would You Use the Funds For?
  const useOfFundsText = `The requested $12,000 USDC grant is 100% dedicated to open-source software engineering, security tooling, and community infrastructure across 3 defined milestones:

1. Milestone 1 ($4,500 USDC | Weeks 1-4): Non-Custodial Multi-Agent Terminal on Ink L2
- Full web & CLI onboarding tool using Nado LinkSigner EIP-712 architecture.
- Users delegate trading authority with ZERO withdrawal capability, guaranteeing absolute fund safety.
- Real-time subaccount dashboard monitoring PnL, margin ratio, and Sentinel SL/TP state.

2. Milestone 2 ($4,000 USDC | Weeks 5-8): Public AI Market Intelligence & Community Signal Hub
- Free Telegram bot with zero-token read-only Showcase Mode (/status, /regime, /stats).
- Community broadcast channel streaming real-time Nado CLOB anomaly alerts (OI spikes, volatility squeeze).
- 6-month dedicated low-latency Ink RPC and inference infrastructure reserve.

3. Milestone 3 ($3,500 USDC | Weeks 9-12): Open-Source Python Execution & Risk SDK for Nado CLOB
- Published PyPI package (nado-agent-sdk) with comprehensive typing and async architecture.
- Modular risk controls: Sentinel parabolic trailing stop, free-ride corridor, and anti-chase guards.
- Reference bot templates (Maker Quoter, Momentum Follower) and developer documentation.

Note: Zero funds are requested or used as proprietary trading capital.`;

  await fillByBlockId('8e89380d-375f-4837-a3a6-15268d2d73e7', useOfFundsText, 'textarea');

  // Funding Fit Acknowledgement (Checkbox)
  await page.evaluate(() => {
    const cb = document.getElementById('checkbox_ae3ec993-6e78-4b62-9b46-060e51c52879');
    if (cb && !cb.checked) {
      cb.click();
      cb.dispatchEvent(new Event('change', { bubbles: true }));
    }
  });
  await new Promise(r => setTimeout(r, 200));

  // What Other Support Would Help You?
  await page.evaluate(() => {
    const ms = document.querySelector('[data-block-id="a7071a6a-4d2a-4703-b41e-4f20e876fab7"] input');
    if (ms) {
      ms.focus();
      ms.click();
    }
  });
  await new Promise(r => setTimeout(r, 200));

  await page.evaluate(() => {
    document.getElementById('bef810b5-e393-4b4d-98f8-e304cf7777cf')?.click(); // Exposure/Amplification from INK
    document.getElementById('a640b692-ceec-493c-b971-8d7753691313')?.click(); // Ecosystem connections
    document.getElementById('8d68e376-e241-4ec6-85c0-b1d38a1739ae')?.click(); // Community testers
  });
  await new Promise(r => setTimeout(r, 200));

  // Anything Else We Should Know?
  const anythingElseText = `We are in active communication with the Nado ecosystem track to register our Nado Builder ID, and we have fully implemented appendix bitmask tagging in production. We are excited to help grow algorithmic trading and liquidity on Ink L2!`;

  await fillByBlockId('c1fca4b9-bc14-4719-b121-00ff103f71b5', anythingElseText, 'textarea');

  // How Did You Hear About Spark? -> From Ink's website
  await page.evaluate(() => {
    const rWebsite = document.getElementById('choice_30761e75-60fa-4e58-b1ab-c86f6a355238');
    if (rWebsite) {
      rWebsite.click();
      rWebsite.dispatchEvent(new Event('change', { bubbles: true }));
    }
  });
  await new Promise(r => setTimeout(r, 200));

  await page.screenshot({ path: path.join(SCREENSHOTS_DIR, '03_section3_filled.png'), fullPage: true });
  console.log('📸 Saved screenshot: 03_section3_filled.png');

  // Check if --submit flag is passed
  const doSubmit = process.argv.includes('--submit');
  if (doSubmit) {
    console.log('🚀 SUBMITTING OFFICIAL APPLICATION TO INK FOUNDATION...');
    await page.evaluate(() => {
      const btn = document.querySelector('button[type="submit"]');
      if (btn) btn.click();
    });
    await new Promise(r => setTimeout(r, 5000));

    await page.screenshot({ path: path.join(SCREENSHOTS_DIR, '04_submitted_confirmation.png'), fullPage: true });
    console.log('🎉 Application submitted! Confirmation screenshot saved: 04_submitted_confirmation.png');
    const confirmationText = await page.evaluate(() => document.body.innerText);
    console.log('\n--- CONFIRMATION BODY ---');
    console.log(confirmationText.slice(0, 1000));
  } else {
    console.log('ℹ️ Dry-run mode completed. Run with --submit to execute final submission.');
  }

  await browser.close();
  console.log('✅ Finished successfully.');
}

run().catch(err => {
  console.error('❌ Error during execution:', err);
  process.exit(1);
});
