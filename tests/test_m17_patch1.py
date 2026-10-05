"""M17-补丁1：风格学习系统 + 睡眠唤醒行为——对应任务书 T1-T17。

A 组：StyleLearner 六维提炼 / AI 检测丢弃 / 单次调用 / 素材库幂等与
淘汰 / 加权取用 / 注入硬上限与静默 / 演化 / read·surf 触发。
C 组：随机吵醒阈值（入睡抽定落盘、不重抽、阶段加权、随机关兼容）/
睡眠期未回消息（有界、只留当前窗口）/ 醒来三档补回复（不回零输出、
回则发送+M16 双写）/ 零消息零调用 / 异常静默。
钩子专项：on_llm_request 只追加不覆盖（与 prompt-preset 共存）、异常吞掉。

测试不写 AstrBot 本体数据目录；真实身份一律用先例假号 10001。
"""

import asyncio
import importlib
import logging
import itertools
import random
import sys
import types
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from core.living_loop import LivingLoop
from core.sleep import SleepManager
from core.style_learning import (
    DIMS_ORDER,
    MIN_MATERIAL_CHARS,
    StyleLearner,
    conversational_score,
    looks_like_code_or_list,
    strip_json_block,
)

MASTER = "aiocqhttp:FriendMessage:10001"  # 先例假号，非真实身份
NOW = datetime(2026, 10, 5, 9, 30, 0)

HUMAN_DISTILL = (
    '{"kind": "human", "source": "dialogue", "dims": {'
    '"wording": "爱说\\u201c说实话\\u201d", "syntax": "多用短句", '
    '"thinking": "先问背景再下判断", "emotion_style": "开心就一句笑死", '
    '"interaction": "被夸就顺着接，不推辞", "avoid": "不用书面连接词"}}'
)
ARTICLE_DISTILL = (
    '{"kind": "human", "source": "article", "dims": {'
    '"wording": "", "syntax": "长句铺垫", "thinking": "从反面举例", '
    '"emotion_style": "", "interaction": "", "avoid": ""}}'
)
AI_DISTILL = '{"kind": "ai", "source": "article", "dims": {}}'
UNCERTAIN_DISTILL = '{"kind": "uncertain", "source": "article", "dims": {}}'

MATERIAL = (
    "楼主这个思路有点东西啊\n"
    "说实话我第一次也没看懂，后来把背景查了一遍才反应过来\n"
    "楼上说反了吧，他明明是先看数据再下的结论\n"
    "笑死，你们俩说的是同一篇论文吗\n"
    "我跟你说，这种事情不能只看结论，过程才有意思"
)


def learner_config(**overrides):
    cfg = {
        "style_learning": {
            "enabled": True,
            "max_inject_chars": 300,
            "max_items_per_pick": 2,
            "pool_limit": 200,
            "decay_days": 14,
        }
    }
    cfg["style_learning"].update(overrides)
    return cfg


class FakeStyleLLM:
    """判定调用替身：按序返回脚本输出，计数调用。"""

    def __init__(self, *responses):
        self._responses = list(responses)
        self.calls = []

    async def __call__(self, prompt, system_prompt=None, **kwargs):
        self.calls.append(prompt)
        if not self._responses:
            raise RuntimeError("no more scripted responses")
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeSleepGate:
    """SleepManager 的 gate 依赖替身：在睡/待机可切换，state 内存实现。"""

    def __init__(self, asleep=True, standby=False):
        self._asleep = asleep
        self._standby = standby
        self.state = {}

    def is_asleep_now(self, now=None):
        return self._asleep

    def awake_standby_active(self, now=None):
        return self._standby

    async def state_get(self, key):
        return self.state.get(key)

    async def state_set(self, key, value):
        self.state[key] = value


class FakeConvMgr:
    async def get_curr_conversation_id(self, umo):
        return "conv-1"

    async def add_message_pair(self, cid, user, asst):
        self.pairs.append((cid, user, asst))


class FakeLM:
    def __init__(self):
        self.added = []

    async def add_message(self, **kwargs):
        self.added.append(kwargs)
        return len(self.added)


class FakeSender:
    def __init__(self):
        self.sent = []

    async def send(self, session, text):
        self.sent.append((session, text))
        return True


def make_learner(*responses, tmp=None, config=None, samples=None,
                 rng=None, search_on=True):
    llm = FakeStyleLLM(*responses)
    learner = StyleLearner(
        config_getter=lambda: config if config is not None else learner_config(),
        llm_call=llm,
        pool_path=str(tmp / "style_pool.json") if tmp is not None else "",
        sample_getter=(lambda: samples) if samples is not None else None,
        rng=rng or random.Random(7),
        now_provider=lambda: NOW,
        search_enabled_getter=lambda: search_on,
    )
    return learner, llm


def sample(text=MATERIAL, url="https://example.com/post/1"):
    return {"url": url, "title": "一个帖子", "text": text, "at": NOW.isoformat()}


# ---------------------------------------------------------------------------
# A 组：T1 六维提炼
# ---------------------------------------------------------------------------
def test_t1_learned_item_has_six_dims():
    """T1：提炼产物六维齐全——thinking 与 interaction 不能只有 wording。"""
    learner, llm = make_learner(HUMAN_DISTILL)
    entry = asyncio.run(learner.distill(MATERIAL, "act_1 某帖", NOW))
    assert entry is not None
    assert set(DIMS_ORDER) <= set(entry["dims"]) or set(entry["dims"]) <= set(DIMS_ORDER)
    assert all(entry["dims"].get(k) for k in
               ("wording", "syntax", "thinking", "emotion_style", "interaction", "avoid"))
    # 重点维度（主人原话"不只口癖"）必须被认真提炼
    assert "判断" in entry["dims"]["thinking"]
    assert "接" in entry["dims"]["interaction"] or "夸" in entry["dims"]["interaction"]


# ---------------------------------------------------------------------------
# A 组：T2 AI 检测三态
# ---------------------------------------------------------------------------
def test_t2_ai_verdict_discarded():
    learner, llm = make_learner(AI_DISTILL)
    entry = asyncio.run(learner.distill(MATERIAL, "act_1", NOW))
    assert entry is None
    assert learner.entries() == []


def test_t2_uncertain_verdict_discarded():
    """uncertain 同样整条丢弃（保守：宁可少学）。"""
    learner, llm = make_learner(UNCERTAIN_DISTILL)
    entry = asyncio.run(learner.distill(MATERIAL, "act_1", NOW))
    assert entry is None
    assert learner.entries() == []


def test_t2_human_verdict_enters_pool():
    learner, llm = make_learner(HUMAN_DISTILL)
    entry = asyncio.run(learner.distill(MATERIAL, "act_1", NOW))
    assert entry is not None
    assert len(learner.entries()) == 1
    assert learner.entries()[0]["source_kind"] == "dialogue"


# ---------------------------------------------------------------------------
# A 组：T3 判定与提炼合并为一次调用
# ---------------------------------------------------------------------------
def test_t3_single_llm_call_per_learn():
    learner, llm = make_learner(HUMAN_DISTILL)
    asyncio.run(learner.distill(MATERIAL, "act_1", NOW))
    assert len(llm.calls) == 1  # 禁止一条文本调用两次（任务书 A3 硬性）


# ---------------------------------------------------------------------------
# A 组：T4 素材库幂等与有界淘汰
# ---------------------------------------------------------------------------
def test_t4_idempotent_same_source_and_fingerprint(tmp_path):
    learner, llm = make_learner(HUMAN_DISTILL, HUMAN_DISTILL, tmp=tmp_path)
    first = asyncio.run(learner.distill(MATERIAL, "act_1", NOW))
    second = asyncio.run(learner.distill(MATERIAL, "act_1", NOW))
    assert first is not None and second is None
    assert len(learner.entries()) == 1
    # 幂等命中在调用 LLM 之前（第二次零成本）
    assert len(llm.calls) == 1
    # 重启（重读磁盘）后幂等依然成立
    learner2, _ = make_learner(tmp=tmp_path)
    third = asyncio.run(learner2.distill(MATERIAL, "act_1", NOW))
    assert third is None
    assert len(learner2.entries()) == 1


def test_t4_eviction_drops_lowest_score(tmp_path):
    """超上限按综合分淘汰：article（权重 0.5）先于 dialogue（权重 1.0）
    被淘汰。pool_limit 拉到 10（下限），塞 12 条。"""
    responses = [HUMAN_DISTILL] * 10 + [ARTICLE_DISTILL] * 2
    config = learner_config(pool_limit=10)
    learner, llm = make_learner(*responses, tmp=tmp_path, config=config)
    for i in range(12):
        asyncio.run(learner.distill(f"{MATERIAL}\n变体{i}", f"act_{i}", NOW))
    assert len(llm.calls) == 12
    pool = learner.entries()
    assert len(pool) == 10
    assert all(e["source_kind"] == "dialogue" for e in pool)


# ---------------------------------------------------------------------------
# A 组：T5 取用
# ---------------------------------------------------------------------------
def test_t5_pick_returns_one_to_two():
    responses = [HUMAN_DISTILL] * 5
    learner, _ = make_learner(*responses)
    for i in range(5):
        asyncio.run(learner.distill(f"{MATERIAL}\n变体{i}", f"act_{i}", NOW))
    picked = learner.pick(NOW)
    assert 1 <= len(picked) <= 2
    assert len({e["id"] for e in picked}) == len(picked)  # 不重复


def test_t5_pick_weighted_toward_dialogue():
    """固定 rng 大数方向：同新鲜度下 dialogue（1.0）被抽中次数显著多于
    article（0.5）。"""
    responses = [HUMAN_DISTILL, ARTICLE_DISTILL]
    learner, _ = make_learner(*responses, rng=random.Random(42))
    asyncio.run(learner.distill(MATERIAL, "act_dialogue", NOW))
    asyncio.run(learner.distill(MATERIAL + "\n另一篇", "act_article", NOW))
    dialogue_id = learner.entries()[0]["id"]
    article_id = learner.entries()[1]["id"]
    # 重置使用痕迹保证公平起点（distill 的入库时间相同，但 pick 会记账）
    for e in learner.entries():
        e["used_count"] = 0
        e["weight"] = e["base_weight"]
        e["last_used_at"] = NOW.isoformat()
    counts = {dialogue_id: 0, article_id: 0}
    rounds = 300
    for _ in range(rounds):
        picked = learner.pick(NOW, n=1)
        counts[picked[0]["id"]] += 1
        for e in learner.entries():
            e["used_count"] = 0
            e["last_used_at"] = NOW.isoformat()
    # 权重 1.0 vs 0.5 → 期望 2:1（约 200:100）；方向断言留足余量
    assert counts[dialogue_id] > counts[article_id] * 1.5


# ---------------------------------------------------------------------------
# A 组：T6 注入开关/硬上限/静默
# ---------------------------------------------------------------------------
def test_t6_disabled_means_no_learn_and_no_inject():
    config = learner_config()
    config["style_learning"]["enabled"] = False
    learner, llm = make_learner(HUMAN_DISTILL, config=config)
    assert asyncio.run(learner.distill(MATERIAL, "act_1", NOW)) is None
    assert llm.calls == []
    assert learner.inject_block(NOW) == ""


def test_t6_inject_block_hard_cap():
    """注入量硬上限（任务书 A5）：整个注入块 ≤ max_inject_chars。"""
    learner, _ = make_learner(
        '{"kind": "human", "source": "dialogue", "dims": {'
        '"wording": "' + "口癖" * 40 + '", "syntax": "' + "句式" * 40 + '", '
        '"thinking": "' + "思维" * 40 + '", "emotion_style": "' + "情绪" * 40 + '", '
        '"interaction": "' + "互动" * 40 + '", "avoid": "' + "反面" * 40 + '"}}'
    )
    asyncio.run(learner.distill(MATERIAL, "act_1", NOW))
    config = learner_config(max_inject_chars=100)
    learner._config_getter = lambda: config
    block = learner.inject_block(NOW)
    assert 0 < len(block) <= 100


def test_t6_inject_and_learn_errors_are_silent():
    """素材 getter / rng 异常 → 学习返回 None、注入空串，绝不抛出。"""
    def bad_samples():
        raise RuntimeError("disk hiccup")
    learner, llm = make_learner(HUMAN_DISTILL, samples=[])
    learner._sample_getter = bad_samples
    assert asyncio.run(learner.on_activity_end("read", NOW, "act_1")) is None
    assert llm.calls == []

    learner2, _ = make_learner()

    def bad_rng():
        raise RuntimeError("rng broke")
    learner2._rng = bad_rng
    learner2._pool.append({
        "id": "x", "dims": {"thinking": "先问背景"}, "source_kind": "dialogue",
        "source_note": "", "content_fp": "f", "weight": 1.0, "base_weight": 1.0,
        "learned_at": NOW.isoformat(), "used_count": 0,
        "last_used_at": NOW.isoformat(),
    })
    assert learner2.inject_block(NOW) == ""


# ---------------------------------------------------------------------------
# A 组：T7 演化
# ---------------------------------------------------------------------------
def test_t7_usage_boosts_weight_with_cap():
    learner, _ = make_learner(HUMAN_DISTILL)
    entry = asyncio.run(learner.distill(MATERIAL, "act_1", NOW))
    for _ in range(10):
        learner.record_used([entry], NOW)
    stored = learner.entries()[0]
    assert stored["used_count"] == 10
    # 缓升封顶 base × 1.5（来源层级不被使用率抹平）
    assert stored["weight"] == pytest.approx(stored["base_weight"] * 1.5)


def test_t7_stale_items_decay_and_evict():
    """超 decay_days 未用 → 有效权重减半衰减，低到淘汰线移出库（T7）。"""
    config = learner_config(decay_days=2)
    learner, _ = make_learner(HUMAN_DISTILL, config=config)
    asyncio.run(learner.distill(MATERIAL, "act_1", NOW))
    # 60 天没用（decay_days=2 → 30 个减半周期）→ 有效权重 ≈ 0 → 淘汰出库
    much_later = NOW + timedelta(days=60)
    learner._now = lambda: much_later
    assert learner.pick(much_later) == []
    assert learner.entries() == []


# ---------------------------------------------------------------------------
# A 组：T8 read/surf 触发
# ---------------------------------------------------------------------------
def test_t8_activity_end_learns_at_most_one():
    """read 结束触发学习；一轮活动最多入库 1 条（多个样本也只送检一次）。"""
    samples = [sample(MATERIAL), sample(MATERIAL + "\n评论区二楼", "https://e.com/2")]
    learner, llm = make_learner(HUMAN_DISTILL, samples=samples)
    entry = asyncio.run(learner.on_activity_end("read", NOW, "act_1"))
    assert entry is not None
    assert len(learner.entries()) == 1
    assert len(llm.calls) == 1
    assert "act_1" in learner.entries()[0]["source_note"]


def test_t8_other_activities_do_not_learn():
    learner, llm = make_learner(HUMAN_DISTILL, samples=[sample()])
    assert asyncio.run(learner.on_activity_end("game", NOW, "act_1")) is None
    assert llm.calls == []
    assert learner.entries() == []


def test_t8_search_off_skips_with_debug(caplog):
    """搜索关闭 → 学习不发生，DEBUG 说明（A7）。"""
    learner, llm = make_learner(HUMAN_DISTILL, samples=[sample()], search_on=False)
    with caplog.at_level(logging.DEBUG, logger="astrbot"):
        entry = asyncio.run(learner.on_activity_end("read", NOW, "act_1"))
    assert entry is None
    assert llm.calls == []
    assert any("搜索已关闭" in r.message for r in caplog.records)


def test_t8_thin_or_code_material_skipped():
    """太短 / 纯代码 → 不硬提（A7），零 LLM 调用。"""
    thin = "顶一下" * 3
    code = ("def f(x):\n" * 40) + ("    return x\n" * 40)
    for text in (thin, code):
        learner, llm = make_learner(HUMAN_DISTILL, samples=[sample(text=text)])
        assert asyncio.run(learner.on_activity_end("surf", NOW, "act_1")) is None
        assert llm.calls == []


# ---------------------------------------------------------------------------
# A 组辅助函数自检（取样启发式）
# ---------------------------------------------------------------------------
def test_helpers_score_and_code_detection():
    assert conversational_score(MATERIAL) > 0
    assert looks_like_code_or_list("def f():\n    return 1\nimport os\nx=1;") is True
    assert looks_like_code_or_list(MATERIAL) is False
    assert strip_json_block('前言```json\n{"kind": "ai"}\n```后记') == {"kind": "ai"}
    assert strip_json_block("不是 JSON") is None
    assert len(MATERIAL) >= MIN_MATERIAL_CHARS


# ---------------------------------------------------------------------------
# C 组：C1 随机吵醒阈值
# ---------------------------------------------------------------------------
def sleep_cfg(**overrides):
    cfg = {"sleep": {
        "wake_random_enabled": True,
        "wake_messages_min": 1,
        "wake_messages_max": 3,
        "wake_n_messages": 3,
        "wake_window_minutes": 10,
        "pending_reply_enabled": False,
    }}
    cfg["sleep"].update(overrides)
    return cfg


def make_manager(config=None, rng=None, asleep=True, standby=False, now=NOW):
    gate = FakeSleepGate(asleep=asleep, standby=standby)
    manager = SleepManager(
        config_getter=lambda: config if config is not None else sleep_cfg(),
        gate=gate,
        rng=rng,
        now_provider=lambda: now,
    )
    return manager, gate


def test_t9_threshold_drawn_on_sleep_and_persisted():
    """C1：阈值在入睡时抽定并落盘（gate.state 可读回）。"""
    manager, gate = make_manager(rng=iter([0.4] + [0.9] * 50).__next__)
    drawn = asyncio.run(manager.arm_wake_threshold("long", NOW))
    assert drawn == 2  # u=0.4 落在深睡 cdf 的第 2 档（P(1)=1/6, P(≤2)=1/2）
    assert manager.current_wake_threshold() == 2
    assert gate.state["wake_threshold"] == "2"


def test_t9_threshold_not_redrawn_per_message():
    """C1：同一次睡眠内每条消息面对同一阈值（不是每条重抽）。

    rng 序列：arm 消耗 0.4 → 阈值 2；若 register_message 每条重抽，
    会拿到 0.9 → 阈值 3，第 2 条就不会触发吵醒——用行为区分两种实现。"""
    manager, gate = make_manager(rng=iter([0.4] + [0.9] * 50).__next__)
    asyncio.run(manager.arm_wake_threshold("long", NOW))
    first, _ = manager.register_message(NOW + timedelta(minutes=1), "owner", MASTER)
    second, _ = manager.register_message(NOW + timedelta(minutes=2), "owner", MASTER)
    third, _ = manager.register_message(NOW + timedelta(minutes=3), "owner", MASTER)
    assert (first, second, third) == (False, True, False)  # 阈值 2 的行为


def test_t10_stage_ratio_weights_direction():
    """T10：深睡（t=0）→ 偏 max；浅睡/快醒（t=1）→ 偏 min。

    单点方向：同一随机数 0.3，深睡抽 2、浅睡抽 1。
    大数方向：固定种子两个分布均值深睡显著更高。"""
    manager, _ = make_manager(rng=random.Random(3))
    manager._rng_float = lambda: 0.3
    assert manager.draw_wake_threshold(0.0) == 2
    assert manager.draw_wake_threshold(1.0) == 1

    rng = random.Random(2026)
    seq = [rng.random() for _ in range(2000)]
    # 深睡分布
    manager._rng_float = iter(seq).__next__
    deep = [manager.draw_wake_threshold(0.0) for _ in range(2000)]
    # 同一批随机数喂浅睡分布，对比均值（方向性，不是绝对差）
    manager._rng_float = iter(seq).__next__
    shallow = [manager.draw_wake_threshold(1.0) for _ in range(2000)]
    assert sum(deep) / len(deep) > sum(shallow) / len(shallow) + 0.2


def test_t10_nap_draws_shallow_end():
    """小睡按浅睡端（t=0.8）抽定——固定 rng 下与深睡抽定可区分。"""
    manager, _ = make_manager(rng=random.Random(3))
    manager._rng_float = lambda: 0.4
    deep = manager.draw_wake_threshold(0.0)
    nap = manager.draw_wake_threshold(0.8)
    assert nap <= deep  # 浅睡端不会比深睡端更难叫醒
    assert nap == 1 and deep == 2


def test_t11_random_off_uses_fixed_threshold():
    """C1：关掉随机 → 回归固定 wake_n_messages 行为（兼容）。"""
    manager, gate = make_manager(config=sleep_cfg(wake_random_enabled=False))
    drawn = asyncio.run(manager.arm_wake_threshold("long", NOW))
    assert drawn is None
    assert manager.current_wake_threshold() == 3
    assert gate.state["wake_threshold"] == ""  # 不落随机值


def test_t11_off_range_degenerate_still_fixed():
    """范围退化（min==max）且随机开 → 恒等于该单值。"""
    manager, _ = make_manager(config=sleep_cfg(wake_messages_min=2,
                                               wake_messages_max=2))
    asyncio.run(manager.arm_wake_threshold("long", NOW))
    assert manager.current_wake_threshold() == 2


def test_t9_threshold_restored_after_restart():
    """C1"落盘"的另一半：重启恢复（仍睡着时沿用抽定值）。"""
    manager, gate = make_manager(rng=iter([0.4] + [0.9] * 50).__next__)
    asyncio.run(manager.arm_wake_threshold("long", NOW))
    # 模拟重启：新 manager 同一 gate
    manager2 = SleepManager(
        config_getter=lambda: sleep_cfg(), gate=gate,
        now_provider=lambda: NOW,
    )
    asyncio.run(manager2.restore_wake_threshold())
    assert manager2.current_wake_threshold() == 2


# ---------------------------------------------------------------------------
# C 组：C2 未回消息
# ---------------------------------------------------------------------------
def test_t12_pending_recorded_bounded():
    """T12：睡眠期消息入队、每会话 ≤10、全局 ≤30（旧的先丢）。"""
    manager, gate = make_manager(config=sleep_cfg(pending_reply_enabled=True))
    clock = {"t": 0}

    def tick():
        clock["t"] += 1
        return NOW + timedelta(minutes=clock["t"])

    for i in range(25):
        asyncio.run(manager.record_pending_message(
            MASTER, f"第 {i} 条", tick()))
    messages = asyncio.run(manager.take_pending_messages())
    assert len(messages) == 10  # 每会话只留最近 10 条
    assert messages[-1]["text"] == "第 24 条"  # 留的是最近的

    for i in range(12):  # 三个会话各 12 条
        for session in ("s1", "s2", "s3"):
            asyncio.run(manager.record_pending_message(
                session, f"msg {i}", tick()))
    messages = asyncio.run(manager.take_pending_messages())
    assert len(messages) <= 30
    per = {}
    for m in messages:
        per[m["session"]] = per.get(m["session"], 0) + 1
    assert all(v <= 10 for v in per.values())


def test_t12_pending_requires_sleep_and_enabled():
    """不在睡 / 开关关 → 不记录（T12 边界）。"""
    manager, gate = make_manager(asleep=False,
                                 config=sleep_cfg(pending_reply_enabled=True))
    assert asyncio.run(manager.record_pending_message(MASTER, "早", NOW)) is False
    manager2, _ = make_manager(asleep=True, config=sleep_cfg())
    assert asyncio.run(manager2.record_pending_message(MASTER, "早", NOW)) is False
    # 插件命令永不记录
    manager3, _ = make_manager(config=sleep_cfg(pending_reply_enabled=True))
    assert asyncio.run(manager3.record_pending_message(
        MASTER, "living_wake_now", NOW)) is False


def test_t12_pending_only_current_window():
    """C2"只留最近一次睡眠窗口"：入睡（新窗口）清空上一窗口留档。"""
    manager, gate = make_manager(config=sleep_cfg(pending_reply_enabled=True))
    asyncio.run(manager.record_pending_message(MASTER, "睡前的消息", NOW))
    asyncio.run(manager.arm_wake_threshold("long", NOW))
    assert asyncio.run(manager.take_pending_messages()) == []


def test_t12_pending_persisted_across_restart():
    """留档持久化：重启（同 gate 新 manager）后醒来仍能判断。"""
    manager, gate = make_manager(config=sleep_cfg(pending_reply_enabled=True))
    asyncio.run(manager.record_pending_message(MASTER, "睡着的消息", NOW))
    manager2 = SleepManager(
        config_getter=lambda: sleep_cfg(pending_reply_enabled=True),
        gate=gate, now_provider=lambda: NOW,
    )
    asyncio.run(manager2.load_pending_messages())
    messages = asyncio.run(manager2.take_pending_messages())
    assert [m["text"] for m in messages] == ["睡着的消息"]


# ---------------------------------------------------------------------------
# C 组：C2 醒来三档补回复（LivingLoop._settle_pending_replies）
# ---------------------------------------------------------------------------
class FakePendingManager:
    def __init__(self, messages):
        self._messages = list(messages)
        self.taken = False

    async def take_pending_messages(self):
        self.taken = True
        return list(self._messages)


class FakeJudgeLLM:
    def __init__(self, *responses):
        self._responses = list(responses)
        self.calls = []

    async def __call__(self, prompt, system_prompt=None, **kwargs):
        self.calls.append(prompt)
        item = self._responses.pop(0) if self._responses else None
        if isinstance(item, Exception):
            raise item
        return item


REPLY_CONFIG = {
    "sleep": {"pending_reply_enabled": True},
    "decision": {"activity_context_write": True},
    "output_gate": {"target_sessions": MASTER},
}


def make_reply_loop(*, manager=None, llm=None, config=None, standby=False):
    cfg = REPLY_CONFIG
    if config:
        for group, kv in config.items():
            cfg = {**cfg, group: {**cfg.get(group, {}), **kv}}
    sender = FakeSender()
    gate = FakeSleepGate(asleep=False, standby=standby)
    mgr = FakeConvMgr()
    mgr.pairs = []
    lm = FakeLM()
    loop = LivingLoop(
        gate=gate,
        memory_getter=lambda: asyncio.sleep(0, result=object()),
        config_getter=lambda: cfg,
        activities=[],
        sender=sender,
        dream_llm_call=llm if llm is not None else FakeJudgeLLM(),
        sleep_manager=manager if manager is not None else FakePendingManager([]),
        conversation_manager=mgr,
        lm_conversation_manager_getter=lambda: lm,
    )
    calls = []
    real = loop._write_speech_to_stores

    async def spy(text, dedup_key, user_msg, label="活动经历"):
        calls.append({"text": text, "dedup_key": dedup_key,
                      "user_msg": user_msg, "label": label})
        await real(text, dedup_key, user_msg, label)

    loop._write_speech_to_stores = spy
    return loop, sender, calls, mgr, lm


def test_t13_wake_reply_skip_means_silence():
    """T13：判断"不回" → 零输出、零落库（不回是合法结果）。"""
    manager = FakePendingManager([
        {"session": MASTER, "text": "睡了？", "at": NOW.isoformat()},
    ])
    llm = FakeJudgeLLM("SKIP")
    loop, sender, calls, mgr, lm = make_reply_loop(manager=manager, llm=llm)
    asyncio.run(loop._settle_pending_replies(NOW, actual_h=7.2))
    assert sender.sent == []
    assert calls == []
    assert len(llm.calls) == 1  # 判断照做（材料里带了睡时长）
    assert "7.2" in llm.calls[0]


def test_t13_wake_reply_brief_sends_and_double_writes():
    """T13：糊弄回 → 发送 + M16 双写落库被调（dedup 前缀 #wake-reply:）。"""
    manager = FakePendingManager([
        {"session": MASTER, "text": "那个链接发我下", "at": NOW.isoformat()},
    ])
    reply_text = "昨晚睡着了，你说的那个我看看哈"
    llm = FakeJudgeLLM(f"BRIEF\n{reply_text}")
    loop, sender, calls, mgr, lm = make_reply_loop(manager=manager, llm=llm)
    asyncio.run(loop._settle_pending_replies(NOW, actual_h=7.2))
    assert sender.sent == [(MASTER, reply_text)]
    assert len(calls) == 1
    assert calls[0]["text"] == reply_text
    assert calls[0]["dedup_key"].startswith("#wake-reply:")
    assert calls[0]["label"] == "醒来补回复"
    # 双写两落点真实写入
    assert len(mgr.pairs) == 1
    assert len(lm.added) == 1
    assert lm.added[0]["session_id"] == MASTER


def test_t13_wake_reply_full_reply_also_sends():
    """T13：认真回（REPLY 档）同样发送 + 双写。"""
    manager = FakePendingManager([
        {"session": MASTER, "text": "明天记得带伞", "at": NOW.isoformat()},
    ])
    reply_text = "知道啦，出门前会看天气预报的，你也别淋着"
    llm = FakeJudgeLLM(f"REPLY\n{reply_text}")
    loop, sender, calls, mgr, lm = make_reply_loop(manager=manager, llm=llm)
    asyncio.run(loop._settle_pending_replies(NOW, actual_h=8.0))
    assert sender.sent == [(MASTER, reply_text)]
    assert calls and calls[0]["text"] == reply_text


def test_t14_no_pending_means_zero_llm_calls():
    """T14：无未回消息 → 零 LLM 调用（成本红线）。"""
    manager = FakePendingManager([])
    llm = FakeJudgeLLM()
    loop, sender, calls, mgr, lm = make_reply_loop(manager=manager, llm=llm)
    asyncio.run(loop._settle_pending_replies(NOW, actual_h=7.0))
    assert llm.calls == []
    assert sender.sent == []


def test_t14_standby_active_skips_reply():
    """边界：醒来时主人正在聊天（待机期活跃）→ 不插补回复。"""
    manager = FakePendingManager([
        {"session": MASTER, "text": "睡了？", "at": NOW.isoformat()},
    ])
    llm = FakeJudgeLLM()
    loop, sender, calls, mgr, lm = make_reply_loop(
        manager=manager, llm=llm, standby=True)
    asyncio.run(loop._settle_pending_replies(NOW, actual_h=7.0))
    assert llm.calls == []
    assert sender.sent == []


def test_t14_judge_error_is_silent():
    """T14：判断异常 → 静默，不影响醒来流程，不发送不落库。"""
    manager = FakePendingManager([
        {"session": MASTER, "text": "睡了？", "at": NOW.isoformat()},
    ])
    llm = FakeJudgeLLM(RuntimeError("provider down"))
    loop, sender, calls, mgr, lm = make_reply_loop(manager=manager, llm=llm)
    asyncio.run(loop._settle_pending_replies(NOW, actual_h=7.0))  # 不抛
    assert sender.sent == []
    assert calls == []


def test_t14_disabled_means_zero_calls():
    """开关关闭 → 直接返回（零调用），消息也不留档。"""
    manager = FakePendingManager([
        {"session": MASTER, "text": "睡了？", "at": NOW.isoformat()},
    ])
    llm = FakeJudgeLLM()
    loop, sender, calls, mgr, lm = make_reply_loop(
        manager=manager, llm=llm,
        config={"sleep": {"pending_reply_enabled": False}})
    asyncio.run(loop._settle_pending_replies(NOW, actual_h=7.0))
    assert llm.calls == []
    assert sender.sent == []


# ---------------------------------------------------------------------------
# 钩子专项 T16/T17：on_llm_request 只追加、不覆盖、异常吞掉
# ---------------------------------------------------------------------------
def load_plugin_main():
    """与 test_main_wiring 同法：合成包上下文加载插件 main.py。"""
    pkg_name = "living_plugin_m17"
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


def test_t16_hook_appends_without_overwriting():
    """T16：与 prompt-preset 共存场景——preset 先整体替换 system_prompt，
    living 钩子只往 extra_user_content_parts 追加；system_prompt / contexts /
    prompt 全部原样。"""
    main_module = load_plugin_main()
    learner = types.SimpleNamespace(
        enabled=lambda: True,
        inject_block=lambda: "（说话语气参考：多用短句）",
    )
    plugin_self = types.SimpleNamespace(_style_learner=learner)
    req = types.SimpleNamespace(
        system_prompt="preset 替换后的人设提示词",
        prompt="早上好",
        contexts=[{"role": "user", "content": "早上好"}],
        extra_user_content_parts=[],
    )
    event = types.SimpleNamespace()
    asyncio.run(
        main_module.LivingPlugin.inject_style_on_llm_request(
            plugin_self, event, req
        )
    )
    # 只追加，不覆盖/不重排
    assert req.system_prompt == "preset 替换后的人设提示词"
    assert req.contexts == [{"role": "user", "content": "早上好"}]
    assert req.prompt == "早上好"
    assert len(req.extra_user_content_parts) == 1
    assert "语气参考" in req.extra_user_content_parts[0].text


def test_t16_hook_disabled_or_empty_means_noop():
    main_module = load_plugin_main()
    learner = types.SimpleNamespace(
        enabled=lambda: False, inject_block=lambda: "不应该出现",
    )
    plugin_self = types.SimpleNamespace(_style_learner=learner)
    req = types.SimpleNamespace(
        system_prompt="p", prompt="m", contexts=[],
        extra_user_content_parts=[],
    )
    asyncio.run(
        main_module.LivingPlugin.inject_style_on_llm_request(
            plugin_self, types.SimpleNamespace(), req
        )
    )
    assert req.extra_user_content_parts == []
    assert req.system_prompt == "p"


def test_t17_hook_exception_swallowed():
    """T17：钩子内抛异常 → 一律吞掉，正常聊天不受影响。"""
    main_module = load_plugin_main()

    def boom():
        raise RuntimeError("style pool exploded")

    learner = types.SimpleNamespace(enabled=lambda: True, inject_block=boom)
    plugin_self = types.SimpleNamespace(_style_learner=learner)
    req = types.SimpleNamespace(
        system_prompt="p", prompt="m", contexts=[],
        extra_user_content_parts=[],
    )
    asyncio.run(
        main_module.LivingPlugin.inject_style_on_llm_request(
            plugin_self, types.SimpleNamespace(), req
        )
    )
    assert req.extra_user_content_parts == []
    assert req.system_prompt == "p"


# ---------------------------------------------------------------------------
# 装配级：LivingLoop 挂 style_learner 后梦话注入走通（A5 第三路）
# ---------------------------------------------------------------------------
def test_dream_prompt_carries_style_hint():
    class DreamLLM:
        def __init__(self):
            self.calls = []

        async def __call__(self, prompt, system_prompt=None, **kw):
            self.calls.append(prompt)
            return "我梦见热泉在冒泡"

    class LearnOK:
        def inject_block(self, now=None):
            return "（说话语气参考：多用短句）"

    class Mem:
        async def search(self, query, k=3, **kw):
            return [{"content": "碎片一"}]

        async def add(self, *a, **kw):
            return 1

    llm = DreamLLM()
    loop = LivingLoop(
        gate=types.SimpleNamespace(),
        memory_getter=lambda: asyncio.sleep(0, result=Mem()),
        config_getter=lambda: {"sleep": {"dream_probability": 1.0}},
        activities=[],
        dream_llm_call=llm,
        rng=random.Random(1),  # 0.0 < 1.0 → 命中掷梦
        style_learner=LearnOK(),
    )

    async def _none():
        return None
    loop._bot_identity = _none
    loop._persona_id = _none
    loop._session_id = lambda event: "living_test"

    async def no_share(*a, **k):
        return None
    loop._maybe_share = no_share
    asyncio.run(loop._maybe_dream(NOW))
    assert llm.calls and "语气参考" in llm.calls[0]


def test_loop_style_learner_trigger_on_activity_end():
    """A7 接线：活动周期结束调 learner.on_activity_end（异常只 DEBUG）。"""
    class SpyLearner:
        def __init__(self):
            self.calls = []

        async def on_activity_end(self, name, now, activity_id):
            self.calls.append((name, activity_id))

    learner = SpyLearner()
    loop = _minimal_loop(style_learner=learner)
    result = asyncio.run(loop.run_activity_cycle(now=NOW, force_activity="read"))
    assert result["activity"] == "read"
    assert learner.calls and learner.calls[0][0] == "read"


class FakeSearcher:
    async def search(self, topic, count=5):
        return [{"title": "一条结果", "url": "https://example.com/a"}]


class FakeFetcher:
    async def fetch(self, url):
        return {"title": "一篇文章", "text": "正文内容" * 50, "status": 200}


class Gate:
    async def note_activity_started(self, now=None):
        pass

    async def note_activity_finished(self, now=None):
        pass

    async def daily_limit_info(self, now=None):
        return False, 0, 3

    async def should_send_message(self, now=None):
        return False, "quiet"

    async def note_message_sent(self, now=None):
        pass


def _minimal_loop(style_learner=None):
    """活动周期最小装配（read 脚本模式：真搜真读一条，走完整周期）。"""
    from core.activities import ReadArticleActivity

    cfg = {
        "decision": {"daily_impulse_limit": 3, "max_run_seconds": 10},
        "capabilities": {"web_search_enabled": True,
                         "cooldown_between_activities_hours": 0},
        "output_gate": {"daily_message_limit": 10,
                        "target_sessions": MASTER},
        "sleep": {"dream_probability": 0.0},
    }
    memory = types.SimpleNamespace()
    loop = LivingLoop(
        gate=Gate(),
        memory_getter=lambda: asyncio.sleep(0, result=memory),
        config_getter=lambda: cfg,
        activities=[ReadArticleActivity()],
        abilities={"searcher": FakeSearcher(), "fetcher": FakeFetcher()},
        sender=FakeSender(),
        style_learner=style_learner,
    )
    return loop
