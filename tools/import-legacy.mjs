import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import vm from 'node:vm';
import { TextDecoder } from 'node:util';
import {
  PROJECT_ROOT,
  extensionForMime,
  sha256,
  writeJson,
} from './lib/project.mjs';

const input = path.resolve(PROJECT_ROOT, process.argv[2] || '../tech-reader.html');
const srcRoot = path.join(PROJECT_ROOT, 'src');
if (!fs.existsSync(input)) throw new Error(`找不到原始文件: ${input}`);
if (fs.existsSync(srcRoot) || fs.existsSync(path.join(PROJECT_ROOT, 'manifest.json'))) {
  throw new Error('目标项目已经导入。为保护已有源码，导入器拒绝覆盖 src/ 或 manifest.json。');
}

const originalBuffer = fs.readFileSync(input);
const original = new TextDecoder('utf-8', { fatal: true }).decode(originalBuffer);
const baselineSha256 = sha256(originalBuffer);
const outputFiles = new Map();

function must(condition, message) {
  if (!condition) throw new Error(`导入边界校验失败: ${message}`);
}

function put(relativePath, text) {
  must(!outputFiles.has(relativePath), `重复输出 ${relativePath}`);
  outputFiles.set(relativePath, text);
}

function findAll(pattern, from, to) {
  const re = new RegExp(pattern.source, 'g');
  re.lastIndex = from;
  const matches = [];
  let match;
  while ((match = re.exec(original)) !== null && match.index < to) {
    matches.push({ index: match.index, id: match[1] });
  }
  return matches;
}

function findMatchingBrace(source, open) {
  let depth = 1;
  let quote = null;
  let lineComment = false;
  let blockComment = false;
  for (let i = open + 1; i < source.length; i += 1) {
    const char = source[i];
    const next = source[i + 1];
    if (lineComment) {
      if (char === '\n') lineComment = false;
      continue;
    }
    if (blockComment) {
      if (char === '*' && next === '/') { blockComment = false; i += 1; }
      continue;
    }
    if (quote) {
      if (char === '\\') { i += 1; continue; }
      if (char === quote) quote = null;
      continue;
    }
    if (char === '/' && next === '/') { lineComment = true; i += 1; continue; }
    if (char === '/' && next === '*') { blockComment = true; i += 1; continue; }
    if (char === '"' || char === "'" || char === '`') { quote = char; continue; }
    if (char === '{') depth += 1;
    if (char === '}' && --depth === 0) return i;
  }
  throw new Error(`未找到对象闭合括号: ${open}`);
}

const firstStyle = original.indexOf('<style>');
const bodyEnd = original.indexOf('<body>') + '<body>'.length;
const firstChapter = original.indexOf('<section class="chapter"');
const dshStart = original.indexOf('<div class="dsh-scope"');
const langgraphStart = original.indexOf('<div class="lg-scope"');
const firstLlm = original.indexOf('<section class="llm-mod');
const firstScript = original.indexOf('<script>', firstLlm);
const llmEnd = original.lastIndexOf('</section>', firstScript) + '</section>'.length;
must(firstStyle > 0 && bodyEnd > firstStyle, 'head/style/body');
must(firstChapter > bodyEnd && dshStart > firstChapter, 'book/dsh');
must(langgraphStart > dshStart && firstLlm > langgraphStart, 'langgraph/llm');
must(firstScript > llmEnd && llmEnd > firstLlm, 'llm/script');

const styleNames = ['shell', 'dsh', 'langgraph', 'llm'];
const styles = [];
const styleSeparators = [];
let cursor = firstStyle;
for (let i = 0; i < styleNames.length; i += 1) {
  const open = original.indexOf('<style>', cursor);
  if (i > 0) styleSeparators.push(original.slice(cursor, open));
  const close = original.indexOf('</style>', open);
  must(open >= cursor && close > open, `style ${styleNames[i]}`);
  styles.push(original.slice(open + '<style>'.length, close));
  cursor = close + '</style>'.length;
}
const head = original.slice(0, firstStyle);
const headClose = original.slice(cursor, bodyEnd);
const home = original.slice(bodyEnd, firstChapter);

const chapters = findAll(/<section class="chapter" id="([^"]+)"/, firstChapter, dshStart);
must(chapters.length === 13, `book 章节数应为 13，实际 ${chapters.length}`);
const bookContent = chapters.map((chapter, index) => ({
  id: chapter.id.replace(/^ch-/, ''),
  sectionId: chapter.id,
  text: original.slice(chapter.index, index + 1 < chapters.length ? chapters[index + 1].index : dshStart),
}));

const dshContent = original.slice(dshStart, langgraphStart);
const langgraphContent = original.slice(langgraphStart, firstLlm);
const llmSections = findAll(/<section class="llm-mod[^"]*" id="([^"]+)"/, firstLlm, llmEnd);
must(llmSections.length === 58, `LLM section 数应为 58，实际 ${llmSections.length}`);
const llmContent = llmSections.map((section, index) => ({
  id: section.id,
  slug: section.id.replace(/^llm-/, ''),
  text: original.slice(section.index, index + 1 < llmSections.length ? llmSections[index + 1].index : llmEnd),
}));
const bodyTail = original.slice(llmEnd, firstScript);

const scripts = [];
const scriptSeparators = [];
cursor = firstScript;
for (let i = 0; i < 4; i += 1) {
  const open = original.indexOf('<script>', cursor);
  if (i > 0) scriptSeparators.push(original.slice(cursor, open));
  const close = original.indexOf('</script>', open);
  must(open >= cursor && close > open, `script ${i + 1}`);
  scripts.push(original.slice(open + '<script>'.length, close));
  cursor = close + '</script>'.length;
}
const documentTail = original.slice(cursor);

const exactRebuild = [
  head,
  styles.map((style, index) => `${index ? styleSeparators[index - 1] : ''}<style>${style}</style>`).join(''),
  headClose,
  home,
  ...bookContent.map((item) => item.text),
  dshContent,
  langgraphContent,
  ...llmContent.map((item) => item.text),
  bodyTail,
  scripts.map((script, index) => `${index ? scriptSeparators[index - 1] : ''}<script>${script}</script>`).join(''),
  documentTail,
].join('');
must(exactRebuild === original, '初始连续切片无法逐字符还原原文');
must(sha256(Buffer.from(exactRebuild, 'utf8')) === baselineSha256, '初始连续切片 SHA-256 不一致');

const registryContext = { window: {} };
vm.createContext(registryContext);
vm.runInContext(`${scripts[0]}\n;globalThis.__export={toc:TOC_DATA,tech:window.TECH_POINTS};`, registryContext, { timeout: 2000 });
const tocData = JSON.parse(JSON.stringify(registryContext.__export.toc));
const techPoints = JSON.parse(JSON.stringify(registryContext.__export.tech));

const modulesAt = scripts[3].indexOf('var MODULES =');
must(modulesAt >= 0, 'app.js 中找不到 MODULES');
const modulesOpen = scripts[3].indexOf('{', modulesAt);
const modulesClose = findMatchingBrace(scripts[3], modulesOpen);
const modulesExpression = scripts[3].slice(modulesOpen, modulesClose + 1);
const moduleRegistry = vm.runInNewContext(`(${modulesExpression})`, { TOC_DATA: tocData }, { timeout: 1000 });
const routesAt = scripts[3].indexOf('var ROUTE_TO_MOD', modulesClose);
const routesEnd = scripts[3].indexOf(';', routesAt);
must(routesAt > modulesClose && routesEnd > routesAt, 'app.js 中找不到 ROUTE_TO_MOD');
const generatedMarker = '/* @generated:module-registry */';
const appSource = `${scripts[3].slice(0, modulesAt)}${generatedMarker}${scripts[3].slice(routesEnd + 1)}`;

function splitLlmCss(css) {
  const startPattern = /(?:^|\n)(?=\.llm-m-([a-z0-9-]+) \.module-wrapper \*)/g;
  const rawStarts = [];
  let match;
  while ((match = startPattern.exec(css)) !== null) rawStarts.push({ index: match.index, prefix: match[1] });
  // MySQL 的历史样式由三个相邻模板块组成；相邻同前缀仍归同一 section。
  const starts = rawStarts.filter((entry, index) => index === 0 || entry.prefix !== rawStarts[index - 1].prefix);
  must(starts.length === 53, `LLM CSS 前缀块应为 53，实际 ${starts.length}`);
  const tp = starts.find((entry) => entry.prefix === 'tp');
  const sharedStart = css.indexOf('\n.llm-scope .welcome-hero');
  const technicalComment = css.lastIndexOf('\n/*', tp.index);
  must(sharedStart > 0 && technicalComment > sharedStart, 'LLM shared/technical CSS 边界');
  const pieces = [];
  for (let i = 0; i < starts.length; i += 1) {
    const current = starts[i];
    if (current.prefix === 'tp') break;
    const next = starts[i + 1];
    const end = next?.prefix === 'tp' ? sharedStart : next.index;
    pieces.push({ prefix: current.prefix, text: css.slice(current.index, end) });
  }
  pieces.push({ prefix: 'shared', text: css.slice(sharedStart, technicalComment) });
  pieces.push({ prefix: 'technical-sections', text: css.slice(technicalComment) });
  must(pieces.map((piece) => piece.text).join('') === css, 'LLM CSS 拆分无法原样拼回');
  return pieces;
}

put('src/shell/head.html', head);
put('src/shell/head-close.html', headClose);
put('src/shell/home.html', home);
put('src/shell/body-tail.html', bodyTail);
put('src/shell/document-tail.html', documentTail);
put('src/shell/shell.css', styles[0]);
put('src/modules/dsh/dsh.css', styles[1]);
put('src/modules/langgraph/langgraph.css', styles[2]);
for (const piece of splitLlmCss(styles[3])) {
  if (piece.prefix === 'shared' || piece.prefix === 'technical-sections') {
    put(`src/modules/llm/${piece.prefix}.css`, piece.text);
  } else {
    put(`src/modules/llm/sections/${piece.prefix}/style.css`, piece.text);
  }
}
for (const chapter of bookContent) put(`src/modules/book/chapters/${chapter.id}/content.html`, chapter.text);
put('src/modules/dsh/content.html', dshContent);
put('src/modules/langgraph/content.html', langgraphContent);
for (const section of llmContent) put(`src/modules/llm/sections/${section.slug}/content.html`, section.text);
put('src/data/registry.legacy.js', scripts[0]);
put('src/vendor/mermaid.min.js', scripts[1]);
put('src/vendor/mermaid-init.js', scripts[2]);
put('src/shell/app.legacy.js', scripts[3]);
put('src/shell/app.js', appSource);

const assetEntries = {};
let assetOccurrences = 0;
const dataUriPattern = /(data:(image\/[a-z0-9.+-]+)(?:;charset=[^;,]+)?;base64,)([A-Za-z0-9+/=]+)/gi;
for (const [relativePath, text] of outputFiles) {
  if (!relativePath.endsWith('.html')) continue;
  const replaced = text.replace(dataUriPattern, (full, dataUriPrefix, mime, payload) => {
    const buffer = Buffer.from(payload, 'base64');
    must(buffer.toString('base64') === payload, `非规范 base64: ${relativePath}`);
    const digest = sha256(buffer);
    const fileName = `${digest.slice(0, 20)}${extensionForMime(mime)}`;
    const id = `generated/${fileName}`;
    const existing = assetEntries[id];
    if (existing) must(existing.sha256 === digest && existing.dataUriPrefix === dataUriPrefix, `资源 ID 冲突 ${id}`);
    assetEntries[id] = {
      file: `src/assets/${id}`,
      mime,
      dataUriPrefix,
      sha256: digest,
    };
    const assetPath = path.join(PROJECT_ROOT, 'src', 'assets', id);
    fs.mkdirSync(path.dirname(assetPath), { recursive: true });
    if (!fs.existsSync(assetPath)) fs.writeFileSync(assetPath, buffer);
    assetOccurrences += 1;
    return `@asset/${id}`;
  });
  outputFiles.set(relativePath, replaced);
}
must(assetOccurrences === 114, `正文图片引用应为 114，实际 ${assetOccurrences}`);

for (const [relativePath, text] of outputFiles) {
  const target = path.join(PROJECT_ROOT, relativePath);
  fs.mkdirSync(path.dirname(target), { recursive: true });
  fs.writeFileSync(target, text, 'utf8');
}

writeJson(path.join(PROJECT_ROOT, 'src', 'assets', 'manifest.json'), assetEntries);
writeJson(path.join(PROJECT_ROOT, 'src', 'data', 'tech-points.json'), techPoints);

const keyToFolder = { book: 'book', dsh: 'dsh', lg: 'langgraph', llm: 'llm' };
const moduleConfigs = {};
for (const [key, folder] of Object.entries(keyToFolder)) {
  const registry = moduleRegistry[key];
  moduleConfigs[folder] = {
    key,
    route: registry.route,
    scopeId: registry.scopeId,
    title: registry.title,
    subtitle: registry.sub,
    styles: [],
    content: [],
    toc: tocData[key],
  };
}
moduleConfigs.book.content = bookContent.map((item) => ({ id: item.id, sectionId: item.sectionId, file: `chapters/${item.id}/content.html` }));
moduleConfigs.dsh.styles = ['dsh.css'];
moduleConfigs.dsh.content = [{ id: 'dsh', file: 'content.html' }];
moduleConfigs.langgraph.styles = ['langgraph.css'];
moduleConfigs.langgraph.content = [{ id: 'langgraph', file: 'content.html' }];
const llmStylePieces = splitLlmCss(styles[3]);
moduleConfigs.llm.styles = llmStylePieces.map((piece) => piece.prefix === 'shared' || piece.prefix === 'technical-sections'
  ? `${piece.prefix}.css`
  : `sections/${piece.prefix}/style.css`);
moduleConfigs.llm.content = llmContent.map((item) => ({ id: item.id, file: `sections/${item.slug}/content.html` }));
for (const [folder, config] of Object.entries(moduleConfigs)) {
  writeJson(path.join(PROJECT_ROOT, 'src', 'modules', folder, 'module.json'), config);
}

writeJson(path.join(PROJECT_ROOT, 'manifest.json'), {
  version: 1,
  note: '源码清单。模块顺序、CSS 级联顺序和正文顺序均依赖此文件及各 module.json。',
  output: 'dist/tech-reader.html',
  assets: 'src/assets/manifest.json',
  baselineSha256,
  generatedMarker,
  shell: {
    head: 'src/shell/head.html',
    headClose: 'src/shell/head-close.html',
    home: 'src/shell/home.html',
    bodyTail: 'src/shell/body-tail.html',
    documentTail: 'src/shell/document-tail.html',
    style: 'src/shell/shell.css',
  },
  modules: [
    'src/modules/book/module.json',
    'src/modules/dsh/module.json',
    'src/modules/langgraph/module.json',
    'src/modules/llm/module.json'
  ],
  styleSeparators,
  scripts: {
    legacyRegistry: 'src/data/registry.legacy.js',
    techPoints: 'src/data/tech-points.json',
    vendor: 'src/vendor/mermaid.min.js',
    mermaidInit: 'src/vendor/mermaid-init.js',
    app: 'src/shell/app.js',
    legacyApp: 'src/shell/app.legacy.js',
    separators: scriptSeparators,
  },
});

console.log(`无损连续切片校验通过: ${baselineSha256}`);
console.log(`book ${bookContent.length} 章 / LLM ${llmContent.length} sections`);
console.log(`外置正文图片 ${assetOccurrences} 次引用 / ${Object.keys(assetEntries).length} 个唯一资源`);
console.log(`写入 ${outputFiles.size} 个文本源码文件`);
