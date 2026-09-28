import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import { fileURLToPath } from 'node:url';
import { detectImageMime, extensionForMime, readJson, writeJson } from './lib/project.mjs';

const PROJECT_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const HAPPY_ROOT = path.resolve(PROJECT_ROOT, '..', '..', 'happy-llm');
const LLM_ROOT = path.join(PROJECT_ROOT, 'src', 'modules', 'llm');
const ASSET_ROOT = path.join(PROJECT_ROOT, 'src', 'assets');
const ASSET_MANIFEST = path.join(ASSET_ROOT, 'manifest.json');

if (!fs.existsSync(HAPPY_ROOT)) throw new Error(`找不到 Happy-LLM 来源目录: ${HAPPY_ROOT}`);

const manifest = readJson(ASSET_MANIFEST);
const imageFiles = [];
const imageByRelative = new Map();
const imageByBasename = new Map();

function walk(directory) {
  for (const entry of fs.readdirSync(directory, { withFileTypes: true })) {
    const absolute = path.join(directory, entry.name);
    if (entry.isDirectory()) walk(absolute);
    else if (/\.(png|jpe?g|webp|gif|avif)$/i.test(entry.name)) imageFiles.push(absolute);
  }
}

walk(HAPPY_ROOT);
for (const file of imageFiles) {
  const rel = path.relative(HAPPY_ROOT, file).replaceAll('\\', '/');
  imageByRelative.set(rel.toLowerCase(), file);
  const base = path.basename(file).toLowerCase();
  const list = imageByBasename.get(base) || [];
  list.push(file);
  imageByBasename.set(base, list);
}

function sha256(buffer) {
  return crypto.createHash('sha256').update(buffer).digest('hex');
}

function slug(value) {
  return value
    .normalize('NFKD')
    .replace(/[^A-Za-z0-9._-]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .toLowerCase() || 'image';
}

function resolveImage(reference, sourceFile) {
  const clean = decodeURIComponent(String(reference).split(/[?#]/, 1)[0]).trim();
  let relative = clean;
  if (/^https?:\/\//i.test(clean)) {
    try {
      const url = new URL(clean);
      const marker = '/happy-llm/';
      const index = url.pathname.toLowerCase().indexOf(marker);
      if (index >= 0) relative = url.pathname.slice(index + marker.length);
      else {
        const main = '/main/';
        const mainIndex = url.pathname.toLowerCase().indexOf(main);
        if (mainIndex >= 0) relative = url.pathname.slice(mainIndex + main.length);
      }
    } catch {
      return null;
    }
  }
  try { relative = decodeURIComponent(relative); } catch { /* keep the original path when malformed */ }
  relative = relative.replace(/^\.\//, '').replaceAll('\\', '/');
  // GitHub raw/blob URLs include the repository branch segment (usually
  // `main/`) after the repository name; it is not part of the local tree.
  relative = relative.replace(/^(?:blob\/)?main\//i, '');
  const sourceRelative = path.relative(HAPPY_ROOT, sourceFile).replaceAll('\\', '/');
  const direct = path.normalize(path.join(path.dirname(sourceRelative), relative));
  const directFile = imageByRelative.get(direct.replaceAll('\\', '/').toLowerCase());
  if (directFile) return directFile;
  const repositoryFile = imageByRelative.get(relative.toLowerCase());
  if (repositoryFile) return repositoryFile;
  const basenameMatches = imageByBasename.get(path.basename(relative).toLowerCase()) || [];
  return basenameMatches.length === 1 ? basenameMatches[0] : null;
}

const registered = new Map();
function registerImage(file) {
  if (!file) return null;
  const cached = registered.get(file);
  if (cached) return cached;
  const buffer = fs.readFileSync(file);
  const mime = detectImageMime(buffer, file);
  const digest = sha256(buffer);
  const relative = path.relative(HAPPY_ROOT, file).replaceAll('\\', '/');
  const parts = relative.split('/');
  const base = slug(path.basename(file, path.extname(file)));
  const folder = parts.length > 1 ? parts.slice(0, -1).map(slug).join('/') : 'root';
  const extension = extensionForMime(mime);
  const id = `happy-llm/${folder}/${base}-${digest.slice(0, 10)}${extension}`;
  const destinationRelative = `src/assets/${id}`;
  const destination = path.join(PROJECT_ROOT, destinationRelative);
  fs.mkdirSync(path.dirname(destination), { recursive: true });
  if (!fs.existsSync(destination)) fs.copyFileSync(file, destination);
  manifest[id] = {
    file: destinationRelative.replaceAll('\\', '/'),
    mime,
    dataUriPrefix: `data:${mime};base64,`,
    sha256: digest,
  };
  registered.set(file, id);
  return id;
}

function escapeHtml(value) {
  return String(value)
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;');
}

/*
 * Happy-LLM 的 Markdown 源文档使用 KaTeX/MathJax 风格的 $$...$$ 和
 * $...$。Tech Reader 是离线单文件应用，因此这里把常见 TeX 语法转换为
 * 轻量 HTML，而不是依赖外部 CDN。转换结果复用 shell.css 中已有的
 * .math/.math-display/.mfrac 样式，并保留可读的语义结构（上下标、分数、
 * 根号和矩阵）。
 */
const TEX_SYMBOLS = {
  alpha: 'α', beta: 'β', gamma: 'γ', delta: 'δ', epsilon: 'ε', varepsilon: 'ϵ',
  zeta: 'ζ', eta: 'η', theta: 'θ', vartheta: 'ϑ', iota: 'ι', kappa: 'κ',
  lambda: 'λ', mu: 'μ', nu: 'ν', xi: 'ξ', omicron: 'ο', pi: 'π', varpi: 'ϖ',
  rho: 'ρ', varrho: 'ϱ', sigma: 'σ', varsigma: 'ς', tau: 'τ', upsilon: 'υ',
  phi: 'φ', varphi: 'ϕ', chi: 'χ', psi: 'ψ', omega: 'ω',
  Gamma: 'Γ', Delta: 'Δ', Theta: 'Θ', Lambda: 'Λ', Xi: 'Ξ', Pi: 'Π',
  Sigma: 'Σ', Upsilon: 'Υ', Phi: 'Φ', Psi: 'Ψ', Omega: 'Ω',
  cdot: '·', times: '×', div: '÷', pm: '±', mp: '∓', ast: '∗',
  le: '≤', leq: '≤', ge: '≥', geq: '≥', neq: '≠', ne: '≠', approx: '≈',
  equiv: '≡', sim: '∼', simeq: '≃', propto: '∝', to: '→', rightarrow: '→',
  leftarrow: '←', leftrightarrow: '↔', Rightarrow: '⇒', Leftarrow: '⇐',
  Leftrightarrow: '⇔', mapsto: '↦', in: '∈', notin: '∉', subset: '⊂',
  subseteq: '⊆', supset: '⊃', supseteq: '⊇', forall: '∀', exists: '∃',
  neg: '¬', land: '∧', lor: '∨', cap: '∩', cup: '∪', emptyset: '∅',
  infty: '∞', partial: '∂', nabla: '∇', sum: '∑', prod: '∏', coprod: '∐',
  int: '∫', oint: '∮', ldots: '…', dots: '…', cdots: '⋯', vdots: '⋮',
  ddots: '⋱', mid: '|', vert: '|', langle: '⟨', rangle: '⟩',
  lvert: '|', rvert: '|', ell: 'ℓ', Re: 'ℜ', Im: 'ℑ',
};

function readBalanced(source, start, open = '{', close = '}') {
  if (source[start] !== open) return { value: '', next: start };
  let depth = 0;
  for (let index = start; index < source.length; index += 1) {
    const char = source[index];
    if (char === '\\' && source[index + 1] === close) { index += 1; continue; }
    if (char === open) depth += 1;
    if (char === close) {
      depth -= 1;
      if (depth === 0) return { value: source.slice(start + 1, index), next: index + 1 };
    }
  }
  return { value: source.slice(start + 1), next: source.length };
}

function splitMathRows(source) {
  return source.split(/\\\\/).map((row) => row.trim()).filter(Boolean);
}

function renderMathEnvironment(environment, source) {
  if (!['matrix', 'pmatrix', 'bmatrix', 'Bmatrix', 'vmatrix', 'Vmatrix', 'cases', 'array'].includes(environment)) {
    return renderTex(source);
  }
  const rows = splitMathRows(source).map((row) => row.split('&').map((cell) => renderTex(cell.trim())));
  const tableClass = environment === 'cases' ? 'math-matrix math-cases' : 'math-matrix';
  const body = rows.map((row) => `<tr>${row.map((cell) => `<td>${cell}</td>`).join('')}</tr>`).join('');
  const left = environment === 'bmatrix' ? '[' : environment === 'Bmatrix' ? '{' : environment === 'pmatrix' ? '(' : environment === 'vmatrix' || environment === 'Vmatrix' ? '|' : environment === 'cases' ? '{' : '';
  const right = environment === 'bmatrix' ? ']' : environment === 'Bmatrix' ? '}' : environment === 'pmatrix' ? ')' : environment === 'vmatrix' || environment === 'Vmatrix' ? '|' : '';
  return `<span class="math-environment math-${environment}">${left ? `<span class="math-delim">${left}</span>` : ''}<table class="${tableClass}"><tbody>${body}</tbody></table>${right ? `<span class="math-delim">${right}</span>` : ''}</span>`;
}

function renderTex(formula) {
  const source = String(formula).replace(/\r?\n/g, ' ').replace(/\s+/g, ' ').trim();
  let index = 0;

  const skipSpaces = () => { while (/\s/.test(source[index] || '')) index += 1; };
  const parseSequence = (stopAt = '') => {
    const out = [];
    while (index < source.length) {
      if (stopAt && source[index] === stopAt) { index += 1; break; }
      if (source[index] === '^' || source[index] === '_') {
        const tag = source[index] === '^' ? 'sup' : 'sub';
        index += 1;
        out.push(`<${tag}>${parseArgument()}</${tag}>`);
        continue;
      }
      if (source[index] === '{') {
        index += 1;
        out.push(parseSequence('}'));
        continue;
      }
      if (source[index] === '&') { index += 1; continue; }
      if (source[index] === '~') { index += 1; out.push('<span class="math-space"> </span>'); continue; }
      if (source[index] === '\\') {
        if (source[index + 1] === '\\') { index += 2; out.push('<br>'); continue; }
        out.push(parseCommand());
        continue;
      }
      const char = source[index];
      index += 1;
      out.push(escapeHtml(char));
    }
    return out.join('');
  };

  const parseArgument = () => {
    skipSpaces();
    if (source[index] === '{') { index += 1; return parseSequence('}'); }
    if (source[index] === '[') { index += 1; return `<span class="math-optional">[${parseSequence(']')}]</span>`; }
    if (source[index] === '\\') return parseCommand();
    if (index >= source.length) return '';
    const char = source[index];
    index += 1;
    return escapeHtml(char);
  };

  const parseCommand = () => {
    index += 1;
    if (index >= source.length) return '';
    if (source[index] === '\\') { index += 1; return '<br>'; }
    if (!/[A-Za-z]/.test(source[index])) {
      const symbol = source[index];
      index += 1;
      return symbol === ' ' ? '<span class="math-space"> </span>' : escapeHtml(symbol);
    }
    const start = index;
    while (/[A-Za-z]/.test(source[index] || '')) index += 1;
    const command = source.slice(start, index);
    if (command === 'begin') {
      skipSpaces();
      const group = readBalanced(source, index);
      index = group.next;
      const endToken = `\\end{${group.value}}`;
      const end = source.indexOf(endToken, index);
      if (end < 0) return '';
      const inner = source.slice(index, end);
      index = end + endToken.length;
      return renderMathEnvironment(group.value, inner);
    }
    if (command === 'end') { const group = readBalanced(source, index); index = group.next; return ''; }
    if (['left', 'right', 'big', 'Big', 'bigl', 'bigr', 'Bigl', 'Bigr', 'limits', 'nolimits'].includes(command)) {
      skipSpaces();
      if (source[index] === '\\') return parseCommand();
      if (source[index] && '{}[]()|.'.includes(source[index])) { const delimiter = source[index]; index += 1; return delimiter === '.' ? '' : escapeHtml(delimiter); }
      return '';
    }
    if (command === 'frac' || command === 'dfrac' || command === 'tfrac') {
      const numerator = parseArgument();
      const denominator = parseArgument();
      return `<span class="mfrac"><span class="mnum">${numerator}</span><span class="mden">${denominator}</span></span>`;
    }
    if (command === 'sqrt') {
      let degree = '';
      skipSpaces();
      if (source[index] === '[') { index += 1; degree = parseSequence(']'); }
      const argument = parseArgument();
      return `<span class="msqrt">${degree ? `<sup class="mroot-index">${degree}</sup>` : '√'}<span class="msqrt-arg">${argument}</span></span>`;
    }
    if (['mathrm', 'text', 'textrm', 'operatorname', 'rm'].includes(command)) return `<span class="mtext">${parseArgument()}</span>`;
    if (['mathbf', 'boldsymbol', 'bm', 'textbf', 'bf'].includes(command)) return `<span class="mtext bf">${parseArgument()}</span>`;
    if (['mathcal', 'cal'].includes(command)) return `<span class="mcal">${parseArgument()}</span>`;
    if (['mathbb', 'Bbb'].includes(command)) return `<span class="mbb">${parseArgument()}</span>`;
    if (['mathit', 'textit', 'it'].includes(command)) return `<span class="mit">${parseArgument()}</span>`;
    if (['overline', 'bar', 'widetilde', 'tilde', 'widehat', 'hat'].includes(command)) return `<span class="math-overline">${parseArgument()}</span>`;
    if (['underline', 'underbar'].includes(command)) return `<span class="math-underline">${parseArgument()}</span>`;
    if (['underbrace', 'overbrace'].includes(command)) return `<span class="math-brace">${parseArgument()}</span>`;
    if (command === 'boxed') return `<span class="math-boxed">${parseArgument()}</span>`;
    if (command === 'space' || command === 'quad' || command === 'qquad' || command === ',' || command === ';' || command === ':' || command === '!') return '<span class="math-space"> </span>';
    if (Object.prototype.hasOwnProperty.call(TEX_SYMBOLS, command)) return TEX_SYMBOLS[command];
    if (command === 'top') return '⊤';
    if (command === 'bot') return '⊥';
    if (command === 'dotsb' || command === 'dotsp' || command === 'dotsm') return '…';
    if (command === 'displaystyle' || command === 'textstyle' || command === 'scriptstyle') return '';
    return escapeHtml(command);
  };

  return parseSequence();
}

function inlineMarkdown(value, sourceFile) {
  const placeholders = [];
  let text = String(value);
  const placeholder = (html) => {
    const token = `\u0000${placeholders.length}\u0000`;
    placeholders.push(html);
    return token;
  };
  text = text.replace(/!\[([^\]]*)\]\(([^)]+)\)/g, (_, alt, reference) => {
    const file = resolveImage(reference, sourceFile);
    const id = registerImage(file);
    if (!id) return `[图片未找到：${escapeHtml(reference)}]`;
    const html = `<figure class="happy-figure"><img src="@asset/${id}" alt="${escapeHtml(alt || 'Happy-LLM 图示')}" loading="lazy"><figcaption>${escapeHtml(alt || 'Happy-LLM 图示')}</figcaption></figure>`;
    return placeholder(html);
  });
  text = text.replace(/\$\$([\s\S]*?)\$\$/g, (_, formula) => placeholder(`<span class="math-display-inline" role="math" aria-label="数学公式">${renderTex(formula)}</span>`));
  text = text.replace(/(^|[^\\$])\$(?!\$)([^$\n]+?)\$(?!\$)/g, (_, prefix, formula) => `${prefix}${placeholder(`<span class="math" role="math" aria-label="数学公式">${renderTex(formula)}</span>`)}`);
  text = escapeHtml(text);
  text = text.replace(/`([^`]+)`/g, '<code>$1</code>');
  text = text.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
  text = text.replace(/__([^_]+)__/g, '<strong>$1</strong>');
  text = text.replace(/\*([^*]+)\*/g, '<em>$1</em>');
  text = text.replace(/\[([^\]]+)\]\((https?:\/\/[^)]+)\)/g, '<a href="$2" target="_blank" rel="noreferrer">$1</a>');
  return text.replace(/\u0000(\d+)\u0000/g, (_, index) => placeholders[Number(index)]);
}

function replaceHtmlImages(line, sourceFile) {
  return line.replace(/<img\b([^>]*?)\bsrc\s*=\s*(["'])([^"']+)\2([^>]*)>/gi, (full, before, quote, reference, after) => {
    const file = resolveImage(reference, sourceFile);
    const id = registerImage(file);
    if (!id) return `<div class="callout callout-warn"><p>⚠️ 图片未找到：${escapeHtml(reference)}</p></div>`;
    return `<img${before}src="@asset/${id}"${after}>`;
  });
}

function renderTable(lines, sourceFile) {
  const rows = lines.map((line) => line.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map((cell) => cell.trim()));
  const header = rows[0];
  const body = rows.slice(2);
  return `<table><thead><tr>${header.map((cell) => `<th>${inlineMarkdown(cell, sourceFile)}</th>`).join('')}</tr></thead><tbody>${body.map((row) => `<tr>${row.map((cell) => `<td>${inlineMarkdown(cell, sourceFile)}</td>`).join('')}</tr>`).join('')}</tbody></table>`;
}

function renderMarkdown(markdown, sourceFile, idPrefix) {
  const lines = markdown.replace(/^---[\s\S]*?---\s*/m, '').split(/\r?\n/).map((line) => replaceHtmlImages(line, sourceFile));
  const out = [];
  let paragraph = [];
  let list = null;
  let code = null;
  let sectionIndex = 0;
  let sectionOpen = false;

  const flushParagraph = () => {
    if (!paragraph.length) return;
    const text = paragraph.join(' ').trim();
    const display = text.match(/^\s*\$\$([\s\S]*?)\$\$\s*$/);
    if (display) out.push(`<div class="math-display" role="math" aria-label="数学公式">${renderTex(display[1])}</div>`);
    else if (text) out.push(`<p>${inlineMarkdown(text, sourceFile)}</p>`);
    paragraph = [];
  };
  const closeList = () => {
    if (!list) return;
    out.push(`</${list}>`);
    list = null;
  };
  const closeSection = () => {
    closeList();
    flushParagraph();
    if (sectionOpen) out.push('</div>');
    sectionOpen = false;
  };

  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index];
    const fence = line.match(/^\s*```\s*([\w+-]*)\s*$/);
    if (fence) {
      flushParagraph();
      closeList();
      if (!code) code = { language: fence[1] || 'text', lines: [] };
      else {
        out.push(`<div class="code-block-wrapper"><div class="code-header"><span class="code-lang">${escapeHtml(code.language)}</span></div><pre><code>${escapeHtml(code.lines.join('\n'))}</code></pre></div>`);
        code = null;
      }
      continue;
    }
    if (code) {
      code.lines.push(line);
      continue;
    }
    const heading = line.match(/^\s*(#{1,4})\s+(.+?)\s*#*\s*$/);
    if (heading) {
      const level = heading[1].length;
      const title = heading[2].replace(/\s+#+\s*$/, '');
      if (level === 1) {
        flushParagraph();
        closeList();
        continue;
      }
      if (level === 2) {
        closeSection();
        sectionIndex += 1;
        sectionOpen = true;
        out.push(`<div class="section" id="${idPrefix}-s${sectionIndex}"><h2>${inlineMarkdown(title, sourceFile)}</h2>`);
      } else {
        flushParagraph();
        closeList();
        out.push(`<h${Math.min(level, 4)}>${inlineMarkdown(title, sourceFile)}</h${Math.min(level, 4)}>`.replaceAll('<h2>', '<h3>').replaceAll('</h2>', '</h3>'));
      }
      continue;
    }
    if (/^\s*\|.*\|\s*$/.test(line) && index + 1 < lines.length && /^\s*\|?\s*:?-{3,}/.test(lines[index + 1])) {
      flushParagraph();
      closeList();
      const tableLines = [line, lines[index + 1]];
      index += 2;
      while (index < lines.length && /^\s*\|.*\|\s*$/.test(lines[index])) tableLines.push(lines[index++]);
      index -= 1;
      out.push(renderTable(tableLines, sourceFile));
      continue;
    }
    const item = line.match(/^\s*([-*+] |\d+[.)] )(.+)$/);
    if (item) {
      flushParagraph();
      const ordered = /^\d/.test(item[1]);
      const type = ordered ? 'ol' : 'ul';
      if (list && list !== type) closeList();
      if (!list) { list = type; out.push(`<${list}>`); }
      out.push(`<li>${inlineMarkdown(item[2], sourceFile)}</li>`);
      continue;
    }
    if (/^\s*>/.test(line)) {
      flushParagraph();
      closeList();
      out.push(`<blockquote>${inlineMarkdown(line.replace(/^\s*>\s?/, ''), sourceFile)}</blockquote>`);
      continue;
    }
    if (/^\s*([-*_])\s*\1\s*\1/.test(line)) {
      flushParagraph();
      closeList();
      out.push('<hr>');
      continue;
    }
    if (/^\s*<\/?[a-z][^>]*>/i.test(line) || /^\s*<!--/.test(line) || /^\s*\|/.test(line)) {
      flushParagraph();
      closeList();
      if (line.trim()) out.push(line);
      continue;
    }
    if (!line.trim()) {
      flushParagraph();
      closeList();
      continue;
    }
    paragraph.push(line.trim());
  }
  if (code) out.push(`<div class="code-block-wrapper"><div class="code-header"><span class="code-lang">${escapeHtml(code.language)}</span></div><pre><code>${escapeHtml(code.lines.join('\n'))}</code></pre></div>`);
  closeSection();
  return out.join('\n');
}

const sources = [
  {
    id: 'llm-happy-transformer',
    title: '🔬 Happy-LLM：Transformer 架构原文融合',
    subtitle: '从注意力机制、位置编码到完整 Encoder-Decoder，实现级拆解',
    files: ['docs/chapter2/第二章 Transformer架构.md', 'Extra-Chapter/transformer-architecture/readme.md'],
  },
  {
    id: 'llm-build-from-scratch',
    title: '🧱 Happy-LLM：从零搭建 LLaMA2',
    subtitle: 'RMSNorm、RoPE、多头注意力、Tokenizer 与小型预训练实践',
    files: ['docs/chapter5/第五章 动手搭建大模型.md'],
  },
  {
    id: 'llm-happy-training',
    title: '⚙️ Happy-LLM：训练、对齐与微调实践',
    subtitle: '预训练数据管线、DeepSpeed、LoRA/QLoRA 与偏好对齐',
    files: ['docs/chapter6/第六章 大模型训练流程实践.md', 'docs/chapter6/6.4[WIP] 偏好对齐.md', 'Extra-Chapter/why-fine-tune-small-large-language-models/readme.md'],
  },
  {
    id: 'llm-happy-rag-agent',
    title: '🔍 Happy-LLM：评测、RAG 与 Agent 应用',
    subtitle: '从基准评测到 Tiny-Agent，串起大模型应用落地链路',
    files: ['docs/chapter7/第七章 大模型应用.md'],
  },
  {
    id: 'llm-agentic-rl',
    title: '🎯 Happy-LLM：Agentic RL 实践',
    subtitle: 'GRPO、OPD、Search-R1 与 ReTool 的训练机制和工程边界',
    files: ['docs/chapter8/第八章 大模型强化学习.md'],
  },
  {
    id: 'llm-happy-extras',
    title: '🧰 Happy-LLM：扩展专题与工程案例',
    subtitle: '文本数据处理、生成策略、思考预算、多模态微调与 CDDRS',
    files: [
      'Extra-Chapter/text-data-processing/readme.md',
      'Extra-Chapter/generation-method/readme.md',
      'Extra-Chapter/s1-vllm-thinking-budget/readme.md',
      'Extra-Chapter/vlm-concatenation-finetune/README.md',
      'Extra-Chapter/CDDRS/readme.md',
    ],
  },
];

function sourceSection(source) {
  const blocks = source.files.map((relative, index) => {
    const file = path.join(HAPPY_ROOT, relative);
    if (!fs.existsSync(file)) return `<div class="callout callout-warn"><p>⚠️ 来源文件不存在：${escapeHtml(relative)}</p></div>`;
    const title = path.basename(relative, path.extname(relative));
    return `<div class="happy-source-file"><div class="source-file-label">来源文件 · ${escapeHtml(relative)}</div>${renderMarkdown(fs.readFileSync(file, 'utf8'), file, `${source.id}-src${index + 1}`)}</div>`;
  }).join('\n');
  return `<section class="llm-mod llm-m-happy-source ${source.id}" id="${source.id}"><div class="module-wrapper"><div class="llm-hero" style="--hero-a:#0f766e;--hero-b:#2563eb;"><h1>${source.title}</h1><p>${source.subtitle}</p><div class="source-meta">来源：Datawhale China · Happy-LLM（本地离线融合）</div></div><div class="callout callout-info"><p>📌 本节保留 Happy-LLM 原教程的概念、公式、代码与图示，并按 Tech Reader 的单文件离线规范转换。外部图片已复制并登记为本地资源。</p></div>${blocks}<div class="footer">Happy-LLM 融合内容 · 建议结合左侧路径和术语卡交叉阅读</div></div></section>`;
}

const galleryId = 'llm-happy-images';
const galleryEntries = imageFiles
  .sort((a, b) => path.relative(HAPPY_ROOT, a).localeCompare(path.relative(HAPPY_ROOT, b)))
  .map((file) => ({ file, id: registerImage(file), rel: path.relative(HAPPY_ROOT, file).replaceAll('\\', '/') }))
  .map(({ id, rel }) => `<figure class="happy-gallery-item"><img src="@asset/${id}" alt="${escapeHtml(rel)}" loading="lazy"><figcaption>${escapeHtml(rel)}</figcaption></figure>`)
  .join('\n');
const gallery = `<section class="llm-mod llm-m-happy-source llm-m-happy-images" id="${galleryId}"><div class="module-wrapper"><div class="llm-hero" style="--hero-a:#7c3aed;--hero-b:#db2777;"><h1>🖼️ Happy-LLM 图片资源库</h1><p>109 张教程与专题图片全部本地内嵌，离线打开也可查看</p><div class="source-meta">资源登记：src/assets/manifest.json · 无文件大小上限</div></div><details class="happy-gallery-details"><summary>展开全部图片（${imageFiles.length} 张）</summary><div class="happy-gallery">${galleryEntries}</div></details></div></section>`;

for (const source of sources) {
  const file = path.join(LLM_ROOT, 'sections', source.id.replace(/^llm-/, ''), 'content.html');
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(file, sourceSection(source), 'utf8');
}
const galleryFile = path.join(LLM_ROOT, 'sections', 'happy-images', 'content.html');
fs.mkdirSync(path.dirname(galleryFile), { recursive: true });
fs.writeFileSync(galleryFile, gallery, 'utf8');

const moduleFile = path.join(LLM_ROOT, 'module.json');
const module = JSON.parse(fs.readFileSync(moduleFile, 'utf8'));
const additions = [
  ...sources.map((source) => ({ id: source.id, file: `sections/${source.id.replace(/^llm-/, '')}/content.html` })),
  { id: galleryId, file: 'sections/happy-images/content.html' },
];
for (const addition of additions) if (!module.content.some((item) => item.id === addition.id)) module.content.push(addition);
const group = { g: 1, t: '📚 Happy-LLM 原文融合', fold: 1, c: [
  ...sources.map((source) => ({ id: source.id, t: source.title.replace(/^\S+\s*/, '') })),
  { id: galleryId, t: '🖼️ 图片资源库（109张）' },
]};
module.toc = module.toc.filter((entry) => entry.t !== group.t);
module.toc.push(group);
module.subtitle = '系统学习指南 · 65 个模块 · 八大领域 · Happy-LLM 融合版';
fs.writeFileSync(moduleFile, `${JSON.stringify(module, null, 2)}\n`, 'utf8');
writeJson(ASSET_MANIFEST, manifest);
console.log(`已生成 ${sources.length} 个融合章节和 1 个图片资源库`);
console.log(`已登记 Happy-LLM 图片 ${imageFiles.length} 张，当前资源总数 ${Object.keys(manifest).length}`);
