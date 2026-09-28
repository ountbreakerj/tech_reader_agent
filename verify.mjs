import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import {
  PROJECT_ROOT,
  collectProjectSourceFiles,
  detectImageMime,
  flattenToc,
  loadProject,
  readJson,
  readProjectFile,
  resolveInside,
  sha256,
} from './tools/lib/project.mjs';

const project = loadProject(PROJECT_ROOT);
const outputPath = resolveInside(PROJECT_ROOT, project.manifest.output);
if (!fs.existsSync(outputPath)) throw new Error('找不到构建产物，请先运行 node build.mjs');
const outputBuffer = fs.readFileSync(outputPath);
const output = outputBuffer.toString('utf8');
const failures = [];
const notes = [];

function check(condition, message) {
  if (!condition) failures.push(message);
}

function sameJson(left, right) {
  return JSON.stringify(left) === JSON.stringify(right);
}

const styleBlocks = [...output.matchAll(/<style>([\s\S]*?)<\/style>/g)];
const scriptBlocks = [...output.matchAll(/<script>([\s\S]*?)<\/script>/g)];
const expectedStyleBlocks = 1 + project.modules.filter(({ config }) => config.styles?.length).length;
check(styleBlocks.length === expectedStyleBlocks, `style 块应为 ${expectedStyleBlocks}，实际 ${styleBlocks.length}`);
check(scriptBlocks.length === 4, `script 块应为 4，实际 ${scriptBlocks.length}`);
check(!output.includes('@asset/'), '产物仍含 @asset 引用');
check(!output.includes(project.manifest.generatedMarker), '产物仍含模块注册表生成标记');
check(!output.includes('<!-- @learning-menu -->'), '产物仍含未生成的学习目录');

for (const [index, block] of scriptBlocks.entries()) {
  try {
    new vm.Script(block[1], { filename: `tech-reader.inline-${index + 1}.js` });
  } catch (error) {
    failures.push(`第 ${index + 1} 个 script 语法错误: ${error.message}`);
  }
}

let generatedData;
if (scriptBlocks[0]) {
  try {
    const context = { window: {} };
    vm.createContext(context);
    vm.runInContext(`${scriptBlocks[0][1]}\n;globalThis.__audit={toc:TOC_DATA,tech:window.TECH_POINTS};`, context, { timeout: 2000 });
    generatedData = JSON.parse(JSON.stringify(context.__audit));
  } catch (error) {
    failures.push(`生成数据脚本无法执行: ${error.message}`);
  }
}

const expectedToc = Object.fromEntries(project.modules.map(({ config }) => [config.key, config.toc]));
const expectedTechPoints = readJson(resolveInside(PROJECT_ROOT, project.manifest.scripts.techPoints));
if (generatedData) {
  check(sameJson(generatedData.toc, expectedToc), '产物 TOC_DATA 与 module.json 不一致');
  check(sameJson(generatedData.tech, expectedTechPoints), '产物 TECH_POINTS 与 tech-points.json 不一致');
}

const bodyStart = output.indexOf('<body>');
const firstScript = output.indexOf('<script>', bodyStart);
const domSource = output.slice(bodyStart, firstScript);
const idOccurrences = new Map();
for (const match of domSource.matchAll(/<([a-z][\w:-]*)\b([^>]*)>/gi)) {
  const idMatch = match[2].match(/\bid="([^"]+)"/i);
  if (!idMatch) continue;
  const records = idOccurrences.get(idMatch[1]) || [];
  records.push({ tag: match[1].toLowerCase(), offset: match.index });
  idOccurrences.set(idMatch[1], records);
}
const duplicateIds = [...idOccurrences].filter(([, records]) => records.length > 1).map(([id]) => id);
check(duplicateIds.length === 0, `发现重复 DOM id: ${duplicateIds.slice(0, 20).join(', ')}`);

const routes = new Set();
const moduleKeys = new Set();
const contentFiles = new Set();
for (const { config, configDir, configPath } of project.modules) {
  for (const field of ['key', 'route', 'scopeId', 'title', 'subtitle']) {
    check(typeof config[field] === 'string' && config[field].length > 0, `${configPath} 缺少 ${field}`);
  }
  check(!moduleKeys.has(config.key), `模块 key 重复: ${config.key}`);
  check(!routes.has(config.route), `模块 route 重复: ${config.route}`);
  moduleKeys.add(config.key);
  routes.add(config.route);
  check(idOccurrences.has(config.scopeId), `模块 ${config.key} 的 scopeId 不存在: ${config.scopeId}`);
  check(Array.isArray(config.content) && config.content.length > 0, `模块 ${config.key} 没有正文文件`);
  check(Array.isArray(config.toc), `模块 ${config.key} 的 toc 不是数组`);
  for (const item of config.content || []) {
    const absolute = path.resolve(configDir, item.file);
    check(fs.existsSync(absolute), `模块 ${config.key} 正文不存在: ${item.file}`);
    const normalized = path.normalize(absolute);
    check(!contentFiles.has(normalized), `正文文件被重复注册: ${item.file}`);
    contentFiles.add(normalized);
  }
  for (const style of config.styles || []) check(fs.existsSync(path.resolve(configDir, style)), `模块 ${config.key} CSS 不存在: ${style}`);
  for (const entry of flattenToc(config.toc || [])) {
    check(typeof entry.t === 'string' && entry.t.length > 0, `模块 ${config.key} 有空 TOC 标题`);
    if (entry.id) check(idOccurrences.has(entry.id), `模块 ${config.key} 的 TOC anchor 不存在: ${entry.id}`);
    else check(entry.g === 1 && Array.isArray(entry.c), `模块 ${config.key} 有无 id 且非分组的 TOC 项: ${entry.t}`);
  }
}

for (const [key, point] of Object.entries(expectedTechPoints)) {
  check(typeof point.t === 'string' && point.t.length > 0, `术语卡 ${key} 缺少标题`);
  check(typeof point.tgt === 'string' && idOccurrences.has(point.tgt), `术语卡 ${key} 的目标不存在: ${point.tgt}`);
}

const referencedAssets = new Set();
for (const relative of collectProjectSourceFiles(project)) {
  const source = readProjectFile(PROJECT_ROOT, relative);
  for (const match of source.matchAll(/@asset\/([A-Za-z0-9._/-]+)/g)) referencedAssets.add(match[1]);
}
for (const [id, entry] of Object.entries(project.assets)) {
  const file = resolveInside(PROJECT_ROOT, entry.file);
  check(fs.existsSync(file), `图片文件不存在: ${id}`);
  if (!fs.existsSync(file)) continue;
  const buffer = fs.readFileSync(file);
  try {
    check(detectImageMime(buffer, file) === entry.mime, `图片 MIME 不一致: ${id}`);
  } catch (error) {
    failures.push(error.message);
  }
  check(sha256(buffer) === entry.sha256, `图片哈希不一致: ${id}`);
  check(referencedAssets.has(id), `图片已登记但未使用: ${id}`);
}
for (const id of referencedAssets) check(Boolean(project.assets[id]), `源码引用未登记图片: ${id}`);

const externalScript = /<script\b[^>]*\bsrc=["']https?:\/\//i.test(output);
const externalStyle = /<link\b[^>]*\brel=["']stylesheet["'][^>]*\bhref=["']https?:\/\//i.test(output);
const externalImage = /<(?:img|source)\b[^>]*\b(?:src|srcset)=["']https?:\/\//i.test(output);
const externalCss = /url\(\s*["']?https?:\/\//i.test(styleBlocks.map((block) => block[1]).join('\n'));
check(!externalScript, '产物依赖外部 script');
check(!externalStyle, '产物依赖外部 stylesheet');
check(!externalImage, '产物依赖外部图片');
check(!externalCss, '产物 CSS 依赖外部资源');

notes.push(`产物 ${outputBuffer.length} 字节`);
notes.push(`SHA-256 ${sha256(outputBuffer)}`);
notes.push(`${project.modules.length} 个模块 / ${contentFiles.size} 个正文单元`);
notes.push(`${idOccurrences.size} 个 DOM id / ${referencedAssets.size} 个唯一正文图片`);
notes.push(`${Object.keys(expectedTechPoints).length} 张术语卡`);

if (failures.length) {
  console.error('验证失败:');
  for (const failure of failures) console.error(`- ${failure}`);
  process.exitCode = 1;
} else {
  console.log('验证通过');
  for (const note of notes) console.log(`- ${note}`);
}
