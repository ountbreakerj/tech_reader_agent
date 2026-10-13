"""沙箱：把「允许执行」变成「限制了损失面地执行」。

对照源码见正文 hx-15。权限引擎回答「该不该跑」，沙箱回答「跑起来之后它还能
碰到什么」。两者是串行的两道关：过了权限规则，不代表可以在全盘范围内乱跑。

本模块是教学版，只用标准库，做四件事：

1. **工作目录**：命令在一个指定的根目录里跑，相对路径都相对它解析。
2. **路径关**：任何要读写的路径先规范化（展开 `..` 与软链接）再判定是否越界。
3. **环境清理**：子进程只拿到白名单环境变量，凭据类变量一律不传。
4. **资源上限**：超时、输出上限、输出落盘。进程被超时杀掉时结果要说明原因。

真正的操作系统级隔离（容器、seccomp、只读挂载）不在标准库里，本模块用
「路径 + 环境 + 资源」三件事演示沙箱要管的接口。**它挡不住子进程自己打开的文件**：
`allow_write=False` 只作用于 harness 看得见的 argv 路径，一条
`python -c "open('/tmp/x', 'w')"` 仍然能写到任何当前用户可写的地方。
测试 test_readonly_flag_does_not_stop_child_writes 把这个局限钉住了，正文 hx-15 第 6 节专门讲它。
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from .permissions import PathEscape, resolve_within

# 会写文件系统的程序（按程序名精确匹配，不做子串匹配：`format` 里有 rm 但它不是 rm）。
WRITE_PROGRAMS = frozenset({
    "rm", "rmdir", "mv", "cp", "touch", "mkdir", "tee", "ln", "chmod", "chown",
    "truncate", "dd", "install", "rsync", "unzip", "tar", "patch",
})

# 子进程默认能拿到的环境变量。凭据类的一律不在名单里：
# 一旦传进去，模型只要跑一条 `env` 就能把它们读出来并写进上下文。
SAFE_ENV_KEYS = (
    "PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC",
    "TEMP", "TMP", "TMPDIR", "HOME", "USERPROFILE", "LANG", "LC_ALL",
    "PYTHONIOENCODING", "PYTHONPATH", "NUMBER_OF_PROCESSORS", "OS",
)

# 明确要剥离的变量名（子串匹配，大小写不敏感）。命中就删，不做例外。
SECRET_HINTS = (
    "TOKEN", "SECRET", "PASSWORD", "PASSWD", "APIKEY", "API_KEY", "CREDENTIAL",
    "AUTH", "COOKIE", "SESSION_KEY", "PRIVATE_KEY", "ACCESS_KEY", "SSH_",
)


class SandboxViolation(Exception):
    """调用试图越过沙箱边界。"""


@dataclass
class SandboxPolicy:
    """沙箱的策略。所有字段都是「更严的方向」上的默认值。"""

    root: str
    timeout: float = 10.0
    max_output: int = 200_000          # 字符数上限，超出部分落盘（正文 hx-08）
    allow_network: bool = False        # 只影响我们注入的环境变量，不构成真实隔离
    allow_write: bool = False          # 只挡 argv 里看得见的写路径；子进程自己开文件挡不住
    extra_env: dict[str, str] = field(default_factory=dict)
    shell: bool = False                # 默认不用 shell 执行，避免二次解析

    def __post_init__(self) -> None:
        self.root = str(Path(self.root).resolve())


@dataclass
class ExecResult:
    """一次沙箱执行的完整结果。"""

    ok: bool
    argv: list[str]
    returncode: int | None
    stdout: str
    stderr: str
    duration: float
    timed_out: bool = False
    truncated: bool = False
    violation: str = ""
    spill_path: str | None = None

    def as_tool_result(self) -> str:
        """给模型看的文本。失败要带上原因，否则模型只会重试同一条命令。"""
        if self.violation:
            return f"Error: 沙箱拒绝执行：{self.violation}"
        head = " ".join(self.argv)
        if self.timed_out:
            return f"Error: 命令在 {self.duration:.1f}s 后超时被杀：{head}（已终止整个进程）"
        body = self.stdout if self.ok else (self.stdout + self.stderr)
        note = f"（输出超过 {len(body)} 字符，已落盘 {self.spill_path}）" if self.truncated else ""
        return f"$ {head}\n{body}{note}"


class Sandbox:
    """一个目录 + 一份策略 = 一个能跑命令的受限环境。"""

    def __init__(self, policy: SandboxPolicy) -> None:
        self.policy = policy
        self.root = Path(policy.root)
        self.spill_dir = self.root / ".harness" / "spill"
        if not self.root.exists():
            raise SandboxViolation(f"沙箱根目录不存在：{self.root}")

    # ---- 路径 ----

    def resolve(self, candidate: str, *, write: bool = False) -> Path:
        """把候选路径解析到沙箱内。写操作在只读沙箱里直接拒绝。

        越界统一转成 SandboxViolation：调用方只需要认识沙箱这一种异常，
        permissions 里的 PathEscape 是它的实现细节。
        """
        if write and not self.policy.allow_write:
            raise SandboxViolation(f"沙箱是只读的，不能写 {candidate!r}")
        try:
            return resolve_within(self.root, candidate)
        except PathEscape as exc:
            raise SandboxViolation(str(exc)) from exc

    def check_argv_paths(self, argv: Sequence[str], *, write: bool = False) -> None:
        """检查 argv 里看起来像路径的参数。

        教学版只挑「含分隔符或点是文件」的裸参数；真实实现会给每个工具声明
        哪些参数是路径，而不是靠猜。这里保守：拿不准就放过，由权限层兜底。
        """
        for arg in argv[1:]:
            if arg.startswith("-"):
                continue
            if any(sep in arg for sep in ("/", "\\", "..")) or Path(arg).suffix:
                self.resolve(arg, write=write)

    # ---- 环境 ----

    def build_env(self, base: Mapping[str, str] | None = None) -> dict[str, str]:
        """构造子进程环境：白名单 + 剥离敏感变量 + 注入额外变量。"""
        source = dict(base if base is not None else os.environ)
        env: dict[str, str] = {}
        for key in SAFE_ENV_KEYS:
            if key in source:
                env[key] = source[key]
        for key, value in self.policy.extra_env.items():
            if any(hint in key.upper() for hint in SECRET_HINTS):
                # 策略里想注入凭据也不行：这条路径只用于无害的开关。
                continue
            env[key] = value
        if not self.policy.allow_network:
            # 不构成真实隔离（进程仍能开 socket），只是把常见的代理变量清掉，
            # 让「不能用网」这件事在行为上可见。正文会说明这一点。
            for key in list(env):
                if "proxy" in key.lower():
                    env.pop(key, None)
        return env

    # ---- 执行 ----

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: str | None = None,
        timeout: float | None = None,
        stdin: str | None = None,
    ) -> ExecResult:
        """在沙箱里跑一条命令。不抛异常，所有失败都变成结果。"""
        started = time.monotonic()
        args = list(argv)
        if not args:
            return ExecResult(False, args, None, "", "空命令", 0.0, violation="空命令")

        workdir = self.resolve(cwd) if cwd else self.root
        try:
            write_hint = Path(args[0]).name.lower().removesuffix(".exe") in WRITE_PROGRAMS
            self.check_argv_paths(args, write=write_hint)
        except SandboxViolation as exc:
            return ExecResult(False, args, None, "", "", time.monotonic() - started, violation=str(exc))

        budget = timeout if timeout is not None else self.policy.timeout
        env = self.build_env()
        try:
            proc = subprocess.run(
                args,
                cwd=str(workdir),
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=budget,
                shell=self.policy.shell,
                stdin=subprocess.DEVNULL if stdin is None else subprocess.PIPE,
                input=stdin,
                creationflags=_no_window_flags(),
            )
        except subprocess.TimeoutExpired as exc:
            stdout = _as_text(exc.stdout)
            stderr = _as_text(exc.stderr)
            return ExecResult(
                False, args, None, stdout, stderr, time.monotonic() - started,
                timed_out=True, truncated=False,
            )
        except FileNotFoundError:
            return ExecResult(
                False, args, None, "", f"命令不存在：{args[0]}", time.monotonic() - started,
            )
        except OSError as exc:
            return ExecResult(False, args, None, "", f"{type(exc).__name__}: {exc}", time.monotonic() - started)

        duration = time.monotonic() - started
        stdout, truncated_out, spill_out = self._cap(proc.stdout, "stdout")
        stderr, truncated_err, spill_err = self._cap(proc.stderr, "stderr")
        return ExecResult(
            ok=proc.returncode == 0,
            argv=args,
            returncode=proc.returncode,
            stdout=stdout,
            stderr=stderr,
            duration=duration,
            truncated=truncated_out or truncated_err,
            spill_path=spill_out or spill_err,
        )

    def _cap(self, text: str, stream: str) -> tuple[str, bool, str | None]:
        """超长输出：截前 N 个字符，全文落盘，返回路径。"""
        if len(text) <= self.policy.max_output:
            return text, False, None
        self.spill_dir.mkdir(parents=True, exist_ok=True)
        path = self.spill_dir / f"{stream}-{int(time.time() * 1000)}.txt"
        path.write_text(text, encoding="utf-8")
        head = text[: self.policy.max_output]
        return head, True, str(path.relative_to(self.root))


def _as_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def _no_window_flags() -> int:
    """Windows 上不要弹出控制台窗口；其他平台返回 0。"""
    if sys.platform == "win32":
        return getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return 0


def readonly_policy(root: str, **kwargs: object) -> SandboxPolicy:
    """一个只读沙箱：最常用的默认。"""
    return SandboxPolicy(root=root, allow_write=False, **kwargs)  # type: ignore[arg-type]


def writable_policy(root: str, **kwargs: object) -> SandboxPolicy:
    """可写沙箱：写操作仍然要在权限层单独过一遍规则。"""
    return SandboxPolicy(root=root, allow_write=True, **kwargs)  # type: ignore[arg-type]


def describe_limits(policy: SandboxPolicy) -> list[str]:
    """把策略翻译成人话，方便写进工具说明或日志。"""
    return [
        f"工作目录：{policy.root}",
        f"超时：{policy.timeout:.0f} 秒",
        f"输出上限：{policy.max_output} 字符，超出落盘",
        f"写文件：{'允许' if policy.allow_write else '拦截参数里的写路径（子进程自己开文件挡不住）'}",
        f"网络：{'允许' if policy.allow_network else '不提供（非强制隔离）'}",
        f"执行方式：{'shell' if policy.shell else '直接 exec，不经过 shell'}",
    ]
