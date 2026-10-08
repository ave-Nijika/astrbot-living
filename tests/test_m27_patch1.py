"""M27-补丁1 测试：生效链面板挂载 + dom_stub 选择器加固 + 7.1/7.2/7.3 面板
修复组 + 7.4 peek 看留言改造。

断言方式与 M26 同源：node 桥 + 最小 DOM 桩让**真实 app.js**全链路渲染，
对桩树做装配级断言（非源码正则）；7.4 走真实 PeekFeedbackActivity 与
LivingLoop 周期（test_m13 的 make_loop 替身，跨文件复用为本仓先例）。
"""

import asyncio
import json
import subprocess
from datetime import datetime
from pathlib import Path

import pytest

from core.activities import PeekFeedbackActivity, ActivityContext

WORKDIR = Path(__file__).resolve().parents[1]
SCHEMA = json.loads((WORKDIR / "_conf_schema.json").read_text(encoding="utf-8"))
LAYOUT = json.loads((WORKDIR / "panel_layout.json").read_text(encoding="utf-8"))
APP_JS = (WORKDIR / "pages" / "config" / "app.js").read_text(encoding="utf-8")

HARNESS = WORKDIR / "tests" / "js" / "status_engine_harness.mjs"
HAS_NODE = __import__("shutil").which("node") is not None
NODE_SKIP = pytest.mark.skipif(not HAS_NODE, reason="node 不可用（DOM 桩渲染测试）")

# 布局声明里带 requires 的 6 个组（组级生效链面板）与 3 个 requires 键（键级）
GROUP_CHAIN_GROUPS = {"A2", "A3", "A4", "F2", "F3", "F4"}
KEY_CHAIN_KEYS = {
    "autonomy.tier", "sleep.pending_reply_enabled",
    "style_learning.daily_review_enabled",
}


def build_payload(**overrides):
    payload = {
        "knobs": {},
        "advanced": {},
        "schema": {
            "preset": {"items": SCHEMA["preset"]["items"]},
            "advanced": {"items": SCHEMA["advanced"]["items"]},
        },
        "layout": LAYOUT,
        "providers": ["p-chat"],
        "agent_tools": ["recall_long_term_memory", "web_search", "fetch_page"],
    }
    payload.update(overrides)
    return payload


def run_harness(req, timeout=60):
    proc = subprocess.run(
        ["node", str(HARNESS)],
        input=json.dumps(req),
        capture_output=True, text=True, timeout=timeout, encoding="utf-8",
    )
    assert proc.returncode == 0, f"node 桥失败: {proc.stderr[-800:]}"
    return json.loads(proc.stdout)


# ---------------------------------------------------------------------------
# 验收 1/2/4：chain-panel 挂载（键级 3 + 组级 6 = 9）、默认收起、位置正确
# ---------------------------------------------------------------------------
@NODE_SKIP
def test_acc1_chain_panels_mounted_collapsed_in_place():
    data = run_harness({"op": "renderPanel", "payload": build_payload()})
    assert data["loadError"] == ""
    panels = data["chainPanels"]
    assert len(panels) == 9, f"chain-panel 可达数应为 9，实际 {len(panels)}"
    # 默认收起（密度纪律不变）
    assert all(p["hidden"] for p in panels), "9 个面板必须全部默认带 hidden"
    # 位置正确：与按钮同一容器（key-side / group-chain）的兄弟节点
    by_parent = {"key-side": 0, "group-chain": 0}
    for p in panels:
        assert p["parentCls"] in by_parent, f"面板挂错容器: {p['parentCls']!r}"
        by_parent[p["parentCls"]] += 1
        assert p["siblingToggle"], "面板必须与 .chain-toggle 同容器相邻"
    assert by_parent == {"key-side": 3, "group-chain": 6}, (
        f"键级 3 / 组级 6 的分布被破坏: {by_parent}"
    )
    # 挂载数与声明数互洽：键级 = schema requires 键数，组级 = layout requires 组数
    req_groups = {
        g["id"]
        for sec in LAYOUT["sections"] for g in sec.get("groups", [])
        if g.get("requires")
    }
    assert req_groups == GROUP_CHAIN_GROUPS
    req_keys = {
        f"{group}.{key}"
        for group, body in SCHEMA["advanced"]["items"].items()
        for key, item in body["items"].items()
        if item.get("requires")
    }
    assert req_keys == KEY_CHAIN_KEYS


# ---------------------------------------------------------------------------
# 验收 3：点击交互——hidden 移除/恢复，按钮文案 ▸/▾ 切换
# ---------------------------------------------------------------------------
@NODE_SKIP
def test_acc3_chain_panel_click_toggles():
    data = run_harness({"op": "renderPanel", "payload": build_payload()})
    probe = data["chainProbe"]
    assert probe.get("mounted") is True, "面板未挂进 DOM（修前形态）"
    assert probe["toggles"] == 9
    assert probe["before"] is True, "点击前面板应收起"
    assert probe["openHidden"] is False, "点击后面板应展开（hidden 移除）"
    assert probe["openText"] == "生效链 ▾"
    assert probe["closedHidden"] is True, "再点一次应恢复收起"
    assert probe["closeText"] == "生效链 ▸"


# ---------------------------------------------------------------------------
# 验收 5（修前必红）：还原"只 append 按钮"形态 → 上面两条必须红
# （红方流程见报告；此处以源码锚点补一条结构性断言：
#   buildKeyRow/buildGroup 内 panel 必须随按钮一起 append）
# ---------------------------------------------------------------------------
@NODE_SKIP
def test_acc5_chain_panel_mount_source_anchor():
    # 该锚点验证代码写法，不替代浏览器渲染——渲染行为由 acc1/acc3 的
    # DOM 桩断言与维护者的 VM 实测覆盖。
    assert "side.append(chainToggleBtn(panel), panel)" in APP_JS
    assert "chainWrap.append(chainToggleBtn(panel), panel)" in APP_JS
    # 修前形态（按钮单独 append、panel 游离）不得回归
    assert "side.append(chainToggleBtn(chainPanelEl(" not in APP_JS
    assert "chainWrap.append(chainToggleBtn(chainPanelEl(" not in APP_JS


# ---------------------------------------------------------------------------
# 验收 6：dom_stub 桩加固——~ 选择器正例 + 不支持的选择器抛错
# ---------------------------------------------------------------------------
@NODE_SKIP
def test_stub_sibling_selector_positive():
    out = run_harness({"op": "siblingProbe"})
    assert out["count"] == 2, f"~ 应命中 toggle 之后的 2 张卡，实际 {out['count']}"
    assert out["hits"] == ["b1", "b2"], "命中次序必须按文档序"
    assert out["noSiblingBefore"] is True, "toggle 之前的兄弟不得命中"
    assert out["tagButtonsInLabel"] == 1, "tag 选择器（嵌套）正例"
    assert out["tagButtonsInParent"] == 1, "tag 选择器（递归后代）正例"


@NODE_SKIP
def test_stub_render_path_uses_sibling_selector():
    # 渲染路径真的走 .novice-detail-toggle ~ .knob-card：13 张功能卡被标记
    data = run_harness({"op": "renderPanel", "payload": build_payload()})
    assert data["loadError"] == ""
    assert data["noviceDetailCards"] == 13
    assert data["noviceKnobCards"] == 21  # 8 旋钮 + 13 功能卡


@NODE_SKIP
def test_stub_unsupported_selector_throws():
    out = run_harness({
        "op": "selectorThrowProbe",
        "selectors": ["div > .x", ".a ~ .b ~ .c", "[data-x]", ".a.b", ""],
    })
    for sel, result in out.items():
        assert result == "threw", f"不支持的选择器 {sel!r} 必须抛错，实际 {result}"


# ---------------------------------------------------------------------------
# 配套 d：DOM 层级联置灰覆盖（style_learning.enabled=false → dimmed + 因果行）
# ---------------------------------------------------------------------------
@NODE_SKIP
def test_acc_d_cascade_dimming_visible_in_dom():
    payload = build_payload(advanced={"style_learning": {"enabled": False}})
    data = run_harness({"op": "renderPanel", "payload": payload})
    assert data["loadError"] == ""
    dimmed = [r for r in data["keyRows"] if r["dimmed"]]
    assert dimmed, "总闸关闭时必须出现 dimmed 键行（级联置灰）"
    assert all(r["code"].startswith("style_learning.") for r in dimmed)
    # 因果说明节点：级联休眠的组必须带 cascade-note，且文案指向「风格学习总闸」
    cascaded = [g for g in data["groups"] if g["hasCascadeNote"]]
    assert cascaded, "被级联休眠的组必须渲染因果说明（.cascade-note）"
    assert all("风格学习总闸" in " ".join(g["cascadeNotes"]) for g in cascaded)
    assert all(g["dimmedKeyRows"] > 0 for g in cascaded), "因果行的组内键行必须被置灰"


# ---------------------------------------------------------------------------
# 7.2：状态点实时重算（不点保存、即时生效；保滚动/展开）
# ---------------------------------------------------------------------------
@NODE_SKIP
def test_72_status_dot_recomputes_on_change():
    payload = build_payload(advanced={"judge": {"mode": "off", "provider_id": ""}})
    data = run_harness({"op": "statusRecomputeProbe", "payload": payload})
    assert data["loadError"] == ""
    steps = {s["step"]: s for s in data["steps"]}
    a2_init = steps["初始(off)"]["a2"]
    assert a2_init["dotText"] == "● 关" and a2_init["countText"].endswith("0/4")
    assert steps["初始(off)"]["a3"]["dotText"] == "● 哑"

    a2_api = steps["mode→api(provider空)"]["a2"]
    assert a2_api["dotText"] == "▲ 卡着", "mode→api 且 provider 空 → 组头应变卡着"
    assert steps["mode→api(provider空)"]["a3"]["dotText"] == "● 开"

    a2_full = steps["provider→p-chat"]["a2"]
    assert a2_full["dotText"] == "● 开", "provider 配上 → 组头变开"
    assert a2_full["countText"].endswith("4/4"), "生效计数同步变化"

    a2_off = steps["mode→off"]["a2"]
    assert a2_off["dotText"] == "● 关", "改回 off → 组头变关"
    assert steps["mode→off"]["a3"]["dotText"] == "● 哑"

    # 滚动恢复：3 次相关变化各触发一次 scrollTo(0, 4321)
    assert data["scrolls"] == [4321, 4321, 4321], "重渲染必须恢复滚动位置"
    # 展开态保持：点击展开后经历 3 次重渲染，A2 body 仍展开
    assert a2_api["bodyHidden"] is False and a2_full["bodyHidden"] is False
    assert a2_off["bodyHidden"] is False
    # 无关键（预算数字）变化不触发重渲染
    assert steps["无关键变化(预算数字)"]["rerendered"] is False


# ---------------------------------------------------------------------------
# 7.1：judge.provider_id 渲染为 provider 下拉；留空语义文案准确
# ---------------------------------------------------------------------------
@NODE_SKIP
def test_71_judge_provider_renders_picker_with_accurate_empty_copy():
    data = run_harness({"op": "renderPanel", "payload": build_payload()})
    rows = {r["code"]: r for r in data["keyRows"]}
    judge = rows["judge.provider_id"]["control"]
    assert "provider-picker" in judge["childCls"], "judge.provider_id 必须渲染为 provider 下拉"
    assert "（留空 = 不判断）" in judge["text"]
    assert "不判断（输入建议与输出检查都不运行）" in judge["text"], "留空状态行必须是 judge 语义"


@NODE_SKIP
def test_71_model_provider_keeps_original_semantics():
    data = run_harness({"op": "renderPanel", "payload": build_payload()})
    rows = {r["code"]: r for r in data["keyRows"]}
    model = rows["model.provider_id"]["control"]
    assert "provider-picker" in model["childCls"]
    assert "（留空 = 与聊天共用模型）" in model["text"], "model 的留空语义不得被 judge 文案污染"
    assert "不判断" not in model["text"]


# ---------------------------------------------------------------------------
# 7.3：工具白名单多选——中文说明、未知工具保留、保存仍逗号分隔串
# ---------------------------------------------------------------------------
@NODE_SKIP
def test_73_agent_tools_descriptions_and_save_contract():
    payload = build_payload(
        advanced={"capabilities": {
            "agent_tools": "recall_long_term_memory,web_search,ghost_tool",
            "agent_tools_mode": "custom",
        }},
    )
    data = run_harness({"op": "toolsProbe", "payload": payload})
    assert data["found"] is True
    assert data["loadError"] == ""
    labels = data["labels"]
    assert "recall_long_term_memory（回忆长期记忆）" in labels
    assert "web_search（联网搜索）" in labels
    assert "fetch_page（抓取网页内容）" in labels
    assert "ghost_tool（当前注册表里没有，请核对）" in labels, "未知工具必须保留显示并标注"
    # 勾选 fetch_page 后保存：仍是逗号分隔字符串，ghost_tool 不被丢弃
    assert any(c.startswith("ghost_tool") for c in data["checkedAfter"])
    posts = [p for p in data["savePosts"] if p["endpoint"] == "config"]
    assert posts, "保存请求未被捕获"
    saved = posts[0]["body"]["advanced"]["capabilities"]["agent_tools"]
    assert saved == "recall_long_term_memory,web_search,fetch_page,ghost_tool", (
        f"保存契约被破坏（须逗号分隔字符串）: {saved!r}"
    )


# ---------------------------------------------------------------------------
# 7.4：peek 看留言——三情形经历、绝不发送、真留经历
# ---------------------------------------------------------------------------
NOW = datetime(2026, 10, 8, 15, 0, 0)


class PeekStateGate:
    """带 state_get 的最小闸门替身；should_send_message 被调即测试失败。"""

    def __init__(self, state):
        self._state = state
        self.send_gate_calls = 0

    async def state_get(self, key):
        return self._state.get(key)

    async def should_send_message(self, now=None):
        self.send_gate_calls += 1
        return True, "ok"


def _peek_ctx(gate):
    return ActivityContext(
        searcher=None, fetcher=None, sandbox=None, memory=None,
        gate=gate, event=None, rng=__import__("random").Random(7), now=NOW,
    )


def _run_peek(state):
    gate = PeekStateGate(state)
    outcome = asyncio.run(PeekFeedbackActivity().run(_peek_ctx(gate)))
    return outcome, gate


def test_74_peek_unanswered_streak_writes_missing_memory():
    """streak≥1（有未回）→ 惦记；经历非空；不发送。"""
    outcome, gate = _run_peek({
        "last_message_at": "2026-10-08T10:00:00",
        "initiative_unanswered_streak": "2",
    })
    assert outcome.summary is None, "peek 不得产生候选发送（summary 必须为空）"
    assert outcome.memory_content, "必须留下经历"
    assert "还没回" in outcome.memory_content and "惦记" not in outcome.memory_content
    assert "对方" in outcome.memory_content, "指代必须用「对方」（中性化口径）"
    assert gate.send_gate_calls == 0, "peek 不得触碰发送闸门"


def test_74_peek_answered_writes_reassured_memory():
    """streak=0 且有主动消息历史 → 安心。"""
    outcome, _ = _run_peek({
        "last_message_at": "2026-10-08T10:00:00",
        "initiative_unanswered_streak": "0",
    })
    assert outcome.summary is None
    assert "对方接了" in outcome.memory_content


def test_74_peek_no_history_writes_expectant_memory():
    """无主动消息历史 → 淡淡的期待。"""
    outcome, _ = _run_peek({})
    assert outcome.summary is None
    assert outcome.memory_content
    assert "还没翻到我主动开过口的记录" in outcome.memory_content


def test_74_peek_tolerates_dirty_state_values():
    """streak 坏值/时间戳坏值按缺失处理，不抛出（宽容读取）。"""
    outcome, _ = _run_peek({
        "last_message_at": "不是时间",
        "initiative_unanswered_streak": "abc",
    })
    assert outcome.summary is None
    assert "还没翻到我主动开过口的记录" in outcome.memory_content


def test_74_peek_gate_without_state_get_still_works():
    """gate 不支持 state_get（老闸门替身/极端环境）→ 无历史情形，不崩。"""
    class BareGate:
        async def should_send_message(self, now=None):
            return True, "ok"

    ctx = _peek_ctx(BareGate())
    outcome = asyncio.run(PeekFeedbackActivity().run(ctx))
    assert outcome.summary is None and outcome.memory_content


def test_74_peek_in_real_cycle_writes_experience_and_stays_silent():
    """抽中 peek → 真实周期里经历落库（对话上下文），分享零条。"""
    from test_m13_patch1 import FakeGate, make_loop

    class PeekGate(FakeGate):
        def __init__(self):
            super().__init__(allow_message=True)
            self._state = {
                "last_message_at": "2026-10-08T10:00:00",
                "initiative_unanswered_streak": "1",
            }

        async def state_get(self, key):
            return self._state.get(key)

    loop, mgr, lm, sender, memory = make_loop(PeekFeedbackActivity(), gate=PeekGate())
    result = asyncio.run(loop.run_activity_cycle(NOW))
    assert result["activity"] == "peek" and result["ok"] is True
    assert len(mgr.pairs) == 1, "经历必须写进对话上下文（不再是零产出）"
    assert "还没回" in mgr.pairs[0][2]["content"]
    assert sender.sent == [], "peek 仍然不发送（定位不变）"
    assert lm.calls, "livingmemory 侧同样落库"


def test_74_peek_never_enters_agent_mode():
    """peek 保持脚本模式：即使 ctx.agent 存在也不调用。"""
    from test_agent_loop import _ctx_with_agent  # 复用既有替身构造

    def agent(intent):
        raise AssertionError("peek 不应进入 agent 模式")

    ctx = _ctx_with_agent(agent)
    ctx.gate = PeekStateGate({})
    outcome = asyncio.run(PeekFeedbackActivity().run(ctx))
    assert outcome.summary is None and outcome.memory_content
