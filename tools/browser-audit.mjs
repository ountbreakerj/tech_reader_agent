import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { spawn, spawnSync } from 'node:child_process';
import { pathToFileURL } from 'node:url';
import { PROJECT_ROOT, loadProject, resolveInside, writeJson } from './lib/project.mjs';

const project = loadProject(PROJECT_ROOT);
const output = resolveInside(PROJECT_ROOT, project.manifest.output);
if (!fs.existsSync(output)) throw new Error('找不到构建产物，请先运行 node build.mjs');

const candidates = process.platform === 'win32' ? [
  path.join(process.env['PROGRAMFILES(X86)'] || '', 'Microsoft', 'Edge', 'Application', 'msedge.exe'),
  path.join(process.env.PROGRAMFILES || '', 'Microsoft', 'Edge', 'Application', 'msedge.exe'),
  path.join(process.env.PROGRAMFILES || '', 'Google', 'Chrome', 'Application', 'chrome.exe'),
  path.join(process.env.LOCALAPPDATA || '', 'Google', 'Chrome', 'Application', 'chrome.exe'),
] : ['google-chrome', 'chromium', 'chromium-browser'];
const browser = candidates.find((candidate) => candidate && (path.isAbsolute(candidate) ? fs.existsSync(candidate) : true));
if (!browser) throw new Error('未找到 Edge/Chrome，无法执行浏览器审计');

const sleep = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));
const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'tech-reader-browser-audit-'));
const browserProcess = spawn(browser, [
  '--headless=new',
  '--disable-gpu',
  '--disable-background-networking',
  '--remote-debugging-port=0',
  `--user-data-dir=${profile}`,
  'about:blank',
], { stdio: 'ignore', windowsHide: true });

async function waitForPort() {
  const portFile = path.join(profile, 'DevToolsActivePort');
  for (let attempt = 0; attempt < 100; attempt += 1) {
    if (fs.existsSync(portFile)) return Number(fs.readFileSync(portFile, 'utf8').split(/\r?\n/)[0]);
    // Edge on Windows may hand the browser window to a relaunched child and
    // exit its initial process with code 0. Keep waiting for DevToolsActivePort
    // in that case; a non-zero exit still indicates a genuine launch failure.
    if (browserProcess.exitCode !== null && browserProcess.exitCode !== 0) throw new Error(`浏览器提前退出: ${browserProcess.exitCode}`);
    await sleep(50);
  }
  throw new Error('等待浏览器调试端口超时');
}

function createCdp(webSocketUrl) {
  const socket = new WebSocket(webSocketUrl);
  const pending = new Map();
  const listeners = new Map();
  let nextId = 1;
  socket.addEventListener('message', (event) => {
    const message = JSON.parse(event.data);
    if (message.id) {
      const request = pending.get(message.id);
      if (!request) return;
      pending.delete(message.id);
      if (message.error) request.reject(new Error(message.error.message));
      else request.resolve(message.result);
      return;
    }
    for (const listener of listeners.get(message.method) || []) listener(message.params);
  });
  return {
    ready: new Promise((resolve, reject) => {
      socket.addEventListener('open', resolve, { once: true });
      socket.addEventListener('error', reject, { once: true });
    }),
    send(method, params = {}) {
      const id = nextId++;
      return new Promise((resolve, reject) => {
        pending.set(id, { resolve, reject });
        socket.send(JSON.stringify({ id, method, params }));
      });
    },
    on(method, listener) {
      const group = listeners.get(method) || [];
      group.push(listener);
      listeners.set(method, group);
    },
    close() { socket.close(); },
  };
}

const viewports = [
  { name: 'desktop', width: 1440, height: 900 },
  { name: 'tablet', width: 1024, height: 768 },
  { name: 'mobile', width: 390, height: 844 },
];
const routes = [
  { key: 'home', hash: '#/', scopeId: 'view-home' },
  ...project.modules.map(({ config }) => ({ key: config.key, hash: config.route, scopeId: config.scopeId })),
];
const report = { generatedAt: new Date().toISOString(), browser, results: [] };

try {
  const port = await waitForPort();
  const createResponse = await fetch(`http://127.0.0.1:${port}/json/new?about:blank`, { method: 'PUT' });
  if (!createResponse.ok) throw new Error(`创建浏览器标签页失败: ${createResponse.status}`);
  const target = await createResponse.json();
  const cdp = createCdp(target.webSocketDebuggerUrl);
  await cdp.ready;
  const exceptions = [];
  cdp.on('Runtime.exceptionThrown', (event) => exceptions.push(event.exceptionDetails?.text || '未捕获异常'));
  await Promise.all([cdp.send('Page.enable'), cdp.send('Runtime.enable'), cdp.send('Log.enable')]);
  for (const viewport of viewports) {
    await cdp.send('Emulation.setDeviceMetricsOverride', {
      width: viewport.width,
      height: viewport.height,
      deviceScaleFactor: 1,
      mobile: viewport.name === 'mobile',
    });
    for (const route of routes) {
      exceptions.length = 0;
      const url = `${pathToFileURL(output).href}${route.hash}`;
      await cdp.send('Page.navigate', { url });
      for (let attempt = 0; attempt < 60; attempt += 1) {
        const state = await cdp.send('Runtime.evaluate', { returnByValue: true, expression: 'document.readyState' });
        if (state.result.value === 'complete') break;
        await sleep(250);
      }
      await sleep(300);
      const evaluation = await cdp.send('Runtime.evaluate', {
        returnByValue: true,
        expression: `(() => {
          const visible = (element) => {
            for (let current = element; current; current = current.parentElement) {
              if (current.hidden || getComputedStyle(current).display === 'none' || getComputedStyle(current).visibility === 'hidden') return false;
            }
            return Boolean(element);
          };
          const activeScopes = ${JSON.stringify(routes.map(route => route.scopeId))}.filter(id => visible(document.getElementById(id)));
          const root = document.documentElement;
          const body = document.body;
          const target = document.getElementById(${JSON.stringify(route.scopeId)});
          const search = document.querySelector('input[type="search"], input[placeholder*="搜索"]');
          return {
            readyState: document.readyState,
            title: document.title,
            activeScopes,
            expectedVisible: visible(target),
            clientWidth: root.clientWidth,
            scrollWidth: Math.max(root.scrollWidth, body ? body.scrollWidth : 0),
            horizontalOverflow: Math.max(root.scrollWidth, body ? body.scrollWidth : 0) > root.clientWidth + 1,
            overflowElements: Array.from(document.querySelectorAll('*')).filter(el => el.scrollWidth > el.clientWidth + 1).slice(0, 12).map(el => ({
              tag: el.tagName.toLowerCase(), id: el.id || null, className: typeof el.className === 'string' ? el.className.slice(0, 100) : null,
              clientWidth: el.clientWidth, scrollWidth: el.scrollWidth
            })),
            rightmostElements: Array.from(document.querySelectorAll('*')).map(el => { const rect = el.getBoundingClientRect(); return { el, right: rect.right, left: rect.left, width: rect.width }; }).filter(item => item.right > root.clientWidth + 1).sort((a,b) => b.right - a.right).slice(0, 12).map(item => ({ tag: item.el.tagName.toLowerCase(), id: item.el.id || null, className: typeof item.el.className === 'string' ? item.el.className.slice(0, 100) : null, right: Math.round(item.right), left: Math.round(item.left), width: Math.round(item.width) })),
            headings: target ? target.querySelectorAll('h1,h2,h3').length : 0,
            searchPresent: Boolean(search),
            homeControlPresent: Boolean(document.querySelector('[href="#/"], [data-route="#/"], #homeBtn, .home-btn'))
          };
        })()`,
      });
      const value = evaluation.result.value;
      const checks = {
        ready: value.readyState === 'complete',
        expectedRouteVisible: value.expectedVisible && value.activeScopes.length === 1,
        noHorizontalOverflow: !value.horizontalOverflow,
        noUncaughtExceptions: exceptions.length === 0,
        searchPresent: value.searchPresent,
        homeControlPresent: route.key === 'home' || value.homeControlPresent,
      };
      report.results.push({ route: route.key, viewport: viewport.name, url, checks, measurements: value, exceptions: [...exceptions], pass: Object.values(checks).every(Boolean) });
    }
  }
  cdp.close();
} finally {
  browserProcess.kill();
  // Edge may relaunch the initial process on Windows. Ask the OS to close
  // the whole process tree before removing the temporary profile.
  if (process.platform === 'win32' && browserProcess.pid) {
    spawnSync('taskkill', ['/PID', String(browserProcess.pid), '/T', '/F'], { stdio: 'ignore' });
  }
  if (browserProcess.exitCode === null) {
    await Promise.race([
      new Promise((resolve) => browserProcess.once('exit', resolve)),
      sleep(3000),
    ]);
  }
  try {
    fs.rmSync(profile, { recursive: true, force: true, maxRetries: 12, retryDelay: 250 });
  } catch (error) {
    // A renderer can hold a Chromium cache file briefly after taskkill. The
    // audit result is still valid; leave the disposable temp profile for the
    // OS cleanup service instead of turning a passed audit into a failure.
    if (error?.code !== 'EPERM' && error?.code !== 'EBUSY') throw error;
  }
}

const reportPath = path.join(PROJECT_ROOT, 'reports', 'browser-audit.json');
writeJson(reportPath, report);
const failed = report.results.filter((result) => !result.pass);
if (failed.length) {
  console.error(`浏览器审计发现 ${failed.length} 个失败视口:`);
  for (const result of failed) {
    const problems = Object.entries(result.checks).filter(([, pass]) => !pass).map(([name]) => name);
    console.error(`- ${result.route}/${result.viewport}: ${problems.join(', ')}`);
  }
  process.exitCode = 1;
} else {
  console.log(`浏览器审计通过: ${report.results.length} 个路由/视口组合`);
}
