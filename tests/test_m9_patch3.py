"""M9-补丁3 测试：身份白名单语义锁定 + SelfHeal 存量回填。

根因修正说明（与任务书的差异，报告详述）：任务书认为白名单只认平台
实例 id——实际 _platform_prefixes 自补丁 XVI（22bbcd7f）起就收 id+type
双维度；真正断点是 M5-补丁4（a32dae8）在 on_any_message 上方插入
_extract_schedule_safe 时把 @filter.event_message_type(ALL) 装饰器
"抢走"，on_any_message 自此失去注册，_remember_self_identity 从未
执行。本文件对这两点都做锁定。

测试身份一律用既有先例假号（10001，test_m3_patch16 同款）——
真实 QQ 号不得进 git（任务书红线 7）。
"""

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from test_m3_patch5 import FakeContext, make_identity_plugin  # noqa: E402
from test_m3_patch16 import FakeRealEvent  # noqa: E402

BOT_IDENTITY = {
    "identity_key": "aiocqhttp:10001",
    "sender_id": "10001",
    "platform": "aiocqhttp",
    "display_name": "aiocqhttp",
    "aliases": ["aiocqhttp"],
    "is_bot": True,
}

GHOST_SESSION = "living_ghost:FriendMessage:living_autonomous"
NATIVE_SESSION = "aiocqhttp:FriendMessage:10002"


def _load_main():
    from test_m3_patch5 import _load_main as loader

    return loader()


# ---------------------------------------------------------------------------
# A 组：白名单语义（任务书 A1/A2——实为锁定既有行为，无代码改动）
# ---------------------------------------------------------------------------
def test_platform_prefixes_and_whitelist_matrix():
    """白名单收 id+type+cron 三源；default: 靠污染过滤而非白名单拒绝。"""
    from core.selfheal import POLLUTED_PREFIX

    ctx = FakeContext({"platform": [{"id": "default", "type": "aiocqhttp"}]})
    plugin = make_identity_plugin(context=ctx)
    prefixes = plugin._platform_prefixes()
    assert prefixes == {"cron", "default", "aiocqhttp"}
    # 任务书 A2 断言矩阵（号值为既有测试先例假号）
    assert plugin._identity_whitelisted("aiocqhttp:10001", prefixes)
    assert plugin._identity_whitelisted("cron:astrbot", prefixes)
    # 白名单本身放行 default:（id 维度），拒绝靠补丁 XVI 的污染过滤——
    # 两道闸语义不同，必须分别锁定（红线 5：污染过滤语义不变）
    assert plugin._identity_whitelisted("default:abc", prefixes)
    assert plugin._identity_is_polluted("default:abc")
    assert POLLUTED_PREFIX == "default:"
    assert not plugin._identity_whitelisted("telegram:12345", prefixes)


def test_remember_then_path0_serves_identity():
    """连锁验证（A3/验收#2）：真实消息 → 缓存 → _bot_identity 路径 0 采信。

    engine 空记忆（路径 1 无从提取）——采信到的 aiocqhttp 身份必然来自
    路径 0（真实事件缓存），即 M5-补丁4 弄丢装饰器前补丁 XVI 的设计链路。
    """
    from test_m3_patch5 import FakeEngine

    engine = FakeEngine(rows=[])
    ctx = FakeContext({"platform": [{"id": "default", "type": "aiocqhttp"}]})
    plugin = make_identity_plugin(engine=engine, context=ctx)
    plugin._remember_self_identity(
        FakeRealEvent(self_id="10001", platform="aiocqhttp")
    )
    assert plugin._self_identity["identity_key"] == "aiocqhttp:10001"
    identity = asyncio.run(plugin._bot_identity())
    assert identity is not None
    assert identity["identity_key"] == "aiocqhttp:10001"
    assert engine.searched == []  # 路径 0 命中，未触发记忆搜索


def test_pollution_cache_still_discarded():
    """污染回归（C3）：缓存的 default: 身份仍被丢弃重新提取（补丁 XVI）。"""
    from test_m3_patch5 import FakeEngine

    engine = FakeEngine(rows=[])
    ctx = FakeContext({"platform": [{"id": "default", "type": "aiocqhttp"}]})
    plugin = make_identity_plugin(engine=engine, context=ctx)
    plugin._bot_identity_cache = {
        "identity_key": "default:deadbeef",
        "is_bot": True,
    }
    identity = asyncio.run(plugin._bot_identity())
    # 污染缓存被丢弃；无其他来源 → None（绝不回写污染身份）
    assert identity is None
    assert plugin._bot_identity_cache is None


def test_on_any_message_registered_with_event_filter():
    """M9-补丁3 核心：on_any_message 的 ALL 事件过滤器注册已恢复。

    M5-补丁4 把装饰器让给了 _extract_schedule_safe，本方法在线上失联
    近两周（身份采集/吵醒计数/待机刷新/静默拦截全部失效）。直接查
    AstrBot 的全局 handler 注册表——比反射方法体更能代表运行时行为。
    """
    from astrbot.core.star.filter.event_message_type import (
        EventMessageType,
        EventMessageTypeFilter,
    )
    from astrbot.core.star.register.star_handler import (
        get_handler_full_name,
        star_handlers_registry,
    )

    main_module = _load_main()
    for name in ("on_any_message", "_extract_schedule_safe"):
        handler = getattr(main_module.LivingPlugin, name)
        md = star_handlers_registry.get_handler_by_full_name(
            get_handler_full_name(handler)
        )
        assert md is not None, f"{name} 未注册进 AstrBot handler 表"
        filters = [
            f
            for f in md.event_filters
            if isinstance(f, EventMessageTypeFilter)
        ]
        assert filters, f"{name} 缺 EventMessageTypeFilter"
        assert any(
            f.event_message_type == EventMessageType.ALL for f in filters
        ), f"{name} 未覆盖 ALL 消息"


def test_on_any_message_no_duplicate_schedule_call():
    """on_any_message 体内不再 create_task 调 _extract_schedule_safe。

    该调用块传参与凛 1ecef9c 修好的签名 (self, event) 失配，恢复注册后
    每条消息都会 TypeError；约定提取已由装饰器路径覆盖（上一条测试锁定）。
    """
    import inspect

    main_module = _load_main()
    source = inspect.getsource(main_module.LivingPlugin.on_any_message)
    assert "self._extract_schedule_safe(" not in source
    assert "asyncio.create_task(" not in source


# ---------------------------------------------------------------------------
# B 组：SelfHeal 存量回填（documents 同构替身 + 临时 db）
# ---------------------------------------------------------------------------
class BackfillFakeEngine:
    """documents 表同构的 LivingMemory 引擎替身。

    update_memory 按 2026-09-23 对 LivingMemory 源码核实的真实行为建模：
    updates={"metadata": {...}} 为合并式补丁（其余键保留），且键触及
    participant_identities 时引擎内部联动 graph_memory_manager.index_memory
    重建图谱——回填与图谱联动由此单次调用完成（任务书 B3）。
    """

    def __init__(self, db_path):
        self.db_path = str(db_path)
        self.db_connection = None
        self.updates_called = []
        self.graph_indexed = []
        self.fail_ids = set()

    async def _conn(self):
        if self.db_connection is None:
            import aiosqlite

            self.db_connection = await aiosqlite.connect(self.db_path)
            # LivingMemory 的连接使用带名访问（get_session_memories 源码
            # 用 row["metadata"]），生产扫描代码同款依赖 Row factory
            self.db_connection.row_factory = aiosqlite.Row
            await self.db_connection.execute(
                "CREATE TABLE IF NOT EXISTS documents "
                "(id INTEGER PRIMARY KEY, text TEXT, metadata TEXT)"
            )
        return self.db_connection

    async def seed(self, rows):
        db = await self._conn()
        for rid, text, meta in rows:
            await db.execute(
                "INSERT INTO documents (id, text, metadata) VALUES (?, ?, ?)",
                (rid, text, json.dumps(meta, ensure_ascii=False)),
            )
        await db.commit()

    async def update_memory(self, memory_id, updates):
        self.updates_called.append((memory_id, updates))
        if memory_id in self.fail_ids:
            raise RuntimeError("engine boom")
        db = await self._conn()
        cur = await db.execute(
            "SELECT metadata FROM documents WHERE id = ?", (memory_id,)
        )
        row = await cur.fetchone()
        meta = json.loads(row[0]) if row and row[0] else {}
        patch = updates.get("metadata") or {}
        meta.update(patch)
        await db.execute(
            "UPDATE documents SET metadata = ? WHERE id = ?",
            (json.dumps(meta, ensure_ascii=False), memory_id),
        )
        await db.commit()
        if "participant_identities" in patch:
            self.graph_indexed.append(memory_id)  # 引擎的自动图谱联动
        return True

    async def metadata_of(self, memory_id):
        db = await self._conn()
        cur = await db.execute(
            "SELECT metadata FROM documents WHERE id = ?", (memory_id,)
        )
        row = await cur.fetchone()
        return json.loads(row[0]) if row and row[0] else {}

    async def close(self):
        if self.db_connection is not None:
            await self.db_connection.close()
            self.db_connection = None


def _seed_rows():
    return [
        # 命中：living 直写、无身份、正文非空
        (1, "昨晚冲浪看到一篇讲深海生物的文章", {"session_id": GHOST_SESSION}),
        # 已有身份：幂等跳过
        (
            2,
            "整理了记忆宫殿",
            {
                "session_id": GHOST_SESSION,
                "participant_identities": [dict(BOT_IDENTITY)],
            },
        ),
        # 空正文：占位/测试残留跳过
        (3, "", {"session_id": GHOST_SESSION}),
        # 原生侧对话记忆：防误伤（任务书 B4/验收#5）
        (
            4,
            "主人发来的消息总结",
            {"session_id": NATIVE_SESSION},
        ),
    ]


def test_backfill_scans_and_fills_ghost_memories(tmp_path):
    """验收#4/#5：只有 living_ghost 无身份且正文非空的记忆被回填。"""
    from core.selfheal import run_ghost_identity_backfill

    async def flow():
        engine = BackfillFakeEngine(tmp_path / "mem.db")
        try:
            await engine.seed(_seed_rows())
            summary = await run_ghost_identity_backfill(engine, dict(BOT_IDENTITY))
            native_meta = await engine.metadata_of(4)
            return summary, engine, native_meta
        finally:
            await engine.close()

    summary, engine, native_meta = asyncio.run(flow())
    # 统计（B5）：SQL 命中 3 行 living_ghost（id 1/2/3），回填 1，跳过 2
    assert summary == {"scanned": 3, "backfilled": 1, "skipped": 2}
    # 只有 id=1 被回填，且身份结构与写入规范同构（B2）
    assert [rid for rid, _ in engine.updates_called] == [1]
    patch = engine.updates_called[0][1]["metadata"]["participant_identities"]
    assert patch == [dict(BOT_IDENTITY)]
    assert engine.graph_indexed == [1]  # 回填触发引擎图谱联动（B3）
    # 原生侧记忆原封不动
    assert "participant_identities" not in native_meta


def test_backfill_idempotent_second_run(tmp_path):
    """验收#4 幂等：第二遍运行 0 回填（已有身份即跳过）。"""
    from core.selfheal import run_ghost_identity_backfill

    async def flow():
        engine = BackfillFakeEngine(tmp_path / "mem.db")
        try:
            await engine.seed(_seed_rows())
            first = await run_ghost_identity_backfill(engine, dict(BOT_IDENTITY))
            calls_after_first = len(engine.updates_called)
            second = await run_ghost_identity_backfill(engine, dict(BOT_IDENTITY))
            return first, second, calls_after_first, len(engine.updates_called)
        finally:
            await engine.close()

    first, second, calls_first, calls_total = asyncio.run(flow())
    assert first["backfilled"] == 1
    assert second == {"scanned": 3, "backfilled": 0, "skipped": 3}
    assert calls_total == calls_first  # 第二遍零落库调用


def test_backfill_survives_engine_errors(tmp_path):
    """B3 解耦：单条回填失败只记日志不中断，其余照常处理。"""
    from core.selfheal import run_ghost_identity_backfill

    async def flow():
        engine = BackfillFakeEngine(tmp_path / "mem.db")
        try:
            rows = _seed_rows()
            rows.append((5, "第二次自主活动记录", {"session_id": GHOST_SESSION}))
            await engine.seed(rows)
            engine.fail_ids = {1}  # 第一条失败
            summary = await run_ghost_identity_backfill(engine, dict(BOT_IDENTITY))
            return summary, engine
        finally:
            await engine.close()

    summary, engine = asyncio.run(flow())
    assert summary["backfilled"] == 1  # id=5 成功
    assert {rid for rid, _ in engine.updates_called} == {1, 5}


def test_backfill_without_db_connection_is_safe():
    """引擎接口变化（无 db_connection）→ 空统计不抛异常。"""

    class NoDb:
        update_memory = None  # 连方法都没有

    from core.selfheal import run_ghost_identity_backfill

    summary = asyncio.run(run_ghost_identity_backfill(NoDb(), dict(BOT_IDENTITY)))
    assert summary == {"scanned": 0, "backfilled": 0, "skipped": 0}


def test_remember_self_identity_schedules_backfill(tmp_path):
    """端到端：真实事件 → 身份缓存 → 后台回填任务自动执行。

    主人场景（清空记忆 → 第一条真实消息 → 身份可用 → 存量并回图谱）
    的自动化版本；任务引用被实例持有（防 GC），完成后可断言落库调用。
    """
    async def flow():
        engine = BackfillFakeEngine(tmp_path / "mem.db")
        try:
            await engine.seed(_seed_rows())
            ctx = FakeContext(
                {"platform": [{"id": "default", "type": "aiocqhttp"}]}
            )
            plugin = make_identity_plugin(engine=engine, context=ctx)
            plugin._remember_self_identity(
                FakeRealEvent(self_id="10001", platform="aiocqhttp")
            )
            task = getattr(plugin, "_backfill_task", None)
            assert task is not None
            await task  # 后台回填跑完
            return engine
        finally:
            await engine.close()

    engine = asyncio.run(flow())
    assert [rid for rid, _ in engine.updates_called] == [1]
    assert engine.graph_indexed == [1]


def test_backfill_skipped_when_no_identity(tmp_path):
    """身份不可用（无缓存、记忆空）→ 触发后零落库调用。"""
    from test_m3_patch5 import FakeEngine

    async def flow():
        engine = BackfillFakeEngine(tmp_path / "mem.db")
        try:
            await engine.seed(_seed_rows())
            ctx = FakeContext(
                {"platform": [{"id": "default", "type": "aiocqhttp"}]}
            )
            plugin = make_identity_plugin(engine=FakeEngine(rows=[]), context=ctx)
            plugin._self_identity = None
            plugin._schedule_ghost_backfill()
            task = getattr(plugin, "_backfill_task", None)
            if task is not None:
                await task
            return engine
        finally:
            await engine.close()

    engine = asyncio.run(flow())
    assert engine.updates_called == []


def test_terminate_covers_backfill_task():
    """terminate 的后台任务清理清单包含回填任务（防热重载遗弃）。"""
    import inspect

    main_module = _load_main()
    source = inspect.getsource(main_module.LivingPlugin.terminate)
    assert "_backfill_task" in source
