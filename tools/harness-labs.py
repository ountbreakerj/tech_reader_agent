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
MODULES = {
    "harness": (ROOT / "src/modules/harness", "chapters"),
    "dsh": (ROOT / "src/modules/dsh", "chapters"),
}

LAB_BLOCK = re.compile(r'<code class="lang-python" data-lab="([^"]+)">(.*?)</code>', re.S)
SRC_REF = re.compile(r'<code class="inl src-ref" data-src="([^"]+)" data-expect="([^"]*)">')
TAG = re.compile(r"<[^>]+>")
_blob_cache: dict[tuple[str, str], list[str] | None] = {}


def chapter_files() -> list[Path]:
    files: list[Path] = []
    for base, sub in MODULES.values():
        files += sorted((base / sub).glob("*/content.html"))
    return files


def lab_source(ref: str) -> str:
    """按 `文件::名字` 取出定义的完整源码（含装饰器），或整个文件。"""
    file, _, name = ref.partition("::")
    text = (ROOT / file).read_text(encoding="utf-8")
    if not name:
        return text.rstrip("\n")
    tree = ast.parse(text)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == name:
            start = min([node.lineno] + [d.lineno for d in node.decorator_list])
            lines = text.splitlines()[start - 1 : node.end_lineno]
            return "\n".join(lines)
    raise KeyError(f"{file} 里没有 {name}")


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
    base, sub = MODULES[module_key]
    config_path = base / "module.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    drafts = sorted(draft_dir.glob(f"{prefix}-[0-9][0-9].html"))
    if not drafts:
        raise SystemExit(f"{draft_dir} 里没有 {prefix}-NN.html")
    by_id = {item["id"]: item for item in config["content"]}
    toc_by_id = {entry.get("id"): entry for entry in config["toc"] if not entry.get("g")}
    for draft in drafts:
        chapter_id = draft.stem
        number = int(chapter_id.split("-")[1])
        label = "导读" if number == 0 else f"第 {number} 章"
        section, title, toc = render_chapter(draft.read_text(encoding="utf-8"), chapter_id, label)
        out = base / sub / chapter_id / "content.html"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(section, encoding="utf-8", newline="\n")
        by_id.setdefault(chapter_id, {"id": chapter_id, "sectionId": f"ch-{chapter_id}",
                                      "file": f"{sub}/{chapter_id}/content.html"})
        entry = {"id": chapter_id, "t": f"{number:02d} · {title}", "c": toc}
        toc_by_id[chapter_id] = entry
        print(f"集成 {chapter_id}：{title}（{len(toc)} 个小节）")
    config["content"] = [by_id[k] for k in sorted(by_id, key=_chapter_sort)]
    config["toc"] = [toc_by_id[k] for k in sorted(toc_by_id, key=_chapter_sort)]
    config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    link_home(base, prefix, {c["id"] for c in config["content"]})


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


def _chapter_sort(chapter_id: str) -> tuple[int, str]:
    # 目录页排最前，收尾文件（关闭 .module 容器）排最后，章节按编号夹在中间
    tail = chapter_id.rsplit("-", 1)[-1]
    if tail == "end":
        return (10_000, chapter_id)
    return (int(tail) if tail.isdigit() else -1, chapter_id)


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
