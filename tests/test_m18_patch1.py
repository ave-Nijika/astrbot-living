"""M18-补丁1 测试：配置链路三处修复。

A 组：/living config 写到运行时真正读取的位置（advanced.<group>.<key>，
     与面板 apply_panel_save 同源）+ 回读验证 + 白名单修正（T1-T4）；
B 组：面板恢复默认值后，旋钮监视器不再把非默认档位映射写回——
     稳态 == schema 默认（T5-T6，T5 修复前必须红）；
C 组：面板 GET 与 _build_agent_tools 统一运行时同源 _effective_config
     ——手改 JSON 后面板显示真实值，保存后 GET 立即见新值（T7-T8）。
"""

import asyncio
import inspect
import json
import sys
import types
from pathlib import Path

from core.autonomy import read_tier, read_write_level
from core.config_knobs import ConfigKnobs, apply_knob_value
from core.conf_path import conf_group
from core.panel_api import apply_panel_save, default_tree, load_schema

WORKDIR = Path(__file__).resolve().parents[1]
SCHEMA = load_schema(WORKDIR)


def _load_plugin_main():
    import importlib

    pkg_name = "living_plugin_under_test"
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


class DiskConfig(dict):
    """带 save_config 的内存配置：落盘写 JSON——与生产链路同语义
    （面板/命令保存 → self.config 变更 → save_config 写盘 → 运行时
    _effective_config 从盘上读）。"""

    def __init__(self, seed, path):
        super().__init__(seed)
        self.path = Path(path)

    def save_config(self):
        self.path.write_text(
            json.dumps(self, ensure_ascii=False), encoding="utf-8"
        )


class FailingDiskConfig(DiskConfig):
    """落盘必失败（T4：写失败路径不得谎称生效）。"""

    def save_config(self):
        raise OSError("权限不足：配置文件只读")


# ---------------------------------------------------------------------------
# A 组：/living config 命令（T1-T4）
# ---------------------------------------------------------------------------
def test_t1_command_write_lands_in_advanced_readback_ok(tmp_path):
    """T1 端到端：命令写 decision.daily_impulse_limit → 落盘 advanced 嵌套
    → 运行时同源 _effective_config() 回读到新值（不是只断言写了 dict）。"""
    plugin, _main, cfg_path = make_plugin(tmp_path)
    seed = {"preset": {}, "advanced": {"decision": {"daily_impulse_limit": 3}}}
    cfg_path.write_text(json.dumps(seed), encoding="utf-8")
    plugin.config = DiskConfig(seed, cfg_path)

    lines = asyncio.run(
        plugin._living_config_lines("decision.daily_impulse_limit", "5")
    )
    assert any("已设置" in ln for ln in lines), lines
    assert any("已生效" in ln for ln in lines), lines
    assert not any("未生效" in ln for ln in lines), lines

    # 运行时同源回读（磁盘直读 + schema 缺键补默认）
    eff = plugin._effective_config()
    assert conf_group(eff, "decision")["daily_impulse_limit"] == 5

    # 磁盘上落在 advanced 嵌套里；顶层无同名孤儿键
    disk = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert disk["advanced"]["decision"]["daily_impulse_limit"] == 5
    assert "decision" not in disk


def test_t2_command_write_autonomy_tier_changes_read_tier(tmp_path):
    """T2：命令写 autonomy.tier → read_tier(运行时同源配置) 返回新值
    （工具构建按此挂载档位），write_level 不受牵连。"""
    plugin, _main, cfg_path = make_plugin(tmp_path)
    seed = {"preset": {}, "advanced": {"autonomy": {"tier": 1, "write_level": 0}}}
    cfg_path.write_text(json.dumps(seed), encoding="utf-8")
    plugin.config = DiskConfig(seed, cfg_path)

    lines = asyncio.run(plugin._living_config_lines("autonomy.tier", "3"))
    assert any("已生效" in ln for ln in lines), lines

    eff = plugin._effective_config()
    assert read_tier(eff) == 3
    assert read_write_level(eff) == 0


def test_t3_rejects_unknown_group_key_and_illegal_values(tmp_path):
    """T3：非法组/未知键/非法值/越界 tier → 明确拒绝，不写脏数据。"""
    plugin, _main, cfg_path = make_plugin(tmp_path)
    seed = {"preset": {}, "advanced": {"decision": {"daily_impulse_limit": 3}}}
    cfg_path.write_text(json.dumps(seed), encoding="utf-8")
    plugin.config = DiskConfig(seed, cfg_path)

    # 白名单里已无 persona（schema 不存在的组）
    lines = asyncio.run(plugin._living_config_lines("persona.greeting", "hi"))
    assert any("不允许" in ln for ln in lines), lines
    # schema 里不存在的键
    lines = asyncio.run(plugin._living_config_lines("decision.nonexistent", "1"))
    assert any("未知配置键" in ln for ln in lines), lines
    # int 键收到非数字
    lines = asyncio.run(
        plugin._living_config_lines("decision.daily_impulse_limit", "abc")
    )
    assert any("整数" in ln for ln in lines), lines
    # autonomy.tier 越界 → 给合法值提示（0-3）
    lines = asyncio.run(plugin._living_config_lines("autonomy.tier", "9"))
    assert any("0-3" in ln for ln in lines), lines

    # 全部拒绝路径都不得落脏数据
    disk = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert disk["advanced"]["decision"]["daily_impulse_limit"] == 3
    assert "persona" not in disk
    assert "autonomy" not in disk["advanced"]


def test_t4_failed_persist_reports_not_applied(tmp_path):
    """T4：落盘失败 → 运行时读的还是旧值 → 回复必须如实说未生效。"""
    plugin, _main, cfg_path = make_plugin(tmp_path)
    seed = {"preset": {}, "advanced": {"decision": {"daily_impulse_limit": 3}}}
    cfg_path.write_text(json.dumps(seed), encoding="utf-8")
    plugin.config = FailingDiskConfig(seed, cfg_path)

    lines = asyncio.run(
        plugin._living_config_lines("decision.daily_impulse_limit", "5")
    )
    assert not any("已生效" in ln for ln in lines), lines
    assert any("未生效" in ln for ln in lines), lines
    # 磁盘上仍是旧值（内存变了也没用——运行时读磁盘）
    disk = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert disk["advanced"]["decision"]["daily_impulse_limit"] == 3


# ---------------------------------------------------------------------------
# B 组：恢复默认值 vs 旋钮监视器（T5-T6）
# ---------------------------------------------------------------------------
def _knob_plugin(tmp_path):
    """从 schema 默认树起步的配置 + 旋钮监视器（基线落 tmp）。"""
    plugin, main_module, cfg_path = make_plugin(tmp_path)
    config = default_tree(SCHEMA)
    plugin.config = config
    plugin._panel_schema = lambda: SCHEMA
    plugin._knobs = ConfigKnobs(
        lambda: plugin.config,
        save_config=None,
        state_path=Path(tmp_path) / "knobs_state.json",
    )
    return plugin, config, cfg_path


def test_t5_reset_stays_default_after_monitor_cycle(tmp_path):
    """T5 复现式回归（修复前必须红）：档位设为 active → 面板恢复默认 →
    过一个监视周期 → 所有键仍是 schema 默认。

    修复前：监视器持旧基线（active），把"回到默认"当成"用户改了档位"，
    按映射把 impulse_check_interval_minutes 写回 45（normal 档）、
    daily_impulse_limit 写回 0（均 ≠ schema 默认 5 / 3）→ 本测试红。"""
    plugin, config, _cfg = _knob_plugin(tmp_path)
    config["preset"]["preset_activity_level"] = "active"
    apply_knob_value(config, "preset_activity_level", "active")
    assert config["advanced"]["decision"]["impulse_check_interval_minutes"] == 20

    plugin._knobs.arm()  # 基线 = active 档

    asyncio.run(plugin._api_config_reset())  # 面板恢复默认（真实 handler）

    # 过一个监视周期（直接调监视器一次，等价 5s tick）
    asyncio.run(plugin._knobs.apply_changes())

    defaults = default_tree(SCHEMA)
    assert config["advanced"] == defaults["advanced"]
    assert config["preset"] == defaults["preset"]
    # 点名任务书复现键：默认 5 / 3，不是 normal 档映射的 45 / 0
    decision = config["advanced"]["decision"]
    assert decision["impulse_check_interval_minutes"] == 5
    assert decision["daily_impulse_limit"] == 3


def test_t6_knob_mapping_still_applies_after_reset(tmp_path):
    """T6：reset 后再改档位 → 映射正常生效（B1 不得把监视器弄哑）。"""
    plugin, config, _cfg = _knob_plugin(tmp_path)
    plugin._knobs.arm()
    asyncio.run(plugin._api_config_reset())

    config["preset"]["preset_activity_level"] = "active"
    applied = asyncio.run(plugin._knobs.apply_changes())
    assert applied, "旋钮监视器应在 reset 后照常工作"
    assert config["advanced"]["decision"]["impulse_check_interval_minutes"] == 20
    assert config["advanced"]["decision"]["activity_probability"] == 1.0


def test_t5b_reset_handler_resets_baseline_to_defaults(tmp_path):
    """B1 机制断言：reset 后旋钮基线 == 默认旋钮值（盘上基线同步归位）。"""
    plugin, config, _cfg = _knob_plugin(tmp_path)
    config["preset"]["preset_write_level"] = "comment"
    apply_knob_value(config, "preset_write_level", "comment")
    plugin._knobs.arm()
    assert plugin._knobs._last_knobs["preset_write_level"] == "comment"

    asyncio.run(plugin._api_config_reset())

    assert plugin._knobs._last_knobs == ConfigKnobs.snapshot_knobs(config)
    stored = json.loads(
        (Path(tmp_path) / "knobs_state.json").read_text(encoding="utf-8")
    )
    assert stored["last_knobs"] == ConfigKnobs.snapshot_knobs(config)


# ---------------------------------------------------------------------------
# C 组：面板 GET / agent 工具构建统一运行时同源（T7-T8）
# ---------------------------------------------------------------------------
def test_t7_panel_get_reflects_manual_disk_edit(tmp_path):
    """T7：磁盘直接改值（不经面板）→ 面板 GET 返回新值。"""
    plugin, _main, cfg_path = make_plugin(tmp_path)
    seed = {"preset": {}, "advanced": {"decision": {"daily_impulse_limit": 3}}}
    cfg_path.write_text(json.dumps(seed), encoding="utf-8")

    got = asyncio.run(plugin._api_config_get())
    assert got["status"] == "ok"
    assert got["data"]["advanced"]["decision"]["daily_impulse_limit"] == 3

    # 手改 JSON（运行时早已用新值，面板此前显示旧值——反向假象）
    seed["advanced"]["decision"]["daily_impulse_limit"] = 7
    cfg_path.write_text(json.dumps(seed), encoding="utf-8")
    got = asyncio.run(plugin._api_config_get())
    assert got["data"]["advanced"]["decision"]["daily_impulse_limit"] == 7


def test_t8_panel_save_then_get_returns_new_value_immediately(tmp_path):
    """T8 回归：面板保存 → 立即 GET 能看到新值（C1 不得破坏正常保存路径）。"""
    plugin, main_module, cfg_path = make_plugin(tmp_path)
    seed = {"preset": {}, "advanced": {"decision": {"daily_impulse_limit": 3}}}
    cfg_path.write_text(json.dumps(seed), encoding="utf-8")
    plugin.config = DiskConfig(seed, cfg_path)

    saved_payload = {"advanced": {"decision": {"daily_impulse_limit": 42}}}

    async def flow():
        from astrbot.api import web as astrbot_web

        class FakeRequest:
            async def json(self, default=None):
                return saved_payload

        original = astrbot_web.request
        astrbot_web.request = FakeRequest()
        try:
            return await plugin._api_config_post()
        finally:
            astrbot_web.request = original

    posted = asyncio.run(flow())
    assert posted["status"] == "ok", posted

    got = asyncio.run(plugin._api_config_get())
    assert got["data"]["advanced"]["decision"]["daily_impulse_limit"] == 42


def test_c2_agent_tools_tier_comes_from_effective_config(tmp_path):
    """C2 行为：_build_agent_tools_async 按磁盘上的 autonomy.tier 挂载
    （此前用 self.config——面板保存后不同步的旧值）。tier=3 → local_shell
    挂载；磁盘没写的 tier 回落默认 1 → 不挂。"""
    plugin, _main, cfg_path = make_plugin(tmp_path)
    plugin.searcher = types.SimpleNamespace(close=lambda: asyncio.sleep(0))
    plugin.fetcher = types.SimpleNamespace()
    plugin.sandbox = types.SimpleNamespace()
    plugin._get_memory = _async_none
    plugin._bot_identity = lambda: {}
    plugin._living_workspace = lambda: ""
    plugin._get_browser_session = lambda write_level: None
    plugin._activity_image_probe = lambda: None
    plugin._caption_screenshot = lambda *a, **k: None

    async def _no_append(tools):
        pass

    plugin._append_agent_tools = _no_append

    async def flow():
        cfg_path.write_text(
            json.dumps({"preset": {}, "advanced": {"autonomy": {"tier": 3}}}),
            encoding="utf-8",
        )
        tools3 = await plugin._build_agent_tools_async()
        names3 = {t.name for t in tools3.tools}
        cfg_path.write_text(json.dumps({"preset": {}, "advanced": {}}),
                            encoding="utf-8")
        tools_default = await plugin._build_agent_tools_async()
        return names3, {t.name for t in tools_default.tools}

    names3, names_default = asyncio.run(flow())
    assert "local_shell" in names3  # tier 3
    assert "local_shell" not in names_default  # 默认 tier 1


def test_c2_source_single_config_source():
    """C2/C1 源码锚点：_build_agent_tools_async 不再有双来源；面板 GET
    走 _effective_config；reset 接线基线归位。
    M19-补丁1：面板 GET 的 build_config_payload 换多行调用（追加
    providers 数据源），锚点同步放宽为"GET 函数内以 _effective_config
    为数据源"。"""
    src = (WORKDIR / "main.py").read_text(encoding="utf-8")
    assert "config = self.config if isinstance(self.config, dict) else {}" not in src
    assert "config = self._effective_config()" in src
    # _api_config_get 内：payload 数据源是 _effective_config（多行形态）
    api_get_src = inspect.getsource(_load_plugin_main().LivingPlugin._api_config_get)
    assert "build_config_payload(" in api_get_src
    assert "self._effective_config()" in api_get_src
    assert "reset_baseline" in src


async def _async_none():
    return None
