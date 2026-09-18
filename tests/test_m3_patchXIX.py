"""M3 补丁 XIX 测试：旋钮基线持久化——修复"热重载后旋钮改动不生效"。

根因回顾：旋钮改动经"保存配置 + 热重载"生效，而 XVIII 的 arm() 用当前
配置值建基线 → 重载后基线被重置为新值 → 变化窗口在检测前关闭。
本批 arm() 优先读盘上旧基线，"模拟热重载"路径是本文件的核心测试。
"""

import asyncio
import json
from pathlib import Path

from core.conf_path import conf_group
from core.config_knobs import ConfigKnobs


def _nested_config():
    """嵌套结构的最小配置（advanced 下各组 + preset 空组）。"""
    advanced = {g: {} for g in ("autonomy", "decision", "output_gate",
                                "sleep", "capabilities", "memory", "model")}
    return {"preset": {}, "advanced": advanced}


def _baseline_file(state: Path, knobs: dict) -> None:
    state.write_text(
        json.dumps({"last_knobs": knobs}, ensure_ascii=False), encoding="utf-8"
    )


def _stored_baseline(state: Path) -> dict:
    return json.loads(state.read_text(encoding="utf-8"))["last_knobs"]


# ---------------------------------------------------------------------------
# 验收 1（关键）：模拟热重载——新实例 arm 读盘上旧基线，仍能检测差异并写入
# ---------------------------------------------------------------------------
def test_hot_reload_still_writes(tmp_path):
    state = tmp_path / "knobs_state.json"
    config = _nested_config()
    config["preset"]["preset_activity_level"] = "normal"

    # 第一代实例：首次引入 → 用当前值建立基线并落盘
    knobs1 = ConfigKnobs(lambda: config, state_path=state)
    knobs1.arm()
    assert _stored_baseline(state)["preset_activity_level"] == "normal"

    # GUI 改旋钮（PUT 已把它写进 config）→ 插件热重载 → 新实例
    config["preset"]["preset_activity_level"] = "quiet"
    knobs2 = ConfigKnobs(lambda: config, state_path=state)
    knobs2.arm()  # 关键：基线来自盘上（normal），不是当前值（quiet）

    applied = asyncio.run(knobs2.apply_changes())
    assert applied, "重载后必须仍能检测到'新值 vs 盘上旧值'的差异"
    decision = conf_group(config, "decision")
    assert decision["impulse_check_interval_minutes"] == 90
    assert decision["activity_probability"] == 0.4
    assert decision["daily_impulse_limit"] == 1

    # 清单 3：写入后盘上基线已同步为 quiet
    assert _stored_baseline(state)["preset_activity_level"] == "quiet"


def test_hot_reload_reverse_direction(tmp_path):
    """凛 VM 实测的反向路径：quiet → normal 也生效。"""
    state = tmp_path / "knobs_state.json"
    config = _nested_config()
    config["preset"]["preset_activity_level"] = "quiet"
    ConfigKnobs(lambda: config, state_path=state).arm()

    config["preset"]["preset_activity_level"] = "normal"
    knobs2 = ConfigKnobs(lambda: config, state_path=state)
    knobs2.arm()
    asyncio.run(knobs2.apply_changes())
    decision = conf_group(config, "decision")
    assert decision["impulse_check_interval_minutes"] == 45
    assert decision["daily_impulse_limit"] == 0


# ---------------------------------------------------------------------------
# 验收 2：首次引入不误写
# ---------------------------------------------------------------------------
def test_first_run_builds_baseline_without_writing(tmp_path):
    state = tmp_path / "knobs_state.json"
    config = _nested_config()
    config["preset"]["preset_write_level"] = "browse"

    knobs = ConfigKnobs(lambda: config, state_path=state)
    knobs.arm()  # 无盘基线 → 当前值建立并落盘
    assert asyncio.run(knobs.apply_changes()) == []  # 无变化不写入
    assert conf_group(config, "autonomy") == {}  # 底层键未被碰
    assert state.exists()  # 基线已落盘


# ---------------------------------------------------------------------------
# 验收 3：不重复写入
# ---------------------------------------------------------------------------
def test_no_rewrite_after_baseline_synced(tmp_path):
    state = tmp_path / "knobs_state.json"
    config = _nested_config()
    config["preset"]["preset_free_activity"] = "on"
    knobs = ConfigKnobs(lambda: config, state_path=state)
    knobs.arm()
    config["preset"]["preset_free_activity"] = "off"
    first = asyncio.run(knobs.apply_changes())
    assert first  # 写入一次
    decision = conf_group(config, "decision")
    assert decision["free_activity_enabled"] is False

    # 同实例再 apply：内存基线已同步 → 无变化
    assert asyncio.run(knobs.apply_changes()) == []
    # 模拟又一次重载：新实例 arm 读到的盘基线已是 off → 仍无变化
    knobs2 = ConfigKnobs(lambda: config, state_path=state)
    knobs2.arm()
    assert asyncio.run(knobs2.apply_changes()) == []
    assert decision["free_activity_enabled"] is False


# ---------------------------------------------------------------------------
# 验收 4：落盘失败降级（WARNING + 内存基线继续，不抛异常）
# ---------------------------------------------------------------------------
def test_save_state_failure_degrades_to_memory(tmp_path):
    state_dir = tmp_path / "blocker"
    state_dir.mkdir()  # state_path 指向目录 → write_text 必然抛 OSError
    config = _nested_config()
    config["preset"]["preset_sleep_style"] = "fixed"

    knobs = ConfigKnobs(lambda: config, state_path=str(state_dir))
    knobs.arm()  # 落盘失败 → 降级内存基线，不抛
    assert knobs._last_knobs is not None

    config["preset"]["preset_sleep_style"] = "autonomous"
    applied = asyncio.run(knobs.apply_changes())  # 写入后落盘再失败，仍不抛
    assert applied
    assert conf_group(config, "sleep")["sleep_mode"] == "autonomous"


def test_load_state_corrupt_falls_back(tmp_path):
    """基线文件损坏 → WARNING + 按"无基线"用当前值重建，不抛不误写。"""
    state = tmp_path / "knobs_state.json"
    state.write_text("{bad json", encoding="utf-8")
    config = _nested_config()
    config["preset"]["preset_talk_frequency"] = "rare"

    knobs = ConfigKnobs(lambda: config, state_path=state)
    knobs.arm()
    assert asyncio.run(knobs.apply_changes()) == []
    assert conf_group(config, "output_gate") == {}


# ---------------------------------------------------------------------------
# 验收 5：进程重启存活（新实例 arm 读到的是文件值，不是当前值）
# ---------------------------------------------------------------------------
def test_baseline_survives_restart(tmp_path):
    state = tmp_path / "knobs_state.json"
    config = _nested_config()
    config["preset"]["preset_capability_tier"] = "watch"
    ConfigKnobs(lambda: config, state_path=state).arm()

    # 重启期间用户改了旋钮（config 已含新值），进程重启
    config["preset"]["preset_capability_tier"] = "full"
    knobs2 = ConfigKnobs(lambda: config, state_path=state)
    knobs2.arm()
    assert knobs2._last_knobs["preset_capability_tier"] == "watch"  # 文件值
    # 且后续 apply 能据此写入
    asyncio.run(knobs2.apply_changes())
    assert conf_group(config, "autonomy")["tier"] == 3


# ---------------------------------------------------------------------------
# 边界：盘上基线缺新旋钮键 → 用当前值补齐，不视为"用户刚改"
# ---------------------------------------------------------------------------
def test_new_knob_key_filled_from_current_not_treated_as_change(tmp_path):
    state = tmp_path / "knobs_state.json"
    _baseline_file(state, {"preset_sleep_style": "autonomous"})  # 只有旧旋钮

    config = _nested_config()
    config["preset"]["preset_sleep_style"] = "autonomous"
    config["preset"]["preset_write_level"] = "comment"  # 后加的旋钮

    knobs = ConfigKnobs(lambda: config, state_path=state)
    knobs.arm()
    assert knobs._last_knobs["preset_sleep_style"] == "autonomous"  # 盘上值
    assert knobs._last_knobs["preset_write_level"] == "comment"  # 当前值补齐
    assert asyncio.run(knobs.apply_changes()) == []  # 新键不算"刚改"，不写入
    # 补齐后的完整基线已同步回盘
    assert _stored_baseline(state)["preset_write_level"] == "comment"


def test_arm_without_state_path_keeps_memory_semantics(tmp_path):
    """state_path=None（XVIII 既有用法）：纯内存基线，行为不变。"""
    config = _nested_config()
    config["preset"]["preset_sleep_style"] = "fixed"
    knobs = ConfigKnobs(lambda: config, state_path=None)
    knobs.arm()
    assert asyncio.run(knobs.apply_changes()) == []
    config["preset"]["preset_sleep_style"] = "autonomous"
    assert asyncio.run(knobs.apply_changes())
    assert conf_group(config, "sleep")["sleep_mode"] == "autonomous"
