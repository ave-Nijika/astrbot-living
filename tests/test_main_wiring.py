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
from core.sleep import SleepManager

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
    plugin.mood = main_module.MoodState(db_path=str(db_path) + ".mood")
    # 屏蔽真实插件数据目录（不写 AstrBot 的 data/）与网络能力
    plugin._gate_db_path = lambda: str(db_path) + ".gate"
    plugin._memory_db_path = lambda: str(db_path)
    plugin._mood_db_path = lambda: str(db_path) + ".mood"
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


# ---------------------------------------------------------------------------
# M2-C/E：persona 读取与决策 LLM 接线
# ---------------------------------------------------------------------------
def test_persona_prompt_read_success(tmp_path):
    """persona_manager 正常时返回 Personality['prompt']（TypedDict）。"""

    async def flow():
        plugin = make_plugin(tmp_path / "m.db")

        class FakePersonaManager:
            async def get_default_persona_v3(self, umo=None):
                return {"prompt": "你是一只住在机器人里的猫。", "name": "猫"}

        plugin.context = types.SimpleNamespace(persona_manager=FakePersonaManager())
        return await plugin._persona_prompt()

    assert asyncio.run(flow()) == "你是一只住在机器人里的猫。"


def test_persona_prompt_read_failure_silent(tmp_path):
    """persona 缺失/异常/空 prompt：一律静默返回 None（决策仍可用）。"""

    class BrokenManager:
        async def get_default_persona_v3(self, umo=None):
            raise RuntimeError("gone")

    class EmptyPersonaManager:
        async def get_default_persona_v3(self, umo=None):
            return {"prompt": "  ", "name": "空"}

    async def flow_broken():
        plugin = make_plugin(tmp_path / "m1.db")
        plugin.context = types.SimpleNamespace(persona_manager=BrokenManager())
        return await plugin._persona_prompt()

    async def flow_missing():
        plugin = make_plugin(tmp_path / "m2.db")
        plugin.context = types.SimpleNamespace()  # 没有 persona_manager
        return await plugin._persona_prompt()

    async def flow_empty():
        plugin = make_plugin(tmp_path / "m3.db")
        plugin.context = types.SimpleNamespace(persona_manager=EmptyPersonaManager())
        return await plugin._persona_prompt()

    assert asyncio.run(flow_broken()) is None
    assert asyncio.run(flow_missing()) is None
    assert asyncio.run(flow_empty()) is None


def test_decision_llm_call_uses_configured_provider(tmp_path):
    """model.provider_id 配置优先，且把 completion_text 透出。"""
    captured = {}

    async def flow():
        plugin = make_plugin(tmp_path / "m.db")
        plugin.config = {"model": {"provider_id": "my-decision-llm"}}

        class FakeContext:
            async def llm_generate(self, *, chat_provider_id, prompt,
                                   system_prompt=None, **kw):
                captured["provider"] = chat_provider_id
                captured["prompt"] = prompt
                captured["system"] = system_prompt
                return types.SimpleNamespace(
                    completion_text='{"topic": "x"}', result_chain=None
                )

        plugin.context = FakeContext()
        return await plugin._decision_llm_call("提示词", "系统提示")

    text = asyncio.run(flow())
    assert text == '{"topic": "x"}'
    assert captured["provider"] == "my-decision-llm"
    assert captured["prompt"] == "提示词"
    assert captured["system"] == "系统提示"


def test_decision_llm_call_failure_returns_none(tmp_path):
    """LLM 调用任何失败 → None → decider 静默回退（默认配置能跑的底线）。"""

    async def flow():
        plugin = make_plugin(tmp_path / "m.db")

        class BoomContext:
            async def llm_generate(self, **kw):
                raise RuntimeError("no provider")

        plugin.context = BoomContext()
        # provider_id 留空 + get_current_chat_provider_id 也挂 → None
        return await plugin._decision_llm_call("p", None)

    assert asyncio.run(flow()) is None


def test_initialize_wires_mood_and_decider(tmp_path):
    """M2 接线：loop 持有 mood 与 decider；terminate 清理 mood。"""

    async def flow():
        plugin = make_plugin(tmp_path / "m.db")
        await plugin.initialize()
        try:
            assert plugin.loop._mood is plugin.mood
            assert plugin.loop._decider is not None
        finally:
            await plugin.terminate()
        assert plugin.loop is None

    asyncio.run(flow())


# ---------------------------------------------------------------------------
# M3：/living_wake 命令 与 消息监听（吵醒计数 / 静默拦截）
# ---------------------------------------------------------------------------
def _living_gate():
    """一个"当前时刻在休眠窗内"的闸门：窗口按真实时间动态生成。"""
    from datetime import datetime, timedelta

    from core.living_state import LivingGate

    now = datetime.now()
    start = (now - timedelta(minutes=30)).strftime("%H:%M")
    end = (now + timedelta(minutes=30)).strftime("%H:%M")
    config = {
        "decision": {"daily_impulse_limit": 3, "activity_probability": 0.8},
        "capabilities": {"cooldown_between_activities_hours": 2.0},
        "sleep": {
            "sleep_window": f"{start}-{end}",
            "wake_n_messages": 3,
            "wake_window_minutes": 10,
            "grouchiness_percent": 20,
            "wake_source": "all",
            "sleep_mute_replies": True,
        },
        "output_gate": {"daily_message_limit": 10, "message_min_interval_minutes": 30},
    }
    return LivingGate(config_getter=lambda: config, db_path=":memory:", rng=lambda: 0.5)


class FakeEvent:
    def __init__(self, message_str="", sender_id="u1"):
        self.message_str = message_str
        self._sender_id = sender_id
        self.replies = []
        self.stopped = False

    def plain_result(self, text):
        self.replies.append(text)
        return text

    def get_sender_id(self):
        return self._sender_id

    def stop_event(self):
        self.stopped = True


class FakeLoop:
    def __init__(self, awake=True, reason="ok", activity="surf"):
        self.awake = awake
        self.reason = reason
        self.activity = activity
        self.kwargs_seen = None
        self.woke_requested = False

    async def heartbeat_once_detailed(self, now=None, force=False):
        self.kwargs_seen = {"force": force}
        return self.awake, self.reason, self.activity

    def request_wake(self):
        self.woke_requested = True


def test_living_wake_command_reports_awake(tmp_path):
    async def flow():
        plugin = make_plugin(tmp_path / "m.db")
        plugin.loop = FakeLoop(awake=True, activity="surf")
        event = FakeEvent()
        results = [r async for r in plugin.living_wake(event)]
        return results, plugin.loop

    results, loop = asyncio.run(flow())
    assert results[0] == "收到，判定中…"
    assert any("surf" in r for r in results)
    assert loop.kwargs_seen == {"force": True}


def test_living_wake_command_reports_blocked_reason(tmp_path):
    async def flow():
        plugin = make_plugin(tmp_path / "m.db")
        plugin.loop = FakeLoop(awake=False, reason="cooldown")
        event = FakeEvent()
        return [r async for r in plugin.living_wake(event)]

    results = asyncio.run(flow())
    assert any("冷却" in r for r in results)


def test_on_any_message_mutes_during_sleep(tmp_path):
    """睡眠窗内的普通消息：计数、静默拦截（stop_event）。"""

    async def flow():
        plugin = make_plugin(tmp_path / "m.db")
        plugin.sleep_manager = SleepManager(
            config_getter=lambda: plugin.config, gate=_living_gate(), mood=None,
        )
        event = FakeEvent(message_str="有人说话")
        await plugin.on_any_message(event)
        return event

    assert asyncio.run(flow()).stopped is True


def test_on_any_message_command_not_muted(tmp_path):
    """本插件命令不拦（任务书 B4 例外）。"""

    async def flow():
        plugin = make_plugin(tmp_path / "m.db")
        plugin.sleep_manager = SleepManager(
            config_getter=lambda: plugin.config, gate=_living_gate(), mood=None,
        )
        event = FakeEvent(message_str="/living_wake")
        await plugin.on_any_message(event)
        return event

    assert asyncio.run(flow()).stopped is False


def test_on_any_message_wake_threshold_requests_wake_not_muted(tmp_path):
    """连续 3 条消息达到吵醒阈值：第 3 条不拦 + 请求主循环唤醒。"""

    async def flow():
        plugin = make_plugin(tmp_path / "m.db")
        plugin.sleep_manager = SleepManager(
            config_getter=lambda: plugin.config, gate=_living_gate(), mood=None,
        )
        loop = FakeLoop()
        plugin.loop = loop
        events = []
        for i in range(3):
            event = FakeEvent(message_str=f"第{i}条")
            await plugin.on_any_message(event)
            events.append(event)
        return loop, events

    loop, events = asyncio.run(flow())
    assert loop.woke_requested is True  # 第 3 条触发吵醒 → 请求唤醒
    assert [e.stopped for e in events] == [True, True, False]  # 触发那条不拦
