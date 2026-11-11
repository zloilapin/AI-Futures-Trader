const puppeteer = require('C:/Users/Admin/AppData/Local/npm-cache/_npx/ab5cd9f6d13a2312/node_modules/puppeteer');
const fs = require('fs');

async function inspectSec3Elements() {
  const browser = await puppeteer.launch({ headless: "new", args: ['--no-sandbox', '--disable-setuid-sandbox'] });
  const page = await browser.newPage();
  await page.setViewport({ width: 1280, height: 1200 });

  await page.goto('https://tally.so/r/0QO4oy', { waitUntil: 'networkidle2', timeout: 60000 });

  async function fill(bId, val, t='input') {
    await page.evaluate((b, v, tag) => {
      const el = document.querySelector('[data-block-id="' + b + '"]').querySelector(tag);
      const proto = Object.getPrototypeOf(el);
      Object.getOwnPropertyDescriptor(proto, 'value').set.call(el, v);
      el.dispatchEvent(new Event('input', { bubbles: true }));
      el.dispatchEvent(new Event('change', { bubbles: true }));
    }, bId, val, t);
  }

  // S1
  await fill('e20a3c9a-145e-4754-bd17-cecda6880083', 'AI-Futures-Trader');
  await fill('3b579226-d444-43bb-909d-6c3278d30230', 'https://github.com');
  await fill('80a0de6e-2fb6-4b72-b1e4-046143faaba4', 'https://github.com', 'textarea');
  await fill('3c3e5853-5c7f-4da6-a21b-4e77b9e9759a', 'Maxim Lapin');
  await fill('df4f607c-89db-420f-9bbf-ec0c5a64fc5f', 'zloilapinbs@gmail.com');
  await fill('a5748407-be09-4ebc-a315-31a7c1b041f3', 'Tg: @lazerniyyyeth ; DC: lapinzloyyy');
  await page.evaluate(() => document.querySelector('[data-block-id="9ff2397f-abe2-4d5e-8e33-c614c16498ed"] input').click());
  await page.evaluate(() => Array.from(document.querySelectorAll('button')).find(b => b.innerText.includes('Next')).click());
  await new Promise(r => setTimeout(r, 1500));

  // S2
  await fill('85ad5b0c-23a5-45c5-9fc7-3d1e2a08978a', 'Algorithmic trading terminal', 'textarea');
  await fill('6671c138-b4c5-4ddf-a7d9-2d889935d8d8', 'Live on Ink Mainnet', 'textarea');

  async function pick(bId, textSub) {
    await page.evaluate((b) => document.querySelector('[data-block-id="' + b + '"] input').click(), bId);
    await new Promise(r => setTimeout(r, 300));
    await page.evaluate((sub) => {
      const opts = Array.from(document.querySelectorAll('.tally-multi-select-list-option, [role="option"]'));
      const target = opts.find(o => o.innerText.includes(sub));
      if (target) target.click();
    }, textSub);
    await page.evaluate(() => document.body.click());
    await new Promise(r => setTimeout(r, 200));
  }
  await pick('e1fdc97f-4d4c-415b-92da-4a7c18a29fb6', 'Bots');
  await pick('5038926d-f73e-4765-a694-7e9f71eb19d1', 'Consumer');
  await pick('169cefcd-8208-4fa3-8592-280f30c9157c', 'Trading volume');
  await page.evaluate(() => document.querySelector('[data-block-id="09aaaa0e-eb28-4908-925d-12bc3d247aa7"] input').click());
  await page.evaluate(() => Array.from(document.querySelectorAll('button')).find(b => b.innerText.includes('Next')).click());
  await new Promise(r => setTimeout(r, 1500));

  // Inspect S3 multi-selects or inputs
  async function getOptions(bId) {
    await page.evaluate((b) => {
      const inp = document.querySelector('[data-block-id="' + b + '"] input');
      if (inp) inp.click();
    }, bId);
    await new Promise(r => setTimeout(r, 400));
    const opts = await page.evaluate(() => {
      return Array.from(document.querySelectorAll('.tally-multi-select-list-option, [role="option"]')).map(o => o.innerText.trim());
    });
    await page.evaluate(() => document.body.click());
    return opts;
  }

  const s3IntegrateOpts = await getOptions('986e6318-24a8-48a9-be04-2c890cb1214a');
  console.log('s3IntegrateOpts:', s3IntegrateOpts);

  const s3SupportOpts = await getOptions('f6912c20-8eed-4cd8-9ced-b19a71af3ed8');
  console.log('s3SupportOpts:', s3SupportOpts);

  const s3Inputs = await page.evaluate(() => {
    return [
      '11dbdae5-9299-4518-92e7-5b48682efef7',
      '3d1055c6-cde5-4129-9cb8-5be8bdb12b6f',
      '2e060550-730a-4211-a6a2-508dbbd30b6c',
      'ea4dd79b-845c-489a-9691-577b2283f477'
    ].map(id => {
      const b = document.querySelector('[data-block-id="' + id + '"]');
      const inp = b?.querySelector('input, textarea');
      return {
        id,
        tag: inp?.tagName,
        type: inp?.type,
        placeholder: inp?.placeholder
      };
    });
  });
  console.log('s3Inputs:', s3Inputs);

  await browser.close();
}

inspectSec3Elements().catch(e => console.error(e));

