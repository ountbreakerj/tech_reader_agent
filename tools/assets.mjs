import fs from 'node:fs';
import path from 'node:path';
import {
  PROJECT_ROOT,
  detectImageMime,
  extensionForMime,
  loadProject,
  resolveInside,
  sha256,
  writeJson,
} from './lib/project.mjs';

const [command = 'audit', sourceArg, idArg] = process.argv.slice(2);
const project = loadProject(PROJECT_ROOT);

function audit() {
  const failures = [];
  let bytes = 0;
  for (const [id, entry] of Object.entries(project.assets)) {
    try {
      const file = resolveInside(PROJECT_ROOT, entry.file);
      const buffer = fs.readFileSync(file);
      bytes += buffer.length;
      const mime = detectImageMime(buffer, file);
      if (mime !== entry.mime) failures.push(`${id}: MIME ${mime} != ${entry.mime}`);
      if (sha256(buffer) !== entry.sha256) failures.push(`${id}: SHA-256 不一致`);
    } catch (error) {
      failures.push(`${id}: ${error.message}`);
    }
  }
  if (failures.length) throw new Error(`图片审计失败\n${failures.join('\n')}`);
  console.log(`图片审计通过: ${Object.keys(project.assets).length} 个资源 / ${bytes} 字节`);
}

function slugify(value) {
  return value.normalize('NFKD').replace(/[^A-Za-z0-9._-]+/g, '-').replace(/^-+|-+$/g, '').toLowerCase();
}

function add() {
  if (!sourceArg) throw new Error('用法: node tools/assets.mjs add <图片路径> [资源ID]');
  const source = path.resolve(process.cwd(), sourceArg);
  if (!fs.existsSync(source) || !fs.statSync(source).isFile()) throw new Error(`图片不存在: ${source}`);
  const buffer = fs.readFileSync(source);
  const mime = detectImageMime(buffer, source);
  const digest = sha256(buffer);
  const extension = extensionForMime(mime);
  const base = slugify(path.basename(source, path.extname(source))) || 'image';
  let id = idArg || `custom/${base}-${digest.slice(0, 10)}${extension}`;
  if (!id.includes('/')) id = `custom/${id}`;
  if (!/^[A-Za-z0-9._/-]+$/.test(id) || id.includes('..')) throw new Error(`资源 ID 不合法: ${id}`);
  if (!path.extname(id)) id += extension;
  const destinationRelative = `src/assets/${id}`;
  const destination = resolveInside(PROJECT_ROOT, destinationRelative);
  const existing = project.assets[id];
  if (existing && existing.sha256 !== digest) throw new Error(`资源 ID 已存在且内容不同: ${id}`);
  fs.mkdirSync(path.dirname(destination), { recursive: true });
  fs.copyFileSync(source, destination);
  project.assets[id] = {
    file: destinationRelative.replaceAll('\\', '/'),
    mime,
    dataUriPrefix: `data:${mime};base64,`,
    sha256: digest,
  };
  writeJson(project.assetsPath, project.assets);
  console.log(`已加入 ${id}`);
  console.log(`在 HTML/CSS 中引用: @asset/${id}`);
}

if (command === 'audit') audit();
else if (command === 'list') console.log(Object.keys(project.assets).sort().join('\n'));
else if (command === 'add') add();
else throw new Error(`未知命令 ${command}。可用: audit, list, add`);
