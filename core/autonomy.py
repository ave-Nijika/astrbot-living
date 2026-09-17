"""自主活动能力档位与工具白名单（任务书 M3 补丁 XI-B1/B3）。"""

from __future__ import annotations

from typing import Any


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


# 红线路径前缀：无论档位多高一律拒绝写入
PROTECTED_PREFIXES = (
    "data/config/", "data/cmd_config.json", "data/data.db",
    "astrbot/", "data/plugins/",
)


def is_path_protected(path: str) -> bool:
    normalized = str(path).replace("\\", "/").strip().lower()
    return any(normalized.startswith(p) for p in PROTECTED_PREFIXES)


def is_write_allowed(path: str, workspace: str, write_level: int) -> bool:
    """路径写入校验。

    write_level < 2 → False（只读/浏览交互不允许文件写入）
    红线路径 → False
    workspace 内 → True
    workspace 外且 write_level >= 3 → True
    """
    if write_level < 2:
        return False
    if is_path_protected(path):
        return False
    if write_level >= 3:
        return True
    ws = str(workspace or "").replace("\\", "/").strip().rstrip("/")
    p = str(path).replace("\\", "/").strip()
    return bool(ws) and p.startswith(ws)


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


def build_tool_manifest(tier: int, write_level: int, has_browser: bool = False) -> list[str]:
    """返回当前档位可用的工具名清单（日志可观测用）。"""
    names = ["web_search", "fetch_page", "run_python", "remember"]
    if tier >= 1 and has_browser:
        names += ["browser_navigate", "browser_read", "browser_screenshot",
                  "browser_click", "browser_type"]
    if tier >= 2:
        names += ["workspace_read", "workspace_write"]
    if tier >= 3:
        names += ["local_shell"]
    return names
