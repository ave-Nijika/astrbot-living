"""M34-补丁1 测试：面板档位联动提示（新手旋钮与实际生效值不一致时告知）。

A 组（Python）：payload 下发旋钮映射表（验收 1，同一份来源——改映射表后
   payload 自动跟随；副本隔离）。
B 组（node 桥 knobMismatchProbe，真实 app.js 全链路渲染）：
   档位类整组判定（验收 2/3/4）、直通类（验收 5）、schema 默认兜底、
   全新安装默认态已知行为、专家页 mapped-chip 增强（已脱离/仍在档内）、
   旧后端 payload（无映射表字段）不亮提示。
C 组（node 桥）：preset_model 卡状态行显示真正的实际生效值（验收 6）；
   judge.provider_id 专家卡/新手卡行为不变（验收 7）。
验收 8（零回归）由全量测试承担；node 缺失时 B/C 组整体 skip。

红线对应：只提示不回写（本批无任何"专家键→旋钮"回写代码，测试断言
payload 保存链路未变——buildSavePayload/diffSection 源码锚点在 C 组源码
检查里）。
"""

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from core.config_knobs import DIRECT_KNOB, KNOB_PRESETS
from core.panel_api import build_config_payload, load_schema

WORKDIR = Path(__file__).resolve().parents[1]
SCHEMA = load_schema(WORKDIR)
LAYOUT = json.loads((WORKDIR / "panel_layout.json").read_text(encoding="utf-8"))
APP_JS = (WORKDIR / "pages" / "config" / "app.js").read_text(encoding="utf-8")
sys.path.insert(0, str(WORKDIR / "scripts"))

HAS_NODE = shutil.which("node") is not None
NODE_SKIP = pytest.mark.skipif(not HAS_NODE, reason="node 不可用（JS 引擎行为测试）")

HARNESS = WORKDIR / "tests" / "js" / "status_engine_harness.mjs"

# 新手旋钮卡标题（schema description，按它识别 knobMismatchProbe 摘要里的卡）
TITLE_MODEL = "它独处时用哪个 AI 大脑"
TITLE_ACTIVITY = "它独处时折腾的频率"
TITLE_TALK = "它主动找你说话的频率"
TITLE_TOPIC = "它聊天的口味"


def run_probe(payload, op="knobMismatchProbe"):
    proc = subprocess.run(
        ["node", str(HARNESS)],
        input=json.dumps({"op": op, "payload": payload}),
        capture_output=True, text=True, timeout=60, encoding="utf-8",
    )
    assert proc.returncode == 0, f"node 桥失败: {proc.stderr[-800:]}"
    return json.loads(proc.stdout)


def knob_payload(knobs, advanced, providers=("p-a", "p-b"), with_map=True):
    """probe 用 payload：映射表字段从 core.config_knobs 常量序列化（同一份
    来源，前端测试不硬编码第二份映射）。with_map=False 模拟旧后端。"""
    payload = {
        "knobs": knobs,
        "advanced": advanced,
        "schema": {
            "preset": {"items": SCHEMA["preset"]["items"]},
            "advanced": {"items": SCHEMA["advanced"]["items"]},
        },
        "layout": LAYOUT,
        "providers": list(providers),
        "agent_tools": [],
    }
    if with_map:
        payload["knob_presets"] = json.loads(json.dumps(KNOB_PRESETS))
        payload["knob_direct"] = DIRECT_KNOB
    return payload


def card_of(data, title):
    matches = [c for c in data["novice"] if c["title"] == title]
    assert matches, f"新手页找不到卡片: {title}（现有: {[c['title'] for c in data['novice']]}）"
    return matches[0]


def chip_of(data, code):
    matches = [c for c in data["mappedChips"] if c["code"] == code]
    assert matches, f"专家页找不到带档位联动 chip 的键行: {code}"
    return matches[0]


def aligned_knobs():
    """8 个旋钮全部取 schema 默认档（全新安装形态）。"""
    return {
        "preset_activity_level": "normal",
        "preset_talk_frequency": "normal",
        "preset_capability_tier": "watch",
        "preset_write_level": "read",
        "preset_topic_taste": "balanced",
        "preset_free_activity": "on",
        "preset_decision_mode": "normal",
        "preset_model": "",
    }


def aligned_advanced(overrides=None):
    """与默认档定义逐键相等的底层值（除 impulse 三键外其余默认档恰等于
    schema 默认；impulse_check_interval_minutes/daily_impulse_limit 的
    schema 默认与 normal 档定义本就不对齐——历史既成事实，M34 不改）。
    overrides: {组: {键: 值}} 原地覆盖。"""
    advanced = {
        "decision": {
            "impulse_check_interval_minutes": 45, "activity_probability": 0.8,
            "daily_impulse_limit": 0,
            "interest_daily_decay": 0.7, "recent_topic_window": 6,
            "exploration_trigger": 3,
            "free_activity_enabled": True, "decision_mode": "hybrid",
        },
        "output_gate": {"daily_message_limit": 10, "message_min_interval_minutes": 30},
        "autonomy": {"tier": 1, "write_level": 0},
        "model": {"provider_id": ""},
    }
    for group, keys in (overrides or {}).items():
        advanced.setdefault(group, {}).update(keys)
    return advanced


# ---------------------------------------------------------------------------
# A 组：映射表下发（验收 1）
# ---------------------------------------------------------------------------

def test_a1_payload_carries_knob_map():
    """验收 1：payload 含映射表字段，内容与 KNOB_PRESETS/DIRECT_KNOB 一致。"""
    payload = build_config_payload({"preset": {}, "advanced": {}}, SCHEMA)
    assert payload["knob_presets"] == KNOB_PRESETS
    assert payload["knob_direct"] == DIRECT_KNOB == "preset_model"
    # 深拷贝隔离：前端拿到的副本与常量不是同一对象（防御引用共享）
    assert payload["knob_presets"] is not KNOB_PRESETS


def test_a1_same_source_follows_mapping_changes():
    """验收 1（同一份来源）：往 KNOB_PRESETS 原地加假档，payload 自动跟随——
    证明后端没有第二份映射常量。"""
    KNOB_PRESETS["preset_fake_knob"] = {"x": {"decision": {"max_run_seconds": 999}}}
    try:
        payload = build_config_payload({"preset": {}, "advanced": {}}, SCHEMA)
        assert "preset_fake_knob" in payload["knob_presets"]
    finally:
        del KNOB_PRESETS["preset_fake_knob"]
    payload = build_config_payload({"preset": {}, "advanced": {}}, SCHEMA)
    assert "preset_fake_knob" not in payload["knob_presets"]


def test_a1_payload_size_bounded():
    """A 组：映射表极小（json < 3KB），不构成面板加载负担。"""
    assert len(json.dumps(KNOB_PRESETS, ensure_ascii=False)) < 3000


# ---------------------------------------------------------------------------
# B 组：档位类整组判定 + 新手页提示（验收 2/3/4，node 桥）
# ---------------------------------------------------------------------------

@NODE_SKIP
def test_b2_aligned_no_note():
    """验收 2：旋钮值与底层键完全对应 → 该旋钮无提示（一致不打扰）。"""
    data = run_probe(knob_payload(aligned_knobs(), aligned_advanced()))
    assert data["loadError"] == ""
    for card in data["novice"]:
        if card["title"].startswith("它"):  # 8 张旋钮卡全部一致
            assert not card["mismatch"], card["title"]
    assert card_of(data, TITLE_ACTIVITY)["mismatch"] is False


@NODE_SKIP
def test_b3_diverged_shows_note():
    """验收 3：底层键改成非任何档位值（impulse 45→10）→ 提示出现；
    同屏其他一致旋钮不亮（不误伤）。"""
    data = run_probe(knob_payload(
        aligned_knobs(),
        aligned_advanced({"decision": {"impulse_check_interval_minutes": 10}}),
    ))
    act = card_of(data, TITLE_ACTIVITY)
    assert act["mismatch"] is True
    # 文案面向用户：说清"显示≠生效"与"怎么办"，不出现内部键名
    assert "实际生效" in act["noteText"]
    for word in ("impulse", "decision", "preset", "advanced", "provider_id"):
        assert word not in act["noteText"]
    assert card_of(data, TITLE_TOPIC)["mismatch"] is False
    assert card_of(data, TITLE_TALK)["mismatch"] is False


@NODE_SKIP
def test_b4_multi_key_partial_change_counts_as_mismatch():
    """验收 4：多对一整组判——topic 三键只改 1 个（window 6→8），
    另两个仍命中档位定义，仍必须判不一致。"""
    data = run_probe(knob_payload(
        aligned_knobs(),
        aligned_advanced({"decision": {"recent_topic_window": 8}}),
    ))
    assert card_of(data, TITLE_TOPIC)["mismatch"] is True
    assert card_of(data, TITLE_ACTIVITY)["mismatch"] is False


@NODE_SKIP
def test_b_missing_group_counts_as_mismatch():
    """档位组整组缺失（缺键走 schema 默认；组定义值≠schema 默认时即不一致）：
    output_gate 组整个不存在 → daily_message_limit 按默认 10 判（normal 档
    定义恰为 10），仍一致——证明"缺键"不武断判不一致，而是按实际生效值判。"""
    advanced = aligned_advanced()
    del advanced["output_gate"]
    data = run_probe(knob_payload(aligned_knobs(), advanced))
    assert card_of(data, TITLE_TALK)["mismatch"] is False
    # 反向：把组删掉但旋钮在 rare 档（定义 3/90 ≠ 默认 10/30）→ 不一致
    knobs = aligned_knobs()
    knobs["preset_talk_frequency"] = "rare"
    data = run_probe(knob_payload(knobs, advanced))
    assert card_of(data, TITLE_TALK)["mismatch"] is True


@NODE_SKIP
def test_b_unknown_display_value_no_note():
    """显示值不在预设表（未知档/空）→ 无法判定不亮提示。"""
    knobs = aligned_knobs()
    knobs["preset_activity_level"] = ""  # 空档：无从比对
    data = run_probe(knob_payload(knobs, aligned_advanced()))
    assert card_of(data, TITLE_ACTIVITY)["mismatch"] is False


@NODE_SKIP
def test_b_fresh_install_default_state():
    """全新安装默认态（advanced 全空，判定走 schema 默认）的已知行为：
    只有 preset_activity_level 一处亮——schema 默认 5/3 与 normal 档定义
    45/0 本就不对齐（历史既成事实，本批不改运行时默认值）。"""
    data = run_probe(knob_payload(aligned_knobs(), {}))
    expect = {
        TITLE_ACTIVITY: True,   # 5/0.8/3 ≠ 45/0.8/0
        TITLE_TALK: False,      # 10/30 == 10/30
        "它有多少手脚": False,   # tier 1 == 1
        "它在网上能做到什么程度": False,  # write_level 0 == 0
        TITLE_TOPIC: False,     # 0.7/6/3 == 0.7/6/3
        "允许它自由发挥": False,  # True == True
        "让它想事情花多少心思": False,  # hybrid == hybrid
        TITLE_MODEL: False,     # '' == ''
    }
    for title, want in expect.items():
        assert card_of(data, title)["mismatch"] is want, title


@NODE_SKIP
def test_b_legacy_payload_without_map_no_notes():
    """旧后端（payload 无映射表字段）：判定全部静默，面板行为与从前一致。"""
    data = run_probe(knob_payload(
        aligned_knobs(),
        aligned_advanced({"decision": {"impulse_check_interval_minutes": 10}}),
        with_map=False,
    ))
    for card in data["novice"]:
        assert not card["mismatch"], card["title"]


@NODE_SKIP
def test_b_expert_chip_enhanced():
    """B 组：专家页 mapped-chip 增强与新手页同口径——脱离时标注"已脱离"
    且 title 说明不一致；在档内时 title 说明一致。"""
    data = run_probe(knob_payload(
        aligned_knobs(),
        aligned_advanced({"decision": {"impulse_check_interval_minutes": 10}}),
    ))
    off = chip_of(data, "decision.impulse_check_interval_minutes")
    assert off["chipText"] == "档位联动·已脱离"
    assert "不一致" in off["chipTitle"]
    in_knob = chip_of(data, "decision.activity_probability")
    assert in_knob["chipText"] == "档位联动"
    assert "一致" in in_knob["chipTitle"]
    # 直通键同口径：preset_model=p-a vs model.provider_id=p-b → 已脱离
    knobs = aligned_knobs()
    knobs["preset_model"] = "p-a"
    data = run_probe(knob_payload(
        knobs, aligned_advanced({"model": {"provider_id": "p-b"}}),
    ))
    assert chip_of(data, "model.provider_id")["chipText"] == "档位联动·已脱离"


# ---------------------------------------------------------------------------
# C 组：preset_model 卡真实生效值（验收 5/6）+ judge 不误伤（验收 7）
# ---------------------------------------------------------------------------

@NODE_SKIP
def test_c5_direct_knob_mismatch_note():
    """验收 5：preset.preset_model ≠ model.provider_id → 提示出现；
    相等时不出现。"""
    knobs = aligned_knobs()
    knobs["preset_model"] = "p-a"
    data = run_probe(knob_payload(
        knobs, aligned_advanced({"model": {"provider_id": "p-b"}}),
    ))
    assert card_of(data, TITLE_MODEL)["mismatch"] is True
    data = run_probe(knob_payload(
        knobs, aligned_advanced({"model": {"provider_id": "p-a"}}),
    ))
    assert card_of(data, TITLE_MODEL)["mismatch"] is False


@NODE_SKIP
def test_c6_status_shows_real_effective_value():
    """验收 6：新手 provider 卡"当前实际生效"显示的是 advanced.model.
    provider_id（不再是旋钮旧值）。三个形态：独立/不在列表/留空。"""
    knobs = aligned_knobs()
    knobs["preset_model"] = "p-a"
    data = run_probe(knob_payload(
        knobs, advanced=aligned_advanced({"model": {"provider_id": "p-b"}}),
    ))
    assert card_of(data, TITLE_MODEL)["providerStatus"] == "当前实际生效：p-b（独立 provider）"
    # 实际生效值不在已启用列表（专家填了停用的 id）——如实报出
    data = run_probe(knob_payload(
        knobs, advanced=aligned_advanced({"model": {"provider_id": "p-gone"}}),
        providers=("p-a",),
    ))
    assert card_of(data, TITLE_MODEL)["providerStatus"] == "当前实际生效：p-gone（不在已启用列表中）"
    # 实际留空：显示共用模型语义（不受旋钮选了 p-a 影响）
    data = run_probe(knob_payload(
        knobs, advanced=aligned_advanced({"model": {"provider_id": ""}}),
    ))
    assert card_of(data, TITLE_MODEL)["providerStatus"] == "当前实际生效：聊天模型（共用账号）"


@NODE_SKIP
def test_c7_judge_provider_unchanged():
    """验收 7：judge.provider_id 新手卡/专家卡共用 advanced.judge，本就同步
    ——两处行为不变（专家卡控件渲染选中值；新手卡状态行文案原样）。"""
    advanced = aligned_advanced({"judge": {"mode": "api", "provider_id": "p-j"}})
    data = run_probe(knob_payload(aligned_knobs(), advanced, providers=("p-a", "p-b", "p-j")))
    judge_card = card_of(data, "判断模型")
    assert judge_card["mismatch"] is False  # judge 卡不是旋钮卡，不挂档位提示
    assert "p-j" in (judge_card["judgeStatus"] or "")
    # 专家页 key-row：judge.provider_id 控件选中 p-j（renderPanel 摘要）
    data2 = run_probe(knob_payload(aligned_knobs(), advanced, providers=("p-a", "p-b", "p-j")),
                      op="renderPanel")
    row = next(r for r in data2["keyRows"] if r["code"] == "judge.provider_id")
    assert "p-j" in (row["control"] or {}).get("text", "")
    assert not row["hasMappedChip"]  # judge.provider_id 不在旋钮映射内


# ---------------------------------------------------------------------------
# 红线源码检查：只提示不回写 + 保存链路零改动
# ---------------------------------------------------------------------------

def test_redline_no_reverse_writeback():
    """红线 1：app.js 不得出现"专家键变化→写 knobs"的反向回写——判定函数
    只读 state（读形态允许，赋值形态禁止）。"""
    assign = re.compile(r"state\.values\.knobs\[[^\]]*\]\s*=(?!=)")
    for fn in ("function knobMismatch", "function mappedKeyKnobStatus", "function knobEffectiveValue"):
        idx = APP_JS.index(fn)
        end = APP_JS.find("\nfunction ", idx + 1)
        body = APP_JS[idx:end if end > 0 else len(APP_JS)]
        assert not assign.search(body), f"{fn} 内不得回写旋钮值"


def test_redline_save_chain_untouched():
    """红线 4：保存链路函数签名/入口零改动（源码锚点）。"""
    for anchor in (
        "function buildSavePayload(",
        "function diffSection(",
    ):
        assert anchor in APP_JS


def test_redline_frontend_uses_payload_map():
    """红线 2：前端不硬编码第二份映射——app.js 内不得出现档位映射值常量
    （如 45/90/20 档间隔或 0.95/0.5 衰减值成组出现）。"""
    assert "state.knobPresets = payload.knob_presets" in APP_JS
    for literal in ('"impulse_check_interval_minutes": 45', "'impulse_check_interval_minutes': 45",
                    '"interest_daily_decay": 0.95', "'interest_daily_decay': 0.95"):
        assert literal not in APP_JS
