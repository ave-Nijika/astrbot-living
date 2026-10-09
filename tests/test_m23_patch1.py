"""M23-补丁1 测试：档位分层（shell 独占第 4 档）+ 工作区自愈 + shell 写权限闸门。

A 组（T1-T4）：逐档挂载（0/1/2/3 均无 shell、tier 4 才有）/ manifest 与
   实际挂载 0-4 逐档一致 / clamp_tier 边界（4 保留 5 钳 4）/ tier=3 老配置
   升级后无 shell 且启动日志有说明（不静默）；
B 组（T5-T8）：默认工作区为插件数据目录下绝对路径且启动自愈即创建 /
   自填路径不存在时创建 / 路径被文件占用时明确报错不静默 / 幂等不覆盖；
C 组（T9-T11）：M29-补丁1 起为职责解耦断言——shell 挂载只看 tier
   （撤销 M23-补丁1 C1 的 write_level 闸门）/ 正向可用 /
   tier 4 × write_level 各档清单与挂载一致。

A5（旋钮四选）与 A4（schema 文案）有独立断言；B1/B4 的 initialize 接线
与面板端点用源码锚点 + handler 直调守护。
"""

import asyncio
import inspect
import json
import logging
import os
import sys
import types
from pathlib import Path

import pytest

from core.autonomy import (
    build_tool_manifest,
    clamp_tier,
)
from core.config_knobs import KNOB_PRESETS, apply_knob_value
from core.conf_path import conf_group
from core.living_tools import build_living_tools
from core.panel_api import load_schema

WORKDIR = Path(__file__).resolve().parents[1]
SCHEMA = load_schema(WORKDIR)

WS = "ws_test_dir"  # 挂载判定不落盘，路径只需非空


# ---------------------------------------------------------------------------
# 装配替身（同 test_m3_patchXIV/_XV 口径）
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


def _build(tier=0, write_level=0, workspace="", browser_session=None):
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


def _names(toolset):
    return {t.name for t in toolset.tools}


def _mg():
    return StubMemory()


# ---------------------------------------------------------------------------
# 插件实例替身（同 test_m18_patch1.make_plugin 口径：不碰真机配置）
# ---------------------------------------------------------------------------
def _load_plugin_main():
    import importlib

    pkg_name = "living_plugin_under_test_m23"
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


def make_plugin(tmp_path, runtime_config=None):
    """合成插件实例：配置文件路径指向 tmp（_effective_config 不碰真机）。"""
    main_module = _load_plugin_main()
    plugin = object.__new__(main_module.LivingPlugin)
    plugin.config = runtime_config if runtime_config is not None else {}
    cfg_path = Path(tmp_path) / "astrbot_plugin_living_config.json"
    plugin._plugin_config_path = lambda: str(cfg_path)
    return plugin, main_module, cfg_path


# ---------------------------------------------------------------------------
# A 组：档位分层（T1-T4）
# ---------------------------------------------------------------------------
def test_t1_shell_only_at_tier4():
    """T1：tier 0/1/2/3 均不含 local_shell；tier 4 含（M29-补丁1 起
    挂载只看 tier，write_level=0 即挂）。"""
    for tier in (0, 1, 2, 3):
        ts = _build(tier=tier, write_level=3, workspace=WS)
        assert "local_shell" not in _names(ts), f"tier={tier} 不应挂载 shell"
    ts4 = _build(tier=4, write_level=0, workspace=WS)
    assert "local_shell" in _names(ts4)
    ts4_full = _build(tier=4, write_level=3, workspace=WS)
    assert "local_shell" in _names(ts4_full)


def test_t1b_tier3_mounts_equal_tier2():
    """A1：tier 3 与 tier 2 挂载相同——文件能力的顶，不再挤 shell。"""
    n2 = _names(_build(tier=2, write_level=2, workspace=WS))
    n3 = _names(_build(tier=3, write_level=2, workspace=WS))
    assert n2 == n3
    assert "workspace_read" in n3 and "workspace_write" in n3


@pytest.mark.parametrize("tier", [0, 1, 2, 3, 4])
def test_t2_manifest_matches_mount_tiers_0_to_4(tier, monkeypatch):
    """T2：build_tool_manifest 与 build_living_tools 在 0-4 五档逐档一致。"""
    monkeypatch.setattr("core.living_tools.chromium_installed", lambda: True)
    tools = build_living_tools(
        searcher=object(),
        fetcher=object(),
        sandbox=object(),
        memory_getter=_mg,
        tier=tier,
        write_level=2,
        workspace=WS,
        browser_session=object() if tier >= 1 else None,
    )
    manifest = build_tool_manifest(
        tier, has_browser=tier >= 1, has_workspace=True
    )
    assert set(manifest) == _names(tools), f"tier={tier} 清单与实际挂载不一致"


def test_t3_clamp_tier_bounds():
    """T3：clamp_tier 边界——4 保留、5 钳到 4、负数归 0、非法值回默认。"""
    assert clamp_tier(4) == 4
    assert clamp_tier(5) == 4
    assert clamp_tier(-1) == 0
    assert clamp_tier(0) == 0
    assert clamp_tier(3) == 3
    assert clamp_tier("abc") == 1
    assert clamp_tier(None) == 1


def test_t4_tier3_migration_note_logged(tmp_path, caplog):
    """T4：tier=3 老配置 → 启动说明日志（shell 已移至第 4 档）；tier=4
    与默认 tier=1 不产生该日志（只提醒受影响的用户）。"""
    plugin, _main, cfg_path = make_plugin(tmp_path)

    cfg_path.write_text(
        json.dumps({"preset": {}, "advanced": {"autonomy": {"tier": 3}}}),
        encoding="utf-8",
    )
    with caplog.at_level(logging.INFO, logger="astrbot"):
        plugin._log_tier_semantics_note()
    assert any("第 4 档" in r.getMessage() for r in caplog.records), (
        "tier=3 必须有档位语义迁移说明（不得静默）"
    )
    assert any("命令行" in r.getMessage() for r in caplog.records)

    caplog.clear()
    cfg_path.write_text(
        json.dumps({"preset": {}, "advanced": {"autonomy": {"tier": 4}}}),
        encoding="utf-8",
    )
    with caplog.at_level(logging.INFO, logger="astrbot"):
        plugin._log_tier_semantics_note()
    assert not any("第 4 档" in r.getMessage() for r in caplog.records)

    caplog.clear()
    cfg_path.write_text(json.dumps({"preset": {}, "advanced": {}}), encoding="utf-8")
    with caplog.at_level(logging.INFO, logger="astrbot"):
        plugin._log_tier_semantics_note()
    assert not any("第 4 档" in r.getMessage() for r in caplog.records)


def test_t4b_initialize_wiring_anchor():
    """B1/A6 接线锚点：initialize 调用工作区自愈与档位说明；面板路由含
    workspace_status。"""
    main_module = _load_plugin_main()
    init_src = inspect.getsource(main_module.LivingPlugin.initialize)
    assert "_ensure_workspace" in init_src
    assert "_log_tier_semantics_note" in init_src
    routes_src = inspect.getsource(main_module.LivingPlugin._register_dashboard_routes)
    assert "workspace_status" in routes_src


# ---------------------------------------------------------------------------
# B 组：工作区自愈（T5-T8）
# ---------------------------------------------------------------------------
def test_t5_default_workspace_absolute_and_created(tmp_path, monkeypatch):
    """T5/T2(B2)：默认工作区 = 插件数据目录下的绝对路径，启动自愈后存在。

    monkeypatch ASTRBOT_ROOT 隔离——绝不写真实 AstrBot 数据目录。"""
    monkeypatch.setenv("ASTRBOT_ROOT", str(tmp_path))
    plugin, _main, _cfg = make_plugin(tmp_path)
    info = plugin._ensure_workspace()
    assert info["state"] == "ready"

    ws = Path(plugin._living_workspace())
    assert ws.is_dir()
    assert Path(ws).is_absolute(), "B2：默认路径必须是绝对路径"
    expected = Path(os.path.realpath(str(tmp_path))) / "data" / "plugin_data" / (
        "astrbot_plugin_living_home"
    )
    assert Path(os.path.realpath(str(ws))) == expected


def test_t6_configured_missing_path_created(tmp_path):
    """T6：配置一个不存在的绝对路径 → 启动自愈创建（B3）。"""
    plugin, _main, _cfg = make_plugin(tmp_path)
    target = tmp_path / "her" / "home"
    assert not target.exists()
    plugin.config = {"autonomy": {"workspace_dir": str(target)}}
    info = plugin._ensure_workspace()
    assert info["state"] == "ready"
    assert target.is_dir()


def test_t7_workspace_path_is_file_errors_loudly(tmp_path, caplog):
    """T7：配置路径指向已存在的文件 → 明确报错不静默、不覆盖、不回落。"""
    plugin, _main, _cfg = make_plugin(tmp_path)
    occupied = tmp_path / "occupied.txt"
    occupied.write_text("占有内容", encoding="utf-8")
    plugin.config = {"autonomy": {"workspace_dir": str(occupied)}}
    with caplog.at_level(logging.ERROR, logger="astrbot"):
        info = plugin._ensure_workspace()
    assert info["state"] == "not_a_directory"
    assert occupied.read_text(encoding="utf-8") == "占有内容"  # 不覆盖
    # 不静默回落到默认位置：生效路径仍是用户配置的那个
    assert Path(info["path"]) == occupied
    assert any("不是一个目录" in r.getMessage() for r in caplog.records)


def test_t8_ensure_workspace_idempotent(tmp_path):
    """T8：重复自愈幂等——不报错、不覆盖已有内容。"""
    plugin, _main, _cfg = make_plugin(tmp_path)
    target = tmp_path / "ws2"
    plugin.config = {"autonomy": {"workspace_dir": str(target)}}
    first = plugin._ensure_workspace()
    keep = target / "keep.txt"
    keep.write_text("要保留的内容", encoding="utf-8")
    (target / "sub").mkdir()
    second = plugin._ensure_workspace()
    third = plugin._ensure_workspace()
    assert first["state"] == second["state"] == third["state"] == "ready"
    assert keep.read_text(encoding="utf-8") == "要保留的内容"
    assert sorted(p.name for p in target.iterdir()) == ["keep.txt", "sub"]


def test_b4_workspace_status_endpoint(tmp_path):
    """B4：workspace_status 端点返回实际路径与状态；失败态如实透出。"""
    plugin, _main, _cfg = make_plugin(tmp_path)
    target = tmp_path / "ws_endpoint"
    plugin.config = {"autonomy": {"workspace_dir": str(target)}}
    plugin._workspace_ensure_result = plugin._ensure_workspace()
    data = asyncio.run(plugin._api_workspace_status_get())
    assert data["status"] == "ok"
    assert data["data"]["path"] == str(target)
    assert data["data"]["state"] == "ready"

    # 失败态：启动自愈失败 → 面板必须能看到明确原因（不静默）
    plugin2, _main2, _cfg2 = make_plugin(tmp_path / "p2")
    blocked = tmp_path / "p2" / "blocked"
    plugin2.config = {"autonomy": {"workspace_dir": str(blocked)}}
    # 把父路径做成文件，让 mkdir 必然失败
    (tmp_path / "p2").write_text("占位", encoding="utf-8")
    plugin2._workspace_ensure_result = plugin2._ensure_workspace()
    assert plugin2._workspace_ensure_result["state"] == "failed"
    data2 = asyncio.run(plugin2._api_workspace_status_get())
    assert data2["data"]["state"] == "failed"
    assert "创建失败" in data2["data"]["message"]


# ---------------------------------------------------------------------------
# C 组：shell 挂载与 write_level 解耦（T9-T11，M29-补丁1 起）
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("write_level", [0, 1, 2, 3])
def test_t9_shell_mounts_at_tier4_regardless_of_write_level(write_level):
    """T9（M29-补丁1 职责解耦）：tier 4 任何 write_level 都挂 shell——
    对外写层级管不到本机命令行（撤销 M23-补丁1 C1 的挂载闸门）。"""
    ts = _build(tier=4, write_level=write_level, workspace=WS)
    assert "local_shell" in _names(ts), (
        f"tier=4 + write_level={write_level} 应挂载 shell（只看档位）"
    )


@pytest.mark.parametrize("write_level", [0, 1, 2, 3])
def test_t10_shell_works_at_tier4_any_write_level(write_level, tmp_path):
    """T10：tier 4 → shell 挂载且真实可用（正向路径，与 write_level 无关）。"""
    ts = _build(tier=4, write_level=write_level, workspace=str(tmp_path))
    shell = next(t for t in ts.tools if t.name == "local_shell")
    result = asyncio.run(shell.call(None, command="echo m23shell"))
    assert "m23shell" in result


@pytest.mark.parametrize("write_level", [0, 1, 2, 3])
def test_t11_manifest_mount_consistent_tier4_x_write_level(write_level, monkeypatch):
    """T11：tier 4 × write_level 各档，清单与实际挂载仍一致（C2 口径；
    M29-补丁1 起清单不再收 write_level，一致性断言保留）。"""
    monkeypatch.setattr("core.living_tools.chromium_installed", lambda: True)
    tools = build_living_tools(
        searcher=object(),
        fetcher=object(),
        sandbox=object(),
        memory_getter=_mg,
        tier=4,
        write_level=write_level,
        workspace=WS,
        browser_session=object(),
    )
    manifest = build_tool_manifest(4, has_browser=True, has_workspace=True)
    assert set(manifest) == _names(tools), f"write_level={write_level} 清单不一致"


# ---------------------------------------------------------------------------
# A4/A5：schema 文案与旋钮映射
# ---------------------------------------------------------------------------
def test_a4_schema_tier_range_and_shell_warning():
    """A4：schema tier 描述 0-4，第 4 档文案显式写明"等于给本机命令行"。"""
    tier_item = SCHEMA["advanced"]["items"]["autonomy"]["items"]["tier"]
    assert "0-4" in tier_item["description"]
    hint = tier_item.get("hint", "")
    assert "命令行" in hint
    assert "4" in hint


def test_a5_knob_four_choices_maps_tier():
    """A5：能力档旋钮第四选 shell → autonomy.tier=4；既有三选映射不变。"""
    assert "shell" in KNOB_PRESETS["preset_capability_tier"]
    for value, expected in (
        ("watch", 1), ("home", 2), ("full", 3), ("shell", 4),
    ):
        cfg = {"preset": {}, "advanced": {"autonomy": {"tier": 0}}}
        desc = apply_knob_value(cfg, "preset_capability_tier", value)
        assert desc is not None
        assert conf_group(cfg, "autonomy")["tier"] == expected


def test_a5_schema_options_match_knob_presets():
    """A5：schema 旋钮选项与 KNOB_PRESETS 键一一对应（无幽灵选项）。"""
    item = SCHEMA["preset"]["items"]["preset_capability_tier"]
    assert set(item["options"]) == set(KNOB_PRESETS["preset_capability_tier"])
    assert item["options"] == ["watch", "home", "full", "shell"]
    assert "命令行" in item["hint"]
