"""M5 补丁 1 测试：自带配置面板——API 结构、旋钮/专家保存、非法值、reset。

后端业务逻辑在 core/panel_api.py（main handler 与浏览器实测 mock 服务器
共用的纯函数层）；本文件同时经合成包直调 main 的三个 handler 验证装配。
"""

import asyncio
import json
from pathlib import Path

import pytest

from core.config_knobs import KNOB_PRESETS
from core.conf_path import conf_group
from core.panel_api import (
    PanelApiError,
    apply_panel_reset,
    apply_panel_save,
    build_config_payload,
    default_tree,
    load_schema,
)

WORKDIR = Path(__file__).resolve().parents[1]
SCHEMA = load_schema(WORKDIR)


def _full_config():
    """按 schema 默认树构造一份完整配置（含 preset/advanced 两层）。"""
    tree = default_tree(SCHEMA)
    config = {"preset": tree["preset"], "advanced": tree["advanced"]}
    return config


# ---------------------------------------------------------------------------
# 验收 1：GET 结构——knobs/advanced/schema 三部分，键数与 schema 一致
# ---------------------------------------------------------------------------
def test_get_payload_structure_and_key_counts():
    config = _full_config()
    payload = build_config_payload(config, SCHEMA)
    assert set(payload) == {"knobs", "advanced", "schema"}

    knobs = payload["schema"]["preset"]["items"]
    assert sum(1 for k in knobs if k.startswith("preset_")) == 8  # M6-补丁1：preset_sleep_style 随 sleep_mode 移除
    assert "life_extra" in knobs
    assert payload["schema"]["advanced"]["items"].keys() == \
        SCHEMA["advanced"]["items"].keys()
    advanced_count = sum(
        len(g["items"]) for g in payload["schema"]["advanced"]["items"].values()
    )
    assert advanced_count == 62  # 61 + M10-补丁1 的 free_choice_ratio

    # 当前值区：knobs 含全部旋钮默认、advanced 7 组 57 键
    assert sum(1 for k in payload["knobs"] if k.startswith("preset_")) == 8
    assert sum(len(v) for v in payload["advanced"].values()) == 62


# ---------------------------------------------------------------------------
# 验收 2：旋钮保存走 ConfigKnobs 的同一份映射（不重写映射表）
# ---------------------------------------------------------------------------
def test_knob_save_uses_shared_mapping():
    config = _full_config()
    payload = {"knobs": {"preset_activity_level": "quiet"}}
    summary = apply_panel_save(config, SCHEMA, payload)
    decision = conf_group(config, "decision")
    # 期望值与 KNOB_PRESETS 一致（同一数据源，行为等价于 ConfigKnobs 写入）
    assert decision["impulse_check_interval_minutes"] == \
        KNOB_PRESETS["preset_activity_level"]["quiet"]["decision"]["impulse_check_interval_minutes"]
    assert decision["impulse_check_interval_minutes"] == 90
    assert decision["daily_impulse_limit"] == 1
    # 旋钮键本身同步（回显）
    assert config["preset"]["preset_activity_level"] == "quiet"
    assert summary["count"] == 1


def test_knob_save_all_knobs_via_shared_table():
    """数据驱动：全部旋钮 × 全部选项经面板保存与映射表逐值一致。"""
    for knob, options in KNOB_PRESETS.items():
        for option, mapping in options.items():
            config = _full_config()
            apply_panel_save(config, SCHEMA, {"knobs": {knob: option}})
            for group, keys in mapping.items():
                for key, expected in keys.items():
                    assert conf_group(config, group)[key] == expected, (
                        f"{knob}={option}: {group}.{key}"
                    )


def test_preset_model_direct_write():
    config = _full_config()
    apply_panel_save(config, SCHEMA, {"knobs": {"preset_model": "cheap-llm"}})
    assert config["preset"]["preset_model"] == "cheap-llm"
    assert conf_group(config, "model")["provider_id"] == "cheap-llm"


def test_knob_mapping_wins_over_stale_advanced_payload():
    """GUI 全量提交场景：advanced 载荷携带页面加载时的旧值时，旋钮映射
    必须最后落笔（浏览器实测抓出的覆盖缺陷——保存 quiet 后 interval 仍
    是旧值 5）。应用顺序：advanced 先、knobs 后。"""
    config = _full_config()
    stale_advanced = {"decision": {"impulse_check_interval_minutes": 5}}
    apply_panel_save(config, SCHEMA, {
        "knobs": {"preset_activity_level": "quiet"},
        "advanced": stale_advanced,
    })
    decision = conf_group(config, "decision")
    assert decision["impulse_check_interval_minutes"] == 90  # 不是旧值 5
    assert decision["daily_impulse_limit"] == 1
    assert config["preset"]["preset_activity_level"] == "quiet"


def test_life_extra_saved_into_preset():
    config = _full_config()
    apply_panel_save(config, SCHEMA, {"knobs": {"life_extra": "喜欢天文"}})
    assert config["preset"]["life_extra"] == "喜欢天文"


# ---------------------------------------------------------------------------
# 验收 3：专家保存——单键写回且其他键不变
# ---------------------------------------------------------------------------
def test_advanced_single_key_save_keeps_others():
    config = _full_config()
    before = json.dumps(config, ensure_ascii=False, sort_keys=True)
    summary = apply_panel_save(
        config, SCHEMA,
        {"advanced": {"sleep": {"sleepiness_threshold": 0.42}}},
    )
    assert summary["count"] == 1
    assert conf_group(config, "sleep")["sleepiness_threshold"] == 0.42
    # 其余键全部不变
    after = json.dumps(config, ensure_ascii=False, sort_keys=True)
    restored = json.loads(after)
    restored["advanced"]["sleep"]["sleepiness_threshold"] = \
        SCHEMA["advanced"]["items"]["sleep"]["items"]["sleepiness_threshold"]["default"]
    original = json.loads(before)
    assert restored == original


# ---------------------------------------------------------------------------
# 验收 4：非法值拒绝（明确错误文本）
# ---------------------------------------------------------------------------
def test_invalid_values_rejected_with_clear_message():
    config = _full_config()

    with pytest.raises(PanelApiError, match="需要整数"):
        apply_panel_save(config, SCHEMA, {"advanced": {"decision": {"daily_impulse_limit": "三"}}})
    with pytest.raises(PanelApiError, match="需要 true/false"):
        apply_panel_save(config, SCHEMA, {"advanced": {"sleep": {"nap_enabled": "yes"}}})
    with pytest.raises(PanelApiError, match="需要列表"):
        apply_panel_save(config, SCHEMA, {"advanced": {"decision": {"agent_activities": "surf"}}})
    with pytest.raises(PanelApiError, match="不在允许选项"):
        apply_panel_save(config, SCHEMA, {"knobs": {"preset_topic_taste": "乱写"}})
    with pytest.raises(PanelApiError, match="未知旋钮"):
        apply_panel_save(config, SCHEMA, {"knobs": {"preset_hack": "x"}})
    with pytest.raises(PanelApiError, match="未知配置键"):
        apply_panel_save(config, SCHEMA, {"advanced": {"sleep": {"hack_key": 1}}})
    with pytest.raises(PanelApiError, match="未知配置组"):
        apply_panel_save(config, SCHEMA, {"advanced": {"hack_group": {"a": 1}}})
    with pytest.raises(PanelApiError, match="至少一项"):
        apply_panel_save(config, SCHEMA, {})

    # 被拒的保存不得半途写入（第一个错误抛出前已改的键保留、错误键不写入）
    assert conf_group(config, "decision")["daily_impulse_limit"] == \
        SCHEMA["advanced"]["items"]["decision"]["items"]["daily_impulse_limit"]["default"]


def test_bool_not_accepted_as_int():
    """bool 是 int 子类——类型校验必须排除（True 不等于 1）。"""
    config = _full_config()
    with pytest.raises(PanelApiError, match="需要整数"):
        apply_panel_save(config, SCHEMA, {"advanced": {"decision": {"daily_impulse_limit": True}}})


# ---------------------------------------------------------------------------
# reset：默认树重写 + 幂等
# ---------------------------------------------------------------------------
def test_reset_restores_defaults_and_idempotent():
    config = _full_config()
    # 用户大改一通
    apply_panel_save(config, SCHEMA, {
        "knobs": {"preset_activity_level": "active", "life_extra": "改过"},
        "advanced": {"sleep": {"sleepiness_threshold": 0.9}},
    })
    apply_panel_reset(config, SCHEMA)
    snapshot1 = json.dumps(config, ensure_ascii=False, sort_keys=True)
    assert config["preset"]["preset_activity_level"] == \
        SCHEMA["preset"]["items"]["preset_activity_level"]["default"]
    assert conf_group(config, "sleep")["sleepiness_threshold"] == \
        SCHEMA["advanced"]["items"]["sleep"]["items"]["sleepiness_threshold"]["default"]

    apply_panel_reset(config, SCHEMA)
    snapshot2 = json.dumps(config, ensure_ascii=False, sort_keys=True)
    assert snapshot1 == snapshot2  # 幂等


# ---------------------------------------------------------------------------
# main handler 装配（合成包直调：GET/POST/reset 全链路，含落盘跳过）
# ---------------------------------------------------------------------------
def _load_plugin_main():
    import importlib
    import sys
    import types

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


def _plugin_with_full_config(tmp_path):
    main_module = _load_plugin_main()
    plugin = object.__new__(main_module.LivingPlugin)
    plugin.context = types_simple_namespace_with_register()
    plugin.config = _full_config()
    plugin._panel_schema = lambda: SCHEMA  # 与生产同源（同目录 schema 文件）
    return plugin, main_module


class types_simple_namespace_with_register:
    """带 register_web_api 的假 context：捕获注册的路由供直调。"""

    def __init__(self):
        self.routes = {}

    def register_web_api(self, route, handler, methods, desc):
        self.routes[(route, tuple(methods))] = handler


def test_dashboard_routes_register_and_full_flow(tmp_path):
    plugin, main_module = _plugin_with_full_config(tmp_path)
    plugin._register_dashboard_routes()
    prefix = f"/{main_module.PLUGIN_NAME}"
    assert (f"{prefix}/config", ("GET",)) in plugin.context.routes
    assert (f"{prefix}/config", ("POST",)) in plugin.context.routes
    assert (f"{prefix}/config/reset", ("POST",)) in plugin.context.routes

    # GET：结构 + 当前值
    got = asyncio.run(plugin.context.routes[(f"{prefix}/config", ("GET",))]())
    assert got["status"] == "ok"
    assert got["data"]["schema"]["advanced"]["items"].keys() == \
        SCHEMA["advanced"]["items"].keys()

    # POST：旋钮保存 → 底层键写入；普通 dict 无 save_config → 落盘静默跳过
    post = plugin.context.routes[(f"{prefix}/config", ("POST",))]
    async def fake_json(default=None):
        return {"knobs": {"preset_write_level": "comment"}}
    saved_request = main_module

    async def flow():
        import types as t
        from astrbot.api import web as astrbot_web
        # 临时替换 astrbot.api.web.request 的 json 读取
        class FakeRequest:
            async def json(self, default=None):
                return {"knobs": {"preset_write_level": "comment"}}
        original = astrbot_web.request
        astrbot_web.request = FakeRequest()
        try:
            return await post()
        finally:
            astrbot_web.request = original

    result = asyncio.run(flow())
    assert result["status"] == "ok", result
    assert plugin.config["preset"]["preset_write_level"] == "comment"
    assert plugin.config["advanced"]["autonomy"]["write_level"] == 2

    # POST 非法值 → 明确错误文本
    async def flow_bad():
        from astrbot.api import web as astrbot_web

        class FakeRequest:
            async def json(self, default=None):
                return {"advanced": {"decision": {"daily_impulse_limit": "三"}}}

        original = astrbot_web.request
        astrbot_web.request = FakeRequest()
        try:
            return await post()
        finally:
            astrbot_web.request = original

    bad = asyncio.run(flow_bad())
    assert bad["status"] == "error" and "需要整数" in bad["message"]

    # reset：恢复默认
    reset = plugin.context.routes[(f"{prefix}/config/reset", ("POST",))]
    async def flow_reset():
        from astrbot.api import web as astrbot_web

        class FakeRequest:
            async def json(self, default=None):
                return {}

        original = astrbot_web.request
        astrbot_web.request = FakeRequest()
        try:
            return await reset()
        finally:
            astrbot_web.request = original

    ok = asyncio.run(flow_reset())
    assert ok["status"] == "ok"
    assert plugin.config["advanced"]["autonomy"]["write_level"] == \
        SCHEMA["advanced"]["items"]["autonomy"]["items"]["write_level"]["default"]


def test_register_skipped_gracefully_without_context_api():
    """context 无 register_web_api（局部 mock/旧环境）→ WARNING 不抛。"""
    import types

    main_module = _load_plugin_main()
    plugin = object.__new__(main_module.LivingPlugin)
    plugin.context = types.SimpleNamespace()  # 无 register_web_api
    plugin.config = _full_config()
    plugin._panel_schema = lambda: SCHEMA
    plugin._register_dashboard_routes()  # 不抛即可
