"""M26-补丁1 测试：修复专家面板键级标签全部丢失（回归）+ 组级 summary 渲染。

根因（任务书第一节，本测试以 DOM 装配断言锁死）：M25 提取 buildKeyRow 时
label 的挂载被误写进 `if (item.requires)` 分支——121 个无 requires 键的
标签从未进入 DOM。

断言方式：node 桥 + 最小 DOM 桩（tests/js/dom_stub.mjs）让**真实 app.js**
全链路渲染（boot→load→renderNovice+renderExpert+renderGlobalStatus），
再对桩树做装配级断言——不是源码正则（两处源码锚点测试除外，已显式标注）。
"""

import copy
import json
import subprocess
from pathlib import Path

import pytest

WORKDIR = Path(__file__).resolve().parents[1]
SCHEMA = json.loads((WORKDIR / "_conf_schema.json").read_text(encoding="utf-8"))
LAYOUT = json.loads((WORKDIR / "panel_layout.json").read_text(encoding="utf-8"))
APP_JS = (WORKDIR / "pages" / "config" / "app.js").read_text(encoding="utf-8")

HARNESS = WORKDIR / "tests" / "js" / "status_engine_harness.mjs"
HAS_NODE = __import__("shutil").which("node") is not None
NODE_SKIP = pytest.mark.skipif(not HAS_NODE, reason="node 不可用（DOM 桩渲染测试）")

# 与 app.js DANGER_KEYS 一致的 advanced 危险键（验收 3）
DANGER_KEYS = {
    "sleep.weights", "sleep.fatigue_rate_per_hour",
    "decision.recent_topic_penalty", "decision.single_run_token_budget",
    "decision.max_tool_rounds", "decision.max_run_seconds",
    "decision.daily_impulse_limit",
}
# 与 app.js KNOB_MAPPED_KEYS 一致的旋钮映射目标键（顺带锁配套 f 不回退）
MAPPED_KEYS = {
    "decision.impulse_check_interval_minutes", "decision.activity_probability",
    "decision.daily_impulse_limit", "decision.interest_daily_decay",
    "decision.recent_topic_window", "decision.exploration_trigger",
    "decision.free_activity_enabled", "decision.decision_mode",
    "output_gate.daily_message_limit", "output_gate.message_min_interval_minutes",
    "autonomy.tier", "autonomy.write_level", "model.provider_id",
}
# 3 个带 requires 的键（验收 2 / 任务书零节）
REQUIRES_KEYS = {
    "autonomy.tier", "sleep.pending_reply_enabled",
    "style_learning.daily_review_enabled",
}


def build_payload(layout=LAYOUT):
    return {
        "knobs": {},
        "advanced": {},
        "schema": {
            "preset": {"items": SCHEMA["preset"]["items"]},
            "advanced": {"items": SCHEMA["advanced"]["items"]},
        },
        "layout": layout,
        "providers": ["p-chat"],
        "agent_tools": [],
    }


def run_render(payload, op="renderPanel"):
    proc = subprocess.run(
        ["node", str(HARNESS)],
        input=json.dumps({"op": op, "payload": payload}),
        capture_output=True, text=True, timeout=60, encoding="utf-8",
    )
    assert proc.returncode == 0, f"node 桥失败: {proc.stderr[-800:]}"
    data = json.loads(proc.stdout)
    assert not data.get("loadError"), f"渲染期报错: {data['loadError']}"
    return data


def advanced_items():
    return SCHEMA["advanced"]["items"]


def iter_schema_keys():
    for group, body in advanced_items().items():
        for key, item in body["items"].items():
            yield f"{group}.{key}", item


def rows_by_code(data):
    return {r["code"]: r for r in data["keyRows"]}


# ---------------------------------------------------------------------------
# 验收 1：每个键行都含标签（.key-label + .key-name == description）
# ---------------------------------------------------------------------------
@NODE_SKIP
def test_acc1_every_key_row_has_label():
    data = run_render(build_payload())
    rows = rows_by_code(data)
    all_keys = dict(iter_schema_keys())
    assert data["loadError"] == ""
    assert len(data["keyRows"]) == len(all_keys) == 129  # M32-补丁1 +1
    assert set(rows) == set(all_keys), "键行集合必须与 schema 逐一对应"
    for path, item in all_keys.items():
        row = rows[path]
        assert row["hasLabel"], f"{path} 的 .key-label 缺失（回归！）"
        expected_name = item.get("description") or path.split(".")[1]
        # 徽章键的 .key-name 内 prepend 了徽章（真 DOM textContent 递归拼接：
        # 徽章文本在前、description 在后）；无徽章键全等
        if path in DANGER_KEYS or path in MAPPED_KEYS:
            assert row["name"].endswith(expected_name), (
                f"{path} 的标签名错位（徽章应在前、description 在后）: {row['name']!r}"
            )
        else:
            assert row["name"] == expected_name, f"{path} 的标签名错位: {row['name']!r}"
        assert row["code"] == path
        assert row["hint"] == (item.get("hint") or ""), f"{path} 的 hint 缺失或错位"


# ---------------------------------------------------------------------------
# 验收 2：生效链按钮（.key-side）仅 3 个 requires 键有，且位于 label 与控件之间
# ---------------------------------------------------------------------------
@NODE_SKIP
def test_acc2_chain_side_only_on_requires_keys():
    data = run_render(build_payload())
    rows = rows_by_code(data)
    for path, row in rows.items():
        if path in REQUIRES_KEYS:
            assert row["hasSide"], f"{path} 带 requires，必须有生效链按钮"
            assert row["childClasses"] == ["key-label", "key-side", "key-control"], (
                f"{path} 的行内次序应为 label→side→control，实得 {row['childClasses']}"
            )
        else:
            assert not row["hasSide"], f"{path} 无 requires，不得出现 .key-side（密度纪律）"
    assert sum(1 for r in rows.values() if r["hasSide"]) == 3


# ---------------------------------------------------------------------------
# 验收 3：危险徽章/联动徽章随 label 恢复（在 .key-label 内）
# ---------------------------------------------------------------------------
@NODE_SKIP
def test_acc3_danger_and_mapped_chips_inside_label():
    data = run_render(build_payload())
    rows = rows_by_code(data)
    for path in DANGER_KEYS:
        row = rows[path]
        assert row["dangerRow"], f"{path} 应带 danger-row 类"
        assert row["hasDangerChip"], f"{path} 的 ⚠ 危险徽章缺失（应在 .key-label 内）"
    for path in MAPPED_KEYS:
        assert rows[path]["hasMappedChip"], f"{path} 的档位联动徽章缺失（M18/M25 配套 f）"


# ---------------------------------------------------------------------------
# 验收 4（配套 a）：组级 summary 渲染；无 summary 的组不产生空元素
# ---------------------------------------------------------------------------
@NODE_SKIP
def test_acc4_group_summary_rendered():
    data = run_render(build_payload())
    layout_summaries = [
        grp["summary"]
        for sec in LAYOUT["sections"] for grp in sec.get("groups", [])
        if grp.get("summary")
    ]
    rendered = {g["title"]: g for g in data["groups"]}
    assert data["groups"], "应有二级组渲染"
    assert len(data["groups"]) == 25, f"渲染组数应为 25（Z 区无未覆盖键不渲染），实得 {len(data['groups'])}"
    assert len(layout_summaries) == 25
    # 每个渲染组恰一个 .group-summary、无空元素
    for title, g in rendered.items():
        assert g["hasSummary"], f"组 {title} 缺 .group-summary"
        assert g["summaryNodeCount"] == 1, f"组 {title} 的 .group-summary 应恰一个节点"
        assert g["summaryText"], f"组 {title} 的 summary 为空（不得渲染空行）"
    # summary 文本与布局声明一一对应（多重集；title 有同名组——C5/F4 均叫
    # "提示词"——故按文本集合比对，逐一性由集合长度相等保证）
    assert sorted(g["summaryText"] for g in data["groups"]) == sorted(layout_summaries)


@NODE_SKIP
def test_acc4b_group_without_summary_renders_no_empty_node():
    """无 summary 的组不渲染空 .group-summary（变体：删掉 A3 的 summary）。"""
    layout = copy.deepcopy(LAYOUT)
    a3 = next(g for s in layout["sections"] if s["id"] == "A"
              for g in s["groups"] if g["id"] == "A3")
    a3.pop("summary")
    data = run_render(build_payload(layout=layout))
    a3_row = next(g for g in data["groups"] if "把关怎么把守" in g["title"])
    assert a3_row["hasSummary"] is False
    assert a3_row["summaryNodeCount"] == 0, "无 summary 的组不得产生空 .group-summary 元素"
    # 其他组不受影响
    assert sum(1 for g in data["groups"] if g["hasSummary"]) == 24


# ---------------------------------------------------------------------------
# 配套 c：扁平回退路径（renderExpertFlat）同样每行含标签
# ---------------------------------------------------------------------------
@NODE_SKIP
def test_acc5_flat_fallback_path_also_has_labels():
    data = run_render(build_payload(layout=None), op="renderPanelFlat")
    assert data["loadError"] == ""
    rows = rows_by_code(data)
    all_keys = dict(iter_schema_keys())
    assert len(data["keyRows"]) == 129  # M32-补丁1 +1
    for path, item in all_keys.items():
        row = rows[path]
        assert row["hasLabel"], f"扁平回退路径 {path} 的 .key-label 缺失"
        expected_name = item.get("description") or path.split(".")[1]
        if path in DANGER_KEYS or path in MAPPED_KEYS:
            assert row["name"].endswith(expected_name)
        else:
            assert row["name"] == expected_name


# ---------------------------------------------------------------------------
# 源码锚点（辅助）：显式标注——以下断言验证代码写法，不替代上面的 DOM 渲染断言
# ---------------------------------------------------------------------------
def test_src_anchor_label_mounted_unconditionally():
    """锚点：buildKeyRow 中 label 挂载无条件存在、旧 bug 形态不存在。
    （验证写法，不替代 DOM 渲染断言——真正的行为锁定在 acc1/acc2/acc5。）"""
    start = APP_JS.index("function buildKeyRow(")
    end = APP_JS.index("\n}", APP_JS.index("return row;", start))
    body = APP_JS[start:end]
    assert "row.appendChild(labelEl);" in body, "label 必须无条件挂载"
    assert "row.append(labelEl, side);" not in body, "旧 bug 形态（label 圈进 requires 分支）不得回归"


def test_src_anchor_group_summary_read():
    """锚点：buildGroup 读取 gDecl.summary（配套 a 的数据通路）。
    （验证写法，不替代 DOM 渲染断言——真正的行为锁定在 acc4/acc4b。）"""
    start = APP_JS.index("function buildGroup(")
    end = APP_JS.index("\nfunction renderExpertFlat", start)
    body = APP_JS[start:end]
    assert "gDecl.summary" in body
