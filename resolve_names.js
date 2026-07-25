#!/usr/bin/env node
/*
 * resolve_names.js — resolve every visit's placeId to Google's real place NAME,
 * with NO API key, by rendering the public place page in headless Chromium and
 * reading the resolved "/maps/place/<NAME>/…!1s<ftid>…" URL.
 *
 * This is the browser-based alternative to place_names.py (Places API). It uses the
 * placeId/cid (Google's own building-level identity) — never coordinate guessing —
 * and cross-checks that each resolved page's ftid matches the one we asked for, so a
 * merged/moved place is flagged rather than silently mislabelled.
 *
 * Prereqs:  npm install    (puppeteer-core)   +   a system Chromium/Chrome
 *           ./get_consent_cookie.sh           (writes out/consent_cookies.txt)
 * Usage:    node resolve_names.js out/Timeline-latest.json -o out/Timeline-named.json
 *
 * Cache:  out/place_cache_browser.json — resumable; re-runs cost nothing for cached
 *         places. Paced with a delay to stay polite (rendering 300+ Maps pages fast
 *         from one IP can trip Google throttling/CAPTCHA).
 */
const P = require('puppeteer-core');
const fs = require('fs');
const path = require('path');

const args = process.argv.slice(2);
const inFile = args[0];
const outFile = (args.includes('-o') ? args[args.indexOf('-o') + 1] : null);
const CHROME = process.env.CHROME_PATH || '/usr/bin/chromium-browser';
const DELAY_MS = parseInt(process.env.RESOLVE_DELAY_MS || '2500');
const LIMIT = parseInt(process.env.RESOLVE_LIMIT || '0');            // 0 = all
const NAV_TIMEOUT = parseInt(process.env.NAV_TIMEOUT_MS || '45000');  // page-load budget
const ATTEMPTS = parseInt(process.env.RESOLVE_ATTEMPTS || '2');       // retry transient timeouts
if (!inFile) { console.error('usage: node resolve_names.js <timeline.json> [-o out.json]'); process.exit(2); }

const outDir = path.dirname(outFile || inFile);
const jarFile = process.env.CONSENT_JAR || path.join(outDir, 'consent_cookies.txt');
const cacheFile = path.join(outDir, 'place_cache_browser.json');

const doc = JSON.parse(fs.readFileSync(inFile));
const cache = fs.existsSync(cacheFile) ? JSON.parse(fs.readFileSync(cacheFile)) : {};
const cookies = fs.existsSync(jarFile)
  ? fs.readFileSync(jarFile, 'utf8').split('\n').filter(l => l.includes('\t'))
      .map(l => { const p = l.split('\t'); return { name: p[5], value: p[6], domain: '.google.com', path: '/' }; })
      .filter(c => c.name && c.value && c.value.trim())
  : [];
if (!cookies.length) console.error('WARN: no consent cookies (' + jarFile + ') — run ./get_consent_cookie.sh first');

// distinct featureIds in visit order
const fids = [];
for (const s of doc.semanticSegments) {
  const fid = s.visit && s.visit.topCandidate && s.visit.topCandidate.featureId;
  if (fid && !fids.includes(fid)) fids.push(fid);
}
// (re)resolve if uncached OR a pre-address-schema entry (no 'category' field yet)
let todo = fids.filter(f => !cache[f] || cache[f].category === undefined);
if (LIMIT) todo = todo.slice(0, LIMIT);
console.error(`${fids.length} distinct places; ${Object.keys(cache).length} cached; resolving ${todo.length}`);

const cidOf = fid => BigInt(fid.split(':')[1]).toString();
const sleep = ms => new Promise(r => setTimeout(r, ms));

(async () => {
  const browser = await P.launch({ executablePath: CHROME, headless: 'new',
    args: ['--no-sandbox', '--disable-gpu', '--disable-dev-shm-usage', '--lang=en-US'] });
  const pg = await browser.newPage();
  for (const c of cookies) { try { await pg.setCookie(c); } catch (e) {} }

  let done = 0;
  for (const fid of todo) {
    const cid = cidOf(fid);
    const rec = { name: null, ftidMatch: false };
    for (let attempt = 1; attempt <= ATTEMPTS; attempt++) {
    try {
      await pg.goto(`https://www.google.com/maps?cid=${cid}&hl=en&gl=US`, { waitUntil: 'networkidle2', timeout: NAV_TIMEOUT });
      await sleep(3000);
      const u = pg.url();
      rec.ftidMatch = u.toLowerCase().includes(fid.toLowerCase());
      // name from the resolved URL path (stable); proper address + category from the DOM panel
      const m = u.match(/maps\/place\/([^/@]+)/);
      const urlName = m ? decodeURIComponent(m[1].replace(/\+/g, ' ')).replace(/,\s*$/, '').split(',')[0] : null;
      const dom = await pg.evaluate(() => {
        const t = s => { const e = document.querySelector(s); return e ? (e.getAttribute('aria-label') || e.textContent || '').trim() : null; };
        return { h1: t('h1'), addr: t('button[data-item-id="address"]'), cat: t('button[jsaction*="category"]') };
      });
      rec.name = urlName || dom.h1;
      rec.address = dom.addr ? dom.addr.replace(/^Address:\s*/i, '') : null;   // proper formatted address
      rec.category = dom.cat || null;
      if (!rec.name && /consent|before you continue/i.test(await pg.title()))
        rec.error = 'consent-wall (refresh cookie)';
    } catch (e) { rec.error = e.message.slice(0, 60); }
      // retry only transient failures; a resolved (or genuinely nameless) place is final
      if (rec.name || !/timeout|timed out|ERR_ABORTED|ERR_NETWORK/i.test(rec.error || '')) break;
      if (attempt < ATTEMPTS) await sleep(3000);
    }
    cache[fid] = rec;
    if (++done % 10 === 0 || done === todo.length) {
      fs.writeFileSync(cacheFile, JSON.stringify(cache, null, 1));
      console.error(`  ${done}/${todo.length}`);
    }
    await sleep(DELAY_MS);
  }
  fs.writeFileSync(cacheFile, JSON.stringify(cache, null, 1));
  await browser.close();

  // merge names into export
  let named = 0;
  for (const s of doc.semanticSegments) {
    const tc = s.visit && s.visit.topCandidate;
    if (!tc || !tc.featureId) continue;
    const r = cache[tc.featureId];
    if (r && r.name) { tc.placeName = r.name; if (r.address) tc.placeAddress = r.address; if (r.category) tc.placeCategory = r.category; named++; }
  }
  if (outFile) { fs.writeFileSync(outFile, JSON.stringify(doc, null, 2)); console.error(`wrote ${outFile}: ${named} visits named`); }
  const bad = Object.values(cache).filter(r => r && !r.ftidMatch && r.name).length;
  if (bad) console.error(`note: ${bad} places resolved to a different ftid (merged/moved) — kept but flag ftidMatch=false`);
})().catch(e => { console.error('FATAL', e.message); process.exit(1); });
