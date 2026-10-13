"""Shell 命令解析：把一条命令拆成「谁在跑、跑什么、会不会写、能不能预测」。

对照正文 hx-14。权限引擎看到的是 `run_bash({"command": "..."})` 一个字符串，
而真正要判定的是这条字符串背后会发生什么。这一层做三件事：

1. **拆**：按 shell 的语法切开 `;`、`&&`、`||`、`|`、换行、`&`，
   不把被引号包住、被反斜杠转义的符号当分隔符。
2. **分类**：每个片段的第一段是程序名，先看它在不在只读白名单里，
   再看参数有没有把只读程序变成写操作（`sed -i`、`find -delete`、`git checkout`）。
3. **标记**：重定向写文件、命令替换、变量展开、解释器内联代码。
   这些要在去掉引号内容之后再找，否则 `echo "a > b"` 会被当成重定向。

结论只有三种：readonly（可以自动放行）、needs_review（必须问人或拒绝）、
opaque（无法判定，按最坏情况处理）。**任何不确定都往更严的方向倒**。

只用标准库。这不是一个 shell 实现，故意保持「宁可看不懂，也不猜错」。
"""
from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field

# 只读命令白名单：这些命令按定义只读文件、只往标准输出写。
# 刻意没有：env（带参数时会执行后面的命令）、xargs、tee、cp、mv、install、
# xxd（xxd a b 会写 b）、tree（tree -o 会写文件）。git、find、sed、awk、sort、uniq、date
# 在名单里，但它们的参数可能让命令变成写操作，由 _program_risk() 单独判断。
READ_ONLY_COMMANDS = frozenset({
    "cat", "head", "tail", "wc", "ls", "find", "grep", "rg", "awk", "sed", "cut", "sort",
    "uniq", "diff", "file", "stat", "du", "df", "echo", "printf", "pwd", "which",
    "whoami", "date", "basename", "dirname", "realpath", "jq", "md5sum",
    "sha256sum", "od", "nl", "tr", "type", "git",
})

# git 的只读子命令。branch 要再看参数：`git branch` 列分支，`git branch -D x` 删分支。
READ_ONLY_GIT = frozenset({"status", "log", "diff", "show", "branch", "rev-parse", "describe", "blame", "ls-files"})

# 这些程序带 -c / -e 时，命令本身就变成了一段代码。
SHELL_INTERPRETERS = frozenset({"sh", "bash", "zsh", "dash", "powershell", "pwsh", "cmd", "eval"})
CODE_RUNNERS = frozenset({"python", "python3", "py", "node", "perl", "ruby", "php", "deno", "bun"})

# 在去掉引号内容之后的文本上找。`2>&1`、`>/dev/null` 不算写文件。
REDIRECT = re.compile(r"(?:\d|&)?(?:>>|>\||>)\s*(&?[^\s;|&<>]*)")
HARMLESS_TARGETS = frozenset({"/dev/null", "&1", "&2", "NUL", "nul"})
# 只去掉单引号里的内容之后再找：双引号里的 $(...) 和 $VAR 仍然会被展开。
SUBSTITUTION = re.compile(r"\$\(|`|\$\{|\$[A-Za-z_]|[<>]\(")
GLOB = re.compile(r"[*?]")

SPLITTERS = ("&&", "||", ";", "|", "\n", "&")


@dataclass
class Segment:
    """命令里的一个片段（被分隔符切开的最小单位）。"""

    text: str
    argv: list[str] = field(default_factory=list)
    separator_before: str = ""      # 它前面是哪种连接符
    flags: list[str] = field(default_factory=list)   # write-redirect / substitution / glob

    @property
    def program(self) -> str:
        return self.argv[0] if self.argv else ""


@dataclass
class CommandAnalysis:
    """一条命令的整体判定。"""

    command: str
    segments: list[Segment] = field(default_factory=list)
    verdict: str = "needs_review"    # readonly / needs_review / opaque
    reasons: list[str] = field(default_factory=list)

    @property
    def programs(self) -> list[str]:
        return [s.program for s in self.segments if s.program]


def split_segments(command: str) -> list[tuple[str, str]]:
    """按 shell 分隔符切分，返回 [(连接符, 片段)]。引号与转义内部不切。

    这是本模块最容易出错的地方：`echo "a; b"` 是一段，`echo a; b` 是两段；
    `2>&1` 和 `&>` 里的 & 是重定向的一部分，不是「放到后台」。
    """
    pieces: list[tuple[str, str]] = []
    buffer: list[str] = []
    separator = ""
    quote = ""
    escaped = False
    index = 0
    while index < len(command):
        char = command[index]
        if escaped:
            buffer.append(char)
            escaped = False
            index += 1
            continue
        if char == "\\" and quote != "'":
            buffer.append(char)
            escaped = True
            index += 1
            continue
        if quote:
            buffer.append(char)
            if char == quote:
                quote = ""
            index += 1
            continue
        if char in ('"', "'"):
            quote = char
            buffer.append(char)
            index += 1
            continue
        matched = next((sep for sep in SPLITTERS if command.startswith(sep, index)), "")
        if matched == "&" and _is_redirect_ampersand(command, index):
            matched = ""
        if matched:
            pieces.append((separator, "".join(buffer).strip()))
            separator = matched
            buffer = []
            index += len(matched)
            continue
        buffer.append(char)
        index += 1
    pieces.append((separator, "".join(buffer).strip()))
    return [(sep, text) for sep, text in pieces if text]


def _is_redirect_ampersand(command: str, index: int) -> bool:
    """`2>&1`、`>&2`、`&>file` 里的 & 属于重定向。"""
    before = command[index - 1] if index > 0 else ""
    after = command[index + 1] if index + 1 < len(command) else ""
    return before in "<>" or after == ">"


def strip_quoted(text: str, *, double: bool = True) -> str:
    """把引号里的内容和被转义的字符换成空格，保留引号本身与位置。

    double=True：单双引号里的内容都去掉（找重定向、通配符时用：引号里的 > 和 * 都是字面量）。
    double=False：只去掉单引号里的（找替换时用：双引号里的 $(...) 仍然会被展开）。
    """
    out: list[str] = []
    quote = ""
    escaped = False
    for char in text:
        if escaped:
            out.append(" ")
            escaped = False
        elif char == "\\" and quote != "'":
            out.append(" ")
            escaped = True
        elif quote:
            if char == quote:
                quote = ""
                out.append(char)
            else:
                out.append(" " if (quote == "'" or double) else char)
        elif char in ("'", '"'):
            quote = char
            out.append(char)
        else:
            out.append(char)
    return "".join(out)


def redirect_targets(text: str) -> list[str]:
    """片段里会写入的重定向目标。`>/dev/null`、`2>&1` 不算；目标被引号包住时记作 '?'。"""
    bare = strip_quoted(text, double=True)
    targets: list[str] = []
    for match in REDIRECT.finditer(bare):
        target = match.group(1)
        if target not in HARMLESS_TARGETS:
            targets.append(target or "?")
    return targets


def _tokenize(text: str) -> list[str]:
    """把片段切成 argv。切不出来（引号没闭合等）就返回空列表，交给上层判为 opaque。"""
    try:
        return shlex.split(text, posix=True)
    except ValueError:
        return []


def _program_risk(argv: list[str]) -> tuple[str, str] | None:
    """白名单程序里，参数会改变性质的那几个。返回 (结论, 理由)；None 表示参数没问题。

    sed 和 awk 本身是完整的语言，这里只覆盖最常见的写法；拿不准的交给沙箱兜底（hx-15）。
    """
    program, args = argv[0], argv[1:]
    positional = [arg for arg in args if not arg.startswith("-")]
    if program == "find":
        if any(arg in ("-exec", "-execdir", "-ok", "-okdir") for arg in args):
            return "opaque", "find -exec 会对每个结果执行任意命令"
        if any(arg in ("-delete", "-fprint", "-fprintf", "-fls") for arg in args):
            return "needs_review", "find 带 -delete / -fprint，会删除或写文件"
    elif program == "sed":
        if any(arg.startswith("-i") or arg.startswith("--in-place") for arg in args):
            return "needs_review", "sed -i 会原地修改文件"
        script = positional[0] if positional else ""
        if re.search(r"(^|[;}/])\s*[we]\b", script):
            return "needs_review", "sed 脚本里有 w（写文件）或 e（执行命令）"
    elif program == "awk":
        script = positional[0] if positional else ""
        if "system(" in script or "|" in script:
            return "opaque", "awk 脚本里调用了 system() 或管道，会执行别的命令"
        if ">" in script:
            return "needs_review", "awk 脚本里有重定向，会写文件"
    elif program == "sort":
        if any(arg in ("-o", "--output") or arg.startswith("--output=") for arg in args):
            return "needs_review", "sort -o 会写文件"
    elif program == "uniq":
        if len(positional) >= 2:
            return "needs_review", "uniq 的第二个参数是输出文件"
    elif program == "date":
        if any(arg in ("-s", "--set") or arg.startswith("--set=") for arg in args):
            return "needs_review", "date -s 会修改系统时间"
    elif program == "git":
        sub = positional[0] if positional else ""
        if sub not in READ_ONLY_GIT:
            return "needs_review", f"git {sub or '（无子命令）'} 不是只读子命令"
        if sub == "branch":
            rest = args[args.index("branch") + 1:]
            if any(arg in ("-d", "-D", "-m", "-M", "-c", "-C", "--delete", "--move", "--copy") for arg in rest) \
                    or any(not arg.startswith("-") for arg in rest):
                return "needs_review", "git branch 带参数会创建、删除或重命名分支"
    return None


def analyze(command: str) -> CommandAnalysis:
    """解析一条 shell 命令，给出判定与理由。"""
    analysis = CommandAnalysis(command=command)
    if not command.strip():
        analysis.verdict = "opaque"
        analysis.reasons.append("空命令")
        return analysis

    for separator, text in split_segments(command):
        segment = Segment(text=text, separator_before=separator, argv=_tokenize(text))
        if redirect_targets(text):
            segment.flags.append("write-redirect")
        if SUBSTITUTION.search(strip_quoted(text, double=False)):
            segment.flags.append("substitution")
        if GLOB.search(strip_quoted(text, double=True)):
            segment.flags.append("glob")
        analysis.segments.append(segment)

    reasons: list[str] = []
    verdict = "readonly"
    for segment in analysis.segments:
        program = segment.program
        if not program:
            verdict = _worse(verdict, "opaque")
            reasons.append(f"无法解析片段 {segment.text[:40]!r}（引号未闭合或语法不完整）")
            continue
        if "substitution" in segment.flags:
            verdict = _worse(verdict, "opaque")
            reasons.append(f"{program}: 含命令替换或变量展开，实际执行的命令在解析阶段无法确定")
        if "write-redirect" in segment.flags:
            verdict = _worse(verdict, "needs_review")
            targets = ", ".join(redirect_targets(segment.text))
            reasons.append(f"{program}: 重定向会写入 {targets}")
        if program in SHELL_INTERPRETERS or (program in CODE_RUNNERS and _runs_inline_code(segment.argv)):
            verdict = _worse(verdict, "opaque")
            reasons.append(f"{program}: 直接把代码当参数执行，内容无法静态判定")
        elif program not in READ_ONLY_COMMANDS:
            verdict = _worse(verdict, "needs_review")
            reasons.append(f"{program}: 不在只读白名单里")
        else:
            risk = _program_risk(segment.argv)
            if risk:
                verdict = _worse(verdict, risk[0])
                reasons.append(f"{program}: {risk[1]}")

    analysis.reasons = reasons or ["全部片段都是白名单里的只读命令，且没有写文件的重定向"]
    analysis.verdict = verdict
    return analysis


def _worse(current: str, candidate: str) -> str:
    order = {"readonly": 0, "needs_review": 1, "opaque": 2}
    return candidate if order[candidate] > order[current] else current


def _runs_inline_code(argv: list[str]) -> bool:
    return any(arg in ("-c", "-e", "--eval", "-") for arg in argv[1:])


def to_hook_decision(analysis: CommandAnalysis, *, allow_opaque: bool = False) -> tuple[str, str]:
    """把分析结果映射成权限引擎的判定（allow / ask / deny）与理由。"""
    if analysis.verdict == "readonly":
        return "allow", "命令的每一段都是只读的，且没有写文件的重定向"
    if analysis.verdict == "needs_review":
        return "ask", "命令里有会写入的片段：" + "；".join(analysis.reasons)
    return ("ask" if allow_opaque else "deny"), "无法判定这条命令会做什么：" + "；".join(analysis.reasons) \
        + "。请把要执行的命令直接写出来，不要用替换、变量或内联代码"
