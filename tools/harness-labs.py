"""Harness 模块的 labs 测试、代码一致性和源码引用核对，以及草稿集成。

用法（从项目根目录）：
  py tools/harness-labs.py check              运行 labs 测试 + 核对正文代码块与 src-ref
  py tools/harness-labs.py integrate <dir>    把草稿目录里的 hx-NN.html / dsh-NN.html 组装进模块
"""
from __future__ import annotations

import ast
import html
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LLM_ROOT = ROOT.parent / "LLM"
REPOS = {
    "dsh": ("DSH/deepseek-harness", "d743267388641bc76f17c45ce8b4c231aed1d32c"),
    "oh": ("01-agent-harness/OpenHarness", "9b2efd795c6aa09f88b0c257d269a9e518da6ae7"),
    "lcc": ("02-claude-code/learn-claude-code", "ce8f9f186058939da54c9d6fead78dfb5d0fd6c3"),
    "amt": ("03-agent-memory/Agent_Memory_Techniques", "4767c0341ccd8b714147334c5f17c26ff99d8e41"),
    "lhe": ("01-agent-harness/learn-harness-engineering", "38ddcd2bf8d65271f668b94e7c875ca1d629d622"),
}
# (模块目录, 章节子目录, 是否给每章单独包一层 .book-scope)
# harness 的 .book-scope 由 home.html / end.html 统一包住；dsh 里新旧排版并存，每章自己包
MODULES = {
    "harness": (ROOT / "src/modules/harness", "chapters", False),
    "dsh": (ROOT / "src/modules/dsh", "chapters", True),
}

LAB_BLOCK = re.compile(r'<code class="lang-python" data-lab="([^"]+)">(.*?)</code>', re.S)
SRC_REF = re.compile(r'<code class="inl src-ref" data-src="([^"]+)" data-expect="([^"]*)">')
TAG = re.compile(r"<[^>]+>")
_blob_cache: dict[tuple[str, str], list[str] | None] = {}


def chapter_files() -> list[Path]:
    files: list[Path] = []
    for base, sub, _ in MODULES.values():
        files += sorted((base / sub).glob("*/content.html"))
    return files


def lab_source(ref: str) -> str:
    """按 `文件::名字` 取出定义的完整源码（含装饰器），或整个文件。

    名字可以是 `类.方法` 这样的点号路径；方法摘录保留它在类里的原缩进。
    """
    file, _, name = ref.partition("::")
    text = (ROOT / file).read_text(encoding="utf-8")
    if not name:
        return text.rstrip("\n")
    defs = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
    module_body: list[ast.stmt] = ast.parse(text).body
    scope = module_body
    node = None
    for part in name.split("."):
        node = next((n for n in scope if isinstance(n, defs) and n.name == part), None)
        if node is None:
            break
        scope = node.body
    if node is None and "." not in name:
        # 模块级常量（如提示词模板）：`NAME = ...` / `NAME: T = ...`
        for stmt in module_body:
            targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target] if isinstance(stmt, ast.AnnAssign) else []
            if any(isinstance(t, ast.Name) and t.id == name for t in targets):
                node = stmt
                break
    if node is None and "." not in name:
        # 没写类名的方法：在所有顶层类里找，必须唯一，避免摘错同名方法
        hits = [m for c in module_body if isinstance(c, ast.ClassDef)
                for m in c.body if isinstance(m, defs) and m.name == name]
        node = hits[0] if len(hits) == 1 else None
        if len(hits) > 1:
            raise KeyError(f"{file} 里有 {len(hits)} 个 {name}，请写成 类名.{name}")
    if node is None:
        raise KeyError(f"{file} 里没有 {name}")
    start = min([node.lineno] + [d.lineno for d in getattr(node, "decorator_list", [])])
    return "\n".join(text.splitlines()[start - 1 : node.end_lineno])


def repo_lines(key: str, path: str) -> list[str] | None:
    """从钉住的 commit 读文件（git show），不受工作区改动影响。"""
    cache_key = (key, path)
    if cache_key not in _blob_cache:
        rel, sha = REPOS[key]
        proc = subprocess.run(
            ["git", "-C", str(LLM_ROOT / rel), "show", f"{sha}:{path}"],
            capture_output=True,
        )
        _blob_cache[cache_key] = (
            proc.stdout.decode("utf-8", "replace").splitlines() if proc.returncode == 0 else None
        )
    return _blob_cache[cache_key]


def check_src_ref(spec: str, expect: str) -> str | None:
    key, _, rest = spec.partition(":")
    if key not in REPOS:
        return f"未知仓库 {key}"
    m = re.fullmatch(r"(.+?):(\d+)(?:-(\d+))?", rest)
    if not m:
        return f"格式错误 {spec}"
    path, start, end = m.group(1), int(m.group(2)), int(m.group(3) or m.group(2))
    lines = repo_lines(key, path)
    if lines is None:
        return f"文件不存在 {spec}"
    if not (1 <= start <= end <= len(lines)):
        return f"行号越界 {spec}（共 {len(lines)} 行）"
    if expect and html.unescape(expect) not in lines[start - 1]:
        return f"行内容不匹配 {spec}：期望含 {html.unescape(expect)!r}，实际 {lines[start - 1].strip()[:80]!r}"
    return None


def check_chapters() -> list[str]:
    errors: list[str] = []
    refs = labs = 0
    for file in chapter_files():
        text = file.read_text(encoding="utf-8")
        rel = file.relative_to(ROOT)
        for m in LAB_BLOCK.finditer(text):
            labs += 1
            shown = html.unescape(TAG.sub("", m.group(2))).rstrip("\n")
            try:
                actual = lab_source(m.group(1))
            except (OSError, KeyError, SyntaxError) as exc:
                errors.append(f"{rel}: data-lab {m.group(1)} 无法读取：{exc}")
                continue
            if shown != actual:
                errors.append(f"{rel}: 代码块与 {m.group(1)} 不一致")
        for m in SRC_REF.finditer(text):
            refs += 1
            problem = check_src_ref(m.group(1), m.group(2))
            if problem:
                errors.append(f"{rel}: {problem}")
    print(f"核对 {labs} 个 labs 代码块、{refs} 条源码引用")
    return errors


def run_tests() -> bool:
    proc = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "labs/tests", "-t", "labs"],
        cwd=ROOT,
    )
    return proc.returncode == 0


# ---------- 集成：草稿 → 模块章节 ----------

PY_KEYWORDS = {
    "False", "None", "True", "and", "as", "assert", "async", "await", "break", "class",
    "continue", "def", "del", "elif", "else", "except", "finally", "for", "from", "global",
    "if", "import", "in", "is", "lambda", "nonlocal", "not", "or", "pass", "raise",
    "return", "try", "while", "with", "yield", "match", "case",
}
PY_TOKEN = re.compile(
    r'(?P<cmt>#[^\n]*)'
    r'|(?P<str>[rRbBfFuU]{0,2}(?:"""[\s\S]*?"""|\'\'\'[\s\S]*?\'\'\'|"(?:\\.|[^"\\\n])*"|\'(?:\\.|[^\'\\\n])*\'))'
    r'|(?P<dec>^[ \t]*@[\w.]+)'
    r'|(?P<num>\b\d[\d_]*(?:\.\d+)?\b)'
    r'|(?P<name>\b[A-Za-z_]\w*\b)'
    r'|(?P<op>[=+\-*/%<>!&|^~:]+)',
    re.M,
)


def highlight_python(code: str) -> str:
    """与 book 模块相同的 token 类（kw/fn/str/cmt/num/op/dec/cls），保证复制出的文本不变。"""
    out: list[str] = []
    pos = 0
    prev_name = ""
    for m in PY_TOKEN.finditer(code):
        out.append(html.escape(code[pos : m.start()], quote=False))
        text = m.group(0)
        kind = m.lastgroup
        if kind == "name":
            if text in PY_KEYWORDS:
                kind = "kw"
            elif prev_name in ("def",):
                kind = "fn"
            elif prev_name == "class" or text[:1].isupper():
                kind = "cls"
            else:
                kind = None
            prev_name = text
        esc = html.escape(text, quote=False)
        out.append(f'<span class="{kind}">{esc}</span>' if kind else esc)
        pos = m.end()
    out.append(html.escape(code[pos:], quote=False))
    return "".join(out)


TITLE = re.compile(r"<!--\s*title:\s*(.*?)\s*-->")
HEADING = re.compile(r'<h([23]) id="([^"]+)"[^>]*>(.*?)</h\1>', re.S)
# 非 Python 的源码摘录（TS 等）由作者手写；正文里的 `Promise<S>` 一类泛型如果没转义，
# 浏览器会把 <S> 当成删除线标签，后面整段都被划掉
SOURCE_BLOCK = re.compile(r'(<code class="lang-(?!python)[\w-]+"[^>]*>)(.*?)(</code>)', re.S)
ALLOWED_TAGS = {
    "a", "b", "blockquote", "br", "code", "div", "em", "figcaption", "figure", "h2", "h3", "h4",
    "hr", "i", "img", "kbd", "li", "ol", "p", "pre", "small", "span", "strong", "sub", "sup",
    "table", "tbody", "td", "th", "thead", "tr", "ul", "details", "summary",
}


def escape_source_block(match: re.Match[str]) -> str:
    # 先还原已有实体再整体转义，已经转义过的内容不会被二次转义
    return match.group(1) + html.escape(html.unescape(match.group(2)), quote=False) + match.group(3)


def render_chapter(draft: str, chapter_id: str, label: str) -> tuple[str, str, list[dict]]:
    """返回（章节 HTML, 标题, TOC 子项）。代码块从 labs 重新取源码并高亮，保证与文件一致。"""
    m = TITLE.search(draft)
    if not m:
        raise ValueError(f"{chapter_id} 缺少 <!-- title: … --> 注释")
    title = m.group(1)
    body = TITLE.sub("", draft, count=1).strip()

    def lab_repl(match: re.Match[str]) -> str:
        ref = match.group(1)
        return f'<code class="lang-python" data-lab="{ref}">{highlight_python(lab_source(ref))}</code>'

    body = LAB_BLOCK.sub(lab_repl, body)
    # 不带 data-lab 的 Python 块是其他仓库的逐字摘录（紧跟 src-ref），只转义 + 高亮，不和 labs 比对
    body = re.sub(
        r'<code class="lang-python">(.*?)</code>',
        lambda m: f'<code class="lang-python">{highlight_python(html.unescape(TAG.sub("", m.group(1))))}</code>',
        body, flags=re.S)
    body = SOURCE_BLOCK.sub(escape_source_block, body)
    # 属性值里的尖括号（如 data-expect 引用的泛型）浏览器不会当标签，扫描前先去掉属性值
    tag_text = re.sub(r'="[^"]*"', '=""', body)
    stray = [t for t in re.findall(r"</?([A-Za-z][\w-]*)", tag_text) if t not in ALLOWED_TAGS]
    if stray:
        raise ValueError(f"{chapter_id} 有未转义的尖括号（会被浏览器当成标签）：{sorted(set(stray))}")
    # 草稿必须是 HTML：正文文本里残留的 **粗体** 或 `代码` 会原样显示给读者
    prose = re.sub(r"<pre\b.*?</pre>|<code\b.*?</code>|<[^>]+>", " ", body, flags=re.S)
    leaked = re.findall(r"\*\*[^*\n]{1,30}|`[^`\n]{1,30}", html.unescape(prose))
    if leaked:
        raise ValueError(f"{chapter_id} 正文里有 Markdown 残留（改成 <strong> / <code class=\"inl\">）：{leaked[:5]}")
    toc = []
    for h in HEADING.finditer(body):
        text = html.unescape(TAG.sub("", h.group(3))).strip()
        toc.append({"id": h.group(2), "t": text, "lvl": int(h.group(1))})
    section = (
        f'<section class="chapter" id="ch-{chapter_id}" data-slug="{chapter_id}">'
        f'<div class="ch-head"><span class="ch-num">{html.escape(label)}</span></div>'
        f'<h1 id="{chapter_id}">{html.escape(title)}</h1>\n{body}\n</section>\n'
    )
    return section, title, toc


def integrate(draft_dir: Path, module_key: str, prefix: str) -> None:
    base, sub, wrap = MODULES[module_key]
    config_path = base / "module.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    drafts = sorted(draft_dir.glob(f"{prefix}-[0-9][0-9].html"))
    if not drafts:
        raise SystemExit(f"{draft_dir} 里没有 {prefix}-NN.html")
    by_id = {item["id"]: item for item in config["content"]}
    toc_by_id = {entry.get("id"): entry for entry in config["toc"] if not entry.get("g")}
    present = {i for i in by_id if _is_chapter(i, prefix)} | {d.stem for d in drafts}
    for draft in drafts:
        chapter_id = draft.stem
        number = int(chapter_id.split("-")[1])
        label = "导读" if number == 0 else f"第 {number} 章"
        section, title, toc = render_chapter(draft.read_text(encoding="utf-8"), chapter_id, label)
        out = base / sub / chapter_id / "content.html"
        out.parent.mkdir(parents=True, exist_ok=True)
        # 指向尚未集成章节的链接先降级成带标记的纯文本，章节集成后下一次 integrate 自动恢复
        section = re.sub(
            rf'<a href="#({prefix}-\d\d)(-[\w-]+)?">(.*?)</a>|<span class="xref-pending" data-href="#({prefix}-\d\d)(-[\w-]+)?">(.*?)</span>',
            lambda m: _xref(m, present), section)
        if wrap:
            section = f'<div class="book-scope">\n{section}</div>\n'
        out.write_text(section, encoding="utf-8", newline="\n")
        by_id.setdefault(chapter_id, {"id": chapter_id, "sectionId": f"ch-{chapter_id}",
                                      "file": f"{sub}/{chapter_id}/content.html"})
        entry = {"id": chapter_id, "t": f"{number:02d} · {title}", "c": toc}
        toc_by_id[chapter_id] = entry
        print(f"集成 {chapter_id}：{title}（{len(toc)} 个小节）")
    config["content"] = _insert_chapters(list(by_id.values()), prefix)
    config["toc"] = _insert_chapters(
        [e for e in config["toc"] if not _is_chapter(e.get("id"), prefix)] +
        [toc_by_id[k] for k in toc_by_id if _is_chapter(k, prefix)], prefix)
    config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    link_home(base, prefix, {c["id"] for c in config["content"]})


def _xref(m: re.Match[str], present: set[str]) -> str:
    """站内链接：目标章已集成就是 <a>，否则是带 data-href 的 span，二者可来回转换。"""
    chapter, anchor, label = (m.group(1), m.group(2), m.group(3)) if m.group(1) else (m.group(4), m.group(5), m.group(6))
    href = f"#{chapter}{anchor or ''}"
    if chapter in present:
        return f'<a href="{href}">{label}</a>'
    return f'<span class="xref-pending" data-href="{href}">{label}</span>'


def link_home(base: Path, prefix: str, present: set[str]) -> None:
    """目录页里“NN 标题”只有在该章已集成时才变成链接，避免出现死链。"""
    home = base / "home.html"
    if not home.exists():
        return
    text = home.read_text(encoding="utf-8")

    def repl(m: re.Match[str]) -> str:
        num, label = m.group(1), m.group(2)
        cid = f"{prefix}-{num}"
        return f'<a href="#{cid}">{num} {label}</a>' if cid in present else f"{num} {label}"

    # 先把已有链接还原成纯文本，再按当前集成状态重新加链接
    text = re.sub(rf'<a href="#{prefix}-(\d\d)">(\d\d) ([^<]+)</a>', lambda m: f"{m.group(2)} {m.group(3)}", text)
    text = re.sub(r"(?<![\w#-])(\d\d) ([^<·]+?)(?=\s*(?:·|</td>))", repl, text)
    home.write_text(text, encoding="utf-8", newline="\n")


def _is_chapter(item_id: str | None, prefix: str) -> bool:
    return bool(item_id) and re.fullmatch(rf"{prefix}-\d\d", item_id) is not None


def _insert_chapters(items: list[dict], prefix: str) -> list[dict]:
    """章节按编号插在 `<prefix>-home` 之后，其余条目（收尾文件、旧版内容）保持原有顺序。"""
    chapters = sorted((i for i in items if _is_chapter(i.get("id"), prefix)), key=lambda i: i["id"])
    others = [i for i in items if not _is_chapter(i.get("id"), prefix)]
    at = next((n + 1 for n, i in enumerate(others) if i.get("id") == f"{prefix}-home"), 0)
    return others[:at] + chapters + others[at:]


def main(argv: list[str]) -> int:
    if len(argv) >= 2 and argv[1] == "integrate":
        draft_dir = Path(argv[2]) if len(argv) > 2 else ROOT.parent / "harness-drafts"
        prefixes = argv[3:] or ["hx"]
        for prefix in prefixes:
            integrate(draft_dir, "harness" if prefix == "hx" else prefix, prefix)
        return 0
    ok = run_tests()
    errors = check_chapters()
    for e in errors:
        print("  ✗", e)
    if not ok or errors:
        print(f"失败：测试{'通过' if ok else '未通过'}，{len(errors)} 处核对错误")
        return 1
    print("通过")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
