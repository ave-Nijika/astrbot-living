"""决策层三档测试（任务书 M2-B/C/D）：mock LLM 各路径 + 回退 + prompt 组装。"""

import asyncio
import random
from datetime import datetime
from typing import Any

from core.activities import default_activities
from core.decider import ActivityDecider, extract_json_object
from core.mood import MoodState

NOW = datetime(2026, 9, 8, 14, 0, 0)


class FakeRng:
    """同时支持 choice 与 random 的脚本化 rng（decider 两种路径都会用）。"""

    def __init__(self, choices=None, randoms=None):
        self._choices = list(choices or [])
        self._randoms = list(randoms or [])

    def choice(self, seq):
        target = self._choices.pop(0) if self._choices else seq[0]
        for item in seq:
            if item.name == target:
                return item
        return seq[0]

    def random(self):
        if self._randoms:
            return self._randoms.pop(0)
        return 0.5


class FakeMood:
    def __init__(self, energy=0.5, valence=0.2):
        self.energy = energy
        self.valence = valence

    def digest(self):
        return f"测试心境 energy={self.energy}"


def make_decider(config=None, llm=None, rng=None, mood=None,
                 persona=None, life_extra=None, memory=None, activities=None):
    return ActivityDecider(
        activities=activities if activities is not None else default_activities(),
        config_getter=lambda: config if config is not None else {},
        rng=rng or FakeRng(),
        llm_call=llm,
        mood=mood,
        persona_getter=persona,
        life_extra_getter=(lambda: life_extra) if life_extra is not None else None,
        memory_getter=(lambda: asyncio.sleep(0, result=memory)) if memory else None,
    )


CONFIG_HYBRID = {"decision": {"decision_mode": "hybrid"}}
CONFIG_RULES = {"decision": {"decision_mode": "rules"}}
CONFIG_LLM = {"decision": {"decision_mode": "llm"}}


# ---------------------------------------------------------------------------
# JSON 提取
# ---------------------------------------------------------------------------
def test_extract_json_variants():
    assert extract_json_object('{"topic": "咖啡"}') == {"topic": "咖啡"}
    assert extract_json_object('好的！```json\n{"topic": "咖啡"}\n```') == {"topic": "咖啡"}
    assert extract_json_object('废话 {"activity": "surf", "params": {}} 尾巴') == {
        "activity": "surf",
        "params": {},
    }
    assert extract_json_object("没有任何 json") is None
    assert extract_json_object("{bad json") is None
    assert extract_json_object("") is None
    assert extract_json_object(None) is None


# ---------------------------------------------------------------------------
# rules 档
# ---------------------------------------------------------------------------
def test_rules_mode_returns_activity_without_llm():
    calls = {"n": 0}

    async def llm(prompt, system_prompt=None):
        calls["n"] += 1
        return '{"topic": "x"}'

    decider = make_decider(config=CONFIG_RULES, llm=llm, rng=FakeRng(choices=["read"]))
    decision = asyncio.run(decider.decide(NOW))
    assert decision.mode == "rules"
    assert decision.activity.name == "read"
    assert decision.params == {}
    assert calls["n"] == 0  # rules 档零 LLM 成本


def test_rules_avoid_repeat():
    rng = FakeRng(choices=["game", "game", "game"])
    decider = make_decider(config=CONFIG_RULES, rng=rng)
    first = decider.rules_pick()  # rules_pick 是同步方法
    second = decider.rules_pick()
    assert first.name == "game"
    assert second.name != "game"


def test_rules_mood_bias_low_energy_prefers_quiet():
    """energy < 0.3：reminisce 权重 2、game 权重 1——同一点位选出的活动不同。"""
    pool = [a for a in default_activities() if a.name in ("game", "reminisce")]
    tired = make_decider(
        config=CONFIG_RULES, mood=FakeMood(energy=0.1), activities=pool
    )
    # point=0.5*3=1.5：game(cum=1) 落空 → reminisce(cum=3) 命中
    pick = tired._weighted_choice(pool, [tired._weight_for(a) for a in pool])
    assert pick.name == "reminisce"

    energetic = make_decider(
        config=CONFIG_RULES, mood=FakeMood(energy=0.9), activities=pool
    )
    # point=0.5*3=1.5：game 权重 2，cum=2 ≥ 1.5 → game 命中
    pick2 = energetic._weighted_choice(pool, [energetic._weight_for(a) for a in pool])
    assert pick2.name == "game"


def test_rules_without_mood_uniform():
    """无心境时权重全 1，走等权 choice 路径（兼容 M1 脚本化 rng）。"""
    pool = default_activities()[:2]
    decider = make_decider(config=CONFIG_RULES, rng=FakeRng(choices=["read"]))
    assert decider._weighted_choice(pool, [1.0, 1.0]).name == "read"


# ---------------------------------------------------------------------------
# hybrid 档
# ---------------------------------------------------------------------------
def test_hybrid_llm_topic_injected():
    """hybrid：rules 选活动，LLM 给主题词，进 params。"""

    async def llm(prompt, system_prompt=None):
        return '{"topic": "深海生物"}'

    decider = make_decider(
        config=CONFIG_HYBRID, llm=llm, rng=FakeRng(choices=["surf"])
    )
    decision = asyncio.run(decider.decide(NOW))
    assert decision.mode == "hybrid"
    assert decision.activity.name == "surf"
    assert decision.params == {"topic": "深海生物"}


def test_hybrid_llm_failure_falls_back_to_empty_params():
    """LLM 挂掉/返回垃圾：params 为空不阻塞活动（活动内部回退随机主题）。"""

    async def broken(prompt, system_prompt=None):
        raise RuntimeError("provider down")

    decider = make_decider(config=CONFIG_HYBRID, llm=broken, rng=FakeRng(choices=["surf"]))
    decision = asyncio.run(decider.decide(NOW))
    assert decision.activity.name == "surf"
    assert decision.params == {}

    decider2 = make_decider(
        config=CONFIG_HYBRID,
        llm=lambda p, s=None: asyncio.sleep(0, result="我觉得吧，今天天气不错"),
        rng=FakeRng(choices=["read"]),
    )
    decision2 = asyncio.run(decider2.decide(NOW))
    assert decision2.activity.name == "read"
    assert decision2.params == {}  # 解析不出 JSON → 回退


def test_hybrid_game_style_param():
    async def llm(prompt, system_prompt=None):
        return '```json\n{"style": "骰子"}\n```'

    decider = make_decider(config=CONFIG_HYBRID, llm=llm, rng=FakeRng(choices=["game"]))
    decision = asyncio.run(decider.decide(NOW))
    assert decision.params == {"style": "骰子"}


def test_hybrid_prompt_contains_persona_life_extra_and_mood():
    """hybrid 的 LLM 输入应带上人格 + 生活补充 + 心境摘要（总纲 D4 拼接序）。"""
    captured = {}

    async def llm(prompt, system_prompt=None):
        captured["prompt"] = prompt
        captured["system"] = system_prompt

        async def _fake():
            return '{"topic": "x"}'

        return '{"topic": "x"}'

    async def persona():
        return "你是一只住在机器人里的猫。"

    # energy=0.5 不触发 rules 倾向加权（等权走 choice 路径），脚本化选 surf
    mood = FakeMood(energy=0.5)
    decider = make_decider(
        config=CONFIG_HYBRID,
        llm=llm,
        rng=FakeRng(choices=["surf"]),
        mood=mood,
        persona=persona,
        life_extra="# 生活补充设定\n- 深夜有精神",
    )
    asyncio.run(decider.decide(NOW))
    assert "你是一只住在机器人里的猫" in captured["system"]
    assert "深夜有精神" in captured["system"]
    assert "测试心境" in captured["prompt"]
    assert "主题词" in captured["prompt"]


def test_hybrid_persona_failure_silent():
    """persona 读取抛异常：静默跳过，决策照常出参数。"""

    async def bad_persona():
        raise RuntimeError("persona mgr gone")

    async def llm(prompt, system_prompt=None):
        assert system_prompt is None  # persona 失败后 system_prompt 为空
        return '{"topic": "ok"}'

    decider = make_decider(
        config=CONFIG_HYBRID, llm=llm, rng=FakeRng(choices=["surf"]), persona=bad_persona
    )
    decision = asyncio.run(decider.decide(NOW))
    assert decision.params == {"topic": "ok"}


def test_hybrid_peek_skips_llm():
    """peek 没有可参数化的部分：不应浪费一次 LLM 调用。"""
    calls = {"n": 0}

    async def llm(prompt, system_prompt=None):
        calls["n"] += 1
        return None

    decider = make_decider(config=CONFIG_HYBRID, llm=llm, rng=FakeRng(choices=["peek"]))
    decision = asyncio.run(decider.decide(NOW))
    assert decision.activity.name == "peek"
    assert calls["n"] == 0


# ---------------------------------------------------------------------------
# llm 档
# ---------------------------------------------------------------------------
def test_llm_mode_selects_activity_and_params():
    async def llm(prompt, system_prompt=None):
        return '{"activity": "read", "params": {"topic": "咖啡文化"}}'

    decider = make_decider(config=CONFIG_LLM, llm=llm)
    decision = asyncio.run(decider.decide(NOW))
    assert decision.mode == "llm"
    assert decision.activity.name == "read"
    assert decision.params == {"topic": "咖啡文化"}


def test_llm_mode_prompt_contains_memories_and_activities():
    """llm 档输入 = 人设+心境+近期记忆+活动清单。"""
    captured = {}

    class FakeMemory:
        async def search(self, query, k=5):
            return [
                {"content": "9月7日我看了场日落"},
                {"content": "9月6日我读了《三体》"},
            ]

    async def llm(prompt, system_prompt=None):
        captured["prompt"] = prompt
        return '{"activity": "reminisce", "params": {}}'

    async def memory_getter():
        return FakeMemory()

    decider = make_decider(
        config=CONFIG_LLM, llm=llm, mood=FakeMood(), memory=FakeMemory()
    )
    decision = asyncio.run(decider.decide(NOW))
    assert decision.activity.name == "reminisce"
    assert "9月7日我看了场日落" in captured["prompt"]
    assert "reminisce" in captured["prompt"]
    assert "独处时间" in captured["prompt"]


def test_llm_mode_invalid_activity_falls_back_to_rules():
    async def llm(prompt, system_prompt=None):
        return '{"activity": "环游世界", "params": {}}'

    decider = make_decider(config=CONFIG_LLM, llm=llm, rng=FakeRng(choices=["surf"]))
    decision = asyncio.run(decider.decide(NOW))
    assert decision.mode == "rules"
    assert decision.note == "llm_failed_fallback_rules"
    assert decision.activity.name == "surf"


def test_llm_mode_garbage_falls_back_to_rules():
    async def llm(prompt, system_prompt=None):
        return "我今天不太想动。"

    decider = make_decider(config=CONFIG_LLM, llm=llm, rng=FakeRng(choices=["game"]))
    decision = asyncio.run(decider.decide(NOW))
    assert decision.mode == "rules"
    assert decision.activity.name == "game"


def test_llm_mode_llm_missing_falls_back():
    decider = make_decider(config=CONFIG_LLM, llm=None, rng=FakeRng(choices=["read"]))
    decision = asyncio.run(decider.decide(NOW))
    assert decision.mode == "rules"


def test_bad_mode_config_defaults_to_hybrid():
    decider = make_decider(
        config={"decision": {"decision_mode": "胡写"}}, llm=None, rng=FakeRng(choices=["peek"])
    )
    decision = asyncio.run(decider.decide(NOW))
    assert decision.mode == "hybrid"


def test_decider_internal_error_falls_back():
    """决策器内部意外：回退 rules，绝不让"想一想"卡住生活。"""
    decider = make_decider(config=CONFIG_HYBRID, rng=FakeRng(choices=["peek"]))
    decider._params_for = lambda activity: (_ for _ in ()).throw(RuntimeError("boom"))
    decision = asyncio.run(decider.decide(NOW))
    assert decision.mode == "rules"
    assert "error_fallback" in decision.note


def test_llm_params_overlong_truncated():
    async def llm(prompt, system_prompt=None):
        return '{"topic": "' + "很" * 100 + '长的主题"}'

    decider = make_decider(config=CONFIG_HYBRID, llm=llm, rng=FakeRng(choices=["surf"]))
    decision = asyncio.run(decider.decide(NOW))
    assert len(decision.params["topic"]) == 30  # 主题词不是文章
