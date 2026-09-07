"""C3 沙箱代码执行能力：子进程 + 超时 + 输出截断 + import 静态白名单。

⚠️ 隔离级别如实声明（M0，Windows 本机无 docker）：
  本组件的隔离是**启发式的，防呆不防黑客**。它只能防止 LLM 生成的代码
  "无意中"干坏事（误删文件、误联网、死循环卡死主进程），挡不住故意构造
  的逃逸（如通过内建函数间接访问被禁模块、C 扩展、fork 炸弹等）。
  已知局限：
    - 静态扫描基于正则，可能被字符串拼接、动态 import 等手法绕过；
    - 白名单外模块一律拒绝，但标准库白名单本身也可能被滥用（如 re 正则
      灾难性回溯——有超时兜底）；
    - 未做内存限制（Windows Job Object 强化与内存上限在 M4）；
    - 子进程以 AstrBot 同等用户权限运行，无权限隔离。
  M1 后 zcode 安全审核将以此组件为第一重点。
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import sys
import tempfile

# 允许 import 的标准库白名单（任务书 R2-C3：random/math/time/datetime/json/re + 纯逻辑）
IMPORT_WHITELIST = frozenset(
    {"random", "math", "time", "datetime", "json", "re", "itertools", "collections"}
)

# 无论以何种形式出现都直接拒绝的调用（正则按词边界匹配）
_DENY_CALLS = (
    r"open\s*\(",
    r"__import__\s*\(",
    r"\beval\s*\(",
    r"\bexec\s*\(",
    r"\bcompile\s*\(",
)

class SandboxStaticScanError(Exception):
    """源码未通过静态扫描。"""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


_IMPORT_RE = re.compile(r"^\s*import\s+([^\n#]+)", re.MULTILINE)
_FROM_IMPORT_RE = re.compile(
    r"^\s*from\s+([A-Za-z_][\w.]*)\s+import\b", re.MULTILINE
)


def _root_module(name: str) -> str:
    """'x.y' -> 'x'；处理 'import a.b as c' 的 as 别名。"""
    return name.strip().split(" as ")[0].strip().split(".")[0]


def static_scan(code: str) -> None:
    """执行前静态扫描，不通过则抛 SandboxStaticScanError。

    规则（启发式，可被绕过，见模块 docstring 的局限声明）：
      1. import / from import 的顶层模块必须在白名单内
        （含 'import a, b' 逗号分隔与 'import a.b' 子模块形式）；
      2. 出现 open(/eval(/exec(/compile(/__import__ 即拒绝。
    """
    for m in _IMPORT_RE.finditer(code):
        for part in m.group(1).split(","):
            root = _root_module(part)
            if root and root not in IMPORT_WHITELIST:
                raise SandboxStaticScanError(
                    f"禁止 import 模块: {part.strip()!r}"
                    f"（白名单: {sorted(IMPORT_WHITELIST)}）"
                )
    for m in _FROM_IMPORT_RE.finditer(code):
        root = m.group(1).split(".")[0]
        if root not in IMPORT_WHITELIST:
            raise SandboxStaticScanError(
                f"禁止 from-import 模块: {m.group(1)!r}"
                f"（白名单: {sorted(IMPORT_WHITELIST)}）"
            )
    for pattern in _DENY_CALLS:
        if re.search(pattern, code):
            raise SandboxStaticScanError(f"源码包含被禁止的调用: {pattern}")


class Sandbox:
    """受限代码执行器。run() 返回 {ok, stdout, stderr, timed_out, ...}。"""

    def __init__(
        self,
        python_exe: str | None = None,
        timeout: float = 10.0,
        max_output_bytes: int = 8192,
    ) -> None:
        # 默认用当前解释器：插件运行于 AstrBot venv 时即为 venv python
        self._python_exe = python_exe or sys.executable
        self._timeout = timeout
        self._max_output = max_output_bytes

    async def run(self, code: str, timeout: float | None = None) -> dict:
        """在受限子进程中执行 code。

        Returns:
            {"ok": bool, "stdout": str, "stderr": str, "timed_out": bool,
             "exit_code": int | None, "refused_reason": str | None}
            静态扫描拒绝时 stdout/stderr 为空，refused_reason 说明原因。
        """
        try:
            static_scan(code)
        except SandboxStaticScanError as e:
            return {
                "ok": False,
                "stdout": "",
                "stderr": "",
                "timed_out": False,
                "exit_code": None,
                "refused_reason": e.reason,
            }

        limit = min(max(timeout if timeout is not None else self._timeout, 1.0), 120.0)
        workdir = tempfile.mkdtemp(prefix="living_sandbox_")
        script = os.path.join(workdir, "main.py")
        with open(script, "w", encoding="utf-8") as f:
            f.write(code)

        try:
            return await self._execute(script, workdir, limit)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    async def _execute(self, script: str, workdir: str, limit: float) -> dict:
        creationflags = 0
        if sys.platform == "win32":
            creationflags = getattr(asyncio.subprocess, "CREATE_NO_WINDOW", 0)

        proc = await asyncio.create_subprocess_exec(
            self._python_exe,
            "-I",  # isolated：忽略用户 site 与 PYTHON* 环境变量
            "-B",  # 不写 .pyc
            script,
            cwd=workdir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL,
            creationflags=creationflags,
        )

        out_task = asyncio.create_task(self._drain(proc.stdout))
        err_task = asyncio.create_task(self._drain(proc.stderr))

        timed_out = False
        try:
            await asyncio.wait_for(proc.wait(), timeout=limit)
        except asyncio.TimeoutError:
            timed_out = True
            await self._kill_tree(proc)

        stdout, stdout_truncated = await out_task
        stderr, stderr_truncated = await err_task

        notes = []
        if stdout_truncated:
            notes.append("stdout 超长已截断")
        if stderr_truncated:
            notes.append("stderr 超长已截断")

        return {
            "ok": (not timed_out) and proc.returncode == 0,
            "stdout": stdout,
            "stderr": stderr,
            "timed_out": timed_out,
            "exit_code": proc.returncode,
            "refused_reason": None,
            "notes": "; ".join(notes),
        }

    async def _drain(self, stream: asyncio.StreamReader | None) -> tuple[str, bool]:
        """持续排空管道（防子进程写满管道卡死），超出上限部分丢弃。"""
        if stream is None:
            return "", False
        data = bytearray()
        truncated = False
        while True:
            chunk = await stream.read(65536)
            if not chunk:
                break
            remain = self._max_output - len(data)
            if remain > 0:
                data.extend(chunk[:remain])
            if len(chunk) > remain:
                truncated = True
        return bytes(data).decode("utf-8", errors="replace"), truncated

    @staticmethod
    async def _kill_tree(proc: asyncio.subprocess.Process) -> None:
        """尽力终止进程树。Windows 用 taskkill /T；失败则退回 proc.kill()。

        注：proc.kill() 只杀直接子进程，这是无 docker 环境下的尽力而为。
        """
        if sys.platform == "win32":
            try:
                killer = await asyncio.create_subprocess_exec(
                    "taskkill",
                    "/F",
                    "/T",
                    "/PID",
                    str(proc.pid),
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await asyncio.wait_for(killer.wait(), timeout=5)
                return
            except Exception:
                pass
        try:
            proc.kill()
        except ProcessLookupError:
            pass
