"""M15-补丁2：浏览器工具装配修复（A1 死类复活 + A2 fail-closed 挂载）。

覆盖任务书测试要求：
- T1 装配形态：五件套挂载且每个工具的 _session_ref 是 BrowserSessionRef、
  .session 正是传入的会话对象、write_level 已同步；
- T2 五路调用链：navigate/read/screenshot/click/type 用替身会话逐一调用，
  不再 AttributeError、返回内容正确（screenshot 按 C0 既有行为断言）；
- T3 fail-closed：chromium_installed() 返回 False 与 None 两种 → 五件套均
  不挂 + INFO 日志（caplog 断言）；
- T5 ref.write_level 语义：与 bind_session 第二参数 write_level 的关系
  （ref 同步装配期值；click/type 仍以自带 _write_level 做权限判定）。
- T4 全量回归（python -m pytest tests -q）见 docs/task_M15-补丁2_报告.md。

替身会话只实现工具取值路径用到的成员：_ensure_page / save_state /
_workspace——不依赖 playwright、不启动真实浏览器。
"""

import asyncio
import logging
from pathlib import Path

import pytest

from core.living_tools import BrowserSessionRef, build_living_tools

BROWSER_FIVE = [
    "browser_navigate",
    "browser_read",
    "browser_screenshot",
    "browser_click",
    "browser_type",
]


@pytest.fixture
def fake_session(tmp_path):
    """替身浏览器会话：只实现五个工具取值路径用到的三个成员。"""

    class FakePage:
        def __init__(self):
            self.goto_url = None
            self.clicked = []
            self.filled = []
            self.shot_path = None

        async def goto(self, url, timeout=None, wait_until=None):
            self.goto_url = url

        async def title(self):
            return "替身页面"

        async def inner_text(self, selector):
            return "替身正文" * 400  # > 2000/3000 截断上限，便于断言截断行为

        async def click(self, selector, timeout=None):
            self.clicked.append(selector)

        async def fill(self, selector, text):
            self.filled.append((selector, text))

        async def screenshot(self, path=None):
            self.shot_path = path
            Path(path).write_bytes(b"\x89PNG\r\n\x1a\nfake-bytes")

    class FakeBrowserSession:
        def __init__(self, workspace):
            self._workspace = str(workspace)
            self._page = FakePage()
            self.save_state_calls = 0

        async def _ensure_page(self):
            return self._page

        async def save_state(self):
            self.save_state_calls += 1

    return FakeBrowserSession(tmp_path)


@pytest.fixture
def chromium_on(monkeypatch):
    """模拟 Chromium 已安装（A2 探测返回 True → 走正常挂载路径）。"""
    monkeypatch.setattr("core.living_tools.chromium_installed", lambda: True)


def _browser_tools(toolset):
    return {t.name: t for t in toolset.tools if t.name in BROWSER_FIVE}


# ---------------------------------------------------------------------------
# T1 装配形态
# ---------------------------------------------------------------------------
def test_t1_mount_shape_uses_session_ref(fake_session, chromium_on):
    ts = build_living_tools(tier=1, browser_session=fake_session, write_level=2)
    browser = _browser_tools(ts)
    assert set(browser) == set(BROWSER_FIVE), "chromium_installed()=True 时五件套必须全部挂载"
    for name, tool in browser.items():
        ref = tool._session_ref
        assert isinstance(ref, BrowserSessionRef), (
            f"{name} 的 _session_ref 必须是 BrowserSessionRef（A1 死类复活）"
        )
        assert ref.session is fake_session, (
            f"{name} 的 ref.session 必须正是传入的会话对象（跨工具共享同一实例）"
        )
        assert ref.write_level == 2, (
            f"{name} 的 ref.write_level 必须同步装配期 write_level=2"
        )
    # bind_session 第二参数 write_level 的既有传参不变：click/type 仍各带
    # 自己的 _write_level（权限判定读它，不读 ref.write_level）
    assert browser["browser_click"]._write_level == 2
    assert browser["browser_type"]._write_level == 2
    assert browser["browser_navigate"]._session_ref is browser["browser_read"]._session_ref, (
        "五个工具必须共享同一个 BrowserSessionRef 实例"
    )


# ---------------------------------------------------------------------------
# T2 五路调用链（替身会话；核心断言：不再 'BrowserSession' object has no
# attribute 'session'）
# ---------------------------------------------------------------------------
def test_t2_five_call_chains_no_attribute_error(fake_session, chromium_on):
    ts = build_living_tools(tier=1, browser_session=fake_session, write_level=1)
    b = _browser_tools(ts)

    r = asyncio.run(b["browser_navigate"].call(None, url="https://example.com/"))
    assert "替身页面" in r and "https://example.com/" in r and "替身正文" in r
    assert fake_session.save_state_calls == 1, "navigate 成功后应 save_state() 一次"
    assert fake_session._page.goto_url == "https://example.com/"

    r = asyncio.run(b["browser_read"].call(None))
    assert "替身页面" in r and "替身正文" in r
    # _max_text=3000 截断：1200 字正文不足上限，全文返回
    body = r.split(chr(10), 1)[1]
    assert len(body) == 1600  # 4 字 x 400 次，不足 3000 上限不截断

    r = asyncio.run(b["browser_screenshot"].call(None))
    # C0 既有行为：未注入 image_probe/captioner → 默认看图路径，返回含
    # ImageContent 的 CallToolResult（本补丁不改动该通道，仅按其既有行为断言）
    assert type(r).__name__ == "CallToolResult"
    kinds = [c.type for c in r.content]
    assert "image" in kinds and "text" in kinds
    shot = fake_session._page.shot_path
    assert shot is not None and Path(shot).is_file(), "截图应落工作区 screenshots/"

    r = asyncio.run(
        b["browser_click"].call(None, selector="#go", action_kind="navigate")
    )
    assert r == "已点击 #go"
    assert fake_session._page.clicked == ["#go"]

    r = asyncio.run(
        b["browser_type"].call(None, selector="#q", text="你好", action_kind="fill")
    )
    assert r == "已在 #q 填入 2 字"
    assert fake_session._page.filled == [("#q", "你好")]


def test_t2_screenshot_probe_false_falls_back_to_text(fake_session, chromium_on):
    """C0 既有行为回归：探针明确返回 False 且无 captioner → 仅存盘文本。"""
    ts = build_living_tools(
        tier=1,
        browser_session=fake_session,
        image_probe=lambda: False,
    )
    tool = _browser_tools(ts)["browser_screenshot"]
    r = asyncio.run(tool.call(None))
    assert isinstance(r, str)
    assert "当前模型不支持查看图片" in r
    assert Path(fake_session._page.shot_path).is_file()


# ---------------------------------------------------------------------------
# T3 fail-closed：False 与 None 两态
# ---------------------------------------------------------------------------
def test_t3_fail_closed_when_not_installed(fake_session, monkeypatch, caplog):
    monkeypatch.setattr("core.living_tools.chromium_installed", lambda: False)
    with caplog.at_level(logging.INFO, logger="astrbot"):
        ts = build_living_tools(tier=1, browser_session=fake_session)
    assert _browser_tools(ts) == {}, "chromium_installed()=False 时五件套一件都不挂"
    assert any(
        "Chromium 未安装" in r.getMessage() and "浏览器工具不挂载" in r.getMessage()
        for r in caplog.records
    ), "必须留下 INFO 日志说明不挂载的原因"


def test_t3_fail_closed_when_probe_none(fake_session, monkeypatch, caplog):
    """探测失败（None）也按未装处理——fail-closed，防"挂了却必错"。"""
    monkeypatch.setattr("core.living_tools.chromium_installed", lambda: None)
    with caplog.at_level(logging.INFO, logger="astrbot"):
        ts = build_living_tools(tier=1, browser_session=fake_session)
    assert _browser_tools(ts) == {}, "chromium_installed()=None（探测失败）同样不挂载"
    assert any(
        "Chromium 未安装" in r.getMessage() for r in caplog.records
    )


def test_t3_fail_closed_keeps_other_tools(fake_session, monkeypatch):
    """不挂浏览器不影响其他能力（fail-closed 只收窄浏览器五件套）。"""
    monkeypatch.setattr("core.living_tools.chromium_installed", lambda: False)
    # M23-补丁1：shell 独占第 4 档且需 write_level>=2（C1 挂载闸门），
    # 用 tier=4 + write_level=2 验证"浏览器缺席、其余照旧"
    ts = build_living_tools(
        tier=4,
        browser_session=fake_session,
        workspace=str(fake_session._workspace),
        write_level=2,
    )
    names = {t.name for t in ts.tools}
    assert names.isdisjoint(set(BROWSER_FIVE))
    assert "local_shell" in names and "workspace_read" in names


# ---------------------------------------------------------------------------
# T5 ref.write_level 与 bind_session 第二参数的关系
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("write_level", [0, 1, 2, 3])
def test_t5_write_level_synced_to_ref_and_tools(fake_session, chromium_on, write_level):
    ts = build_living_tools(
        tier=1, browser_session=fake_session, write_level=write_level
    )
    b = _browser_tools(ts)
    ref = b["browser_navigate"]._session_ref
    # 1) ref.write_level 同步装配期传入值（A1）；
    assert ref.write_level == write_level
    # 2) click/type 的 bind_session 第二参数 write_level 既有传参不变，
    #    权限判定（check_action_kind）读的是工具自带 _write_level，不读 ref；
    assert b["browser_click"]._write_level == write_level
    assert b["browser_type"]._write_level == write_level


def test_t5_write_level_permission_still_enforced(fake_session, chromium_on):
    """write_level=0 时 click/type 仍被权限层拒（补丁 XV 语义不回退）。"""
    ts = build_living_tools(tier=1, browser_session=fake_session, write_level=0)
    b = _browser_tools(ts)
    r = asyncio.run(
        b["browser_click"].call(None, selector="#go", action_kind="navigate")
    )
    assert "拒绝" in r
    assert fake_session._page.clicked == [], "被拒时连页面都不碰"
