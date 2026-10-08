"""OutputJudge——判断模型引擎（M19-补丁1 A/B/C/E 组）。

外置的"很小的大脑"（需求原话：所有输出用完一次就要丢，相当于外置一个
很小的大脑，分工合作）：
- 输入侧（B 组）：用户发消息时判断该用什么模式回应（mode/length/tone +
  一句提醒），结果按模板拼成小段追加到请求末尾——**用完即弃**，不写记忆、
  不写会话、不进聊天历史（红线 1）；
- 输出侧（C 组）：聊天模型回复后过一遍质量检查。默认 log_only（只记录，
  先用几天真实数据看判得准不准）；可选 rewrite（最多重写 1 次、有超时、
  失败必须放行原回复，只做轻量修正）；
- 三档（A 组）：off（默认，零调用零注入零行为变化）/ local（本地推理，
  本轮只留预留位——明确提示未实现，绝不静默降级）/ api（云端 provider）。

设计约束：
- 判断 LLM 调用经注入的 llm_call（main 侧固定走 judge.provider_id，不与
  决策链混用），本模块不直接依赖 AstrBot provider——测试可注入 mock；
- 所有时限（min_interval/timeout）热读配置；任何失败/超时只记日志，
  绝不影响正常聊天（红线 6）；
- 判断记录（E1）只在插件数据目录的 judge_records.json——与记忆/图谱/
  会话存储完全分开（红线 1 的物理隔离）。
"""

from __future__ import annotations

import asyncio
import json
import re
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from astrbot.api import logger

from .prompts import read_template, render_template

JUDGE_MODES = ("off", "local", "api")
OUTPUT_ACTIONS = ("log_only", "rewrite")

# 判断输出的合法取值（越界值按中性默认处理——小模型偶尔不听话，不较真）
VALID_LENGTHS = ("short", "normal", "long")
VALID_TONES = ("plain", "warm", "playful")
VALID_CHECK_MODES = ("work", "chat")

# 判断输出的中性默认（任务书 B 组：解析失败视为"本轮不注入"）
NEUTRAL_VERDICT = {"mode": "chat", "length": "normal", "tone": "plain"}

# note 的硬上限（任务书：极短，≤40 字，只允许提醒语气）
NOTE_MAX_CHARS = 40

# rewrite 护栏（C2 轻量修正）：修正文本超出该比例视为小模型自作主张，
# 放行原回复（防"打回"变成"整条重写"）
_REWRITE_MAX_RATIO = 1.2

# 记录摘要的截断长度（面板回看不刷屏）
_SUMMARY_MAX_CHARS = 80


@dataclass
class JudgeVerdict:
    """一次输入判断的结构化建议。note 是"提醒"语气的一句话（≤40 字）。"""

    mode: str
    length: str
    tone: str
    note: str


class OutputJudge:
    """判断模型引擎。外部依赖（LLM/配置/时钟）全部注入、全部可失效。"""

    def __init__(
        self,
        config_getter: Callable[[], Any],
        llm_call: Callable[..., Any] | None,
        records_path: str | Path | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._config_getter = config_getter
        self._llm_call = llm_call  # async (prompt, system_prompt) -> str | None
        self._records_path = Path(records_path) if records_path else None
        self._clock = clock or datetime.now
        self._records: deque = deque(maxlen=1000)  # 硬顶防脏配置撑爆内存
        self._records_loaded = False
        self._last_input_judge_at: datetime | None = None
        self._local_warned = False
        self._provider_warned_at: datetime | None = None

    # ------------------------------------------------------------------
    # 配置（judge 组，热读）
    # ------------------------------------------------------------------
    def _cfg(self) -> dict:
        try:
            from .conf_path import conf_group

            return conf_group(self._config_getter() or {}, "judge")
        except Exception:
            return {}

    def _int(self, key: str, default: int, minimum: int = 0, maximum: int = 10**9) -> int:
        try:
            value = int(self._cfg().get(key, default))
        except (TypeError, ValueError):
            return default
        except Exception:
            return default
        return min(max(value, minimum), maximum)

    def mode(self) -> str:
        """三档档位（A1）：非法值一律按 off 处理（默认档 = 绝对安全）。"""
        mode = str(self._cfg().get("mode") or "off").strip().lower()
        return mode if mode in JUDGE_MODES else "off"

    def enabled(self) -> bool:
        """只有 api 档真正工作（A3：local 明确不工作，不静默降级）。"""
        return self.mode() == "api" and self._llm_call is not None

    def output_action(self) -> str:
        action = str(self._cfg().get("output_action") or "log_only").strip().lower()
        return action if action in OUTPUT_ACTIONS else "log_only"

    def min_interval_seconds(self) -> int:
        return self._int("min_interval_seconds", 20, 0, 3600)

    def timeout_seconds(self) -> float:
        return float(self._int("timeout_seconds", 6, 1, 60))

    def record_limit(self) -> int:
        return self._int("record_limit", 50, 1, 500)

    # ------------------------------------------------------------------
    # A3：local 档——明确不工作，绝不静默降级
    # ------------------------------------------------------------------
    def warn_local_once(self) -> None:
        """local 档的一次性 WARNING（面板文案 + 运行日志各一次，A3）。"""
        if self._local_warned:
            return
        self._local_warned = True
        logger.warning(
            "[Judge] 判断模型档位为 local，但本地推理尚未实现——"
            "判断模型暂不工作，目前请用 api 档"
        )

    # ------------------------------------------------------------------
    # B 组：输入侧判断
    # ------------------------------------------------------------------
    def should_skip_input(self, message_text: str, now: datetime | None = None) -> str | None:
        """B1 跳过规则（任一命中即不判断）：返回跳过原因或 None。

        命令前缀的判定在 main 钩子侧（需要 context 读全局唤醒前缀），
        这里只管档位/限频/极短消息。
        """
        if not self.enabled():
            return f"mode={self.mode()}"
        text = str(message_text or "").strip()
        if len(text) <= 3:
            return "too_short"
        now = now or self._clock()
        if self._last_input_judge_at is not None:
            elapsed = (now - self._last_input_judge_at).total_seconds()
            if elapsed < self.min_interval_seconds():
                return "min_interval"
        return None

    def _mark_input_judged(self) -> None:
        """时间戳在真正发起调用前更新（跳过/失败的消耗也算进间隔——
        连环消息下宁可少判不多判）。"""
        self._last_input_judge_at = self._clock()

    async def judge_input(
        self, message_text: str, context_lines: list[str] | None = None
    ) -> JudgeVerdict | None:
        """B 组主流程：调判断模型 → 解析 → 记录。任何失败返回 None。"""
        if not self.enabled():
            return None
        cfg = self._cfg()
        context_block = "\n".join(context_lines or []) or "（没有上文）"
        prompt = render_template(
            read_template(cfg, "prompt_input", DEFAULT_PROMPT_INPUT),
            {
                "context_block": context_block,
                "message_text": str(message_text or "").strip(),
            },
            name="judge.prompt_input",
            default=DEFAULT_PROMPT_INPUT,
        )
        self._mark_input_judged()
        try:
            raw = await asyncio.wait_for(
                self._llm_call(prompt, None), timeout=self.timeout_seconds()
            )
        except asyncio.TimeoutError:
            logger.debug("[Judge] 输入判断超时（本轮不注入，主回复照常）")
            self._add_record(
                side="input",
                input_summary=message_text,
                verdict="timeout",
                injected=False,
            )
            return None
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.debug(f"[Judge] 输入判断调用失败（本轮不注入）: {e}")
            self._add_record(
                side="input",
                input_summary=message_text,
                verdict=f"error: {e}"[:_SUMMARY_MAX_CHARS],
                injected=False,
            )
            return None
        verdict = self._parse_verdict(raw)
        if verdict is None:
            self._add_record(
                side="input",
                input_summary=message_text,
                verdict="parse_failed",
                injected=False,
            )
            return None
        # E1/E2：判断成功不在此处记录——注入与否要等钩子追加完才算数，
        # 由钩子侧 record_input_injected 记一条（避免同一轮两条冗余记录）
        logger.info(
            f"[Judge] 输入判断: mode={verdict.mode} length={verdict.length} "
            f"tone={verdict.tone} note={verdict.note or '（无）'}"
        )
        return verdict

    def build_input_injection(self, verdict: JudgeVerdict) -> str | None:
        """B4：把判断结果按 inject_template 拼成注入块（None = 本轮不注入）。

        note 在解析端已截断到 40 字；空 note 渲染后靠 strip 判空兜底。
        """
        if verdict is None:
            return None
        injection = render_template(
            read_template(self._cfg(), "inject_template", DEFAULT_INJECT_TEMPLATE),
            {
                "mode": verdict.mode,
                "length": verdict.length,
                "tone": verdict.tone,
                "note": verdict.note,
            },
            name="judge.inject_template",
            default=DEFAULT_INJECT_TEMPLATE,
        )
        injection = injection.strip()
        return injection or None

    def record_input_injected(self, message_text: str, verdict: JudgeVerdict) -> None:
        """E1：注入成功后由钩子补一条"injected=True"的记录（judge_input
        里记录的是未注入形态——注入与否要等钩子追加完才算数）。"""
        self._add_record(
            side="input",
            input_summary=message_text,
            verdict=f"{verdict.mode}/{verdict.length}/{verdict.tone}"
            + (f" note={verdict.note}" if verdict.note else ""),
            injected=True,
        )

    def _parse_verdict(self, raw: Any) -> JudgeVerdict | None:
        """T4 四态解析：合法 JSON / 非法 JSON（None）/ 缺字段（中性默认）/
        超长 note（截断 40 字）。宽容解析先例：extract_json_object。"""
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

        def pick(key: str, valid: tuple, neutral: str) -> str:
            value = str(data.get(key) or "").strip().lower()
            return value if value in valid else neutral

        note = str(data.get("note") or "").strip()
        return JudgeVerdict(
            mode=pick("mode", VALID_CHECK_MODES, NEUTRAL_VERDICT["mode"]),
            length=pick("length", VALID_LENGTHS, NEUTRAL_VERDICT["length"]),
            tone=pick("tone", VALID_TONES, NEUTRAL_VERDICT["tone"]),
            note=note[:NOTE_MAX_CHARS],
        )

    # ------------------------------------------------------------------
    # C 组：输出侧检查
    # ------------------------------------------------------------------
    async def check_output(
        self, reply_text: str, context_lines: list[str] | None = None
    ) -> dict | None:
        """C1：回复生成后过一遍判断模型。返回检查结果（None = 没判成）。

        不修改、不拦截输出——记录在案（日志 + 面板回看），供用户决定
        要不要开启 rewrite。"""
        if not self.enabled():
            return None
        text = str(reply_text or "").strip()
        if not text:
            return None
        cfg = self._cfg()
        context_block = "\n".join(context_lines or []) or "（没有上文）"
        prompt = render_template(
            read_template(cfg, "prompt_output", DEFAULT_PROMPT_OUTPUT),
            {
                "context_block": context_block,
                "reply_text": text,
            },
            name="judge.prompt_output",
            default=DEFAULT_PROMPT_OUTPUT,
        )
        try:
            raw = await asyncio.wait_for(
                self._llm_call(prompt, None), timeout=self.timeout_seconds()
            )
        except asyncio.TimeoutError:
            logger.debug("[Judge] 输出检查超时（不干预）")
            self._add_record(
                side="output", input_summary=text, verdict="timeout", injected=False
            )
            return None
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.debug(f"[Judge] 输出检查调用失败: {e}")
            self._add_record(
                side="output",
                input_summary=text,
                verdict=f"error: {e}"[:_SUMMARY_MAX_CHARS],
                injected=False,
            )
            return None
        check = self._parse_check(raw)
        if check is None:
            self._add_record(
                side="output",
                input_summary=text,
                verdict="parse_failed",
                injected=False,
            )
            return None
        logger.info(
            f"[Judge] 输出检查: ok={check['ok']} note={check['note'] or '（无）'}"
        )
        self._add_record(
            side="output",
            input_summary=text,
            verdict=f"{'ok' if check['ok'] else 'issue'}: {check['note']}",
            injected=False,
        )
        return check

    async def rewrite_output(
        self, reply_text: str, context_lines: list[str] | None = None
    ) -> str | None:
        """C2 rewrite：拿检查结果做轻量修正。返回修正文本或 None（放行原文）。

        轻量修正策略（报告说明）：修正由判断模型在同一次检查里给出
        （prompt_output 的 fixed 字段——一次调用完成判断与修正，不加第二
        轮昂贵生成）；修复护栏——fixed 为空/与原文相同/长度超过原文 1.2 倍
        一律视为修正失败放行原文（防小模型自作主张整条重写）。失败/超时
        必须放行原回复（红线 6：不能让用户收不到消息）。"""
        if not self.enabled():
            return None
        text = str(reply_text or "").strip()
        if not text:
            return None
        check = await self.check_output(text, context_lines)
        if check is None or check["ok"]:
            return None
        fixed = str(check.get("fixed") or "").strip()
        if not fixed or fixed == text:
            logger.debug("[Judge] 检查判有问题但无有效修正，放行原回复")
            return None
        if len(fixed) > len(text) * _REWRITE_MAX_RATIO:
            logger.debug(
                f"[Judge] 修正文本长度 {len(fixed)} 超过原文 {len(text)} 的"
                "1.2 倍，视为整条重写，放行原回复"
            )
            self._add_record(
                side="rewrite",
                input_summary=text,
                verdict="rejected_overlong_fix",
                injected=False,
                rewrote=False,
            )
            return None
        self._add_record(
            side="rewrite",
            input_summary=text,
            verdict=f"rewrote: {check['note']}",
            injected=False,
            rewrote=True,
        )
        return fixed

    def _parse_check(self, raw: Any) -> dict | None:
        """输出检查解析：{"ok": bool, "note": str, "fixed": str}。"""
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
        if not isinstance(data, dict) or not isinstance(data.get("ok"), bool):
            return None
        return {
            "ok": data["ok"],
            "note": str(data.get("note") or "").strip()[:NOTE_MAX_CHARS],
            "fixed": str(data.get("fixed") or "").strip() or None,
        }

    # ------------------------------------------------------------------
    # E 组：判断记录（与记忆/图谱/会话存储物理分离——红线 1）
    # ------------------------------------------------------------------
    def _add_record(
        self,
        *,
        side: str,
        input_summary: str,
        verdict: str,
        injected: bool,
        rewrote: bool = False,
    ) -> None:
        """记录一条判断结果（内存 + 落盘）。任何失败只 DEBUG——记录绝不
        影响判断主流程。"""
        try:
            self._records.appendleft(
                {
                    "ts": self._clock().strftime("%Y-%m-%d %H:%M:%S"),
                    "side": side,
                    "input_summary": str(input_summary or "").strip()[
                        :_SUMMARY_MAX_CHARS
                    ],
                    "verdict": str(verdict or "").strip()[:_SUMMARY_MAX_CHARS],
                    "injected": bool(injected),
                    "rewrote": bool(rewrote),
                }
            )
            self._save_records()
        except Exception as e:
            logger.debug(f"[Judge] 判断记录写入失败（忽略）: {e}")

    def records(self) -> list[dict]:
        """最近 N 条判断记录（新的在前，N = record_limit 热读）。

        盘上记录只在内存为空时读一次（运行中实例的内存 deque 是权威——
        先 add 后查询时不能再从盘上把同一条读回来）。"""
        if not self._records and not self._records_loaded:
            self._load_records_from_disk()
        return list(self._records)[: self.record_limit()]

    def _load_records_from_disk(self) -> None:
        self._records_loaded = True
        if self._records_path is None:
            return
        try:
            with open(self._records_path, encoding="utf-8") as f:
                rows = json.load(f)
            if isinstance(rows, list):
                for row in reversed(rows):
                    if isinstance(row, dict):
                        self._records.appendleft(row)
        except FileNotFoundError:
            pass
        except Exception as e:
            logger.debug(f"[Judge] 判断记录读取失败（按空记录继续）: {e}")

    def _save_records(self) -> None:
        if self._records_path is None:
            return
        rows = list(self._records)[: self.record_limit()]
        try:
            self._records_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._records_path, "w", encoding="utf-8") as f:
                json.dump(rows, f, ensure_ascii=False, indent=1)
        except Exception as e:
            logger.debug(f"[Judge] 判断记录落盘失败（忽略）: {e}")


# ---------------------------------------------------------------------------
# 判断提示词默认值（B4/C3：面板可编辑，judge.prompt_input / inject_template /
# judge.prompt_output；空值回落默认——share_rewrite_prompt 同口径）
# ---------------------------------------------------------------------------
DEFAULT_PROMPT_INPUT = (
    "你是聊天助手的幕后小助手，负责判断用户刚发来的这条消息适合"
    "用什么方式回应。\n\n"
    "最近聊天（旧→新）：\n{context_block}\n\n"
    "用户刚发的消息：{message_text}\n\n"
    "请只输出一个 JSON 对象，不要输出任何其他文字：\n"
    '{"mode": "work|chat", "length": "short|normal|long", '
    '"tone": "plain|warm|playful", "note": "≤40字的一句话提醒"}\n'
    "字段含义：mode=这条消息的性质（work=要办事/问信息，chat=闲聊）；"
    "length=合适回应长度（short=几个字到一句话，normal=两三句，"
    "long=可以展开聊）；tone=合适语气；note=给回复模型的提醒，"
    "只允许提醒语气（例如'这条可以短一点答'），禁止指定具体措辞。"
    "判断不了就全给中性值（chat/normal/plain），note 给空串。"
)
DEFAULT_INJECT_TEMPLATE = (
    "（内部提醒，用户和其他人都看不到这段：回复前先参考这条消息"
    "的判断建议——消息性质：{mode}，建议长度：{length}，建议语气："
    "{tone}。{note}这是参考建议，按你自己自然的表达来。）"
)
DEFAULT_PROMPT_OUTPUT = (
    "你是聊天助手的幕后质检小助手。下面是助手刚发出的回复，"
    "请检查它有没有明显的输出惯性毛病。\n\n"
    "最近聊天（旧→新）：\n{context_block}\n\n"
    "助手刚发的回复：\n{reply_text}\n\n"
    "检查点：是不是明显比平时啰嗦（同样的意思车轱辘话说好几遍）；"
    "是不是把闲聊回成了公文（列表/标题/汇报腔）；结尾是否残缺"
    "（话说一半）；是否重复了上一条回复里刚说过的话。\n\n"
    "只输出一个 JSON 对象，不要输出任何其他文字：\n"
    '{"ok": true} 或 {"ok": false, "note": "问题一句话", '
    '"fixed": "轻量修正后的全文"}\n'
    "fixed 的规则：只做轻量修正（删掉重复句/残缺尾句，收紧啰嗦段），"
    "保持原回复的语气与内容，禁止改写内容、禁止添加新内容、长度"
    "不得超过原文。没有问题就只给 {\"ok\": true}。"
)
