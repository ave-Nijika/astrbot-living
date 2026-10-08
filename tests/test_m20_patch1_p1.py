"""M20-补丁1 P1 批测试：库分层与沉淀机制（I/J/K/L/M/N 组）。

覆盖：
- I 组：语料库改名迁移（style_pool.json → style_corpus.json 一次性兼容）、
  素材库（只由用户添加 / 有界 / FIFO 优先处理 / 不删除只标记）、人工优选
  权重（略高于 dialogue，钳位 ≤1.5 不设 2 倍以上）；
- L 组：触发扩展（任何活动 + 本轮读网证据；素材库优先；证据时间窗）；
- M 组：material_max_chars 等参数可配热生效；
- K 组：调用记录（注入写入 + 有界 + 不进记忆）、每日复盘（mock 判断模型
  正常路径 / 判定模型未配置跳过 / 无记录零调用）、重要度防僵化上限、
  留存度（判好不被清出 / 判差优先清理）、沉淀层归纳与注入优先级；
- J 组：面板逻辑层（语料/素材增删改清空 + 非法输入拒绝 + 立即处理限频）；
- N 组：可点元素清单（选择器生成策略、稳定性、上限、失效明确报错、
  browser_read 闭环——假 page 端到端，测试环境无 Playwright）。
"""

import asyncio
import json
import random
import types
from datetime import datetime
from pathlib import Path

import pytest

from core.panel_api import (
    PanelApiError,
    apply_style_corpus_action,
    apply_style_materials_action,
)
from core.style_learning import (
    DEFAULT_PROMPT_DISTILL,
    DEFAULT_PROMPT_REVIEW,
    DIMS_ORDER,
    StyleLearner,
)
from core.style_review import DEFAULT_PROMPT_INDUCT, StyleReviewer

NOW = datetime(2026, 10, 6, 14, 5, 0)

HUMAN_DISTILL = (
    '{"kind": "human", "source": "dialogue", "dims": {'
    '"wording": "爱说“说实话”", "syntax": "多用短句", '
    '"thinking": "先问背景再下判断", "emotion_style": "开心就一句笑死", '
    '"interaction": "被夸就顺着接，不推辞", "avoid": "不用书面连接词"}}'
)
ARTICLE_DISTILL = (
    '{"kind": "human", "source": "article", "dims": {'
    '"wording": "", "syntax": "长句铺垫", "thinking": "从反面举例", '
    '"emotion_style": "", "interaction": "", "avoid": ""}}'
)
AI_DISTILL = '{"kind": "ai", "source": "article", "dims": {}}'

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
    """判定/复盘调用替身：按序返回脚本输出，记录 (prompt, kwargs)。"""

    def __init__(self, *responses):
        self._responses = list(responses)
        self.calls = []

    async def __call__(self, prompt, system_prompt=None, **kwargs):
        self.calls.append({"prompt": prompt, "system_prompt": system_prompt,
                           "kwargs": kwargs})
        if not self._responses:
            raise RuntimeError("no more scripted responses")
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def sample(text=MATERIAL, url="https://example.com/post/1",
           at=NOW.isoformat()):
    return {"url": url, "title": "一个帖子", "text": text, "at": at}


def make_learner(*responses, tmp=None, config=None, samples=None,
                 browser_reads=None, rng=None, now=None):
    """构造完整分层库的 StyleLearner（全部文件落 tmp）。"""
    llm = FakeStyleLLM(*responses)
    tmp = tmp or Path("/tmp")
    learner = StyleLearner(
        config_getter=lambda: config if config is not None else learner_config(),
        llm_call=llm,
        pool_path=str(tmp / "style_corpus.json"),
        sample_getter=(lambda: samples) if samples is not None else None,
        rng=rng or random.Random(7),
        now_provider=lambda: now or NOW,
        materials_path=str(tmp / "style_materials.json"),
        usage_path=str(tmp / "feature_usage.json"),
        features_path=str(tmp / "style_features.json"),
        browser_reads_getter=(
            (lambda: browser_reads) if browser_reads is not None else None
        ),
    )
    return learner, llm


# ---------------------------------------------------------------------------
# I 组：语料库改名迁移 + 素材库
# ---------------------------------------------------------------------------
def test_i1_legacy_pool_migrated_to_corpus(tmp_path):
    """I1：旧 style_pool.json 存在而新库不存在 → 一次性迁移（旧文件保留）。"""
    legacy = [
        {"id": "old-1", "dims": {"thinking": "先看数据"}, "source_kind": "dialogue",
         "weight": 1.0, "base_weight": 1.0},
    ]
    (tmp_path / "style_pool.json").write_text(
        json.dumps(legacy, ensure_ascii=False), encoding="utf-8"
    )
    learner, _llm = make_learner(tmp=tmp_path)
    entries = learner.entries()
    assert len(entries) == 1 and entries[0]["id"] == "old-1"
    # 写动作触发后新文件落盘
    learner.record_used(entries, NOW)
    assert (tmp_path / "style_corpus.json").exists()
    # 重启（新实例）后从新库读，且不重复迁移
    learner2, _ = make_learner(tmp=tmp_path)
    assert len(learner2.entries()) == 1


def test_i1_no_corpus_no_legacy_means_empty(tmp_path):
    learner, _ = make_learner(tmp=tmp_path)
    assert learner.entries() == []


def test_p1_t1_material_priority_processed_marked(tmp_path):
    """P1-T1：用户添加素材 → 活动结束优先处理 → 标记已处理（不删除）。"""
    learner, llm = make_learner(HUMAN_DISTILL, tmp=tmp_path, samples=[sample()])
    learner.add_material("用户手动丢进来的一段话" * 10, note="评论区精华")
    entry = asyncio.run(learner.on_activity_end("game", NOW, "act_1"))
    assert entry is not None, "素材应优先于抓取样本被处理"
    assert "评论区精华" in llm.calls[0]["prompt"], "备注应进提示词"
    material = learner.materials()[0]
    assert material["processed"] is True
    assert material["result"] == "learned"
    assert material["processed_at"]
    assert len(learner.materials()) == 1, "处理完不删除（用户可看状态）"
    assert entry["manual"] is True


def test_p1_t1_material_queue_fifo(tmp_path):
    """I3：素材库多条待处理 → 一次活动只处理最早的一条（克制）。"""
    learner, _ = make_learner(HUMAN_DISTILL, tmp=tmp_path)
    learner.add_material("第一条素材" * 20, now=datetime(2026, 10, 6, 10, 0, 0))
    learner.add_material("第二条素材" * 20, now=datetime(2026, 10, 6, 11, 0, 0))
    asyncio.run(learner.on_activity_end("read", NOW, "act_1"))
    materials = learner.materials()
    assert materials[0]["processed"] is True
    assert materials[1]["processed"] is False, "留到下次活动继续"


def test_p1_t2_empty_materials_use_fetch_path(tmp_path):
    """P1-T2：素材库为空 → 走原有"从抓取内容挑一条"路径（回归）。"""
    learner, llm = make_learner(HUMAN_DISTILL, tmp=tmp_path, samples=[sample()])
    entry = asyncio.run(learner.on_activity_end("read", NOW, "act_1"))
    assert entry is not None
    assert entry["manual"] is False
    assert learner.materials() == []


def test_p1_t3_manual_weight_above_dialogue_but_capped(tmp_path):
    """P1-T3/I4：人工优选权重 = 来源权重 × manual_weight，高于 dialogue、
    倍率有硬上限（配置 3.0 也被钳到 1.5——不设 2 倍以上）。"""
    learner, _ = make_learner(
        HUMAN_DISTILL, tmp=tmp_path, config=learner_config(manual_weight=3.0)
    )
    assert learner.manual_weight() == 1.5, "配置越界被钳位"
    learner.add_material(MATERIAL)
    entry = asyncio.run(learner.on_activity_end("read", NOW, "act_1"))
    assert entry is not None
    assert entry["weight"] == pytest.approx(1.5, abs=1e-6), "1.0 × 1.5 钳位"
    # 正常 1.2 倍
    learner2, _ = make_learner(
        HUMAN_DISTILL, tmp=tmp_path / "b", config=learner_config(manual_weight=1.2)
    )
    learner2.add_material(MATERIAL)
    entry2 = asyncio.run(learner2.on_activity_end("read", NOW, "act_1"))
    assert entry2["weight"] == pytest.approx(1.2, abs=1e-6)


def test_i2_material_pool_bounded(tmp_path):
    learner, _ = make_learner(
        tmp=tmp_path, config=learner_config(material_pool_limit=10)
    )
    for i in range(10):
        learner.add_material(f"素材{i}_" * 20)
    with pytest.raises(ValueError):
        learner.add_material("超出上限" * 20)


def test_i2_material_stored_truncated_to_material_max(tmp_path):
    learner, _ = make_learner(
        tmp=tmp_path, config=learner_config(material_max_chars=100)
    )
    entry = learner.add_material("长" * 500)
    assert len(entry["text"]) == 100


# ---------------------------------------------------------------------------
# M 组：参数可配
# ---------------------------------------------------------------------------
def test_p1_t12_material_max_chars_hot_read(tmp_path):
    """P1-T12：material_max_chars 改动后生效（超长材料按新上限截断）。"""
    long_material = "字" * 2000
    learner, llm = make_learner(
        HUMAN_DISTILL, tmp=tmp_path,
        config=learner_config(material_max_chars=1500),
        samples=[sample(long_material)],
    )
    asyncio.run(learner.on_activity_end("read", NOW, "act_1"))
    sent = llm.calls[0]["prompt"]
    assert "字" * 1500 in sent and "字" * 1501 not in sent


def test_m_min_material_chars_configurable(tmp_path):
    learner, _ = make_learner(
        tmp=tmp_path,
        config=learner_config(min_material_chars=200),
        samples=[sample("短文本" * 5)],  # 20 字 < 200 → 不提炼
    )
    assert asyncio.run(learner.on_activity_end("read", NOW, "act_1")) is None


def test_m_item_max_chars_in_injection(tmp_path):
    learner, _ = make_learner(
        HUMAN_DISTILL, tmp=tmp_path,
        config=learner_config(item_max_chars=10),
        samples=[sample()],
    )
    asyncio.run(learner.on_activity_end("read", NOW, "act_1"))
    block = learner.inject_block(NOW)
    assert block, "注入块应非空"
    for line in block.splitlines():
        if line.startswith("（说话语气参考") or line.strip().endswith("）"):
            continue  # 框架文案不受 item 上限约束
        for seg in line.split("；"):
            assert len(seg) <= 30, f"单维展示应被截断：{seg!r}"


# ---------------------------------------------------------------------------
# K1：调用记录库
# ---------------------------------------------------------------------------
def test_p1_t6_usage_log_written_bounded_no_memory(tmp_path):
    """P1-T6：注入时写入调用记录 + 有界 + 不进记忆（零记忆写入断言）。"""
    learner, _ = make_learner(HUMAN_DISTILL, tmp=tmp_path, samples=[sample()])
    asyncio.run(learner.on_activity_end("read", NOW, "act_1"))
    block = learner.inject_block(NOW, trigger="对话")
    assert block
    records = learner.usage_records()
    assert len(records) == 1
    assert records[0]["trigger"] == "对话"
    assert records[0]["entry_ids"] == [asyncio.run(_first_id(learner))]
    # 落盘回读
    learner2, _ = make_learner(tmp=tmp_path)
    assert len(learner2.usage_records()) == 1
    # 有界
    learner3, _ = make_learner(
        tmp=tmp_path, config=learner_config(usage_log_limit=50)
    )
    for i in range(60):
        learner3._log_usage([], None, "对话", NOW)
    assert len(learner3.usage_records()) == 50


async def _first_id(learner):
    return learner.entries()[0]["id"]


def test_k1_usage_no_memory_backend_calls(tmp_path):
    """红线：调用记录只写独立文件——StyleLearner 不持有任何记忆后端。"""
    import inspect

    params = inspect.signature(StyleLearner.__init__).parameters
    assert "memory" not in params and "memory_getter" not in params


# ---------------------------------------------------------------------------
# K3：每日复盘
# ---------------------------------------------------------------------------
def make_reviewer(learner, llm, reactions=None, config=None):
    async def reactions_getter():
        return reactions or []

    return StyleReviewer(
        learner=learner,
        llm_call=llm,
        config_getter=lambda: config if config is not None else learner_config(),
        reactions_getter=reactions_getter,
        now_provider=lambda: NOW,
    )


REVIEW_OK = (
    '{"entries": ['
    '{"id": "ID1", "verdict": "good", "reason": "用户没反感"}, '
    '{"id": "ID2", "verdict": "bad", "reason": "用户说别这样说话"}, '
    '{"id": "ID3", "verdict": "neutral", "reason": "信息不足"}]}'
)


def _seed_used_entries(learner, ids):
    """写入语料 + 构造取用记录（先触发加载，防磁盘空库覆盖内存）。"""
    learner.entries()  # _ensure_loaded
    for eid in ids:
        learner._pool.append(
            {
                "id": eid, "dims": {"thinking": "先看数据"},
                "source_kind": "dialogue", "weight": 1.0, "base_weight": 1.0,
                "learned_at": NOW.isoformat(), "last_used_at": NOW.isoformat(),
                "used_count": 1, "importance": 1.0, "retention": 1.0,
            }
        )
    learner._dirty = True
    learner._log_usage(
        [e for e in learner._pool if e["id"] in ids], None, "对话", NOW
    )


def test_p1_t7_review_normal_path(tmp_path):
    """P1-T7：复盘正常路径（mock 判断模型）——调 1 次、结果落地。"""
    learner, llm = make_learner(REVIEW_OK, tmp=tmp_path)
    _seed_used_entries(learner, ["ID1", "ID2", "ID3"])
    reviewer = make_reviewer(learner, llm, reactions=["用户：你今天说话怪怪的"])
    summary = asyncio.run(reviewer.run_review(NOW))
    assert summary.get("judged") == 2, "neutral 不动，good+bad 落地"
    assert summary.get("good") == 1 and summary.get("bad") == 1
    assert len(llm.calls) == 1, "汇总判定只调 1 次"
    entries = {e["id"]: e for e in learner.entries()}
    assert entries["ID1"]["importance"] > 1.0
    assert entries["ID1"]["retention"] > 1.0
    assert entries["ID2"]["importance"] < 1.0
    assert entries["ID2"]["retention"] < 1.0
    assert entries["ID3"]["importance"] == 1.0, "neutral 条目不动"
    assert "别这样说话" in entries["ID2"]["review_note"]
    # 复盘完成落日期（当天不重跑）
    assert reviewer.due(NOW) is False


def test_p1_t7_review_no_usage_zero_calls(tmp_path):
    """P1-T7：无取用记录 → 零调用（不烧判断模型）。"""
    learner, llm = make_learner(tmp=tmp_path)
    reviewer = make_reviewer(learner, llm)
    summary = asyncio.run(reviewer.run_review(NOW))
    assert summary == {"skipped": "no_usage"}
    assert llm.calls == []


def test_p1_t7_review_skips_without_judge_provider(tmp_path):
    """P1-T7：判断模型未配置（llm_call 返回 None）→ 复盘跳过。"""
    learner, _ = make_learner(tmp=tmp_path)
    _seed_used_entries(learner, ["ID1"])
    reviewer = make_reviewer(learner, None)  # None = _judge_llm_call 未配置形态
    summary = asyncio.run(reviewer.run_review(NOW))
    assert summary.get("skipped") in ("llm_failed", "llm_empty")


def test_k3_review_disabled_by_total_switch(tmp_path):
    """红线 3：style_learning.enabled=false → 复盘不跑（零调用零写入）。"""
    learner, llm = make_learner(
        tmp=tmp_path, config=learner_config(enabled=False)
    )
    _seed_used_entries(learner, ["ID1"])
    reviewer = make_reviewer(learner, llm)
    assert reviewer.due(NOW) is False
    summary = asyncio.run(reviewer.run_review(NOW))
    assert summary == {"skipped": "already_done_today"} or llm.calls == []


def test_p1_t8_importance_hard_cap(tmp_path):
    """P1-T8（防僵化硬约束）：反复判好 → 重要度 ≤ 上限倍率，综合分
    有效权重 ≤ 基础权重 × 上限；不超过同类中位数的 1.5 倍。"""
    learner, _ = make_learner(
        tmp=tmp_path, config=learner_config(feature_importance_cap=1.5)
    )
    _seed_used_entries(learner, ["A", "B", "C"])
    verdicts = {
        "A": {"verdict": "good", "reason": "好"},
        "B": {"verdict": "neutral", "reason": ""},
        "C": {"verdict": "neutral", "reason": ""},
    }
    for _ in range(8):  # 反复判好 A
        counts = learner.apply_review(verdicts, NOW)
        assert counts["changed"] == 1
    entries = {e["id"]: e for e in learner.entries()}
    assert entries["A"]["importance"] <= 1.5, "重要度不超上限倍率"
    # 同类中位数（B/C 都是 1.0）× 1.5 = 1.5
    assert entries["A"]["importance"] <= 1.5
    # 综合分有效部分 ≤ base × cap（A 没被取用过，weight=base=1.0）
    now = datetime(2026, 10, 20, 9, 0, 0)
    score_a = learner.entry_score(entries["A"], now)
    assert score_a <= 1.0 * 1.5, "综合分中的权重部分不超过基础权重×1.5"


def test_p1_t9_retention_good_kept_bad_evicted(tmp_path):
    """P1-T9：判好的条目在定期清理中不被清出；判差的被优先清理。"""
    learner, _ = make_learner(
        tmp=tmp_path,
        config=learner_config(pool_limit=10, decay_days=14),
    )
    learner.entries()  # 先触发加载（防磁盘空库覆盖内存种子）
    # 11 条 > 上限 10：GOOD 判好（留存度高）、BAD 判差（留存度低）、
    # 其余 9 条中性垫底（同等新鲜度）
    for eid in ("GOOD", "BAD"):
        learner._pool.append(
            {
                "id": eid, "dims": {"thinking": "先看数据"},
                "source_kind": "dialogue", "weight": 1.0, "base_weight": 1.0,
                "learned_at": NOW.isoformat(),
                "last_used_at": NOW.isoformat(),  # 同等新鲜度：只比留存度
                "used_count": 0, "importance": 1.0, "retention": 1.0,
            }
        )
    for i in range(9):
        learner._pool.append(
            {
                "id": f"F{i}", "dims": {"thinking": "垫底"},
                "source_kind": "dialogue", "weight": 1.0, "base_weight": 1.0,
                "learned_at": NOW.isoformat(),
                "last_used_at": NOW.isoformat(),
                "used_count": 0, "importance": 1.0, "retention": 1.0,
            }
        )
    learner._dirty = True
    learner.apply_review(
        {"GOOD": {"verdict": "good", "reason": ""},
         "BAD": {"verdict": "bad", "reason": ""}},
        NOW,
    )
    # 容量淘汰到 10 条：按 综合分×留存度——GOOD（×1.3）留下，
    # BAD（×0.6）排最后被清出
    learner._evolve(NOW)
    ids = {e["id"] for e in learner.entries()}
    assert "GOOD" in ids, "判好不被清出"
    assert "BAD" not in ids, "判差优先清理"


# ---------------------------------------------------------------------------
# K2：沉淀层归纳 + 注入优先级
# ---------------------------------------------------------------------------
INDUCT_OK = (
    '{"features": ['
    '{"note": "先看数据再下判断", "dims": {"thinking": "先查背景", '
    '"interaction": "被夸就顺接"}}]}'
)


def test_p1_t10_induction_triggered_by_threshold(tmp_path):
    """P1-T10：达到归纳阈值（判好数）触发一次归纳并落沉淀层。"""
    learner, llm = make_learner(
        tmp=tmp_path, config=learner_config(feature_promote_threshold=1)
    )
    _seed_used_entries(learner, ["ID1", "ID2"])
    reviewer = make_reviewer(learner, llm, reactions=["用户：说得好"])
    llm._responses.extend([REVIEW_OK, INDUCT_OK])
    summary = asyncio.run(reviewer.run_review(NOW))
    assert summary.get("inducted") == 1
    features = learner.features()
    assert len(features) == 1
    assert features[0]["dims"]["thinking"] == "先查背景"
    assert features[0]["source_entry_ids"]
    # good_since_induction 归零
    assert learner.features_meta()["good_since_induction"] == 0
    # 累计调用 = 1 判定 + 1 归纳 = 2 ≤ 3（K3 上限）
    assert len(llm.calls) == 2


def test_p1_t10_no_induction_below_threshold(tmp_path):
    learner, llm = make_learner(REVIEW_OK, tmp=tmp_path)
    _seed_used_entries(learner, ["A"])
    reviewer = make_reviewer(learner, llm)
    llm._responses.extend([REVIEW_OK])
    summary = asyncio.run(reviewer.run_review(NOW))
    assert summary.get("inducted", 0) == 0
    assert learner.features() == []
    assert len(llm.calls) == 1, "未达阈值只调判定一次"


def test_p1_t10_injection_prefers_feature_layer(tmp_path):
    """P1-T10：注入时先带沉淀层（稳定的一面），语料片段补新鲜感。"""
    learner, _ = make_learner(HUMAN_DISTILL, tmp=tmp_path, samples=[sample()])
    asyncio.run(learner.on_activity_end("read", NOW, "act_1"))
    learner.replace_features(
        [{"id": "f1", "dims": {"thinking": "稳定的沉淀特征线"},
          "importance": 1.0}]
    )
    # 语料库只有 1 条 + max_items_per_pick=2 → 语料片段必被抽中补位
    block = learner.inject_block(NOW, trigger="对话")
    assert "稳定的沉淀特征线" in block
    corpus_thinking = learner.entries()[0]["dims"]["thinking"]
    assert corpus_thinking in block, "语料片段补充新鲜感"
    assert block.index("稳定的沉淀特征线") < block.index(corpus_thinking), (
        "沉淀层在前（稳定的一面），语料在后（新鲜感）"
    )
    # 沉淀层取用也进调用记录（feat: 前缀）
    usage = learner.usage_records()
    assert any(
        str(eid).startswith("feat:") for r in usage for eid in r["entry_ids"]
    )


def test_k2_injection_without_features_unchanged(tmp_path):
    """沉淀层为空 → 注入行为与之前一致（回归）。"""
    learner, _ = make_learner(HUMAN_DISTILL, tmp=tmp_path, samples=[sample()])
    asyncio.run(learner.on_activity_end("read", NOW, "act_1"))
    block = learner.inject_block(NOW)
    assert block and "（说话语气参考" in block


# ---------------------------------------------------------------------------
# J 组：面板逻辑层
# ---------------------------------------------------------------------------
def test_p1_t4_corpus_panel_actions(tmp_path):
    """P1-T4：语料库读与写（改/删/清空）+ 非法输入拒绝。"""
    learner, _ = make_learner(HUMAN_DISTILL, tmp=tmp_path, samples=[sample()])
    asyncio.run(learner.on_activity_end("read", NOW, "act_1"))
    entry_id = learner.entries()[0]["id"]
    # 编辑
    result = apply_style_corpus_action(
        learner, {"action": "update", "id": entry_id,
                  "dims": {"thinking": "改过的思维方式"}}
    )
    assert result["message"] == "已保存"
    assert learner.entries()[0]["dims"]["thinking"] == "改过的思维方式"
    # 非法输入
    with pytest.raises(PanelApiError):
        apply_style_corpus_action(learner, {"action": "update", "id": "ghost",
                                            "dims": {}})
    with pytest.raises(PanelApiError):
        apply_style_corpus_action(learner, {"action": "nuke"})
    with pytest.raises(PanelApiError):
        apply_style_corpus_action(learner, {"action": "clear"})  # 无 confirm
    # 删除
    apply_style_corpus_action(learner, {"action": "delete", "id": entry_id})
    assert learner.entries() == []
    # 清空（二次确认标记）
    _seed_used_entries(learner, ["X1"])
    result = apply_style_corpus_action(learner, {"action": "clear", "confirm": True})
    assert learner.entries() == []


def test_p1_t4_materials_panel_actions(tmp_path):
    """P1-T4：素材库读写（添加/删除/清空）+ 非法输入拒绝。"""
    learner, _ = make_learner(tmp=tmp_path)
    result = apply_style_materials_action(
        learner, {"action": "add", "text": "一段语料" * 10, "note": "备注"}
    )
    assert result["message"] == "已添加"
    mid = learner.materials()[0]["id"]
    with pytest.raises(PanelApiError):
        apply_style_materials_action(learner, {"action": "add", "text": "  "})
    with pytest.raises(PanelApiError):
        apply_style_materials_action(learner, {"action": "delete", "id": "ghost"})
    apply_style_materials_action(learner, {"action": "delete", "id": mid})
    assert learner.materials() == []
    apply_style_materials_action(learner, {"action": "add", "text": "再来一条"})
    with pytest.raises(PanelApiError):
        apply_style_materials_action(learner, {"action": "clear"})
    apply_style_materials_action(learner, {"action": "clear", "confirm": True})
    assert learner.materials() == []


def test_p1_t5_process_now_success_failure_and_rate_limit(tmp_path):
    """P1-T5：立即处理成功路径 + 失败提示 + 限频（连点只处理一次）。"""
    learner, llm = make_learner(HUMAN_DISTILL, tmp=tmp_path)
    # 空库
    result = asyncio.run(learner.process_now())
    assert result["ok"] is False and "没有待处理" in result["message"]
    # 有素材 → 成功
    learner.add_material(MATERIAL)
    result = asyncio.run(learner.process_now())
    assert result["ok"] is True
    # 限频：30 秒内第二次被拒
    result = asyncio.run(learner.process_now())
    assert result["ok"] is False and "稍后再试" in result["message"]
    # 稳定失败：判 AI → 明确提示
    learner2, llm2 = make_learner(AI_DISTILL, tmp=tmp_path / "b")
    (tmp_path / "b").mkdir(exist_ok=True)
    learner2.add_material(MATERIAL)
    result2 = asyncio.run(learner2.process_now())
    assert result2["ok"] is True and "AI" in result2["message"]
    # 瞬时失败 → 明确错误提示
    learner3, llm3 = make_learner(tmp=tmp_path / "c")
    (tmp_path / "c").mkdir(exist_ok=True)
    learner3.add_material(MATERIAL)
    llm3._responses.append(RuntimeError("network down"))
    result3 = asyncio.run(learner3.process_now())
    assert result3["ok"] is False and "失败" in result3["message"]


# ---------------------------------------------------------------------------
# L 组：触发扩展
# ---------------------------------------------------------------------------
def test_p1_t11_browser_read_evidence_triggers(tmp_path):
    """P1-T11：没有 fetch 样本但浏览器读了页面 → 触发学习（N5 呼应）。"""
    browser_reads = [
        {"url": "https://e.com/a", "title": "某帖", "text": MATERIAL,
         "at": NOW.isoformat()},
    ]
    learner, llm = make_learner(HUMAN_DISTILL, tmp=tmp_path, browser_reads=browser_reads)
    entry = asyncio.run(
        learner.on_activity_end("free", NOW, "act_1", started_at=NOW)
    )
    assert entry is not None and llm.calls
    assert "某帖" in learner.entries()[0]["source_note"]


def test_p1_t11_search_off_with_evidence_still_learns(tmp_path):
    """P1-T11/L2：搜索关闭 + 有读网证据 → 仍学习（浏览器不依赖搜索）。"""
    learner, llm = make_learner(
        HUMAN_DISTILL, tmp=tmp_path,
        config=learner_config(),
        samples=[sample(at=NOW.isoformat())],
    )
    entry = asyncio.run(
        learner.on_activity_end("surf", NOW, "act_1", started_at=NOW)
    )
    assert entry is not None


def test_p1_t11_no_evidence_no_learn(tmp_path):
    """P1-T11：无任何读网证据 → 不学（零调用）。"""
    learner, llm = make_learner(tmp=tmp_path)
    assert asyncio.run(
        learner.on_activity_end("game", NOW, "act_1", started_at=NOW)
    ) is None
    assert llm.calls == []


def test_p1_t11_old_evidence_not_counted(tmp_path):
    """L4：证据时间窗——活动开始前的留档不算本轮证据。"""
    old_browser = [
        {"url": "https://e.com/old", "title": "旧页",
         "text": MATERIAL, "at": "2026-10-01T08:00:00"},
    ]
    learner, llm = make_learner(tmp=tmp_path, browser_reads=old_browser)
    started = datetime(2026, 10, 6, 14, 0, 0)
    assert asyncio.run(
        learner.on_activity_end("free", started, "act_1", started_at=started)
    ) is None
    assert llm.calls == []


# ---------------------------------------------------------------------------
# P1-T13：缓存红线——新增调用的 contexts 为空
# ---------------------------------------------------------------------------
def test_p1_t13_new_llm_calls_carry_no_contexts(tmp_path):
    """P1-T13（缓存红线新增侧）：提炼/复盘/归纳调用不带会话历史
    （contexts 形参为空/None）。走独立 provider（对齐形态）时由 P0 批
    的 E 组统一决定，这里锁定"学习与复盘自身不主动传历史"。"""
    src = (Path(__file__).resolve().parents[1] / "core" / "style_learning.py").read_text(
        encoding="utf-8"
    )
    assert "contexts" not in src, "style_learning 的调用不应出现 contexts"
    src_review = (
        Path(__file__).resolve().parents[1] / "core" / "style_review.py"
    ).read_text(encoding="utf-8")
    assert "contexts" not in src_review
    # 行为面：FakeLLM 记录的调用 kwargs 无 contexts
    learner, llm = make_learner(tmp=tmp_path, samples=[sample()])
    asyncio.run(learner.on_activity_end("read", NOW, "act_1"))
    assert llm.calls[0]["system_prompt"] is None
    assert "contexts" not in llm.calls[0]["kwargs"]


# ---------------------------------------------------------------------------
# N 组：可点元素清单
# ---------------------------------------------------------------------------
def test_n_selector_priority_id_href_name_placeholder_path():
    from core.browser_elements import build_selector

    assert build_selector({"tag": "a", "id": "main-link", "href": "/next"}) == "#main-link"
    assert build_selector(
        {"tag": "a", "href": "/search?q=它&x=1"}
    ) == 'a[href="/search?q=它&x=1"]'
    assert build_selector(
        {"tag": "a", "href": '/a"b\\c'}
    ) == 'a[href="/a\\"b\\\\c"]', "特殊字符需转义"
    assert build_selector({"tag": "input", "name": "kw"}) == 'input[name="kw"]'
    assert build_selector(
        {"tag": "textarea", "placeholder": "说点什么"}
    ) == 'textarea[placeholder="说点什么"]'
    assert build_selector(
        {"tag": "button", "path": "body>div:nth-of-type(2)>button:nth-of-type(3)"}
    ) == "body>div:nth-of-type(2)>button:nth-of-type(3)"


def test_n_selector_stable_for_same_element():
    """N3：同一元素连续两次生成应一致。"""
    from core.browser_elements import build_selector

    info = {"tag": "a", "href": "/topic/123", "label": "下一页"}
    assert build_selector(info) == build_selector(dict(info))


def test_n_format_block_cap_and_labels():
    from core.browser_elements import ELEMENTS_CAP, format_elements_block

    infos = [
        {"kind": "link", "tag": "a", "label": f"链接{i}", "href": f"/p/{i}"}
        for i in range(80)
    ]
    block = format_elements_block(infos)
    lines = block.splitlines()
    assert len(lines) == ELEMENTS_CAP + 1, "清单上限 50 条（N1）"
    assert "链接" in lines[1] and "→" in lines[1]
    # 空清单 → 空串
    assert format_elements_block([]) == ""


def test_n_collect_elements_with_fake_page():
    """N2 闭环（无 Playwright 形态）：假 page → 采集 → 格式化。"""
    from core.browser_elements import collect_page_elements

    class FakePage:
        async def evaluate(self, script):
            assert "querySelectorAll" in script
            return [
                {"kind": "link", "tag": "a", "label": "下一页",
                 "href": "/page/2", "path": "body>a:nth-of-type(1)", "inMain": True},
                {"kind": "input", "tag": "input", "label": "搜索",
                 "placeholder": "输入关键词", "path": "body>input:nth-of-type(1)",
                 "inMain": False},
            ]

    block = asyncio.run(collect_page_elements(FakePage()))
    assert "下一页" in block and 'a[href="/page/2"]' in block
    assert "搜索" in block and "input[placeholder" in block


def test_n_collect_elements_error_is_silent():
    from core.browser_elements import collect_page_elements

    class BoomPage:
        async def evaluate(self, script):
            raise RuntimeError("page navigated away")

    assert asyncio.run(collect_page_elements(BoomPage())) == ""


def test_n_read_tool_appends_elements_and_notes(tmp_path, monkeypatch):
    """N1/N2 闭环：browser_read 返回正文 + 元素清单；读取留档成为学习证据；
    拿清单 → browser_click（假 page）→ 读新页，端到端形态验证。"""
    from core.browser_elements import ELEMENTS_JS
    from core.browser_tools import BrowserSession
    from core.living_tools import (
        BrowserClickTool,
        BrowserReadTool,
        BrowserSessionRef,
    )

    session = BrowserSession(str(tmp_path), write_level=0)

    class FakePage:
        url = "https://e.com/1"

        async def title(self):
            return "第一页"

        async def inner_text(self, sel):
            return "正文内容" * 10

        async def evaluate(self, script):
            assert script == ELEMENTS_JS
            return [
                {"kind": "link", "tag": "a", "label": "下一页",
                 "href": "/2", "path": "body>a:nth-of-type(1)", "inMain": True},
            ]

        async def click(self, selector, timeout=5000):
            self.clicked = selector

    page = FakePage()

    async def fake_ensure():
        return page

    session._ensure_page = fake_ensure
    ref = BrowserSessionRef(session)
    read_tool = BrowserReadTool().bind_session(ref)
    result = asyncio.run(read_tool.call(None))
    assert "「第一页」" in result and "正文内容" in result
    assert "可交互元素" in result and 'a[href="/2"]' in result
    # 读取留档（L1 证据）
    reads = session.recent_reads()
    assert len(reads) == 1 and reads[0]["title"] == "第一页"

    # 点击（write_level=1 放行 navigate/fill？点击 navigate kind 需 ≥1）
    session.write_level = 1
    click_tool = BrowserClickTool().bind_session(ref, 1)
    click_result = asyncio.run(
        click_tool.call(None, selector='a[href="/2"]', action_kind="navigate")
    )
    assert click_result == "已点击 a[href=\"/2\"]"
    # 选择器失效 → 明确报错（N3），不静默
    class BoomClickPage(FakePage):
        async def click(self, selector, timeout=5000):
            raise TimeoutError("selector did not match")

    session._ensure_page = lambda: _async_identity(BoomClickPage())
    boom = asyncio.run(
        click_tool.call(None, selector='a[href="/2"]', action_kind="navigate")
    )
    assert "点击失败" in boom and "browser_read" in boom


async def _async_identity(value):
    return value


def test_n_navigation_records_read(tmp_path):
    """browser_navigate 成功也留档（L1 证据来源之一）。"""
    from core.browser_tools import BrowserSession
    from core.living_tools import BrowserNavigateTool, BrowserSessionRef

    session = BrowserSession(str(tmp_path), write_level=0)

    class FakePage:
        async def title(self):
            return "标题"

        async def inner_text(self, sel):
            return "正文"

        async def goto(self, url, timeout=15000, wait_until=None):
            self.url = url

        async def context_cookies(self):
            return []

        @property
        def context(self):
            class C:
                async def cookies(self):
                    return []

            return C()

    async def fake_ensure():
        return FakePage()

    session._ensure_page = fake_ensure
    ref = BrowserSessionRef(session)
    tool = BrowserNavigateTool().bind_session(ref)
    result = asyncio.run(tool.call(None, url="https://e.com/"))
    assert "已打开" in result
    assert len(session.recent_reads()) == 1


# ---------------------------------------------------------------------------
# schema 守护：复盘/归纳提示词默认值与代码常量逐字一致
# ---------------------------------------------------------------------------
def test_schema_prompt_defaults_match_code_constants():
    from core.panel_api import load_schema

    schema = load_schema(Path(__file__).resolve().parents[1])
    items = schema["advanced"]["items"]["style_learning"]["items"]
    assert items["prompt_distill"]["default"] == DEFAULT_PROMPT_DISTILL
    assert items["prompt_review"]["default"] == DEFAULT_PROMPT_REVIEW
    assert items["prompt_induct"]["default"] == DEFAULT_PROMPT_INDUCT


def test_schema_p1_style_learning_keys_complete():
    from core.panel_api import load_schema

    schema = load_schema(Path(__file__).resolve().parents[1])
    items = schema["advanced"]["items"]["style_learning"]["items"]
    expected = {
        "material_max_chars": 1200, "min_material_chars": 80,
        "item_max_chars": 80, "manual_weight": 1.2,
        "feature_importance_cap": 1.5, "daily_review_enabled": True,
        "daily_review_time": "04:00", "usage_log_limit": 500,
        "material_pool_limit": 100, "feature_promote_threshold": 3,
    }
    for key, default in expected.items():
        assert key in items, f"缺配置键 {key}"
        assert items[key]["default"] == default


def test_m4_all_new_config_keys_in_schema():
    """M4（红线）：所有新增配置项必须有前端入口——schema 存在即有入口
    （面板按 schema 渲染）。"""
    from core.panel_api import load_schema

    schema = load_schema(Path(__file__).resolve().parents[1])
    items = schema["advanced"]["items"]["style_learning"]["items"]
    for key in ("material_max_chars", "manual_weight", "feature_importance_cap",
                "daily_review_enabled", "daily_review_time",
                "usage_log_limit", "material_pool_limit",
                "feature_promote_threshold"):
        assert key in items
    js = (Path(__file__).resolve().parents[1] / "pages" / "config" / "app.js"
          ).read_text(encoding="utf-8")
    assert "styleLibraryAdmin" in js and "styleDataCard" in js
    assert "style_process" in js and "style_materials" in js and "style_corpus" in js
