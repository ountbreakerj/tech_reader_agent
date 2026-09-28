import fs from 'node:fs';
import path from 'node:path';
import { PROJECT_ROOT, readJson, sha256 } from './lib/project.mjs';

const proof = readJson(path.join(PROJECT_ROOT, 'reports', 'migration-proof.json'));
const source = path.resolve(PROJECT_ROOT, proof.source);
if (path.dirname(source) !== path.dirname(PROJECT_ROOT)) throw new Error(`基线路径不在项目上一级: ${source}`);
if (!fs.existsSync(source)) throw new Error(`原始基线不存在: ${source}`);
const buffer = fs.readFileSync(source);
const actual = sha256(buffer);
if (buffer.length !== proof.sourceBytes || actual !== proof.sha256) {
  throw new Error(`原始基线已变化: bytes=${buffer.length}/${proof.sourceBytes}, sha256=${actual}/${proof.sha256}`);
}
console.log(`原始基线未被覆盖: ${buffer.length} 字节`);
console.log(`SHA-256: ${actual}`);
console.log('导入证明: 初始连续切片曾通过逐字符与 SHA-256 校验');
