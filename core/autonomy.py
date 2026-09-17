"""自主活动能力档位（任务书 M3 补丁 XI-B/B2）。

tier/write_level 的读取、路径白名单校验、工具清单生成——全部纯函数，
测试可以独立覆盖。运行时接线由 `build_living_tools(tier=...)` 和
`LivingAgentLoop.run()` 在每次活动周期调用时热读。
"""

from __future__ import annotations

from typing import Any

TIER_NAMES = {0: "静养", 1: "观看", 2: "居家", 3: "自由"}
WRITE_LEVEL_NAMES = {0: "只读", 1: "浏览交互", 2: "轻写入", 3: "全权"}

# 红线路径前缀：无论档位多高一律拒绝写入
_PROTECTED_PREFIXES = (
    "data/config/", "data/cmd_config.json", "data/data.db",
    "astrbot/", "data/plugins/",
)


def clamp_tier(value, default: int = 1) -> int:
    try:
        return max(0, min(3, int(value)))
    except (TypeError, ValueError):
        return default


def clamp_write_level(value, default: int = 0) -> int:
    try:
        return max(0, min(3, int(value)))
    except (TypeError, ValueError):
        return default


def read_tier(config: dict) -> int:
    try:
        return clamp_tier(config.get("autonomy", {}).get("tier", 1), 1)
    except Exception:
        return 1


def read_write_level(config: dict) -> int:
    try:
        return clamp_write_level(config.get("autonomy", {}).get("write_level", 0), 0)
    except Exception:
        return 0


def is_path_protected(path: str) -> bool:
    normalized = str(path).replace("\\", "/").strip().lower()
    return any(normalized.startswith(p) for p in _PROTECTED_PREFIXES)


def is_write_allowed(path: str, workspace: str, write_level: int) -> bool:
    if write_level < 2:
        return False
    if is_path_protected(path):
        return False
    if write_level >= 3:
        return True
    ws = str(workspace or "").replace("\\", "/").strip().rstrip("/")
    p = str(path).replace("\\", "/").strip()
    return bool(ws) and p.startswith(ws)


def build_tool_manifest(tier: int, write_level: int, has_browser: bool = False) -> list[str]:
    """当前档位的工具名清单（日志可观测）。"""
    names = ["web_search", "fetch_page", "run_python", "remember"]
    if tier >= 1 and has_browser:
        names += ["browser_navigate", "browser_read", "browser_screenshot"]
    if tier >= 2:
        names += ["workspace_read", "workspace_write"]
    if tier >= 3:
        names += ["local_shell"]
    return names
