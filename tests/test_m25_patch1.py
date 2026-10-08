"""M25-补丁1 测试：面板信息架构重排（从属关系 + 生效状态可视化）。

A 组（元数据与流转，纯 Python）：覆盖完整性（验收 7）/ 元数据流转——
   default_tree 不把 section/requires 当配置键、apply_panel_save 拒绝误带
   元数据的 payload、build_config_payload 透传键级元数据与 layout（验收 6）/
   load_schema 合并 panel_layout.json / 本体安全性（schema 顶层只有
   preset/advanced——AstrBotConfig._parse_schema 对顶层键强制读 type）；
B 组（JS 状态引擎行为级，node 桥）：judge 三件套链（验收 1，且不读
   judge.mode）/ 风格双链（验收 2）/ 补回复=自主链（验收 3）/ 搜索→活动池
   （验收 4，与后端 SEARCH_DEPENDENT_ACTIVITIES 同源对齐）/ tier→Chromium
   （验收 5）/ 配套 a novicePlan、b 素材链、e judge.mode=local 未实现、
   C7 计数、C8 七条出口（4 过闸门 + 3 直发）；
C 组（源码锚点）：配套 c/f/d/h + 红线（保存差量零改动 / mapped-chip 保留 /
   「六条出口」禁语 / 主人三极值相关键不在本轮语义内）。

验收 8（零回归）由全量测试承担；node 缺失时 B 组整体 skip（本机 v25.9.0 可跑）。
"""

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from core.panel_api import (
    apply_panel_save,
    build_config_payload,
    default_tree,
    load_schema,
)

WORKDIR = Path(__file__).resolve().parents[1]
SCHEMA = load_schema(WORKDIR)
LAYOUT = json.loads((WORKDIR / "panel_layout.json").read_text(encoding="utf-8"))
APP_JS = (WORKDIR / "pages" / "config" / "app.js").read_text(encoding="utf-8")
sys.path.insert(0, str(WORKDIR / "scripts"))

HAS_NODE = shutil.which("node") is not None
NODE_SKIP = pytest.mark.skipif(not HAS_NODE, reason="node 不可用（JS 引擎行为测试）")

HARNESS = WORKDIR / "tests" / "js" / "status_engine_harness.mjs"


def run_js(op: str, **kwargs):
    """node 桥：把 status-engine.js 当求值器（断言留在 Python 侧）。"""
    proc = subprocess.run(
        ["node", str(HARNESS)],
        input=json.dumps({"op": op, **kwargs}),
        capture_output=True,
        text=True,
        timeout=30,
        encoding="utf-8",
    )
    assert proc.returncode == 0, f"node 桥失败: {proc.stderr}"
    return json.loads(proc.stdout)


def ctx_of(advanced: dict, knobs: dict | None = None, runtime: dict | None = None):
    return {
        "values": {"knobs": knobs or {}, "advanced": advanced},
        "runtime": runtime or {},
    }


def layout_group(sec_id: str, grp_id: str) -> dict:
    sec = next(s for s in LAYOUT["sections"] if s["id"] == sec_id)
    return next(g for g in sec["groups"] if g["id"] == grp_id)


# ---------------------------------------------------------------------------
# A 组：元数据与流转（验收 6/7 + 本体安全性）
# ---------------------------------------------------------------------------
def test_a1_coverage_completeness():
    """验收 7：_layout 覆盖 schema 全部扁平键（133），遗漏 0 / 重复 0。

    复用 scripts/check_layout.py 的同一套校验（报告贴脚本输出）。"""
    import check_layout

    problems = check_layout.check(SCHEMA, LAYOUT)
    assert problems == [], f"覆盖完整性问题: {problems}"
    keys = check_layout.flat_keys(SCHEMA)
    assert len(keys) == 133, f"扁平键应为 133，实得 {len(keys)}"
    advanced = sum(1 for k in keys if "." in k)
    assert advanced == 124 and len(keys) - advanced == 9


def test_a2_metadata_field_not_named_items():
    """红线 3 / 铁律：元数据字段名绝不是 items（default_tree 会把
    type=object + items 当嵌套组递归）。"""
    for path, item in check_flat().items():
        assert "section" not in item or isinstance(item["section"], list)
        assert "requires" not in item or isinstance(item["requires"], list)
        # 元数据不叫 items：带 section 的键若还有 items，那是 object 组本义
        if "items" in item:
            assert item.get("type") == "object"
    assert "section" not in SCHEMA  # schema 顶层无 section
    assert SCHEMA["_layout"] == LAYOUT


def check_flat():
    import check_layout

    return check_layout.flat_keys(SCHEMA)


def test_a3_default_tree_ignores_metadata():
    """验收 6a：default_tree 不把 section/requires 当配置键建默认值。"""
    schema = {
        "preset": {"items": {}},
        "advanced": {"items": {
            "sleep": {"type": "object", "items": {
                "nap_enabled": {
                    "type": "bool", "default": True,
                    "section": ["E", "E2", 0],
                    "requires": [{"key": "runtime.browser_installed", "op": "truthy"}],
                },
            }},
        }},
        "_layout": {"sections": []},
    }
    tree = default_tree(schema)
    assert tree["advanced"] == {"sleep": {"nap_enabled": True}}  # 只有值
    assert "section" not in tree["advanced"]["sleep"]
    assert "requires" not in tree["advanced"]["sleep"]


def test_a4_apply_save_rejects_metadata_payload():
    """验收 6b：apply_panel_save 拒绝误带元数据的 payload（未知配置键）。"""
    schema = load_schema(WORKDIR)
    config: dict = {}
    with pytest.raises(Exception, match="未知配置键"):
        apply_panel_save(config, schema, {
            "advanced": {"sleep": {"section": ["E", "E2", 0]}},
        })
    with pytest.raises(Exception, match="未知配置键"):
        apply_panel_save(config, schema, {
            "advanced": {"sleep": {"requires": []}},
        })
    # 正常保存不受影响（保存路径零改动）
    summary = apply_panel_save(config, schema, {"advanced": {"sleep": {"nap_enabled": False}}})
    assert summary["count"] == 1


def test_a5_payload_passes_metadata_and_layout():
    """验收 6c + 配套 g：build_config_payload 透传键级元数据 + layout 字段。"""
    providers, tools = ["p1"], []
    payload = build_config_payload(
        {"preset": {}, "advanced": {"sleep": {"nap_enabled": True}}},
        SCHEMA, providers=providers, agent_tools=tools,
    )
    # layout 字段结构正确
    assert payload["layout"] is not None
    assert [s["id"] for s in payload["layout"]["sections"]] == [
        s["id"] for s in LAYOUT["sections"]
    ]
    # 键级元数据随键定义整体透传
    tier = payload["schema"]["advanced"]["items"]["autonomy"]["items"]["tier"]
    assert tier["section"] == ["B", "B1", 1]
    assert tier["requires"][0]["op"] == "anyOf"
    # 无布局 schema → layout 为 null（前端配套 h 回退）
    bare = load_schema_bare()
    payload2 = build_config_payload({}, bare)
    assert payload2["layout"] is None


def load_schema_bare():
    """读盘上 schema（不合并 panel_layout.json）——模拟旧包/缺文件形态。"""
    with open(WORKDIR / "_conf_schema.json", encoding="utf-8") as f:
        return json.load(f)


def test_a6_load_schema_merges_layout(tmp_path):
    """C2：load_schema 读 panel_layout.json 以 "_layout" 内存合并；缺文件时
    无该键（盘上 schema 顶层永远只有 preset/advanced）。"""
    assert "_layout" in SCHEMA
    assert SCHEMA["_layout"]["sections"][0]["id"] == "A"
    # 缺布局文件 → 无 "_layout"（配套 h 的数据前提）
    (tmp_path / "_conf_schema.json").write_text('{"preset": {"items": {}}}', encoding="utf-8")
    assert "_layout" not in load_schema(tmp_path)


def test_a7_on_disk_schema_top_level_safe_for_astrbot():
    """本体安全性（本轮关键偏离的防回归）：盘上 _conf_schema.json 顶层只有
    preset/advanced——AstrBot 本体 AstrBotConfig._parse_schema
    （astrbot_config.py:139-163）对顶层每个键强制读 v["type"]，
    star_manager.py:1159-1165 无过滤传入整文件，多一个无 type 的顶层键
    （如 _layout）会让插件加载直接崩溃。"""
    on_disk = load_schema_bare()
    assert set(on_disk.keys()) == {"preset", "advanced"}


def test_a8_requires_chain_data_discipline():
    """数据级纪律：复盘链不得含 judge.mode（红线 5 / 验收 1 后半）；
    补回复链指向 model 出口、不得指向 judge（验收 3）；每环有 label 与
    anchor（任务书提醒 2：宁可标未知也不猜——anchor 由 check_layout 兜底，
    这里锁三条关键链的指向）。"""
    review = SCHEMA["advanced"]["items"]["style_learning"]["items"]["daily_review_enabled"]
    req_keys = [r.get("key") for r in review["requires"]]
    assert "judge.provider_id" in req_keys
    assert "style_learning.enabled" in req_keys
    assert "judge.mode" not in req_keys, "复盘依赖不得含 judge.mode"
    # F3 组声明同样不得把 judge.mode 当依赖（chainNote 里的说明文字除外）
    f3 = layout_group("F", "F3")
    assert "judge.mode" not in [r.get("key") for r in f3["requires"]]

    pending = SCHEMA["advanced"]["items"]["sleep"]["items"]["pending_reply_enabled"]
    pending_keys = json.dumps(pending["requires"], ensure_ascii=False)
    assert "model.provider_id" in pending_keys, "补回复必须指向自主链（model 出口）"
    assert "judge" not in pending_keys, "补回复不得指向判断模型"
    assert "main.py:1718" in pending["requires"][0]["anchor"]

    tier = SCHEMA["advanced"]["items"]["autonomy"]["items"]["tier"]
    any_of = tier["requires"][0]
    assert any_of["op"] == "anyOf"
    sub_keys = json.dumps(any_of["of"], ensure_ascii=False)
    assert "runtime.browser_installed" in sub_keys and "autonomy.tier" in sub_keys


def test_a9_backend_activity_pool_excludes_search():
    """验收 4（后端侧，真实代码）：web_search_enabled=false → 活动池摘除
    surf/read（activities_excluding_search，activities.py:106-120）。"""
    from core.activities import (
        SEARCH_DEPENDENT_ACTIVITIES,
        activities_excluding_search,
        web_search_enabled,
    )
    assert set(SEARCH_DEPENDENT_ACTIVITIES) == {"surf", "read"}

    class Act:
        def __init__(self, name):
            self.name = name

    pool = [Act(n) for n in ("surf", "read", "game", "free", "peek")]
    for cfg_off in ({"capabilities": {"web_search_enabled": False}},
                    {"capabilities": {"web_search_enabled": "false"}},
                    {"capabilities": {}}):
        pass  # {} 缺键 = 默认开，不在此测
    kept = activities_excluding_search(
        pool, {"capabilities": {"web_search_enabled": False}})
    assert [a.name for a in kept] == ["game", "free", "peek"]
    kept_on = activities_excluding_search(
        pool, {"capabilities": {"web_search_enabled": True}})
    assert [a.name for a in kept_on] == ["surf", "read", "game", "free", "peek"]
    assert web_search_enabled({"capabilities": {"web_search_enabled": False}}) is False


# ---------------------------------------------------------------------------
# B 组：JS 状态引擎行为级（node 桥）
# ---------------------------------------------------------------------------
def make_ctx(provider="", style=True, review=True, mode="off",
             judge_provider=None, pending=True, tier=1, browser=True):
    adv = {
        "style_learning": {"enabled": style, "daily_review_enabled": review},
        "judge": {"mode": mode, "provider_id": judge_provider},
        "sleep": {
            "pending_reply_enabled": pending,
            "farewell_mode": "probability",
            "dream_probability": 0.3,
            "wake_ack_message": "醒了",
        },
        "initiative": {"enabled": True},
        "autonomy": {"tier": tier},
        "decision": {"agent_activities": ["surf", "read", "game", "free"],
                     "free_activity_enabled": True},
        "capabilities": {"web_search_enabled": True},
        "model": {"provider_id": provider},
    }
    return ctx_of(adv, runtime={"browser_installed": browser, "providers": []})


@NODE_SKIP
def test_b1_judge_trio_chain_blocked_by_provider_not_mode():
    """验收 1：judge.provider_id 空 → 每日复盘判定"卡着"；非空 → "开"；
    且判定不读 judge.mode（mode 三档取值下结论不变——复盘路径只看 provider，
    main.py:364 / style_review.py:88-96）。"""
    f3 = layout_group("F", "F3")
    for mode in ("off", "local", "api"):
        ctx = make_ctx(judge_provider="", mode=mode)
        result = run_js("computeNodeStatus", decl=f3, ctx=ctx)
        assert result["status"] == "blocked", f"mode={mode} 时应为卡着"
        ctx2 = make_ctx(judge_provider="cheap-small", mode=mode)
        result2 = run_js("computeNodeStatus", decl=f3, ctx=ctx2)
        assert result2["status"] == "active", f"mode={mode} 时应开"


@NODE_SKIP
def test_b2_style_double_chain():
    """验收 2：提炼链依赖"总闸 + 活动读到网页"（F2：总闸硬环 + 自主大脑
    soft 环——留空回退聊天可用）；复盘链依赖"总闸 × daily_review_enabled ×
    provider"（F3：switch=复盘开关，requires=总闸+provider）。"""
    f2 = layout_group("F", "F2")
    # 总闸关 → 哑（被显式关闭级联，red dot）
    r = run_js("computeNodeStatus", decl=f2, ctx=make_ctx(style=False))
    assert r["status"] == "dead"
    # 总闸开 → 生效（provider 留空走 soft 环：可用但提示烧额度）
    r = run_js("computeNodeStatus", decl=f2, ctx=make_ctx(style=True))
    assert r["status"] == "active"

    f3 = layout_group("F", "F3")
    assert run_js("computeNodeStatus", decl=f3, ctx=make_ctx(
        style=False, review=True))["status"] == "dead"  # 总闸关（复盘开关开着也被级联）
    assert run_js("computeNodeStatus", decl=f3, ctx=make_ctx(
        style=True, review=False))["status"] == "off"  # 自己关 → 关
    assert run_js("computeNodeStatus", decl=f3, ctx=make_ctx(
        style=True, review=True, judge_provider=""))["status"] == "blocked"
    assert run_js("computeNodeStatus", decl=f3, ctx=make_ctx(
        style=True, review=True, judge_provider="j1"))["status"] == "active"
    # 判定顺序：自身 switch 关优先于上游失败（自己关了显示"关"而非"哑"）
    r = run_js("computeNodeStatus", decl=f3, ctx=make_ctx(style=False, review=False))
    assert r["status"] == "off"


@NODE_SKIP
def test_b3_pending_reply_uses_model_chain_not_judge():
    """验收 3：补回复的 requires 指向 model 出口（数据级在 A8 锁定）；
    行为级：自主大脑未配 → 卡着；配了 → 开；判断模型配没配不影响。"""
    pending = SCHEMA["advanced"]["items"]["sleep"]["items"]["pending_reply_enabled"]
    ctx = make_ctx(provider="", judge_provider="j1")  # 判断模型配了也无关
    result = run_js("computeNodeStatus", decl=pending, ctx=ctx)
    assert result["status"] == "blocked"
    assert result["chain"][0]["label"] == "自主大脑（独立 provider）"
    ctx2 = make_ctx(provider="main-brain")
    assert run_js("computeNodeStatus", decl=pending, ctx=ctx2)["status"] == "active"


@NODE_SKIP
def test_b4_search_gate_to_activity_pool():
    """验收 4（前端镜像与后端同源）：web_search_enabled=false → C3 可跑
    活动不含 surf/read；free 开关独立生效；镜像常量与
    core/activities.SEARCH_DEPENDENT_ACTIVITIES 逐字一致。"""
    from core.activities import SEARCH_DEPENDENT_ACTIVITIES
    mirrored = run_js("searchDependent")
    assert mirrored == list(SEARCH_DEPENDENT_ACTIVITIES)
    assert run_js("runnableActivities", agentActivities=["surf", "read", "game", "free"],
                  freeEnabled=True, webSearchEnabled=False) == ["game", "free"]
    assert run_js("runnableActivities", agentActivities=["surf", "read", "game", "free"],
                  freeEnabled=True, webSearchEnabled=True) == ["surf", "read", "game", "free"]
    assert run_js("runnableActivities", agentActivities=["surf", "read", "game", "free"],
                  freeEnabled=False, webSearchEnabled=True) == ["surf", "read", "game"]


@NODE_SKIP
def test_b5_tier_needs_chromium_only_above_tier0():
    """验收 5：tier>=1 且 runtime.browser_installed=false → 浏览器节点
    "卡着"；tier=0 免疫；运行时数据缺失 → "未知"（不猜）。"""
    tier = SCHEMA["advanced"]["items"]["autonomy"]["items"]["tier"]
    assert run_js("computeKeyStatus", groupStatus="active", keyDecl=tier,
                  ctx=make_ctx(tier=0, browser=False)) == "active"
    assert run_js("computeKeyStatus", groupStatus="active", keyDecl=tier,
                  ctx=make_ctx(tier=2, browser=False)) == "blocked"
    assert run_js("computeKeyStatus", groupStatus="active", keyDecl=tier,
                  ctx=make_ctx(tier=2, browser=True)) == "active"
    assert run_js("computeKeyStatus", groupStatus="active", keyDecl=tier,
                  ctx=make_ctx(tier=2, browser=None)) == "unknown"


@NODE_SKIP
def test_b6_novice_plan_first_tier_eight_knobs():
    """配套 a：首层 8 张旋钮（life_extra 是独立块不占旋钮位）、组标题存在、
    13 张功能卡分 5 组折叠。归组数据来自 panel_layout.json（novicePlan）。"""
    plan = run_js("novicePlan", layout=LAYOUT, presetSchema=SCHEMA["preset"]["items"])
    assert plan["firstTierKnobCount"] == 8
    assert "life_extra" not in [k for g in plan["knobGroups"] for k in g["knobs"]]
    titles = {g["title"] for g in plan["knobGroups"]}
    assert {"她的大脑", "她的手脚", "她独处时干什么", "她什么时候开口"} <= titles
    cards = [c for g in plan["cardGroups"] for c in g["cards"]]
    assert len(cards) == 13
    assert set(cards) == {
        "judgeCard", "browserCard", "workspaceCard", "searchToggleCard",
        "agentToolsCard", "initiativeCard", "scheduleCard", "farewellCard",
        "chatGuardCard", "wakeRandomCard", "pendingReplyCard",
        "styleLearningCard", "styleDataCard",
    }
    # 组标题来自布局树的一级 title
    assert all(g["title"] for g in plan["cardGroups"])


@NODE_SKIP
def test_b7_material_chain_on_style_card():
    """配套 b：素材链文案——标题含"生效链"、两环状态（总闸 + 自主大脑）、
    断链时结论说明后果与去向。"""
    f2 = layout_group("F", "F2")
    chain = run_js("renderChain", title="素材总结的生效链", decl=f2,
                   ctx=make_ctx(style=False))
    assert "生效链" in chain["title"]
    assert len(chain["lines"]) == 2
    assert "① 风格学习总闸" in chain["lines"][0] and "关" in chain["lines"][0]
    assert "② 自主大脑可用" in chain["lines"][1]
    assert "①未通" in chain["conclusion"]
    assert "攒着" in chain["conclusion"] and "F1" in chain["conclusion"]
    # 总闸开 + provider 留空 → 软环显示回退提示，全通
    chain2 = run_js("renderChain", title="素材总结的生效链", decl=f2,
                    ctx=make_ctx(style=True))
    assert "回退聊天模型" in chain2["lines"][1]
    assert chain2["status"] == "active"


@NODE_SKIP
def test_b8_judge_mode_local_never_active():
    """配套 e：judge.mode=local 显示"未实现"，不得显示成"开"
    （judge.py:110-117：只有 api 档真正工作）。"""
    a2 = layout_group("A", "A2")
    r = run_js("computeNodeStatus", decl=a2, ctx=make_ctx(mode="local"))
    assert r["status"] == "unimpl"
    assert r["statusLabel"] == "未实现"
    assert run_js("computeNodeStatus", decl=a2, ctx=make_ctx(mode="off"))["status"] == "off"
    assert run_js("computeNodeStatus", decl=a2, ctx=make_ctx(
        mode="api", judge_provider="j1"))["status"] == "active"
    assert run_js("computeNodeStatus", decl=a2, ctx=make_ctx(
        mode="api", judge_provider=""))["status"] == "blocked"
    # local 未实现时，A3/A4（把关参数/提示词）为哑（级联）
    a3 = layout_group("A", "A3")
    assert run_js("computeNodeStatus", decl=a3, ctx=make_ctx(mode="local"))["status"] == "dead"


@NODE_SKIP
def test_b9_exit_lines_seven_outlets():
    """C8：七条出口各带状态；直发与过闸门的 note 逐字来自布局声明
    （红线 4：不得写"六条出口共享闸门"——事实是 4 过闸门 + 3 直发）。"""
    items = LAYOUT["exits"]["items"]
    assert len(items) == 7
    assert {i["id"] for i in items} == {
        "share", "dream", "oversleep", "initiative",
        "farewell", "wake_ack", "pending_reply",
    }
    lines = run_js("computeExitLines", exitsDecl=items, ctx=make_ctx())
    by_id = {e["id"]: e for e in lines}
    assert by_id["share"]["status"] == "on"
    assert by_id["dream"]["status"] == "on"
    assert by_id["farewell"]["note"] == "直发，不受限额"
    assert by_id["wake_ack"]["status"] == "on"
    assert by_id["pending_reply"]["status"] == "blocked"  # 自主大脑未配
    # 关闭形态逐一验证
    off_ctx = make_ctx()
    off_ctx["values"]["advanced"]["sleep"].update(
        farewell_mode="off", dream_probability=0.0, wake_ack_message="")
    off_ctx["values"]["advanced"]["initiative"]["enabled"] = False
    off = {e["id"]: e["status"] for e in
           run_js("computeExitLines", exitsDecl=items, ctx=off_ctx)}
    assert off["farewell"] == "off"
    assert off["dream"] == "off"
    assert off["wake_ack"] == "off"
    assert off["initiative"] == "off"
    assert off["share"] == "on"  # 分享链无总开关（闸门只节流）
    assert off["oversleep"] == "on"


@NODE_SKIP
def test_b10_counts_and_status_meta():
    """C7：生效计数（组头"生效 x/y"、区头"本区 x/y 项生效"的数据源）。"""
    assert run_js("computeCounts", statuses=["active", "active", "off"]) == {
        "active": 2, "total": 3,
    }
    assert run_js("computeCounts", statuses=[]) == {"active": 0, "total": 0}


# ---------------------------------------------------------------------------
# C 组：源码锚点（配套 c/d/f/h + 红线）
# ---------------------------------------------------------------------------
def test_c1_novice_reorder_anchors():
    """配套 a 渲染锚点：renderNovice 消费 novicePlan + 组标题穿插 +
    细项折叠；既有卡片挂载形态（逐张 grid.appendChild）与 D 组先于 E 组
    的次序保持（M14/M15 既有断言的守护意图不变）。"""
    assert "const plan = novicePlan(state.layout, presetSchema);" in APP_JS
    assert "noviceSectionHead" in APP_JS
    assert "show-detail" in APP_JS
    idx_novice = APP_JS.index("function renderNovice()")
    idx_ini = APP_JS.index("grid.appendChild(initiativeCard());", idx_novice)
    idx_sched = APP_JS.index("grid.appendChild(scheduleCard());", idx_novice)
    assert idx_ini < idx_sched
    # app.js 显式挂载序列与 layout.novice.cardGroups 完全一致（防两处漂移）
    declared = [c for g in LAYOUT["novice"]["cardGroups"] for c in g["cards"]]
    mounted = re.findall(r"grid\.appendChild\((\w+Card)\(\)\);", APP_JS[idx_novice:])
    assert mounted == declared, f"挂载序列与布局声明不一致: {mounted} vs {declared}"


def test_c2_layout_tree_render_anchors():
    """C3/C4/C5/C6/C7 渲染锚点：树渲染遍历 _layout、状态点、链、级联、
    计数；GROUP_LABELS 保留（原组名小字用途，老用户迁移缓冲）。"""
    for anchor in (
        "function renderExpert()",
        "renderExpertFlat()",  # 配套 h：无布局回退
        "computeNodeStatus(gDecl, ctx)",
        "computeCounts(keyStatuses)",
        "buildKeyRow(e.group, e.key, e.item, keyStatuses[i], showDot)",
        "cascade-note",  # C6 因果说明
        "本区",  # C7 区头计数
        "statusDotEl",
        "GROUP_LABELS[group]",
        "styleLibraryAdmin()",  # F 区（附）语料管理
        "renderMoodSection()",  # G 区（附）兴趣管理
    ):
        assert anchor in APP_JS, f"缺锚点: {anchor}"


def test_c3_preset_model_effective_line():
    """配套 c：「当前实际生效」行——留空 = 与聊天共用 + 烧额度提示（既有
    providerPickerControl 的状态行，M25 统一前缀）。"""
    assert "当前实际生效：聊天模型（共用账号）" in APP_JS
    assert "当前实际生效：" in APP_JS
    assert "打断你聊天的缓存" in APP_JS  # 烧额度提示保留
    # 专家区 model.provider_id 与新手 preset_model 共用该控件
    assert APP_JS.count("providerPickerControl({") >= 2


def test_c4_fallback_and_mapping_chips():
    """配套 d/f：_layout 未覆盖键进「其他参数」区 + console.warn（不丢键）；
    mapped-chip（档位联动徽章）随重排保留（M18 防回退）。"""
    assert "已归入「其他参数」区" in APP_JS
    assert "console.warn(`[living] _layout 未收录键 ${group}.${key}" in APP_JS
    assert "mapped-chip" in APP_JS and "档位联动" in APP_JS
    assert 'chip.textContent = "档位联动";' in APP_JS
    # 兜底区在布局树里有定义（Z 区）
    zsec = next(s for s in LAYOUT["sections"] if s["id"] == "Z")
    assert zsec.get("fallback") is True


def test_c5_redlines_untouched():
    """红线锚点：保存差量逻辑 / 旋钮映射表 / 热重载路径零改动；
    「六条出口共享闸门」禁语；主人三极值键的 UI 呈现不带钳制文案。"""
    assert "function buildSavePayload()" in APP_JS
    assert "function diffSection(current, loaded)" in APP_JS
    assert "KNOB_MAPPED_KEYS = new Set" in APP_JS
    # 红线 4：禁语（事实是 4 条过闸门 + 3 条直发）
    assert "六条出口" not in APP_JS
    assert "六条" not in json.dumps(LAYOUT, ensure_ascii=False)
    # D1 的组说明按代码如实转译（4 过闸门 + 3 直发）
    d1 = layout_group("D", "D1")
    assert "4 条过闸门" in d1["summary"]
    assert "不受此限" in d1["summary"]
    # E4 直发说明（晚安有自己的开关，不动 D1）
    e4 = layout_group("E", "E4")
    assert "不受 D1" in e4["summary"]
    # 主人三极值：预算/轮数/冲动上限仅在 C4 呈现参数本体，无任何新增钳制
    c4 = layout_group("C", "C4")
    assert "硬闸" in c4["summary"]


def test_c6_global_status_row_anchor():
    """C8：全局状态行容器与渲染接线。"""
    html = (WORKDIR / "pages" / "config" / "index.html").read_text(encoding="utf-8")
    assert 'id="global-status"' in html
    assert "她现在会主动做的事：" in APP_JS
    assert "renderGlobalStatus();" in APP_JS
