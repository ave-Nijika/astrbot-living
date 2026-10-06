# -*- coding: utf-8 -*-
"""M19-补丁1 schema 注入脚本（一次性开发工具，用后可删）。

从 core 模块导入默认模板常量，写入 _conf_schema.json 的新键——
默认值与代码常量逐字一致（防手工转录出错）。
"""
import io
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, r"D:\astrbot\AstrBotLauncher-0.3.0\AstrBotLauncher-0.3.0\AstrBot")

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from core.decider import (
    DEFAULT_PROMPT_DECIDE_LLM_FREE,
    DEFAULT_PROMPT_DECIDE_LLM_INTEREST,
    DEFAULT_PROMPT_DECIDE_PARAMS_BROWSE,
    DEFAULT_PROMPT_DECIDE_PARAMS_GAME,
)
from core.initiative import DEFAULT_PROMPT_LINE, DEFAULT_PROMPT_OPEN_TOPIC
from core.living_loop import (
    DEFAULT_PROMPT_DREAM,
    DEFAULT_PROMPT_FAREWELL,
    DEFAULT_PROMPT_WAKE_REPLY,
)
from core.activities import (
    DEFAULT_PROMPT_INTENT_FREE,
    DEFAULT_PROMPT_INTENT_GAME,
    DEFAULT_PROMPT_INTENT_READ,
    DEFAULT_PROMPT_INTENT_SURF,
)
from core.style_learning import DEFAULT_PROMPT_DISTILL
# 判断提示词默认值直接从 core.judge 导入（代码常量 == schema 默认，逐字一致）
from core.judge import (
    DEFAULT_INJECT_TEMPLATE,
    DEFAULT_PROMPT_INPUT,
    DEFAULT_PROMPT_OUTPUT,
)

SCHEMA_PATH = Path(__file__).resolve().parents[1] / "_conf_schema.json"


def text_key(description: str, hint: str, default: str) -> dict:
    return {
        "description": description,
        "hint": hint,
        "type": "text",
        "default": default,
        "invisible": True,
    }


def main() -> None:
    with open(SCHEMA_PATH, encoding="utf-8") as f:
        schema = json.load(f)
    adv = schema["advanced"]["items"]

    # ---- decision 组：4 选题提示词 + 4 活动意图模板 ----
    dec = adv["decision"]["items"]
    dec["prompt_decide_params_game"] = text_key(
        "选题提示词：小游戏参数（专家）",
        "hybrid/llm 档选中小游戏活动时给判断模型的 prompt（选个游戏风格）。"
        "占位符：{mood_block}=心境摘要。改坏可整段清空恢复默认。",
        DEFAULT_PROMPT_DECIDE_PARAMS_GAME,
    )
    dec["prompt_decide_params_browse"] = text_key(
        "选题提示词：冲浪/读文章参数（专家）",
        "hybrid/llm 档选中 surf 或 read 时给判断模型的 prompt（选个主题）。"
        "占位符：{action}=上网冲浪（搜索）/读一篇文章；{mood_block}=心境摘要；"
        "{recent_line}=近期方向提醒（含换行，可为空）；"
        "{merge_line}=话题归并指令（可为空）；{json_spec}=输出格式说明。",
        DEFAULT_PROMPT_DECIDE_PARAMS_BROWSE,
    )
    dec["prompt_decide_llm_free"] = text_key(
        "选题提示词：全 LLM 决策·自由局（专家）",
        "decision_mode=llm 且掷中自由局配额时的选题 prompt（撤除兴趣牵引）。"
        "占位符：{mood_block}=心境摘要（自由局已切兴趣行）；"
        "{memory_block}=近期记忆清单；{recent_section}=近期话题段（首尾含换行，"
        "可为空）；{activity_lines}=可选活动清单；{exploration_line}=探索强制指令"
        "（可为空）；{merge_line}=话题归并指令（可为空）；{json_spec}=输出格式说明。",
        DEFAULT_PROMPT_DECIDE_LLM_FREE,
    )
    dec["prompt_decide_llm_interest"] = text_key(
        "选题提示词：全 LLM 决策·兴趣局（专家）",
        "decision_mode=llm 且兴趣局时的选题 prompt（对照基线）。占位符同自由局，"
        "但 {mood_block} 是完整心境摘要（含兴趣行）。",
        DEFAULT_PROMPT_DECIDE_LLM_INTEREST,
    )
    dec["prompt_intent_surf"] = text_key(
        "活动意图：上网冲浪（专家）",
        "surf 活动 agent 模式给执行模型的意图说明。占位符：{topic_line}=主题方向"
        "（决策参数，可为空）；{avoid_line}=近期话题避开指令（可为空）。",
        DEFAULT_PROMPT_INTENT_SURF,
    )
    dec["prompt_intent_read"] = text_key(
        "活动意图：读文章（专家）",
        "read 活动 agent 模式的意图说明。占位符：{topic_line}=主题方向（可为空）；"
        "{avoid_line}=近期话题避开指令（可为空）。",
        DEFAULT_PROMPT_INTENT_READ,
    )
    dec["prompt_intent_game"] = text_key(
        "活动意图：写小游戏（专家）",
        "game 活动 agent 模式的意图说明。占位符：{style_line}=风格想法（可为空）。",
        DEFAULT_PROMPT_INTENT_GAME,
    )
    dec["prompt_intent_free"] = text_key(
        "活动意图：自由活动（专家）",
        "free 活动 agent 模式的意图说明。占位符：{extra}=方向偏好（可为空，自带"
        "括号）；{tools_line}=当前可用工具清单；{closing}=收尾指令（随工具有无切换）。",
        DEFAULT_PROMPT_INTENT_FREE,
    )

    # ---- initiative 组：2 键 ----
    ini = adv["initiative"]["items"]
    ini["prompt_open_topic"] = text_key(
        "话题提取提示词（专家）",
        "open_topic 来源从最近聊天里挑'还没聊完的话题'用的 prompt。占位符："
        "{context_block}=聊天记录节选（每行一条，只含角色与内容）。",
        DEFAULT_PROMPT_OPEN_TOPIC,
    )
    ini["prompt_line"] = text_key(
        "搭话台词生成提示词（专家）",
        "主动搭话台词的生成 prompt。占位符：{now_text}=当前时间（X月X日 HH:MM）；"
        "{material}=念头事由；{skip_rule}=终审自查指令（final_review_enabled 控制，"
        "可为空）。",
        DEFAULT_PROMPT_LINE,
    )

    # ---- sleep 组：3 键 ----
    slp = adv["sleep"]["items"]
    slp["prompt_farewell"] = text_key(
        "晚安生成提示词（专家）",
        "farewell_mode=llm 时的晚安 prompt（她自行斟酌说不说）。占位符："
        "{now_text}=当前时间（YYYY-MM-DD HH:MM）；{mood_digest}=心境摘要；"
        "{chat_block}=当天聊天回顾段。",
        DEFAULT_PROMPT_FAREWELL,
    )
    slp["prompt_wake_reply"] = text_key(
        "醒来补回复提示词（专家）",
        "pending_reply_enabled 开启后，睡醒对睡眠期消息的回/不回判断 prompt。"
        "占位符：{messages_block}=睡眠期消息清单；{sleep_hours}=睡眠时长；"
        "{now_text}=当前时间；{mood_section}=心境段（可为空）。输出协议"
        "（SKIP/REPLY/BRIEF）是代码解析依赖的，改协议措辞会导致判读失效。",
        DEFAULT_PROMPT_WAKE_REPLY,
    )
    slp["prompt_dream"] = text_key(
        "梦话生成提示词（专家）",
        "醒来低概率说梦话的 prompt。占位符：{fragments_block}=记忆碎片清单；"
        "{style_section}=风格学习语气参考（可为空）。",
        DEFAULT_PROMPT_DREAM,
    )

    # ---- style_learning 组：1 键 ----
    sty = adv["style_learning"]["items"]
    sty["prompt_distill"] = text_key(
        "风格提炼提示词（专家）",
        "从读到的文本一次完成 AI 判定与六维提炼的 prompt。占位符：{source_note}="
        "来源备注（空时代码兜底'网页'）；{material}=学习材料正文。输出 JSON 协议"
        "（kind/source/dims 六键）是解析依赖的，别改结构。",
        DEFAULT_PROMPT_DISTILL,
    )

    # ---- judge 组（A/B/C 组配置，M19-补丁1 三档判断模型）----
    adv["judge"] = {
        "description": "判断模型（小模型辅助：输入建议 + 输出检查，对抗输出惯性）",
        "type": "object",
        "items": {
            "mode": {
                "description": "判断模型档位",
                "hint": "off=关闭（默认，零调用零注入零行为变化）；local=本地小模型"
                "推理（尚未实现，选中仅占位，不工作）；api=云端 API（用已配置的"
                "provider，建议选个便宜快速的小模型）。",
                "type": "string",
                "options": ["off", "local", "api"],
                "default": "off",
                "invisible": True,
            },
            "provider_id": {
                "description": "判断模型 provider id",
                "hint": "api 档使用的 provider（面板用下拉选择；此处也可手填）。"
                "留空或填了不存在的 id = 本轮不判断（记 WARNING，不影响聊天）。",
                "type": "string",
                "default": "",
                "invisible": True,
            },
            "local_model_path": {
                "description": "本地模型路径（预留）",
                "hint": "local 档预留键：本地权重文件路径。本地推理尚未实现，"
                "本键暂不生效。",
                "type": "string",
                "default": "",
                "invisible": True,
            },
            "local_backend": {
                "description": "本地推理后端（预留）",
                "hint": "local 档预留键：推理后端类型（如 llama.cpp / ollama）。"
                "本地推理尚未实现，本键暂不生效。",
                "type": "string",
                "default": "",
                "invisible": True,
            },
            "context_messages": {
                "description": "输入判断携带的上文条数",
                "hint": "判断'这条消息该用什么模式回'时附带的最近聊天条数"
                "（0-12，默认 6——五到十条聊天记录就够）。",
                "type": "int",
                "default": 6,
                "invisible": True,
            },
            "min_interval_seconds": {
                "description": "输入判断最小间隔（秒）",
                "hint": "两次输入判断的最小间隔，间隔内的消息直接跳过判断"
                "（防刷屏式连环消息每条都烧一次判断）。默认 20。",
                "type": "int",
                "default": 20,
                "invisible": True,
            },
            "timeout_seconds": {
                "description": "判断调用超时（秒）",
                "hint": "输入/输出判断的单次调用超时。超时按'本轮不判断'处理，"
                "主回复照常走。默认 6——判断模型应该又小又快，超过 6 秒不如不判。",
                "type": "int",
                "default": 6,
                "invisible": True,
            },
            "output_action": {
                "description": "输出检查动作",
                "hint": "log_only=只记录不干预（默认，先收集几天数据看判得准不准）；"
                "rewrite=允许打回重写一次（最多 1 次、有超时、失败放行原回复，"
                "只做轻量修正不改写整条）。",
                "type": "string",
                "options": ["log_only", "rewrite"],
                "default": "log_only",
                "invisible": True,
            },
            "record_limit": {
                "description": "判断记录保留条数",
                "hint": "最近 N 条判断结果（输入摘要/判断输出/是否注入/是否打回）"
                "供面板查看，超出自动淘汰最旧的。默认 50。",
                "type": "int",
                "default": 50,
                "invisible": True,
            },
            "prompt_input": {
                "description": "输入判断提示词",
                "hint": "对主人新消息做'该用什么模式回'判断的 prompt。要求输出 "
                "JSON：{\"mode\": \"work|chat\", \"length\": \"short|normal|long\", "
                "\"tone\": \"plain|warm|playful\", \"note\": \"一句话提醒\"}。"
                "note 只允许提醒语气（如'这条可以短一点答'），不得出现具体措辞"
                "指令——小模型只给建议，不指挥输出。",
                "type": "text",
                "default": DEFAULT_PROMPT_INPUT,
                "invisible": True,
            },
            "inject_template": {
                "description": "输入判断注入模板",
                "hint": "把判断结果拼成注入块的模板（追加到请求末尾，用完即弃，"
                "不进记忆）。占位符：{mode}/{length}/{tone}=判断结果；{note}="
                "提醒语（空串则该位置自然消失）。",
                "type": "text",
                "default": DEFAULT_INJECT_TEMPLATE,
                "invisible": True,
            },
            "prompt_output": {
                "description": "输出检查提示词",
                "hint": "聊天模型回复后做质量检查的 prompt。要求输出 JSON："
                '{"ok": true} 或 {"ok": false, "note": "问题一句话", '
                '"fixed": "轻量修正后的全文"}。fixed 只允许轻量修正（删重复句/'
                "删残缺尾句/收紧超长），禁止改写内容或添加新内容。",
                "type": "text",
                "default": DEFAULT_PROMPT_OUTPUT,
                "invisible": True,
            },
        },
    }

    with open(SCHEMA_PATH, "w", encoding="utf-8") as f:
        json.dump(schema, f, ensure_ascii=False, indent=4)
        f.write("\n")
    print("schema 注入完成")
    print("decision 新键:", [k for k in dec if k.startswith("prompt_")])
    print("judge 组键数:", len(adv["judge"]["items"]))


if __name__ == "__main__":
    main()
