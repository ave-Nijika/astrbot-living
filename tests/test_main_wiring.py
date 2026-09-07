"""需求 A/F 测试：记忆后端懒加载（重试+缓存）与 main.py 接线幂等。

注意：`import main` 会撞上 AstrBot 本体根目录的 main.py（conftest 把
AstrBot 根放进了 sys.path），所以本文件用合成包上下文加载插件 main.py
（与 scripts/smoke_import_plugin.py 同法）。
"""

import asyncio
import importlib
import sys
import types
from pathlib import Path

import pytest

from core.lazy_memory import LazyMemory
from core.memory_backend import LivingMemoryBackend, SimpleBackend

WORKDIR = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# 需求 A：LazyMemory
# ---------------------------------------------------------------------------
class FakeLivingEngine:
    """满足 LivingMemoryBackend.probe 全部 hasattr 探测的假引擎。"""

    def __init__(self):
        self.added = []

    async def add_memory(self, content, session_id=None, importance=0.5,
                         metadata=None, **kwargs):
        self.added.append(content)
        return len(self.added)

    async def search_memories(self, query, k=5, session_id=None, **kwargs):
        return []


class MutableRegistryContext:
    """get_registered_star 可变：模拟 LivingMemory 晚于本插件加载就绪。"""

    def __init__(self):
        self.meta = None

    def get_registered_star(self, name):
        return self.meta


def living_meta():
    return types.SimpleNamespace(
        activated=True,
        star_cls=types.SimpleNamespace(
            initializer=types.SimpleNamespace(memory_engine=FakeLivingEngine())
        ),
    )


def make_lazy(ctx, mode="auto", db_path=None):
    return LazyMemory(
        context=ctx,
        mode_getter=lambda: mode,
        db_path_getter=lambda: db_path or "/tmp/nonexistent_dir_xyz/mem.db",
    )


def test_lazy_falls_back_then_retries_then_caches(tmp_path):
    """首探失败降级 Simple；LivingMemory 就绪后重试成功并永久缓存。"""
    ctx = MutableRegistryContext()
    lazy = make_lazy(ctx, db_path=str(tmp_path / "mem.db"))

    # 第一次：注册表里还没有 LivingMemory → SimpleBackend
    first = asyncio.run(lazy.get())
    assert isinstance(first, SimpleBackend)

    # 第二次：LivingMemory 已加载 → 重试成功
    ctx.meta = living_meta()
    second = asyncio.run(lazy.get())
    assert isinstance(second, LivingMemoryBackend)

    # 第三次：命中缓存（同一实例，不再探测）
    third = asyncio.run(lazy.get())
    assert third is second


def test_lazy_forced_mode_failure_falls_back_gracefully(tmp_path):
    """强制 livingmemory 模式且不可用：不抛异常，降级 Simple 可用。"""
    lazy = make_lazy(MutableRegistryContext(), mode="livingmemory",
                     db_path=str(tmp_path / "mem.db"))
    backend = asyncio.run(lazy.get())
    assert isinstance(backend, SimpleBackend)
    doc_id = asyncio.run(backend.add("测试", 0.5))
    assert doc_id >= 1
    asyncio.run(lazy.close())


def test_lazy_probes_only_until_success(tmp_path, caplog):
    """成功后缓存：注册表查询次数不再增长。"""
    ctx = MutableRegistryContext()
    ctx.meta = living_meta()
    lazy = make_lazy(ctx, db_path=str(tmp_path / "mem.db"))
    calls = {"n": 0}
    orig = ctx.get_registered_star

    def counting(name):
        calls["n"] += 1
        return orig(name)

    ctx.get_registered_star = counting
    asyncio.run(lazy.get())
    asyncio.run(lazy.get())
    asyncio.run(lazy.get())
    assert calls["n"] == 1


# ---------------------------------------------------------------------------
# 需求 F：initialize/terminate 接线幂等（经合成包加载真实 main.py）
# ---------------------------------------------------------------------------
def load_plugin_main():
    pkg_name = "living_plugin_under_test"
    if pkg_name not in sys.modules:
        pkg = types.ModuleType(pkg_name)
        pkg.__path__ = [str(WORKDIR)]
        sys.modules[pkg_name] = pkg
        # 把顶层 core 包及其子模块别名进合成包：否则 main.py 会加载出第二份
        # core 模块树，跨模块树的 isinstance/单例语义全部失效
        import core as core_pkg

        sys.modules[f"{pkg_name}.core"] = core_pkg
        for name, mod in list(sys.modules.items()):
            if name == "core" or name.startswith("core."):
                sys.modules.setdefault(f"{pkg_name}.{name}", mod)
    module = importlib.import_module(f"{pkg_name}.main")
    return module


def make_plugin(db_path):
    """绕过 Star.__init__（需要完整 AstrBot 运行时），手工装配方。"""
    main_module = load_plugin_main()
    plugin = object.__new__(main_module.LivingPlugin)
    plugin.context = MutableRegistryContext()
    plugin.config = {"memory": {"backend": "auto"}}
    plugin._lazy_memory = main_module.LazyMemory(
        context=plugin.context,
        mode_getter=lambda: "auto",
        db_path_getter=lambda: str(db_path),
    )
    plugin.memory_note = "尚未初始化"
    plugin.gate = None
    plugin.loop = None
    # 屏蔽真实插件数据目录（不写 AstrBot 的 data/）与网络能力
    plugin._gate_db_path = lambda: str(db_path) + ".gate"
    plugin._memory_db_path = lambda: str(db_path)
    plugin.searcher = types.SimpleNamespace(close=lambda: asyncio.sleep(0))
    plugin.fetcher = types.SimpleNamespace(close=lambda: asyncio.sleep(0))
    plugin.sandbox = types.SimpleNamespace()  # initialize 接线时仅引用不调用
    plugin.sender = types.SimpleNamespace()
    return plugin


def test_initialize_terminate_idempotent(tmp_path):
    """重复 initialize 安全（旧 loop 先停）；terminate 后可再次 initialize。"""

    async def flow():
        plugin = make_plugin(tmp_path / "m.db")
        await plugin.initialize()
        loop1 = plugin.loop
        assert loop1 is not None and loop1.running

        await plugin.initialize()  # 热重载/重复初始化
        assert plugin.loop is not loop1
        assert plugin.loop.running

        await plugin.terminate()
        assert plugin.loop is None and plugin.gate is None

        await plugin.initialize()  # AstrBot 重载场景
        assert plugin.loop is not None
        await plugin.terminate()

    asyncio.run(flow())


def test_plugin_uses_lazy_memory_getter(tmp_path):
    """插件._get_memory 是懒的：调用前不探测，调用后 memory_note 被填充。"""

    async def flow():
        plugin = make_plugin(tmp_path / "m.db")
        assert plugin.memory_note == "尚未初始化"
        backend = await plugin._get_memory()
        assert isinstance(backend, SimpleBackend)  # 注册表为空 → 降级
        assert plugin.memory_note != "尚未初始化"
        await plugin.terminate()

    asyncio.run(flow())
