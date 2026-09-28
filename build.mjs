import fs from 'node:fs';
import path from 'node:path';
import { renderLearningMenu } from './tools/lib/learning-menu.mjs';
import {
  PROJECT_ROOT,
  expandAssetTokens,
  loadProject,
  readProjectFile,
  resolveInside,
  sha256,
} from './tools/lib/project.mjs';

const args = new Set(process.argv.slice(2));
const legacy = args.has('--legacy');
const noWrite = args.has('--no-write');
const checkBaseline = args.has('--check-baseline');
const project = loadProject(PROJECT_ROOT);
const { manifest } = project;
const assetUsage = new Set();

function read(relativePath) {
  return expandAssetTokens(readProjectFile(PROJECT_ROOT, relativePath), project, assetUsage);
}

function readModuleFile(module, relativePath) {
  const absolute = path.resolve(module.configDir, relativePath);
  const projectRelative = path.relative(PROJECT_ROOT, absolute);
  return read(projectRelative);
}

function generateRegistry() {
  const toc = {};
  for (const module of project.modules) toc[module.config.key] = module.config.toc;
  const techPoints = JSON.parse(readProjectFile(PROJECT_ROOT, manifest.scripts.techPoints));
  return `\nconst TOC_DATA = ${JSON.stringify(toc)};\nwindow.TECH_POINTS = ${JSON.stringify(techPoints)};\n`;
}

function generateModuleRegistry() {
  const moduleLines = project.modules.map(({ config }) => [
    `    ${JSON.stringify(config.key)}: {`,
    `      route: ${JSON.stringify(config.route)}, scopeId: ${JSON.stringify(config.scopeId)},`,
    `      title: ${JSON.stringify(config.title)}, sub: ${JSON.stringify(config.subtitle)},`,
    `      toc: TOC_DATA[${JSON.stringify(config.key)}]`,
    '    }',
  ].join('\n'));
  const routes = Object.fromEntries(project.modules.map(({ config }) => [config.route, config.key]));
  return `var MODULES = {\n${moduleLines.join(',\n')}\n  };\n  var ROUTE_TO_MOD = ${JSON.stringify(routes)};`;
}

const styleGroups = [read(manifest.shell.style)];
for (const module of project.modules) {
  if (module.config.styles?.length) {
    styleGroups.push(module.config.styles.map((file) => readModuleFile(module, file)).join(''));
  }
}
if (styleGroups.length !== manifest.styleSeparators.length + 1) {
  throw new Error(`style 分组数 ${styleGroups.length} 与 separator 数 ${manifest.styleSeparators.length} 不匹配`);
}
const styleOutput = styleGroups
  .map((css, index) => `${index ? manifest.styleSeparators[index - 1] : ''}<style>${css}</style>`)
  .join('');

const bodyOutput = project.modules
  .flatMap((module) => module.config.content.map((item) => {
    const source = readModuleFile(module, item.file);
    return source.includes('<!-- @learning-menu -->')
      ? source.replace('<!-- @learning-menu -->', renderLearningMenu(module.config))
      : source;
  }))
  .join('');

let app = read(legacy ? manifest.scripts.legacyApp : manifest.scripts.app);
if (!legacy) {
  const occurrences = app.split(manifest.generatedMarker).length - 1;
  if (occurrences !== 1) throw new Error(`app.js 生成标记应出现一次，实际 ${occurrences}`);
  app = app.replace(manifest.generatedMarker, generateModuleRegistry());
}

const scriptContents = [
  legacy ? read(manifest.scripts.legacyRegistry) : generateRegistry(),
  read(manifest.scripts.vendor),
  read(manifest.scripts.mermaidInit),
  app,
];
if (scriptContents.length !== manifest.scripts.separators.length + 1) {
  throw new Error('script 分组与 separator 数不匹配');
}
const scriptOutput = scriptContents
  .map((script, index) => `${index ? manifest.scripts.separators[index - 1] : ''}<script>${script}</script>`)
  .join('');

const output = [
  read(manifest.shell.head),
  styleOutput,
  read(manifest.shell.headClose),
  read(manifest.shell.home),
  bodyOutput,
  read(manifest.shell.bodyTail),
  scriptOutput,
  read(manifest.shell.documentTail),
].join('');

if (output.includes('@asset/')) throw new Error('产物中残留未解析的 @asset 引用');
const outputBuffer = Buffer.from(output, 'utf8');
const outputSha256 = sha256(outputBuffer);
if (checkBaseline && outputSha256 !== manifest.baselineSha256) {
  throw new Error(`基线 SHA-256 不一致: 期望 ${manifest.baselineSha256}，实际 ${outputSha256}`);
}
if (!noWrite) {
  const destination = resolveInside(PROJECT_ROOT, manifest.output);
  fs.mkdirSync(path.dirname(destination), { recursive: true });
  fs.writeFileSync(destination, outputBuffer);
}

console.log(`${legacy ? 'legacy' : 'generated'} 构建完成${noWrite ? '（未写盘）' : ''}`);
console.log(`字节: ${outputBuffer.length}`);
console.log(`SHA-256: ${outputSha256}`);
console.log(`图片引用: ${assetUsage.size} 个唯一资源`);
