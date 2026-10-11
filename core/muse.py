"""心理活动通道（M39-补丁1）——「看留言 / 翻记忆」的"她真的想一想"。

这两个活动不改性质成 agent 循环：它们只需要"心里过一遍"，不需要
搜索/沙箱/文件手脚——本模块提供的是**一次轻量模型调用**：
读材料 → 产生心理活动 → 自行决定要不要开口。

输出协议（结构化 JSON，一次调用出全部结果）：
    {"thought": "心理活动（第一人称，写记忆）",
     "speak": false,
     "say": ""}          # 仅 speak=true 时有值
解析规则见 parse_muse_output——任何失败返回 None，调用方回落脚本
模板（活动照旧跑完、不空转，且绝不因解析失败而误发消息）。

两套默认提示词（peek 短程 / reminisce 长程）情绪底色不同，必须分开：
  - peek（短程）：最近还没落地的那句话——惦记、悬着、有点不安；
  - reminisce（长程）：已经沉下去的旧事——回味、想念、带点怅然。
面板键 decision.prompt_peek_muse / prompt_reminisce_muse 可配
（空值回落代码内默认，_render_intent 惯例）。
"""

from __future__ import annotations

import json
import re
from datetime import date
from typing import Any

# system 尾锚（防把 JSON 输出稀释成聊天）——人格段由调用方（LivingLoop
# 的 reflect 闭包）拼在前面的同一条 system 里，与分享改写器的组装同构
MUSE_SYSTEM_TAIL = (
    "现在不是聊天。你在独处，按下面要求完成一次自己的内心活动，"
    "最后只输出一个 JSON 对象——不要输出 JSON 以外的任何文字。"
)

# 输出协议段（两套提示词共用——这是协议不是情绪，共用不影响"分开"）
_MUSE_OUTPUT_CONTRACT = (
    "只输出一个 JSON 对象（不要代码块围栏，不要任何别的文字）：\n"
    '{"thought": "你的心理活动，第一人称，一到三句", "speak": false, "say": ""}\n'
    "想跟对方说话就把 speak 改为 true，并在 say 里写下你想说的那句话"
    "（口语，像平时聊天）；自己消化就保持 speak 为 false、say 留空。"
)

# peek（看留言·短程）——像独处久了点开聊天框看一眼有没有留言
DEFAULT_PROMPT_PEEK_MUSE = (
    "你独自待了一会儿，随手点开了和对方的聊天框，看看对方有没有给你留话。\n"
    "\n"
    "你现在的状态：{mood_line}\n"
    "{material_block}"
    "\n"
    "安静地想一想（这是你自己的内心活动，不是给任何人的汇报）：\n"
    "- 那句还没落地的话、或者一直没等来的回应，现在在你心里是什么分量？\n"
    "- 想清楚之后你可以二选一：跟对方说点什么，或者什么都不说、自己消化。\n"
    "\n"
    "像人一样：多数时候只是看一眼、想一想，然后就放下了——不是每次都要开口。\n"
    "\n" + _MUSE_OUTPUT_CONTRACT
)

# reminisce（翻记忆·长程）——像翻一本旧相册，隔着时间回看旧事
DEFAULT_PROMPT_REMINISCE_MUSE = (
    "你独自待着，随手翻起了以前记下的事，像翻一本旧相册。\n"
    "\n"
    "你现在的状态：{mood_line}\n"
    "{material_block}"
    "\n"
    "慢慢回味（这是你自己的内心活动，不是给任何人的汇报）：\n"
    "- 翻到的这段旧事，隔着现在这段时间看，是什么滋味？\n"
    "- 想清楚之后你可以二选一：跟对方分享点什么，或者只是自己想想、把相册合上。\n"
    "\n"
    "像人一样：多数时候翻一翻、想一想，就合上了——不是每次都要找人说起。\n"
    "\n" + _MUSE_OUTPUT_CONTRACT
)

# thought 的硬上限（心理活动是一段话，不是一篇日记）
THOUGHT_MAX_CHARS = 400

# 材料块的边界标记（M7-补丁1 分隔行先例：防材料被当成对方发来的消息）
MATERIAL_BEGIN = "--- 材料开始 ---"
MATERIAL_END = "--- 材料结束 ---"

# reminiscce 候选池大小：多捞一些才有"够旧"可挑（旧的一批里随机）
MEMORY_CANDIDATES = 12

# 记忆正文进材料的截断长度（够她"想起点什么"，又不至于把 prompt 撑爆）
MEMORY_SNIPPET_CHARS = 300

# 活动经历/直塞记忆的日期前缀形态（reminisce 挑"够旧"的判据）
_DAY_PREFIX_RE = re.compile(r"^\s*(\d{1,2})月(\d{1,2})日")


def parse_muse_output(text: str) -> dict | None:
    """轻调用产出的结构化解析（纯函数，无副作用）。

    接受：裸 JSON、markdown 围栏包裹、JSON 前后带解释文字。
    规则（宁可不说不说错）：
    - thought 缺失/空白 → None（无心理活动 = 解析失败，回落模板）；
    - speak 宽容取值（bool；字符串 "true"/"yes"/"1"）；其余一律 False；
    - speak=true 但 say 空白 → 视为不想说（say=None）——模型自相矛盾
      时按"自己消化"处理，绝不让半截意愿变成发送；
    - speak=false 时 say 有值也忽略（意愿以 speak 为准）。
    任何异常 → None。
    """
    raw = str(text or "").strip()
    if not raw:
        return None
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", raw)
        raw = re.sub(r"\s*```\s*$", "", raw)
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(raw[start : end + 1])
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    thought = str(data.get("thought") or "").strip()
    if not thought:
        return None
    speak = data.get("speak")
    if isinstance(speak, str):
        speak = speak.strip().lower() in ("true", "yes", "1")
    say = str(data.get("say") or "").strip() if speak else ""
    return {
        "thought": thought[:THOUGHT_MAX_CHARS],
        "speak": bool(speak) and bool(say),
        "say": say or None,
    }


def mood_line(ctx: Any) -> str:
    """当前心境的人话摘要（{mood_line} 占位符的值；缺失回'心情平静'）。"""
    digest = getattr(getattr(ctx, "mood", None), "digest", None)
    if callable(digest):
        try:
            return str(digest() or "").strip() or "心情平静"
        except Exception:
            pass
    return "心情平静"


def parse_day_prefix(content: str, today: date) -> date | None:
    """记忆正文开头的"X月X日"前缀解析（无年份——晚于今天按去年算）。

    活动经历与直塞记忆都带此前缀（ctx.date_prefix 惯例）；解析不出
    返回 None（该条记忆不参与"旧优先"排序，不报错）。"""
    match = _DAY_PREFIX_RE.match(str(content or ""))
    if not match:
        return None
    try:
        parsed = date(today.year, int(match.group(1)), int(match.group(2)))
    except ValueError:
        return None
    if parsed > today:
        try:
            parsed = date(today.year - 1, parsed.month, parsed.day)
        except ValueError:
            return None
    return parsed


def pick_old_memory(rows: list, rng, today: date) -> dict | None:
    """从候选里挑一条"够旧"的记忆（翻相册：旧的一批里随机）。

    带"X月X日"前缀的按距今最久优先，最旧的一半里随机（保留翻旧
    相册的随机感，不是永远翻最旧那条）；一条带日期的都没有时全体
    随机（与 M1 行为同族）。rows 空/全空白 → None。"""
    dated: list[tuple[date, dict]] = []
    undated: list[dict] = []
    for row in rows or []:
        content = str((row or {}).get("content") or "")
        if not content.strip():
            continue
        parsed = parse_day_prefix(content, today)
        if parsed:
            dated.append((parsed, row))
        else:
            undated.append(row)
    if not dated and not undated:
        return None
    if dated:
        dated.sort(key=lambda kv: kv[0])
        half = dated[: max(len(dated) // 2, 1)]
        return rng.choice(half)[1]
    return rng.choice(undated)


def render_memory_material(picked: dict, today: date) -> str:
    """reminisce 的材料块：一条旧记忆 + 距今多久 + 正文（M35 资料区口径）。

    记忆正文可能嵌着当年从网页读来的原文——按 M35 口径包进资料区
    标记（living_tools.wrap_external 同一套），不绕过。"""
    from .living_tools import wrap_external

    content = str((picked or {}).get("content") or "").strip()
    parsed = parse_day_prefix(content, today)
    if parsed is not None:
        date_line = (
            f"（这条大约是 {parsed.month}月{parsed.day}日记下的，"
            f"距今约 {(today - parsed).days} 天）"
        )
    else:
        date_line = "（这条没有记下具体日期）"
    return (
        f"{MATERIAL_BEGIN}\n"
        "这次翻到的旧记忆：\n"
        f"{date_line}\n"
        f"{wrap_external(content[:MEMORY_SNIPPET_CHARS])}\n"
        f"{MATERIAL_END}\n\n"
    )
