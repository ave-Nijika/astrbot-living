"""Negotiator——双模型协商（M32+M33-补丁1 第二批 C 组）。

输出质检的升级形态：判断模型（小）只提意见（问题 + 建议），不再给
"修正后的全文"直接替换；聊天模型（大）收到意见后**自己判断**——
认可就自己重写一版，不认可就说明理由要求放行。小模型不得压制大模型
（第二批立身之本）。

流程（任务书 3.1）：
  ① 聊天模型产出回复 → 调用方按住不发（输出钩子在 on_agent_done，
     此时回复已生成、尚未发送——"按住"就是本协程在钩子内 await 完成）
  ② 判断模型质检 → 通过 → 直接放行（零额外模型调用）
  ③ 不通过 → 意见交给聊天模型（原 system / 原 contexts / 原 provider，
     材料来自调用方注入的 prefix_getter——M20 聊天前缀缓存）自辩：
       认可 → 重新生成 →（可选再质检一次，negotiate_recheck）→ 放行
       不认可 → 原版放行
  ④ 兜底：任何失败（质检失败/材料缺失/调用失败/解析失败）一律放行
     原版（M31 红线延续：不能丢话）；总超时由调用方 wait_for 包裹。

"不留痕"（3.4）：本模块不写任何存储——不碰会话历史（contexts 用
deepcopy，且只读传入）、不碰记忆、不外发；唯一落点是 judge_records
（内部记录，摘要 80 字，不含被销毁版本全文）。被否/被替换的旧版本只
存在于本协程的局部变量里，协程返回即销毁。

设计约束：
- 不直接依赖 AstrBot provider：重写调用经注入的 chat_call（main 侧
  固定 context.llm_generate 直连——不经 agent 流程与钩子，天然不递
  归、不写历史）；测试可注入 mock；
- 判断侧复用 OutputJudge.check_output（含记录、超时、防扮演框架），
  本模块不另造判断调用。
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any, Awaitable, Callable

from astrbot.api import logger

from .judge import DEFAULT_PROMPT_NEGOTIATE
from .prompts import render_template

# chat_call: async (instruction, system_prompt, contexts, provider_id) -> str | None
ChatCall = Callable[..., Awaitable[str | None]]
# prefix_getter: async (umo) -> {"system_prompt": str, "contexts": list,
#                                "provider_id": str} | None（TTL 已校验）
PrefixGetter = Callable[[str], Awaitable[dict | None]]


def parse_negotiation(raw: Any) -> dict | None:
    """解析聊天模型的自辩输出：{"accept": bool, "reply"/"reason"}。

    宽容解析（与 judge._parse_verdict 同款先例）：剥 ``` 围栏、正则抓
    最外层 JSON。解析失败返回 None——调用方按"放行原版"处理（fail-safe
    取放行：意图不明时不能拿小模型的意见替大模型改话，原版绝不能丢）。"""
    if not raw:
        return None
    cleaned = str(raw).strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z]*\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned.strip())
    match = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    accept = data.get("accept")
    if not isinstance(accept, bool):
        return None
    return {
        "accept": accept,
        "reply": str(data.get("reply") or "").strip(),
        "reason": str(data.get("reason") or "").strip(),
    }


class Negotiator:
    """双模型协商编排。negotiate() 绝不抛异常、绝不返回空——任何路径都
    返回一条可发送的文本（原版或重写版）。"""

    def __init__(
        self,
        *,
        judge: Any,
        chat_call: ChatCall,
        prefix_getter: PrefixGetter,
    ) -> None:
        self._judge = judge
        self._chat_call = chat_call
        self._prefix_getter = prefix_getter

    async def negotiate(
        self, reply_text: str, umo: str = "", persona_text: str = ""
    ) -> str:
        """对一条按住的回复走完整协商。返回最终应发送的文本。

        调用方负责总超时（asyncio.wait_for + judge.negotiate_timeout_
        seconds，到点放行当前版本）——本方法内部各步已有各自超时
        （check=timeout_output_seconds；重写调用无独立超时，靠总超时兜）。"""
        original = str(reply_text or "").strip()
        if not original:
            return str(reply_text or "")

        # ② 质检（复用 check_output：内部已带超时/记录/INFO 日志）
        check = await self._judge.check_output(
            original, [], side="output", persona_text=persona_text
        )
        if check is None:
            # 质检失败/超时/解析失败：没有任何意见可转交 → 放行原版
            logger.info("[Judge] 协商：质检未得出结论，放行原版")
            return original
        if check.get("ok"):
            return original  # 通过 → 直接放行（check_output 已 INFO）

        note = str(check.get("note") or "").strip()
        # ③ 意见交聊天模型自辩。材料（原 system/原历史/原 provider）来自
        # 前缀缓存；取不到 → 没法让它"在自己的完整上下文里"重写，放行
        prefix = await self._safe_prefix(umo)
        if prefix is None:
            logger.info("[Judge] 协商：缺重写材料（前缀缓存无该会话），放行原版")
            self._judge.record_negotiation(original, "material_missing: 放行原版")
            return original

        instruction = render_template(
            self._judge.prompt_negotiate(),
            {"reply_text": original, "note": note or "（质检认为有问题，未给具体意见）"},
            name="judge.prompt_negotiate",
            default=DEFAULT_PROMPT_NEGOTIATE,
        )
        try:
            raw = await self._chat_call(
                instruction,
                str(prefix.get("system_prompt") or "") or None,
                prefix.get("contexts") or [],
                str(prefix.get("provider_id") or ""),
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            # 双层防御：main 侧 chat_call 已兜异常，这里再兜一层——
            # negotiate 的契约是绝不抛异常（任何路径都返回可发送文本）
            logger.info(f"[Judge] 协商：重写调用异常，放行原版（{e}）")
            self._judge.record_negotiation(original, "chat_error: 放行原版")
            return original
        if not raw:
            logger.info("[Judge] 协商：重写调用失败或空输出，放行原版")
            self._judge.record_negotiation(original, "chat_error: 放行原版")
            return original
        parsed = parse_negotiation(raw)
        if parsed is None:
            logger.info("[Judge] 协商：自辩输出形态异常，放行原版")
            self._judge.record_negotiation(original, "parse_failed: 放行原版")
            return original

        if not parsed["accept"]:
            # 不认可 → 必须放行原版（红线 2：小模型不得压制大模型）
            reason = parsed["reason"] or "（未给理由）"
            logger.info(f"[Judge] 协商：聊天模型不接受意见（{reason}），放行原版")
            self._judge.record_negotiation(
                original, f"rejected: {reason}", rewrote=False
            )
            return original

        rewritten = parsed["reply"]
        if not rewritten or rewritten == original:
            logger.info("[Judge] 协商：认可但重写为空/与原版相同，放行原版")
            self._judge.record_negotiation(
                original, "empty_rewrite: 放行原版", rewrote=False
            )
            return original

        # 可选乙方案：重写后再质检一次；仍不通过 → 放行原版（不再循环）
        if getattr(self._judge, "negotiate_recheck", lambda: False)():
            recheck = await self._judge.check_output(
                rewritten, [], side="output", persona_text=persona_text
            )
            if recheck is None or not recheck.get("ok"):
                logger.info("[Judge] 协商：重写版复检未通过，放行原版")
                self._judge.record_negotiation(
                    original, "recheck_failed: 放行原版", rewrote=False
                )
                return original

        # 认可 + 有效重写 → 旧版本原地销毁（局部变量出栈即弃，不落库
        # 不发送，记录里只有摘要）
        logger.info("[Judge] 协商：聊天模型认可意见，已重写（旧版本已弃）")
        self._judge.record_negotiation(
            original, f"accepted: {note}", rewrote=True
        )
        return rewritten

    async def _safe_prefix(self, umo: str) -> dict | None:
        """前缀材料获取的异常兜底（取不到/抛错都按无材料放行处理）。"""
        try:
            prefix = await self._prefix_getter(umo)
            return prefix if isinstance(prefix, dict) else None
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.debug(f"[Judge] 协商材料获取失败（按无材料处理）: {e}")
            return None
