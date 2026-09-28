import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { createRequire } from 'node:module';
import { pathToFileURL } from 'node:url';
import { PROJECT_ROOT, loadProject, sha256, writeJson } from './lib/project.mjs';

const require = createRequire(import.meta.url);
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const project = loadProject(PROJECT_ROOT);
const output = path.join(PROJECT_ROOT, project.manifest.output);
const base = pathToFileURL(output).href;
const screenshots = path.join(PROJECT_ROOT, 'reports/visual/latest');
fs.mkdirSync(screenshots, { recursive: true });
const report = { buildSha256: sha256(fs.readFileSync(output)), checks: [], screenshots: [] };
const browser = await chromium.launch({ channel: 'msedge', headless: true });
const errors = [];
const viewports = [{ name: 'desktop', width: 1440, height: 900 }, { name: 'tablet', width: 1024, height: 768 }, { name: 'mobile', width: 390, height: 844 }];

async function capture(page, name) {
  await page.evaluate(async () => {
    await document.fonts.ready;
    await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
  });
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), true, name);
  await page.screenshot({ path: path.join(screenshots, name + '.png'), animations: 'disabled' });
  report.screenshots.push(name + '.png');
}

async function section(page, id) {
  await page.waitForFunction(target => {
    const node = document.getElementById(target);
    const top = node.getBoundingClientRect().top;
    const bottom = document.getElementById('topbar').getBoundingClientRect().bottom;
    return node.checkVisibility() && top >= bottom - 2 && top < bottom + 55 && document.querySelector('.toc-link.active')?.dataset.target === target;
  }, id);
}

async function openParent(page, viewport) {
  if (viewport.width <= 820) await page.locator('#menuBtn').click();
  await page.locator('#tocParent').click();
}

try {
  for (const viewport of viewports) {
    const context = await browser.newContext({ viewport, reducedMotion: 'reduce' });
    const page = await context.newPage();
    page.setDefaultTimeout(12000);
    page.on('pageerror', error => errors.push(error.message));
    await page.goto(base, { timeout: 90000 });
    const routes = [{ key: 'home', route: '#/', scopeId: 'view-home' }, ...project.modules.map(item => item.config)];
    for (const route of routes) {
      await page.evaluate(hash => { location.hash = hash; }, route.route);
      await page.locator('#' + route.scopeId).waitFor({ state: 'visible' });
      await page.evaluate(() => scrollTo({ top: 0, behavior: 'instant' }));
      await capture(page, `${route.key}-${viewport.name}`);
    }

    for (const item of [
      { route: '#/llm', home: 'llm-home', group: 'llm-menu-training', chapter: 'llm-alignment' },
      { route: '#/langgraph', home: 'lg-home', group: 'lg-menu-core', chapter: 's3-5' },
    ]) {
      await page.evaluate(hash => { location.hash = hash; }, item.route);
      const home = page.locator('#' + item.home);
      await home.waitFor({ state: 'visible' });
      await home.locator(`a[href$="section=${item.group}"]`).click();
      await section(page, item.group);
      assert.equal(await home.isVisible(), false);
      await capture(page, `${item.group}-${viewport.name}`);
      await page.locator(`#${item.group} a[href$="section=${item.chapter}"]`).click();
      await section(page, item.chapter);
      assert.equal(await page.locator('[data-learning-menu]:visible').count(), 0);
      await capture(page, `${item.chapter}-${viewport.name}`);
      await openParent(page, viewport);
      await section(page, item.group);
      await page.goBack();
      await section(page, item.chapter);
      if (viewport.name === 'desktop') {
        await page.reload({ timeout: 90000 });
        await section(page, item.chapter);
      }
      await openParent(page, viewport);
      await section(page, item.group);
      await openParent(page, viewport);
      await section(page, item.home);
      report.checks.push(`${viewport.name}: ${item.route} cards, chapter, parent, history`);
    }

    await page.evaluate(() => { location.hash = '#/llm?section=llm-alignment'; });
    await section(page, 'llm-alignment');
    for (const target of ['llm-classic-arch', 'llm-distributed', 'llm-alignment']) {
      await page.evaluate(id => { location.hash = '#/llm?section=' + id; }, target);
      await section(page, target);
      const links = page.locator('#' + target + ' .nav a');
      assert.equal(await links.count(), 5);
      for (const link of await links.all()) assert.ok((await link.getAttribute('href')).startsWith('#/llm?section='));
      await capture(page, `${target}-nav-${viewport.name}`);
    }
    if (viewport.width <= 820) await page.locator('#menuBtn').click();
    const parent = page.locator('#toc-list > .toc-group-title').filter({ hasText: 'LLM训练教程' });
    await parent.focus();
    await page.keyboard.press('Enter');
    assert.equal(await parent.getAttribute('aria-expanded'), 'false');
    await page.keyboard.press(' ');
    assert.equal(await parent.getAttribute('aria-expanded'), 'true');
    assert.equal(await page.locator('a[data-target="llm-alignment"]').isVisible(), true);
    await capture(page, `learning-toc-${viewport.name}`);
    if (viewport.width <= 820) {
      await page.keyboard.press('Escape');
      assert.equal(await page.locator('#menuBtn').getAttribute('aria-expanded'), 'false');
    }
    await page.locator('#search').fill('DPO');
    await page.waitForFunction(() => document.querySelector('mark.hit-cur')?.checkVisibility());
    await page.locator('#search').fill('');
    await page.locator('mark.hit').first().waitFor({ state: 'detached' });
    await page.evaluate(() => { location.hash = '#/llm?section=llm-home'; });
    await section(page, 'llm-home');
    const llmTitles = await page.locator('#llm-home h3').allTextContents();
    assert.ok(llmTitles.includes('⛓️ LangChain & LangGraph'));
    assert.ok(llmTitles.includes('🧠 LLM训练教程'));
    assert.ok(!llmTitles.some(title => title.includes('LangChain 与 LLM')));
    if (viewport.name === 'desktop') {
      for (const theme of ['light', 'sepia']) {
        await page.locator(`[data-theme-val="${theme}"]`).click();
        await capture(page, `llm-menu-${theme}`);
      }
      await page.locator('[data-theme-val="dark"]').click();
      await page.locator('#nextCh').click();
      await section(page, 'llm-menu-databases');
      await page.locator('#nextCh').click();
      await section(page, 'llm-menu-langchain');
      await page.locator('#prevCh').click();
      await section(page, 'llm-menu-databases');
    }
    const broken = await page.evaluate(() => [...document.querySelectorAll('[data-learning-menu] a[href*="?section="]')].filter(a => !document.getElementById(new URLSearchParams(a.hash.split('?')[1]).get('section'))).map(a => a.hash));
    assert.deepEqual(broken, []);
    report.checks.push(`${viewport.name}: keyboard, nested expansion, search, original categories, links`);
    console.log(`PASS ${viewport.name}`);
    await context.close();
  }
  assert.deepEqual(errors, []);
} finally {
  await browser.close();
  writeJson(path.join(PROJECT_ROOT, 'reports/learning-menu-audit.json'), report);
}
console.log(`PASS ${report.checks.length} workflow checks, ${report.screenshots.length} screenshots`);
