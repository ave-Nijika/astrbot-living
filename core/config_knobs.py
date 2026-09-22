"""新手旋钮（补丁 XVIII 配置分层）：批量预设器。

语义（任务书 2.2）：
- 旋钮只在"被改动"时把映射值**一次性写入**对应底层键（advanced 下）；
- **不持续覆盖**：专家随后在高级组微调底层键，旋钮不会把它改回去——
  写入的触发条件是"旋钮值与上次已知值不同"，而不是"旋钮值≠底层值"；
- 旋钮的当前值只记录用户上次的选择（回显用），不参与运行时计算；
- 写入失败/无映射 → WARNING，绝不阻塞配置保存与主流程。

监视方式：main 起独立周期任务调 apply_changes()（纯内存比对，同
LivingLoop 配置 watcher 的 5s 节奏，零 LLM 零网络）。
"""

from __future__ import annotations

from typing import Any, Callable

from astrbot.api import logger

from .conf_path import conf_group, preset_group

# 直通键：值就是 model.provider_id 本身（不经过预设表）
DIRECT_KNOB = "preset_model"

# 旋钮 → 选项 → 底层键写入（组名 → {键: 值}）。任务书 2.2 映射表定稿。
KNOB_PRESETS: dict[str, dict[str, dict[str, dict[str, Any]]]] = {
    # M6-补丁1：作息旋钮随 fixed 机制移除——autonomous 是唯一睡眠行为，
    # 作息旋钮无映射目标
    "preset_activity_level": {
        "quiet": {"decision": {
            "impulse_check_interval_minutes": 90,
            "activity_probability": 0.4,
            "daily_impulse_limit": 1,
        }},
        "normal": {"decision": {
            "impulse_check_interval_minutes": 45,
            "activity_probability": 0.8,
            "daily_impulse_limit": 0,
        }},
        "active": {"decision": {
            "impulse_check_interval_minutes": 20,
            "activity_probability": 1.0,
            "daily_impulse_limit": 0,
        }},
    },
    "preset_talk_frequency": {
        "rare": {"output_gate": {
            "daily_message_limit": 3,
            "message_min_interval_minutes": 90,
        }},
        "normal": {"output_gate": {
            "daily_message_limit": 10,
            "message_min_interval_minutes": 30,
        }},
        "often": {"output_gate": {
            "daily_message_limit": 30,
            "message_min_interval_minutes": 15,
        }},
    },
    "preset_capability_tier": {
        "watch": {"autonomy": {"tier": 1}},
        "home": {"autonomy": {"tier": 2}},
        "full": {"autonomy": {"tier": 3}},
    },
    "preset_write_level": {
        "read": {"autonomy": {"write_level": 0}},
        "browse": {"autonomy": {"write_level": 1}},
        "comment": {"autonomy": {"write_level": 2}},
        "full": {"autonomy": {"write_level": 3}},
    },
    "preset_topic_taste": {
        "focused": {"decision": {
            "interest_daily_decay": 0.95,
            "recent_topic_window": 3,
            "exploration_trigger": 5,
        }},
        "balanced": {"decision": {
            "interest_daily_decay": 0.7,
            "recent_topic_window": 6,
            "exploration_trigger": 3,
        }},
        "diverse": {"decision": {
            "interest_daily_decay": 0.5,
            "recent_topic_window": 10,
            "exploration_trigger": 2,
        }},
    },
    "preset_free_activity": {
        "on": {"decision": {"free_activity_enabled": True}},
        "off": {"decision": {"free_activity_enabled": False}},
    },
    "preset_decision_mode": {
        "economy": {"decision": {"decision_mode": "rules"}},
        "normal": {"decision": {"decision_mode": "hybrid"}},
        "rich": {"decision": {"decision_mode": "llm"}},
    },
}


def apply_knob_value(config: dict, name: str, value: Any) -> str | None:
    """把单个旋钮值按映射写入底层键（M5 补丁 1 起为面板 API 共用）。

    返回写入描述；旋钮值不在预设表返回 None（调用方决定如何提示）。
    preset_model 直通 advanced.model.provider_id。只改内存，不落盘。
    """
    if name == DIRECT_KNOB:
        conf_group(config, "model")["provider_id"] = str(value or "")
        return f"{name}={value!r} → model.provider_id"
    mapping = (KNOB_PRESETS.get(name) or {}).get(value)
    if not mapping:
        return None
    for group, keys in mapping.items():
        # conf_group 返回的是 config 内层组 dict 的引用，原地改即写入
        conf_group(config, group).update(keys)
    return (
        f"{name}={value!r} → "
        + "; ".join(
            f"{group}.{key}={val!r}"
            for group, keys in mapping.items()
            for key, val in keys.items()
        )
    )


class ConfigKnobs:
    """旋钮监视与写入。arm() 取基线，apply_changes() 处理增量。

    基线持久化（补丁 XIX）：旋钮改动正是通过"保存配置 + 热重载"生效的，
    若基线只存内存，重载后 arm() 会用已含新值的当前配置重建基线，变化
    窗口在检测前就关闭（补丁 XVIII 的缺陷）。因此基线落盘
    knobs_state.json：跨重载/重启存活，arm() 优先读盘上旧基线，才能比出
    "新值 vs 旧值"的差异并写入。

    state_path=None 时退化为纯内存基线（兼容旧测试与不落盘场景）。
    """

    def __init__(
        self,
        config_getter: Callable[[], Any],
        save_config: Callable[[], Any] | None = None,
        state_path: Any = None,
    ) -> None:
        self._config_getter = config_getter
        self._save = save_config  # async () -> None；None=仅改内存
        self._state_path = state_path
        # 上次已知旋钮值；None=尚未 arm（首次 apply 只记基线不写入）
        self._last_knobs: dict[str, Any] | None = None

    @staticmethod
    def snapshot_knobs(config: Any) -> dict[str, Any]:
        """当前全部旋钮值（preset 组里我们关心的那几个键）。"""
        preset = preset_group(config)
        names = set(KNOB_PRESETS) | {DIRECT_KNOB}
        return {name: preset.get(name) for name in sorted(names)}

    # ------------------------------------------------------------------
    # 基线持久化（补丁 XIX）：任何读写异常都降级为 WARNING + 内存基线，
    # 绝不抛到主流程
    # ------------------------------------------------------------------
    def _load_state(self) -> "dict[str, Any] | None":
        """读盘上基线；无文件/损坏/格式不对 → None（走首次建立分支）。"""
        if not self._state_path:
            return None
        try:
            from pathlib import Path
            import json

            path = Path(self._state_path)
            if not path.exists():
                return None
            state = json.loads(path.read_text(encoding="utf-8"))
            knobs = state.get("last_knobs") if isinstance(state, dict) else None
            if isinstance(knobs, dict):
                return knobs
            logger.warning(f"[Knobs] 基线文件格式不对（忽略）: {self._state_path}")
            return None
        except (ValueError, OSError) as e:
            logger.warning(f"[Knobs] 基线文件读取失败，退回内存基线: {e}")
            return None

    def _save_state(self, knobs: dict[str, Any]) -> None:
        """基线落盘；失败只 WARNING（下次重载可能重复写入一次，可接受）。"""
        if not self._state_path:
            return
        try:
            from pathlib import Path
            import json

            path = Path(self._state_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps({"last_knobs": knobs}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError as e:
            logger.warning(f"[Knobs] 基线落盘失败（内存基线继续）: {e}")

    def arm(self) -> None:
        """取基线（补丁 XIX 语义）：

        - 盘上有基线 → 用它（关键：重载后仍能比出"当前新值 vs 盘上旧值"）。
          盘上没有的新旋钮键用当前值补齐——新键不算"用户刚改"；
        - 盘上无基线（首次引入/全新环境）→ 用当前值建立并落盘，不触发写入。
        """
        current = self.snapshot_knobs(self._config_getter())
        stored = self._load_state()
        if stored is None:
            self._last_knobs = current
            self._save_state(current)
            return
        merged = dict(current)
        for name, value in stored.items():
            if name in merged:
                merged[name] = value
        self._last_knobs = merged
        self._save_state(merged)  # 幂等：把补齐的新键同步回盘

    async def apply_changes(self) -> list[str]:
        """检测旋钮变化 → 按映射写底层键 → 持久化。

        返回变更描述列表（"旋钮=新值"），无变化返回空表。
        任何异常都转 WARNING 返回空表——旋钮绝不能影响主流程。
        """
        try:
            return await self._apply_changes_inner()
        except Exception as e:
            logger.warning(f"[Knobs] 旋钮检查异常（忽略本轮）: {e}")
            return []

    async def _apply_changes_inner(self) -> list[str]:
        config = self._config_getter()
        current = self.snapshot_knobs(config)
        if self._last_knobs is None:
            self._last_knobs = current
            return []
        changed = {
            name: value
            for name, value in current.items()
            if value != self._last_knobs.get(name)
        }
        if not changed:
            return []

        applied: list[str] = []
        for name, value in sorted(changed.items()):
            self._last_knobs[name] = value  # 无论成败都不重复处理本轮变更
            description = apply_knob_value(config, name, value)
            if description is None:
                logger.warning(
                    f"[Knobs] 旋钮 {name}={value!r} 不在预设表，跳过写入"
                )
                continue
            applied.append(description)

        if applied and self._save is not None:
            try:
                await self._save()
            except Exception as e:
                logger.warning(f"[Knobs] 旋钮写入持久化失败（内存已生效）: {e}")
        if applied:
            # 补丁 XIX 清单 3：写入后同步落盘基线——否则重载后 arm 会再次
            # 比出同一差异，导致重复写入
            if self._last_knobs is not None:
                self._save_state(self._last_knobs)
            logger.info(f"[Knobs] 旋钮变更已写入: {' | '.join(applied)}")
        return applied
