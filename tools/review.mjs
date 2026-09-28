import fs from 'node:fs';
import path from 'node:path';
import { PROJECT_ROOT } from './lib/project.mjs';

const reportArg = process.argv[2];
if (!reportArg) throw new Error('用法: node tools/review.mjs <report.json>');
const reportPath = path.resolve(process.cwd(), reportArg);
const report = JSON.parse(fs.readFileSync(reportPath, 'utf8'));
const failures = [];
const verdicts = new Set(['pass', 'changes_required', 'user_decision_required']);
const severities = new Set(['blocking', 'major', 'minor', 'suggestion']);
const roles = new Set(['product', 'ux', 'ui', 'art', 'accessibility']);
if (!verdicts.has(report.verdict)) failures.push('verdict 无效');
if (!Array.isArray(report.scope)) failures.push('scope 必须是数组');
if (!Array.isArray(report.findings)) failures.push('findings 必须是数组');
if (!Array.isArray(report.checks)) failures.push('checks 必须是数组');
for (const [index, finding] of (report.findings || []).entries()) {
  for (const field of ['severity', 'role', 'location', 'evidence', 'expected', 'recommendation']) {
    if (typeof finding[field] !== 'string' || finding[field].length === 0) failures.push(`findings[${index}] 缺少 ${field}`);
  }
  if (!severities.has(finding.severity)) failures.push(`findings[${index}] severity 无效`);
  if (!roles.has(finding.role)) failures.push(`findings[${index}] role 无效`);
}
const blocking = (report.findings || []).filter((finding) => finding.severity === 'blocking');
const major = (report.findings || []).filter((finding) => finding.severity === 'major');
if (report.verdict === 'pass' && (blocking.length || major.length)) failures.push('pass 报告不能包含 blocking 或 major');
if (report.verdict === 'pass' && !report.checks?.length) failures.push('pass 报告必须列出已执行的检查');
if (failures.length) {
  console.error('设计审计报告无效:');
  for (const failure of failures) console.error(`- ${failure}`);
  process.exitCode = 1;
} else if (blocking.length) {
  console.error(`设计审计需要修复: ${blocking.length} 个 blocking，${major.length} 个 major`);
  process.exitCode = 2;
} else {
  console.log(`设计审计报告通过门槛: ${report.verdict}，${major.length} 个 major，${(report.findings || []).length - blocking.length - major.length} 个较低级别问题`);
}
