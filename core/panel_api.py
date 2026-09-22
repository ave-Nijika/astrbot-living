"""配置面板 API 的纯逻辑层（M5 补丁 1）。

main.py 的 web handler 与 scripts/panel_dev_server.py（浏览器实测用 mock
服务器）都走本模块——保证"面板前端 → mock 实测"与"面板前端 → 生产"执行
同一套校验与写入逻辑。

与 ConfigKnobs 的关系（红线 2：不重写映射表）：旋钮写入复用
config_knobs.apply_knob_value（同一份 KNOB_PRESETS 映射），本模块只负责
面板语义的组装、校验与编排。
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from .config_knobs import DIRECT_KNOB, KNOB_PRESETS, apply_knob_value
from .conf_path import CONF_ADVANCED, CONF_PRESET
from .mood import UNIT_MAX, UNIT_MIN

SCHEMA_FILENAME = "_conf_schema.json"

# M9-补丁1 B1：心境快照的五项只读状态（energy/fatigue/valence/arousal/
# sleep_debt）——由活动和睡眠自然涨落，手改会破坏涌现，不做编辑入口；
# interests 是唯一可管理项（改/删/清空）
MOOD_SNAPSHOT_FIELDS = ("energy", "fatigue", "valence", "arousal", "sleep_debt")
# M9-补丁1 B2：兴趣写操作的全部合法 action
INTEREST_ACTIONS = ("set", "delete", "clear")


class PanelApiError(Exception):
    """面板 API 的业务错误（message 直接反馈给前端）。"""


def load_schema(plugin_dir: str | Path) -> dict:
    """读插件目录的 _conf_schema.json（面板渲染元数据来源）。"""
    path = Path(plugin_dir) / SCHEMA_FILENAME
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def default_tree(schema: dict) -> dict:
    """按 schema 生成默认值树（仅 preset/advanced 两层，reset 用）。"""
    def parse(items: dict) -> dict:
        conf: dict = {}
        for key, item in items.items():
            if not isinstance(item, dict) or "type" not in item:
                continue
            if item["type"] == "object" and isinstance(item.get("items"), dict):
                conf[key] = parse(item["items"])
            elif "default" in item:
                conf[key] = copy.deepcopy(item["default"])
            else:
                conf[key] = {"int": 0, "float": 0.0, "bool": False, "string": "",
                             "text": "", "list": [], "object": {}}.get(item["type"])
        return conf

    return {
        CONF_PRESET: parse(schema.get(CONF_PRESET, {}).get("items", {})),
        CONF_ADVANCED: parse(schema.get(CONF_ADVANCED, {}).get("items", {})),
    }


def build_config_payload(config: Any, schema: dict) -> dict:
    """组装 GET 返回体：当前值（knobs + advanced）+ 渲染元数据（schema）。"""
    from .conf_path import preset_group

    def section(group_items: dict) -> dict:
        # 只导出带 type 的键定义（组元数据不进面板）
        return {
            key: item
            for key, item in group_items.items()
            if isinstance(item, dict) and "type" in item
        }

    advanced_values = {
        group: dict(items)
        for group, items in (config.get(CONF_ADVANCED) or {}).items()
        if isinstance(items, dict)
    }
    return {
        "knobs": dict(preset_group(config)),
        "advanced": advanced_values,
        # 与原 schema 同构（{preset/advanced: {items: {键: 定义}}}），前端
        # 按 .items 遍历渲染
        "schema": {
            CONF_PRESET: {"items": section(schema.get(CONF_PRESET, {}).get("items", {}))},
            CONF_ADVANCED: {"items": section(schema.get(CONF_ADVANCED, {}).get("items", {}))},
        },
    }


def _check_type(item: dict, value: Any) -> str | None:
    """按 schema type 校验；返回错误文本或 None（通过）。"""
    t = item.get("type")
    if t == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            return f"需要整数，收到 {type(value).__name__}"
    elif t == "float":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return f"需要数字，收到 {type(value).__name__}"
    elif t == "bool":
        if not isinstance(value, bool):
            return f"需要 true/false，收到 {type(value).__name__}"
    elif t in ("string", "text"):
        if not isinstance(value, str):
            return f"需要字符串，收到 {type(value).__name__}"
    elif t == "list":
        if not isinstance(value, list):
            return f"需要列表，收到 {type(value).__name__}"
    elif t == "object":
        if not isinstance(value, dict):
            return f"需要对象，收到 {type(value).__name__}"
    return None


def apply_panel_save(config: dict, schema: dict, payload: dict) -> dict:
    """应用面板保存（POST body）到内存 config，返回写入摘要。

    body 允许：{"knobs": {...}}、{"advanced": {组: {键: 值}}}、两者同时。
    - knobs：复用 KNOB_PRESETS 映射写底层键（preset_model 直通），并更新
      旋钮键本身（回显）；life_extra 直接写 preset 组；
    - advanced：逐键按 schema 类型校验后写入，未知键/类型不符抛 PanelApiError；
    - **应用顺序 advanced 在前、knobs 在后**：GUI 全量提交时 advanced 载荷
      可能携带页面加载时的旧值，旋钮映射必须最后落笔（旋钮=批量预设器，
      它对底层键的意图优先于快照旧值）——浏览器实测抓出的覆盖缺陷；
    - 只改内存，落盘由调用方（main 走 AstrBotConfig.save_config）负责。
    """
    if not isinstance(payload, dict):
        raise PanelApiError("请求体必须是 JSON 对象")
    if "knobs" not in payload and "advanced" not in payload:
        raise PanelApiError("请求体需包含 knobs 或 advanced 至少一项")

    preset: dict = config.setdefault(CONF_PRESET, {})
    advanced: dict = config.setdefault(CONF_ADVANCED, {})
    advanced_schema = schema.get(CONF_ADVANCED, {}).get("items", {})
    preset_schema = schema.get(CONF_PRESET, {}).get("items", {})
    changes: list[str] = []

    advanced_payload = payload.get("advanced")
    if advanced_payload is not None:
        if not isinstance(advanced_payload, dict):
            raise PanelApiError("advanced 必须是对象")
        for group, keys in advanced_payload.items():
            group_schema = advanced_schema.get(group, {}).get("items")
            if not isinstance(group_schema, dict):
                raise PanelApiError(f"未知配置组 {group!r}")
            if not isinstance(keys, dict):
                raise PanelApiError(f"{group} 必须是键值对象")
            target = advanced.setdefault(group, {})
            for key, value in keys.items():
                item = group_schema.get(key)
                if not isinstance(item, dict) or "type" not in item:
                    raise PanelApiError(f"未知配置键 {group}.{key!r}")
                error = _check_type(item, value)
                if error:
                    raise PanelApiError(f"{group}.{key}：{error}")
                target[key] = value
                changes.append(f"{group}.{key}={value!r}")

    knobs = payload.get("knobs")
    if knobs is not None:
        if not isinstance(knobs, dict):
            raise PanelApiError("knobs 必须是对象")
        for name, value in knobs.items():
            if name == "life_extra":
                item = preset_schema.get(name)
                if item:
                    error = _check_type(item, value)
                    if error:
                        raise PanelApiError(f"life_extra：{error}")
                preset[name] = str(value or "")
                changes.append("life_extra")
                continue
            if name == DIRECT_KNOB:
                item = preset_schema.get(name)
                if item:
                    error = _check_type(item, value)
                    if error:
                        raise PanelApiError(f"{name}：{error}")
                preset[name] = str(value or "")
                apply_knob_value(config, name, value)
                changes.append(f"{name} → model.provider_id")
                continue
            options = KNOB_PRESETS.get(name, {})
            if name not in preset_schema:
                raise PanelApiError(f"未知旋钮 {name!r}")
            if value not in options:
                raise PanelApiError(
                    f"旋钮 {name} 的值 {value!r} 不在允许选项 {sorted(options)} 内"
                )
            preset[name] = value
            apply_knob_value(config, name, value)
            changes.append(f"{name}={value}")

    return {"changed": changes, "count": len(changes)}


def apply_panel_reset(config: dict, schema: dict) -> dict:
    """恢复默认值：preset/advanced 按 schema default 树重写（内存），返回摘要。"""
    defaults = default_tree(schema)
    config[CONF_PRESET] = defaults[CONF_PRESET]
    config[CONF_ADVANCED] = defaults[CONF_ADVANCED]
    return {
        "preset_keys": len(defaults[CONF_PRESET]),
        "advanced_keys": sum(
            len(items) for items in defaults[CONF_ADVANCED].values()
        ),
    }


# ---------------------------------------------------------------------------
# 心境与兴趣（M9-补丁1 B 组）：读写统一走 MoodState 运行中实例——禁止绕过
# 实例直接写 mood.db（绕过会与内存态脱节，热失效）。
# ---------------------------------------------------------------------------
def build_mood_snapshot(mood: Any) -> dict:
    """GET 心境全量快照（B1）：五项只读状态 + interests 表。

    只读函数：不修改实例任何字段，不触发落盘。"""
    return {
        "energy": float(mood.energy),
        "fatigue": float(mood.fatigue),
        "valence": float(mood.valence),
        "arousal": float(mood.arousal),
        "sleep_debt": float(mood.sleep_debt),
        "interests": mood.get_interests(),
    }


async def apply_mood_interests(mood: Any, payload: Any) -> dict:
    """POST 兴趣写操作（B2-B4）：set / delete / clear。

    校验全部通过才动实例（不部分写入，B4）；写入内存后立即 mood.save()
    持久化（B2 热生效 + 重启不丢）。返回 {"interests": 修改后的完整表}
    （B3，前端无需二次拉取）。校验失败抛 PanelApiError（handler 层转
    错误响应）。
    """
    if not isinstance(payload, dict):
        raise PanelApiError("请求体必须是 JSON 对象")
    action = payload.get("action")
    if action not in INTEREST_ACTIONS:
        raise PanelApiError(
            f"未知 action {action!r}，允许：{list(INTEREST_ACTIONS)}"
        )
    topic = payload.get("topic")
    if action in ("set", "delete"):
        if not isinstance(topic, str) or not topic.strip():
            raise PanelApiError("topic 必须是非空字符串")
        topic = topic.strip()
    if action == "set":
        weight = payload.get("weight")
        if isinstance(weight, bool) or not isinstance(weight, (int, float)):
            raise PanelApiError(f"weight 需要数字，收到 {type(weight).__name__}")
        weight = float(weight)
        if not (UNIT_MIN <= weight <= UNIT_MAX):
            raise PanelApiError(f"weight 需在 [0,1] 内，收到 {weight}")
        mood.interests[topic] = weight  # 同名 topic 覆盖
    elif action == "delete":
        # 不存在的 topic 幂等删除：结果状态正确即成功（与 RESTful 语义一致）
        mood.interests.pop(topic, None)
    else:  # clear
        mood.interests.clear()
    await mood.save()
    return {"interests": mood.get_interests()}
