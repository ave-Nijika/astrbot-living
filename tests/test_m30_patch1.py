"""M30-补丁1：不靠搜索也能上网逛（解绑 + 意图改造 + 点链接通路修复）。

对应任务书验收 1-6（第 7 条端到端由凛在 VM 实测；第 8 条全量回归）：
  A 组 解绑：搜索关 + 浏览器可用 → surf/read 保留；浏览器也不可用 → 仍摘。
  B 组 意图：无搜索路径的浏览器浏览意图（可点清单挑链接连点几跳），
       有搜索路径逐字不变（M19 定稿不回退）。
  C 组 点链接通路：说明修正 + 元素清单带建议 action_kind；写权限闸门
       （submit_form/comment/post/message/purchase）零放松——红线。
测试不写 AstrBot 本体数据目录；真实身份一律用先例假号 10001。
"""

import asyncio
import json
import random
import types
from pathlib import Path

import pytest

from core.activities import (
    DEFAULT_PROMPT_INTENT_READ,
    DEFAULT_PROMPT_INTENT_READ_BROWSE,
    DEFAULT_PROMPT_INTENT_SURF,
    DEFAULT_PROMPT_INTENT_SURF_BROWSE,
    ActivityContext,
    ReadArticleActivity,
    SurfActivity,
    activities_excluding_search,
    default_activities,
)
from core.autonomy import _WRITE_LEVEL_ALLOWED
from core.browser_elements import format_elements_block
from core.decider import ActivityDecider
from core.living_loop import LivingLoop
from core.living_tools import BrowserClickTool, BrowserTypeTool

WORKDIR = Path(__file__).resolve().parents[1]

MASTER = "aiocqhttp:FriendMessage:10001"  # 先例假号，非真实身份


def _cfg(search: bool) -> dict:
    return {"capabilities": {"web_search_enabled": search}}


def _pool_names(pool) -> list:
    return [a.name for a in pool]


# ---------------------------------------------------------------------------
# A 组：搜索解绑（验收 1/2）
# ---------------------------------------------------------------------------
def test_a1_search_off_browser_on_keeps_surf_read():
    """验收 1 前半：搜索关闭 + 浏览器可用 → surf/read 仍在活动池
    （不再被无条件摘除——本补丁的核心解绑）。"""
    names = _pool_names(activities_excluding_search(
        default_activities(), _cfg(False), lambda: True
    ))
    assert "surf" in names and "read" in names
    assert "game" in names and "free" in names


def test_a2_search_off_browser_off_still_excludes():
    """验收 1 后半：搜索关闭且浏览器也不可用 → 仍摘除
    （不留"两条上网通路都没有还硬要上网"的空转）。"""
    names = _pool_names(activities_excluding_search(
        default_activities(), _cfg(False), lambda: False
    ))
    assert "surf" not in names and "read" not in names
    assert "game" in names and "free" in names


def test_a3_search_off_no_getter_keeps_old_behavior():
    """缺省（不传第三参，None）＝维持 M15-M29 既有行为：搜索关即摘。
    既有两参调用（M15/M25 的测试）零变化的原因。"""
    names = _pool_names(activities_excluding_search(
        default_activities(), _cfg(False)
    ))
    assert "surf" not in names and "read" not in names


def test_a4_search_on_unaffected():
    """验收 2：搜索开着时行为与现状逐字一致——全池保留，
    浏览器可用性根本不参与（不误伤搜索路径）。"""
    names = _pool_names(activities_excluding_search(
        default_activities(), _cfg(True), lambda: False
    ))
    assert set(names) == {a.name for a in default_activities()}


def test_a5_getter_exception_treated_as_unavailable():
    """getter 抛异常按不可用（保守摘除，回旧行为）——不空转不炸决策。"""
    def boom():
        raise RuntimeError("probe exploded")

    names = _pool_names(activities_excluding_search(
        default_activities(), _cfg(False), boom
    ))
    assert "surf" not in names and "read" not in names


def test_a6_getter_is_lazy():
    """惰性：搜索开着时 getter 一次都不被调用（浏览器探测零开销）。"""
    calls = []

    def probe():
        calls.append(1)
        return True

    activities_excluding_search(default_activities(), _cfg(True), probe)
    assert calls == []


def test_a7_decider_and_loop_pools_wire_the_getter():
    """接线：decider 与 loop 两池同口径——搜索关 + 浏览器可用时两侧都
    保留 surf/read（/living do 指名与决策池不再分叉）。"""
    activities = default_activities()
    decider = ActivityDecider(
        activities=activities,
        config_getter=lambda: _cfg(False),
        browser_available_getter=lambda: True,
    )
    decider_names = [a.name for a in decider._effective_activities()]
    assert "surf" in decider_names and "read" in decider_names
    loop = LivingLoop(
        gate=types.SimpleNamespace(),
        memory_getter=lambda: object(),
        config_getter=lambda: _cfg(False),
        activities=list(activities),
        browser_available_getter=lambda: True,
    )
    assert "surf" in loop.activity_names
    assert "read" in loop.activity_names
    # 浏览器不可用 → 两侧都摘（同口径的另一面）
    decider_off = ActivityDecider(
        activities=list(activities),
        config_getter=lambda: _cfg(False),
        browser_available_getter=lambda: False,
    )
    off_names = [a.name for a in decider_off._effective_activities()]
    assert "surf" not in off_names and "read" not in off_names


# ---------------------------------------------------------------------------
# B 组：无搜索路径的浏览器浏览意图（验收 6）
# ---------------------------------------------------------------------------
def _ctx(agent=None, *, search_enabled=True, config=None):
    return ActivityContext(
        searcher=object(), fetcher=object(), sandbox=object(),
        memory=object(), gate=None, event=None, rng=random.Random(7),
        agent=agent, search_enabled=search_enabled,
        config_getter=(lambda: config) if config is not None else None,
    )


def test_b1_search_path_intent_verbatim():
    """验收 2/6：有搜索时意图与 M19 定稿逐字一致（渲染后 == 默认模板，
    不回退）；且不含浏览器指引（两路不混）。"""
    intent = SurfActivity().agent_intent(_ctx(search_enabled=True))
    assert intent == DEFAULT_PROMPT_INTENT_SURF.replace(
        "{topic_line}", "主题你自己挑。"
    ).replace("{avoid_line}", "")
    assert "browser_navigate" not in intent


def test_b2_surf_browse_intent_guides_browsing():
    """验收 6：无搜索路径的 surf 意图——浏览器五件套 + 可点清单 +
    连点多跳的明确指引，且不再提 web_search。"""
    intent = SurfActivity().agent_intent(_ctx(search_enabled=False))
    assert "browser_navigate" in intent
    assert "browser_read" in intent
    assert "browser_click" in intent
    assert "点" in intent and "好几跳" in intent
    assert "action_kind=navigate" in intent
    assert "web_search" not in intent
    # 与有搜索模板是两个不同的文本
    assert intent != DEFAULT_PROMPT_INTENT_SURF.replace(
        "{topic_line}", "主题你自己挑。"
    ).replace("{avoid_line}", "")


def test_b3_read_browse_intent_guides_browsing():
    """验收 6：无搜索路径的 read 意图——同款浏览器浏览指引。"""
    intent = ReadArticleActivity().agent_intent(_ctx(search_enabled=False))
    assert "browser_navigate" in intent
    assert "browser_read" in intent
    assert "browser_click" in intent
    assert "好几页" in intent
    assert "web_search" not in intent


def test_b4_browse_intent_has_own_config_key():
    """新意图做成配置键（沿用 M19 先例）：面板键
    decision.prompt_intent_surf_browse 覆盖生效；read_browse 同理。"""
    config = {"decision": {"prompt_intent_surf_browse": "自定义逛意图 {topic_line}"}}
    intent = SurfActivity().agent_intent(
        _ctx(search_enabled=False, config=config)
    )
    assert intent == "自定义逛意图 主题你自己挑。"

    config_r = {"decision": {"prompt_intent_read_browse": "自定义读意图"}}
    intent_r = ReadArticleActivity().agent_intent(
        _ctx(search_enabled=False, config=config_r)
    )
    assert intent_r == "自定义读意图"


def test_b5_browse_path_does_not_read_search_key():
    """两键独立：只覆盖有搜索键（prompt_intent_surf）时，无搜索路径
    仍用浏览模板默认值——不互相串。"""
    config = {"decision": {"prompt_intent_surf": "只影响搜索路径"}}
    intent = SurfActivity().agent_intent(
        _ctx(search_enabled=False, config=config)
    )
    assert intent == DEFAULT_PROMPT_INTENT_SURF_BROWSE.replace(
        "{topic_line}", "主题你自己挑。"
    ).replace("{avoid_line}", "")


def test_b6_schema_has_browse_keys_with_defaults():
    """面板可见性：schema 的 decision 组含两个浏览意图键，type=text、
    有 default（非空）。默认值逐字对照由 test_m19_patch1 的 pairs 覆盖。"""
    schema = json.loads(
        (WORKDIR / "_conf_schema.json").read_text(encoding="utf-8")
    )
    decision = schema["advanced"]["items"]["decision"]["items"]
    for key in ("prompt_intent_surf_browse", "prompt_intent_read_browse"):
        item = decision[key]
        assert item["type"] == "text"
        assert item["default"].strip()
        assert "{topic_line}" in item["default"]


# ---------------------------------------------------------------------------
# C 组：点链接通路（验收 3/4/5）+ 写权限红线
# ---------------------------------------------------------------------------
class _FakePage:
    def __init__(self):
        self.clicked = 0
        self.filled = None

    async def click(self, selector, timeout=5000):
        self.clicked += 1

    async def fill(self, selector, text):
        self.filled = (selector, text)


class _FakeBrowser:
    def __init__(self, page):
        self._page = page

    async def _ensure_page(self):
        return self._page


def _click_tool(page, write_level):
    tool = BrowserClickTool()
    tool._session_ref = types.SimpleNamespace(
        session=_FakeBrowser(page), write_level=write_level
    )
    tool._write_level = write_level
    return tool


def _type_tool(page, write_level):
    tool = BrowserTypeTool()
    tool._session_ref = types.SimpleNamespace(
        session=_FakeBrowser(page), write_level=write_level
    )
    tool._write_level = write_level
    return tool


def test_c1_click_link_with_navigate_passes():
    """验收 3：点普通链接（标 navigate）真调用走通——write_level=1 即放行，
    页面真的被点（_FakePage.click 计数）。"""
    page = _FakePage()
    tool = _click_tool(page, write_level=1)
    result = asyncio.run(
        tool.call(None, selector='a[href="/next"]', action_kind="navigate")
    )
    assert "已点击" in str(result)
    assert page.clicked == 1


@pytest.mark.parametrize(
    "write_level,kind",
    [
        (1, "submit_form"),  # 验收 4：写入类在低写权限下仍拒
        (1, None),  # 验收 4：漏标 → unknown → 保守拒绝
        (1, "post"),
        (2, "post"),  # 红线：post 的闸门是 write_level>=3，档位不够照拒
        (0, "navigate"),  # 只看档位连点链接也不行（M29 解耦不松动对外）
    ],
)
def test_c2_write_kinds_still_blocked(write_level, kind):
    """验收 4 + 红线：写入类动作闸门零放松——拒绝时连页面都不碰。"""
    page = _FakePage()
    tool = _click_tool(page, write_level=write_level)
    kwargs = {"selector": "#x"}
    if kind is not None:
        kwargs["action_kind"] = kind
    result = str(asyncio.run(tool.call(None, **kwargs)))
    assert "已点击" not in result
    assert ("已拒绝" in result) or ("不允许" in result)
    assert page.clicked == 0


def test_c3_type_fill_passes_and_message_kind_blocked():
    """fill（write_level>=1）走通；message（私信）红线仍在。"""
    page = _FakePage()
    tool = _type_tool(page, write_level=1)
    result = str(asyncio.run(
        tool.call(None, selector="#q", text="宇宙", action_kind="fill")
    ))
    assert "已" in result and "填入" in result
    assert page.filled == ("#q", "宇宙")

    page2 = _FakePage()
    tool2 = _type_tool(page2, write_level=1)
    result2 = str(asyncio.run(
        tool2.call(None, selector="#dm", text="hi", action_kind="message")
    ))
    assert ("已拒绝" in result2) or ("不允许" in result2)
    assert page2.filled is None


def test_c4_tool_description_no_longer_misleads():
    """C(a) 说明修正锚点：click 的说明明确"点链接/翻页标 navigate"，
    且保留"漏标会被拒"；type 的说明明确普通填字标 fill。
    判定表本身零改动（红线：本批只动说明与提示，不动闸门）。"""
    click = BrowserClickTool()
    assert "navigate" in click.description
    assert "点链接" in click.description
    assert "漏标" in click.description
    kind_desc = click.parameters["properties"]["action_kind"]["description"]
    assert "navigate(点链接" in kind_desc

    typ = BrowserTypeTool()
    assert 'action_kind="fill"' in typ.description
    assert "漏标" in typ.description

    # 红线：判定表与改造前逐字一致（submit/comment/post/message/purchase 不放松）
    assert _WRITE_LEVEL_ALLOWED == {
        0: frozenset(),
        1: frozenset({"navigate", "fill"}),
        2: frozenset({"navigate", "fill", "submit_form", "comment"}),
        3: None,
    }


def test_c5_elements_block_suggests_action_kinds():
    """验收 5：清单为每条元素附建议 action_kind——链接 navigate、
    输入框/下拉 fill、按钮明说留给她判断（照清单点就能走通，不用猜）。"""
    infos = [
        {"kind": "link", "tag": "a", "label": "下一页", "href": "/2",
         "path": "body>a:nth-of-type(1)"},
        {"kind": "input", "tag": "input", "label": "搜索", "placeholder": "关键词",
         "path": "body>input:nth-of-type(1)"},
        {"kind": "select", "tag": "select", "label": "排序", "name": "sort",
         "path": "body>select:nth-of-type(1)"},
        {"kind": "button", "tag": "button", "label": "GO",
         "path": "body>button:nth-of-type(1)"},
    ]
    block = format_elements_block(infos)
    lines = block.splitlines()
    assert lines[0].startswith("可交互元素")
    assert "建议的 action_kind" in lines[0]
    assert "建议 action_kind: navigate" in lines[1]
    assert "建议 action_kind: fill" in lines[2]
    assert "建议 action_kind: fill" in lines[3]
    assert "按用途自行判断" in lines[4]
    # 建议值是提示，不是闸门：清单措辞里不得出现"无需标"一类误导
    assert "无需标" not in block and "不用标" not in block


def test_c5b_empty_block_still_empty():
    assert format_elements_block([]) == ""


# ---------------------------------------------------------------------------
# D 组：文案口径（搜索关 ≠ 不能上网）
# ---------------------------------------------------------------------------
def test_d1_search_off_wording_updated_in_live_docs():
    """D 组：活文档不再有"搜索关了就不能上网"的旧表述；新表述明确
    "改用浏览器直接逛、两条路都没有才剔除"。"""
    needle_old = [
        "冲浪和读文章会停",
        "冲浪、读文章停",
        "冲浪/读文章停",
        "上网冲浪、读文章会停",
    ]
    for rel in ("_conf_schema.json", "pages/config/help-content.js",
                "README.md"):
        text = (WORKDIR / rel).read_text(encoding="utf-8")
        for needle in needle_old:
            assert needle not in text, f"{rel} 残留旧表述: {needle!r}"

    schema = json.loads(
        (WORKDIR / "_conf_schema.json").read_text(encoding="utf-8")
    )
    hint = (
        schema["advanced"]["items"]["capabilities"]["items"]
        ["web_search_enabled"]["hint"]
    )
    assert "浏览器" in hint and "剔除" in hint

    help_text = (WORKDIR / "pages" / "config" / "help-content.js").read_text(
        encoding="utf-8"
    )
    assert "改用浏览器直接逛" in help_text

    readme = (WORKDIR / "README.md").read_text(encoding="utf-8")
    assert "改用浏览器直接逛" in readme


def test_d2_activities_module_docs_match_new_semantics():
    """源码口径自洽：SEARCH_DEPENDENT_ACTIVITIES 的注释与
    activities_excluding_search 的 docstring 都写明解绑语义。"""
    import core.activities as mod

    text = Path(mod.__file__).read_text(encoding="utf-8")
    assert "browser_available" in text
    assert "M30-补丁1" in text
