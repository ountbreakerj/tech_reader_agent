"""工具输出卸载（spill）：结果太大时不塞进上下文，落盘后只留预览和路径。

三条设计不变量：

1. **阈值是估算值，不是精确值**。真实 token 数由模型侧决定，harness 只能估算，
   所以阈值取小一点（宁可多卸载一次）比取大一点安全。
2. **文件名由内容哈希决定**。同一份输出重复出现时第二次落盘没有任何收益，反而
   让磁盘和「这个文件对应哪次调用」的对照表一起膨胀——哈希同时解决去重和命名。
3. **只卸载普通文本结果**。错误结果、二进制内容、以及「被截断的 JSON」三种情况
   一旦落盘就会破坏模型对结果的判断（见 ``should_spill`` 的注释）。

对照的三个生产实现：
- DSH ``packages/spill/*``：拆成 spill（接口）/ spill-local（落盘）/ spill-policy（策略）三个包。
- OpenHarness ``services/tool_outputs.py`` + ``engine/query.py:524``：一个函数搞定。
- learn-claude-code ``s08_context_compact/code.py:92``：只切前 50000 字符，不落盘。

只用标准库，Python 3.10+。
"""

from __future__ import annotations

import hashlib
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

# --- 阈值：教学示意，不是任何仓库里的常量 ------------------------------------
# DSH 用 token（maxInlineTokens: 12500），OH 用字符（16000），lcc 直接切 50000。
# 这里统一用字符，让 labs 不依赖分词器；换算比例大致 1 token ≈ 3.5 个英文字符。
DEFAULT_LIMIT_CHARS = 12_000
# 预览的头部/尾部分配。OH 只留头部 3000；DSH 把预算对半分给 head 和 tail，
# 因为报错信息常在输出的最后几行。
DEFAULT_HEAD_CHARS = 2_000
DEFAULT_TAIL_CHARS = 2_000
# 分页读取的默认每页行数，模拟模型后续怎么按需取回。
DEFAULT_PAGE_LINES = 200

TRUNCATION_MARK = "…[中间内容已省略]…"

# 取回工具的保留名。生产实现用各自的文件读取工具：DSH 是 ``read``（offset/limit
# 都是必填正整数，offset 从 1 起），OH 是 ``read_file``（offset/limit 都是行）。
RECALL_TOOL = "read_spill"

# 会破坏预览可读性的字节：NUL 几乎只出现在二进制里。
_BINARY_PROBE = "\x00"


class SpillPathError(ValueError):
    """卸载文件的路径不在会话目录里。取回时读到别的会话的文件就是越权。"""


@dataclass(frozen=True)
class SpillResult:
    """一次卸载（或未卸载）的结果。``spilled`` 为 False 时 ``content`` 就是原样。"""

    content: str
    spilled: bool
    # 未卸载时是 None；已卸载时是落盘文件的绝对路径。
    path: Path | None = None
    original_chars: int = 0
    omitted_chars: int = 0
    # 落盘文件是否被复用了（同一份内容之前已经写过一次）。
    deduplicated: bool = False


def looks_binary(content: str) -> bool:
    """粗略判断是不是二进制内容。

    真正的 harness 在工具层就知道输出的类型（DSH 的工具必须声明 output schema）；
    到了卸载这一步只剩字符串，只能靠特征猜。猜错的方向要选对：把二进制当文本
    交给模型，代价是一堆乱码 token；把文本当二进制不卸载，代价只是上下文变大一点。
    """
    return _BINARY_PROBE in content


def is_complete_json(content: str) -> bool:
    """内容是否是一份完整的 JSON 文档。

    用来区分两种「JSON 结果」：工具返回的完整 JSON（截断不可接受）和一段以 ``{``
    开头的普通日志（截断无所谓）。判断刻意写得很松——只做首尾配对检查，不解析，
    因为解析一份超大 JSON 本身就是要避免的开销。
    """
    stripped = content.strip()
    if not stripped or stripped[0] not in "{[":
        return False
    pairs = {"{": "}", "[": "]"}
    return stripped[-1] == pairs[stripped[0]]


def should_spill(*, is_error: bool, content: str) -> bool:
    """判断一份工具结果能不能卸载。

    三种不卸载的情况，理由各不相同：

    - **错误结果**：错误信息是模型纠正自己的唯一依据，长度通常很小，卸载只会让它
      多一次取回操作。DSH 的 spill-policy 不区分 isError（``retainable()`` 只看
      block 类型），但它的阈值是 12500 token，错误信息根本够不到。
    - **二进制**：预览里是乱码，尾部也是乱码，模型看不出内容是什么。
    - **完整的 JSON**：预览必然把它截断，而模型看到以 ``{`` 开头的内容会当成 JSON
      去解析，然后得到一个「语法错误」——比不卸载更糟。
    """
    if is_error:
        return False
    if looks_binary(content):
        return False
    if is_complete_json(content):
        return False
    return True


def estimate_tokens(text: str) -> int:
    """粗略估算 token 数：教学示意，四个字符一个 token。

    真实实现要么调用厂商的分词器，要么用「字节数 / 3」这种经验值（DSH 用的是
    ``ctx.tokenMeter``，它按模型路由定价，连图片都算得出来）。
    """
    return len(text) // 4


def safe_name(raw: str) -> str:
    """把工具名或 call_id 压成一个安全路径段。

    DSH 的做法更彻底（``encodeSegment``）：逐字符转义，映射是单射的，所以两个
    不同的输入永远不会撞成同一个文件名，``../`` 里的 ``.`` 和 ``/`` 都被转义成
    ``~XXXX``。labs 这里用白名单清洗，代价是两个不同的工具名可能清洗成同一个
    名字——文件名前面还有内容哈希，不影响正确性。
    """
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", raw.strip())
    cleaned = cleaned.strip("._")[:64]
    return cleaned or "tool"


def build_preview(content: str, head_chars: int, tail_chars: int) -> tuple[str, int]:
    """切出头部 + 尾部，返回（预览文本，被省略的字符数）。

    两个细节：

    - 头尾之和可能超过原内容（内容比阈值大但比 head+tail 小），这时直接退化成
      整份内容，不能凭空多出省略标记。
    - 省略量必须按 ``原文长度 - 头 - 尾`` 算，不能用 ``len(TRUNCATION_MARK)``
      之类的近似值——这个数字会写进预览给模型看。
    """
    if len(content) <= head_chars + tail_chars:
        return content, 0
    head = content[:head_chars] if head_chars > 0 else ""
    tail = content[-tail_chars:] if tail_chars > 0 else ""
    omitted = len(content) - len(head) - len(tail)
    return f"{head}{TRUNCATION_MARK}{tail}", omitted


def format_notice(
    *,
    path: Path,
    original_chars: int,
    omitted_chars: int,
    head_chars: int,
    tail_chars: int,
) -> str:
    """给模型看的说明块。这是整章最重要的一段文本。

    它必须回答模型的三个问题：**我看到的不是全部吗**（已省略 N 字符）、**剩下
    的去哪了**（路径）、**我怎么拿回来**（用带 offset/limit 的读取工具）。缺了
    第一条模型会把预览当全文（lcc 的 ``out[:50000]`` 就是这个下场）；缺了第三条
    模型只会说「输出被截断了，我换个命令重试一遍」。
    """
    return (
        f"[工具输出已卸载]\n"
        f"原始大小：{original_chars} 字符（本次只显示头部 {head_chars} 与尾部 {tail_chars} 字符，"
        f"已省略 {omitted_chars} 字符）\n"
        f"完整输出：{path}\n"
        f"取回方式：{RECALL_TOOL}(path, page=N)，每页 {DEFAULT_PAGE_LINES} 行；"
        f"也可以对路径做 grep 检索。\n"
        f"注意：上面只是预览，不是完整内容——不要根据预览的结尾断定输出已结束。"
    )


class SpillStore:
    """把超大工具结果落到会话专属目录，并返回带预览的替换文本。

    目录结构仿 DSH 的 spill-local：``<root>/session-<hash>/<content-hash>-<tool>.txt``。
    会话目录名用会话 id 的哈希，而不是会话 id 本身——会话 id 可能来自配置或命令行，
    直接当路径段用就是一个路径穿越入口。
    """

    def __init__(self, root: Path, session_id: str) -> None:
        self.root = Path(root)
        digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:12]
        self.session_dir = self.root / f"session-{digest}"
        self._written: dict[str, Path] = {}

    def spill(
        self,
        *,
        tool_name: str,
        call_id: str,
        content: str,
        is_error: bool = False,
        limit_chars: int = DEFAULT_LIMIT_CHARS,
        head_chars: int = DEFAULT_HEAD_CHARS,
        tail_chars: int = DEFAULT_TAIL_CHARS,
    ) -> SpillResult:
        """需要就卸载，不需要就原样返回。

        ``call_id`` 目前只用于让调用方把预览和自己的日志对上；DSH 把
        ``{kind: 'tool', toolName, callId, label}`` 整份塞进 spill 存储，
        因为它的后端可能是数据库或远端，需要这份来源信息建索引。
        """
        del call_id  # 本地后端只靠内容哈希和工具名命名，来源信息记在会话日志里。
        if not should_spill(is_error=is_error, content=content):
            return SpillResult(content=content, spilled=False, original_chars=len(content))
        if len(content) <= limit_chars:
            return SpillResult(content=content, spilled=False, original_chars=len(content))

        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
        path = self.session_dir / f"{digest}-{safe_name(tool_name)}.txt"
        deduplicated = digest in self._written
        if not deduplicated:
            self.session_dir.mkdir(parents=True, exist_ok=True)
            # 先写临时文件再原子替换：并发写同一份内容时不会留下半份文件。
            tmp = path.with_suffix(".tmp")
            tmp.write_text(content, encoding="utf-8")
            tmp.replace(path)
            self._written[digest] = path

        preview, omitted = build_preview(content, head_chars, tail_chars)
        notice = format_notice(
            path=path,
            original_chars=len(content),
            omitted_chars=omitted,
            head_chars=head_chars,
            tail_chars=tail_chars,
        )
        return SpillResult(
            content=f"{notice}\n\n--- 预览开始 ---\n{preview}\n--- 预览结束 ---",
            spilled=True,
            path=path,
            original_chars=len(content),
            omitted_chars=omitted,
            deduplicated=deduplicated,
        )

    def read_page(self, path: Path, page: int = 0, page_lines: int = DEFAULT_PAGE_LINES) -> str:
        """按页取回卸载文件，模拟模型后续的读取。

        两件事必须做：**把路径钉回会话目录**（否则模型可以传 ``../`` 去读别的
        会话甚至别的用户的文件），以及**在页尾明确写出「还有没有下一行」**——
        模型无法从一页内容的结尾看出来它是不是文件的结尾。
        """
        resolved = Path(path).resolve()
        base = self.session_dir.resolve()
        if not resolved.is_relative_to(base):
            raise SpillPathError(f"取回路径必须在 {base} 之内：{resolved}")
        if not resolved.is_file():
            raise FileNotFoundError(f"卸载文件不存在（可能已被清理）：{resolved}")
        if page < 0:
            raise ValueError("page 不能为负数")

        lines = resolved.read_text(encoding="utf-8").splitlines()
        start = page * page_lines
        chunk = lines[start:start + page_lines]
        if not chunk:
            return f"[空页] page={page} 超出范围：共 {len(lines)} 行。"
        last = start + len(chunk)
        more = f"还有 {len(lines) - last} 行，用 page={page + 1} 继续。" if last < len(lines) else "这是最后一页。"
        return f"[page={page} 第 {start + 1}-{last} 行 / 共 {len(lines)} 行] {more}\n" + "\n".join(chunk)


def spill_directory(session_id: str, prefix: str = "labs-spill-") -> Path:
    """建一个临时卸载目录（教学用）。生产实现应该放在会话目录旁边，
    这样会话被删除时卸载文件跟着走——DSH 用 ``cleanupPeriodDays`` 定期清理，
    因为它的卸载根在系统临时目录下，不会跟会话一起删。
    """
    return Path(tempfile.mkdtemp(prefix=prefix)) / hashlib.sha256(
        session_id.encode("utf-8")
    ).hexdigest()[:12]
