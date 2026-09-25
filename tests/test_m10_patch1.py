"""M10-补丁1 测试：选题兴趣配额（50/50 硬保证）。

主人诉求：兴趣一半、自由发挥一半——从"LLM 自觉"改成"代码保证"。
掷骰子复用 decider 既有 rng 注入点（random.Random 实例，脚本化替身
替换后完全可控）；兴趣局 prompt 为对照基线逐字不变。
"""

import asyncio
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.activities import default_activities  # noqa: E402
from core.decider import ActivityDecider  # noqa: E402
from core.mood import MoodState  # noqa: E402

WORKDIR = Path(__file__).resolve().parent.parent

INTEREST_LINE = "最近偶尔在琢磨的方向："
FREE_TAIL = "这次凭当下的好奇心自由发挥，不用考虑平时的兴趣方向"
AVOID_TAIL = "如果上面列了你最近反复折腾的话题，这次避开它们"
NEW_DIRECTION_DIRECTIVE = "这次想一个和它们都不同的新方向"


class _ScriptRng:
    """脚本化 rng 替身：random() 按脚本吐值（choice 兼容 rules 档）。"""

    def __init__(self, values):
        self._values = list(values)
        self._i = 0

    def random(self):
        value = self._values[self._i % len(self._values)]
        self._i += 1
        return value

    def choice(self, seq):
        return seq[0]


class _CaptureLlm:
    """捕获 prompt、返回固定 JSON 的决策 LLM 替身（零新增调用点）。"""

    def __init__(self, payload):
        self._payload = payload
        self.prompts = []

    async def __call__(self, prompt, system_prompt=None):
        self.prompts.append(prompt)
        return json.dumps(self._payload, ensure_ascii=False)


async def _make_mood(tmp_path, interests=("星际咖啡", "像素游戏")):
    mood = MoodState(db_path=str(tmp_path / "mood.db"))
    await mood.load()
    for topic in interests:
        mood.bump_interest(topic, 0.8)
    mood.record_recent_topics(["星际咖啡", "像素游戏", "星际咖啡"])
    return mood


def _make_decider(mood, llm, rng, config=None):
    return ActivityDecider(
        activities=default_activities(),
        config_getter=lambda: config
        if config is not None
        else {"decision": {"decision_mode": "llm"}},
        rng=rng,
        llm_call=llm,
        mood=mood,
    )


def _payload(topic="深海热泉", directions=None):
    data = {"activity": "surf", "params": {"topic": topic}}
    if directions:
        data["directions"] = directions
    return data


# ---------------------------------------------------------------------------
# D1/D2：掷骰子可控与边界
# ---------------------------------------------------------------------------
def test_free_roll_controlled(tmp_path):
    """验收#1：ratio=0.5 时 rng=0.3 → 自由局；rng=0.7 → 兴趣局。"""

    async def flow():
        mood = await _make_mood(tmp_path)
        try:
            free_llm = _CaptureLlm(_payload())
            await _make_decider(mood, free_llm, _ScriptRng([0.3]))._llm_decide()
            interest_llm = _CaptureLlm(_payload())
            await (
                _make_decider(mood, interest_llm, _ScriptRng([0.7]))
                ._llm_decide()
            )
            return free_llm.prompts[0], interest_llm.prompts[0]
        finally:
            await mood.close()

    free_prompt, interest_prompt = asyncio.run(flow())
    assert FREE_TAIL in free_prompt and NEW_DIRECTION_DIRECTIVE in free_prompt
    assert INTEREST_LINE not in free_prompt
    assert AVOID_TAIL in interest_prompt and INTEREST_LINE in interest_prompt


def test_roll_boundary_around_half(tmp_path):
    """边界 0.5 两侧：0.5 本身 → 兴趣局（< 严格小于），0.49 → 自由局。"""

    async def flow():
        mood = await _make_mood(tmp_path)
        try:
            at_half = _CaptureLlm(_payload())
            await _make_decider(mood, at_half, _ScriptRng([0.5]))._llm_decide()
            just_below = _CaptureLlm(_payload())
            await (
                _make_decider(mood, just_below, _ScriptRng([0.49]))
                ._llm_decide()
            )
            return at_half.prompts[0], just_below.prompts[0]
        finally:
            await mood.close()

    at_half_prompt, just_below_prompt = asyncio.run(flow())
    assert AVOID_TAIL in at_half_prompt  # 0.5 < 0.5 为 False → 兴趣局
    assert FREE_TAIL in just_below_prompt


def test_ratio_zero_always_interest_ratio_one_always_free(tmp_path):
    """验收#2：ratio=0 恒兴趣局；ratio=1 恒自由局（rng 取极端也一致）。"""

    async def flow():
        mood = await _make_mood(tmp_path)
        try:
            cfg0 = {"decision": {"decision_mode": "llm", "free_choice_ratio": 0}}
            zero_llm = _CaptureLlm(_payload())
            await (
                _make_decider(mood, zero_llm, _ScriptRng([0.0]), cfg0)
                ._llm_decide()
            )
            cfg1 = {"decision": {"decision_mode": "llm", "free_choice_ratio": 1}}
            one_llm = _CaptureLlm(_payload())
            await (
                _make_decider(mood, one_llm, _ScriptRng([0.999]), cfg1)
                ._llm_decide()
            )
            return zero_llm.prompts[0], one_llm.prompts[0]
        finally:
            await mood.close()

    zero_prompt, one_prompt = asyncio.run(flow())
    assert INTEREST_LINE in zero_prompt and AVOID_TAIL in zero_prompt
    assert INTEREST_LINE not in one_prompt and FREE_TAIL in one_prompt


def test_roll_ratio_read_failure_falls_back_to_half(tmp_path):
    """红线 5：配置读取失败/键缺失 → 回落 0.5，决策不中断。"""

    class _BrokenConfig:
        def __call__(self):
            raise RuntimeError("config boom")

    async def flow():
        mood = await _make_mood(tmp_path)
        try:
            below = _CaptureLlm(_payload())
            decider = ActivityDecider(
                activities=default_activities(),
                config_getter=_BrokenConfig(),
                rng=_ScriptRng([0.4]),
                llm_call=below,
                mood=mood,
            )
            await decider._llm_decide()
            above = _CaptureLlm(_payload())
            decider = ActivityDecider(
                activities=default_activities(),
                config_getter=_BrokenConfig(),
                rng=_ScriptRng([0.6]),
                llm_call=above,
                mood=mood,
            )
            await decider._llm_decide()
            return below.prompts[0], above.prompts[0]
        finally:
            await mood.close()

    below_prompt, above_prompt = asyncio.run(flow())
    assert FREE_TAIL in below_prompt  # 0.4 < 0.5（回落值）→ 自由局
    assert INTEREST_LINE in above_prompt  # 0.6 ≥ 0.5 → 兴趣局


def test_roll_statistics_seed42_deterministic():
    """报告需求 3 的统计验证（测试化）：固定种子 20 连跑分布可控。

    Random(42) 确定性：两次连跑结果完全一致；ratio=0.5 下自由局数量
    落在 [5, 15]（半数的宽松带）。
    """

    def run_once():
        decider = ActivityDecider(
            activities=default_activities(),
            config_getter=lambda: {"decision": {"decision_mode": "llm"}},
            rng=random.Random(42),
            llm_call=None,
            mood=None,
        )
        return [decider._free_choice_roll() for _ in range(20)]

    first, second = run_once(), run_once()
    assert first == second  # 同种子完全可复现（VM 统计的前提）
    assert 5 <= sum(first) <= 15
    assert sum(first) == 11  # Random(42) 的实际分布（锁定回归基线）


# ---------------------------------------------------------------------------
# D3：prompt 隔离（兴趣局为逐字基线、自由局无兴趣牵引）
# ---------------------------------------------------------------------------
def test_interest_branch_prompt_verbatim_baseline(tmp_path):
    """D3/红线 3：兴趣局 prompt 与历史结构逐字一致（关键子串全锁定）。

    代码层面：git diff 显示兴趣局构造语句原样保留在 else 分支（仅缩进）。
    """
    from core.conf_path import conf_group

    async def flow():
        mood = await _make_mood(tmp_path)
        try:
            llm = _CaptureLlm(_payload())
            await _make_decider(mood, llm, _ScriptRng([0.9]))._llm_decide()
            return llm.prompts[0]
        finally:
            await mood.close()

    prompt = asyncio.run(flow())
    assert prompt.startswith("现在是你的独处时间，没有人在找你，可以自己决定干点什么。")
    assert "你现在的状态：" in prompt
    assert "最近记得的事：" in prompt
    assert "你最近已经折腾过这些话题（太多了会腻）：" in prompt
    assert "星际咖啡（2 次）、像素游戏（1 次）" in prompt
    assert "可以做的活动：" in prompt
    assert AVOID_TAIL in prompt


def test_interest_branch_prompt_verbatim_baseline_with_fingerprint(tmp_path):
    """兴趣局在有近期话题时的完整形态（归并指令 + directions JSON）。"""

    async def flow():
        mood = await _make_mood(tmp_path)
        try:
            llm = _CaptureLlm(
                _payload(directions=["星际主题", "像素游戏", "深海", "咖啡"])
            )
            await _make_decider(mood, llm, _ScriptRng([0.9]))._llm_decide()
            return llm.prompts[0]
        finally:
            await mood.close()

    prompt = asyncio.run(flow())
    assert "顺带把你最近折腾过的话题归并成不超过 4 个方向。" in prompt
    assert (
        '只输出 JSON，格式：{"activity": "…", "params": {"topic": "…"}, '
        '"directions": ["方向×出现次数", "…"]}' in prompt
    )
    assert "（2 次）" in prompt


def test_free_branch_prompt_strips_all_interest_pull(tmp_path):
    """D3/B3：自由局 prompt 三处牵引全部撤除，新指令就位。"""

    async def flow():
        mood = await _make_mood(tmp_path)
        try:
            llm = _CaptureLlm(
                _payload(directions=["星际主题", "像素游戏", "深海", "咖啡"])
            )
            await _make_decider(mood, llm, _ScriptRng([0.3]))._llm_decide()
            return llm.prompts[0]
        finally:
            await mood.close()

    prompt = asyncio.run(flow())
    # 1) digest 兴趣行不出现（含兴趣名与弱化措辞）
    assert INTEREST_LINE not in prompt
    assert "浅尝过，未必延续" not in prompt
    # 2) recent 段变成正向新方向指令
    assert NEW_DIRECTION_DIRECTIVE in prompt
    assert "太多了会腻" not in prompt
    # 3) 结尾避开句换成自由发挥句
    assert AVOID_TAIL not in prompt
    assert FREE_TAIL in prompt
    # 归并与 JSON 照旧（B2 保留项）
    assert "顺带把你最近折腾过的话题归并成不超过 4 个方向。" in prompt
    # 近期话题清单仍在（作为"想新方向"的对照清单）
    assert "星际咖啡（2 次）" in prompt


# ---------------------------------------------------------------------------
# D4：自由局走完整决策链路
# ---------------------------------------------------------------------------
def test_free_branch_produces_valid_decision_and_directions(tmp_path):
    """验收#4：自由局产出合法 Decision（活动/参数/directions 归并不回归）。"""

    async def flow():
        mood = await _make_mood(tmp_path)
        try:
            decider = _make_decider(
                mood,
                _CaptureLlm(
                    _payload(topic="深海热泉", directions=["深海", "热泉生态"])
                ),
                _ScriptRng([0.3]),
            )
            decision = await decider._llm_decide()
            return decision, decider._direction_cache
        finally:
            await mood.close()

    decision, cache = asyncio.run(flow())
    assert decision is not None
    assert decision.activity.name == "surf"
    assert decision.params == {"topic": "深海热泉"}
    assert decision.mode == "llm"
    # directions 归并缓存照常更新（B2 保留项）
    fingerprint, directions = cache
    assert directions == ["深海", "热泉生态"]
    assert "星际咖啡" in fingerprint and "像素游戏" in fingerprint


def test_free_branch_direction_cache_update(tmp_path):
    """directions 归并在自由局照常入库（方向缓存键 = recent 指纹）。"""

    async def flow():
        mood = await _make_mood(tmp_path)
        try:
            decider = _make_decider(
                mood, _CaptureLlm(_payload(directions=["深海", "热泉"])),
                _ScriptRng([0.3]),
            )
            decision = await decider._llm_decide()
            return decision, decider._direction_cache
        finally:
            await mood.close()

    decision, cache = asyncio.run(flow())
    assert decision.activity.name == "surf"
    fingerprint, directions = cache
    assert "星际咖啡" in fingerprint and "像素游戏" in fingerprint
    assert directions == ["深海", "热泉"]


# ---------------------------------------------------------------------------
# D5：digest 参数化
# ---------------------------------------------------------------------------
def test_digest_with_interests_false_only_drops_interest_line(tmp_path):
    """验收#5：with_interests=False 与 True 的差异仅为兴趣行有无。"""

    async def flow():
        mood = await _make_mood(tmp_path)
        try:
            mood.fatigue = 70
            mood.sleep_debt = 50
            with_i = mood.digest()
            without_i = mood.digest(with_interests=False)
            return with_i, without_i
        finally:
            await mood.close()

    with_i, without_i = asyncio.run(flow())
    # True 模式 = False 模式 + 兴趣行后缀（其余逐字一致——C2）
    assert with_i.startswith(without_i)
    assert with_i.endswith("最近偶尔在琢磨的方向：星际咖啡、像素游戏（浅尝过，未必延续）")
    assert INTEREST_LINE not in without_i
    assert "身体有些疲惫" in without_i and "最近没睡好" in without_i
    # 默认参数 = True（既有调用方零改动）
    assert MoodState.digest.__defaults__ == (True,)


def test_schema_free_choice_ratio_key():
    """A1：schema 新键 float / 默认 0.5 / invisible。"""
    schema = json.loads(
        (WORKDIR / "_conf_schema.json").read_text(encoding="utf-8")
    )
    key = schema["advanced"]["items"]["decision"]["items"]["free_choice_ratio"]
    assert key["type"] == "float"
    assert key["default"] == 0.5
    assert key["invisible"] is True
    assert "0=全靠兴趣驱动" in key["hint"]
