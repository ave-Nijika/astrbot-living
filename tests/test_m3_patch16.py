"""M3 补丁 XVI 测试：bot 身份与原生侧同源（消除"两个独立图谱"）。

两团根因（2026-09-18 现场诊断）：
  - 原生侧 LivingMemory 给 bot 建 person 节点用 {platform_name}:{self_id}
    （实测 aiocqhttp:10001）；
  - living 的 _platform_prefixes 只收 platform **id**（"default"），
    白名单匹配不上 → 提取链落空 → 兜底 cron:{dashboard_username}；
  - 兜底身份在原生的"家"并不存在（环境重置后尤甚）→ 孤儿节点 → 两团。

本补丁三处修正：白名单补 type 维度、真实消息事件记录自身身份（最权威）、
兜底不再造身份（返回 None）。
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.ghost_event import build_ghost_event  # noqa: E402

from test_m3_patch5 import FakeContext, make_identity_plugin  # noqa: E402


class FakeRealEvent:
    """真实平台事件替身（aiocqhttp 适配器的 PlatformMetadata.name="aiocqhttp"）。"""

    def __init__(self, self_id="10001", platform="aiocqhttp"):
        self._self_id = self_id
        self._platform = platform

    def get_self_id(self):
        return self._self_id

    def get_platform_name(self):
        return self._platform


# ---------------------------------------------------------------------------
# 白名单：type 维度必须被收集（否则永远匹配不上原生身份）
# ---------------------------------------------------------------------------
def test_platform_prefixes_includes_platform_type():
    """补丁 XVI：白名单同时收 platform 的 id 与 type。"""
    ctx = FakeContext({"platform": [{"id": "default", "type": "aiocqhttp"}]})
    plugin = make_identity_plugin(context=ctx)
    prefixes = plugin._platform_prefixes()
    assert "default" in prefixes  # id 维度（补丁 VI 原有）
    assert "aiocqhttp" in prefixes  # type 维度（补丁 XVI 新增，关键）
    assert "cron" in prefixes


def test_identity_whitelisted_accepts_platform_type_prefix():
    """补丁 XVI：aiocqhttp:{qq} 形式的身份通过白名单。"""
    ctx = FakeContext({"platform": [{"id": "default", "type": "aiocqhttp"}]})
    plugin = make_identity_plugin(context=ctx)
    assert plugin._identity_whitelisted(
        "aiocqhttp:10001", plugin._platform_prefixes()
    )
    assert not plugin._identity_whitelisted(
        "telegram:12345", plugin._platform_prefixes()
    )


def test_bot_identity_extracted_from_native_memory():
    """提取链修复后：原生记忆里的 aiocqhttp 身份可被采信。

    这是两团修复的正向证据——补丁 VI 时代该候选会被白名单拒绝，
    退到 cron 兜底。
    """
    native_bot = {
        "identity_key": "aiocqhttp:10001",
        "sender_id": "10001",
        "platform": "aiocqhttp",
        "display_name": "aiocqhttp",
        "is_bot": True,
    }
    from test_m3_patch5 import FakeEngine

    engine = FakeEngine(
        rows=[
            {
                "content": "对话记忆",
                "score": 1.0,
                "metadata": {"participant_identities": [native_bot]},
            }
        ]
    )
    ctx = FakeContext({"platform": [{"id": "default", "type": "aiocqhttp"}]})
    plugin = make_identity_plugin(engine=engine, context=ctx)
    identity = asyncio.run(plugin._bot_identity())
    assert identity is not None
    assert identity["identity_key"] == "aiocqhttp:10001"


# ---------------------------------------------------------------------------
# 自身身份记录（真实事件 → 缓存；幽灵事件 → 拒绝）
# ---------------------------------------------------------------------------
def test_remember_self_identity_from_real_event():
    ctx = FakeContext({"platform": [{"id": "default", "type": "aiocqhttp"}]})
    plugin = make_identity_plugin(context=ctx)
    plugin._remember_self_identity(FakeRealEvent())
    assert plugin._self_identity["identity_key"] == "aiocqhttp:10001"
    assert plugin._self_identity["platform"] == "aiocqhttp"
    assert plugin._self_identity["is_bot"] is True


def test_remember_self_identity_rejects_ghost_event():
    """幽灵事件（living_ghost 平台）不得被采信为自身身份。"""
    ctx = FakeContext({"platform": [{"id": "default", "type": "aiocqhttp"}]})
    plugin = make_identity_plugin(context=ctx)
    ghost = build_ghost_event()
    plugin._remember_self_identity(ghost)
    assert getattr(plugin, "_self_identity", None) is None


def test_remember_self_identity_rejects_non_whitelisted_platform():
    """不在平台白名单内的平台不采信。"""
    ctx = FakeContext({"platform": [{"id": "default", "type": "aiocqhttp"}]})
    plugin = make_identity_plugin(context=ctx)
    plugin._remember_self_identity(FakeRealEvent(platform="telegram"))
    assert getattr(plugin, "_self_identity", None) is None


def test_remember_self_identity_is_fault_tolerant():
    """任何异常都不得冒泡（身份采集不能影响消息主链路）。"""

    class Broken:
        def get_self_id(self):
            raise RuntimeError("boom")

        def get_platform_name(self):
            raise RuntimeError("boom")

    ctx = FakeContext({"platform": [{"id": "default", "type": "aiocqhttp"}]})
    plugin = make_identity_plugin(context=ctx)
    plugin._remember_self_identity(Broken())  # 不应抛异常
    assert getattr(plugin, "_self_identity", None) is None


# ---------------------------------------------------------------------------
# 落空时不造孤儿身份
# ---------------------------------------------------------------------------
def test_bot_identity_returns_none_when_nothing_available():
    """无自身身份、原生记忆也无 bot 身份 → None（不是 cron 兜底）。"""
    from test_m3_patch5 import FakeEngine

    engine = FakeEngine(rows=[])  # 原生侧什么都没有
    ctx = FakeContext({"platform": [{"id": "default", "type": "aiocqhttp"}]})
    plugin = make_identity_plugin(engine=engine, context=ctx)
    assert asyncio.run(plugin._bot_identity()) is None
