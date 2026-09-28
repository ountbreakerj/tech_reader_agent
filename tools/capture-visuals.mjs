import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import { pathToFileURL } from 'node:url';
import { PROJECT_ROOT, loadProject, resolveInside, writeJson } from './lib/project.mjs';

const project = loadProject(PROJECT_ROOT);
const output = resolveInside(PROJECT_ROOT, project.manifest.output);
if (!fs.existsSync(output)) throw new Error('找不到产物，请先运行 node build.mjs');

const candidates = process.platform === 'win32' ? [
  path.join(process.env['PROGRAMFILES(X86)'] || '', 'Microsoft', 'Edge', 'Application', 'msedge.exe'),
  path.join(process.env.PROGRAMFILES || '', 'Microsoft', 'Edge', 'Application', 'msedge.exe'),
  path.join(process.env.PROGRAMFILES || '', 'Google', 'Chrome', 'Application', 'chrome.exe'),
  path.join(process.env.LOCALAPPDATA || '', 'Google', 'Chrome', 'Application', 'chrome.exe'),
] : ['google-chrome', 'chromium', 'chromium-browser'];
const browser = candidates.find((candidate) => candidate && (path.isAbsolute(candidate) ? fs.existsSync(candidate) : true));
if (!browser) throw new Error('未找到 Edge/Chrome，无法生成视觉审计截图');

const requestedRoute = process.argv.find((arg) => arg.startsWith('--route='))?.slice('--route='.length);
const routes = [{ key: 'home', hash: '#/' }, ...project.modules.map(({ config }) => ({ key: config.key, hash: config.route }))]
  .filter((route) => !requestedRoute || route.key === requestedRoute);
if (!routes.length) throw new Error(`未知 route: ${requestedRoute}`);
const viewports = [
  { name: 'desktop', width: 1440, height: 900 },
  { name: 'tablet', width: 1024, height: 768 },
  { name: 'mobile', width: 390, height: 844 },
];
const reportDir = path.join(PROJECT_ROOT, 'reports', 'visual', 'latest');
fs.mkdirSync(reportDir, { recursive: true });
const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'tech-reader-visual-'));
const results = [];
try {
  for (const route of routes) {
    for (const viewport of viewports) {
      const screenshot = path.join(reportDir, `${route.key}-${viewport.name}.png`);
      const url = `${pathToFileURL(output).href}${route.hash}`;
      const args = [
        '--headless=new',
        '--disable-gpu',
        '--disable-background-networking',
        '--hide-scrollbars',
        '--virtual-time-budget=2500',
        `--user-data-dir=${profile}`,
        `--window-size=${viewport.width},${viewport.height}`,
        `--screenshot=${screenshot}`,
        url,
      ];
      const run = spawnSync(browser, args, { encoding: 'utf8', timeout: 30000, windowsHide: true });
      const exists = fs.existsSync(screenshot) && fs.statSync(screenshot).size > 0;
      results.push({ route: route.key, viewport: viewport.name, file: path.relative(PROJECT_ROOT, screenshot).replaceAll('\\', '/'), ok: run.status === 0 && exists, status: run.status, error: run.stderr?.trim() || null });
      if (!exists) console.error(`${route.key}/${viewport.name} 截图失败: ${run.stderr || run.stdout}`);
    }
  }
} finally {
  // Edge may keep a short-lived child process alive after the screenshot
  // command returns, leaving the disposable profile locked on Windows.
  // The screenshots are already complete, so cleanup failure must not turn a
  // successful visual audit into a false negative.
  try {
    fs.rmSync(profile, { recursive: true, force: true, maxRetries: 12, retryDelay: 250 });
  } catch (error) {
    if (error?.code !== 'EPERM' && error?.code !== 'EBUSY') throw error;
    console.warn(`视觉审计临时目录仍被浏览器占用，已保留待系统回收: ${profile}`);
  }
}
writeJson(path.join(reportDir, 'report.json'), { generatedAt: new Date().toISOString(), browser, results });
if (results.some((result) => !result.ok)) process.exitCode = 1;
else console.log(`视觉审计截图完成: ${results.length} 张，目录 ${reportDir}`);
