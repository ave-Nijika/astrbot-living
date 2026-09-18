"""配置路径 helper（补丁 XVIII 配置分层）。

分层后的 schema 结构：

    config["preset"]   ← 新手设置（9 个旋钮 + life_extra，顶层组）
    config["advanced"] ← 高级调参（原 autonomy/decision/output_gate/
                         sleep/capabilities/memory/model 七组收拢于此）

conf_group 统一做"嵌套优先、平铺兜底"的组读取：

- 生产环境：schema 嵌套后 AstrBotConfig 的键树是 advanced.group.key，
  走嵌套分支；
- 测试与历史数据：手搓的平铺 dict（{"sleep": {...}}）走兜底分支，
  全部既有测试配置无需改动。

主流程的组读取必须经本模块，禁止再直接 config.get("<组名>")——
那会漏读嵌套结构（grep 守护见 tests/test_m3_patchXVIII.py）。
"""

from __future__ import annotations

from typing import Any

CONF_ADVANCED = "advanced"
CONF_PRESET = "preset"


def conf_group(config: Any, group: str) -> dict:
    """读一个配置组：advanced.group 嵌套优先，顶层 group 平铺兜底。"""
    if not isinstance(config, dict):
        return {}
    advanced = config.get(CONF_ADVANCED)
    if isinstance(advanced, dict):
        nested = advanced.get(group)
        if isinstance(nested, dict):
            return nested
    flat = config.get(group)
    return flat if isinstance(flat, dict) else {}


def preset_group(config: Any) -> dict:
    """读新手设置组（旋钮与 life_extra）。"""
    if not isinstance(config, dict):
        return {}
    value = config.get(CONF_PRESET)
    return value if isinstance(value, dict) else {}


def preset_value(config: Any, key: str, default: Any = None) -> Any:
    """读单个新手旋钮/预设值。"""
    value = preset_group(config).get(key, default)
    return default if value in ("", None) and default is not None else value
