"""M3 补丁 XVII 测试：配置减重 / L1 措辞三改 / L2.5 方向归并（含回退路径）。"""

import asyncio
import json
import re
from datetime import datetime
from pathlib import Path

from core.activities import default_activities
from core.decider import ActivityDecider, _clean_directions
from core.mood import MoodState

NOW = datetime(2026, 9, 19, 14, 0, 0)

WORKDIR = Path(__file__).resolve().parents[1]

DEAD_KEYS = [
    ("capabilities", "enable_search"),
    ("capabilities", "enable_fetch"),
    ("capabilities", "enable_sandbox"),
    ("capabilities", "enable_send"),
    ("capabilities", "sandbox_max_memory_mb"),
    ("sleep", "fatigue_threshold"),
    ("memory", "default_importance"),
    ("memory", "recall_count"),
    ("misc", "log_level"),
]


# ---------------------------------------------------------------------------
# 验收 1/清单 1-3：schema 减重
# ---------------------------------------------------------------------------
def test_schema_dead_keys_removed():
    """9 个死键从 schema 消失；misc 组整组删除；其余组不留空壳。"""
    schema = json.loads((WORKDIR / "_conf_schema.json").read_text(encoding="utf-8"))
    for group, key in DEAD_KEYS:
        if group == "misc":
            assert group not in schema  # 只剩死键的组整组删
            continue
        assert key not in schema[group]["items"], f"{group}.{key} 应已删除"
        assert schema[group]["items"], f"{group} 组不应是空壳"
    # fatigue_rate_per_hour 保留但移出 UI
    rate = schema["sleep"]["items"]["fatigue_rate_per_hour"]
    assert rate.get("invisible") is True
    assert rate["default"] == 4.0  # 代码兜底值与 schema 默认一致


def test_schema_decay_default_lowered():
    """清单 8：interest_daily_decay 默认 0.9 → 0.7。"""
    schema = json.loads((WORKDIR / "_conf_schema.json").read_text(encoding="utf-8"))
    assert schema["decision"]["items"]["interest_daily_decay"]["default"] == 0.7


def test_production_code_no_dead_key_reads():
    """9 个死键在生产代码（core/ + main.py）零读取（grep 的单测化）。"""
    sources = list((WORKDIR / "core").glob("*.py")) + [WORKDIR / "main.py"]
    needles = [
        "enable_search", "enable_fetch", "enable_sandbox", "enable_send",
        "sandbox_max_memory_mb", "fatigue_threshold", "default_importance",
        "recall_count", "log_level",
    ]
    for path in sources:
        text = path.read_text(encoding="utf-8")
        for needle in needles:
            assert needle not in text, f"{path.name} 不应再引用 {needle}"


def test_add_fatigue_removed():
    """清单 2：MoodState.add_fatigue 死方法删除，全库零命中。"""
    assert not hasattr(MoodState, "add_fatigue")
    for path in (WORKDIR / "core").glob("*.py"):
        assert "add_fatigue" not in path.read_text(encoding="utf-8")
    assert "add_fatigue" not in (WORKDIR / "main.py").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 验收 3/4：L1-a 与 L1-b 措辞
# ---------------------------------------------------------------------------
class _ScriptRng:
    """脚本化 rng：按活动名出队（rules_pick 强制选指定活动，确保走 topic 链路）。"""

    def __init__(self, choices=None):
        self._choices = list(choices or [])

    def choice(self, seq):
        target = self._choices.pop(0) if self._choices else seq[0].name
        for item in seq:
            if item.name == target:
                return item
        return seq[0]

    def random(self):
        return 0.5


def _make_decider(llm=None, mood=None, life_extra=None, config=None, choices=None):
    return ActivityDecider(
        activities=default_activities(),
        config_getter=lambda: config or {"decision": {"decision_mode": "hybrid"}},
        rng=_ScriptRng(choices or ["surf"]),
        llm_call=llm,
        mood=mood,
        life_extra_getter=(lambda: life_extra) if life_extra is not None else None,
    )


def test_system_prompt_uses_background_wording():
    """L1-a：life_extra 引导语降为"背景参考"，不再出现"生活补充设定"。"""

    async def flow():
        decider = _make_decider(life_extra="对技术、宇宙、咖啡、独立游戏感兴趣")
        return await decider._system_prompt()

    text = asyncio.run(flow())
    assert "背景参考" in text
    assert "不必围绕它选题" in text
    assert "生活补充设定" not in text
    assert "咖啡" in text  # 内容本身原样保留


def test_digest_interest_line_softened(tmp_path):
    """L1-b：兴趣行只报 top2、无数值、措辞降级；digest 全文无小数数字。"""

    async def flow():
        mood = MoodState(db_path=str(tmp_path / "mood.db"))
        await mood.load()
        mood.valence = -0.6
        mood.energy = 0.2
        for topic in ("咖啡科学", "记忆术", "独立游戏", "发酵化学"):
            mood.bump_interest(topic, 0.8)
        text = mood.digest()
        await mood.close()
        return text

    text = asyncio.run(flow())
    assert "低落" in text and "累了" in text
    # 兴趣行：最多 2 项、无数值、"偶尔/浅尝"措辞
    assert "最近偶尔在琢磨的方向：" in text
    interest_part = text.split("最近偶尔在琢磨的方向：")[1]
    assert "咖啡科学" in interest_part and "记忆术" in interest_part
    assert "独立游戏" not in interest_part  # top2 之外的项不出现
    assert "未必延续" in interest_part
    assert "最近对这些有兴趣" not in text  # 旧措辞消失
    assert not re.search(r"\d+\.\d+", text)  # 全文无数值（含旧 valence 数字）
    assert "valence" not in text


# ---------------------------------------------------------------------------
# 验收 5：L1-c topic prompt 新鲜度半句
# ---------------------------------------------------------------------------
def test_hybrid_topic_prompt_demands_fresh_direction():
    captured = {}

    async def llm(prompt, system_prompt=None):
        captured["prompt"] = prompt
        return '{"topic": "陶艺", "directions": ["咖啡×2", "记忆术×2"]}'

    mood = _TopicsMood(["coffee extraction science", "memory palace techniques"])
    decider = _make_decider(llm=llm, mood=mood)
    decision = asyncio.run(decider.decide(NOW))
    assert decision.activity.name in ("surf", "read")  # 脚本化 rng 首个
    assert "没碰过的方向" in captured["prompt"]
    assert "越新鲜越好" in captured["prompt"]


# ---------------------------------------------------------------------------
# 验收 6/7：L2.5 方向归并——正常路径与失败回退
# ---------------------------------------------------------------------------
class _TopicsMood:
    """带 recent_topics 的假心境（digest 输出可控、兴趣表可注入）。"""

    def __init__(self, topics, interests=None):
        self._topics = list(topics)
        self.interests = dict(interests or {})
        self.energy = 0.5
        self.valence = 0.2

    def digest(self):
        return "测试心境"

    def recent_topics_list(self):
        return list(self._topics)


def test_direction_merge_normal_path_caches_and_injects():
    """首决策注入原始清单并缓存归并；同 topics 再决策改注入方向级表述。"""
    prompts = []
    responses = iter([
        '{"topic": "陶艺入门", "directions": ["咖啡/发酵×2", "记忆术×2"]}',
        '{"topic": "中世纪航海史", "directions": ["咖啡/发酵×2", "记忆术×2"]}',
    ])

    async def llm(prompt, system_prompt=None):
        prompts.append(prompt)
        return next(responses)

    mood = _TopicsMood([
        "coffee extraction science", "memory palace techniques",
        "memory palace techniques", "fermentation chemistry",
    ])
    decider = _make_decider(llm=llm, mood=mood)

    asyncio.run(decider.decide(NOW))  # 第一次：无缓存 → 原始清单
    assert "你最近已经折腾过这些话题" in prompts[0]
    assert "coffee extraction science" in prompts[0]
    # 缓存已建立（清洗后 2 个方向）
    assert decider._direction_cache is not None
    assert decider._direction_cache[1] == ["咖啡/发酵×2", "记忆术×2"]

    asyncio.run(decider.decide(NOW))  # 第二次：同 topics → 方向级表述
    assert "你最近折腾过的方向：咖啡/发酵×2、记忆术×2——这次挑一个完全不同的方向。" in prompts[1]
    assert "coffee extraction science" not in prompts[1]  # 原始清单被方向级替代


def test_direction_merge_llm_mode_also_merges():
    """llm 档同样并入决策调用：注入方向级表述、缓存归并结果。"""
    prompts = []
    responses = iter([
        '{"activity": "read", "params": {"topic": "陶艺"}, '
        '"directions": ["咖啡×2", "记忆术×2"]}',
        '{"activity": "surf", "params": {"topic": "航海史"}, '
        '"directions": ["咖啡×2", "记忆术×2"]}',
    ])

    async def llm(prompt, system_prompt=None):
        prompts.append(prompt)
        return next(responses)

    mood = _TopicsMood(["coffee extraction science", "memory palace techniques"])
    decider = _make_decider(
        llm=llm, mood=mood,
        config={"decision": {"decision_mode": "llm"}},
    )

    first = asyncio.run(decider.decide(NOW))
    assert first.mode == "llm"
    assert "你最近已经折腾过这些话题" in prompts[0]  # 回退形态
    second = asyncio.run(decider.decide(NOW))
    assert second.mode == "llm"
    assert "你最近折腾过的方向：咖啡×2、记忆术×2" in prompts[1]


def test_direction_merge_garbage_llm_falls_back():
    """验收 7：LLM 返回垃圾 → 决策不中断、缓存不建立、下轮回退原始清单。"""
    prompts = []

    async def llm(prompt, system_prompt=None):
        prompts.append(prompt)
        return "我觉得今天想看点天文学的东西（没有 JSON）"

    mood = _TopicsMood(["coffee extraction science", "memory palace techniques"])
    decider = _make_decider(llm=llm, mood=mood, choices=["surf"])

    decision = asyncio.run(decider.decide(NOW))
    assert decider._direction_cache is None  # 没建立缓存
    assert decision.params == {}  # 参数为空（活动内部回退随机主题）

    decision2 = asyncio.run(decider.decide(NOW))
    assert decision2 is not None  # 决策链路不中断
    assert decision2.params == {}
    assert "你最近已经折腾过这些话题" in prompts[1]  # 回退原始清单注入


def test_direction_merge_timeout_falls_back():
    """验收 7：LLM 超时/异常 → _safe_llm 吞掉，hybrid 照常出活动（参数为空）。"""

    async def llm(prompt, system_prompt=None):
        raise TimeoutError("llm timeout")

    mood = _TopicsMood(["coffee extraction science"])
    decider = _make_decider(llm=llm, mood=mood, choices=["surf"])
    decision = asyncio.run(decider.decide(NOW))
    assert decider._direction_cache is None
    assert decision.mode == "hybrid"  # hybrid 参数失败不回退 rules、不阻塞
    assert decision.params == {}
    assert decision.activity.name == "surf"


def test_direction_merge_bad_directions_keeps_old_cache():
    """directions 字段垃圾（非 list/全空）→ 缓存保留旧值，注入仍走方向级。"""
    prompts = []
    responses = iter([
        '{"topic": "陶艺", "directions": ["咖啡×2", "记忆术×2"]}',
        '{"topic": "航海史", "directions": "不是列表"}',
    ])

    async def llm(prompt, system_prompt=None):
        prompts.append(prompt)
        return next(responses)

    mood = _TopicsMood(["coffee extraction science", "memory palace techniques"])
    decider = _make_decider(llm=llm, mood=mood)
    asyncio.run(decider.decide(NOW))
    asyncio.run(decider.decide(NOW))
    # 第二次 directions 垃圾 → 旧缓存保留；第三次注入仍是方向级
    assert decider._direction_cache[1] == ["咖啡×2", "记忆术×2"]

    async def llm3(prompt, system_prompt=None):
        prompts.append(prompt)
        return '{"topic": "天文学"}'

    decider._llm_call = llm3
    prompts.clear()
    asyncio.run(decider.decide(NOW))
    assert "你最近折腾过的方向：咖啡×2、记忆术×2" in prompts[0]


def test_direction_cache_invalidated_when_topics_change():
    """topics 变化 → 指纹失配 → 回退原始清单（缓存不误用）。"""
    prompts = []
    responses = iter([
        '{"topic": "陶艺", "directions": ["咖啡×2"]}',
        '{"topic": "航海史", "directions": ["航海×1"]}',
    ])

    async def llm(prompt, system_prompt=None):
        prompts.append(prompt)
        return next(responses)

    mood = _TopicsMood(["coffee extraction science"])
    decider = _make_decider(llm=llm, mood=mood)
    asyncio.run(decider.decide(NOW))
    mood._topics = ["medieval maritime history"]  # 话题换谱系
    asyncio.run(decider.decide(NOW))
    assert "你最近已经折腾过这些话题" in prompts[1]  # 指纹失配 → 原始清单
    assert decider._direction_cache[0] == ("medieval maritime history",)
    assert decider._direction_cache[1] == ["航海×1"]  # 新归并覆盖


def test_no_recent_topics_no_injection():
    """无近期话题（新装/清空）→ 不注入任何近期段，归并不触发。"""
    prompts = []

    async def llm(prompt, system_prompt=None):
        prompts.append(prompt)
        return '{"topic": "陶艺"}'

    mood = _TopicsMood([])
    decider = _make_decider(llm=llm, mood=mood)
    asyncio.run(decider.decide(NOW))
    assert "折腾过" not in prompts[0]  # 近期段与归并指令都不出现
    assert "directions" not in prompts[0]  # JSON 示例也不带归并字段
    assert decider._direction_cache is None


def test_clean_directions_normalization():
    """方向词清洗：非 list/空串/去重/超长截断/数量上限。"""
    assert _clean_directions("不是列表") == []
    assert _clean_directions(None) == []
    assert _clean_directions([]) == []
    assert _clean_directions(["", "  "]) == []
    assert _clean_directions(["咖啡×2", "咖啡×2", "记忆术×2"]) == ["咖啡×2", "记忆术×2"]
    long_item = "超长方向词" * 10
    cleaned = _clean_directions([long_item])
    assert len(cleaned[0]) <= 24
    assert len(_clean_directions([f"方向{i}" for i in range(10)])) == 4


def test_rules_mode_untouched_by_direction_merge():
    """rules 档零 LLM 成本，方向归并完全不参与。"""
    calls = {"n": 0}

    async def llm(prompt, system_prompt=None):
        calls["n"] += 1
        return "{}"

    mood = _TopicsMood(["coffee extraction science"])
    decider = _make_decider(
        llm=llm, mood=mood,
        config={"decision": {"decision_mode": "rules"}},
    )
    decision = asyncio.run(decider.decide(NOW))
    assert calls["n"] == 0
    assert decision.mode == "rules"
    assert decider._direction_cache is None
