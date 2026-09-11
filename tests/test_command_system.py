"""M3 补丁 III-B 测试：/living 命令体系 + 睡前回顾 topics（部分 A 差量）。"""

import asyncio
import types
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from core.living_loop import LivingLoop
from core.living_state import LivingGate
from core.mood import MoodState
from core.sleep import SleepManager

T0 = datetime(2026, 9, 9, 14, 0, 0)

BASE_CONFIG = {
    "decision": {"daily_impulse_limit": 3, "activity_probability": 1.0,
                 "impulse_check_interval_minutes": 45, "max_run_seconds": 300,
                 "decision_mode": "hybrid"},
    "capabilities": {"cooldown_between_activities_hours": 0.0},
    "sleep": {"sleep_window": "", "fatigue_rate_per_hour": 4.0,
              "dream_probability": 0.0, "awake_standby_minutes": 30,
              "wake_ack_message": "", "sleep_farewell_message": "",
              "sleep_mute_replies": True, "wake_source": "all",
              "sleep_debt_decay_per_day": 30.0},
    "output_gate": {"daily_message_limit": 10, "message_min_interval_minutes": 30,
                    "target_sessions": "", "quiet_hours": ""},
    "model": {"provider_id": "", "fallback_chain": []},
    "persona": {"life_extra": ""},
    "memory": {"backend": "simple"},
}

WORKDIR = Path(__file__).resolve().parents[1]


def _load_plugin_main():
    import importlib
    import sys
    import types

    pkg_name = "living_plugin_under_test"
    if pkg_name not in sys.modules:
        pkg = types.ModuleType(pkg_name)
        pkg.__path__ = [str(WORKDIR)]
        sys.modules[pkg_name] = pkg
        import core as core_pkg

        sys.modules[f"{pkg_name}.core"] = core_pkg
        for name, mod in list(sys.modules.items()):
            if name == "core" or name.startswith("core."):
                sys.modules.setdefault(f"{pkg_name}.{name}", mod)
    return importlib.import_module(f"{pkg_name}.main")


class FakeEvent:
    def __init__(self, message_str=""):
        self.message_str = message_str
        self.replies = []

    def get_sender_id(self):
        return "master"

    def plain_result(self, text):
        # AstrBot 命令 handler 用它组回复链，测试里直接记录文本
        self.replies.append(text)
        return text

    @property
    def unified_msg_origin(self):
        return "aiocqhttp:FriendMessage:master"


class RecordingMemory:
    def __init__(self, rows=None):
        self.added = []
        self.rows = rows or []
        self.search_calls = []

    async def add(self, content, importance=0.5, metadata=None, **kwargs):
        self.added.append({"content": content, "importance": importance,
                           "metadata": metadata})
        return len(self.added)

    async def search(self, query, k=5):
        self.search_calls.append((query, k))
        return self.rows[:k]

    async def close(self):
        pass


class ScriptedActivity:
    def __init__(self, name="surf", fail=False):
        self.name = name
        self.description = f"{name} 描述"
        self.fail = fail
        self.runs = 0
        self.last_ctx = None

    async def run(self, ctx):
        self.runs += 1
        self.last_ctx = ctx
        if self.fail:
            raise RuntimeError("fail")
        from core.activities import ActivityOutcome

        return ActivityOutcome(name=self.name, summary="s", memory_content="m")


class RecordingSender:
    def __init__(self):
        self.sent = []

    async def send(self, session, text):
        self.sent.append((session, text))
        return True


class FakeAgentLoop:
    def __init__(self):
        self.last_result = None

    async def run(self, intent):
        return None


def build_plugin(tmp_path, config=None, memory_rows=None):
    """真实组件装配的插件（绕过 Star.__init__），命令测试用。"""
    main_module = _load_plugin_main()
    plugin = object.__new__(main_module.LivingPlugin)
    config = dict(BASE_CONFIG if config is None else config)
    plugin.config = config
    plugin.context = types.SimpleNamespace()  # 仅 debug 的 provider 链用到（可失败）
    plugin.memory_note = "测试后端"

    plugin.gate = main_module.LivingGate(
        config_getter=lambda: plugin.config,
        db_path=str(tmp_path / "gate.db"),
        rng=lambda: 0.0,
    )
    plugin.mood = main_module.MoodState(db_path=str(tmp_path / "mood.db"))
    asyncio.run(plugin.mood.load())

    memory = RecordingMemory(rows=memory_rows)
    plugin._lazy_memory = main_module.LazyMemory(
        context=None, mode_getter=lambda: "simple",
        db_path_getter=lambda: str(tmp_path / "mem.db"),
    )
    # 直接劫持懒加载：让 _get_memory 返回可记录的假后端
    plugin._memory_backend = memory

    async def memory_getter():
        return memory

    gate = plugin.gate
    sender = RecordingSender()
    activity = ScriptedActivity("surf")
    read_activity = ScriptedActivity("read")
    loop = LivingLoop(
        gate=gate,
        memory_getter=memory_getter,
        config_getter=lambda: plugin.config,
        abilities={},
        activities=[activity, read_activity],
        sender=sender,
        mood=plugin.mood,
        sleep_manager=main_module.SleepManager(
            config_getter=lambda: plugin.config, gate=gate, mood=plugin.mood,
        ),
    )
    plugin._agent_loop = FakeAgentLoop()
    loop._agent_loop = plugin._agent_loop

    async def memory_getter_override():
        return memory

    plugin._get_memory = memory_getter_override  # 实例属性遮蔽类方法
    loop._get_memory = memory_getter_override
    plugin.loop = loop
    plugin.sleep_manager = loop._sleep_manager
    plugin.sender = sender
    plugin.searcher = types.SimpleNamespace(close=lambda: asyncio.sleep(0))
    plugin.fetcher = types.SimpleNamespace(close=lambda: asyncio.sleep(0))
    plugin.sandbox = types.SimpleNamespace()
    plugin._memory_db_path = lambda: str(tmp_path / "mem.db")
    plugin._gate_db_path = lambda: str(tmp_path / "gate.db")
    plugin._mood_db_path = lambda: str(tmp_path / "mood.db")
    return plugin, memory, activity, read_activity, sender


async def run_cmd(plugin, message_str):
    event = FakeEvent(message_str=message_str)
    lines = [line async for line in plugin.living(event)]
    return lines, event


# ---------------------------------------------------------------------------
# 根命令与状态类
# ---------------------------------------------------------------------------
def test_root_living_shows_summary(tmp_path):
    plugin, memory, _act, _read, _sender = build_plugin(tmp_path)
    lines = asyncio.run(run_cmd(plugin, "/living"))[0]
    assert any("状态一览" in line for line in lines)
    assert any("主循环" in line for line in lines)
    assert any("今日活动" in line for line in lines)
    assert memory.search_calls, "根状态应读取最近记忆"


def test_status_shows_details(tmp_path):
    plugin, _memory, _act, _read, _sender = build_plugin(tmp_path)
    plugin.mood.sleep_debt = 40.0
    asyncio.run(gate_refresh(plugin, 30))
    lines = asyncio.run(run_cmd(plugin, "/living status"))[0]
    text = "\n".join(lines)
    assert "valence=" in text
    assert "睡眠债" in text
    assert "清醒待机：生效中" in text
    assert "休眠窗" in text


def test_mood_shows_dimensions_and_interests(tmp_path):
    plugin, _memory, _act, _read, _sender = build_plugin(tmp_path)
    plugin.mood.bump_interest("咖啡", 0.8)
    lines = asyncio.run(run_cmd(plugin, "/living mood"))[0]
    text = "\n".join(lines)
    assert "心境详情" in text
    assert "valence=" in text
    assert "咖啡" in text


def test_memories_default_five_and_custom_n(tmp_path):
    rows = [{"content": f"记忆{i}", "score": 0} for i in range(7)]
    plugin, memory, _act, _read, _sender = build_plugin(tmp_path, memory_rows=rows)

    lines = asyncio.run(run_cmd(plugin, "/living memories"))[0]
    assert any("最近 5 条" in line for line in lines)
    items = [line for line in lines if line.startswith("- ")]
    assert len(items) == 5
    assert memory.search_calls[-1] == ("", 5)

    asyncio.run(run_cmd(plugin, "/living memories 2"))
    assert memory.search_calls[-1] == ("", 2)


def test_memories_empty_db(tmp_path):
    plugin, _memory, _act, _read, _sender = build_plugin(tmp_path)
    lines = asyncio.run(run_cmd(plugin, "/living memories"))[0]
    assert any("还没有任何记忆" in line for line in lines)


# ---------------------------------------------------------------------------
# pause / resume
# ---------------------------------------------------------------------------
def test_pause_resume_via_command(tmp_path):
    plugin, _memory, _act, _read, _sender = build_plugin(tmp_path)
    loop = plugin.loop

    lines = asyncio.run(run_cmd(plugin, "/living pause"))[0]
    assert loop.paused is True
    assert any("已暂停" in line for line in lines)
    # 幂等
    lines = asyncio.run(run_cmd(plugin, "/living pause"))[0]
    assert any("已经在暂停" in line for line in lines)

    lines = asyncio.run(run_cmd(plugin, "/living resume"))[0]
    assert loop.paused is False
    assert any("已恢复" in line for line in lines)


def test_paused_loop_skips_heartbeat(tmp_path):
    """pause 后心跳不触发活动/判定（定时器继续跑，resume 即恢复）。"""
    plugin, _memory, activity, _read, _sender = build_plugin(tmp_path)
    loop = plugin.loop
    gate = plugin.gate

    blocking = asyncio.Event()
    loop._sleep = blocking.wait  # 挂住等待（旧钩子不再被 _run 使用也不碍事）

    calls = []
    original_should_wake = gate.should_wake

    async def counting(now=None, force=False):
        calls.append((now, force))
        return await original_should_wake(now, force=force)

    gate.should_wake = counting  # 实例属性遮蔽：统计真实判定调用

    async def flow():
        await loop.start()
        await asyncio.sleep(0.05)
        await loop.pause()
        loop.request_wake()
        await asyncio.sleep(0.15)
        judged_while_paused = len(calls)
        runs_while_paused = activity.runs
        await loop.resume()
        loop.request_wake()
        await asyncio.sleep(0.15)
        judged_after = len(calls)
        await loop.stop()
        return judged_while_paused, runs_while_paused, judged_after

    judged_while_paused, runs_while_paused, judged_after = asyncio.run(flow())
    assert judged_while_paused == 0
    assert runs_while_paused == 0
    assert judged_after >= 1  # resume 后恢复判定


# ---------------------------------------------------------------------------
# wake / sleep
# ---------------------------------------------------------------------------
def test_wake_subcommand_forces(tmp_path):
    plugin, _memory, _act, _read, _sender = build_plugin(tmp_path)
    loop = plugin.loop
    calls = []
    original = loop.heartbeat_once_detailed

    async def spy(now=None, force=False):
        calls.append(force)
        return True, "ok", "surf"

    loop.heartbeat_once_detailed = spy
    lines = asyncio.run(run_cmd(plugin, "/living wake"))[0]
    assert calls == [True]
    assert any("醒了" in line for line in lines)


def test_sleep_clears_standby_and_sends_farewell(tmp_path):
    config = {
        **BASE_CONFIG,
        "sleep": {**BASE_CONFIG["sleep"],
                  "sleep_farewell_message": "我先睡了，晚安。"},
    }
    plugin, _memory, _act, _read, sender = build_plugin(tmp_path, config=config)
    gate = plugin.gate
    asyncio.run(gate.refresh_awake_until(30))  # 手动进入待机（真实当前时刻）
    plugin.sleep_manager.last_active_session = "aiocqhttp:GroupMessage:42"

    lines = asyncio.run(run_cmd(plugin, "/living sleep"))[0]
    assert any("已清除" in line for line in lines)
    assert gate.awake_standby_active() is False  # 待机已清除
    assert sender.sent == [("aiocqhttp:GroupMessage:42", "我先睡了，晚安。")]


# ---------------------------------------------------------------------------
# /living do
# ---------------------------------------------------------------------------
def test_do_runs_named_activity_with_topic(tmp_path):
    plugin, _memory, activity, read_activity, _sender = build_plugin(tmp_path)

    lines = asyncio.run(run_cmd(plugin, "/living do surf 深海生物"))[0]
    assert any("这就去surf" in line for line in lines)
    assert activity.runs == 1
    assert read_activity.runs == 0
    assert activity.last_ctx.params == {"topic": "深海生物"}


def test_do_unknown_activity_shows_options(tmp_path):
    plugin, _memory, _act, _read, _sender = build_plugin(tmp_path)
    lines = asyncio.run(run_cmd(plugin, "/living do 跳伞"))[0]
    assert any("未知活动" in line for line in lines)
    assert any("surf" in line for line in lines)  # 提示可选活动


def test_do_without_activity_shows_usage(tmp_path):
    plugin, _memory, _act, _read, _sender = build_plugin(tmp_path)
    lines = asyncio.run(run_cmd(plugin, "/living do"))[0]
    assert any("用法" in line for line in lines)


def test_do_respects_daily_limit(tmp_path):
    """红线：/living do 不能绕过每日上限——满了提示且不执行。"""
    plugin, memory, activity, _read, _sender = build_plugin(tmp_path)
    gate = plugin.gate

    async def fill():
        # 用真实当前时间：固定 T0 会在 get_state 里触发跨日清零
        now = datetime.now()
        for _ in range(3):
            await gate.note_activity_started(now)

    asyncio.run(fill())
    before = len(memory.added)

    lines = asyncio.run(run_cmd(plugin, "/living do surf"))[0]
    assert any("上限" in line for line in lines)
    assert activity.runs == 0
    assert len(memory.added) == before  # 没有写活动记忆


# ---------------------------------------------------------------------------
# /living config
# ---------------------------------------------------------------------------
def test_config_hot_effect(tmp_path):
    """config 改完即热生效：gate 下一次判定读到新值。"""
    plugin, _memory, _act, _read, _sender = build_plugin(tmp_path)

    lines = asyncio.run(
        run_cmd(plugin, "/living config decision.daily_impulse_limit 10")
    )[0]
    assert any("已设置" in line for line in lines)
    assert plugin.config["decision"]["daily_impulse_limit"] == 10  # int 转换
    reached, count, limit = asyncio.run(plugin.gate.daily_limit_info(T0))
    assert limit == 10  # 热生效：gate 读到了新值


def test_config_bool_conversion(tmp_path):
    plugin, _memory, _act, _read, _sender = build_plugin(tmp_path)
    lines = asyncio.run(
        run_cmd(plugin, "/living config sleep.sleep_mute_replies false")
    )[0]
    assert plugin.config["sleep"]["sleep_mute_replies"] is False


def test_config_rejects_unknown_group(tmp_path):
    plugin, _memory, _act, _read, _sender = build_plugin(tmp_path)
    lines = asyncio.run(
        run_cmd(plugin, "/living config evil.key 1")
    )[0]
    assert any("不允许" in line for line in lines)
    assert "evil" not in plugin.config


def test_config_missing_value_shows_usage(tmp_path):
    plugin, _memory, _act, _read, _sender = build_plugin(tmp_path)
    lines = asyncio.run(run_cmd(plugin, "/living config decision.daily_impulse_limit"))[0]
    assert any("用法" in line for line in lines)


# ---------------------------------------------------------------------------
# debug / 未知子命令
# ---------------------------------------------------------------------------
def test_debug_shows_chain_and_provider(tmp_path):
    plugin, _memory, _act, _read, _sender = build_plugin(tmp_path)
    plugin._agent_loop.last_result = AgentResultLike(tokens=1234, steps=3, max_steps=8)

    lines = asyncio.run(run_cmd(plugin, "/living debug"))[0]
    text = "\n".join(lines)
    assert "闸门判定链" in text
    assert "清醒待机" in text and "每日上限" in text
    assert "决策模式：hybrid" in text
    assert "provider 链" in text
    assert "tokens=1234" in text


class AgentResultLike:
    def __init__(self, tokens, steps, max_steps):
        self.tokens_used = tokens
        self.steps_used = steps
        self.max_steps = max_steps
        self.budget_exceeded = False


def test_unknown_subcommand_shows_help(tmp_path):
    plugin, _memory, _act, _read, _sender = build_plugin(tmp_path)
    lines = asyncio.run(run_cmd(plugin, "/living 跳舞"))[0]
    assert any("未知子命令" in line for line in lines)
    assert any("可用子命令" in line for line in lines)


# ---------------------------------------------------------------------------
# 部分 A 差量：睡前回顾带 topics
# ---------------------------------------------------------------------------
def test_bedtime_review_metadata_has_topics(tmp_path):
    """睡前回顾的记忆 metadata 应带 topics=["睡前回顾"]（图谱原料）。"""
    memory = RecordingMemory()

    class InWindowGate:
        def in_sleep_window(self, now=None):
            return True

        def awake_standby_active(self, now=None):
            return False  # 不在待机：首次心跳判"入睡"并写回顾

        async def consume_standby_expiry(self, now=None):
            return False

        async def should_wake(self, now=None, force=False):
            return False, "sleeping"

        async def should_send_message(self, now=None):
            return False, "blocked"

        async def note_activity_started(self, now=None):
            pass

        async def note_activity_finished(self, now=None):
            pass

        async def close(self):
            pass

    gate = InWindowGate()
    loop = LivingLoop(
        gate=gate,
        memory_getter=lambda: asyncio.sleep(0, result=memory),
        config_getter=lambda: BASE_CONFIG,
        activities=[],
    )
    asyncio.run(loop.heartbeat_once_detailed(T0))
    assert memory.added, "入睡应写睡前回顾"
    # 补丁 III：回顾记忆的 metadata 携带 topics=["睡前回顾"]
    reviews = [
        c for c in memory.added
        if c["metadata"] == {"topics": ["睡前回顾"]}
    ]
    assert reviews, "睡前回顾应携带 topics 状态"


# ---- 工具 ----
async def gate_refresh(plugin, minutes):
    # 用真实当前时间：awake_standby_active 默认比较 datetime.now()，
    # 固定的 T0 会立刻"过期"
    await plugin.gate.refresh_awake_until(minutes)
