"""M8 补丁 1 测试：db 连接生命周期纪律与哨兵配置的存在性验证。

背景：aiosqlite 连接的后台线程绑定创建时所在的事件循环；实例跨
asyncio.run 复用或用后不 close，线程会向已关 loop 投递（Event loop
is closed）——间歇失败与 PytestUnhandledThreadExceptionWarning 的根源。

本文件锁定两件事：
1. 收尾纪律的可靠性——"连接在 loop1 触库、loop1 关闭后于新 loop close"
   是安全的（这是全部修复采用的手法）；close 幂等。
2. pytest.ini 的哨兵（线程异常升级为失败）与收集范围防呆在位。
"""

import asyncio
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from core.living_state import LivingGate
from core.mood import MoodState

WORKDIR = Path(__file__).resolve().parents[1]
NOW_BASE = datetime(2026, 9, 22, 10, 0, 0)
NOW_LATER = NOW_BASE + timedelta(hours=5)


def test_close_from_fresh_loop_after_loop_closed_is_safe(tmp_path):
    """收尾纪律的可靠性证明：run1 触库（不关）→ run1 关闭 → 新 run close。

    close 的 future 绑定当前（存活的）loop，安全落地；连接关闭、后台
    线程退出。这正是 M8-补丁1 全部修复采用的收尾手法。"""
    gate = LivingGate(config_getter=lambda: {}, db_path=str(tmp_path / "g.db"),
                      rng=lambda: 0.5)

    async def touch():
        await gate.enter_autonomous_sleep(NOW_LATER, "long", NOW_BASE)
        return gate._db is not None

    async def closer():
        await gate.close()
        return gate._db

    assert asyncio.run(touch()) is True
    assert asyncio.run(closer()) is None


def test_mood_close_is_idempotent(tmp_path):
    """close 重复调用安全（_db 为 None 时 no-op）——补 close 时不必担心
    重复收尾。"""
    mood = MoodState(db_path=str(tmp_path / "mood.db"))

    async def flow():
        await mood.load()
        await mood.close()
        await mood.close()  # 第二次应静默
        return mood._db

    assert asyncio.run(flow()) is None


def test_never_connected_instance_close_is_noop():
    """惰性连接：从未触库的实例没有连接，close 是 no-op（不炸、不建库）。"""
    mood = MoodState(db_path=":memory:")
    gate = LivingGate(config_getter=lambda: {}, db_path=":memory:", rng=lambda: 0.5)

    async def flow():
        await mood.close()
        await gate.close()
        return mood._db, gate._db

    assert asyncio.run(flow()) == (None, None)


def test_sentinel_config_in_place():
    """pytest.ini：线程异常升级为失败的哨兵 + 收集范围限定 tests/。"""
    text = (WORKDIR / "pytest.ini").read_text(encoding="utf-8")
    assert "error::pytest.PytestUnhandledThreadExceptionWarning" in text
    assert "testpaths = tests" in text
