import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { createRequire } from 'node:module';
import { pathToFileURL } from 'node:url';
import { PROJECT_ROOT, loadProject, resolveInside, sha256, writeJson } from './lib/project.mjs';

const require = createRequire(import.meta.url);
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const project = loadProject(PROJECT_ROOT);
const output = resolveInside(PROJECT_ROOT, project.manifest.output);
const base = pathToFileURL(output).href;
const visualDir = path.join(PROJECT_ROOT, 'reports', 'visual', 'latest');
fs.mkdirSync(visualDir, { recursive: true });
const report = { generatedAt: new Date().toISOString(), buildSha256: sha256(fs.readFileSync(output)), checks: [], screenshots: [] };
const visualReport = { generatedAt: report.generatedAt, buildSha256: report.buildSha256, browser: 'Playwright / Microsoft Edge', results: [] };
const browser = await chromium.launch({ channel: process.env.BROWSER_CHANNEL || 'msedge', headless: true });
const fullAudit = process.argv.includes('--full');
const routes = [{ key: 'home', hash: '#/', scopeId: 'view-home' }, ...project.modules.map(({ config }) => ({ key: config.key, hash: config.route, scopeId: config.scopeId }))].filter(route => fullAudit || ['harness', 'codex-harness', 'dsh'].includes(route.key));
const viewports = [{ name: 'desktop', width: 1440, height: 900 }, { name: 'tablet', width: 1024, height: 768 }, { name: 'mobile', width: 390, height: 844 }];
const errors = [];

async function capture(page, filename) {
  await page.evaluate(async () => {
    await document.fonts.ready;
    await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
  });
  await page.screenshot({ path: path.join(visualDir, filename), animations: 'disabled' });
  report.screenshots.push(`reports/visual/latest/${filename}`);
}

async function check(name, run) {
  try {
    await run();
    report.checks.push({ name, pass: true });
    console.log(`PASS ${name}`);
  } catch (error) {
    report.checks.push({ name, pass: false, error: error.message });
    throw error;
  }
}

async function expectScope(page, id) {
  await page.waitForFunction(expected => {
    const visible = [...document.querySelectorAll('#view-home, .module')].filter(el => el.checkVisibility());
    return visible.length === 1 && visible[0].id === expected;
  }, id);
}

async function expectSection(page, id) {
  await page.waitForFunction(expected => {
    const target = document.getElementById(expected);
    const top = target.getBoundingClientRect().top;
    const bar = document.getElementById('topbar').getBoundingClientRect();
    return document.querySelector('.toc-link.active')?.dataset.target === expected && top >= bar.bottom - 2 && top < bar.bottom + 50;
  }, id);
}

async function expectNoOverflow(page) {
  const dimensions = await page.evaluate(() => ({ width: document.documentElement.clientWidth, scroll: Math.max(document.documentElement.scrollWidth, document.body.scrollWidth) }));
  assert.ok(dimensions.scroll <= dimensions.width + 1, JSON.stringify(dimensions));
}

try {
  for (const viewport of viewports) {
    const context = await browser.newContext({ viewport, reducedMotion: 'reduce' });
    const page = await context.newPage();
    page.on('pageerror', error => errors.push(error.message));
    await check(`route and screenshot matrix / ${viewport.name}`, async () => {
      for (const route of routes) {
        await page.goto(base + route.hash, { timeout: 90000 });
        if (await page.locator('#resumeDlg.open').count()) await page.locator('#resumeNo').click();
        await expectScope(page, route.scopeId);
        await expectNoOverflow(page);
        await page.evaluate(() => window.scrollTo({ top: 0, behavior: 'instant' }));
        await capture(page, `${route.key}-${viewport.name}.png`);
        visualReport.results.push({ route: route.key, viewport: viewport.name, file: `reports/visual/latest/${route.key}-${viewport.name}.png`, ok: true });
      }
    });
    await context.close();
  }

  const context = await browser.newContext({ viewport: viewports[0], reducedMotion: 'reduce' });
  const page = await context.newPage();
  page.on('pageerror', error => errors.push(error.message));
  await page.goto(base + '#/', { timeout: 90000 });

  await check('home to Harness to Codex to chapter', async () => {
    await page.locator('#view-home a[href="#/harness"]').click();
    await expectScope(page, 'mod-harness');
    assert.equal(await page.locator('#mod-harness .harness-mark').count(), 0);
    await page.locator('#mod-harness a[href="#/codex-harness"]').click();
    await expectScope(page, 'mod-codex-harness');
    assert.equal(await page.locator('#mod-codex-harness .harness-module-grid .home-card').count(), 12);
    await page.locator('#mod-codex-harness .home-card[href$="cx-context"]').click();
    await expectSection(page, 'cx-context');
    assert.equal(await page.evaluate(() => document.activeElement.id), 'cx-context');
  });
  await check('repeat chapter link, deep-link reload and browser back', async () => {
    await page.evaluate(() => window.scrollBy({ top: 500, behavior: 'instant' }));
    await page.locator('#toc-list a[data-target="cx-context"]').click();
    await expectSection(page, 'cx-context');
    await page.reload({ timeout: 90000 });
    await expectSection(page, 'cx-context');
    await page.locator('#toc-list a[data-target="cx-tools"]').click();
    await expectSection(page, 'cx-tools');
    await page.goBack();
    await expectSection(page, 'cx-context');
  });
  await check('directory return and keyboard TOC', async () => {
    await page.locator('#cx-context .cx-chapter-nav a[href$="cx-home"]').click();
    await expectSection(page, 'cx-home');
    const link = page.locator('#toc-list a[data-target="cx-history"]');
    await link.focus();
    await page.keyboard.press('Enter');
    await expectSection(page, 'cx-history');
  });
  await check('chapter pager updates the route and survives reload', async () => {
    await page.keyboard.press('ArrowRight');
    await expectSection(page, 'cx-compact');
    assert.ok(page.url().endsWith('#/codex-harness?section=cx-compact'));
    await page.reload({ timeout: 90000 });
    await expectSection(page, 'cx-compact');
    await page.keyboard.press('ArrowLeft');
    await expectSection(page, 'cx-history');
    assert.ok(page.url().endsWith('#/codex-harness?section=cx-history'));
  });
  await check('search opens collapsed content and clears on navigation', async () => {
    await page.locator('#search').fill('旧配置');
    await page.waitForFunction(() => document.querySelector('#cx-context-question').open && document.querySelector('#cx-context-question mark.hit'));
    await page.locator('#search').fill('no-such-harness-term-98765');
    await page.waitForFunction(() => document.querySelector('#searchStatus').textContent.includes('无结果'));
    await page.locator('#search').fill('StepContext');
    await page.waitForSelector('#mod-codex-harness mark.hit');
    await page.locator('#tocParent').click();
    await expectScope(page, 'mod-harness');
    assert.equal(await page.locator('#search').inputValue(), '');
    assert.equal(await page.locator('mark.hit').count(), 0);
  });
  await check('DSH directory, original chapters and search', async () => {
    await page.locator('#mod-harness a[href="#/dsh"]').click();
    assert.equal(await page.locator('#mod-dsh .harness-module-grid .home-card').count(), 13);
    await page.locator('#mod-dsh .home-card[href$="s7"]').click();
    await expectSection(page, 's7');
    const head = page.locator('#s7 .card-head').first();
    const card = head.locator('..');
    const wasOpen = await card.evaluate(el => el.classList.contains('open'));
    await head.click();
    assert.notEqual(await card.evaluate(el => el.classList.contains('open')), wasOpen);
    await page.locator('#search').fill('生命周期');
    await page.waitForSelector('#mod-dsh .harness-scope mark.hit');
    await page.locator('#tocParent').click();
    await expectScope(page, 'mod-harness');
  });
  await check('copy returns exact code and diagram opens by keyboard', async () => {
    await page.locator('#mod-harness a[href="#/codex-harness"]').click();
    await page.locator('#toc-list a[data-target="cx-loop"]').click();
    await page.evaluate(() => Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: async text => { window.__copiedCode = text; } } }));
    await page.locator('#cx-loop .copy-btn').first().click();
    assert.equal(await page.evaluate(() => window.__copiedCode), await page.locator('#cx-loop pre').first().innerText());
    const diagram = page.locator('.cx-diagram-open');
    await diagram.focus();
    await page.keyboard.press('Enter');
    await page.waitForSelector('#lightbox.open');
    assert.equal(await page.locator('#lightbox img').evaluate(img => img.complete && img.naturalWidth === 1120), true);
    await capture(page, 'codex-diagram-lightbox-desktop.png');
    await page.keyboard.press('Escape');
    assert.equal(await page.locator('#lightbox.open').count(), 0);
    assert.equal(await diagram.evaluate(el => el === document.activeElement), true);
  });
  await check('desktop, tablet and mobile chapter layout in three themes', async () => {
    for (const viewport of viewports) {
      await page.setViewportSize(viewport);
      for (const theme of ['dark', 'light', 'sepia']) {
        await page.locator(`.theme-btn[data-theme-val="${theme}"]`).click();
        await page.evaluate(() => { location.hash = '#/codex-harness?section=cx-context'; });
        if (viewport.width <= 820) {
          await page.locator('#menuBtn').click();
          await page.locator('#toc-list a[data-target="cx-context"]').click();
        } else await page.locator('#toc-list a[data-target="cx-context"]').click();
        await expectSection(page, 'cx-context');
        await expectNoOverflow(page);
        assert.equal(await page.locator('#topbar').evaluate(el => Math.round(el.getBoundingClientRect().top)), 0);
        await capture(page, `codex-context-${theme}-${viewport.name}.png`);
      }
    }
    await page.reload({ timeout: 90000 });
    assert.equal(await page.locator('html').getAttribute('data-theme'), 'sepia');
  });
  await check('Python examples copy exactly and Rust references open by keyboard', async () => {
    await page.setViewportSize(viewports[0]);
    await page.locator('#toc-list a[data-target="cx-context"]').click();
    await expectSection(page, 'cx-context');
    const snippets = page.locator('#mod-codex-harness code[data-language="python"]');
    assert.equal(await snippets.count(), 15);
    await page.evaluate(() => Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: async text => { window.__copiedCode = text; } } }));
    for (const snippet of await snippets.all()) {
      const wrapper = snippet.locator('..').locator('..');
      await wrapper.locator('.copy-btn').click();
      assert.equal(await page.evaluate(() => window.__copiedCode), await snippet.innerText());
    }
    for (const reference of await page.locator('.cx-rust-reference').all()) {
      await reference.evaluate(el => { el.open = false; });
      await reference.locator('summary').focus();
      await page.keyboard.press('Enter');
      assert.equal(await reference.evaluate(el => el.open), true);
      await reference.locator('.copy-btn').click();
      assert.equal(await page.evaluate(() => window.__copiedCode), await reference.locator('pre').innerText());
      await reference.locator('summary').focus();
      await page.keyboard.press('Enter');
      assert.equal(await reference.evaluate(el => el.open), false);
    }
    await page.waitForFunction(() => !document.querySelector('#toast').classList.contains('show'));
    for (const viewport of viewports) {
      await page.setViewportSize(viewport);
      for (const example of ['context', 'tools', 'history', 'complete', 'loop-state', 'tool-errors', 'stream-order', 'process', 'retry', 'lab']) {
        const chapter = { complete: 'example', 'loop-state': 'loop', 'tool-errors': 'tools', 'stream-order': 'stream' }[example] || example;
        const section = `cx-${chapter}`;
        if (viewport.width <= 820) await page.locator('#menuBtn').click();
        await page.locator(`#toc-list a[data-target="${section}"]`).click();
        await expectSection(page, section);
        const snippet = page.locator(`code[data-example="${example}"]`);
        await snippet.evaluate(el => {
          const block = el.closest('.code-block-wrapper');
          const bar = document.getElementById('topbar');
          window.scrollBy({ top: block.getBoundingClientRect().top - bar.getBoundingClientRect().bottom - 16, behavior: 'instant' });
        });
        await expectNoOverflow(page);
        await capture(page, `codex-python-${example}-${viewport.name}.png`);
      }
      if (viewport.width <= 820) await page.locator('#menuBtn').click();
      await page.locator('#toc-list a[data-target="cx-context"]').click();
      await expectSection(page, 'cx-context');
      const reference = page.locator('#cx-context .cx-rust-reference');
      await reference.evaluate(el => {
        el.open = true;
        const bar = document.getElementById('topbar');
        window.scrollBy({ top: el.getBoundingClientRect().top - bar.getBoundingClientRect().bottom - 16, behavior: 'instant' });
      });
      await expectNoOverflow(page);
      await capture(page, `codex-rust-reference-${viewport.name}.png`);
      await reference.evaluate(el => { el.open = false; });
    }
  });
  await check('new chapters, long tables and return links on all viewports', async () => {
    for (const viewport of viewports) {
      await page.setViewportSize(viewport);
      for (const section of ['cx-stream', 'cx-process', 'cx-retry', 'cx-lab']) {
        if (viewport.width <= 820) await page.locator('#menuBtn').click();
        await page.locator(`#toc-list a[data-target="${section}"]`).click();
        await expectSection(page, section);
        assert.ok(page.url().endsWith(`section=${section}`));
        await expectNoOverflow(page);
        await capture(page, `codex-${section.slice(3)}-${viewport.name}.png`);
        await page.locator(`#${section} .cx-chapter-nav a[href$="cx-home"]`).click();
        await expectSection(page, 'cx-home');
      }
    }
  });
  await check('mobile drawer Escape and upper-level navigation', async () => {
    await page.locator('#menuBtn').click();
    assert.equal(await page.locator('#menuBtn').getAttribute('aria-expanded'), 'true');
    await capture(page, 'harness-drawer-mobile.png');
    await page.keyboard.press('Escape');
    assert.equal(await page.locator('#menuBtn').getAttribute('aria-expanded'), 'false');
    assert.equal(await page.locator('#menuBtn').evaluate(el => el === document.activeElement), true);
    await page.locator('#menuBtn').click();
    await page.locator('#tocParent').click();
    await expectScope(page, 'mod-harness');
    assert.equal(await page.locator('#menuBtn').getAttribute('aria-expanded'), 'false');
    await page.locator('#homeBtn').click();
    await expectScope(page, 'view-home');
    assert.equal(await page.evaluate(() => window.scrollY), 0);
  });
  await check('mobile diagram and search feedback', async () => {
    await page.locator('#view-home a[href="#/harness"]').click();
    await page.locator('#mod-harness a[href="#/codex-harness"]').click();
    await page.locator('#mod-codex-harness .home-card[href$="cx-loop"]').click();
    await expectSection(page, 'cx-loop');
    assert.equal(await page.locator('.cx-diagram img').evaluate(img => img.complete && img.naturalWidth === 650 && img.naturalHeight === 1120), true);
    await page.locator('.cx-diagram-open').click();
    await page.waitForSelector('#lightbox.open');
    assert.equal(await page.locator('#lightbox img').evaluate(img => img.naturalWidth), 650);
    await capture(page, 'codex-diagram-mobile.png');
    await page.keyboard.press('Escape');
    await page.locator('#search').fill('not-found-123456');
    await page.waitForFunction(() => document.querySelector('#searchStatus').textContent.includes('无结果'));
    assert.equal(await page.locator('#searchStatus').isVisible(), true);
    await expectNoOverflow(page);
    await capture(page, 'harness-search-empty-mobile.png');
  });
  if (fullAudit) await check('keyboard collapsible TOC in existing module', async () => {
    await page.setViewportSize(viewports[0]);
    await page.goto(base + '#/langgraph', { timeout: 90000 });
    await expectScope(page, 'mod-lg');
    const group = page.locator('.toc-group-title.collapsible').first();
    const expanded = await group.getAttribute('aria-expanded');
    await group.focus();
    await page.keyboard.press('Enter');
    assert.equal(await group.getAttribute('aria-expanded'), String(expanded !== 'true'));
    await page.keyboard.press(' ');
    assert.equal(await group.getAttribute('aria-expanded'), expanded);
  });
  if (fullAudit) await check('existing module chapters clear the sticky toolbar', async () => {
    for (const viewport of [viewports[0], viewports[2]]) {
      await page.setViewportSize(viewport);
      for (const route of ['book', 'langgraph', 'llm']) {
        await page.goto(base + '#/' + route, { timeout: 90000 });
        if (await page.locator('#resumeDlg.open').count()) await page.locator('#resumeNo').click();
        if (viewport.width <= 820) await page.locator('#menuBtn').click();
        const link = page.locator('#toc-list .toc-link:visible').nth(4);
        const id = await link.getAttribute('data-target');
        await link.click();
        await expectSection(page, id);
        await expectNoOverflow(page);
        await capture(page, `${route}-chapter-${viewport.name}.png`);
      }
    }
  });
  await check('no browser exceptions', async () => assert.deepEqual(errors, []));
  await context.close();
} catch (error) {
  console.error(error);
  process.exitCode = 1;
} finally {
  await browser.close();
  writeJson(path.join(PROJECT_ROOT, 'reports', 'harness-audit.json'), report);
  writeJson(path.join(visualDir, 'report.json'), visualReport);
}
