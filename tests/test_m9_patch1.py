"""M9 补丁 1 测试：主人身份自动认领（Part A）+ 心境与兴趣面板管理（Part B）。

Part A 走真实 LivingLoop / SleepManager（gate/sender 用可控替身）+ 真实
derive_admin_identity 派生函数；Part B 走真实 MoodState（临时 db）+ 真实
panel_api 纯逻辑层；装配用合成包直调 main 的 mood handler（M5-补丁1 先例）。
全部显式传测试虚拟时钟。
"""

import asyncio
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from core.living_loop import LivingLoop, derive_admin_identity
from core.mood import MoodState
from core.panel_api import (
    PanelApiError,
    apply_mood_interests,
    build_mood_snapshot,
)
from core.sleep import SleepManager

NOW = datetime(2026, 9, 23, 10, 0, 0)

GLOBAL_CONFIG = {
    "admins_id": ["111", "222"],
    "platform": [
        {"id": "off", "type": "aiocqhttp", "enable": False},
        {"id": "default", "type": "aiocqhttp", "enable": True},
    ],
}

PLUGIN_CONFIG = {
    "output_gate": {"target_sessions": ""},
    "sleep": {"wake_source": "all", "owner_id": ""},
}


# ---------------------------------------------------------------------------
# 替身（与 test_living_loop 同款最小版）
# ---------------------------------------------------------------------------
class FakeGate:
    async def should_send_message(self, now=None):
        return True, "ok"

    async def note_message_sent(self, now=None):
        pass


class FakeSender:
    def __init__(self):
        self.sent = []

    async def send(self, session, text):
        self.sent.append((session, text))
        return True


def make_loop(config=None, global_config=None, sender=None) -> LivingLoop:
    return LivingLoop(
        gate=FakeGate(),
        memory_getter=lambda: None,
        config_getter=lambda: PLUGIN_CONFIG if config is None else config,
        sender=sender or FakeSender(),
        global_config_getter=(
            (lambda: GLOBAL_CONFIG) if global_config is None else global_config
        ),
    )


def make_manager(config=None, global_config=None) -> SleepManager:
    return SleepManager(
        config_getter=lambda: PLUGIN_CONFIG if config is None else config,
        gate=object(),  # counts_toward_wake 不消费 gate
        global_config_getter=(
            (lambda: GLOBAL_CONFIG) if global_config is None else global_config
        ),
    )


def make_mood(tmp_path=None, interests=None) -> MoodState:
    base = Path(tmp_path) if tmp_path is not None else Path(tempfile.mkdtemp())
    mood = MoodState(db_path=str(base / "mood.db"), now_provider=lambda: NOW)
    asyncio.run(mood.load())
    if interests is not None:
        mood.interests = dict(interests)
        asyncio.run(mood.save())
    return mood


# ---------------------------------------------------------------------------
# Part A：派生函数本身（A2）
# ---------------------------------------------------------------------------
def test_derive_admin_identity_basic():
    info = derive_admin_identity(GLOBAL_CONFIG)
    assert info["admins_id"] == ["111", "222"]
    assert info["platform_id"] == "default"  # 第一个 enable=True 的适配器


def test_derive_admin_identity_empty_and_garbage():
    assert derive_admin_identity(None) == {"admins_id": [], "platform_id": None}
    assert derive_admin_identity({}) == {"admins_id": [], "platform_id": None}
    assert derive_admin_identity({"admins_id": ["", "  "], "platform": []}) == {
        "admins_id": [],
        "platform_id": None,
    }
    # 非法条目跳过、空白清洗、无启用适配器时 platform_id 为 None
    info = derive_admin_identity({"admins_id": [333, " 444 "], "platform": [{"id": "x"}]})
    assert info["admins_id"] == ["333", "444"]
    assert info["platform_id"] is None


# ---------------------------------------------------------------------------
# Part A：target_sessions 三段优先级（验收 1/2/3）
# ---------------------------------------------------------------------------
def test_explicit_target_sessions_wins_over_derived():
    """验收 1：显式配置非空 → 用显式值，派生不覆盖。"""
    config = {"output_gate": {"target_sessions": "qq:GroupMessage:777\n"}}
    sender = FakeSender()
    loop = make_loop(config=config, sender=sender)
    asyncio.run(loop._maybe_share("今天翻了不少有意思的东西", NOW))
    assert sender.sent == [("qq:GroupMessage:777", "今天翻了不少有意思的东西")]


def test_derived_sessions_all_admins_friend_chats():
    """验收 2：显式空 + admins_id=["111","222"] + platform default →
    sessions = [default:FriendMessage:111, default:FriendMessage:222]。"""
    sender = FakeSender()
    loop = make_loop(sender=sender)
    sessions, source = loop._resolve_target_sessions()
    assert sessions == [
        "default:FriendMessage:111",
        "default:FriendMessage:222",
    ]
    assert source == "派生"
    asyncio.run(loop._maybe_share("今天翻了不少有意思的东西", NOW))
    assert [s for s, _ in sender.sent] == [
        "default:FriendMessage:111",
        "default:FriendMessage:222",
    ]


def test_no_admins_falls_back_to_silent():
    """验收 3：显式空 + admins_id 空 → 现状行为（不发送）。"""
    sender = FakeSender()
    loop = make_loop(global_config=lambda: {"admins_id": [], "platform": []}, sender=sender)
    sessions, source = loop._resolve_target_sessions()
    assert sessions == [] and source == "空"
    asyncio.run(loop._maybe_share("今天翻了不少有意思的东西", NOW))
    assert sender.sent == []


def test_no_global_config_getter_falls_back_to_silent():
    """未注入 global_config_getter（旧用法）→ 不派生，保持现状语义。"""
    loop = LivingLoop(
        gate=FakeGate(),
        memory_getter=lambda: None,
        config_getter=lambda: PLUGIN_CONFIG,
        sender=FakeSender(),
    )
    assert loop._resolve_target_sessions() == ([], "空")


def test_short_text_guard_precedes_derivation():
    """M9-补丁1 A5：M7 的空产物防线保持在 sessions 计算之前——空产物
    即使有管理员可派生也不发送。"""
    sender = FakeSender()
    loop = make_loop(sender=sender)
    asyncio.run(loop._maybe_share("嗯", NOW))
    assert sender.sent == []


# ---------------------------------------------------------------------------
# Part A：owner_id 派生（验收 4）与既有语义
# ---------------------------------------------------------------------------
def test_owner_wake_derived_from_first_admin():
    """验收 4：wake_source=owner_only 且 owner_id 显式空 → 生效主人为
    admins_id[0]。"""
    config = {"sleep": {"wake_source": "owner_only", "owner_id": ""}}
    manager = make_manager(config=config)
    assert manager.counts_toward_wake("111") is True
    assert manager.counts_toward_wake("222") is False  # 第二位管理员不算主人
    assert manager.counts_toward_wake("333") is False
    assert manager.counts_toward_wake(None) is False


def test_owner_explicit_config_wins_over_derived():
    config = {"sleep": {"wake_source": "owner_only", "owner_id": "999"}}
    manager = make_manager(config=config)
    assert manager.counts_toward_wake("999") is True
    assert manager.counts_toward_wake("111") is False  # 手填优先，派生不覆盖


def test_owner_wake_all_mode_unchanged():
    """wake_source=all 行为与派生引入前逐位一致：任何人都计入。"""
    config = {"sleep": {"wake_source": "all", "owner_id": ""}}
    manager = make_manager(config=config)
    assert manager.counts_toward_wake("anyone") is True
    assert manager.counts_toward_wake(None) is True


def test_owner_wake_no_getter_no_admin_falls_back_to_all():
    """owner_only + owner 手填/派生均空 → 回退 all（现状语义）。"""
    config = {"sleep": {"wake_source": "owner_only", "owner_id": ""}}
    manager = make_manager(config=config, global_config=lambda: {"admins_id": []})
    assert manager.counts_toward_wake("anyone") is True
    legacy = SleepManager(
        config_getter=lambda: config, gate=object()
    )  # 不传 getter（向后兼容）
    assert legacy.counts_toward_wake("anyone") is True


def test_global_config_hot_reload_no_cache():
    """验收 5：global_config_getter 返回值变化 → 下次读取即新值（无缓存）。"""
    holder = {"cfg": dict(GLOBAL_CONFIG)}
    sender = FakeSender()
    loop = make_loop(global_config=lambda: holder["cfg"], sender=sender)
    asyncio.run(loop._maybe_share("今天翻了不少有意思的东西", NOW))
    assert [s for s, _ in sender.sent] == [
        "default:FriendMessage:111",
        "default:FriendMessage:222",
    ]
    # 管理员名单变化（删掉 222、新增 333）——下次分享即按新名单派生
    holder["cfg"] = {"admins_id": ["111", "333"], "platform": GLOBAL_CONFIG["platform"]}
    asyncio.run(loop._maybe_share("今天又翻了点别的东西", NOW))
    assert [s for s, _ in sender.sent][-2:] == [
        "default:FriendMessage:111",
        "default:FriendMessage:333",
    ]


# ---------------------------------------------------------------------------
# Part B：GET 快照（验收 6）
# ---------------------------------------------------------------------------
def test_mood_snapshot_full_fields():
    mood = make_mood(interests={"咖啡": 0.5, "记忆宫殿": 0.3})
    snap = build_mood_snapshot(mood)
    assert set(snap) == {
        "energy", "fatigue", "valence", "arousal", "sleep_debt", "interests",
    }
    assert snap["interests"] == {"咖啡": 0.5, "记忆宫殿": 0.3}
    for key in ("energy", "fatigue", "valence", "arousal", "sleep_debt"):
        assert isinstance(snap[key], float)
    asyncio.run(mood.close())


def test_mood_snapshot_is_readonly():
    mood = make_mood(interests={"咖啡": 0.5})
    before = (mood.energy, mood.fatigue, mood.valence, mood.arousal, mood.sleep_debt)
    build_mood_snapshot(mood)
    after = (mood.energy, mood.fatigue, mood.valence, mood.arousal, mood.sleep_debt)
    assert before == after and mood.interests == {"咖啡": 0.5}
    asyncio.run(mood.close())


# ---------------------------------------------------------------------------
# Part B：POST set / delete / clear（验收 7/8）
# ---------------------------------------------------------------------------
def test_interest_set_new_and_overwrite():
    mood = make_mood(interests={"咖啡": 0.5})
    out = asyncio.run(apply_mood_interests(mood, {"action": "set", "topic": " 电路 ", "weight": 0.25}))
    assert out["interests"] == {"咖啡": 0.5, "电路": 0.25}  # topic 去空白
    out = asyncio.run(apply_mood_interests(mood, {"action": "set", "topic": "咖啡", "weight": 0.8}))
    assert out["interests"] == {"咖啡": 0.8, "电路": 0.25}  # 同名覆盖
    asyncio.run(mood.close())


def test_interest_set_weight_out_of_range_rejected():
    """weight 越界 → 400 语义（PanelApiError）且不部分写入。"""
    mood = make_mood(interests={"咖啡": 0.5})
    for bad in (1.5, -0.1, 2):
        with pytest.raises(PanelApiError, match=r"weight 需在"):
            asyncio.run(apply_mood_interests(mood, {"action": "set", "topic": "x", "weight": bad}))
    assert mood.interests == {"咖啡": 0.5}
    asyncio.run(mood.close())


def test_interest_set_weight_type_rejected():
    mood = make_mood()
    for bad in ("0.5", True, None, [0.5]):
        with pytest.raises(PanelApiError, match="weight 需要数字"):
            asyncio.run(apply_mood_interests(mood, {"action": "set", "topic": "x", "weight": bad}))
    assert mood.interests == {}
    asyncio.run(mood.close())


def test_interest_set_topic_validation():
    mood = make_mood()
    for topic in ("", "   ", None, 123):
        with pytest.raises(PanelApiError, match="topic 必须是非空字符串"):
            asyncio.run(apply_mood_interests(mood, {"action": "set", "topic": topic, "weight": 0.5}))
    assert mood.interests == {}
    asyncio.run(mood.close())


def test_interest_delete_existing_and_missing_idempotent():
    mood = make_mood(interests={"咖啡": 0.5, "电路": 0.2})
    out = asyncio.run(apply_mood_interests(mood, {"action": "delete", "topic": "咖啡"}))
    assert out["interests"] == {"电路": 0.2}
    # 不存在的 topic 幂等删除（结果状态正确即成功）
    out = asyncio.run(apply_mood_interests(mood, {"action": "delete", "topic": "不存在"}))
    assert out["interests"] == {"电路": 0.2}
    asyncio.run(mood.close())


def test_interest_clear_empties_all():
    mood = make_mood(interests={"咖啡": 0.5, "电路": 0.2, "记忆宫殿": 0.3})
    out = asyncio.run(apply_mood_interests(mood, {"action": "clear"}))
    assert out["interests"] == {} and mood.interests == {}
    asyncio.run(mood.close())


def test_unknown_action_rejected():
    mood = make_mood()
    with pytest.raises(PanelApiError, match="未知 action"):
        asyncio.run(apply_mood_interests(mood, {"action": "update", "topic": "x"}))
    with pytest.raises(PanelApiError, match="未知 action"):
        asyncio.run(apply_mood_interests(mood, {}))
    asyncio.run(mood.close())


def test_interest_write_hot_effect_same_instance_and_persisted():
    """验收 8（C2）：POST set 后同一实例 interests 立即变化（不只是 db 变），
    且 save 落库——新开实例 load 读到同值（重启不丢）。"""
    db = tempfile.mktemp(suffix=".db", dir=None)
    mood = MoodState(db_path=db, now_provider=lambda: NOW)
    asyncio.run(mood.load())
    asyncio.run(apply_mood_interests(mood, {"action": "set", "topic": "咖啡", "weight": 0.6}))
    # 同一实例内存态立即变化
    assert mood.interests == {"咖啡": 0.6}

    mood2 = MoodState(db_path=db, now_provider=lambda: NOW)
    asyncio.run(mood2.load())
    assert mood2.interests == {"咖啡": 0.6}  # 持久化验证
    asyncio.run(mood.close())
    asyncio.run(mood2.close())


def test_clear_keeps_recent_topics_and_decay_chain_works():
    """验收 9：清空只动 interests——recent_topics 是独立键不受影响；
    清空后 bump/decay 衰减链照常运转。"""
    mood = make_mood(interests={"咖啡": 0.5})
    mood.recent_topics = ["咖啡", "电路"]
    asyncio.run(mood.save())

    asyncio.run(apply_mood_interests(mood, {"action": "clear"}))
    assert mood.interests == {}
    assert mood.recent_topics == ["咖啡", "电路"]  # 独立键不受影响

    # 衰减链兼容：清空后 bump 照常累积（从 0 起步、饱和曲线）、decay 照常衰减
    mood.bump_interest("合成器", 0.15)
    assert mood.interests == {"合成器": pytest.approx(0.15)}
    mood.decay_interests(0.9)
    assert mood.interests["合成器"] == pytest.approx(0.135)
    asyncio.run(mood.close())


# ---------------------------------------------------------------------------
# 装配：合成包直调 main 的 mood handler（M5-补丁1 先例）
# ---------------------------------------------------------------------------
def _load_plugin_main():
    import importlib
    import sys
    import types

    pkg_name = "living_plugin_under_test"
    if pkg_name not in sys.modules:
        pkg = types.ModuleType(pkg_name)
        pkg.__path__ = [str(Path(__file__).resolve().parents[1])]
        sys.modules[pkg_name] = pkg
        import core as core_pkg

        sys.modules[f"{pkg_name}.core"] = core_pkg
        for name, mod in list(sys.modules.items()):
            if name == "core" or name.startswith("core."):
                sys.modules.setdefault(f"{pkg_name}.{name}", mod)
    return importlib.import_module(f"{pkg_name}.main")


def test_dashboard_routes_register_mood_endpoints_and_flow(tmp_path):
    main_module = _load_plugin_main()
    plugin = object.__new__(main_module.LivingPlugin)

    class Ctx:
        def __init__(self):
            self.routes = {}

        def register_web_api(self, route, handler, methods, desc):
            self.routes[(route, tuple(methods))] = handler

    plugin.context = Ctx()
    plugin.mood = make_mood(tmp_path, interests={"咖啡": 0.5})
    plugin._register_dashboard_routes()
    prefix = f"/{main_module.PLUGIN_NAME}"
    assert (f"{prefix}/mood", ("GET",)) in plugin.context.routes
    assert (f"{prefix}/mood/interests", ("POST",)) in plugin.context.routes

    # GET：五项状态 + interests
    got = asyncio.run(plugin.context.routes[(f"{prefix}/mood", ("GET",))]())
    assert got["status"] == "ok"
    assert set(got["data"]) == {
        "energy", "fatigue", "valence", "arousal", "sleep_debt", "interests",
    }
    assert got["data"]["interests"] == {"咖啡": 0.5}

    # POST set：写操作走真实 MoodState（热生效 + 落盘）
    async def flow(body):
        from astrbot.api import web as astrbot_web

        class FakeRequest:
            async def json(self, default=None):
                return body

        original = astrbot_web.request
        astrbot_web.request = FakeRequest()
        try:
            return await plugin.context.routes[(f"{prefix}/mood/interests", ("POST",))]()
        finally:
            astrbot_web.request = original

    ok = asyncio.run(flow({"action": "set", "topic": "电路", "weight": 0.4}))
    assert ok["status"] == "ok" and ok["data"]["interests"]["电路"] == 0.4
    assert plugin.mood.interests["电路"] == 0.4  # 同一实例热生效

    bad = asyncio.run(flow({"action": "set", "topic": "x", "weight": 9}))
    assert bad["status"] == "error" and "weight 需在" in bad["message"]
    assert "x" not in plugin.mood.interests  # 非法输入不部分写入

    asyncio.run(plugin.mood.close())
