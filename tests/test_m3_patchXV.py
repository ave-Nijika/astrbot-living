"""M3 补丁 XV 测试：截图落工作区 / manifest 一致性 / free 开关 / L2-L3 写操作判定。

对应任务书《M3-补丁XV_遗留四项补全》验收表 1/3/4/5（验收 2 的 main 接线
断言在 tests/test_main_wiring.py）。
"""

import asyncio
import logging
import random
import types
from pathlib import Path

import pytest

from core.activities import default_activities
from core.autonomy import (
    ACTION_KINDS,
    build_tool_manifest,
    check_action_kind,
)
from core.decider import ActivityDecider
from core.living_loop import LivingLoop
from core.living_tools import (
    BrowserClickTool,
    BrowserScreenshotTool,
    BrowserTypeTool,
    build_living_tools,
)


# ---------------------------------------------------------------------------
# 清单 4：write_level × action_kind 允许表（纯函数）
# ---------------------------------------------------------------------------
def test_action_kind_allow_table():
    """L0 全拒 / L1 navigate+fill / L2 +submit_form+comment / L3 全部。"""
    for kind in ACTION_KINDS:
        assert check_action_kind(0, kind)[0] is False  # L0 只读：全拒
    for kind in ("navigate", "fill"):
        assert check_action_kind(1, kind)[0] is True
    for kind in ("submit_form", "comment", "post", "message", "purchase"):
        assert check_action_kind(1, kind)[0] is False
    for kind in ("navigate", "fill", "submit_form", "comment"):
        assert check_action_kind(2, kind)[0] is True
    for kind in ("post", "message", "purchase"):
        assert check_action_kind(2, kind)[0] is False
    for kind in ACTION_KINDS:
        assert check_action_kind(3, kind)[0] is True  # L3 全权：全放
    assert check_action_kind(3, None)[0] is True
    assert check_action_kind(3, "unknown")[0] is True


def test_unknown_action_kind_conservative_reject():
    """缺省 / unknown / 胡写 → 保守拒绝 + 明确文本（验收 5）。"""
    for bad in (None, "", "   ", "unknown", "bogus_kind"):
        allowed, reason = check_action_kind(2, bad)
        assert allowed is False
        assert "当前权限无法确认该操作的风险等级，已拒绝" in reason


def test_action_kind_case_insensitive():
    assert check_action_kind(2, " Comment ")[0] is True
    assert check_action_kind(2, "POST")[0] is False  # 在表内但不允许


def test_disallowed_kind_reason_names_level():
    allowed, reason = check_action_kind(2, "post")
    assert allowed is False
    assert "write_level=2" in reason and "post" in reason


# ---------------------------------------------------------------------------
# 清单 4：判定在工具执行路径上生效（拒绝时连页面都不碰）
# ---------------------------------------------------------------------------
class RecordingPage:
    def __init__(self):
        self.filled = []
        self.clicked = []

    async def fill(self, selector, text):
        self.filled.append((selector, text))

    async def click(self, selector, timeout=5000):
        self.clicked.append(selector)


def _fake_session(page):
    return types.SimpleNamespace(
        _ensure_page=lambda: asyncio.sleep(0, result=page)
    )


def _type_tool(page, write_level):
    tool = BrowserTypeTool()
    tool._session_ref = types.SimpleNamespace(session=_fake_session(page))
    tool._write_level = write_level
    return tool


def _click_tool(page, write_level):
    tool = BrowserClickTool()
    tool._session_ref = types.SimpleNamespace(session=_fake_session(page))
    tool._write_level = write_level
    return tool


def test_l2_type_post_rejected_comment_allowed():
    """验收 4：write_level=2 + action_kind=post → 拒；comment → 放行。"""
    page = RecordingPage()
    tool = _type_tool(page, write_level=2)
    rejected = asyncio.run(
        tool.call(None, selector="#editor", text="hi", action_kind="post")
    )
    assert "已拒绝" in rejected
    assert page.filled == []  # 拒绝时页面未触碰

    ok = asyncio.run(
        tool.call(None, selector="#comment_box", text="hi", action_kind="comment")
    )
    assert "已拒绝" not in ok
    assert page.filled == [("#comment_box", "hi")]


def test_l2_click_purchase_rejected_navigate_allowed():
    page = RecordingPage()
    tool = _click_tool(page, write_level=2)
    rejected = asyncio.run(
        tool.call(None, selector=".buy", action_kind="purchase")
    )
    assert "已拒绝" in rejected and page.clicked == []
    ok = asyncio.run(
        tool.call(None, selector="a.next", action_kind="navigate")
    )
    assert "已拒绝" not in ok and page.clicked == ["a.next"]


def test_missing_action_kind_rejected_with_info_log(caplog):
    """漏标 action_kind → 保守拒绝 + INFO 日志（可观测）。"""
    page = RecordingPage()
    tool = _click_tool(page, write_level=1)
    with caplog.at_level(logging.INFO, logger="astrbot"):
        result = asyncio.run(tool.call(None, selector="#btn"))
    assert "当前权限无法确认该操作的风险等级，已拒绝" in result
    assert page.clicked == []
    assert any(
        "写操作被拒" in r.getMessage() and "browser_click" in r.getMessage()
        for r in caplog.records
    )


def test_l3_allows_unlabeled_actions():
    """L3 全权：漏标也放行（表语义：3=全部；标注义务只压在 L0-2）。"""
    page = RecordingPage()
    tool = _type_tool(page, write_level=3)
    ok = asyncio.run(tool.call(None, selector="#box", text="x"))
    assert "已在 #box 填入" in ok
    assert page.filled == [("#box", "x")]


def test_action_kind_declared_in_tool_schema():
    """action_kind 出现在两个写工具的 parameters 里（LLM 可见）。"""
    for tool_cls in (BrowserClickTool, BrowserTypeTool):
        props = tool_cls().parameters["properties"]
        assert "action_kind" in props


# ---------------------------------------------------------------------------
# 清单 1：截图落工作区 screenshots/（含失败保护与 cwd 兜底）
# ---------------------------------------------------------------------------
class ShotPage:
    def __init__(self):
        self.saved_to = None

    async def screenshot(self, path):
        self.saved_to = path
        Path(path).write_bytes(b"\x89PNG fake")


def _shot_tool(session):
    tool = BrowserScreenshotTool()
    tool._session_ref = types.SimpleNamespace(session=session)
    return tool


def test_screenshot_lands_in_workspace_screenshots(tmp_path):
    """验收 1：截图落在 <workspace>/screenshots/（目录自动创建），不再写
    系统 temp 根下的旧固定文件。"""
    page = ShotPage()
    session = types.SimpleNamespace(
        _workspace=str(tmp_path), _ensure_page=_fake_session(page)._ensure_page
    )
    result = asyncio.run(_shot_tool(session).call(None))
    assert "截图已保存" in result
    shots = list((tmp_path / "screenshots").glob("living_screenshot_*.png"))
    assert len(shots) == 1
    assert Path(page.saved_to).parent == tmp_path / "screenshots"


def test_screenshot_failure_returns_text_not_raise(tmp_path):
    """工作区不可写 → 明确错误文本，不抛异常（任务书 2.1 失败保护）。"""
    blocker = tmp_path / "not_a_dir"
    blocker.write_text("x", encoding="utf-8")  # 目录创建必然失败
    session = types.SimpleNamespace(
        _workspace=str(blocker), _ensure_page=_fake_session(ShotPage())._ensure_page
    )
    result = asyncio.run(_shot_tool(session).call(None))  # 不抛
    assert "截图失败" in result


def test_screenshot_falls_back_to_cwd(tmp_path, monkeypatch):
    """session 未带 _workspace → 兜底 Path.cwd()/screenshots。"""
    monkeypatch.setattr(Path, "cwd", classmethod(lambda cls: tmp_path))
    session = types.SimpleNamespace(
        _ensure_page=_fake_session(ShotPage())._ensure_page
    )  # 没有 _workspace 属性
    result = asyncio.run(_shot_tool(session).call(None))
    assert (tmp_path / "screenshots").is_dir()
    assert "截图已保存" in result


# ---------------------------------------------------------------------------
# 清单 2：manifest 与实际挂载一致（名称对齐：click/type/list）
# ---------------------------------------------------------------------------
def _mg():
    class M:
        async def add(self, c, importance=0.5, metadata=None, **kw):
            return 1

        async def search(self, q, k=5):
            return []

        async def close(self):
            pass

    return M()


@pytest.mark.parametrize("tier", [0, 1, 2, 3])
def test_manifest_matches_actual_mount(tier):
    """build_tool_manifest(…, has_browser, has_workspace) 的集合必须等于
    build_living_tools 同参装配出的实际工具名集合。"""
    from core.browser_tools import BrowserSession

    session = BrowserSession(workspace="ws") if tier >= 1 else None
    tools = build_living_tools(
        searcher=object(),
        fetcher=object(),
        sandbox=object(),
        memory_getter=_mg,
        tier=tier,
        write_level=min(tier, 2),
        workspace="ws",
        browser_session=session,
    )
    manifest = build_tool_manifest(
        tier,
        min(tier, 2),
        has_browser=tier >= 1,  # session 非 None 即 True
        has_workspace=bool("ws"),
    )
    assert set(manifest) == {t.name for t in tools.tools}


# ---------------------------------------------------------------------------
# 清单 3：free_activity_enabled 开关
# ---------------------------------------------------------------------------
def test_default_activities_free_switch():
    """验收 3：默认含 free；enabled_free=False 时 5 项固定池。"""
    assert any(a.name == "free" for a in default_activities())
    pool = default_activities(enabled_free=False)
    assert all(a.name != "free" for a in pool)
    assert len(pool) == 5


def _decider(config, **kw):
    return ActivityDecider(
        activities=default_activities(),
        config_getter=lambda: config,
        rng=random.Random(7),
        **kw,
    )


def test_decider_free_switch_hot():
    """决策池现读配置：关闭 → rules 决不出 free；改回 true → 立即回池。"""
    config = {"decision": {"decision_mode": "rules", "free_activity_enabled": False}}
    decider = _decider(config)
    picks = {decider.rules_pick().name for _ in range(200)}
    assert "free" not in picks
    config["decision"]["free_activity_enabled"] = True  # 热改，无需重建
    picks = {decider.rules_pick().name for _ in range(200)}
    assert "free" in picks


def test_decider_llm_excludes_free_from_prompt_and_lookup():
    """llm 档：free 不进活动清单 prompt；LLM 硬点 free → 查无 → 回退 rules。"""
    config = {"decision": {"decision_mode": "llm", "free_activity_enabled": False}}
    captured = {}

    async def llm_call(prompt, system_prompt=None):
        captured["prompt"] = prompt
        return '{"activity": "free", "params": {}}'

    decider = _decider(config, llm_call=llm_call)
    decision = asyncio.run(decider.decide())
    assert "- free:" not in captured["prompt"]
    assert decision.activity.name != "free"
    assert decision.mode == "rules"  # lookup miss → 回退


def _bare_loop(config, **kw):
    return LivingLoop(
        gate=object(),
        memory_getter=lambda: None,
        config_getter=lambda: config,
        activities=default_activities(),
        rng=random.Random(3),
        **kw,
    )


def test_loop_free_switch_hot():
    """执行池现读配置：activity_names 与随机选择都摘除 free，热改回 true 即恢复。"""
    config = {"decision": {"free_activity_enabled": False}}
    loop = _bare_loop(config)
    assert "free" not in loop.activity_names
    picks = {loop._pick_activity().name for _ in range(100)}
    assert "free" not in picks
    config["decision"]["free_activity_enabled"] = True
    assert "free" in loop.activity_names
    picks = {loop._pick_activity().name for _ in range(100)}
    assert "free" in picks


def test_schema_declares_free_activity_enabled():
    """schema decision 组声明了该键（bool，默认 true）——GUI 可见。"""
    import json

    schema = json.loads(Path("_conf_schema.json").read_text(encoding="utf-8"))
    key = schema["decision"]["items"]["free_activity_enabled"]
    assert key["type"] == "bool"
    assert key["default"] is True
