"""M29-补丁1 测试：写权限职责解耦（"对外"档位不得管"本机"操作）。

主人设计意图（任务书 1.1）：autonomy.write_level 只管对外（网页上的动作：
点链接/填表/评论/发帖/私信/下单），autonomy.tier 管对内（本机文件读写 +
命令行）——两者不得交叉。

A 组：本机写入与 write_level 解耦（is_write_allowed 两参 + 工具真实路径）；
B 组：shell 挂载只看 tier（tier 4 × write_level 各档）；
C 组：对外判定不回退（check_action_kind 矩阵 + browser_click 真实路径）；
D 组：清单 == 实际挂载（tier 0-4 × write_level 0-3 全组合）；
E 组：文案一致性（活文档无"命令行需写权限"残留）+ 装配线仍同步 write_level。
"""

import asyncio
import json
import types
from pathlib import Path

import pytest

from core.autonomy import build_tool_manifest, check_action_kind, is_write_allowed
from core.living_tools import (
    BrowserClickTool,
    WorkspaceReadTool,
    WorkspaceWriteTool,
    build_living_tools,
)

WORKDIR = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# 装配替身（同 test_m3_patchXIV/_XII 口径）
# ---------------------------------------------------------------------------
class StubSearcher:
    pass


class StubFetcher:
    pass


class StubSandbox:
    pass


class StubMemory:
    async def add(self, c, importance=0.5, metadata=None, **kw):
        return 1


def _mg():
    return StubMemory()


def _build(tier=0, write_level=0, workspace="", browser_session=None):
    return build_living_tools(
        searcher=StubSearcher(),
        fetcher=StubFetcher(),
        sandbox=StubSandbox(),
        memory_getter=_mg,
        tier=tier,
        write_level=write_level,
        workspace=workspace,
        browser_session=browser_session,
    )


def _names(toolset):
    return {t.name for t in toolset.tools}


# ---------------------------------------------------------------------------
# A 组：本机写入不再受 write_level 约束（验收 1/2/3）
# ---------------------------------------------------------------------------
def test_a1_write_allowed_ignores_write_level():
    """is_write_allowed 两参签名：区内即允、红线即拒、区外即拒——
    不再有 write_level 闸门。"""
    assert is_write_allowed("ws/file.txt", "ws") is True
    # 验收 2：受保护路径仍拒（相对形态即红线前缀判定的语义层）
    assert is_write_allowed("data/config/cmd_config.json", "ws") is False
    assert is_write_allowed("astrbot/core/star/x.py", "ws") is False
    assert is_write_allowed("data/plugins/other/x.py", "ws") is False
    # 验收 3：工作区外仍拒
    assert is_write_allowed("/etc/passwd", "ws") is False


def test_a2_write_tool_writes_at_tier2_write_level_0(tmp_path):
    """验收 1：tier=2 + write_level=0 → WorkspaceWriteTool 写工作区内
    文件成功（旧闸门下会被拒——本用例即 bug 的反证）。"""
    ws = tmp_path / "home"
    ws.mkdir()
    ts = _build(tier=2, write_level=0, workspace=str(ws))
    tool = next(t for t in ts.tools if t.name == "workspace_write")
    result = asyncio.run(
        tool.call(None, path="games/galaxy.html", content="<html>hi</html>")
    )
    assert "已写入" in str(result), f"write_level=0 下应可写工作区: {result}"
    assert (ws / "games" / "galaxy.html").read_text(encoding="utf-8") == (
        "<html>hi</html>"
    )


def test_a3_write_tool_still_rejects_protected_and_outside(tmp_path):
    """验收 2/3（工具层）：受保护前缀的相对形态与越界路径仍拒、不落盘。"""
    ws = tmp_path / "home"
    ws.mkdir()
    ts = _build(tier=2, write_level=0, workspace=str(ws))
    tool = next(t for t in ts.tools if t.name == "workspace_write")

    # 越界：../ 逃出工作区 → 拒绝且不落盘
    result = asyncio.run(tool.call(None, path="../escape.txt", content="x"))
    assert "拒绝" in str(result)
    assert not (tmp_path / "escape.txt").exists()

    # 读工具的越界文案（任务书验收 3 的"超出工作区"出处）
    reader = next(t for t in ts.tools if t.name == "workspace_read")
    result2 = asyncio.run(reader.call(None, path="../escape.txt"))
    assert "超出工作区" in str(result2)


# ---------------------------------------------------------------------------
# B 组：shell 挂载只看 tier（验收 4）
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("write_level", [0, 1, 2, 3])
def test_b1_shell_mounted_at_tier4_any_write_level(write_level):
    """验收 4：tier=4 + 任意 write_level → local_shell 在实际挂载；清单同。"""
    ts = _build(tier=4, write_level=write_level, workspace="ws_test_dir")
    assert "local_shell" in _names(ts)
    assert "local_shell" in build_tool_manifest(
        4, has_browser=True, has_workspace=True
    )


@pytest.mark.parametrize("write_level", [0, 3])
def test_b2_shell_absent_below_tier4(write_level):
    """验收 4 反例：tier=3 即使最强 write_level 也不挂 shell（档位分层不变）。"""
    ts = _build(tier=3, write_level=write_level, workspace="ws_test_dir")
    assert "local_shell" not in _names(ts)
    assert "local_shell" not in build_tool_manifest(
        3, has_browser=True, has_workspace=True
    )


# ---------------------------------------------------------------------------
# C 组：对外判定不回退（验收 5）
# ---------------------------------------------------------------------------
def test_c1_check_action_kind_matrix_unchanged():
    """验收 5（纯函数层）：四档 action_kind 允许矩阵与补丁 XV 定稿逐格一致。"""
    assert check_action_kind(0, "navigate")[0] is False
    assert check_action_kind(1, "navigate")[0] is True
    assert check_action_kind(1, "fill")[0] is True
    assert check_action_kind(1, "submit_form")[0] is False
    assert check_action_kind(2, "submit_form")[0] is True
    assert check_action_kind(2, "comment")[0] is True
    assert check_action_kind(2, "post")[0] is False
    assert check_action_kind(3, "post")[0] is True
    assert check_action_kind(3, "purchase")[0] is True
    # 保守拒绝：未标注 / unknown 在 <3 档一律拒
    assert check_action_kind(2, None)[0] is False
    assert check_action_kind(2, "unknown")[0] is False
    assert check_action_kind(3, None)[0] is True


class _FakePage:
    def __init__(self):
        self.clicked = 0

    async def click(self, selector, timeout=5000):
        self.clicked += 1


class _FakeBrowser:
    def __init__(self, page):
        self._page = page

    async def _ensure_page(self):
        return self._page


@pytest.mark.parametrize(
    "write_level,kind,expect_ok",
    [
        (0, "navigate", False),
        (1, "navigate", True),
        (1, "submit_form", False),
        (2, "submit_form", True),
        (2, "comment", True),
        (2, "post", False),
        (3, "post", True),
    ],
)
def test_c2_browser_click_still_gated_by_write_level(write_level, kind, expect_ok):
    """验收 5（真实工具路径）：browser_click 仍由 write_level × action_kind
    把关——拒绝时连页面都不碰。"""
    page = _FakePage()
    tool = BrowserClickTool()
    tool._session_ref = types.SimpleNamespace(
        session=_FakeBrowser(page), write_level=write_level
    )
    tool._write_level = write_level
    result = asyncio.run(
        tool.call(None, selector="#btn", action_kind=kind)
    )
    if expect_ok:
        assert "已点击" in str(result)
        assert page.clicked == 1
    else:
        assert "已拒绝" in str(result) or "不允许" in str(result)
        assert page.clicked == 0


# ---------------------------------------------------------------------------
# D 组：清单 == 实际挂载（验收 6，tier 0-4 × write_level 0-3 全组合）
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("tier", [0, 1, 2, 3, 4])
@pytest.mark.parametrize("write_level", [0, 1, 2, 3])
def test_d1_manifest_matches_mount_all_combos(tier, write_level, monkeypatch):
    """验收 6：全部 20 组合下 build_tool_manifest 与实际挂载一致——
    解耦后清单不再收 write_level，任何档位组合都不得谎报/漏报。"""
    monkeypatch.setattr("core.living_tools.chromium_installed", lambda: True)
    tools = build_living_tools(
        searcher=object(),
        fetcher=object(),
        sandbox=object(),
        memory_getter=_mg,
        tier=tier,
        write_level=write_level,
        workspace="ws_test_dir" if tier >= 2 else "",
        browser_session=object() if tier >= 1 else None,
    )
    manifest = build_tool_manifest(
        tier,
        has_browser=tier >= 1,
        has_workspace=tier >= 2,
    )
    assert set(manifest) == _names(tools), (
        f"tier={tier} write_level={write_level} 清单与实际挂载不一致"
    )
    # shell 语义复核：只在 tier 4 出现，与 write_level 无关
    assert ("local_shell" in _names(tools)) == (tier >= 4)


# ---------------------------------------------------------------------------
# E 组：文案一致性（验收 7）+ 装配线仍同步 write_level（对外线不误删）
# ---------------------------------------------------------------------------
def test_e1_no_coupled_wording_in_live_docs():
    """验收 7：活文档（schema/README/说明书/核心源码）无"命令行需写权限"
    一类残留；write_level 新表述明确"只管对外"。

    范围说明：CHANGELOG 的历史里程碑表按 git log 提炼历史事实，不在此列
    （1.0.0 条目已随本补丁修正）；tests/ 自身的用例注释也不算用户文案。
    """
    needle_legacy = [
        "write_level>=2", "write_level >= 2", "写权限 ≥2", "写权限>=2",
        "写权限也得一起开", "与写文件同级",
    ]
    live_files = [
        WORKDIR / "_conf_schema.json",
        WORKDIR / "README.md",
        WORKDIR / "README_EN.md",
        WORKDIR / "pages" / "config" / "help-content.js",
        WORKDIR / "core" / "autonomy.py",
        WORKDIR / "core" / "living_tools.py",
        WORKDIR / "main.py",
    ]
    for path in live_files:
        text = path.read_text(encoding="utf-8")
        for needle in needle_legacy:
            assert needle not in text, f"{path.name} 仍残留耦合表述: {needle!r}"

    schema = json.loads((WORKDIR / "_conf_schema.json").read_text(encoding="utf-8"))
    wl = schema["advanced"]["items"]["autonomy"]["items"]["write_level"]
    assert "只管" in wl["hint"] and "本机" in wl["hint"], (
        "schema write_level hint 必须明确'只管对外（网上动作），不影响本机'"
    )
    tier_hint = schema["advanced"]["items"]["autonomy"]["items"]["tier"]["hint"]
    assert "write_level" not in tier_hint, "tier hint 不得再提 write_level"


def test_e2_build_tools_still_syncs_write_level_to_browser_line(monkeypatch):
    """防误删：对外线（浏览器会话引用与 click/type 的 _write_level）仍由
    build_living_tools 的 write_level 参数装配——本机解耦不得连带拆了对外。"""
    monkeypatch.setattr("core.living_tools.chromium_installed", lambda: True)

    class _FakeSession:
        _workspace = "ws"

    ts = build_living_tools(
        tier=1,
        write_level=2,
        workspace="ws",
        browser_session=_FakeSession(),
    )
    click = next(t for t in ts.tools if t.name == "browser_click")
    typ = next(t for t in ts.tools if t.name == "browser_type")
    assert click._write_level == 2
    assert typ._write_level == 2
    assert click._session_ref.write_level == 2


def test_e3_source_anchors():
    """源码锚点：解耦后的关键实现逐字在位（调用链守护）。"""
    aut = (WORKDIR / "core" / "autonomy.py").read_text(encoding="utf-8")
    assert "def is_write_allowed(path: str, workspace: str) -> bool:" in aut
    assert "if tier >= 4:\n        names += [\"local_shell\"]" in aut
    lt = (WORKDIR / "core" / "living_tools.py").read_text(encoding="utf-8")
    assert "if tier >= 4:\n        tools.append(LocalShellTool().bind(workspace))" in lt
    assert "LocalShellTool().bind(workspace, write_level)" not in lt
    assert "WorkspaceWriteTool().bind(workspace, write_level)" not in lt
    main_src = (WORKDIR / "main.py").read_text(encoding="utf-8")
    # main 的清单调用点不再传 write_level（manifest 新签名）
    assert "build_tool_manifest(\n            tier,\n            # M15-补丁2" in main_src
