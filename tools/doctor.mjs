import fs from 'node:fs';
import path from 'node:path';
import { PROJECT_ROOT, flattenToc, loadProject, resolveInside } from './lib/project.mjs';

const project = loadProject(PROJECT_ROOT);
const rows = project.modules.map(({ config }) => ({
  key: config.key,
  route: config.route,
  content: config.content.length,
  styles: config.styles?.length || 0,
  toc: flattenToc(config.toc).filter((entry) => entry.id).length,
}));
const output = resolveInside(PROJECT_ROOT, project.manifest.output);
console.log(`Node ${process.version}`);
console.log(`项目 ${path.basename(PROJECT_ROOT)}`);
console.table(rows);
console.log(`图片 ${Object.keys(project.assets).length} 个`);
console.log(`产物 ${fs.existsSync(output) ? `${fs.statSync(output).size} 字节` : '尚未构建'}`);
