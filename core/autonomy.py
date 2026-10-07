"""自主活动能力档位（任务书 M3 补丁 XI-B/B2）。

tier/write_level 的读取、路径白名单校验、工具清单生成——全部纯函数，
测试可以独立覆盖。运行时接线由 `build_living_tools(tier=...)` 和
`LivingAgentLoop.run()` 在每次活动周期调用时热读。
"""

from __future__ import annotations

from typing import Any

# 档位阶梯（M23-补丁1 A1 起 shell 独占最高档，老 tier=3 不再含命令行）：
#   0 静养 = 仅自带四件套（搜索/抓取/沙箱/记忆）
#   1 观看 = + 浏览器五件套
#   2 居家 = + 工作区文件三件套（读/写/列）
#   3 自由 = 同 2（文件能力的顶，不含命令行）
#   4 命令行 = + 本机命令行（等价于把这台电脑交给她，须 write_level>=2）
TIER_NAMES = {0: "静养", 1: "观看", 2: "居家", 3: "自由", 4: "命令行"}
WRITE_LEVEL_NAMES = {0: "只读", 1: "浏览交互", 2: "轻写入", 3: "全权"}

# 红线路径前缀：无论档位多高一律拒绝写入
_PROTECTED_PREFIXES = (
    "data/config/", "data/cmd_config.json", "data/data.db",
    "astrbot/", "data/plugins/",
)


def clamp_tier(value, default: int = 1) -> int:
    """档位合法域 0-4（M23-补丁1 A2：上限 3 → 4，shell 独占第 4 档）。"""
    try:
        return max(0, min(4, int(value)))
    except (TypeError, ValueError):
        return default


def clamp_write_level(value, default: int = 0) -> int:
    try:
        return max(0, min(3, int(value)))
    except (TypeError, ValueError):
        return default


def read_tier(config: dict) -> int:
    try:
        from .conf_path import conf_group

        return clamp_tier(conf_group(config, "autonomy").get("tier", 1), 1)
    except Exception:
        return 1


def read_write_level(config: dict) -> int:
    try:
        from .conf_path import conf_group

        return clamp_write_level(
            conf_group(config, "autonomy").get("write_level", 0), 0
        )
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


def build_tool_manifest(
    tier: int,
    write_level: int,
    has_browser: bool = False,
    has_workspace: bool = False,
    has_search: bool = True,
) -> list[str]:
    """当前档位的工具名清单（日志可观测，补丁 XV 清单2 起由
    main._build_agent_tools 的档位日志使用）。

    名称必须与 build_living_tools 的实际挂载一致（一致性由
    tests/test_m3_patchXV.py 与 tests/test_m23_patch1.py 的接线测试守护）：
    browser 五件套含 click/type，工作区三件套含 list。M15-补丁3 A3 顺手
    （grep 同源问题）：has_search=False（capabilities.web_search_enabled=
    false）时不列 web_search——否则搜索关闭下"清单 vs 实际挂载"天天假报
    不一致（与 M15-补丁2 给 has_browser 做的 fail-closed 同口径）。
    M23-补丁1 A3/C2：local_shell 只在 tier>=4 且 write_level>=2 时列入，
    与 build_living_tools 的挂载条件逐字同款（shell 独占第 4 档 + 写权限
    闸门都是挂载时判断，清单才不会谎报能力）。
    """
    names: list[str] = []
    if has_search:
        names.append("web_search")
    names += ["fetch_page", "run_python", "remember"]
    if tier >= 1 and has_browser:
        names += [
            "browser_navigate", "browser_read", "browser_screenshot",
            "browser_click", "browser_type",
        ]
    if tier >= 2 and has_workspace:
        names += ["workspace_read", "workspace_write", "workspace_list"]
    if tier >= 4 and write_level >= 2:
        names += ["local_shell"]
    return names


# ---------------------------------------------------------------------------
# 写操作分级判定（补丁 XV 清单4）：write_level × action_kind 允许表。
# 这是 L2（轻写入）与 L3（全权）的第一个行为差异：评论/点赞 ≠ 发帖/私信/下单。
# ---------------------------------------------------------------------------
ACTION_KINDS = (
    "navigate", "fill", "submit_form",
    "comment", "post", "message", "purchase", "unknown",
)

# write_level → 允许的 action_kind；3 = 全权（None 表示不设限，含未标注操作）
_WRITE_LEVEL_ALLOWED: dict[int, "frozenset[str] | None"] = {
    0: frozenset(),
    1: frozenset({"navigate", "fill"}),
    2: frozenset({"navigate", "fill", "submit_form", "comment"}),
    3: None,
}


def check_action_kind(write_level: int, action_kind: str | None) -> "tuple[bool, str]":
    """写操作分级判定。返回 (是否放行, 拒绝原因)。

    保守拒绝策略：write_level < 3 时未提供 action_kind 或取值 unknown 一律
    拒绝——LLM 没标注的操作按"无法确认风险"对待，宁可让它重标一次也不
    静默放行；write_level=3（主人明示全权，见总纲 D12）不设限，未标注也
    放行。放行时第二项为空串。
    """
    allowed = _WRITE_LEVEL_ALLOWED.get(clamp_write_level(write_level, 0), frozenset())
    if allowed is None:
        return True, ""
    kind = str(action_kind or "").strip().lower()
    if not kind or kind not in ACTION_KINDS or kind == "unknown":
        return False, (
            "当前权限无法确认该操作的风险等级，已拒绝。"
            "请在调用时标明 action_kind（navigate/fill/submit_form/comment/"
            "post/message/purchase）；发帖、私信、下单等高风险动作需要主人把 "
            "write_level 调到 3。"
        )
    if kind in allowed:
        return True, ""
    return False, (
        f"当前写层级（write_level={write_level}）不允许 {kind} 这类操作，已拒绝。"
        "填表/跳转需要 write_level>=1，评论/提交表单等轻写入需要 >=2，"
        "发帖/私信/下单需要 3（需联系主人调整）。"
    )
