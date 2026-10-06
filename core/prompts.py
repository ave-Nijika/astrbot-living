"""面板可编辑提示词的安全渲染器（M19-补丁1 D-b）。

为什么不用 str.format：提示词模板里普遍含有 JSON 格式说明的字面花括号
（如 `只输出 JSON，格式：{"activity": "…"}`），str.format 会把它们当
占位符解析直接 KeyError；逐占位符 str.replace 则完全不受影响——字面
花括号原样保留，已知占位符精确替换。

兑底约定（任务书 D-b，用户误编辑不得导致崩溃）：
- 模板整体为空（strip 后空串）→ 回落默认模板 + WARNING（空模板几乎必然
  是误清空，全部内容消失比"恢复默认"更反直觉）；
- 模板缺已知占位符 → 对应内容段自然消失（按空串渲染，尊重用户其余编辑）
  + WARNING 指明缺失清单；
- 模板里未知形态的 {xxx} → 原样保留（与 JSON 字面花括号同一处理逻辑）。

与既有 share_rewrite_prompt 的口径一致（D-d）：配置值为空 → 用代码内
默认模板（"覆盖默认模板"语义，不叠加、不拼接）。
"""

from __future__ import annotations

from typing import Any

from astrbot.api import logger


def read_template(group_cfg: Any, key: str, default: str) -> str:
    """从配置组读一个提示词模板：空值回落默认（share_rewrite_prompt 同口径）。

    group_cfg 可为 dict 或 None；任何异常都回落默认（提示词读取失败绝不
    让调用方崩）。
    """
    try:
        raw = (group_cfg or {}).get(key)
        return str(raw) if raw else default
    except Exception:
        return default


def render_template(
    template: str, values: dict[str, Any], *, name: str = "", default: str = ""
) -> str:
    """安全渲染提示词模板（逐占位符替换，见模块 docstring）。

    Args:
        template: 用户配置的模板（可为空）。
        values: 占位符名 → 值（None 按空串渲染）。
        name: 模板标识（WARNING 里指路用，如 "decision.prompt_decide_llm_free"）。
        default: 模板为空时的回落模板。

    Returns:
        渲染后的文本（绝不抛异常——任何意外回落 default 或空串）。
    """
    try:
        text = str(template or "")
        if not text.strip():
            if default:
                logger.warning(
                    f"[Prompts] {name} 模板为空，回落默认模板"
                )
            return default
        missing = [
            key for key in values if "{" + str(key) + "}" not in text
        ]
        if missing:
            logger.warning(
                f"[Prompts] {name} 模板缺少占位符 {missing}，"
                "对应内容将不注入（在面板把占位符加回去即可恢复）"
            )
        for key, value in values.items():
            text = text.replace("{" + str(key) + "}", str(value if value is not None else ""))
        return text
    except Exception as e:
        logger.warning(f"[Prompts] {name} 模板渲染失败（回落默认）: {e}")
        return default
