import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import { fileURLToPath } from 'node:url';

export const PROJECT_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..');

export function readJson(file) {
  return JSON.parse(fs.readFileSync(file, 'utf8'));
}

export function writeJson(file, value) {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(file, `${JSON.stringify(value, null, 2)}\n`, 'utf8');
}

export function sha256(value) {
  return crypto.createHash('sha256').update(value).digest('hex');
}

export function resolveInside(root, relativePath) {
  const resolvedRoot = path.resolve(root);
  const resolved = path.resolve(resolvedRoot, relativePath);
  const prefix = `${resolvedRoot}${path.sep}`;
  if (resolved !== resolvedRoot && !resolved.startsWith(prefix)) {
    throw new Error(`路径越出项目目录: ${relativePath}`);
  }
  return resolved;
}

export function readProjectFile(root, relativePath) {
  return fs.readFileSync(resolveInside(root, relativePath), 'utf8');
}

export function loadProject(root = PROJECT_ROOT) {
  const manifest = readJson(path.join(root, 'manifest.json'));
  const modules = manifest.modules.map((configPath) => {
    const absoluteConfig = resolveInside(root, configPath);
    return {
      configPath,
      configDir: path.dirname(absoluteConfig),
      config: readJson(absoluteConfig),
    };
  });
  const assetsPath = resolveInside(root, manifest.assets);
  return {
    root,
    manifest,
    modules,
    assetsPath,
    assets: readJson(assetsPath),
  };
}

const MIME_BY_EXTENSION = new Map([
  ['.svg', 'image/svg+xml'],
  ['.png', 'image/png'],
  ['.jpg', 'image/jpeg'],
  ['.jpeg', 'image/jpeg'],
  ['.webp', 'image/webp'],
  ['.gif', 'image/gif'],
  ['.avif', 'image/avif'],
]);

const EXTENSION_BY_MIME = new Map([
  ['image/svg+xml', '.svg'],
  ['image/png', '.png'],
  ['image/jpeg', '.jpg'],
  ['image/webp', '.webp'],
  ['image/gif', '.gif'],
  ['image/avif', '.avif'],
]);

export function extensionForMime(mime) {
  const extension = EXTENSION_BY_MIME.get(mime.toLowerCase());
  if (!extension) throw new Error(`不支持的图片 MIME: ${mime}`);
  return extension;
}

export function detectImageMime(buffer, fileName = '') {
  if (buffer.length >= 8 && buffer.subarray(0, 8).equals(Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]))) return 'image/png';
  if (buffer.length >= 3 && buffer[0] === 0xff && buffer[1] === 0xd8 && buffer[2] === 0xff) return 'image/jpeg';
  if (buffer.length >= 6 && (buffer.subarray(0, 6).toString('ascii') === 'GIF87a' || buffer.subarray(0, 6).toString('ascii') === 'GIF89a')) return 'image/gif';
  if (buffer.length >= 12 && buffer.subarray(0, 4).toString('ascii') === 'RIFF' && buffer.subarray(8, 12).toString('ascii') === 'WEBP') return 'image/webp';
  if (buffer.length >= 12 && buffer.subarray(4, 8).toString('ascii') === 'ftyp' && /^(avif|avis)$/.test(buffer.subarray(8, 12).toString('ascii'))) return 'image/avif';
  const head = buffer.subarray(0, Math.min(buffer.length, 2048)).toString('utf8').replace(/^\uFEFF/, '').trimStart();
  if (head.startsWith('<svg') || (head.startsWith('<?xml') && head.includes('<svg'))) return 'image/svg+xml';
  const fromExtension = MIME_BY_EXTENSION.get(path.extname(fileName).toLowerCase());
  if (fromExtension) return fromExtension;
  throw new Error(`无法识别图片类型: ${fileName || '(无文件名)'}`);
}

export function expandAssetTokens(text, project, usage = new Set(), options = {}) {
  // inline=false 时输出 tr-asset://<id> 占位符，真实 data URI 由构建端汇总进
  // window.__TR_ASSETS 注册表（每资源单份存储），运行时由 app.js 统一回填 img.src。
  const { inline = true, uriCache = null } = options;
  const tokenPattern = /@asset\/([A-Za-z0-9._/-]+)/g;
  const cache = uriCache || new Map();
  return text.replace(tokenPattern, (token, id) => {
    const entry = project.assets[id];
    if (!entry) throw new Error(`未登记的图片引用 ${token}`);
    usage.add(id);
    if (!cache.has(id)) {
      const file = resolveInside(project.root, entry.file);
      const buffer = fs.readFileSync(file);
      const actualMime = detectImageMime(buffer, file);
      if (actualMime !== entry.mime) throw new Error(`图片 MIME 不一致: ${id}，登记 ${entry.mime}，实际 ${actualMime}`);
      if (entry.sha256 && sha256(buffer) !== entry.sha256) throw new Error(`图片内容哈希不一致: ${id}`);
      cache.set(id, `${entry.dataUriPrefix || `data:${entry.mime};base64,`}${buffer.toString('base64')}`);
    }
    return inline ? cache.get(id) : `tr-asset://${id}`;
  });
}

export function flattenToc(entries, result = []) {
  for (const entry of entries) {
    result.push(entry);
    if (Array.isArray(entry.c)) flattenToc(entry.c, result);
  }
  return result;
}

export function collectProjectSourceFiles(project) {
  const files = new Set([
    project.manifest.shell.head,
    project.manifest.shell.headClose,
    project.manifest.shell.home,
    project.manifest.shell.bodyTail,
    project.manifest.shell.documentTail,
    project.manifest.shell.style,
    project.manifest.scripts.vendor,
    project.manifest.scripts.mermaidInit,
    project.manifest.scripts.app,
  ]);
  for (const module of project.modules) {
    for (const style of module.config.styles || []) files.add(path.relative(project.root, path.resolve(module.configDir, style)));
    for (const item of module.config.content || []) files.add(path.relative(project.root, path.resolve(module.configDir, item.file)));
  }
  return [...files];
}
