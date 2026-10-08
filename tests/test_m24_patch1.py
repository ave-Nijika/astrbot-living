"""M24-补丁1 测试：浏览器探测事件循环安全化（chromium_installed）。

验收对照（任务书 三、验收要求）：
1  on-loop 返回 True（核心——旧代码在该场景必抛 "Sync API inside the
   asyncio loop"，被吞后恒判"未安装"，浏览器五件套从不挂载）
2  off-loop 行为不变（探测留在调用线程直接跑）
3  playwright 缺失 → False（最短路保持）
4  探测抛异常 → False（fail-closed 不外抛；异常结果不入缓存，下一轮重试）
5  缓存生效：TTL 内命中不重复探测 / force=True 绕过并回写 / 过期重探
6  挂载端 fail-closed 未回退（living_tools 引用同一函数对象；False/None
   一件不挂、True 五件全挂——沿用 m15p2 断言强度，适配新签名的替身）
7  配套 a：面板端点 force=True（运行时记录 + 源码锚点 + 异常分支保留）
   配套 b：启动预热（正常回写缓存且首个活动命中 / 预热异常不阻断 +
   initialize 接线锚点）

探测环境说明：测试 venv 未装 playwright（HAS_PLAYWRIGHT=False），因此
验收 1/2/5/7b 一律 monkeypatch HAS_PLAYWRIGHT(_SYNC)=True + 假
sync_playwright——假实现记录调用次数与探测所在线程，用于断言
"在环时探测发生在子线程"这一核心行为。
"""

import asyncio
import inspect
import logging
import sys
import threading
import time
import types
from pathlib import Path

import pytest

import core.browser_tools as bt
from core.living_tools import build_living_tools

WORKDIR = Path(__file__).resolve().parents[1]

# 同 test_m15_patch2 口径：浏览器五件套名单
BROWSER_FIVE = [
    "browser_navigate",
    "browser_read",
    "browser_screenshot",
    "browser_click",
    "browser_type",
]


# ---------------------------------------------------------------------------
# 替身：假 sync_playwright（记录调用次数与探测所在线程）
# ---------------------------------------------------------------------------
class _FakeChromium:
    def __init__(self, exe_path):
        self.executable_path = exe_path


class _FakePlaywright:
    def __init__(self, exe_path):
        self.chromium = _FakeChromium(exe_path)


class _FakeSyncPlaywright:
    """替身 sync_playwright：可调用、可作上下文管理器，__enter__ 时记录
    所在线程（验收 1 断言"在环调用时探测发生在子线程"的证据）。"""

    def __init__(self, exe_path, calls, thread_ids, enter_exc=None):
        self._exe_path = exe_path
        self.calls = calls
        self.thread_ids = thread_ids
        self._enter_exc = enter_exc

    def __call__(self):
        self.calls.append(1)
        return self

    def __enter__(self):
        self.thread_ids.append(threading.get_ident())
        if self._enter_exc is not None:
            raise self._enter_exc
        return _FakePlaywright(self._exe_path)

    def __exit__(self, *exc):
        return False


def _install_fake(monkeypatch, tmp_path, name="chrome.exe", enter_exc=None):
    """装好"playwright 已安装 + 假探测"环境，返回 (calls, thread_ids)。"""
    exe = tmp_path / name
    exe.write_bytes(b"MZ fake binary")  # 必须真实存在（探测查文件存在性）
    calls, thread_ids = [], []
    monkeypatch.setattr(bt, "HAS_PLAYWRIGHT", True)
    monkeypatch.setattr(bt, "HAS_PLAYWRIGHT_SYNC", True)
    monkeypatch.setattr(
        bt,
        "sync_playwright",
        _FakeSyncPlaywright(str(exe), calls, thread_ids, enter_exc=enter_exc),
    )
    return calls, thread_ids


@pytest.fixture(autouse=True)
def _fresh_probe_cache():
    """每个测试独享干净缓存——模块级缓存不跨测试泄漏。"""
    bt._chromium_probe_cache.clear()
    yield
    bt._chromium_probe_cache.clear()


# ---------------------------------------------------------------------------
# 装配替身（同 test_m23_patch1 口径）
# ---------------------------------------------------------------------------
class StubSearcher:
    async def search(self, q, count=5):
        return []


class StubFetcher:
    async def fetch(self, url):
        return {"title": "", "text": ""}


class StubSandbox:
    async def run(self, code, timeout=10):
        return {"stdout": "", "stderr": "", "exit_code": 0}


class StubMemory:
    async def add(self, c, importance=0.5, metadata=None, **kw):
        return 1


def _build(tier=0, write_level=0, workspace="ws_test_dir", browser_session=None):
    return build_living_tools(
        searcher=StubSearcher(),
        fetcher=StubFetcher(),
        sandbox=StubSandbox(),
        memory_getter=lambda: asyncio.sleep(0, result=StubMemory()),
        tier=tier,
        write_level=write_level,
        workspace=workspace,
        browser_session=browser_session,
    )


def _browser_names(toolset):
    return {t.name for t in toolset.tools if t.name in BROWSER_FIVE}


# ---------------------------------------------------------------------------
# 插件实例替身（同 test_m23_patch1.make_plugin 口径：不碰真机配置）
# ---------------------------------------------------------------------------
def _load_plugin_main():
    import importlib

    pkg_name = "living_plugin_under_test_m24"
    if pkg_name not in sys.modules:
        pkg = types.ModuleType(pkg_name)
        pkg.__path__ = [str(WORKDIR)]
        sys.modules[pkg_name] = pkg
        import core as core_pkg

        sys.modules[f"{pkg_name}.core"] = core_pkg
        for name, mod in list(sys.modules.items()):
            if name == "core" or name.startswith("core."):
                sys.modules.setdefault(f"{pkg_name}.{name}", mod)
    return importlib.import_module(f"{pkg_name}.main")


def make_plugin(tmp_path):
    main_module = _load_plugin_main()
    plugin = object.__new__(main_module.LivingPlugin)
    plugin.config = {}
    cfg_path = Path(tmp_path) / "astrbot_plugin_living_config.json"
    plugin._plugin_config_path = lambda: str(cfg_path)
    return plugin, main_module


# ---------------------------------------------------------------------------
# 验收 1：on-loop 返回 True（核心——旧版在此场景恒 False）
# ---------------------------------------------------------------------------
def test_acc1_on_loop_force_returns_true(tmp_path, monkeypatch):
    """在 asyncio.run 的协程里调 chromium_installed(force=True)：不抛
    "Sync API inside the asyncio loop"，Chromium 已装（假实现指向真实
    存在的文件）时返回 True。"""
    calls, thread_ids = _install_fake(monkeypatch, tmp_path)

    async def in_loop():
        return bt.chromium_installed(force=True)

    assert asyncio.run(in_loop()) is True
    assert len(calls) == 1, "必须真的跑过探测体"
    assert thread_ids and thread_ids[0] != threading.get_ident(), (
        "在环调用时探测必须发生在子线程（同步 API 在环内线程会直接抛错）"
    )


def test_acc1b_on_loop_default_call_returns_true(tmp_path, monkeypatch):
    """不带 force 的在环调用（等价挂载点 core/living_tools.py:295 的
    调用形态，冷缓存）同样返回 True——这是五件套能否挂上的正主场景。"""
    calls, _tids = _install_fake(monkeypatch, tmp_path)

    async def in_loop():
        return bt.chromium_installed()

    assert asyncio.run(in_loop()) is True
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# 验收 2：off-loop 行为不变
# ---------------------------------------------------------------------------
def test_acc2_off_loop_unchanged(tmp_path, monkeypatch):
    """无事件循环的线程里直调：返回 True 且探测留在调用线程（与旧版
    行为一致，不做线程跳转）。"""
    calls, thread_ids = _install_fake(monkeypatch, tmp_path)

    assert bt.chromium_installed(force=True) is True
    assert len(calls) == 1
    assert thread_ids[0] == threading.get_ident(), (
        "离环调用必须留在调用线程（行为与旧版一致）"
    )


def test_acc2b_probe_blocking_false_when_exe_missing(tmp_path, monkeypatch):
    """探测体本体：可执行文件不存在 → 探测成功返回 False（不是异常）。"""
    _install_fake(monkeypatch, tmp_path, name="not_installed.exe")
    # 让假实现的路径指向不存在的文件
    monkeypatch.setattr(
        bt,
        "sync_playwright",
        _FakeSyncPlaywright(str(tmp_path / "missing_dir" / "nope.exe"), [], []),
    )
    assert bt._probe_blocking() is False


# ---------------------------------------------------------------------------
# 验收 3：playwright 缺失 → False
# ---------------------------------------------------------------------------
def test_acc3_no_playwright_false(monkeypatch):
    """HAS_PLAYWRIGHT=False（venv 未装 playwright 的真实形态）→ 一律
    False，最短路保持、不起任何子进程。"""
    monkeypatch.setattr(bt, "HAS_PLAYWRIGHT", False)
    monkeypatch.setattr(bt, "HAS_PLAYWRIGHT_SYNC", False)
    assert bt.chromium_installed() is False
    assert bt.chromium_installed(force=True) is False


# ---------------------------------------------------------------------------
# 验收 4：探测抛异常 → False（fail-closed）
# ---------------------------------------------------------------------------
def test_acc4_probe_exception_fail_closed_off_loop(tmp_path, monkeypatch):
    """探测体抛异常（离环）：返回 False 不外抛；异常结果不入缓存——
    下一轮调用重新探测（与旧版每轮重试的行为一致）。"""
    calls, _tids = _install_fake(
        monkeypatch, tmp_path, enter_exc=RuntimeError("driver 起不来")
    )

    assert bt.chromium_installed(force=True) is False
    assert bt.chromium_installed() is False  # 不外抛，依旧 False
    assert len(calls) == 2, "异常结果不得入缓存（瞬时故障下一轮要能重试）"
    assert "value" not in bt._chromium_probe_cache


def test_acc4b_probe_exception_fail_closed_in_loop(tmp_path, monkeypatch):
    """探测体抛异常（在环 → 子线程跑）：同样 False 不外抛。"""
    _install_fake(monkeypatch, tmp_path, enter_exc=RuntimeError("driver 起不来"))

    async def in_loop():
        return bt.chromium_installed(force=True)

    assert asyncio.run(in_loop()) is False


# ---------------------------------------------------------------------------
# 验收 5：缓存生效
# ---------------------------------------------------------------------------
def test_acc5_cache_ttl_hit_force_bypass_and_expiry(tmp_path, monkeypatch):
    """TTL 内连续调用只探测一次；force=True 绕过并回写；回写后普通调用
    继续命中；时间戳退到 TTL 外 → 重探。"""
    calls, _tids = _install_fake(monkeypatch, tmp_path)

    assert bt.chromium_installed() is True  # 冷缓存 → 探测（1）
    assert bt.chromium_installed() is True  # TTL 内命中（仍 1）
    assert bt.chromium_installed(force=False) is True  # 仍命中（仍 1）
    assert len(calls) == 1, "TTL 内不得重复起探测"

    assert bt.chromium_installed(force=True) is True  # 绕过缓存 → 探测（2）
    assert bt.chromium_installed() is True  # force 结果已回写（仍 2）
    assert len(calls) == 2, "force=True 必须绕过缓存重探，且结果回写"

    bt._chromium_probe_cache["at"] -= bt.CHROMIUM_PROBE_TTL_SECONDS + 1.0
    assert bt.chromium_installed() is True  # 过期 → 重探（3）
    assert len(calls) == 3, "TTL 过期后必须重新探测"


def test_acc5b_cached_false_short_circuits_probe(tmp_path, monkeypatch):
    """探测成功返回 False（真没装）同样入缓存——TTL 内不再重复起
    driver 子进程（这正是"未装 Chromium 的机器每次活动都白探测一次"
    的止血）。"""
    exe_missing = str(tmp_path / "no_such_dir" / "chrome.exe")
    calls, _tids = _install_fake(monkeypatch, tmp_path)
    monkeypatch.setattr(
        bt, "sync_playwright", _FakeSyncPlaywright(exe_missing, calls, [])
    )

    assert bt.chromium_installed() is False  # 探测（1）
    assert bt.chromium_installed() is False  # 缓存命中（仍 1）
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# 验收 6：挂载端 fail-closed 未回退
# ---------------------------------------------------------------------------
def test_acc6a_living_tools_binds_same_function_object():
    """调用链口径：core/living_tools 挂载判定引用的就是 core.browser_tools
    的同一函数对象——browser_tools 里的修复自动作用于挂载点，无需在
    living_tools 重复改（也杜绝两处口径分叉）。"""
    import core.living_tools as lt

    assert lt.chromium_installed is bt.chromium_installed


@pytest.mark.parametrize("ret", [False, None])
def test_acc6b_mount_fail_closed_false_none(ret, monkeypatch):
    """chromium_installed 返回 False/None → 五件套一件不挂（替身适配
    新签名 lambda force=False: ...，同时守护调用点不传意外实参）。"""
    monkeypatch.setattr("core.living_tools.chromium_installed", lambda force=False: ret)
    ts = _build(tier=1, write_level=2, browser_session=object())
    assert _browser_names(ts) == set()


def test_acc6c_mount_true_all_five(monkeypatch):
    """返回 True → 五件套全挂（断言强度与 test_m15_patch2 持平）。"""
    monkeypatch.setattr("core.living_tools.chromium_installed", lambda force=False: True)
    ts = _build(tier=1, write_level=2, browser_session=object())
    assert _browser_names(ts) == set(BROWSER_FIVE)


def test_acc6d_real_probe_no_playwright_mounts_nothing():
    """真实探测路径（venv 无 playwright → False）× 挂载端 is not True
    判定：一件不挂——最短路贯通无回归。"""
    ts = _build(tier=1, write_level=2, browser_session=object())
    assert _browser_names(ts) == set()


# ---------------------------------------------------------------------------
# 验收 7a：面板端点 force=True（配套 a）
# ---------------------------------------------------------------------------
def test_acc7a_panel_endpoint_forces_realtime_probe(tmp_path, monkeypatch):
    """面板端点必须以 force=True 调探测（每次打开都是实况，不受缓存
    影响）；运行时记录实参作为证据，非仅源码锚点。"""
    plugin, _main = make_plugin(tmp_path)
    seen = []

    def fake_probe(force=False):
        seen.append(force)
        return True

    monkeypatch.setattr(bt, "chromium_installed", fake_probe)
    data = asyncio.run(plugin._api_browser_status_get())
    assert data == {"status": "ok", "data": {"installed": True}}
    assert seen == [True], "面板端点必须 force=True（面板每次打开要实况）"


def test_acc7a2_panel_endpoint_exception_branch(tmp_path, monkeypatch):
    """配套 a：异常 → {"installed": False} 分支保留（fail-closed 反馈）。"""
    plugin, _main = make_plugin(tmp_path)

    def boom(force=False):
        raise RuntimeError("探测端点炸了")

    monkeypatch.setattr(bt, "chromium_installed", boom)
    data = asyncio.run(plugin._api_browser_status_get())
    assert data["status"] == "ok"
    assert data["data"]["installed"] is False


def test_acc7a3_panel_endpoint_source_anchor():
    """配套 a 源码锚点：端点调用带 force=True（to_thread 第二实参）。"""
    main_module = _load_plugin_main()
    src = inspect.getsource(main_module.LivingPlugin._api_browser_status_get)
    assert "to_thread(chromium_installed, True)" in src


# ---------------------------------------------------------------------------
# 验收 7b：启动预热（配套 b）
# ---------------------------------------------------------------------------
def test_acc7b_prewarm_fills_cache_and_first_activity_hits_it(
    tmp_path, monkeypatch
):
    """预热（离环 to_thread + force=True）回写缓存；随后在环的挂载形态
    调用直接命中缓存，不再重复探测。"""
    plugin, _main = make_plugin(tmp_path)
    calls, thread_ids = _install_fake(monkeypatch, tmp_path)

    asyncio.run(plugin._prewarm_chromium_probe())

    assert bt._chromium_probe_cache.get("value") is True, "预热必须回写缓存"
    assert bt._chromium_probe_cache.get("at", 0.0) > 0

    async def mount_shape():
        return bt.chromium_installed()

    assert asyncio.run(mount_shape()) is True
    assert len(calls) == 1, "预热后首个活动应命中缓存，不得重复探测"
    assert thread_ids and thread_ids[0] != threading.get_ident(), (
        "预热探测必须离环（to_thread 子线程）执行"
    )


def test_acc7b2_prewarm_failure_does_not_block(tmp_path, monkeypatch, caplog):
    """预热抛异常：只记 WARNING，不向 initialize 传异常（绝不阻断启动）。"""
    plugin, _main = make_plugin(tmp_path)

    def boom(force=False):
        raise RuntimeError("预热炸了")

    monkeypatch.setattr(bt, "chromium_installed", boom)
    with caplog.at_level(logging.WARNING, logger="astrbot"):
        asyncio.run(plugin._prewarm_chromium_probe())  # 不抛即通过
    assert any("预热" in r.getMessage() for r in caplog.records), (
        "预热失败必须留日志说明（不得静默）"
    )
    assert "value" not in bt._chromium_probe_cache


def test_acc7b3_initialize_wiring_anchor():
    """配套 b 接线锚点：initialize 调用 _prewarm_chromium_probe（非死代码）。"""
    main_module = _load_plugin_main()
    src = inspect.getsource(main_module.LivingPlugin.initialize)
    assert "_prewarm_chromium_probe" in src


def test_acc7b4_prewarm_helper_source_anchor():
    """预热 helper 本体：离环 to_thread + force=True（与任务书 2.2b 同款）。"""
    main_module = _load_plugin_main()
    src = inspect.getsource(main_module.LivingPlugin._prewarm_chromium_probe)
    assert "to_thread(chromium_installed, True)" in src
