"""凛热修（2026-09-15）回归测试：自愈任务等待 LivingMemory 就绪。

根因：AstrBot 按目录序加载插件，living 先于 livingmemory——自愈触发时
lazy_memory 探测失败降级 Simple，原一次性执行被跳过且不再重试。
修复：改为轮询等待（30s × 20 次），探测到 LivingMemory 后端才开始自愈。
"""

from __future__ import annotations

import asyncio
import types

from test_m3_patch5 import _load_main

# 先触发虚拟包装配，再从虚拟包导入真实类——保证 isinstance 检查与
# 生产代码用的是同一个类对象（本文件最初的失败根因就是两个"同名类"
# 分属不同模块对象，isinstance 恒 False，20 次探测全判未就绪）
main = _load_main()
from living_plugin_under_test.core.memory_backend import (  # noqa: E402
    LivingMemoryBackend as _RealLivingMemoryBackend,
)


class _FakeLivingBackend(_RealLivingMemoryBackend):
    """测试替身：继承真实 LivingMemoryBackend（而非鸭子同名）——
    `_run_identity_selfheal_with_retry` 里的 isinstance 就绪判断
    因此被本测试真实覆盖。

    为什么不设 engine 属性：真实类把它定义为只读 property（返回
    self._engine），这里直接填 _engine 即可。
    """

    def __init__(self):
        self._engine = types.SimpleNamespace()

    async def add(self, content, importance=0.5, metadata=None,
                  session_id=None, persona_id=None):
        return 1

    async def search(self, query, k=5, session_id=None):
        return []

    async def close(self):
        pass


class _FakeSimple:
    """降级后端替身（引擎未就绪时 lazy_memory 返回它）。"""


class _FakePlugin:
    """最小插件替身：_get_memory 依次弹出预设后端。"""

    def __init__(self, backends):
        import itertools

        pool = list(backends)
        self._backends = pool + [pool[-1]] * 50  # 末位复用，永不耗尽

    async def _get_memory(self):
        if len(self._backends) > 1:
            return self._backends.pop(0)
        return self._backends[0]

    async def _bot_identity(self):
        return {
            "identity_key": "cron:astrbot",
            "sender_id": "astrbot",
            "platform": "cron",
            "display_name": "astrbot",
            "aliases": ["astrbot"],
            "is_bot": True,
        }

    def _identity_is_polluted(self, key):
        return str(key or "").startswith("default:")

    def _selfheal_state_path(self):
        import tempfile, os
        return os.path.join(tempfile.mkdtemp(), "selfheal_state.json")


def test_selfheal_waits_for_livingmemory_and_runs(monkeypatch, tmp_path):
    """前两次探测返回 Simple（引擎未就绪），第三次返回 LivingMemory →
    自愈应执行而不是像旧版那样直接跳过。"""
    main = _load_main()
    plugin = _FakePlugin([_FakeSimple(), _FakeSimple(), _FakeLivingBackend()])

    sleeps: list[float] = []
    scanned: list[int] = []

    async def _fake_sleep(sec):
        sleeps.append(sec)

    async def _fake_selfheal(backend, corrected, state_path, engine=None):
        scanned.append(1)
        return {"found": 1, "fixed": 1, "paths": ["update"]}

    async def _scenario():
        monkeypatch.setattr(main.asyncio, "sleep", _fake_sleep)
        monkeypatch.setattr(main, "run_identity_selfheal", _fake_selfheal)
        await main.LivingPlugin._run_identity_selfheal_with_retry(plugin)

    asyncio.run(_scenario())

    assert len(sleeps) == 2  # 前两次未就绪各等 30s
    assert scanned == [1]  # 就绪后自愈确实执行


def test_selfheal_gives_up_after_max_attempts(monkeypatch):
    """始终拿不到 LivingMemory 后端 → 超时放弃，不执行自愈。"""
    main = _load_main()
    plugin = _FakePlugin([_FakeSimple()])

    async def _fake_sleep(sec):
        pass

    called = {"selfheal": 0}

    async def _fake_selfheal(*a, **kw):
        called["selfheal"] += 1
        return {"found": 0, "fixed": 0, "paths": []}

    async def _scenario():
        monkeypatch.setattr(main.asyncio, "sleep", _fake_sleep)
        monkeypatch.setattr(main, "run_identity_selfheal", _fake_selfheal)
        await main.LivingPlugin._run_identity_selfheal_with_retry(plugin)

    asyncio.run(_scenario())

    assert called["selfheal"] == 0  # 从未执行自愈
