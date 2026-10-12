"""长期记忆存储与指令文件：两类“跨会话持久化的知识”。

1. **指令文件**（AGENTS.md / CLAUDE.md）：人写的、受版本控制的规则。harness 只负责
   找到它们、按“由宽到窄”排好、包进一个模型认得的框里。信任级别高于记忆，但低于
   系统提示和用户当轮的直接指令。
2. **记忆条目**：agent 自己（或后台提取）写的笔记。一条一个 Markdown 文件 +
   frontmatter，外加一个 ``MEMORY.md`` 索引（一行一个指针，随时可以从目录重建）。

几条设计不变量：

- **目录是真相，索引是缓存**。读取一律扫目录；索引坏了就重建，绝不反过来。
- **slug 是记忆的身份**。同名（slug 相同）就是更新同一个文件，不另起一份。
- **写入要原子、要上锁**。临时文件 + ``os.replace``，外加一个目录级锁文件。
- **索引永远整份重写**，不做“字符串里没有就追加”（OpenHarness 的子串判断会漏）。

frontmatter 只支持一个手写的 YAML 子集：``key: value`` 与 ``key:`` 下的 ``  - item``
列表、``key: []`` 空列表。写出时一律双引号转义，所以生成的文本仍是合法 YAML。

只用标准库，Python 3.10+。
"""

from __future__ import annotations

import os
import re
import tempfile
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path

# 类型划分与 OpenHarness schema.py:22、learn-claude-code s09:50 相同。
MEMORY_TYPES = ("user", "feedback", "project", "reference")
REQUIRED_FIELDS = ("name", "description", "type")
FIELD_ORDER = ("name", "description", "type", "created_at", "updated_at", "tags")
INDEX_NAME = "MEMORY.md"
LOCK_NAME = ".memory.lock"
MAX_SLUG_LEN = 64

# 与索引同名（大小写不敏感的文件系统上 memory.md 就是 MEMORY.md），
# 以及 Windows 的保留设备名，都不能直接当文件名。
_RESERVED_STEMS = frozenset(
    {"memory", "con", "prn", "aux", "nul"}
    | {f"com{i}" for i in range(1, 10)}
    | {f"lpt{i}" for i in range(1, 10)}
)
_SLUG_RE = re.compile(r"[^\w]+")
_KEY_RE = re.compile(r"[a-z_][a-z0-9_]*")
_POINTER_RE = re.compile(r"\]\(([\w-]+\.md)\)")
_UNESCAPE = {"n": "\n", "r": "\r", '"': '"', "\\": "\\"}

# 指令文件：查找规则对照 DSH packages/context/agent-instructions/src/config.ts:11-12。
INSTRUCTION_NAMES = ("AGENTS.md", "CLAUDE.md")
ROOT_MARKERS = (".git",)
REMINDER_OPEN = "<system-reminder>"
REMINDER_CLOSE = "</system-reminder>"
_CLOSE_RE = re.compile(r"</\s*system-reminder\s*>", re.IGNORECASE)
INSTRUCTIONS_INTRO = (
    "以下是工作区指令文件，按由宽到窄排列；更具体的指令优先于更宽泛的。"
    "它们不覆盖系统、开发者或用户的直接指令。"
)


class InvalidMemory(ValueError):
    """记忆条目或 frontmatter 不合法。"""


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


# --- frontmatter：最小 YAML 子集 ------------------------------------------------


def _quote(text: str) -> str:
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return '"' + escaped.replace("\n", "\\n").replace("\r", "\\r") + '"'


def _unquote(value: str) -> str:
    if not value.startswith('"'):
        return value  # 人手写的裸值，例如 type: user
    if len(value) < 2 or not value.endswith('"'):
        raise InvalidMemory(f"引号没有闭合：{value!r}")
    out: list[str] = []
    chars = iter(value[1:-1])
    for ch in chars:
        if ch == "\\":
            nxt = next(chars, "")
            if nxt not in _UNESCAPE:
                raise InvalidMemory(f"不支持的转义：\\{nxt}")
            out.append(_UNESCAPE[nxt])
        elif ch == '"':
            raise InvalidMemory(f"字符串中间出现未转义的引号：{value!r}")
        else:
            out.append(ch)
    return "".join(out)


def dump_frontmatter(meta: dict[str, object]) -> str:
    lines = ["---"]
    for key, value in meta.items():
        if not _KEY_RE.fullmatch(key):
            raise InvalidMemory(f"非法字段名：{key!r}")
        if isinstance(value, (list, tuple)):
            lines.append(f"{key}: []" if not value else f"{key}:")
            lines.extend(f"  - {_quote(str(item))}" for item in value)
        else:
            lines.append(f"{key}: {_quote(str(value))}")
    lines.append("---")
    return "\n".join(lines) + "\n"


def parse_frontmatter(lines: Sequence[str]) -> dict[str, object]:
    meta: dict[str, object] = {}
    current: list[str] | None = None
    for raw in lines:
        line = raw.rstrip("\r")
        if not line.strip():
            continue
        if line.startswith("  - ") and current is not None:
            current.append(_unquote(line[4:].strip()))
            continue
        key, sep, value = line.partition(":")
        if not sep or not _KEY_RE.fullmatch(key) or key in meta:
            raise InvalidMemory(f"无法解析的 frontmatter 行：{line!r}")
        value = value.strip()
        current = [] if value == "" else None
        meta[key] = current if current is not None else [] if value == "[]" else _unquote(value)
    return meta


def split_memory_text(text: str) -> tuple[dict[str, object], str]:
    lines = text.split("\n")
    if lines[0].rstrip("\r") != "---":
        raise InvalidMemory("文件不是以 --- 开头的 frontmatter")
    for end in range(1, len(lines)):
        if lines[end].rstrip("\r") == "---":
            return parse_frontmatter(lines[1:end]), "\n".join(lines[end + 1 :]).strip()
    raise InvalidMemory("frontmatter 没有闭合")


def render_memory(meta: dict[str, object], body: str) -> str:
    ordered = {k: meta[k] for k in FIELD_ORDER if k in meta}
    ordered.update({k: v for k, v in meta.items() if k not in ordered})
    return dump_frontmatter(ordered) + "\n" + body.strip() + "\n"


# --- 条目：校验、slug、原子写、锁 ------------------------------------------------


@dataclass(frozen=True)
class MemoryEntry:
    slug: str
    name: str
    description: str
    type: str
    body: str
    created_at: str = ""
    updated_at: str = ""
    tags: tuple[str, ...] = ()
    # 人手工加的字段（例如 owner）原样保留，agent 更新时不能把它们抹掉。
    extra: dict[str, object] = field(default_factory=dict)


def validate_meta(meta: dict[str, object]) -> None:
    for key in REQUIRED_FIELDS:
        value = meta.get(key)
        if not isinstance(value, str) or not value.strip():
            raise InvalidMemory(f"缺少必填字段：{key}")
    if meta["type"] not in MEMORY_TYPES:
        raise InvalidMemory(f"未知记忆类型：{meta['type']!r}，可选 {MEMORY_TYPES}")
    for key in ("name", "description"):
        if any(ch in str(meta[key]) for ch in "\r\n"):
            raise InvalidMemory(f"{key} 必须是单行：索引一行一条，换行会把索引拆坏")
    for key in ("created_at", "updated_at"):
        if not isinstance(meta.get(key, ""), str):
            raise InvalidMemory(f"{key} 必须是字符串")
    tags = meta.get("tags", [])
    if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
        raise InvalidMemory("tags 必须是字符串列表")


def parse_entry(slug: str, text: str) -> MemoryEntry:
    meta, body = split_memory_text(text)
    validate_meta(meta)
    return MemoryEntry(
        slug=slug,
        name=str(meta["name"]),
        description=str(meta["description"]),
        type=str(meta["type"]),
        body=body,
        created_at=str(meta.get("created_at", "")),
        updated_at=str(meta.get("updated_at", "")),
        tags=tuple(meta.get("tags", [])),  # type: ignore[arg-type]
        extra={k: v for k, v in meta.items() if k not in FIELD_ORDER},
    )


def slugify(name: str) -> str:
    """名字 → 文件名。非单词字符一律换成 -，所以 / \\ 和 .. 都进不了文件名。"""
    slug = _SLUG_RE.sub("-", name.strip().lower()).strip("-_")[:MAX_SLUG_LEN].rstrip("-_")
    if not slug:
        raise InvalidMemory(f"名字里没有能当文件名的字符：{name!r}")
    if slug in _RESERVED_STEMS:
        slug += "-note"
    return slug


def atomic_write_text(path: Path, text: str) -> None:
    """同目录临时文件 + os.replace：读者要么看到旧文件，要么看到新文件。"""
    # 后缀用 .tmp 而不是 .md，否则扫描目录时会把写了一半的临时文件当成记忆。
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


@contextmanager
def file_lock(path: Path, *, timeout: float = 5.0, stale_after: float = 60.0) -> Iterator[None]:
    """O_CREAT | O_EXCL 是原子的：同一时刻只有一个进程能把锁文件创建出来。"""
    deadline = time.monotonic() + timeout
    while True:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            _break_if_stale(path, stale_after)
            if time.monotonic() >= deadline:
                raise TimeoutError(f"拿不到记忆锁：{path}") from None
            time.sleep(0.05)
    try:
        try:
            os.write(fd, f"{os.getpid()}\n".encode())
        finally:
            os.close(fd)
        yield
    finally:
        path.unlink(missing_ok=True)


def _break_if_stale(path: Path, stale_after: float) -> None:
    # 持锁进程崩溃后锁文件会一直留着，只能靠年龄判定过期。两个进程同时判定过期时
    # 仍有竞态；需要更强保证时用 fcntl / msvcrt 的内核锁。
    try:
        age = time.time() - path.stat().st_mtime
    except FileNotFoundError:
        return
    if age > stale_after:
        path.unlink(missing_ok=True)


# --- 目录型存储 + MEMORY.md 索引 ------------------------------------------------


def render_index(entries: Sequence[MemoryEntry]) -> str:
    def plain(text: str) -> str:
        # 方括号换成圆括号：否则名字或描述里的 "](evil.md)" 能在索引里伪造出第二个指针。
        return text.replace("[", "(").replace("]", ")")

    lines = ["# Memory Index", ""]
    for entry in entries:
        lines.append(f"- [{plain(entry.name)}]({entry.slug}.md) — {plain(entry.description)}")
    return "\n".join(lines) + "\n"


class MemoryStore:
    """一个目录 = 一个记忆库。目录是真相，MEMORY.md 只是可以随时重建的缓存。"""

    def __init__(self, root: str | Path, *, clock: Callable[[], str] = utc_now) -> None:
        self.root = Path(root)
        self.index_path = self.root / INDEX_NAME
        self.lock_path = self.root / LOCK_NAME
        self._clock = clock

    def path_for(self, slug: str) -> Path:
        # 第二道防线：slug 已经滤掉了分隔符，这里再确认解析后仍直接位于根目录下。
        root = self.root.resolve()
        path = (root / f"{slug}.md").resolve()
        if path.parent != root:
            raise InvalidMemory(f"路径逃出了记忆目录：{slug!r}")
        return path

    def save(
        self,
        name: str,
        *,
        memory_type: str,
        description: str,
        body: str,
        tags: Sequence[str] = (),
    ) -> MemoryEntry:
        """同名（slug 相同）就更新原文件：保留 created_at 与人手工加的字段。"""
        if not body.strip():
            raise InvalidMemory("正文不能为空")
        slug = slugify(name)
        self.root.mkdir(parents=True, exist_ok=True)
        with file_lock(self.lock_path):
            path = self.path_for(slug)
            old = parse_entry(slug, path.read_text(encoding="utf-8")) if path.exists() else None
            now = self._clock()
            meta: dict[str, object] = {
                **(old.extra if old else {}),
                "name": name.strip(),
                "description": description.strip(),
                "type": memory_type,
                "created_at": old.created_at if old and old.created_at else now,
                "updated_at": now,
                "tags": list(dict.fromkeys(tag.strip() for tag in tags if tag.strip())),
            }
            validate_meta(meta)
            atomic_write_text(path, render_memory(meta, body))
            self._write_index_locked()
        return parse_entry(slug, path.read_text(encoding="utf-8"))

    def get(self, name: str) -> MemoryEntry | None:
        path = self.path_for(slugify(name))
        return parse_entry(path.stem, path.read_text(encoding="utf-8")) if path.exists() else None

    def delete(self, name: str) -> bool:
        path = self.path_for(slugify(name))
        with file_lock(self.lock_path):
            if not path.exists():
                return False
            path.unlink()
            self._write_index_locked()
        return True

    def scan(self) -> tuple[list[MemoryEntry], list[str]]:
        """扫目录：返回（合法条目, 解析失败的文件名）。坏文件跳过但要报出来。"""
        entries: list[MemoryEntry] = []
        broken: list[str] = []
        if not self.root.is_dir():
            return entries, broken
        for path in sorted(self.root.glob("*.md")):
            if path.name.lower() == INDEX_NAME.lower():
                continue
            try:
                entries.append(parse_entry(path.stem, path.read_text(encoding="utf-8")))
            except (InvalidMemory, OSError, UnicodeDecodeError):
                broken.append(path.name)
        return entries, broken

    def list(self, memory_type: str | None = None) -> list[MemoryEntry]:
        if memory_type is not None and memory_type not in MEMORY_TYPES:
            raise InvalidMemory(f"未知记忆类型：{memory_type!r}")
        entries, _ = self.scan()
        return [e for e in entries if memory_type is None or e.type == memory_type]

    def index_drift(self) -> tuple[set[str], set[str]]:
        """（目录里有、索引里没有的文件, 索引指向、目录里没有的文件）。"""
        on_disk = {f"{e.slug}.md" for e in self.scan()[0]}
        text = self.index_path.read_text(encoding="utf-8") if self.index_path.exists() else ""
        pointed = set(_POINTER_RE.findall(text))
        return on_disk - pointed, pointed - on_disk

    def rebuild_index(self) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        with file_lock(self.lock_path):
            self._write_index_locked()
        return self.index_path

    def _write_index_locked(self) -> None:
        # 每次整份重写，而不是“没出现过就追加一行”：删除、改名、改描述都能同步到索引。
        atomic_write_text(self.index_path, render_index(self.scan()[0]))


# --- 指令文件：查找、合并、渲染 --------------------------------------------------


@dataclass(frozen=True)
class InstructionFile:
    path: Path
    scope: str  # "user"（用户级）或相对项目根的目录，例如 "." / "pkg/api"
    content: str


def find_project_root(start: Path, markers: Sequence[str] = ROOT_MARKERS) -> Path | None:
    for directory in (start, *start.parents):
        if any((directory / marker).exists() for marker in markers):
            return directory
    return None


def find_instruction_files(
    start_dir: str | Path,
    stop_at: str | Path | None = None,
    *,
    names: Sequence[str] = INSTRUCTION_NAMES,
    user_dir: str | Path | None = None,
    max_file_bytes: int = 1_048_576,
) -> list[InstructionFile]:
    """由远到近：用户级 → 项目根 → … → start_dir。

    合并规则：不做语义合并，全部按这个顺序拼接，越靠后越具体，冲突时以后者为准
    （渲染时把这条规则写给模型）。同一目录里内容相同的文件只保留第一个。
    停止条件：给了 stop_at 就停在它（含）；否则停在第一个含 .git 的祖先（含）；
    都没有就只看 start_dir，不会一路找到磁盘根目录。
    """
    start = Path(start_dir).resolve()
    top = Path(stop_at).resolve() if stop_at is not None else find_project_root(start) or start
    if top != start and top not in start.parents:
        raise ValueError(f"stop_at 必须是 start_dir 或它的祖先：{top}")
    chain = [start]
    while chain[-1] != top:
        chain.append(chain[-1].parent)
    scoped = [(d.relative_to(top).as_posix(), d) for d in reversed(chain)]
    if user_dir is not None:
        scoped.insert(0, ("user", Path(user_dir).resolve()))
    found: list[InstructionFile] = []
    seen_paths: set[Path] = set()
    for scope, directory in scoped:
        seen_bodies: set[str] = set()
        for name in names:
            path = directory / name
            if path in seen_paths or not path.is_file() or path.stat().st_size > max_file_bytes:
                continue
            content = path.read_text(encoding="utf-8", errors="replace")
            if content.strip() in seen_bodies:
                continue  # 典型情况：CLAUDE.md 是 AGENTS.md 的软链接或拷贝
            seen_paths.add(path)
            seen_bodies.add(content.strip())
            found.append(InstructionFile(path, scope, content))
    return found


def escape_reminder(text: str) -> str:
    """把正文里的结束标签变形：指令文件就没法提前闭合外框、把后文伪装成系统区。"""
    return _CLOSE_RE.sub("<\\\\/system-reminder>", text)


def _frame(files: Sequence[InstructionFile], omitted: int, truncated: bool = False) -> str:
    notes = [f"（预算不足：省略了 {omitted} 个更宽泛的指令文件）"] if omitted else []
    if truncated:
        notes.append("（预算不足：最具体的那个指令文件被截断）")
    sections = [f"来自 {f.scope}/{f.path.name}：\n\n{f.content.strip()}" for f in files]
    body = "\n\n".join([INSTRUCTIONS_INTRO, *notes, *sections])
    # 路径、作用域、正文都在 body 里，一起转义；外框的开闭标签由这里独占。
    return f"{REMINDER_OPEN}\n{escape_reminder(body)}\n{REMINDER_CLOSE}"


def render_instructions(files: Sequence[InstructionFile], max_bytes: int = 32_000) -> str:
    """超预算时先丢最宽泛的文件，最后才截断最具体的那个（与 DSH render.ts 的顺序相同）。"""
    if not files or max_bytes <= 0:
        return ""
    kept = list(files)
    while len(kept) > 1 and len(_frame(kept, len(files) - len(kept)).encode()) > max_bytes:
        kept.pop(0)
    omitted = len(files) - len(kept)
    if len(text := _frame(kept, omitted).encode()) <= max_bytes:
        return text.decode()
    last = kept[-1]
    cut = last.content.encode()[: max_bytes].decode(errors="ignore")
    while cut and len(_frame([replace(last, content=cut)], omitted, True).encode()) > max_bytes:
        cut = cut[: len(cut) * 9 // 10]  # 每次砍掉一成：转义会让长度变化，不能一步算准
    return _frame([replace(last, content=cut)], omitted, True) if cut else ""
