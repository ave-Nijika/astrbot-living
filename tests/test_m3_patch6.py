"""M3 补丁 VI 测试：bot 身份提取防污染 + 历史污染数据自愈。"""

import asyncio
import inspect
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from core.living_state import LivingGate
from core.memory_backend import LivingMemoryBackend
from core.selfheal import (
    build_corrected_identity,
    corrected_metadata,
    run_identity_selfheal,
    scan_polluted_memories,
)

T0 = datetime(2026, 9, 15, 12, 0, 0)

BASE_CONFIG = {
    "decision": {"daily_impulse_limit": 3, "activity_probability": 1.0,
                 "impulse_check_interval_minutes": 5, "max_run_seconds": 300},
    "capabilities": {"cooldown_between_activities_hours": 0.0},
    "sleep": {"sleep_window": "", "fatigue_rate_per_hour": 4.0,
              "dream_probability": 0.0},
    "output_gate": {"daily_message_limit": 10, "message_min_interval_minutes": 30,
                    "target_sessions": "", "quiet_hours": ""},
}

POLLUTED_PARTICIPANT = {
    "identity_key": "default:ecb3741964abcdef",
    "sender_id": "ecb3741964abcdef",
    "platform": "default",
    "display_name": "default",
    "is_bot": True,
}
NATIVE_BOT_PARTICIPANT = {
    "identity_key": "aiocqhttp:33334444",
    "sender_id": "33334444",
    "platform": "aiocqhttp",
    "display_name": "astrbot",
    "is_bot": True,
}


def make_gate_with_platforms(platforms=None):
    config = dict(BASE_CONFIG)
    ctx = types_ns(get_config=lambda: {"platform": platforms or []})
    return LivingGate(
        config_getter=lambda: config, db_path=":memory:", rng=lambda: 0.0
    ), ctx


def types_ns(**kw):
    return type("NS", (), kw)()


class FakeContext:
    """可配置 get_config 的最小上下文（平台白名单/dashboard 测试用）。"""

    def __init__(self, cfg=None):
        self._cfg = cfg or {}

    def get_config(self):
        return self._cfg


DEFAULT_TEST_CONTEXT_CFG = {
    "platform": [{"id": "aiocqhttp"}],
    "dashboard": {"username": "astrbot"},
}


class FakePlugin:
    """_bot_identity 的宿主替身（只提供它用到的东西）。"""

    def __init__(self, backend=None, context=None, cached=None):
        from test_m3_patch5 import _load_main

        main_module = _load_main()
        # 为什么不用 types_ns+lambda：类属性函数经实例访问会绑定 self，
        # 零参 lambda 被调用时抛 TypeError，被 _platform_prefixes 的
        # except 吞掉后白名单静默退化为只剩 cron（本文件踩过的坑）
        self.context = context if context is not None else FakeContext(
            DEFAULT_TEST_CONTEXT_CFG
        )
        if backend is not None:
            async def memory_getter():
                return backend
            self._get_memory = memory_getter
        if cached is not None:
            self._bot_identity_cache = cached

    # _bot_identity 是类上定义的方法——把主类方法绑到这个替身实例上
    async def _bot_identity_call(self):
        from test_command_system import _load_plugin_main as _  # noqa
        raise NotImplementedError


def bind_bot_identity(plugin_like):
    """把 main.LivingPlugin 的 _bot_identity 及其辅助方法绑到替身实例上。"""
    from test_command_system import _load_plugin_main

    main_module = _load_plugin_main()
    cls = main_module.LivingPlugin
    for name in (
        "_platform_prefixes",
        "_identity_is_polluted",
        "_identity_whitelisted",
        "_normalize_bot_identity",
    ):
        attr = inspect.getattr_static(cls, name)
        if isinstance(attr, staticmethod):
            # 静态方法直接赋函数本体：绑定会多传 self
            setattr(plugin_like, name, attr.__func__)
        else:
            setattr(plugin_like, name, attr.__get__(plugin_like))
    return cls._bot_identity.__get__(plugin_like)


# ---------------------------------------------------------------------------
# 需求 1：提取防污染
# ---------------------------------------------------------------------------
def test_polluted_probe_results_return_none(tmp_path):
    """污染提取场景：probe 结果全为 default: 污染身份 → 返回 None（补丁 XVI）。

    旧行为是退回 cron:astrbot 兜底身份——该身份在原生侧并不天然存在，
    写入会产生孤儿节点（两团根因），故改为不注入身份。"""
    polluted_row = FakeRow(1, "我今天读了篇文章", [POLLUTED_PARTICIPANT])
    engine = FakeSelfHealEngine(rows=[polluted_row])
    backend = LivingMemoryBackend(engine)
    plugin = FakePlugin(backend=backend)
    plugin.config = {"platform": [{"id": "aiocqhttp"}],
                     "dashboard": {"username": "astrbot"}}

    identity = asyncio.run(bind_bot_identity(plugin)())

    assert identity is None  # 补丁 XVI：不再造 cron 兜底身份
    # 污染身份绝不进缓存
    cached = getattr(plugin, "_bot_identity_cache", None)
    assert cached is None or "default:" not in cached["identity_key"]


def test_mixed_results_trust_whitelisted_identity(tmp_path):
    """混合场景：1 条正确身份（aiocqhttp:33334444）+ 多条污染 → 采信正确。"""
    rows = [
        FakeRow(1, "我今天读了篇文章", [POLLUTED_PARTICIPANT]),
        FakeRow(2, "冲浪看到好东西", [POLLUTED_PARTICIPANT]),
        FakeRow(3, "记忆里的旧事", [NATIVE_BOT_PARTICIPANT]),
    ]
    engine = FakeSelfHealEngine(rows=rows)
    backend = LivingMemoryBackend(engine)
    plugin = FakePlugin(backend=backend)
    plugin.config = {"platform": [{"id": "aiocqhttp"}],
                     "dashboard": {"username": "astrbot"}}

    identity = asyncio.run(bind_bot_identity(plugin)())
    assert identity["identity_key"] == "aiocqhttp:33334444"
    # 采信的缓存也过了污染校验
    assert plugin._bot_identity_cache["identity_key"] == "aiocqhttp:33334444"


def test_whitelist_dynamic_from_platform_config():
    """平台白名单从 context.get_config() 动态收集（telegram 也能被采信）。"""
    telegram_participant = {
        "identity_key": "telegram:999",
        "sender_id": "999",
        "platform": "telegram",
        "is_bot": True,
    }
    weird_participant = {
        "identity_key": "weirdplatform:1",
        "sender_id": "1",
        "platform": "weirdplatform",
        "is_bot": True,
    }
    rows = [
        FakeRow(1, "的", [weird_participant]),
        FakeRow(2, "冲浪", [telegram_participant]),
    ]
    engine = FakeSelfHealEngine(rows=rows)
    backend = LivingMemoryBackend(engine)
    # 平台白名单读的是 context.get_config() 的 platform 列表；
    # 只声明 aiocqhttp 和 telegram，weirdplatform 不在白名单内
    context = FakeContext({
        "platform": [{"id": "aiocqhttp"}, {"id": "telegram"}],
        "dashboard": {"username": "astrbot"},
    })
    plugin = FakePlugin(backend=backend, context=context)

    identity = asyncio.run(bind_bot_identity(plugin)())
    assert identity["identity_key"] == "telegram:999"  # weird 被白名单拒绝


def test_polluted_cache_is_discarded_and_reextracted(tmp_path):
    """缓存防污染：缓存里已有 default: 身份 → 丢弃并重新提取。"""
    row = FakeRow(1, "记忆碎片", [POLLUTED_PARTICIPANT])
    engine = FakeSelfHealEngine(rows=[row])
    backend = LivingMemoryBackend(engine)
    polluted_cache = {
        "identity_key": "default:640b1b3cdeadbeef",
        "sender_id": "640b1b3cdeadbeef",
        "platform": "default",
        "is_bot": True,
    }
    plugin = FakePlugin(backend=backend, cached=polluted_cache)
    plugin.config = {"platform": [{"id": "aiocqhttp"}],
                     "dashboard": {"username": "astrbot"}}

    identity = asyncio.run(bind_bot_identity(plugin)())

    # 污染缓存被丢弃；引擎被重新查询（走了提取路径）
    assert engine.searched, "污染缓存应触发重新提取"
    # 重提取产物：要么无身份（None），要么是干净身份
    assert identity is None or not identity["identity_key"].startswith("default:")


def test_clean_cache_is_reused_without_search():
    """干净缓存直接复用，不触发引擎查询（性能语义保持）。"""
    clean_cache = {
        "identity_key": "cron:astrbot", "sender_id": "astrbot",
        "platform": "cron", "display_name": "astrbot", "is_bot": True,
    }
    engine = FakeSelfHealEngine(rows=[
        FakeRow(1, "我今天读了篇文章", [POLLUTED_PARTICIPANT]),
    ])
    backend = LivingMemoryBackend(engine)
    plugin = FakePlugin(backend=backend, cached=clean_cache)

    identity = asyncio.run(bind_bot_identity(plugin)())
    assert identity == clean_cache
    assert engine.searched == []  # 没有重新搜索


# ---------------------------------------------------------------------------
# 需求 2：历史污染数据自愈
# ---------------------------------------------------------------------------
class FakeSelfHealEngine:
    """自愈路径的引擎替身：按 available 集裁剪真实存在的接口（未声明
    的方法直接不存在，逼真模拟旧版 LivingMemory 缺接口的情形）。"""

    def __init__(self, rows=None, available=("index_memory",)):
        self.searched = []
        self.updated = []
        self.deleted = []
        self.readded = []
        self.indexed = []
        self.available = set(available)
        self.rows = {row["id"]: row for row in (rows or [])}
        if "update_memory" in self.available:
            self.update_memory = self._update_memory
        if "delete_memory" in self.available:
            self.delete_memory = self._delete_memory
        if "index_memory" in self.available:
            self.graph_memory_manager = types_ns(
                index_memory=self._index_memory
            )

    def _index_memory(self, memory_id, content, metadata):
        self.indexed.append((memory_id, content, metadata))

    async def search_memories(self, query, k=5, session_id=None, **kw):
        self.searched.append(query)
        return [
            FakeHybrid(result)
            for result in list(self.rows.values())[:k]
        ]

    def _update_memory(self, memory_id, metadata=None, **kw):
        self.updated.append((memory_id, metadata))

    def _delete_memory(self, memory_id):
        self.deleted.append(memory_id)

    async def add_memory(self, content, **kw):
        self.readded.append((content, kw))
        return len(self.readded)


class FakeHybrid:
    def __init__(self, row):
        self.doc_id = row["id"]
        self.content = row["content"]
        self.metadata = row["metadata"]
        self.final_score = 1.0


class FakeRow(dict):
    def __init__(self, rid, content, participants):
        super().__init__(
            id=rid, content=content,
            metadata={"participant_identities": participants},
        )


class BackendWithEngine:
    def __init__(self, engine):
        self.engine = engine

    async def search(self, query, k=5):
        return [
            {"id": r["id"], "content": r["content"], "metadata": r["metadata"]}
            for r in list(self.engine.rows.values())[:k]
        ]


CORRECTED = {
    "identity_key": "cron:astrbot",
    "sender_id": "astrbot",
    "platform": "cron",
    "display_name": "astrbot",
    "aliases": ["astrbot"],
    "is_bot": True,
}


def test_selfheal_detect_fix_and_idempotency(tmp_path):
    """自愈主流程：检测→修正（index_memory 路径）→state 记账→幂等。"""
    polluted_row = FakeRow(101, "我今天冲浪了", [POLLUTED_PARTICIPANT])
    engine = FakeSelfHealEngine(rows=[polluted_row])
    backend = BackendWithEngine(engine)
    state_path = tmp_path / "selfheal_state.json"

    summary = asyncio.run(
        run_identity_selfheal(backend, CORRECTED, state_path, engine=engine)
    )
    assert summary["found"] == 1
    assert summary["fixed"] == 1
    assert "index_memory" in summary["paths"]
    # 修正后的 metadata：污染身份被替换为 cron:astrbot 完整结构
    mid, content, metadata = engine.indexed[0]
    assert mid == 101
    assert metadata["participant_identities"] == [CORRECTED]
    assert "default:" not in json.dumps(metadata)

    # 幂等：state 已记账，重复运行零修正
    summary2 = asyncio.run(
        run_identity_selfheal(backend, CORRECTED, state_path, engine=engine)
    )
    assert summary2["fixed"] == 0
    assert summary2["skipped"] == 1
    assert len(engine.indexed) == 1  # 没有重复重建


def test_selfheal_never_touches_native_memories(tmp_path):
    """原生记忆（aiocqhttp:/cron: 身份）绝不被修正或删除。"""
    rows = [
        FakeRow(201, "主人说早安", [{
            "identity_key": "aiocqhttp:11112222", "sender_id": "11112222",
            "platform": "aiocqhttp", "is_bot": False,
        }]),
        FakeRow(202, "cron 汇总", [{
            "identity_key": "cron:astrbot", "sender_id": "astrbot",
            "platform": "cron", "is_bot": True,
        }]),
        FakeRow(203, "我今天冲浪了", [POLLUTED_PARTICIPANT]),  # 唯一污染条
    ]
    engine = FakeSelfHealEngine(rows=rows, available=("update_memory",))
    backend = BackendWithEngine(engine)
    state_path = tmp_path / "state.json"

    summary = asyncio.run(
        run_identity_selfheal(backend, CORRECTED, state_path, engine=engine)
    )
    assert summary["fixed"] == 1
    # 只有污染条 203 被更新；201/202 无任何写操作
    assert [u[0] for u in engine.updated] == [203]
    assert engine.deleted == []


def test_selfheal_update_path_preferred(tmp_path):
    """引擎有 update_memory 时优先走直接更新（而不是 index/delete）。"""
    polluted_row = FakeRow(301, "我今天整理了记忆", [POLLUTED_PARTICIPANT])
    engine = FakeSelfHealEngine(rows=[polluted_row],
                                available=("update_memory", "index_memory"))
    backend = BackendWithEngine(engine)
    state_path = tmp_path / "state.json"

    summary = asyncio.run(
        run_identity_selfheal(backend, CORRECTED, state_path, engine=engine)
    )
    assert summary["paths"] == ["update_memory"]
    assert engine.indexed == []
    assert engine.deleted == []


def test_selfheal_delete_readd_fallback(tmp_path):
    """update/index 都不可用 → 删除重写兜底路径。"""
    polluted_row = FakeRow(401, "我今天冲浪了", [POLLUTED_PARTICIPANT])
    engine = FakeSelfHealEngine(rows=[polluted_row],
                                available=("delete_memory",))
    backend = BackendWithEngine(engine)
    state_path = tmp_path / "state.json"

    summary = asyncio.run(
        run_identity_selfheal(backend, CORRECTED, state_path, engine=engine)
    )
    assert summary["paths"] == ["delete_and_readd"]
    assert engine.deleted == [401]
    assert engine.readded, "删除后应重写记忆"
    readd_kwargs = engine.readded[0][1]
    assert readd_kwargs["metadata"]["participant_identities"] == [CORRECTED]


def test_selfheal_state_file_written(tmp_path):
    """幂等状态落 selfheal_state.json（记录修正的记忆 id 与时间）。"""
    polluted_row = FakeRow(501, "我今天冲浪了", [POLLUTED_PARTICIPANT])
    engine = FakeSelfHealEngine(rows=[polluted_row])
    backend = BackendWithEngine(engine)
    state_path = tmp_path / "selfheal_state.json"

    asyncio.run(run_identity_selfheal(backend, CORRECTED, state_path, engine=engine))

    state = json.loads(Path(state_path).read_text(encoding="utf-8"))
    assert "501" in state["healed"]
    assert state["healed"]["501"]  # 有时间戳
    assert state["last_run"]
