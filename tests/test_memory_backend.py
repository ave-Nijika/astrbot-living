"""C4 记忆后端测试。

覆盖三层：
  1. SimpleBackend 全流程（真实 SQLite，临时目录）
  2. LivingMemoryBackend 探测逻辑（假对象单测）+ 真实环境探测（可跳过）
  3. 工厂 create_backend 的 auto 降级行为
"""

import asyncio
import types

import pytest

from core.memory_backend import (
    LivingMemoryBackend,
    MemoryBackend,
    SimpleBackend,
    create_backend,
)


# ---------------------------------------------------------------------------
# SimpleBackend 全流程
# ---------------------------------------------------------------------------
def test_simple_backend_full_flow(tmp_path):
    async def flow():
        backend = SimpleBackend(str(tmp_path / "mem.db"))
        try:
            id1 = await backend.add("我学会了用博查搜索 Next.js 16 的新特性", 0.7)
            id2 = await backend.add(
                "主人喜欢让我汇报天气", 0.5, {"topic": "weather"}
            )
            await backend.add("完全无关的记录：今天写了一个贪吃蛇小游戏", 0.3)

            assert id1 != id2

            hits = await backend.search("Next.js 16")
            assert len(hits) >= 1
            assert "Next.js" in hits[0]["content"]
            assert hits[0]["id"] == id1

            # 多关键词 OR 检索
            hits2 = await backend.search("天气 贪吃蛇")
            assert {h["id"] for h in hits2} >= {id2}

            # k 截断
            hits3 = await backend.search("的", k=2)
            assert len(hits3) <= 2
        finally:
            await backend.close()

    asyncio.run(flow())


def test_simple_backend_persists_across_instances(tmp_path):
    async def flow():
        path = str(tmp_path / "mem.db")
        b1 = SimpleBackend(path)
        await b1.add("持久化检查点", 0.9)
        await b1.close()

        b2 = SimpleBackend(path)
        try:
            hits = await b2.search("持久化")
            assert len(hits) == 1
            assert hits[0]["importance"] == pytest.approx(0.9)
        finally:
            await b2.close()

    asyncio.run(flow())


# ---------------------------------------------------------------------------
# LivingMemoryBackend 探测（假对象，不依赖真实插件状态）
# ---------------------------------------------------------------------------
class _FakeEngine:
    def __init__(self):
        self.added = []

    async def add_memory(self, content, session_id=None, importance=0.5,
                         metadata=None, **kwargs):
        self.added.append(content)
        return len(self.added)

    async def search_memories(self, query, k=5, session_id=None, **kwargs):
        return [
            types.SimpleNamespace(
                doc_id=1, final_score=0.9, content=f"关于 {query} 的记忆",
                metadata={},
            )
        ]


def _fake_context(star_meta):
    ctx = types.SimpleNamespace()
    ctx.get_registered_star = lambda name: star_meta
    return ctx


def test_probe_success_with_fake_runtime():
    engine = _FakeEngine()
    meta = types.SimpleNamespace(
        activated=True, star_cls=types.SimpleNamespace(
            initializer=types.SimpleNamespace(memory_engine=engine)
        )
    )
    backend, reason = asyncio.run(LivingMemoryBackend.probe(_fake_context(meta)))
    assert backend is not None and reason == ""

    async def use():
        doc_id = await backend.add("测试记忆", 0.6, {"k": "v"})
        hits = await backend.search("测试")
        return doc_id, hits

    doc_id, hits = asyncio.run(use())
    assert doc_id == 1
    assert hits[0]["content"] == "关于 测试 的记忆"
    assert isinstance(backend, MemoryBackend)


@pytest.mark.parametrize(
    "meta, expect_reason",
    [
        (None, "未安装"),
        (types.SimpleNamespace(activated=False, star_cls=None), "未激活"),
        (types.SimpleNamespace(activated=True, star_cls=None), "star_cls"),
        (
            types.SimpleNamespace(
                activated=True,
                star_cls=types.SimpleNamespace(initializer=None),
            ),
            "initializer",
        ),
        (
            types.SimpleNamespace(
                activated=True,
                star_cls=types.SimpleNamespace(
                    initializer=types.SimpleNamespace(memory_engine=None)
                ),
            ),
            "memory_engine",
        ),
    ],
)
def test_probe_failure_reasons(meta, expect_reason):
    """任何一步探测失败都要给出含关键词的不可用原因。"""
    backend, reason = asyncio.run(LivingMemoryBackend.probe(_fake_context(meta)))
    assert backend is None
    assert expect_reason in reason


def test_probe_rejects_engine_with_wrong_signature():
    class BadEngine:
        pass  # 缺 add_memory / search_memories

    meta = types.SimpleNamespace(
        activated=True, star_cls=types.SimpleNamespace(
            initializer=types.SimpleNamespace(memory_engine=BadEngine())
        )
    )
    backend, reason = asyncio.run(LivingMemoryBackend.probe(_fake_context(meta)))
    assert backend is None
    assert "add_memory" in reason


def test_probe_with_none_context():
    backend, reason = asyncio.run(LivingMemoryBackend.probe(None))
    assert backend is None
    assert reason


# ---------------------------------------------------------------------------
# 真实环境探测（本机已装 LivingMemory，但需要 AstrBot 运行时上下文时跳过）
# ---------------------------------------------------------------------------
def test_probe_against_real_astrbot_context():
    """能构造出带 get_registered_star 的假上下文时尝试真实探测；
    真实 star 注册表只在 AstrBot 进程内存在，脚本环境返回未安装属正常。"""
    from astrbot.core.star.star import star_registry

    meta = next(
        (m for m in star_registry
         if getattr(m, "name", "") == "astrbot_plugin_livingmemory"),
        None,
    )
    if meta is None:
        pytest.skip("非 AstrBot 运行时环境，注册表中无 LivingMemory")
    backend, reason = asyncio.run(LivingMemoryBackend.probe(_fake_context(meta)))
    if meta.activated and getattr(meta.star_cls, "initializer", None):
        assert backend is not None, reason
    else:
        assert backend is None and reason


# ---------------------------------------------------------------------------
# 工厂
# ---------------------------------------------------------------------------
def test_factory_auto_falls_back_to_simple(tmp_path):
    """auto 模式下探测不到 LivingMemory 时应降级 SimpleBackend。"""
    ctx = _fake_context(None)  # 注册表里没有

    async def flow():
        backend, note = await create_backend(
            context=ctx, mode="auto",
            simple_db_path=str(tmp_path / "mem.db"),
        )
        return backend, note

    backend, note = asyncio.run(flow())
    assert isinstance(backend, SimpleBackend)
    assert "降级" in note
    asyncio.run(backend.close())


def test_factory_forced_livingmemory_raises(tmp_path):
    async def flow():
        await create_backend(
            context=_fake_context(None), mode="livingmemory",
            simple_db_path=str(tmp_path / "mem.db"),
        )

    with pytest.raises(RuntimeError, match="不可用"):
        asyncio.run(flow())
