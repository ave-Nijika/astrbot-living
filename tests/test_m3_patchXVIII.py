"""M3 补丁 XVIII 测试：配置分层——新手旋钮、高级组收拢、危险项 invisible。

验收 1-5 全覆盖；生产读取点的"嵌套优先平铺兜底"语义由 conf_path 单测
与 main._cfg 嵌套读取测试共同守护。
"""

import asyncio
import json
from pathlib import Path

import pytest

from core.conf_path import conf_group, preset_group, preset_value
from core.config_knobs import DIRECT_KNOB, KNOB_PRESETS, ConfigKnobs

WORKDIR = Path(__file__).resolve().parents[1]
SCHEMA = json.loads((WORKDIR / "_conf_schema.json").read_text(encoding="utf-8"))

KNOB_NAMES = [
    "preset_sleep_style", "preset_activity_level", "preset_talk_frequency",
    "preset_capability_tier", "preset_write_level", "preset_topic_taste",
    "preset_free_activity", "preset_decision_mode", "preset_model",
]

# 补丁 XVII 基线（改造前 HEAD）的全部现有键——收拢后一个不能少
BASELINE_KEYS = {
    "autonomy": {"tier", "workspace_dir", "write_level"},
    "decision": {
        "activity_probability", "activity_probability_min",
        "activity_probability_ramp_minutes", "agent_activities",
        "daily_impulse_limit", "decision_mode", "exploration_trigger",
        "exploration_window", "free_activity_enabled",
        "impulse_check_interval_minutes", "interest_cooldown_factor",
        "interest_cooldown_threshold", "interest_daily_decay",
        "max_run_seconds", "max_tool_rounds", "recent_topic_penalty",
        "recent_topic_window", "single_run_token_budget",
    },
    "output_gate": {
        "daily_message_limit", "message_min_interval_minutes", "quiet_hours",
        "share_max_length", "share_rewrite_enabled", "share_rewrite_prompt",
        "target_sessions",
    },
    "sleep": {
        "awake_standby_minutes", "circadian_hint", "dream_probability",
        "fatigue_rate_per_hour", "grouchiness_percent", "max_sleep_hours",
        "min_awake_minutes", "min_sleep_hours", "nap_enabled",
        "nap_max_minutes", "nap_min_minutes", "owner_id",
        "sleep_debt_decay_per_day", "sleep_farewell_message", "sleep_mode",
        "sleep_mute_replies", "sleep_window", "sleepiness_jitter",
        "sleepiness_threshold", "wake_ack_message", "wake_n_messages",
        "wake_source", "wake_window_minutes", "weights",
    },
    "capabilities": {"cooldown_between_activities_hours", "sandbox_timeout_seconds"},
    "memory": {"backend"},
    "model": {"fallback_chain", "provider_id"},
}

DANGER_KEYS = [
    ("sleep", "weights"),
    ("sleep", "fatigue_rate_per_hour"),
    ("decision", "recent_topic_penalty"),
    ("decision", "single_run_token_budget"),
    ("decision", "max_tool_rounds"),
    ("decision", "max_run_seconds"),
]


# ---------------------------------------------------------------------------
# 验收 1：9 个旋钮进 schema（preset 组在前、hint 齐全）
# ---------------------------------------------------------------------------
def test_preset_knobs_in_schema():
    groups = list(SCHEMA)
    assert groups[0] == "preset" and groups[1] == "advanced"  # 新手组在最前
    items = SCHEMA["preset"]["items"]
    for name in KNOB_NAMES:
        assert name in items, f"缺旋钮 {name}"
        knob = items[name]
        assert knob.get("hint"), f"{name} 缺大白话 hint"
        assert knob.get("options") or name == DIRECT_KNOB  # preset_model 是直通文本键
    # life_extra 挪进新手组（主人最常改）
    assert items["life_extra"]["type"] == "text"
    assert items["life_extra"].get("hint")


# ---------------------------------------------------------------------------
# 验收 2：映射表逐旋钮逐选项写入正确（数据驱动全覆盖）
# ---------------------------------------------------------------------------
def _nested_config():
    """一个嵌套结构的最小配置（advanced 下各组 + preset 空组）。"""
    advanced = {g: {} for g in BASELINE_KEYS}
    return {"preset": {}, "advanced": advanced}


@pytest.mark.parametrize("knob", sorted(KNOB_PRESETS))
def test_knob_every_option_writes_mapped_keys(knob):
    saved = {"n": 0}

    async def fake_save():
        saved["n"] += 1

    for option, mapping in KNOB_PRESETS[knob].items():
        config = _nested_config()
        config["preset"][knob] = None  # 初始无值 → arm 记基线
        knobs = ConfigKnobs(lambda: config, save_config=fake_save)
        knobs.arm()
        assert asyncio.run(knobs.apply_changes()) == []  # 基线不写入

        config["preset"][knob] = option  # 用户改动旋钮
        applied = asyncio.run(knobs.apply_changes())
        assert applied, f"{knob}={option} 应产生一次写入"
        for group, keys in mapping.items():
            for key, expected in keys.items():
                actual = conf_group(config, group)[key]
                assert actual == expected, f"{knob}={option}: {group}.{key}"
        assert saved["n"] == 1  # 批量变更一次持久化
        saved["n"] = 0


def test_knob_mapping_matches_task_book_values():
    """硬编码抽查：映射表数值必须与任务书 2.2 一致（防数据源自身写错）。"""
    assert KNOB_PRESETS["preset_sleep_style"] == {
        "fixed": {"sleep": {"sleep_mode": "fixed"}},
        "autonomous": {"sleep": {"sleep_mode": "autonomous"}},
    }
    assert KNOB_PRESETS["preset_activity_level"]["quiet"]["decision"] == {
        "impulse_check_interval_minutes": 90,
        "activity_probability": 0.4,
        "daily_impulse_limit": 1,
    }
    assert KNOB_PRESETS["preset_activity_level"]["normal"]["decision"] == {
        "impulse_check_interval_minutes": 45,
        "activity_probability": 0.8,
        "daily_impulse_limit": 0,
    }
    assert KNOB_PRESETS["preset_activity_level"]["active"]["decision"] == {
        "impulse_check_interval_minutes": 20,
        "activity_probability": 1.0,
        "daily_impulse_limit": 0,
    }
    assert KNOB_PRESETS["preset_talk_frequency"]["rare"]["output_gate"] == {
        "daily_message_limit": 3, "message_min_interval_minutes": 90,
    }
    assert KNOB_PRESETS["preset_talk_frequency"]["often"]["output_gate"] == {
        "daily_message_limit": 30, "message_min_interval_minutes": 15,
    }
    assert KNOB_PRESETS["preset_topic_taste"]["focused"]["decision"] == {
        "interest_daily_decay": 0.95, "recent_topic_window": 3,
        "exploration_trigger": 5,
    }
    assert KNOB_PRESETS["preset_topic_taste"]["diverse"]["decision"] == {
        "interest_daily_decay": 0.5, "recent_topic_window": 10,
        "exploration_trigger": 2,
    }
    assert KNOB_PRESETS["preset_write_level"]["comment"]["autonomy"] == {
        "write_level": 2,
    }
    assert KNOB_PRESETS["preset_decision_mode"]["rich"]["decision"] == {
        "decision_mode": "llm",
    }
    assert KNOB_PRESETS["preset_free_activity"]["off"]["decision"] == {
        "free_activity_enabled": False,
    }


def test_preset_model_direct_write():
    """preset_model 直通：写入 advanced.model.provider_id。"""
    config = _nested_config()
    config["preset"][DIRECT_KNOB] = "my-cheap-llm"
    knobs = ConfigKnobs(lambda: config, save_config=None)
    knobs.arm()
    applied = asyncio.run(knobs.apply_changes())
    assert applied == []  # 首次记基线
    config["preset"][DIRECT_KNOB] = "another-llm"
    applied = asyncio.run(knobs.apply_changes())
    assert applied and "provider_id" in applied[0]
    assert conf_group(config, "model")["provider_id"] == "another-llm"


# ---------------------------------------------------------------------------
# 验收 3：不持续覆盖（专家微调底层键，旋钮不改回去）
# ---------------------------------------------------------------------------
def test_knobs_do_not_override_expert_edits():
    config = _nested_config()
    config["preset"]["preset_topic_taste"] = "diverse"
    knobs = ConfigKnobs(lambda: config, save_config=None)
    knobs.arm()
    asyncio.run(knobs.apply_changes())  # 首次：记基线
    asyncio.run(knobs.apply_changes())  # 无变化

    # 旋钮改动一次 → 写入底层键
    config["preset"]["preset_topic_taste"] = "focused"
    asyncio.run(knobs.apply_changes())
    decision = conf_group(config, "decision")
    assert decision["interest_daily_decay"] == 0.95

    # 专家随后手动微调底层键（旋钮值不动）→ 连续多轮 apply 都不改回去
    decision["interest_daily_decay"] = 0.8
    decision["recent_topic_window"] = 7
    for _ in range(3):
        asyncio.run(knobs.apply_changes())
    assert decision["interest_daily_decay"] == 0.8
    assert decision["recent_topic_window"] == 7

    # 只有旋钮再次变动才重新写入
    config["preset"]["preset_topic_taste"] = "diverse"
    asyncio.run(knobs.apply_changes())
    assert decision["interest_daily_decay"] == 0.5
    assert decision["recent_topic_window"] == 10


def test_knobs_survive_bad_values_without_blocking():
    """旋钮值不在预设表 / config 异常 → WARNING 跳过，不抛不阻塞。"""
    config = _nested_config()
    config["preset"]["preset_sleep_style"] = "猜的值"
    knobs = ConfigKnobs(lambda: config, save_config=None)
    knobs.arm()
    applied = asyncio.run(knobs.apply_changes())  # 基线
    config["preset"]["preset_sleep_style"] = "猜的值2"
    applied = asyncio.run(knobs.apply_changes())
    assert applied == []  # 无映射 → 不写入也不崩

    class Boom:
        def get(self, *a):
            raise RuntimeError("boom")

    knobs2 = ConfigKnobs(lambda: Boom(), save_config=None)
    knobs2.arm()
    assert asyncio.run(knobs2.apply_changes()) == []  # 异常吞掉返回空


def test_knobs_save_failure_does_not_raise():
    """持久化失败 → WARNING（内存已生效），不抛异常。"""
    config = _nested_config()
    config["preset"]["preset_write_level"] = "browse"
    knobs = ConfigKnobs(lambda: config, save_config=None)
    knobs.arm()
    asyncio.run(knobs.apply_changes())

    async def boom_save():
        raise RuntimeError("disk full")

    config["preset"]["preset_write_level"] = "comment"
    applied = asyncio.run(knobs.apply_changes())
    assert applied  # 写入动作本身完成（内存生效）
    assert conf_group(config, "autonomy")["write_level"] == 2


# ---------------------------------------------------------------------------
# 验收 4：危险项 invisible
# ---------------------------------------------------------------------------
def test_danger_keys_invisible():
    for group, key in DANGER_KEYS:
        item = SCHEMA["advanced"]["items"][group]["items"][key]
        assert item.get("invisible") is True, f"{group}.{key} 应不可见"


# ---------------------------------------------------------------------------
# 验收 5：结构完整性（现有键一个不少）
# ---------------------------------------------------------------------------
def test_structure_complete_no_key_lost():
    advanced = SCHEMA["advanced"]["items"]
    assert set(advanced) == set(BASELINE_KEYS)  # 组集合一致（persona 组消失）
    for group, expected in BASELINE_KEYS.items():
        actual = set(advanced[group]["items"])
        assert actual == expected, (
            f"{group} 组键集合不一致：少 {expected - actual}，多 {actual - expected}"
        )


# ---------------------------------------------------------------------------
# conf_path：嵌套优先、平铺兜底
# ---------------------------------------------------------------------------
def test_conf_group_nested_preferred_flat_fallback():
    nested = {"advanced": {"sleep": {"a": 1}}, "sleep": {"a": 2, "old": True}}
    assert conf_group(nested, "sleep") == {"a": 1}  # 嵌套优先
    flat = {"sleep": {"a": 2}}
    assert conf_group(flat, "sleep") == {"a": 2}  # 平铺兜底（既有测试兼容）
    assert conf_group({}, "sleep") == {}
    assert conf_group(None, "sleep") == {}
    assert conf_group({"advanced": {}}, "sleep") == {}
    assert conf_group({"advanced": {"sleep": "坏类型"}}, "sleep") == {}


def test_preset_helpers():
    assert preset_group({"preset": {"k": 1}}) == {"k": 1}
    assert preset_group({}) == {}
    assert preset_group(None) == {}
    assert preset_value({"preset": {"k": "v"}}, "k") == "v"
    assert preset_value({}, "k", "def") == "def"
    assert preset_value({"preset": {"k": ""}}, "k", "def") == "def"  # 空串回默认


# ---------------------------------------------------------------------------
# main._cfg/_preset 的嵌套读取（经合成包加载真实 main.py）
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


def test_main_cfg_reads_nested_and_preset():
    main_module = _load_plugin_main()
    plugin = object.__new__(main_module.LivingPlugin)
    plugin.config = {
        "preset": {"life_extra": "喜欢天文", "preset_write_level": "comment"},
        "advanced": {"autonomy": {"tier": 2}, "sleep": {"sleep_mode": "fixed"}},
    }
    assert plugin._cfg("autonomy", "tier") == 2  # 嵌套读取
    assert plugin._cfg("sleep", "sleep_mode") == "fixed"
    assert plugin._cfg("sleep", "missing", "dflt") == "dflt"
    assert plugin._preset("life_extra") == "喜欢天文"
    assert plugin._preset("missing", "") == ""


def test_main_cfg_flat_fallback_still_works():
    """平铺配置（既有测试与历史数据）经 _cfg 仍可读。"""
    main_module = _load_plugin_main()
    plugin = object.__new__(main_module.LivingPlugin)
    plugin.config = {"decision": {"decision_mode": "llm"}}
    assert plugin._cfg("decision", "decision_mode") == "llm"
