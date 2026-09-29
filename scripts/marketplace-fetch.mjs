// Facebook Marketplace search, logged out, low volume — run by `boats marketplace-fetch`.
//
// Facebook's robots.txt disallows automated access; the owner of this repo chose to run it anyway
// for personal boat shopping. Limits kept on purpose: never logged in, no captcha or login-wall
// workarounds (the run stops instead), a pause of several seconds between searches, and a cap on
// searches per run. Output has the same format as the bookmarklet, so the same importer (with the
// model-list filter) takes it in.
//
// Usage: node scripts/marketplace-fetch.mjs <queries.json> <out.json> [--city gothenburg] [--radius 500] [--max 40]
import { readFileSync, writeFileSync } from "node:fs";
import { chromium } from "playwright";

const [queriesFile, outFile, ...rest] = process.argv.slice(2);
const opt = (name, dflt) => { const i = rest.indexOf(`--${name}`); return i >= 0 ? rest[i + 1] : dflt; };
const city = opt("city", "gothenburg");
const radius = opt("radius", "500");
const max = Number(opt("max", "40"));
const queries = JSON.parse(readFileSync(queriesFile, "utf8")).slice(0, max);

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const isPrice = (s) => /^(?:gratis|free|\d[\d\s .,]*\s*(?:kr|sek|:-)?)$/i.test(s) && /\d|gratis|free/i.test(s);

const browser = await chromium.launch();
const ctx = await browser.newContext({ locale: "sv-SE", timezoneId: "Europe/Stockholm", viewport: { width: 1280, height: 1000 } });
const page = await ctx.newPage();
const items = new Map();
const log = [];
let stopped = null;

for (const [n, q] of queries.entries()) {
  const url = `https://www.facebook.com/marketplace/${city}/search/?query=${encodeURIComponent(q)}&radius=${radius}`;
  let resp;
  try {
    resp = await page.goto(url, { waitUntil: "domcontentloaded", timeout: 45000 });
  } catch (e) {
    stopped = `navigation failed on "${q}": ${e.message}`;
    break;
  }
  await sleep(3500);
  const deny = page.locator('text="Neka valfria cookies"');
  if (await deny.count()) { await deny.first().click(); await sleep(2500); }
  // Blocked, sent to a login page or a checkpoint: stop for today, don't try to get around it.
  if (resp.status() >= 400 || /\/(login|checkpoint)\b/.test(page.url()) || await page.locator("text=/captcha|säkerhetskontroll|security check/i").count()) {
    stopped = `blocked or login required on "${q}" (HTTP ${resp.status()}, ${page.url().slice(0, 80)})`;
    break;
  }
  for (let i = 0; i < 2; i++) { await page.mouse.wheel(0, 2500); await sleep(1500); }
  const cards = await page.locator('a[href*="/marketplace/item/"]').evaluateAll((els) => els.map((a) => ({
    href: a.getAttribute("href"), text: a.innerText, image: (a.querySelector("img") || {}).src || null,
  })));
  let found = 0;
  for (const c of cards) {
    const id = (c.href.match(/\/marketplace\/item\/(\d+)/) || [])[1];
    if (!id || items.has(id)) continue;
    const ls = c.text.split("\n").map((s) => s.trim()).filter(Boolean);
    const rest2 = ls.filter((s) => !isPrice(s));
    items.set(id, { id, url: `https://www.facebook.com/marketplace/item/${id}/`, price: ls.find(isPrice) || null,
                    title: rest2[0] || "", location: rest2[1] || "", image: c.image, query: q, saved: new Date().toISOString() });
    found++;
  }
  log.push({ query: q, cards: cards.length, new: found });
  console.error(`  [${n + 1}/${queries.length}] ${q}: ${cards.length} cards, ${found} new`);
  await sleep(8000 + Math.random() * 7000);  // a person's pace, not a crawler's
}
await browser.close();

writeFileSync(outFile, JSON.stringify({ source: "facebook-marketplace", page: `auto:${city}`, saved: new Date().toISOString(),
                                        stopped, log, items: [...items.values()] }, null, 1));
console.log(JSON.stringify({ queries: log.length, items: items.size, stopped }));
