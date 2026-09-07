"""C3 沙箱组件测试。

⚠️ 本组件的隔离是启发式的（防呆不防黑客），测试覆盖任务书要求的三类场景：
合法执行 / 静态拒绝 / 超时击杀。
"""

import asyncio

import pytest

from core.sandbox import Sandbox, SandboxStaticScanError, static_scan


def test_sandbox_runs_legal_script():
    """①合法脚本（算 7*6 + random）通过并返回 stdout。"""
    sandbox = Sandbox()
    code = (
        "import random\n"
        "n = 7 * 6\n"
        "r = random.randint(1, 6)\n"
        "print(f'{n}-{r}')\n"
    )
    result = asyncio.run(sandbox.run(code, timeout=10))
    assert result["ok"], result
    assert result["timed_out"] is False
    assert result["stdout"].startswith("42-")
    roll = int(result["stdout"].strip().split("-")[1])
    assert 1 <= roll <= 6


@pytest.mark.parametrize(
    "code",
    [
        "import os\nprint(os.getcwd())",
        "from subprocess import run",
        "import socket",
        "import json, os",  # 白名单与非白名单混合
        "import ctypes",
        "import sys",
    ],
)
def test_sandbox_rejects_forbidden_imports(code):
    """②含黑名单模块 import（含 from-import、混合 import）的脚本被拒。"""
    with pytest.raises(SandboxStaticScanError):
        static_scan(code)
    result = asyncio.run(Sandbox().run(code))
    assert not result["ok"]
    assert result["refused_reason"]


@pytest.mark.parametrize(
    "code",
    [
        "f = open('x.txt')",
        "print(eval('1+1'))",
        "exec('print(1)')",
        "__import__('os')",
        "compile('1', 's', 'eval')",
    ],
)
def test_sandbox_rejects_dangerous_calls(code):
    """open(/eval(/exec(/compile(/__import__ 一律拒绝。"""
    result = asyncio.run(Sandbox().run(code))
    assert not result["ok"]
    assert result["refused_reason"]


def test_sandbox_kills_infinite_loop():
    """③while True 死循环在超时后被杀，timed_out=True，且沙箱仍可复用。"""
    sandbox = Sandbox()
    result = asyncio.run(sandbox.run("while True:\n    pass\n", timeout=3))
    assert result["timed_out"] is True
    assert not result["ok"]
    assert asyncio.run(sandbox.run("print('alive')", timeout=5))["ok"], (
        "超时击杀后沙箱应仍可用"
    )


def test_sandbox_truncates_output():
    """超长 stdout 被截断到上限附近且不会挂死。"""
    sandbox = Sandbox(max_output_bytes=1024)
    code = "print('x' * 100000)"
    result = asyncio.run(sandbox.run(code, timeout=10))
    assert result["ok"]
    assert len(result["stdout"]) <= 1024
    assert result["notes"], "应有截断说明"


def test_sandbox_reports_runtime_error():
    """白名单内代码运行时报错应进 stderr 且 ok=False（非超时）。"""
    result = asyncio.run(Sandbox().run("import json\njson.loads('{bad')", timeout=5))
    assert not result["ok"]
    assert result["timed_out"] is False
    assert "JSON" in result["stderr"] or "Expecting" in result["stderr"]
