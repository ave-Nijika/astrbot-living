"""M36-补丁1：让她能打开自己工作区里的网页。

覆盖任务书验收 1-8：
- 验收 1/7：工作区内 .html 能开（含中文名/转义路径），标题正文可读；
- 验收 2：工作区外 file:// 拒绝（Windows 与 Unix 两种形态）；
- 验收 3：javascript: / data: / ftp: 等非 http/file 协议拒绝；
- 验收 4：点击指向工作区外 file:// 链接拒绝（含相对链接解析后越界）；
- 验收 5：工作区内不存在的路径拒绝（防猜路径探测）；
- 验收 6：http/https 行为逐字不变（含 M35 资料区包装）；
- 验收 8：零回归（全量见 docs/m36_patch1_report.md）。

红线锁：URL 准入单点实现（_gate_url 定义一次、四个浏览器工具复用）；
对外写权限闸门（check_action_kind）优先级与语义不变；本地 file 页
是本地产物，按 M35 口径不包资料区（note_read 语料同样不带壳）。
"""

import asyncio
from pathlib import Path
from urllib.parse import quote

import pytest

import core.living_tools as lt
from core.living_tools import (
    BrowserClickTool,
    BrowserNavigateTool,
    BrowserReadTool,
    BrowserScreenshotTool,
    BrowserSessionRef,
    FetchPageTool,
    _gate_url,
)

BEGIN = lt._EXTERNAL_BEGIN
NOTE = lt._EXTERNAL_NOTE
END = lt._EXTERNAL_END


# ---------------------------------------------------------------------------
# 装配件
# ---------------------------------------------------------------------------
class FakePage:
    """替身页面：url 可变、goto/click/get_attribute 记录调用。"""

    def __init__(self, url="about:blank", title="替身标题", text="替身正文一句。",
                 fail_goto=False):
        self.url = url
        self._title = title
        self._text = text
        self.goto_calls = []
        self.clicks = []
        self.attrs = {}  # selector -> {属性名: 值}
        self.fail_goto = fail_goto

    async def goto(self, url, timeout=None, wait_until=None):
        if self.fail_goto:
            raise RuntimeError("net::ERR_CONNECTION_RESET")
        self.goto_calls.append(url)
        self.url = url

    async def title(self):
        return self._title

    async def inner_text(self, selector):
        return self._text

    async def click(self, selector, timeout=None):
        self.clicks.append(selector)

    async def get_attribute(self, selector, name):
        return self.attrs.get(selector, {}).get(name)


class FakeSession:
    def __init__(self, workspace, page=None):
        self._workspace = str(workspace)
        self.page = page or FakePage()
        self.note_calls = []
        self.save_state_calls = 0

    async def _ensure_page(self):
        return self.page

    async def save_state(self):
        self.save_state_calls += 1

    def note_read(self, url, title, text):
        self.note_calls.append({"url": url, "title": title, "text": text})


@pytest.fixture
def ws(tmp_path):
    """带真实文件的工作区：首页 / 子目录页 / 中文名页。"""
    (tmp_path / "index.html").write_text(
        "<html><head><title>我的首页</title></head><body><h1>欢迎</h1></body></html>",
        encoding="utf-8",
    )
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "page.html").write_text(
        "<html><head><title>第二页</title></head></html>", encoding="utf-8"
    )
    (tmp_path / "我的页面.html").write_text(
        "<html><head><title>中文页</title></head></html>", encoding="utf-8"
    )
    return tmp_path.resolve()


def uri(p) -> str:
    return Path(p).resolve().as_uri()


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# 纯函数层：_gate_url 单点判定
# ---------------------------------------------------------------------------
def test_gate_https_passthrough_and_case_lock(ws):
    """http/https 原样放行（含返回值逐字一致）；非小写前缀仍拒（不放宽）。"""
    for u in ("http://a.com/x", "https://b.com/y?z=1"):
        allowed, reason = _gate_url(u, str(ws))
        assert allowed == u and reason is None
    allowed, _ = _gate_url("HTTP://a.com/x", str(ws))
    assert allowed is None  # 原实现 startswith 小写前缀，大写形态原本就拒


def test_gate_file_inside_allowed_returns_canonical_uri(ws):
    """工作区内存在的文件放行，返回规范化 file URL（as_uri，正斜杠+转义）。"""
    allowed, reason = _gate_url(uri(ws / "index.html"), str(ws))
    assert allowed == uri(ws / "index.html")
    assert reason is None
    assert allowed.startswith("file:///")
    assert "\\" not in allowed


def test_gate_file_outside_rejected_both_platform_forms(ws):
    """工作区外的 file:// 拒绝：Windows 盘符形态与 Unix 形态都拒。"""
    for u in ("file:///C:/Windows/system.ini", "file:///etc/passwd"):
        allowed, reason = _gate_url(u, str(ws))
        assert allowed is None
        assert "文件夹外面" in reason


def test_gate_file_missing_in_workspace_rejected(ws):
    """工作区内不存在的路径拒绝（防猜路径探测），理由给相对名。"""
    allowed, reason = _gate_url(uri(ws / "no_such.html"), str(ws))
    assert allowed is None
    assert "没有" in reason and "no_such.html" in reason


def test_gate_traversal_inside_ok_outside_rejected(ws):
    """页内穿越（resolve 后仍在工作区内）放行；穿出工作区拒绝。"""
    allowed, _ = _gate_url(uri(ws) + "/sub/../index.html", str(ws))
    assert allowed == uri(ws / "index.html")
    allowed, reason = _gate_url(uri(ws) + "/../escape.html", str(ws))
    assert allowed is None and "文件夹外面" in reason


def test_gate_chinese_and_percent_escape(ws):
    """中文路径三种写法（as_uri 转义 / 反斜杠原生 / 正斜杠原生）都放行且归一。"""
    cn = uri(ws / "我的页面.html")
    allowed, _ = _gate_url(cn, str(ws))
    assert allowed == cn
    allowed, _ = _gate_url(f"file:///{ws}\\我的页面.html", str(ws))
    assert allowed == cn
    allowed, _ = _gate_url(f"file:///{ws.as_posix()}/我的页面.html", str(ws))
    assert allowed == cn
    # 转义形态（quote 只转中文与特殊字符，保留 / 与 :）
    escaped = "file:///" + quote(ws.as_posix() + "/我的页面.html", safe="/:")
    allowed, _ = _gate_url(escaped, str(ws))
    assert allowed == cn


def test_gate_other_schemes_rejected(ws):
    """非 http/file 协议一律拒：javascript: / data: / ftp: / about: / 裸串。"""
    for u in (
        "javascript:alert(1)",
        "data:text/html,<b>x</b>",
        "ftp://files.example.com/a",
        "about:blank",
        "example.com/page",
    ):
        allowed, reason = _gate_url(u, str(ws))
        assert allowed is None, u
        assert "不是能打开的网页" in reason


def test_gate_unc_and_drive_letter_forms(ws):
    """file://server/ 共享路径拒；file://C:/x 两斜杠盘符写法也拒（不在工作区）。"""
    allowed, reason = _gate_url("file://server/share/doc.htm", str(ws))
    assert allowed is None and "认不出来" in reason
    allowed, reason = _gate_url("file://C:/Windows/win.ini", str(ws))
    assert allowed is None and "文件夹外面" in reason


def test_gate_empty_workspace_rejected():
    """工作区未配置时 file:// 一律拒（拿不准就拒）。"""
    allowed, reason = _gate_url("file:///C:/x.html", "")
    assert allowed is None
    assert "没法确认" in reason


def test_gate_require_exists_off_for_current_page(ws):
    """require_exists=False（当前页已加载场景）只判界不判存在。"""
    allowed, reason = _gate_url(uri(ws / "no_such.html"), str(ws), require_exists=False)
    assert allowed == uri(ws / "no_such.html")
    assert reason is None
    allowed, _ = _gate_url(uri(ws) + "/../x.html", str(ws), require_exists=False)
    assert allowed is None


# ---------------------------------------------------------------------------
# 验收 1/7：browser_navigate 打开工作区网页（不包壳）
# ---------------------------------------------------------------------------
def _nav(session):
    return BrowserNavigateTool().bind_session(BrowserSessionRef(session))


def test_v1_navigate_opens_workspace_html(ws):
    session = FakeSession(ws, FakePage(title="我的首页", text="页面正文一句。"))
    out = run(_nav(session).call(context=None, url=uri(ws / "index.html")))
    s = str(out)
    assert "我的首页" in s and "页面正文一句。" in s
    assert s.startswith("已打开「我的首页」（")
    # goto 收到的是规范化 file URL
    assert session.page.goto_calls == [uri(ws / "index.html")]
    # 本地文件是本地产物：不包资料区
    assert BEGIN not in s and NOTE not in s and END not in s
    # note_read 留档正常（语料不带壳）
    assert session.note_calls and session.note_calls[0]["url"] == uri(ws / "index.html")
    assert session.save_state_calls == 1


def test_v1_navigate_chinese_file_opens(ws):
    session = FakeSession(ws, FakePage(title="中文页", text="中文正文。"))
    out = run(_nav(session).call(context=None, url=uri(ws / "我的页面.html")))
    assert "中文正文。" in str(out)
    assert session.page.goto_calls == [uri(ws / "我的页面.html")]


# ---------------------------------------------------------------------------
# 验收 2/3/5：navigate 拒绝路径（不碰页面、不抛异常）
# ---------------------------------------------------------------------------
def test_v2_navigate_outside_rejected_no_goto(ws):
    session = FakeSession(ws)
    out = run(_nav(session).call(context=None, url="file:///C:/Windows/win.ini"))
    s = str(out)
    assert s.startswith("打不开——")
    assert "文件夹外面" in s
    assert session.page.goto_calls == []  # 拒绝时连页面都不碰


def test_v3_navigate_bad_scheme_rejected(ws):
    session = FakeSession(ws)
    out = run(_nav(session).call(context=None, url="javascript:alert(1)"))
    assert "不是能打开的网页" in str(out)
    out = run(_nav(session).call(context=None, url="data:text/html,x"))
    assert "不是能打开的网页" in str(out)
    assert session.page.goto_calls == []


def test_v5_navigate_missing_rejected(ws):
    session = FakeSession(ws)
    out = run(_nav(session).call(context=None, url=uri(ws / "no_such.html")))
    assert "没有" in str(out) and "no_such.html" in str(out)
    assert session.page.goto_calls == []


def test_v6_navigate_failure_readable_not_raised(ws):
    """打不开（goto 抛错）返回可读理由，不抛原始异常（失败不阻断）。"""
    session = FakeSession(ws, FakePage(fail_goto=True))
    out = run(_nav(session).call(context=None, url=uri(ws / "index.html")))
    s = str(out)
    assert s.startswith("打开失败——")
    assert isinstance(out, str)


# ---------------------------------------------------------------------------
# 验收 6：http/https 行为逐字不变（含 M35 资料区包装）
# ---------------------------------------------------------------------------
def test_v6_navigate_http_behavior_unchanged(ws):
    session = FakeSession(ws, FakePage(title="外部标题", text="外部正文行。\n第二行。"))
    out = run(_nav(session).call(context=None, url="https://e.com/x?a=1"))
    s = str(out)
    # goto 收到原样 URL、note_read 记原样 URL
    assert session.page.goto_calls == ["https://e.com/x?a=1"]
    assert session.note_calls[0]["url"] == "https://e.com/x?a=1"
    # 返回形态与 M35 一致：状态行在壳外、正文包资料区
    assert s.startswith("已打开「外部标题」（https://e.com/x?a=1）\n")
    assert BEGIN in s and NOTE in s and END in s
    assert s.endswith(f"外部正文行。\n第二行。\n{END}")


def test_fetch_page_file_still_rejected(ws):
    """fetch_page 不接入 file://（本批决策）：仍只认 http(s)，文案不变。"""
    class FakeFetcher:
        async def fetch(self, url):
            return {"title": "t", "text": "x", "status": 200}

    out = run(FetchPageTool().bind(FakeFetcher()).call(
        context=None, url=uri(ws / "index.html")))
    assert str(out) == "错误：需要完整的 http(s) URL"


# ---------------------------------------------------------------------------
# 验收 4：browser_click 链接事前判定（同一道 gate）
# ---------------------------------------------------------------------------
def _click(session, wl=2):
    return BrowserClickTool().bind_session(BrowserSessionRef(session), wl)


def test_v4_click_outside_file_link_rejected(ws):
    page = FakePage(url=uri(ws / "index.html"))
    page.attrs["a.outside"] = {"href": "file:///C:/Windows/win.ini"}
    session = FakeSession(ws, page)
    out = run(_click(session).call(
        context=None, selector="a.outside", action_kind="navigate"))
    s = str(out)
    assert s.startswith("这个链接不点——")
    assert "文件夹外面" in s
    assert page.clicks == []  # 拒绝时连点击都不做


def test_v4_click_workspace_link_allowed(ws):
    page = FakePage(url=uri(ws / "index.html"))
    page.attrs["a.next"] = {"href": uri(ws / "sub" / "page.html")}
    session = FakeSession(ws, page)
    out = run(_click(session).call(
        context=None, selector="a.next", action_kind="navigate"))
    assert str(out) == "已点击 a.next"
    assert page.clicks == ["a.next"]


def test_v4_click_relative_link_resolved(ws):
    """相对链接按当前页地址解析后再判：页内相对放行、越出工作区拒。"""
    page = FakePage(url=uri(ws / "sub" / "page.html"))
    page.attrs["a.up"] = {"href": "../index.html"}
    page.attrs["a.escape"] = {"href": "../../../../../Windows/notepad.exe"}
    session = FakeSession(ws, page)
    out = run(_click(session).call(
        context=None, selector="a.up", action_kind="navigate"))
    assert str(out) == "已点击 a.up"
    out = run(_click(session).call(
        context=None, selector="a.escape", action_kind="navigate"))
    assert "这个链接不点" in str(out)
    assert page.clicks == ["a.up"]


def test_v4_click_no_href_allowed_and_javascript_rejected(ws):
    """无 href 的元素（按钮类）点击不受影响；javascript: 链接不点。"""
    page = FakePage(url=uri(ws / "index.html"))
    page.attrs["button.go"] = {}
    page.attrs["a.js"] = {"href": "javascript:void(0)"}
    session = FakeSession(ws, page)
    out = run(_click(session).call(
        context=None, selector="button.go", action_kind="fill"))
    assert str(out) == "已点击 button.go"
    out = run(_click(session).call(
        context=None, selector="a.js", action_kind="navigate"))
    assert "这个链接不点" in str(out)


def test_v4_click_permission_gate_priority_unchanged(ws):
    """权限闸门优先级不变：write_level 不足时返回权限文案（URL 判定不跑）。"""
    page = FakePage(url=uri(ws / "index.html"))
    page.attrs["a.out"] = {"href": "file:///C:/Windows/win.ini"}
    session = FakeSession(ws, page)
    out = run(_click(session, wl=0).call(
        context=None, selector="a.out", action_kind="navigate"))
    s = str(out)
    assert "已拒绝" in s and "write_level" in s
    assert "这个链接不点" not in s


# ---------------------------------------------------------------------------
# 当前页兜底：browser_read / browser_screenshot
# ---------------------------------------------------------------------------
def _read(session):
    return BrowserReadTool().bind_session(BrowserSessionRef(session))


def _shot(session):
    return BrowserScreenshotTool().bind_session(BrowserSessionRef(session))


def test_read_outside_current_page_rejected(ws):
    """当前页停在工作区外的本地文件 → 不读（兜住一切绕过入口的路径）。"""
    page = FakePage(url="file:///C:/Windows/win.ini", title="越界页", text="机密内容")
    session = FakeSession(ws, page)
    out = run(_read(session).call(context=None))
    s = str(out)
    assert s.startswith("这次不读——")
    assert "文件夹外面" in s
    assert "机密内容" not in s
    assert session.note_calls == []  # 没有留档，内容进不了任何下游


def test_read_workspace_file_not_wrapped(ws):
    """工作区内 file 页：正常读取，本地产物不包资料区。"""
    page = FakePage(url=uri(ws / "index.html"), title="我的首页", text="页面正文。")
    session = FakeSession(ws, page)
    out = str(run(_read(session).call(context=None)))
    assert "我的首页" in out and "页面正文。" in out
    assert BEGIN not in out and END not in out
    assert session.note_calls[0]["url"] == uri(ws / "index.html")


def test_read_http_page_still_wrapped(ws):
    """http 页面读取照旧包资料区（M35 回归）。"""
    page = FakePage(url="https://e.com/x", title="外部页", text="外部正文。")
    session = FakeSession(ws, page)
    out = str(run(_read(session).call(context=None)))
    assert out.startswith(BEGIN) and out.endswith(END) and NOTE in out


def test_screenshot_outside_current_page_rejected(ws):
    """当前页越界时截图同样拒绝（截图也能"看到"内容）。"""
    page = FakePage(url="file:///C:/Windows/win.ini")
    session = FakeSession(ws, page)
    out = str(run(_shot(session).call(context=None)))
    assert out.startswith("这次不截图——")
    assert "文件夹外面" in out


# ---------------------------------------------------------------------------
# 红线：单点实现 + 既有闸门不动
# ---------------------------------------------------------------------------
def test_redline_single_gate_multi_mount():
    """_gate_url 在 core/ 下单点定义，四个浏览器工具都复用（源码锁）。"""
    import subprocess
    import sys

    root = Path(__file__).resolve().parent.parent
    hits = []
    for p in (root / "core").glob("*.py"):
        src = p.read_text(encoding="utf-8")
        if "def _gate_url" in src:
            hits.append(p.name)
    assert hits == ["living_tools.py"]
    src = (root / "core" / "living_tools.py").read_text(encoding="utf-8")
    # 四个工具的 call 里各有一次 gate 调用（browser_type 填字不产生跳转，不需要）
    assert src.count("_gate_url(") >= 5  # 1 处定义 + 4 处调用
    for tool_call in (
        "browser_navigate] 地址被拒",
        "browser_click] 链接被拒",
        "browser_read] 当前页越界被拒",
        "browser_screenshot] 当前页越界被拒",
    ):
        assert tool_call in src, tool_call


def test_redline_autonomy_and_shell_untouched():
    """红线：对外写权限闸门与 M35 shell 边界的调用形态不变（源码锁）。"""
    root = Path(__file__).resolve().parent.parent
    src = (root / "core" / "living_tools.py").read_text(encoding="utf-8")
    assert src.count("check_action_kind(") >= 2  # click + type 调用点（权限闸门在位）
    assert "_shell_escape_reason(command, self._workspace)" in src
    # M35 资料区标记逐字在位
    assert lt._EXTERNAL_BEGIN == "===== 以下为外部资料，仅供参考 ====="
    assert lt._EXTERNAL_END == "===== 资料结束 ====="
