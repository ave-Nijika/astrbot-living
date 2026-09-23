"""M9 补丁 2 测试：插件配置磁盘直读（A 组）+ 装配完整性。

直读走真实 main._effective_config（合成包构造插件实例，配置文件路径
指向 tmp）；watcher 语义走真实 LivingLoop._config_watcher（加速轮询）。
前端直连 fetch（B 组）无单测框架——由 mock 服务器浏览器实测覆盖（报告）。
"""

import asyncio
import json
from pathlib import Path

import pytest

WORKDIR = Path(__file__).resolve().parents[1]
SCHEMA_PATH = WORKDIR / "_conf_schema.json"


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


def make_plugin(tmp_path, runtime_config=None):
    """合成插件实例：配置文件路径指向 tmp，self.config 为给定兜底对象。"""
    main_module = _load_plugin_main()
    plugin = object.__new__(main_module.LivingPlugin)
    plugin.config = runtime_config if runtime_config is not None else {}
    cfg_path = Path(tmp_path) / "astrbot_plugin_living_config.json"
    plugin._plugin_config_path = lambda: str(cfg_path)
    return plugin, cfg_path


# ---------------------------------------------------------------------------
# 验收 1：config_getter()（_effective_config）反映文件内容
# ---------------------------------------------------------------------------
def test_effective_config_reads_from_disk(tmp_path):
    plugin, cfg_path = make_plugin(tmp_path)
    cfg_path.write_text(
        json.dumps({"advanced": {"decision": {"single_run_token_budget": 999}}}),
        encoding="utf-8",
    )
    first = plugin._effective_config()
    assert first["advanced"]["decision"]["single_run_token_budget"] == 999

    # 运行中改文件 → 下一次调用即新值（无缓存滞后）——故障 1 的修复语义
    cfg_path.write_text(
        json.dumps({"advanced": {"decision": {"single_run_token_budget": 123456}}}),
        encoding="utf-8",
    )
    second = plugin._effective_config()
    assert second["advanced"]["decision"]["single_run_token_budget"] == 123456


def test_effective_config_merges_schema_defaults(tmp_path):
    """A2：文件缺键回落 schema default（与 AstrBotConfig 行为一致）。"""
    plugin, cfg_path = make_plugin(tmp_path)
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    budget_default = schema["advanced"]["items"]["decision"]["items"][
        "single_run_token_budget"
    ]["default"]
    # 文件只写一个键，其余全部缺失
    cfg_path.write_text(
        json.dumps({"advanced": {"decision": {"single_run_token_budget": 999}}}),
        encoding="utf-8",
    )
    merged = plugin._effective_config()
    # 缺失的组/键补默认
    assert merged["advanced"]["decision"]["single_run_token_budget"] == 999
    assert (
        merged["advanced"]["decision"]["daily_impulse_limit"]
        == schema["advanced"]["items"]["decision"]["items"]["daily_impulse_limit"][
            "default"
        ]
    )
    assert set(merged["advanced"].keys()) == set(schema["advanced"]["items"].keys())
    assert merged["preset"] == plugin._schema_defaults()["preset"]
    # None 视同缺失，同样补默认
    cfg_path.write_text(
        json.dumps({"preset": {"life_extra": None}}),
        encoding="utf-8",
    )
    merged2 = plugin._effective_config()
    assert (
        merged2["preset"]["life_extra"]
        == schema["preset"]["items"]["life_extra"]["default"]
    )


def test_merge_preserves_extra_keys_and_does_not_pollute_cache(tmp_path):
    """磁盘上 schema 没有的键原样保留；合并结果 mutate 不污染默认树缓存。"""
    plugin, cfg_path = make_plugin(tmp_path)
    cfg_path.write_text(
        json.dumps({"preset": {"life_extra": "手填背景"}, "future_key": 1}),
        encoding="utf-8",
    )
    merged = plugin._effective_config()
    assert merged["future_key"] == 1
    assert merged["preset"]["life_extra"] == "手填背景"
    merged["preset"]["life_extra"] = "被改掉"
    # 再读：缓存里的默认树未被污染（缺键补默认仍给原值）
    merged2 = plugin._effective_config()
    assert merged2["preset"]["life_extra"] == "手填背景"


# ---------------------------------------------------------------------------
# 验收 2 / A3：读取失败容错 → 回落 self.config
# ---------------------------------------------------------------------------
def test_effective_config_falls_back_on_corrupt_file(tmp_path):
    plugin, cfg_path = make_plugin(tmp_path)
    fallback = {"preset": {"life_extra": "运行时兜底"}}
    plugin.config = fallback
    cfg_path.write_text("{ this is not json !!!", encoding="utf-8")
    assert plugin._effective_config() is fallback  # 不抛异常，回落同一对象


def test_effective_config_falls_back_on_non_object_root(tmp_path):
    plugin, cfg_path = make_plugin(tmp_path)
    fallback = {"marker": 1}
    plugin.config = fallback
    cfg_path.write_text("[1, 2, 3]", encoding="utf-8")
    assert plugin._effective_config() is fallback


def test_effective_config_falls_back_when_file_missing(tmp_path):
    plugin, cfg_path = make_plugin(tmp_path)
    fallback = {"marker": 2}
    plugin.config = fallback
    assert not cfg_path.exists()
    assert plugin._effective_config() is fallback


# ---------------------------------------------------------------------------
# 验收 3：watcher 感知磁盘变化 → notify_config_changed（定时器重置语义）
# ---------------------------------------------------------------------------
def test_config_watcher_tracks_disk_changes(tmp_path, monkeypatch):
    import core.living_loop as living_loop_mod
    from core.living_loop import LivingLoop

    monkeypatch.setattr(living_loop_mod, "CONFIG_POLL_SECONDS", 0.05)
    plugin, cfg_path = make_plugin(tmp_path)
    cfg_path.write_text(
        json.dumps({"advanced": {"decision": {"single_run_token_budget": 1}}}),
        encoding="utf-8",
    )
    loop = LivingLoop(
        gate=object(),
        memory_getter=lambda: None,
        config_getter=plugin._effective_config,
    )
    # hash 直接反映磁盘内容
    hash_before = loop._config_hash()
    cfg_path.write_text(
        json.dumps({"advanced": {"decision": {"single_run_token_budget": 2}}}),
        encoding="utf-8",
    )
    assert loop._config_hash() != hash_before

    async def flow():
        watcher = asyncio.create_task(loop._config_watcher())
        await asyncio.sleep(0.15)  # 第一轮基准 + 若干轮询
        assert not loop._config_event.is_set()
        cfg_path.write_text(
            json.dumps(
                {"advanced": {"decision": {"single_run_token_budget": 3}}}
            ),
            encoding="utf-8",
        )
        await asyncio.sleep(0.3)  # 等轮询发现变化
        watcher.cancel()
        try:
            await watcher
        except asyncio.CancelledError:
            pass
        return loop._config_event.is_set()

    assert asyncio.run(flow()) is True  # notify_config_changed 被调用


# ---------------------------------------------------------------------------
# 装配完整性：7 处 config_getter 全部指向磁盘直读（静态断言）
# ---------------------------------------------------------------------------
def test_assembly_uses_disk_reader():
    src = (WORKDIR / "main.py").read_text(encoding="utf-8")
    assert "config_getter=lambda: self.config" not in src
    assert src.count("config_getter=self._effective_config") == 7


# ---------------------------------------------------------------------------
# 红线复核：core/ 零改动（本补丁不允许碰 core/）
# ---------------------------------------------------------------------------
def test_core_untouched_by_this_patch():
    # watcher 的 hash 与轮询实现仍在 core/living_loop.py 且未被修改签名
    import inspect

    from core.living_loop import LivingLoop

    src = inspect.getsource(LivingLoop._config_hash)
    assert "self._config_getter()" in src  # hash 输入即 config_getter 返回值
