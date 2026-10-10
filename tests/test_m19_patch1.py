"""M19-补丁1 测试：判断模型三档框架 + 提示词搬面板。

A 组：judge.mode 三档——off 零调用零注入（T1）、local 明确不工作不静默
     降级（T2）、api provider 未选/不存在按不判断处理（T3）。
B 组：输入判断解析四态（T4）、注入只追加 extra_user_content_parts 且
     system_prompt/contexts 逐字不变（T5）、限频与跳过（T6）、超时/异常
     不影响主回复（T7）、结果不进记忆（T8）。
C 组：log_only 记录但输出逐字不变（T9）、rewrite 最多一次且失败放行
     原回复（T10）。
D 组：六处提示词搬面板——**默认值渲染结果与搬之前硬编码逐字相等**
     （T11，关键测试）、用户删占位符/清空模板不崩溃有兜底（T12）、
     schema default 与代码常量逐字一致。
E 组：判断记录（内存 + judge_records.json 落盘重载 + 面板端点）。
F 组：面板控件——枚举下拉/活动多选/故障转移链顺序编辑/provider 数据
     源（T13-T16，源码锚点 + schema 断言 + payload 断言）。

测试不写 AstrBot 本体数据目录；真实身份一律用先例假号 10001。
"""

import asyncio
import inspect
import json
import logging
import sys
import types
from datetime import datetime
from pathlib import Path

import pytest

from core.activities import (
    ActivityContext,
    FreeActivity,
    MiniGameActivity,
    ReadArticleActivity,
    SurfActivity,
)
from core.decider import ActivityDecider
from core.initiative import InitiativeEngine
from core.judge import (
    DEFAULT_INJECT_TEMPLATE,
    DEFAULT_PROMPT_INPUT,
    DEFAULT_PROMPT_OUTPUT,
    JudgeVerdict,
    OutputJudge,
)
from core.living_loop import (
    DEFAULT_PROMPT_DREAM,
    DEFAULT_PROMPT_FAREWELL,
    DEFAULT_PROMPT_WAKE_REPLY,
    LivingLoop,
)
from core.panel_api import build_config_payload, load_schema
from core.prompts import read_template, render_template
from core.style_learning import DEFAULT_PROMPT_DISTILL, StyleLearner

WORKDIR = Path(__file__).resolve().parents[1]
SCHEMA = load_schema(WORKDIR)
NOW = datetime(2026, 10, 6, 14, 5, 0)


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


def make_plugin(tmp_path, judge_config=None, llm=None, providers=None):
    """合成插件实例：judge 组写进 tmp 配置文件（_effective_config 直读）。"""
    main_module = _load_plugin_main()
    plugin = object.__new__(main_module.LivingPlugin)
    seed = {"preset": {}, "advanced": {"judge": dict(judge_config or {})}}
    cfg_path = Path(tmp_path) / "astrbot_plugin_living_config.json"
    cfg_path.write_text(json.dumps(seed, ensure_ascii=False), encoding="utf-8")
    plugin._plugin_config_path = lambda: str(cfg_path)
    plugin.config = seed
    plugin.context = FakeContext(providers=providers, llm=llm)
    # object.__new__ 跳过 __init__——judge 相关实例属性手工补齐
    plugin._judge = None
    plugin._judge_tasks = set()
    return plugin, main_module


class FakeContext:
    """judge 链路的 context 替身：llm_generate / get_config / providers。"""

    def __init__(self, providers=None, llm=None):
        self.calls = []
        self._providers = providers or []
        self._llm = llm

    async def llm_generate(self, **kwargs):
        self.calls.append(kwargs)
        if self._llm is None:
            return types.SimpleNamespace(completion_text="", result_chain=None)
        return types.SimpleNamespace(
            completion_text=self._llm(kwargs.get("prompt")), result_chain=None
        )

    def get_config(self, *args, **kwargs):
        return {"provider_settings": {"wake_prefix": ["/"]}}

    def get_all_providers(self):
        return [
            types.SimpleNamespace(meta=lambda pid=pid: types.SimpleNamespace(id=pid))
            for pid in self._providers
        ]


class FakeReq:
    """on_llm_request 的 req 替身：M17-补丁1 T16 同款字段。"""

    def __init__(self, prompt="在吗", contexts=None):
        self.system_prompt = "原始人设提示词"
        self.prompt = prompt
        self.contexts = contexts if contexts is not None else [
            {"role": "user", "content": "今天天气不错"},
            {"role": "assistant", "content": "是啊，想出去走走"},
        ]
        self.extra_user_content_parts = []


class FakeEvent:
    message_str = "帮我看看这个报错是怎么回事，我折腾了一下午了"


def judge_of(plugin, llm_responses, tmp_path, **cfg):
    """构造绑定 plugin 的 OutputJudge（脚本化 LLM 应答）。

    注意：不改写配置磁盘内容——磁盘由 write_plugin_cfg/测试自己负责，
    judge 的 config_getter 走 plugin._effective_config（磁盘直读）。"""
    seq = list(llm_responses)

    async def llm_call(prompt, system_prompt=None):
        if not seq:
            raise RuntimeError("no more scripted responses")
        item = seq.pop(0)
        if isinstance(item, Exception):
            raise item
        if isinstance(item, float):  # 超时模拟：挂起指定秒数
            await asyncio.sleep(item)
        return item

    judge = OutputJudge(
        config_getter=plugin._effective_config,
        llm_call=llm_call,
        records_path=Path(tmp_path) / "judge_records.json",
        clock=lambda: NOW,
    )
    plugin._judge = judge
    return judge


def judge_config(tmp_path, **overrides):
    cfg = {"mode": "api", "provider_id": "judge-provider"}
    cfg.update(overrides)
    return cfg


def write_plugin_cfg(plugin, judge_cfg):
    seed = {"preset": {}, "advanced": {"judge": judge_cfg}}
    Path(plugin._plugin_config_path()).write_text(
        json.dumps(seed, ensure_ascii=False), encoding="utf-8"
    )
    plugin.config = seed


# ---------------------------------------------------------------------------
# A 组：三档骨架（T1-T3）
# ---------------------------------------------------------------------------
def test_t1_off_mode_zero_calls_zero_injection(tmp_path):
    """T1：off 档零 LLM 调用、零注入——默认档必须"什么都感觉不到"。"""
    plugin, _main = make_plugin(tmp_path)
    write_plugin_cfg(plugin, {"mode": "off", "provider_id": "judge-provider"})
    judge = OutputJudge(
        config_getter=plugin._effective_config,
        llm_call=None,  # off 档连 llm_call 都不该被碰
        records_path=Path(tmp_path) / "j.json",
        clock=lambda: NOW,
    )
    plugin._judge = judge

    req = FakeReq()
    asyncio.run(plugin.judge_input_on_llm_request(FakeEvent(), req))

    assert req.extra_user_content_parts == []  # 零注入
    assert plugin.context.calls == []  # 零 provider 调用
    assert judge.records() == []  # 零记录


def test_t2_local_mode_explicitly_not_working(tmp_path, caplog):
    """T2：local 档明确不工作且一次性 WARNING——不静默降级成 api/off。"""
    plugin, _main = make_plugin(tmp_path)
    write_plugin_cfg(plugin, {"mode": "local", "provider_id": "judge-provider"})
    judge = judge_of(plugin, ['{"mode": "chat", "length": "short", "tone": "plain", "note": ""}'],
                     tmp_path)
    req = FakeReq()

    with caplog.at_level(logging.WARNING):
        asyncio.run(plugin.judge_input_on_llm_request(FakeEvent(), req))
        asyncio.run(plugin.judge_input_on_llm_request(FakeEvent(), req))

    assert req.extra_user_content_parts == []  # 不注入
    assert judge.records() == []  # 不判断不记录
    warnings = [r for r in caplog.records if "尚未实现" in r.getMessage()]
    assert warnings, "local 档必须 WARNING 提示未实现"
    # 各一次：两条消息只 warn 一次（一次性提示，不刷屏）
    assert len(warnings) == 1


def test_t3_api_missing_provider_falls_back_to_no_judgement(tmp_path, caplog):
    """T3：api 档 provider 未选/不存在 → 不报错、按本轮不判断处理。"""
    plugin, _main = make_plugin(tmp_path, providers=["chat-provider"])
    # 未选 provider
    write_plugin_cfg(plugin, {"mode": "api", "provider_id": ""})
    judge = judge_of(plugin, [], tmp_path)
    req = FakeReq()
    with caplog.at_level(logging.WARNING):
        asyncio.run(plugin.judge_input_on_llm_request(FakeEvent(), req))
    assert req.extra_user_content_parts == []
    assert plugin.context.calls == []  # provider 为空：连 llm_generate 都不发

    # 选了不存在的 provider：llm_generate 抛 ProviderNotFoundError 形态异常
    write_plugin_cfg(plugin, {"mode": "api", "provider_id": "ghost-provider"})
    judge2 = judge_of(plugin, [], tmp_path)

    async def raising_llm_generate(**kwargs):
        raise RuntimeError("Provider ghost-provider not found")

    plugin.context.llm_generate = raising_llm_generate
    req2 = FakeReq()
    asyncio.run(plugin.judge_input_on_llm_request(FakeEvent(), req2))
    assert req2.extra_user_content_parts == []  # 不注入、不报错、聊天照常


# ---------------------------------------------------------------------------
# B 组：输入侧判断（T4-T8）
# ---------------------------------------------------------------------------
def test_t4_verdict_parse_four_states():
    """T4：合法 JSON / 非法 JSON / 缺字段（中性默认）/ 超长 note 截断 40 字。"""
    judge = OutputJudge(config_getter=lambda: {}, llm_call=None)

    # 合法
    v = judge._parse_verdict(
        '{"mode": "work", "length": "short", "tone": "warm", "note": "这条可以短一点答"}'
    )
    assert (v.mode, v.length, v.tone, v.note) == ("work", "short", "warm", "这条可以短一点答")
    # 带代码围栏的合法 JSON（宽容解析先例）
    v2 = judge._parse_verdict('```json\n{"mode": "chat", "length": "long", "tone": "plain", "note": "x"}\n```')
    assert v2.length == "long"
    # 非法 JSON → None
    assert judge._parse_verdict("这不是 JSON") is None
    assert judge._parse_verdict("") is None
    # 缺字段 → 中性默认
    v3 = judge._parse_verdict('{"mode": "work"}')
    assert v3.mode == "work"  # 给了的保留
    assert v3.length == "normal" and v3.tone == "plain" and v3.note == ""
    # 越界枚举值 → 中性默认
    v4 = judge._parse_verdict('{"mode": "poetry", "length": "epic", "tone": "salty", "note": "n"}')
    assert (v4.mode, v4.length, v4.tone) == ("chat", "normal", "plain")
    # 超长 note 截断 40 字
    v5 = judge._parse_verdict('{"note": "' + "长" * 60 + '"}')
    assert len(v5.note) == 40


def test_t5_injection_only_appends_extra_parts(tmp_path):
    """T5：注入只追加 extra_user_content_parts——system_prompt/contexts/
    prompt 逐字不变（M17 通道红线 4）。"""
    plugin, _main = make_plugin(tmp_path)
    write_plugin_cfg(plugin, judge_config(tmp_path))
    judge = judge_of(
        plugin,
        ['{"mode": "work", "length": "short", "tone": "plain", "note": "这条可以短一点答"}'],
        tmp_path,
    )
    req = FakeReq()
    sys_before = req.system_prompt
    ctx_before = json.dumps(req.contexts, ensure_ascii=False)
    prompt_before = req.prompt

    asyncio.run(plugin.judge_input_on_llm_request(FakeEvent(), req))

    parts = req.extra_user_content_parts
    assert len(parts) == 1
    text = parts[0].text
    assert "work" in text and "short" in text and "这条可以短一点答" in text
    assert "内部提醒" in text  # 模板包裹（用户看不到的注入块形态）
    assert req.system_prompt == sys_before  # 逐字不变
    assert json.dumps(req.contexts, ensure_ascii=False) == ctx_before
    assert req.prompt == prompt_before
    # E1/E2：注入成功有记录 + INFO 日志形态（记录在案）
    injected = [r for r in judge.records() if r["injected"]]
    assert len(injected) == 1
    assert injected[0]["side"] == "input"


def test_t6_skip_rules_interval_short_and_command(tmp_path):
    """T6：限频（间隔内跳过）、极短消息（≤3 字）跳过、命令前缀跳过。"""
    plugin, _main = make_plugin(tmp_path)
    write_plugin_cfg(plugin, judge_config(tmp_path, min_interval_seconds=20))
    judge = judge_of(
        plugin,
        ['{"mode": "chat", "length": "normal", "tone": "plain", "note": ""}'] * 3,
        tmp_path,
    )

    # 命令前缀（wake_prefix="/"）跳过：不发判断
    class CmdEvent:
        message_str = "/living status"

    req_cmd = FakeReq()
    asyncio.run(plugin.judge_input_on_llm_request(CmdEvent(), req_cmd))
    assert req_cmd.extra_user_content_parts == []
    assert plugin.context.calls == []

    # 极短消息（≤3 字）跳过
    class ShortEvent:
        message_str = "嗯"

    req_short = FakeReq()
    asyncio.run(plugin.judge_input_on_llm_request(ShortEvent(), req_short))
    assert req_short.extra_user_content_parts == []
    assert plugin.context.calls == []

    # 第一条正常判断 → 第二条在 20s 间隔内被跳过（时间戳不前进）。
    # 判断走 judge 绑定的 llm_call（不经 plugin.context.llm_generate），
    # 判断成功与否以注入记录为准。
    asyncio.run(plugin.judge_input_on_llm_request(FakeEvent(), FakeReq()))
    assert len([r for r in judge.records() if r["injected"]]) == 1
    assert judge._last_input_judge_at is not None
    req_in_interval = FakeReq()
    asyncio.run(plugin.judge_input_on_llm_request(FakeEvent(), req_in_interval))
    assert req_in_interval.extra_user_content_parts == []
    assert len([r for r in judge.records() if r["injected"]]) == 1  # 间隔内未再判断/注入


def test_t7_timeout_and_error_never_block_reply(tmp_path):
    """T7：判断超时/异常 → 放弃注入，主回复照常走（钩子不抛）。"""
    plugin, _main = make_plugin(tmp_path)
    write_plugin_cfg(plugin, judge_config(tmp_path, timeout_seconds=1))
    judge = judge_of(plugin, [10.0, RuntimeError("provider boom")], tmp_path)

    # 超时（挂起 10s > 1s 上限）
    req1 = FakeReq()
    asyncio.run(plugin.judge_input_on_llm_request(FakeEvent(), req1))
    assert req1.extra_user_content_parts == []
    # 异常（重置限频时间戳——否则第二条被 B1 限频跳过而非走到调用）
    judge._last_input_judge_at = None
    req2 = FakeReq()
    asyncio.run(plugin.judge_input_on_llm_request(FakeEvent(), req2))
    assert req2.extra_user_content_parts == []
    # 两种失败都有记录（verdict=timeout/error），但流程无异常
    verdicts = [r["verdict"] for r in judge.records()]
    assert any(v == "timeout" for v in verdicts)
    assert any(v.startswith("error") for v in verdicts)


def test_t8_judge_never_touches_memory(tmp_path):
    """T8：判断结果不进记忆——全流程零 memory/会话调用；落盘只有
    judge_records.json（红线 1 的物理隔离）。"""
    plugin, _main = make_plugin(tmp_path)
    write_plugin_cfg(plugin, judge_config(tmp_path))
    judge = judge_of(
        plugin,
        ['{"mode": "chat", "length": "normal", "tone": "warm", "note": "语气可以暖一点"}'],
        tmp_path,
    )
    memory_calls = []

    async def memory_getter():
        memory_calls.append(1)
        return None

    plugin._get_memory = memory_getter
    asyncio.run(plugin.judge_input_on_llm_request(FakeEvent(), FakeReq()))
    assert memory_calls == []  # 零记忆调用
    # 记录文件是独立 json，字段白名单（不含任何可被"召回"的正文存储）
    path = Path(tmp_path) / "judge_records.json"
    assert path.exists()
    rows = json.loads(path.read_text(encoding="utf-8"))
    assert set(rows[0]) == {"ts", "side", "input_summary", "verdict", "injected", "rewrote"}
    # 判断模块本身没有任何记忆写入接口（结构性断言：构造参数与公开方法
    # 名都不含 memory——rewrite_output 里的 "write" 是改写语义，另行排除）
    assert "memory" not in inspect.signature(OutputJudge.__init__).parameters
    public = [n for n, _ in inspect.getmembers(OutputJudge, inspect.isfunction)
              if not n.startswith("_")]
    assert not [n for n in public if "memory" in n], public


# ---------------------------------------------------------------------------
# C 组：输出侧检查（T9-T10）
# ---------------------------------------------------------------------------
def _fake_response(text):
    from astrbot.core.provider.entities import LLMResponse

    return LLMResponse(role="assistant", completion_text=text)


def test_t9_log_only_records_but_never_changes_output(tmp_path):
    """T9：log_only（默认）——记录在案但输出逐字不变。"""
    plugin, _main = make_plugin(tmp_path)
    write_plugin_cfg(plugin, judge_config(tmp_path, output_action="log_only"))
    judge = judge_of(
        plugin,
        ['{"ok": false, "note": "车轱辘话有点多", "fixed": ""}'],
        tmp_path,
    )
    original = "好的我帮你看看。好的我帮你看看，你把报错发我。好的我帮你看看。"
    response = _fake_response(original)

    async def flow():
        await plugin.judge_output_on_llm_response(FakeEvent(), response)
        assert response.completion_text == original  # 逐字不变
        for _ in range(5):
            await asyncio.sleep(0)  # 让 fire-and-forget 后台检查跑完（不阻塞主回复）

    asyncio.run(flow())
    assert any(r["side"] == "output" for r in judge.records())


def test_t10_rewrite_once_and_fallback_to_original(tmp_path):
    """T10：rewrite 最多重写 1 次；判断失败/无效修正 → 放行原回复。"""
    plugin, _main = make_plugin(tmp_path)
    write_plugin_cfg(plugin, judge_config(tmp_path, output_action="rewrite"))
    original = "嗯嗯好的，我等下就去看看那个报错，你先把日志发我一下呗。"

    # 成功路径：修正被应用
    judge = judge_of(
        plugin,
        ['{"ok": false, "note": "有点啰嗦", "fixed": "好的，我等下看看那个报错。"}'],
        tmp_path,
    )
    response = _fake_response(original)
    asyncio.run(plugin.judge_output_on_llm_response(FakeEvent(), response))
    assert response.completion_text == "好的，我等下看看那个报错。"
    assert plugin.context.calls == []  # rewrite 走 judge.llm_call，不经 llm_generate
    assert len(judge.records()) == 2  # output + rewrite 两条记录

    # 护栏 1：无有效修正 → 放行原文（仍然只调用了 1 次判断）
    judge2 = judge_of(
        plugin,
        ['{"ok": false, "note": "有问题", "fixed": ""}'],
        tmp_path,
    )
    response2 = _fake_response(original)
    asyncio.run(plugin.judge_output_on_llm_response(FakeEvent(), response2))
    assert response2.completion_text == original

    # 护栏 2：修正超长（>原文 1.2 倍 = 整条重写）→ 放行原文
    judge3 = judge_of(
        plugin,
        ['{"ok": false, "note": "试试重写", "fixed": original + "新增一大段自作主张的内容" * 3}'],
        tmp_path,
    )
    response3 = _fake_response(original)
    asyncio.run(plugin.judge_output_on_llm_response(FakeEvent(), response3))
    assert response3.completion_text == original

    # 护栏 3：判断调用异常 → 放行原文（不能让用户收不到消息）
    judge4 = judge_of(plugin, [RuntimeError("judge down")], tmp_path)
    response4 = _fake_response(original)
    asyncio.run(plugin.judge_output_on_llm_response(FakeEvent(), response4))
    assert response4.completion_text == original


# ---------------------------------------------------------------------------
# D 组：提示词搬面板（T11 逐字一致 / T12 兜底 / schema 对照）
# ---------------------------------------------------------------------------
MOOD = "心情平静，精力一般"


def test_t11_decider_params_game_prompt_verbatim():
    """T11-D1a：hybrid game 选题 prompt 默认值与搬前硬编码逐字相等。"""
    captured = {}

    async def llm(prompt, system_prompt=None):
        captured["prompt"] = prompt
        return '{"style": "像素风"}'

    class Mood:
        def digest(self):
            return MOOD

    decider = ActivityDecider(
        activities=[], config_getter=lambda: {}, llm_call=llm, mood=Mood()
    )

    class GameActivity:
        name = "game"

    asyncio.run(decider._params_for(GameActivity()))
    expected = (
        f"你现在打算写个小游戏自己玩。你现在的状态：{MOOD}。\n"
        '顺着状态选一个具体的小游戏风格。只输出 JSON，格式：{"style": "…"}'
    )
    assert captured["prompt"] == expected


class _TopicsMood:
    """mood 替身：digest 固定 + recent_topics_list 可控。"""

    def __init__(self, topics):
        self._topics = topics

    def digest(self, with_interests=True):
        return MOOD

    def recent_topics_list(self):
        return list(self._topics)


def _memory_backend(rows):
    class Backend:
        async def search(self, query, k=5):
            return rows

    return Backend()


def test_t11_decider_params_browse_prompt_verbatim_two_branches():
    """T11-D1b：hybrid surf/read 选题 prompt——无近期话题与有近期话题
    （归并指令+directions JSON）两分支都与搬前硬编码逐字相等。"""
    captured = []

    async def llm(prompt, system_prompt=None):
        captured.append(prompt)
        return '{"topic": "咖啡"}'

    decider = ActivityDecider(
        activities=[], config_getter=lambda: {}, llm_call=llm,
        mood=_TopicsMood([]),
    )

    class SurfActivity:
        name = "surf"

    asyncio.run(decider._params_for(SurfActivity()))
    mood = MOOD
    assert captured[0] == (
        f"你现在打算上网冲浪（搜索）。你现在的状态：{mood}。\n"
        "顺着状态选一个具体、有生活气息的主题词，"
        "选一个你最近没碰过的方向，越新鲜越好。\n"
        '只输出 JSON，格式：{"topic": "…"}'
    )

    # 有近期话题（方向缓存未命中 → 原始清单 + 归并指令 + directions 规格）
    decider2 = ActivityDecider(
        activities=[], config_getter=lambda: {}, llm_call=llm,
        mood=_TopicsMood(["咖啡"]),
    )
    asyncio.run(decider2._params_for(SurfActivity()))
    assert captured[1] == (
        f"你现在打算上网冲浪（搜索）。你现在的状态：{mood}。\n"
        f"{decider2._raw_topics_section(('咖啡',))}\n"
        "顺着状态选一个具体、有生活气息的主题词，"
        "选一个你最近没碰过的方向，越新鲜越好。\n"
        "顺带把你最近折腾过的话题归并成不超过 4 个方向。\n"
        '只输出 JSON，格式：{"topic": "…", "directions": ["方向×出现次数", "…"]}'
    )


def _make_llm_decider(topics, memories, activities, free_choice_ratio=None):
    from core.activities import default_activities

    captured = {}

    async def llm(prompt, system_prompt=None):
        captured["prompt"] = prompt
        return '{"activity": "surf", "params": {"topic": "咖啡"}}'

    cfg = {}
    if free_choice_ratio is not None:
        cfg["decision"] = {"free_choice_ratio": free_choice_ratio}
    decider = ActivityDecider(
        activities=activities or default_activities(),
        config_getter=lambda: cfg,
        llm_call=llm,
        mood=_TopicsMood(topics),
        memory_getter=lambda: asyncio.sleep(0, result=_memory_backend(
            [{"content": m} for m in memories]
        )),
        rng=__import__("random").Random(1),
    )
    return decider, captured


def test_t11_decider_llm_interest_prompt_verbatim():
    """T11-D1c：llm 档兴趣局选题 prompt（无近期话题/无探索触发的基础
    形态）与搬前硬编码逐字相等。"""
    from core.activities import default_activities

    decider, captured = _make_llm_decider(
        [], ["9月7日我看了场日落"], default_activities(), free_choice_ratio=0.0
    )
    asyncio.run(decider._llm_decide())
    activity_lines = "\n".join(
        f"- {a.name}: {a.description}" for a in decider._effective_activities()
    )
    expected = (
        "现在是你的独处时间，没有人在找你，可以自己决定干点什么。\n\n"
        f"你现在的状态：{MOOD}\n\n"
        "最近记得的事：\n- 9月7日我看了场日落\n"
        "\n"
        "\n"
        f"可以做的活动：\n{activity_lines}\n\n"
        ""
        "请选一个你现在最想做的活动，并给它合适参数（topic 为主题词，"
        "style 为小游戏风格，peek 和 reminisce 不需要参数）。"
        "如果上面列了你最近反复折腾的话题，这次避开它们。\n"
        '只输出 JSON，格式：{"activity": "…", "params": {"topic": "…"}}'
    )
    assert captured["prompt"] == expected


def test_t11_decider_llm_free_prompt_verbatim():
    """T11-D1d：llm 档自由局选题 prompt 与搬前硬编码逐字相等（含
    free_choice_ratio=1.0 强制自由局）。"""
    from core.activities import default_activities

    decider, captured = _make_llm_decider(
        ["咖啡"], [], default_activities(), free_choice_ratio=1.0
    )
    asyncio.run(decider._llm_decide())
    activity_lines = "\n".join(
        f"- {a.name}: {a.description}" for a in decider._effective_activities()
    )
    expected = (
        "现在是你的独处时间，没有人在找你，可以自己决定干点什么。\n\n"
        f"你现在的状态：{MOOD}\n\n"
        "最近记得的事：\n（还没什么记忆）\n"
        "\n这些方向最近都碰过了：咖啡（1 次）。"
        "这次想一个和它们都不同的新方向。\n"
        "\n"
        f"可以做的活动：\n{activity_lines}\n\n"
        ""
        "请选一个你现在最想做的活动，并给它合适参数（topic 为主题词，"
        "style 为小游戏风格，peek 和 reminisce 不需要参数）。"
        "这次凭当下的好奇心自由发挥，不用考虑平时的兴趣方向。\n"
        + "顺带把你最近折腾过的话题归并成不超过 4 个方向。\n"
        + '只输出 JSON，格式：{"activity": "…", "params": {"topic": "…"}, '
        '"directions": ["方向×出现次数", "…"]}'
    )
    assert captured["prompt"] == expected


def test_t11_initiative_prompts_verbatim():
    """T11-D2：搭话话题提取与台词生成 prompt 与搬前硬编码逐字相等。"""
    captured = {}

    async def llm(prompt, system_prompt=None):
        captured["prompt"] = prompt
        captured.setdefault("count", 0)
        captured["count"] += 1
        return "天气" if captured["count"] == 1 else "在忙吗，突然想到你"

    contexts = [
        {"role": "user", "content": "今天天气不错"},
        {"role": "assistant", "content": "是啊，想出去走走"},
    ]
    engine = InitiativeEngine(
        config_getter=lambda: {},
        gate=None,
        llm_call=llm,
        contexts_getter=lambda: contexts,
        session_getter=lambda: "sess",
    )
    # open_topic（经 _extract_topic 调用路径）
    topic = asyncio.run(engine._extract_topic())
    assert topic
    expected_topic = (
        "下面是你和用户最近的聊天记录（节选）：\n"
        "用户：今天天气不错\n你：是啊，想出去走走"
        '\n\n从中找一个"可以自然接上、继续聊下去"的话题，'
        "用一句短语概括（20 字以内）。\n"
        "要求：必须是还没聊完的话题；不能是需要对方回答的追问；"
        "不要重复已经聊完了的话题。\n"
        "如果没有合适的话题，只输出 NONE。"
    )
    assert captured["prompt"] == expected_topic

    # line（经 _generate_line，final_review 开/关两态）
    line_cfg = {"final_review_enabled": True}
    asyncio.run(engine._generate_line(line_cfg, NOW, None))
    expected_line_on = (
        f"当前时间：10月6日 14:05。\n"
        "没有什么特别的事由，就是忽然想找用户说句话。\n\n"
        "写一句你主动发给用户的话。要求：\n"
        "- 用你自己的口吻，30 字以内\n"
        '- 这是主动搭话，不是回答对方：不要"你说""发过来"这类回应式措辞，'
        "不要问对方要任何东西，不要催促\n"
        "- 像朋友间随口聊天：不要标题、列表、Markdown、链接，"
        "只输出这句话本身\n- 如果此刻其实不该说话、或者这话不像你会说的，只输出 SKIP"
    )
    assert captured["prompt"] == expected_line_on

    asyncio.run(engine._generate_line({"final_review_enabled": False}, NOW, None))
    expected_line_off = expected_line_on[: -len(
        "\n- 如果此刻其实不该说话、或者这话不像你会说的，只输出 SKIP"
    )]
    assert captured["prompt"] == expected_line_off


def _bare_loop(**attrs):
    """object.__new__ 构造 LivingLoop，只注入目标方法用到的属性。"""
    loop = object.__new__(LivingLoop)
    # M31-补丁1：主动产出质检回调缺省 None（不质检）
    attrs.setdefault("_proactive_qc", None)
    for key, value in attrs.items():
        setattr(loop, key, value)
    return loop


class _CaptureLLM:
    def __init__(self, reply):
        self.reply = reply
        self.prompts = []

    async def __call__(self, prompt, system_prompt=None):
        self.prompts.append(prompt)
        return self.reply


class _Sender:
    def __init__(self):
        self.sent = []

    async def send(self, session, text):
        self.sent.append((session, text))
        return True


class _Gate:
    def awake_standby_active(self, now=None):
        return False


class _MoodDigest:
    def __init__(self, text):
        self.text = text

    def digest(self):
        return self.text


def test_t11_farewell_prompt_verbatim():
    """T11-D3：晚安 LLM 档 prompt 与搬前硬编码逐字相等。"""
    llm = _CaptureLLM("晚安")
    now = datetime(2026, 10, 6, 23, 30)

    def fake_sessions():
        return (["s1"], "test")

    async def fake_contexts(sessions):
        return None  # → chat_block = "今天还没和用户聊过天。"

    async def fake_persona():
        return None

    async def fake_write(*args, **kwargs):
        return None

    loop = _bare_loop(
        _dream_llm_call=llm,
        _mood=_MoodDigest(MOOD),
        _sender=_Sender(),
        _resolve_target_sessions=fake_sessions,
        _load_chat_contexts=fake_contexts,
        _persona_getter=fake_persona,
        _write_speech_to_stores=fake_write,
        _config_getter=lambda: {},
    )
    loop._farewell_session = lambda: "sess"

    asyncio.run(loop._farewell_llm_mode(now))
    expected = (
        f"现在是 {now.strftime('%Y-%m-%d %H:%M')}，你准备去睡了。"
        f"你现在的状态：{MOOD}。\n\n今天还没和用户聊过天。\n\n"
        "考虑一下今晚要不要跟对方道声晚安：如果今天聊得开心、被关心，"
        "就自然地道声晚安；如果今天有不愉快、你还在气头上，可以不说；"
        "如果你想缓和关系，也可以借这句晚安说点什么。"
        "像人一样自己斟酌，不是每次都非说不可。\n"
        "如果决定不说，只输出 SKIP；决定说就只输出晚安那句话本身"
        "（一两句、口语化，不要任何前缀和引号）。"
    )
    assert llm.prompts == [expected]


class _Manager:
    def __init__(self, messages):
        self._messages = messages

    async def take_pending_messages(self):
        return self._messages


def test_t11_wake_reply_prompt_verbatim_both_moods():
    """T11-D4a：醒来补回复 prompt——无心境与有心境两态都逐字相等。"""
    now = datetime(2026, 10, 6, 8, 15)
    messages = [
        {"at": "2026-10-06T22:30:00", "text": "早睡点"},
        {"at": "2026-10-06T23:00:00", "text": "明天记得吃早饭"},
    ]

    async def fake_write(*args, **kwargs):
        return None

    # 无心境（mood=None）
    llm = _CaptureLLM("SKIP")
    loop = _bare_loop(
        _sleep_manager=_Manager(messages),
        _dream_llm_call=llm,
        _sender=_Sender(),
        _gate=_Gate(),
        _mood=None,
        _config_getter=lambda: {"sleep": {"pending_reply_enabled": True}},
        _write_speech_to_stores=fake_write,
    )
    asyncio.run(loop._settle_pending_replies(now, actual_h=7.5))
    expected = (
        "你刚睡醒。你睡着的时候收到了这些消息（当时你在睡，没回）：\n"
        "- [22:30] 早睡点\n- [23:00] 明天记得吃早饭"
        "\n\n你睡了 7.5 个小时，现在是 10月6日 8:15。"
        ""
        "\n\n想想现在要不要回、怎么回：\n"
        "- 如果已经不用回了（话题早过去了/只是随口一说/现在突然回"
        "反而奇怪）→ 只输出 SKIP\n"
        "- 如果值得正经回应 → 第一行写 REPLY，第二行开始写你要发的话\n"
        "- 如果轻轻带一句就好 → 第一行写 BRIEF，第二行开始写你要发的"
        "话（一句轻描淡写的，比如\"昨晚睡着了，你说的那个我看看哈\"）\n"
        "用你自己的口吻，像刚睡醒看到手机消息那样自然。"
        "只输出上述内容之一。"
    )
    assert llm.prompts == [expected]

    # 有心境（mood digest 非空 → "你现在的状态：…" 紧跟在时间句号后）
    llm2 = _CaptureLLM("SKIP")
    loop2 = _bare_loop(
        _sleep_manager=_Manager(messages),
        _dream_llm_call=llm2,
        _sender=_Sender(),
        _gate=_Gate(),
        _mood=_MoodDigest(MOOD),
        _config_getter=lambda: {"sleep": {"pending_reply_enabled": True}},
        _write_speech_to_stores=fake_write,
    )
    asyncio.run(loop2._settle_pending_replies(now, actual_h=7.5))
    # 有心境：心境句紧跟时间句号后；去掉心境句后应与无心境期望串逐字相等
    assert (
        "你睡了 7.5 个小时，现在是 10月6日 8:15。你现在的状态：" in llm2.prompts[0]
    )
    assert llm2.prompts[0].replace(
        "你现在的状态：" + MOOD + "\n\n想想", "\n\n想想"
    ) == expected


class _MemBackend:
    def __init__(self, rows):
        self._rows = rows
        self.added = []

    async def search(self, query, k=3):
        return self._rows

    async def add(self, *args, **kwargs):
        self.added.append(args)


def test_t11_dream_prompt_verbatim_both_styles():
    """T11-D4b：梦话 prompt——无/有风格注入两态都逐字相等。"""
    now = datetime(2026, 10, 6, 7, 40)
    memory = _MemBackend([{"content": "记忆碎片一"}, {"content": "记忆碎片二"}])

    class FixedRng:
        def random(self):
            return 0.0  # 恒命中 dream_probability

    async def memory_getter():
        return memory

    async def identity():
        return None

    async def persona_id():
        return None

    async def no_share(*args, **kwargs):
        return None  # 梦话分享链路不在本测试范围（prompt 捕获为止）

    def session_id(x):
        return "sess"

    # 无风格注入（style_learner=None → _style_hint 返回 ""）
    llm = _CaptureLLM("梦话内容")
    loop = _bare_loop(
        _dream_llm_call=llm,
        _config_getter=lambda: {"sleep": {"dream_probability": 0.3}},
        _rng=FixedRng(),
        _get_memory=memory_getter,
        _style_learner=None,
        _bot_identity=identity,
        _persona_id=persona_id,
        _session_id=session_id,
        _maybe_share=no_share,
    )
    asyncio.run(loop._maybe_dream(now))
    expected = (
        "你刚从睡梦中醒来，还带着睡意。下面是你最近的记忆碎片：\n"
        "- 记忆碎片一\n- 记忆碎片二"
        "\n\n请说一句你刚才做的梦，80 字以内，第一人称，语气朦胧含糊，"
        "把碎片搅在一起也没关系，梦本来就是不讲道理的。只输出梦话本身。"
    )
    assert llm.prompts == [expected]

    # 有风格注入（"\n" + style_hint 追加）
    class Learner:
        def inject_block(self, now=None):
            return "说话别拖泥带水"

    llm2 = _CaptureLLM("梦话内容")
    loop2 = _bare_loop(
        _dream_llm_call=llm2,
        _config_getter=lambda: {"sleep": {"dream_probability": 0.3}},
        _rng=FixedRng(),
        _get_memory=memory_getter,
        _style_learner=Learner(),
        _bot_identity=identity,
        _persona_id=persona_id,
        _session_id=session_id,
        _maybe_share=no_share,
    )
    asyncio.run(loop2._maybe_dream(now))
    assert llm2.prompts[0] == expected + "\n说话别拖泥带水"


def test_t11_distill_prompt_verbatim():
    """T11-D5：风格提炼 prompt 与搬前硬编码逐字相等（来源备注空值兜底
    '网页'）。"""
    learner = object.__new__(StyleLearner)
    learner._config_getter = lambda: {}

    material = "评论区的口语文本片段"
    got = learner._distill_prompt(material, "")
    expected = (
        "下面是你昨天在网上读到的文本片段"
        "（来源备注：网页）。请完成两件事：\n\n"
        "一、判断它更可能是【人写的】还是【AI 生成的】。判断依据：\n"
        "- AI 特征：结构工整对称、排比铺陈、\"首先/其次/最后\"、无口语"
        "碎语、情绪平铺、无具体到细节的个人经历、套话（\"值得注意的是\""
        "\"让我们一起\"\"希望这对你有帮助\"）、标点规范到不像真人、"
        "段落长度均匀\n"
        "- 人味特征：口语与短句跳跃、错别字/口误/语气词、情绪起伏、"
        "跑题与打断、具体生活细节（时间地点人物）、自嘲与黑话\n\n"
        "二、只有判定为\"人写的\"才做：从中提炼值得学习的说话风格。"
        "六个维度都要看一遍（没有的给空串），其中【思维方式】与"
        "【待人接物】是重点——不要只盯着用词，要看这个人怎么想问题、"
        "怎么跟人打交道：\n"
        "- wording：用词口癖（高频词/语气词）\n"
        "- syntax：句式习惯\n"
        "- thinking：思维方式（先看什么、怎么得出结论）\n"
        "- emotion_style：情绪表达方式\n"
        "- interaction：待人接物（怎么接话、怎么打岔、怎么表达不同意）\n"
        "- avoid：反面特征（这类文本里不出现的）\n\n"
        "source 字段：dialogue=多人在互相说话（评论区、回复串、问答"
        "往来）；article=单作者的长文。拿不准按 article。\n\n"
        "严格输出一个 JSON 对象，不要输出任何其他文字：\n"
        '{"kind": "human", "source": "dialogue", "dims": {'
        '"wording": "", "syntax": "", "thinking": "", '
        '"emotion_style": "", "interaction": "", "avoid": ""}}\n'
        "kind 只能是 human / ai / uncertain；kind 是 ai 或 uncertain 时 "
        "dims 给空对象。\n\n"
        f"文本片段：\n{material}"
    )
    assert got == expected

    # 带来源备注
    got2 = learner._distill_prompt(material, "某评论区")
    assert "（来源备注：某评论区）" in got2


def _bare_ctx(params=None, recent=None, config_getter=None):
    return ActivityContext(
        searcher=None,
        fetcher=None,
        sandbox=None,
        memory=None,
        gate=None,
        event=None,
        rng=None,
        params=params or {},
        recent_topics=recent or [],
        config_getter=config_getter,
    )


def test_t11_activity_intents_verbatim():
    """T11-D6：四个活动意图模板——无参数与有参数/近期话题两种形态都
    与搬前硬编码逐字相等（free 含无工具形态）。"""
    # surf：无 hint 无近期话题
    got = SurfActivity().agent_intent(_bare_ctx())
    assert got == (
        "你现在打算上网冲浪。主题你自己挑。"
        "用 web_search 搜一搜，挑一两条结果看看，"
        "最后用几句话汇报你看到了什么、有什么想法。"
    )
    # surf：有 hint + 近期话题
    got2 = SurfActivity().agent_intent(
        _bare_ctx(params={"topic": "咖啡"}, recent=["咖啡"])
    )
    assert got2 == (
        "你现在打算上网冲浪。主题方向：咖啡。"
        "你最近读过/搜过的主题：咖啡（1 次）。"
        "这次请选一个和它们明显不同的方向。"
        "用 web_search 搜一搜，挑一两条结果看看，"
        "最后用几句话汇报你看到了什么、有什么想法。"
    )
    # read
    got3 = ReadArticleActivity().agent_intent(_bare_ctx())
    assert got3 == (
        "你现在打算读一篇文章。主题你自己挑。"
        "用 web_search 搜索，挑一条你最想读的，用 fetch_page 认真读完，"
        "然后用自己的话总结要点，再说一点你的感想。"
    )
    # game：无风格
    got4 = MiniGameActivity().agent_intent(_bare_ctx())
    assert got4 == (
        "你现在打算写个小游戏自己玩。玩法你自己发挥。"
        "用 run_python 现场写一个秒级能跑完的小游戏"
        "（只能用 random/math/time/datetime/json/re/itertools/collections，"
        "记得 print 出结果），跑一跑，说说结果和你的心得。"
    )
    # game：有风格
    got5 = MiniGameActivity().agent_intent(_bare_ctx(params={"style": "像素"}))
    assert got5.startswith("你现在打算写个小游戏自己玩。风格想法：像素。")
    # free：无工具（searcher 等全缺）
    got6 = FreeActivity().agent_intent(_bare_ctx())
    assert got6 == (
        "现在是完全的自由活动时间，。"
        "你可以使用这些工具：当前没有可用工具。"
        "就在心里想一件有意思的小事，最后告诉我你想了什么。"
    )


def test_t11_panel_override_changes_prompt(tmp_path):
    """T11 补充：面板覆盖生效——改 decision.prompt_intent_surf 后
    agent_intent 用新模板（模板覆盖语义真的工作）。"""
    cfg = {"decision": {"prompt_intent_surf": "自定义冲浪意图 {topic_line}"}}
    got = SurfActivity().agent_intent(_bare_ctx(config_getter=lambda: cfg))
    assert got == "自定义冲浪意图 主题你自己挑。"


def test_t12_empty_template_and_missing_placeholders(tmp_path, caplog):
    """T12：模板为空 → 回落默认（不崩溃）；缺占位符 → 按空串渲染 +
    WARNING；未知 {xxx} 原样保留（JSON 花括号安全）。"""
    # 空模板回落默认
    got = render_template("   ", {"mood": "x"}, name="t.k", default="默认模板内容")
    assert got == "默认模板内容"
    # read_template 空配置值 → 默认（share_rewrite_prompt 同口径）
    assert read_template({"prompt": ""}, "prompt", "默认") == "默认"
    assert read_template({}, "prompt", "默认") == "默认"
    assert read_template(None, "prompt", "默认") == "默认"

    # 缺占位符：对应段消失，其余照渲染，不崩溃
    with caplog.at_level(logging.WARNING):
        got2 = render_template(
            "开头{mood}结尾", {"mood": "心情", "missing_one": "zzz"},
            name="t.k2", default="D",
        )
    assert got2 == "开头心情结尾"
    assert any("missing_one" in r.getMessage() for r in caplog.records)

    # 未知 {xxx} 原样保留（JSON 字面花括号不受影响）
    got3 = render_template(
        '保持 {"json": true} 与 {unknown}，注入 {value}',
        {"value": "V"}, name="t.k3", default="D",
    )
    assert got3 == '保持 {"json": true} 与 {unknown}，注入 V'

    # 判定逻辑兜底：清空面板模板 → decider 仍能出 prompt（用默认）
    decider = ActivityDecider(
        activities=[],
        config_getter=lambda: {"decision": {"prompt_decide_params_game": ""}},
        llm_call=None,
        mood=_TopicsMood([]),
    )

    class Game:
        name = "game"

    # llm_call None → _params_for 直接返回 {}，不崩（决策层容错先例）
    assert asyncio.run(decider._params_for(Game())) == {}


def test_schema_prompt_defaults_match_code_constants():
    """D 组守护：schema 里全部提示词 default 与代码内默认常量逐字相等
    （防止以后改代码忘了 schema 或反之）。"""
    adv = SCHEMA["advanced"]["items"]
    pairs = [
        (adv["decision"]["items"]["prompt_decide_params_game"], "core.decider",
         "DEFAULT_PROMPT_DECIDE_PARAMS_GAME"),
        (adv["decision"]["items"]["prompt_decide_params_browse"], "core.decider",
         "DEFAULT_PROMPT_DECIDE_PARAMS_BROWSE"),
        (adv["decision"]["items"]["prompt_decide_llm_free"], "core.decider",
         "DEFAULT_PROMPT_DECIDE_LLM_FREE"),
        (adv["decision"]["items"]["prompt_decide_llm_interest"], "core.decider",
         "DEFAULT_PROMPT_DECIDE_LLM_INTEREST"),
        (adv["decision"]["items"]["prompt_intent_surf"], "core.activities",
         "DEFAULT_PROMPT_INTENT_SURF"),
        (adv["decision"]["items"]["prompt_intent_read"], "core.activities",
         "DEFAULT_PROMPT_INTENT_READ"),
        (adv["decision"]["items"]["prompt_intent_game"], "core.activities",
         "DEFAULT_PROMPT_INTENT_GAME"),
        (adv["decision"]["items"]["prompt_intent_free"], "core.activities",
         "DEFAULT_PROMPT_INTENT_FREE"),
        (adv["decision"]["items"]["prompt_intent_surf_browse"], "core.activities",
         "DEFAULT_PROMPT_INTENT_SURF_BROWSE"),
        (adv["decision"]["items"]["prompt_intent_read_browse"], "core.activities",
         "DEFAULT_PROMPT_INTENT_READ_BROWSE"),
        (adv["initiative"]["items"]["prompt_open_topic"], "core.initiative",
         "DEFAULT_PROMPT_OPEN_TOPIC"),
        (adv["initiative"]["items"]["prompt_line"], "core.initiative",
         "DEFAULT_PROMPT_LINE"),
        (adv["sleep"]["items"]["prompt_farewell"], "core.living_loop",
         "DEFAULT_PROMPT_FAREWELL"),
        (adv["sleep"]["items"]["prompt_wake_reply"], "core.living_loop",
         "DEFAULT_PROMPT_WAKE_REPLY"),
        (adv["sleep"]["items"]["prompt_dream"], "core.living_loop",
         "DEFAULT_PROMPT_DREAM"),
        (adv["style_learning"]["items"]["prompt_distill"], "core.style_learning",
         "DEFAULT_PROMPT_DISTILL"),
        (adv["judge"]["items"]["prompt_input"], "core.judge",
         "DEFAULT_PROMPT_INPUT"),
        (adv["judge"]["items"]["inject_template"], "core.judge",
         "DEFAULT_INJECT_TEMPLATE"),
        (adv["judge"]["items"]["prompt_output"], "core.judge",
         "DEFAULT_PROMPT_OUTPUT"),
    ]
    import importlib

    for item, mod_name, const_name in pairs:
        module = importlib.import_module(mod_name)
        assert item["default"] == getattr(module, const_name), (
            f"{mod_name}.{const_name} 与 schema default 不一致"
        )
        assert item["type"] == "text"


# ---------------------------------------------------------------------------
# E 组：判断记录
# ---------------------------------------------------------------------------
def test_e1_records_roundtrip_and_limit(tmp_path):
    """E1：记录落盘 + 重启（新实例）可回读；record_limit 热读裁剪。"""
    cfg = {"judge": {"mode": "api", "provider_id": "p", "record_limit": 3}}
    path = tmp_path / "judge_records.json"
    judge = OutputJudge(
        config_getter=lambda: cfg, llm_call=None, records_path=path, clock=lambda: NOW
    )
    for i in range(5):
        judge._add_record(
            side="input", input_summary=f"消息{i}", verdict="chat/normal/plain",
            injected=bool(i % 2),
        )
    assert len(judge.records()) == 3  # 热读裁剪
    # 新实例（模拟重启）读回
    judge2 = OutputJudge(
        config_getter=lambda: cfg, llm_call=None, records_path=path, clock=lambda: NOW
    )
    rows = judge2.records()
    assert len(rows) == 3
    assert rows[0]["input_summary"] == "消息4"  # 新的在前


def test_e3_records_endpoint(tmp_path):
    """E1/E3：面板端点 judge_records 返回最近记录。"""
    plugin, _main = make_plugin(tmp_path)
    write_plugin_cfg(plugin, judge_config(tmp_path))
    judge = judge_of(plugin, [], tmp_path)
    judge._add_record(side="input", input_summary="你好", verdict="chat/normal", injected=True)

    async def flow():
        return await plugin._api_judge_records_get()

    result = asyncio.run(flow())
    assert result["status"] == "ok"
    assert result["data"]["records"][0]["input_summary"] == "你好"


# ---------------------------------------------------------------------------
# F 组：面板控件（T13-T16）
# ---------------------------------------------------------------------------
def test_t13_enum_fields_render_as_select():
    """T13：五个枚举字段（+judge 两枚举）——schema 有 options，前端有
    通用下拉分支与中文标签映射。"""
    app_js = (WORKDIR / "pages" / "config" / "app.js").read_text(encoding="utf-8")
    # F4 硬性：string+options 一律下拉（通用分支存在）
    assert 't === "string" && Array.isArray(item.options)' in app_js
    assert 'document.createElement("select")' in app_js
    # 五个字段在 schema 里都带 options
    adv = SCHEMA["advanced"]["items"]
    for group, key in [
        ("decision", "decision_mode"), ("sleep", "farewell_mode"),
        ("sleep", "wake_source"), ("capabilities", "agent_tools_mode"),
        ("memory", "backend"), ("judge", "mode"), ("judge", "output_action"),
    ]:
        assert adv[group]["items"][key].get("options"), f"{group}.{key} 缺 options"
    # 中文标签映射齐全（显示标签、保存原始值）
    for field in [
        "decision.decision_mode", "sleep.farewell_mode", "sleep.wake_source",
        "capabilities.agent_tools_mode", "memory.backend",
    ]:
        assert field in app_js


def test_t14_agent_activities_multiselect_with_labels():
    """T14：agent_activities 多选——6 个活动选项带中文说明，勾选保存数组。"""
    app_js = (WORKDIR / "pages" / "config" / "app.js").read_text(encoding="utf-8")
    for name in ("surf", "read", "game", "peek", "reminisce", "free"):
        assert f'value: "{name}"' in app_js
    assert "AGENT_ACTIVITY_CHOICES" in app_js
    assert 'box.type = "checkbox"' in app_js
    # 保存仍是数组（F4：类型不变）
    assert "set(next)" in app_js
    # schema 默认仍是 list（不是逗号串）
    default_activities_value = SCHEMA["advanced"]["items"]["decision"]["items"][
        "agent_activities"
    ]["default"]
    assert isinstance(default_activities_value, list)


def test_t15_control_types_preserve_value_types():
    """T15：控件类型变化不改变已存值类型——多选存数组、下拉存字符串、
    权重存 object；已有值不在选项里时原样保留（不规范化）。"""
    app_js = (WORKDIR / "pages" / "config" / "app.js").read_text(encoding="utf-8")
    # 下拉：越界值保留额外选项（不"顺手规范化"）
    assert "choices.push(value)" in app_js
    # judgeCard 的 provider 下拉同样保留不可用值
    assert "当前不可用" in app_js
    # source_weights 保存仍是 object（set(next) 传对象）
    assert "SOURCE_WEIGHT_FIELDS" in app_js
    assert "const next = { ...current };" in app_js
    # fallback_chain 保存仍是数组
    assert "set([...stateList])" in app_js


def test_t16_fallback_chain_options_from_providers(tmp_path):
    """T16：fallback_chain 选项来自已配置 provider——后端 payload 注入
    providers，前端用 state.providers 生成选项。"""
    payload = build_config_payload(
        {"preset": {}, "advanced": {}}, SCHEMA, providers=["p1", "p2"]
    )
    assert payload["providers"] == ["p1", "p2"]
    # 缺省 None → 空列表（旧调用点零改动）
    payload2 = build_config_payload({"preset": {}, "advanced": {}}, SCHEMA)
    assert payload2["providers"] == []
    # 前端锚点
    app_js = (WORKDIR / "pages" / "config" / "app.js").read_text(encoding="utf-8")
    assert "state.providers = payload.providers || []" in app_js
    assert "添加 provider…" in app_js
    # 插件侧从 context.get_all_providers 取 id 清单
    main_js = inspect.getsource(_load_plugin_main().LivingPlugin._judge_provider_ids)
    assert "get_all_providers" in main_js


def test_judge_group_schema_complete():
    """A/B/C 组配置完整性：judge 组 14 键齐备且默认值符合任务书
    （M31-补丁1：+timeout_output_seconds/include_persona；上文默认 6→4）。"""
    items = SCHEMA["advanced"]["items"]["judge"]["items"]
    expected = {
        "mode": ("off", ["off", "local", "api"]),
        "provider_id": ("", None),
        "local_model_path": ("", None),
        "local_backend": ("", None),
        "context_messages": (4, None),
        "min_interval_seconds": (20, None),
        "timeout_seconds": (6, None),
        "output_action": ("log_only", ["log_only", "rewrite"]),
        "record_limit": (50, None),
        "timeout_output_seconds": (10, None),
        "include_persona": (False, None),
        "prompt_input": (DEFAULT_PROMPT_INPUT, None),
        "inject_template": (DEFAULT_INJECT_TEMPLATE, None),
        "prompt_output": (DEFAULT_PROMPT_OUTPUT, None),
    }
    assert set(items) == set(expected)
    for key, (default, options) in expected.items():
        assert items[key]["default"] == default, key
        if options is not None:
            assert items[key]["options"] == options, key
