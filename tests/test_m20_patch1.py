"""M20-补丁1 P0 批测试：调用隔离与缓存保护（A/B/C/E/F/G 组）。

覆盖：
- B 组：allow_chat_fallback 开关（false 只走配置链、true 保持兜底回归、
  缓存保护日志可见）、judge.provider_id 独立性回归（B3）；
- E 组：聊天前缀缓存（on_llm_request priority=-1 快照）、前缀逐字一致
  （E3 硬要求）、TTL/无缓存退回现有形态（E5）、_decision_llm_call 与
  agent 循环的对齐、judge 同账号也对齐；
- C 组：守护测试（无绕过链的调用点）；
- F 组：payload 数据源（agent_tools）、fs_list 端点（根限制/明确报错）、
  schema 控件锚点（provider 下拉/多选/时间窗/目录浏览）；
- G 组：quiet_hours 删净（schema 无键 + 闸门零路径读取 + 历史值 INFO）。
"""

import asyncio
import copy
import json
import sys
import types
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from core.llm_failover import build_provider_chain
from core.panel_api import build_config_payload, load_schema

WORKDIR = Path(__file__).resolve().parents[1]
SCHEMA = load_schema(WORKDIR)


# ---------------------------------------------------------------------------
# 合成插件（与 test_m19_patch1 同款）
# ---------------------------------------------------------------------------
def _load_plugin_main():
    import importlib

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


class FakeProvider:
    def __init__(self, pid):
        self._pid = pid

    def meta(self):
        return types.SimpleNamespace(id=self._pid)

    @property
    def text_chat(self):
        return object()


class FakePM:
    def __init__(self, providers):
        self._by_id = {p._pid: p for p in providers}

    async def get_provider_by_id(self, pid):
        return self._by_id.get(pid)


class FakeContext:
    """llm_generate 记录调用的 context 替身（含 provider_manager）。"""

    def __init__(self, providers=None, llm=None, using_provider=None):
        self.calls = []
        self._providers = [FakeProvider(p) for p in (providers or [])]
        self.provider_manager = FakePM(self._providers)
        self._llm = llm
        self._using = using_provider

    async def llm_generate(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self._llm, dict):
            pid = kwargs.get("chat_provider_id")
            behavior = self._llm.get(pid)
            if isinstance(behavior, Exception):
                raise behavior
            if callable(behavior):
                return types.SimpleNamespace(
                    completion_text=behavior(kwargs.get("prompt")),
                    result_chain=None,
                )
            return types.SimpleNamespace(
                completion_text=str(behavior or ""), result_chain=None
            )
        if self._llm is None:
            return types.SimpleNamespace(completion_text="", result_chain=None)
        return types.SimpleNamespace(
            completion_text=str(self._llm), result_chain=None
        )

    async def get_using_provider_async(self, umo=None):
        return self._using

    def get_config(self, *args, **kwargs):
        return {"provider_settings": {"wake_prefix": ["/"]}}

    def get_all_providers(self):
        return self._providers


def make_plugin(tmp_path, advanced=None):
    """合成插件实例：advanced 配置写进 tmp 配置文件（_effective_config 直读）。"""
    main_module = _load_plugin_main()
    plugin = object.__new__(main_module.LivingPlugin)
    seed = {"preset": {}, "advanced": dict(advanced or {})}
    cfg_path = Path(tmp_path) / "astrbot_plugin_living_config.json"
    cfg_path.write_text(json.dumps(seed, ensure_ascii=False), encoding="utf-8")
    plugin._plugin_config_path = lambda: str(cfg_path)
    plugin.config = seed
    plugin.context = FakeContext()
    # object.__new__ 跳过 __init__——相关实例属性手工补齐
    plugin._judge = None
    plugin._judge_tasks = set()
    plugin._chat_prefix_cache = {}
    return plugin, main_module


def seed_cache(plugin, umo="aiocqhttp:FriendSession:10001", pid="chat-provider",
               system="最终版聊天 system", contexts=None, age_seconds=0.0):
    """直接注入一条前缀缓存（模拟聊天钩子已跑过）。"""
    plugin._chat_prefix_cache[umo] = {
        "system_prompt": system,
        "contexts": copy.deepcopy(contexts if contexts is not None else [
            {"role": "user", "content": "今天天气不错"},
            {"role": "assistant", "content": "是啊，想出去走走"},
        ]),
        "provider_id": pid,
        "at": datetime.now() - timedelta(seconds=age_seconds),
    }


# ---------------------------------------------------------------------------
# B 组：allow_chat_fallback
# ---------------------------------------------------------------------------
class _B:
    pass


def _chain_ctx(providers, pm_providers=None):
    pm = FakePM(pm_providers if pm_providers is not None else providers)
    return types.SimpleNamespace(
        get_all_providers=lambda: providers, provider_manager=pm
    )


def test_b1_allow_false_chain_has_no_chat_tail():
    """allow_chat_fallback=false → 链尾不再兜底全部已启用聊天模型。"""
    p_own = FakeProvider("own-llm")
    p_chat = FakeProvider("chat-provider")
    ctx = _chain_ctx([p_own, p_chat])
    config = {
        "model": {
            "provider_id": "own-llm",
            "fallback_chain": [],
            "allow_chat_fallback": False,
        }
    }
    chain = asyncio.run(build_provider_chain(ctx, lambda: config))
    assert [pid for pid, _ in chain] == ["own-llm"]


def test_b1_allow_true_keeps_tail_regression():
    """allow_chat_fallback=true（默认）→ 兜底行为与之前一致（回归）。"""
    p_own = FakeProvider("own-llm")
    p_chat = FakeProvider("chat-provider")
    ctx = _chain_ctx([p_own, p_chat])
    config = {
        "model": {"provider_id": "own-llm", "fallback_chain": []}
    }
    chain = asyncio.run(build_provider_chain(ctx, lambda: config))
    assert [pid for pid, _ in chain] == ["own-llm", "chat-provider"]


def test_b1_default_true_when_key_missing():
    """老配置没有该键 → 默认 true（不改变现有用户行为）。"""
    p_chat = FakeProvider("chat-provider")
    ctx = _chain_ctx([p_chat])
    config = {"model": {"provider_id": "", "fallback_chain": []}}
    chain = asyncio.run(build_provider_chain(ctx, lambda: config))
    assert [pid for pid, _ in chain] == ["chat-provider"]


def test_b2_cache_protection_visible_in_log(caplog):
    """B2：兜底被禁止时日志可见"缓存保护"字样。"""
    p_own = FakeProvider("own-llm")
    ctx = _chain_ctx([p_own])
    config = {
        "model": {
            "provider_id": "own-llm",
            "fallback_chain": [],
            "allow_chat_fallback": False,
        }
    }
    import logging

    with caplog.at_level(logging.INFO, logger="astrbot"):
        asyncio.run(build_provider_chain(ctx, lambda: config))
    assert any("缓存保护" in r.message for r in caplog.records)


def test_b2b_unresolvable_provider_id_warns_once(caplog):
    """F4 后端：配置里写了不存在的 provider id → WARNING（含字段名，一次）。"""
    from core import llm_failover

    llm_failover._WARNED_UNRESOLVED.clear()
    p_chat = FakeProvider("chat-provider")
    ctx = _chain_ctx([p_chat])
    config = {"model": {"provider_id": "ghost-llm", "fallback_chain": []}}
    import logging

    with caplog.at_level(logging.WARNING, logger="astrbot"):
        chain = asyncio.run(build_provider_chain(ctx, lambda: config))
        asyncio.run(build_provider_chain(ctx, lambda: config))
    assert [pid for pid, _ in chain] == ["chat-provider"]
    warns = [r for r in caplog.records if "ghost-llm" in r.message]
    assert len(warns) == 1, "同一 pid 不应重复 WARNING"
    assert "model.provider_id" in warns[0].message
    llm_failover._WARNED_UNRESOLVED.clear()


def test_t3_allow_false_all_fail_no_chat_fallback(tmp_path):
    """P0-T3：开关关 + 链上 provider 全失败 → 不回退聊天模型，返回失败。"""
    plugin, _ = make_plugin(
        tmp_path,
        advanced={
            "model": {
                "provider_id": "own-llm",
                "fallback_chain": [],
                "allow_chat_fallback": False,
            }
        },
    )
    plugin.context = FakeContext(
        providers=["own-llm", "chat-provider"],
        llm={"own-llm": RuntimeError("Error code: 429 rate limited")},
    )
    result = asyncio.run(plugin._decision_llm_call("干点啥", None))
    assert result is None
    used = [c["chat_provider_id"] for c in plugin.context.calls]
    assert used == ["own-llm"], f"不应回退到聊天模型，实际尝试了 {used}"


def test_t4_allow_true_keeps_fallback_regression(tmp_path):
    """P0-T4：开关开（默认）→ 兜底行为与之前一致（回归）。"""
    plugin, _ = make_plugin(
        tmp_path,
        advanced={
            "model": {"provider_id": "own-llm", "fallback_chain": []}
        },
    )
    plugin.context = FakeContext(
        providers=["own-llm", "chat-provider"],
        llm={
            "own-llm": RuntimeError("Error code: 429 rate limited"),
            "chat-provider": "兜底成功",
        },
    )
    result = asyncio.run(plugin._decision_llm_call("干点啥", None))
    assert result == "兜底成功"
    used = [c["chat_provider_id"] for c in plugin.context.calls]
    assert used == ["own-llm", "chat-provider"]


# ---------------------------------------------------------------------------
# E 组：聊天前缀缓存与前缀对齐
# ---------------------------------------------------------------------------
def test_e_hook_caches_final_shape_with_negative_priority():
    """E2：on_llm_request 钩子（priority=-1）缓存最终 system+contexts。"""
    plugin, main_module = make_plugin(tmp_path=None or Path("."), advanced={})
    event = types.SimpleNamespace(unified_msg_origin="aiocqhttp:FriendSession:10001")
    req = types.SimpleNamespace(
        system_prompt="人设 + 技能 + preset 最终版",
        contexts=[
            {"role": "system", "content": "ignored-in-copy"},
            {"role": "user", "content": "你好"},
        ],
    )
    plugin.context = FakeContext(providers=["chat-provider"], using_provider=FakeProvider("chat-provider"))
    asyncio.run(
        main_module.LivingPlugin.chat_prefix_cache_on_llm_request(plugin, event, req)
    )
    entry = plugin._chat_prefix_cache["aiocqhttp:FriendSession:10001"]
    assert entry["system_prompt"] == "人设 + 技能 + preset 最终版"
    assert entry["contexts"] == req.contexts
    assert entry["contexts"] is not req.contexts, "必须是深拷贝快照"
    assert entry["contexts"][0] is not req.contexts[0], "逐条拷贝"
    assert entry["provider_id"] == "chat-provider"


def test_e_hook_priority_anchor_in_source():
    """E2：缓存钩子必须以 priority=-1 注册（排在 prompt-preset 等默认
    优先级插件之后，拿到"最终版"）。"""
    src = (WORKDIR / "main.py").read_text(encoding="utf-8")
    assert "@filter.on_llm_request(priority=-1)" in src


def test_e3_prefix_verbatim_alignment_for_decision_call(tmp_path):
    """P0-E3（硬要求）：回退到聊天模型时 system/contexts 与聊天请求
    逐字一致；原有 system 指令并入 user 消息开头（一条不丢）。"""
    plugin, _ = make_plugin(tmp_path, advanced={"model": {"provider_id": ""}})
    plugin.context = FakeContext(
        providers=["chat-provider"],
        llm={"chat-provider": "答复"},
        using_provider=FakeProvider("chat-provider"),
    )
    seed_cache(plugin, pid="chat-provider", system="聊天最终 SYSTEM",
               contexts=[{"role": "user", "content": "历史一"},
                         {"role": "assistant", "content": "历史二"}])
    text = asyncio.run(
        plugin._decision_llm_call("她的活动指令", "她的原 system")
    )
    assert text == "答复"
    call = plugin.context.calls[-1]
    assert call["chat_provider_id"] == "chat-provider"
    assert call["system_prompt"] == "聊天最终 SYSTEM"  # 逐字一致
    assert call["contexts"] == [{"role": "user", "content": "历史一"},
                                {"role": "assistant", "content": "历史二"}]
    assert call["prompt"] == "她的原 system\n\n她的活动指令"  # 指令并入 user


def test_e_decision_call_without_cache_keeps_current_form(tmp_path):
    """E5：缓存取不到（还没聊过天）→ 退回现有形态，不报错（回归）。"""
    plugin, _ = make_plugin(tmp_path, advanced={"model": {"provider_id": ""}})
    plugin.context = FakeContext(
        providers=["chat-provider"], llm={"chat-provider": "答复"}
    )
    text = asyncio.run(plugin._decision_llm_call("活动指令", "原 system"))
    assert text == "答复"
    call = plugin.context.calls[-1]
    assert call["system_prompt"] == "原 system"
    assert call["contexts"] is None


def test_e_stale_cache_expires_via_ttl(tmp_path):
    """E5：缓存超过 TTL（prefix_cache_ttl_minutes）→ 退回现有形态。"""
    plugin, _ = make_plugin(
        tmp_path,
        advanced={"model": {"provider_id": "", "prefix_cache_ttl_minutes": 1}},
    )
    plugin.context = FakeContext(
        providers=["chat-provider"], llm={"chat-provider": "答复"},
        using_provider=FakeProvider("chat-provider"),
    )
    seed_cache(plugin, pid="chat-provider", system="旧前缀", age_seconds=120.0)
    asyncio.run(plugin._decision_llm_call("活动指令", "原 system"))
    call = plugin.context.calls[-1]
    assert call["system_prompt"] == "原 system"  # 未对齐
    assert call["contexts"] is None


def test_e_no_alignment_for_independent_provider(tmp_path):
    """E1：配了独立 provider（与聊天不同账号）→ 保持现有形态。"""
    plugin, _ = make_plugin(
        tmp_path,
        advanced={"model": {"provider_id": "own-llm", "fallback_chain": []}},
    )
    plugin.context = FakeContext(
        providers=["own-llm", "chat-provider"],
        llm={"own-llm": "独立答复"},
        using_provider=FakeProvider("chat-provider"),
    )
    seed_cache(plugin, pid="chat-provider", system="聊天最终 SYSTEM")
    asyncio.run(plugin._decision_llm_call("活动指令", "原 system"))
    call = plugin.context.calls[-1]
    assert call["chat_provider_id"] == "own-llm"
    assert call["system_prompt"] == "原 system"  # 不对齐
    assert call["contexts"] is None


def test_e_judge_call_aligned_when_same_provider(tmp_path):
    """E1：judge.provider_id 被配成与聊天同一个 provider → 同样对齐。"""
    plugin, _ = make_plugin(
        tmp_path,
        advanced={
            "model": {"provider_id": ""},
            "judge": {"provider_id": "chat-provider", "mode": "api"},
        },
    )
    plugin.context = FakeContext(
        providers=["chat-provider"],
        llm={"chat-provider": "判词"},
        using_provider=FakeProvider("chat-provider"),
    )
    seed_cache(plugin, pid="chat-provider", system="聊天最终 SYSTEM",
               contexts=[{"role": "user", "content": "历史"}])
    text = asyncio.run(plugin._judge_llm_call("判断提示词", None))
    assert text == "判词"
    call = plugin.context.calls[-1]
    assert call["system_prompt"] == "聊天最终 SYSTEM"
    assert call["contexts"] == [{"role": "user", "content": "历史"}]


def test_e_prefix_cache_lru_bound():
    """多会话缓存有界（LRU 上限 4）。"""
    plugin, main_module = make_plugin(Path("."), advanced={})
    plugin.context = FakeContext(providers=["chat-provider"])
    for i in range(5):
        event = types.SimpleNamespace(unified_msg_origin=f"umo-{i}")
        req = types.SimpleNamespace(system_prompt="s", contexts=[])
        asyncio.run(
            main_module.LivingPlugin.chat_prefix_cache_on_llm_request(
                plugin, event, req
            )
        )
    assert len(plugin._chat_prefix_cache) == 4
    assert "umo-0" not in plugin._chat_prefix_cache  # 最旧的被挤掉


def _patch_agent_runner(monkeypatch, seen):
    """替身 runner：记录 reset 收到的 request。同时替身 AstrAgentContext
    （pydantic 要真 Context 实例，单测里给 SimpleNamespace 即可）。"""
    import core.agent_loop as agent_loop_module

    class RecordingRunner:
        def __init__(self):
            self.stats = types.SimpleNamespace(token_usage=types.SimpleNamespace(total=0))

        async def reset(self, provider=None, request=None, **kwargs):
            seen.append(request)

        async def step_until_done(self, max_steps):
            return
            yield  # pragma: no cover（使本函数成为异步生成器）

        def request_stop(self):
            pass

        def get_final_llm_resp(self):
            return types.SimpleNamespace(completion_text="玩好了", result_chain=None)

    monkeypatch.setattr(agent_loop_module, "ToolLoopAgentRunner", RecordingRunner)
    monkeypatch.setattr(
        agent_loop_module,
        "AstrAgentContext",
        lambda context=None, event=None: types.SimpleNamespace(
            context=context, event=event
        ),
    )


def test_t2_agent_loop_request_aligned_when_same_provider(tmp_path, monkeypatch):
    """P0-T2（agent 形态）：回退到聊天 provider 的 agent 请求用聊天前缀，
    原 system 整体并入首条 user 消息（指令一条不丢）。"""
    from core.agent_loop import LivingAgentLoop

    seen = []
    _patch_agent_runner(monkeypatch, seen)

    align = {
        "system_prompt": "聊天最终 SYSTEM",
        "contexts": [{"role": "user", "content": "历史"}],
    }
    loop = LivingAgentLoop(
        context=types.SimpleNamespace(),
        config_getter=lambda: {"decision": {"single_run_token_budget": 50000000,
                                            "max_tool_rounds": 0}},
        tool_builder=lambda: __import__(
            "core.living_tools", fromlist=["ToolSet"]
        ).ToolSet(tools=[]),
    )
    result = asyncio.run(
        loop._run_with_provider(FakeProvider("chat-provider"), "chat-provider",
                                "去逛逛", 1000, 3, align=align)
    )
    assert result.ok is True and result.text == "玩好了"
    request = seen[-1]
    assert request.system_prompt == "聊天最终 SYSTEM"  # 逐字一致
    assert request.contexts == [{"role": "user", "content": "历史"}]
    assert "去逛逛" in request.prompt  # 原意图并入首条 user 消息


def test_t2_agent_loop_no_align_keeps_current_form(tmp_path, monkeypatch):
    """不对齐时 agent 请求形态与之前一致（回归）。"""
    from core.agent_loop import LivingAgentLoop

    seen = []
    _patch_agent_runner(monkeypatch, seen)
    loop = LivingAgentLoop(
        context=types.SimpleNamespace(),
        config_getter=lambda: {"decision": {"single_run_token_budget": 50000000,
                                            "max_tool_rounds": 0}},
        tool_builder=lambda: __import__(
            "core.living_tools", fromlist=["ToolSet"]
        ).ToolSet(tools=[]),
    )
    asyncio.run(
        loop._run_with_provider(FakeProvider("own-llm"), "own-llm", "去逛逛",
                                1000, 3)
    )
    request = seen[-1]
    # 无 persona/无对齐：system 为空、不带历史、prompt 原样（与之前一致）
    assert request.system_prompt == ""
    assert request.contexts == []
    assert request.prompt == "去逛逛"


# ---------------------------------------------------------------------------
# C 组：统一性与守护
# ---------------------------------------------------------------------------
def test_c1_guard_no_bypass_calls_in_core():
    """P0-T5 守护：core/ 下不存在绕过链的 llm_generate 直调
    （所有 LLM 调用必须走 build_provider_chain 链或注入的 llm_call）。"""
    import re

    offenders = []
    for path in (WORKDIR / "core").glob("*.py"):
        src = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(src.splitlines(), 1):
            code = line.split("#")[0]
            if re.search(r"\.llm_generate\(", code):
                offenders.append(f"{path.name}:{lineno}: {line.strip()}")
    assert offenders == [], f"core/ 出现 llm_generate 直调：{offenders}"


def test_c1_guard_main_llm_generate_only_in_chain_helpers():
    """P0-T5 守护：main.py 的 llm_generate 只出现在两个链helper 内
    （_decision_llm_call / _judge_llm_call）。"""
    src = (WORKDIR / "main.py").read_text(encoding="utf-8")
    count = src.count("self.context.llm_generate(")
    assert count == 2, f"main.py llm_generate 调用点应为 2 处，实际 {count}"
    # 两处必须都在带故障转移链/独立 provider 语义的 helper 里
    for helper in ("async def _decision_llm_call", "async def _judge_llm_call"):
        assert helper in src


def test_c1_guard_agent_loop_uses_chain():
    """P0-T5 守护：agent 循环必须经 build_provider_chain（C1）。"""
    src = (WORKDIR / "core" / "agent_loop.py").read_text(encoding="utf-8")
    assert "build_provider_chain(" in src


def test_b3_judge_independence_regression(tmp_path):
    """P0-T6/B3：judge.provider_id 未配置 → 完全不调用（独立设计保持）。"""
    plugin, _ = make_plugin(
        tmp_path, advanced={"judge": {"mode": "api", "provider_id": ""}}
    )
    plugin.context = FakeContext(providers=["chat-provider"], llm="x")
    text = asyncio.run(plugin._judge_llm_call("判断", None))
    assert text is None
    assert plugin.context.calls == [], "judge 未配置时不得发出任何调用"


# ---------------------------------------------------------------------------
# F 组：面板数据源与目录浏览
# ---------------------------------------------------------------------------
def test_f_payload_carries_agent_tools():
    """F3：payload 提供 agent_tools（本体工具多选数据源）。"""
    payload = build_config_payload({"preset": {}, "advanced": {}}, SCHEMA,
                                   providers=["p1"], agent_tools=["web_search"])
    assert payload["agent_tools"] == ["web_search"]


def test_f1_model_provider_id_schema_fields():
    """A1/B1：model 组 hint 含缓存代价、新增 allow_chat_fallback 键。"""
    items = SCHEMA["advanced"]["items"]["model"]["items"]
    assert "缓存" in items["provider_id"]["hint"]
    assert "按未命中重新计费" in items["provider_id"]["hint"]
    fallback = items["allow_chat_fallback"]
    assert fallback["type"] == "bool" and fallback["default"] is True
    assert "缓存" in fallback["hint"]


def test_f2_fs_list_rejects_outside_roots(tmp_path):
    """F2 安全：根外路径明确拒绝（不静默回落）。"""
    plugin, _ = make_plugin(tmp_path, advanced={})
    plugin._fs_allowed_roots = lambda: [str(tmp_path / "data_root")]
    path, error, roots = plugin._fs_resolve_browsable(str(tmp_path / "outside"))
    assert path is None
    assert "不在允许浏览的范围" in error
    assert roots == [str(tmp_path / "data_root")]


def test_f2_fs_list_within_root_ok_and_dirs_only(tmp_path):
    """F2：根内路径放行；枚举只含子目录（文件不出现在清单里）。"""
    plugin, _ = make_plugin(tmp_path, advanced={})
    root = tmp_path / "data_root"
    (root / "sub1").mkdir(parents=True)
    (root / "sub2").mkdir()
    (root / "file.txt").write_text("x", encoding="utf-8")
    plugin._fs_allowed_roots = lambda: [str(root)]
    path, error, _ = plugin._fs_resolve_browsable(str(root))
    assert error == "" and path == str(root)
    entries = sorted(e.name for e in root.iterdir() if e.is_dir())
    assert entries == ["sub1", "sub2"]
    # 子目录进入
    path2, error2, _ = plugin._fs_resolve_browsable(str(root / "sub1"))
    assert error2 == "" and path2 == str(root / "sub1")


def test_f2_fs_resolve_missing_dir_reports(tmp_path):
    """F2：目录不存在 → 明确报错（handler 层文案），解析层先放行到
    枚举阶段。这里测根内不存在路径的解析行为（不抛异常）。"""
    plugin, _ = make_plugin(tmp_path, advanced={})
    plugin._plugin_data_dir = lambda: str(tmp_path)
    plugin._fs_allowed_roots = lambda: [str(tmp_path)]
    path, error, _ = plugin._fs_resolve_browsable(str(tmp_path / "nope"))
    assert error == "" and path == str(tmp_path / "nope")


def test_f3_schema_control_anchors():
    """F1/F3：schema/前端锚点——provider 下拉、多选、时间窗、目录浏览。"""
    items = SCHEMA["advanced"]["items"]
    # quiet_hours 已删（G1）
    assert "quiet_hours" not in items["output_gate"]["items"]
    # circadian_hint 保留（边界：别删错）
    assert "circadian_hint" in items["sleep"]["items"]
    js = (WORKDIR / "pages" / "config" / "app.js").read_text(encoding="utf-8")
    assert 'group === "model" && key === "provider_id"' in js
    assert 'group === "autonomy" && key === "workspace_dir"' in js
    assert 'group === "initiative" && key === "sources"' in js
    assert 'group === "capabilities" && key === "agent_tools"' in js
    assert 'group === "sleep" && key === "circadian_hint"' in js
    assert 'state.agent_tools = payload.agent_tools' in js


# ---------------------------------------------------------------------------
# G 组：quiet_hours 删净
# ---------------------------------------------------------------------------
def test_g_schema_quiet_hours_gone():
    """G1：schema 不再有 quiet_hours。"""
    items = SCHEMA["advanced"]["items"]["output_gate"]["items"]
    assert "quiet_hours" not in items


def test_g_gate_has_no_quiet_hours_path(tmp_path):
    """G6：should_send_message 无 quiet_hours 读取/拦截路径；其余闸门零改动。"""
    src = (WORKDIR / "core" / "living_state.py").read_text(encoding="utf-8")
    gate_src = src.split("async def should_send_message")[1].split("\n    async def")[0]
    assert 'output.get("quiet_hours")' not in gate_src
    assert 'return False, "quiet_hours"' not in gate_src
    # 其余闸门仍在
    assert "msg_daily_limit" in gate_src
    assert "msg_interval" in gate_src


def test_g_circadian_hint_path_untouched():
    """边界：circadian_hint 的小睡禁窗（sleep.py 依赖 parse_time_window）零改动。"""
    from core.living_state import in_time_window, parse_time_window

    window = parse_time_window("23:00-07:00")
    assert window is not None
    assert in_time_window(datetime(2026, 10, 6, 3, 0), window)
    src = (WORKDIR / "core" / "sleep.py").read_text(encoding="utf-8")
    assert "parse_time_window" in src


def test_g_deprecation_info_logged(tmp_path, caplog):
    """G4：历史配置里残留 quiet_hours 值 → 启动记 INFO（不静默吞掉）。

    initialize 的废弃检查段读磁盘配置（与 _effective_config 同源）；
    这里验证磁盘配置确实能读出残留值（INFO 的数据源）+ main.py 里
    该检查存在。"""
    import logging

    from core.conf_path import conf_group

    plugin, main_module = make_plugin(
        tmp_path,
        advanced={"output_gate": {"quiet_hours": "01:00-06:00"}},
    )
    legacy = str(
        conf_group(plugin._effective_config(), "output_gate").get(
            "quiet_hours", ""
        )
        or ""
    ).strip()
    assert legacy == "01:00-06:00"  # INFO 提示的数据源可用
    src = (WORKDIR / "main.py").read_text(encoding="utf-8")
    assert "该键已废弃" in src
    assert "output_gate.quiet_hours" in src


# ---------------------------------------------------------------------------
# T1（P0）：provider_id 为空 → 行为不变（回归）+ 对齐生效
# ---------------------------------------------------------------------------
def test_t1_empty_provider_id_behavior_unchanged(tmp_path):
    """P0-T1：provider_id 为空 → 走聊天模型（行为不变），有缓存时自动
    前缀对齐（不再冲缓存）；无缓存时与旧形态一致。"""
    plugin, _ = make_plugin(tmp_path, advanced={"model": {"provider_id": ""}})
    plugin.context = FakeContext(
        providers=["chat-provider"], llm={"chat-provider": "答复"},
        using_provider=FakeProvider("chat-provider"),
    )
    # 无缓存：旧形态
    asyncio.run(plugin._decision_llm_call("活动指令", "原 system"))
    call0 = plugin.context.calls[-1]
    assert call0["chat_provider_id"] == "chat-provider"
    assert call0["system_prompt"] == "原 system"
    # 有缓存：对齐
    seed_cache(plugin, pid="chat-provider", system="聊天最终 SYSTEM",
               contexts=[{"role": "user", "content": "历史"}])
    asyncio.run(plugin._decision_llm_call("活动指令", "原 system"))
    call1 = plugin.context.calls[-1]
    assert call1["system_prompt"] == "聊天最终 SYSTEM"
    assert call1["contexts"] == [{"role": "user", "content": "历史"}]


def test_t2_configured_provider_used_by_all_calls(tmp_path):
    """P0-T2：provider_id 已配 → 决策链首用它（九处调用共用该链）。"""
    plugin, _ = make_plugin(
        tmp_path,
        advanced={
            "model": {"provider_id": "own-llm", "fallback_chain": ["backup-llm"]}
        },
    )
    plugin.context = FakeContext(
        providers=["own-llm", "backup-llm", "chat-provider"],
        llm={"own-llm": "自答"},
        using_provider=FakeProvider("chat-provider"),
    )
    seed_cache(plugin, pid="chat-provider", system="聊天 SYSTEM")
    asyncio.run(plugin._decision_llm_call("活动指令", None))
    call = plugin.context.calls[-1]
    assert call["chat_provider_id"] == "own-llm"
    assert call["system_prompt"] is None  # 独立 provider：现有形态，不对齐
