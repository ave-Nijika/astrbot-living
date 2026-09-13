"""M3 补丁 V 测试：图谱身份统一 + 决策兜底链 + config 嵌套键。"""

import asyncio
import types
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from core.living_loop import LivingLoop
from core.living_state import LivingGate
from core.memory_backend import LivingMemoryBackend, SimpleBackend

T0 = datetime(2026, 9, 12, 14, 0, 0)

BASE_CONFIG = {
    "decision": {"daily_impulse_limit": 3, "activity_probability": 1.0,
                 "impulse_check_interval_minutes": 5, "max_run_seconds": 300},
    "capabilities": {"cooldown_between_activities_hours": 0.0},
    "sleep": {"sleep_window": "", "fatigue_rate_per_hour": 4.0,
              "dream_probability": 0.0},
    "output_gate": {"daily_message_limit": 10, "message_min_interval_minutes": 30,
                    "target_sessions": "", "quiet_hours": ""},
    "model": {"provider_id": "", "fallback_chain": []},
}

WORKDIR = Path(__file__).resolve().parents[1]


def _load_main():
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


# ---------------------------------------------------------------------------
# 问题 1：bot 身份统一（cron:{dashboard_username} 格式）
# ---------------------------------------------------------------------------
class FakeContext:
    """可配置 get_config 的最小上下文。"""

    def __init__(self, cfg=None):
        self._cfg = cfg or {}

    def get_config(self):
        return self._cfg


class FakeHybridResult:
    """模拟 LivingMemory HybridResult（backend.search 用 getattr 读字段）。"""

    def __init__(self, doc_id, content, metadata, final_score=1.0):
        self.doc_id = doc_id
        self.content = content
        self.metadata = metadata
        self.final_score = final_score


class FakeEngine:
    """LivingMemory 引擎替身：search 返回带原生 participant_identities 的行。"""

    def __init__(self, rows=None):
        self._rows = rows or []
        self.searched = []

    async def add_memory(self, content, **kwargs):
        return 1

    async def search_memories(self, query, k=5, session_id=None, **kwargs):
        self.searched.append(query)
        results = []
        for i, row in enumerate(self._rows[:k]):
            results.append(
                FakeHybridResult(
                    doc_id=i + 1,
                    content=row["content"],
                    metadata=row.get("metadata", {}),
                )
            )
        return results


def make_identity_plugin(engine=None, context=None, tmp_path=None):
    main_module = _load_main()
    plugin = object.__new__(main_module.LivingPlugin)
    plugin.config = dict(BASE_CONFIG)
    plugin.context = context if context is not None else FakeContext()
    plugin._bot_identity_cache = None
    plugin.loop = None
    plugin.gate = None

    # _get_memory 劫持：engine 非 None 时返回真 LivingMemoryBackend 包装
    if engine is not None:
        backend = LivingMemoryBackend(engine)
    else:
        backend = SimpleBackend(str((tmp_path or Path(".")) / "mem.db"))

    async def memory_getter():
        return backend

    plugin._get_memory = memory_getter
    return plugin


def test_bot_identity_fallback_cron_format():
    """无 dashboard 配置 → 兜底 identity_key = cron:astrbot，display_name=astrbot。"""
    plugin = make_identity_plugin()
    identity = asyncio.run(plugin._bot_identity())
    assert identity["identity_key"] == "cron:astrbot"
    assert identity["platform"] == "cron"
    assert identity["display_name"] == "astrbot"  # 不是 persona 名（凛）
    assert identity["is_bot"] is True
    assert identity["sender_id"] == "astrbot"


def test_bot_identity_dashboard_username_dynamic():
    """dashboard.username 动态取：换管理账号身份跟着变。"""
    ctx = FakeContext({"dashboard": {"username": "neo"}})
    plugin = make_identity_plugin(context=ctx)
    identity = asyncio.run(plugin._bot_identity())
    assert identity["identity_key"] == "cron:neo"
    assert identity["display_name"] == "neo"


def test_bot_identity_prefers_native_memory_identity(tmp_path):
    """更稳妥路径：原生记忆里 is_bot 的身份是权威形态，优先采用并缓存。"""
    native_participant = {
        "identity_key": "cron:astrbot",
        "sender_id": "astrbot",
        "platform": "cron",
        "display_name": "astrbot",
        "is_bot": True,
    }
    engine = FakeEngine(rows=[
        {"content": "主人让我查天气", "score": 1.0,
         "metadata": {"participant_identities": [native_participant]}},
    ])
    plugin = make_identity_plugin(engine=engine, tmp_path=tmp_path)

    identity = asyncio.run(plugin._bot_identity())
    assert identity["identity_key"] == "cron:astrbot"

    # 缓存生效：第二次调用不再查询引擎
    asyncio.run(plugin._bot_identity())
    assert engine.searched == ["我"]


def test_bot_identity_native_rows_without_bot_skipped():
    """原生记忆行里只有人类参与者：跳过，不误用人类身份当 bot。"""
    human_only = {
        "identity_key": "qq:master001",
        "sender_id": "master001",
        "platform": "qq",
        "is_bot": False,
    }
    engine = FakeEngine(rows=[
        {"content": "主人说话", "score": 1.0,
         "metadata": {"participant_identities": [human_only]}},
    ])
    plugin = make_identity_plugin(engine=engine)

    identity = asyncio.run(plugin._bot_identity())
    assert identity["identity_key"] == "cron:astrbot"  # 走兜底，不用人类身份


# ---------------------------------------------------------------------------
# 问题 4：metadata 三件套（topics + key_facts + participant_identities）
# ---------------------------------------------------------------------------
class OkGate:
    async def should_wake(self, now=None, force=False):
        return True, "ok"

    def in_sleep_window(self, now=None):
        return False

    def awake_standby_active(self, now=None):
        return False

    async def consume_standby_expiry(self, now=None):
        return False

    async def daily_limit_info(self, now=None):
        return False, 0, 3

    async def should_send_message(self, now=None):
        return False, "blocked"

    async def note_activity_started(self, now=None):
        pass

    async def note_activity_finished(self, now=None):
        pass

    async def close(self):
        pass


class RecordingMemory:
    def __init__(self):
        self.calls = []

    async def add(self, content, importance=0.5, metadata=None, **kwargs):
        self.calls.append({"content": content, "importance": importance,
                           "metadata": metadata})
        return len(self.calls)

    async def search(self, query, k=5):
        return []

    async def close(self):
        pass


class ScriptedActivity:
    def __init__(self, name="surf", outcome=None):
        self.name = name
        self.outcome = outcome

    async def run(self, ctx):
        from core.activities import ActivityOutcome

        if self.outcome is not None:
            return self.outcome
        return ActivityOutcome(
            name=self.name, summary="s", memory_content="m",
            topics=["测试主题"],
        )


def test_metadata_has_all_three_fields(tmp_path):
    """问题 4 验收：metadata 同时含 topics + key_facts + participant_identities。"""
    from core.activities import ActivityOutcome

    memory = RecordingMemory()
    plugin = make_identity_plugin(tmp_path=tmp_path)
    identity = asyncio.run(plugin._bot_identity())
    loop = LivingLoop(
        gate=OkGate(),
        memory_getter=lambda: asyncio.sleep(0, result=memory),
        config_getter=lambda: BASE_CONFIG,
        activities=[ScriptedActivity()],
        bot_identity_getter=lambda: asyncio.sleep(0, result=identity),
        persona_id_getter=lambda: asyncio.sleep(0, result="default"),
    )
    loop._get_memory = lambda: asyncio.sleep(0, result=memory)

    asyncio.run(loop.run_activity_cycle(T0))
    metadata = memory.calls[0]["metadata"]
    assert metadata["topics"]  # 脚本模式随机主题
    assert metadata["key_facts"] == ["m"]  # content 成为独立 fact 节点
    assert metadata["participant_identities"][0]["identity_key"] == "cron:astrbot"


def test_failure_metadata_keeps_identity_but_no_topics(tmp_path):
    """失败路径：无 topics/key_facts（低价值孤立可接受），参与者身份仍在。"""
    memory = RecordingMemory()
    plugin = make_identity_plugin(tmp_path=tmp_path)
    identity = asyncio.run(plugin._bot_identity())

    class Failing:
        name = "surf"

        async def run(self, ctx):
            raise RuntimeError("挂了")

    loop = LivingLoop(
        gate=OkGate(),
        memory_getter=lambda: asyncio.sleep(0, result=memory),
        config_getter=lambda: BASE_CONFIG,
        activities=[Failing()],
        bot_identity_getter=lambda: asyncio.sleep(0, result=identity),
    )
    loop._get_memory = lambda: asyncio.sleep(0, result=memory)

    asyncio.run(loop.run_activity_cycle(T0))
    metadata = memory.calls[0]["metadata"]
    assert "topics" not in metadata
    assert "key_facts" not in metadata
    assert metadata["participant_identities"][0]["is_bot"] is True


# ---------------------------------------------------------------------------
# 问题 2：决策 LLM 兜底链（审计清单 #9 补测）
# ---------------------------------------------------------------------------
def test_decision_llm_falls_back_to_result_chain(tmp_path):
    """completion_text 缺失时从 result_chain[0].text 取。"""

    async def flow():
        plugin = _make_llm_plugin(tmp_path)
        chain = [("a", object())]

        class Ctx:
            provider_manager = types.SimpleNamespace(
                get_provider_by_id=staticmethod(
                    async_stub(lambda pid: SimpleProvider(pid))
                )
            )
            get_all_providers = staticmethod(
                lambda: [SimpleProvider(pid) for pid in ("a",)]
            )

            @staticmethod
            async def llm_generate(**kw):
                return types.SimpleNamespace(
                    completion_text=None,
                    result_chain=types.SimpleNamespace(
                        chain=[types.SimpleNamespace(text="从链里取到的")]
                    ),
                )

        plugin.context = Ctx()
        return await plugin._decision_llm_call("提示", None)

    assert asyncio.run(flow()) == "从链里取到的"


class SimpleProvider:
    def __init__(self, pid):
        self._pid = pid
        self.text_chat = object()

    def meta(self):
        return types.SimpleNamespace(id=self._pid)


def async_stub(fn):
    async def wrapper(*a, **kw):
        return fn(*a, **kw)
    return wrapper


def _make_llm_plugin(tmp_path):
    plugin = _make_llm_plugin_inner(tmp_path)
    return plugin


def _make_llm_plugin_inner(tmp_path):
    main_module = _load_main()
    plugin = object.__new__(main_module.LivingPlugin)
    plugin.config = dict(BASE_CONFIG)
    plugin.context = None
    return plugin


def test_decision_llm_no_text_anywhere_returns_none(tmp_path):
    """completion_text 与 result_chain 都没有 → None（决策层静默回退）。"""

    async def flow():
        plugin = _make_llm_plugin(tmp_path)

        class Ctx:
            provider_manager = types.SimpleNamespace(
                get_provider_by_id=staticmethod(
                    async_stub(lambda pid: SimpleProvider(pid))
                )
            )
            get_all_providers = staticmethod(
                lambda: [SimpleProvider("a")]
            )

            @staticmethod
            async def llm_generate(**kw):
                return types.SimpleNamespace(
                    completion_text=None, result_chain=None
                )

        plugin.context = Ctx()
        return await plugin._decision_llm_call("提示", None)

    assert asyncio.run(flow()) is None


# ---------------------------------------------------------------------------
# 问题 3：/living config 嵌套键
# ---------------------------------------------------------------------------
def test_config_group_prefix_and_bare_key(tmp_path):
    """组前缀形式直接写组；裸键在允许组内唯一匹配时也能写。"""
    from test_command_system import build_plugin, run_cmd

    plugin, _memory, _act, _read, _sender = build_plugin(tmp_path)

    # 组前缀（任务书点名支持的形式）
    lines = asyncio.run(
        run_cmd(plugin, "/living config decision.daily_impulse_limit 10")
    )[0]
    assert any("已设置" in line for line in lines)
    assert plugin.config["decision"]["daily_impulse_limit"] == 10

    # 裸键（用户习惯形式）：decision 组内唯一 → 写对地方
    lines = asyncio.run(
        run_cmd(plugin, "/living config impulse_check_interval_minutes 7")
    )[0]
    assert plugin.config["decision"]["impulse_check_interval_minutes"] == 7


def test_config_missing_group_reports_clearly(tmp_path):
    """组不存在/键不存在时给出可操作的错误（而不是静默失败）。"""
    from test_command_system import build_plugin, run_cmd

    plugin, _memory, _act, _read, _sender = build_plugin(tmp_path)
    lines = asyncio.run(run_cmd(plugin, "/living config nosuch.key 1"))[0]
    assert any("不允许" in line for line in lines)
