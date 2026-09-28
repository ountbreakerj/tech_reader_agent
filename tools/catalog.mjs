import fs from 'node:fs';
import path from 'node:path';
import { PROJECT_ROOT, flattenToc, loadProject, writeJson } from './lib/project.mjs';

const project = loadProject(PROJECT_ROOT);
const catalog = {
  generatedAt: new Date().toISOString(),
  modules: project.modules.map(({ config, configPath }) => ({
    key: config.key,
    route: config.route,
    title: config.title,
    subtitle: config.subtitle,
    config: configPath,
    content: config.content.map((item) => ({ id: item.id, file: `${path.dirname(configPath)}/${item.file}`.replaceAll('\\', '/') })),
    styles: (config.styles || []).map((file) => `${path.dirname(configPath)}/${file}`.replaceAll('\\', '/')),
    tocAnchors: flattenToc(config.toc || []).filter((entry) => entry.id).map((entry) => entry.id),
  })),
  assets: Object.keys(project.assets).length,
};
writeJson(path.join(PROJECT_ROOT, 'catalog.json'), catalog);
console.log(`目录索引已生成: ${catalog.modules.length} 个模块 / ${catalog.assets} 个图片资源`);
