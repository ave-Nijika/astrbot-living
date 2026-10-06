"""StyleLearner——风格学习系统（M17-补丁1 A 组，M20-补丁1 I/K/L/M 升级）。

她在活动里读到人类文本 → **一次** LLM 调用同时判定"是不是人写的"
（ai / uncertain 整条丢弃，主人红线：宁可少学也不让疑似 AI 的语料进来）并
提炼六维风格片段 → 存进独立**语料库** style_corpus.json（与记忆/图谱/会话
存储完全分开，不参与记忆召回——红线 2）→ 说话时按情境取 1-2 条低调注入
（"参考语气"框架，硬上限字符数——红线 1）→ 用得多权重缓升成习惯、久不用
自然衰减淘汰（A6 演化）。

M20-补丁1 分层结构（I 组）：
- **语料库**（style_corpus.json，原 style_pool.json 改名迁移）：已提炼的
  六维片段，语义与格式不变；
- **素材库**（style_materials.json）：主人手动投入的原始语料（只由面板
  写入，插件自己绝不写；未经处理的按 FIFO 优先提炼——I3）；
- **调用记录库**（feature_usage.json）：注入取用事实（K1，只记录取用，
  不记聊天内容、不进记忆）；
- **沉淀层**（style_features.json）：由每日复盘后归纳产出的稳定特征
  （K2），注入时优先带沉淀层、再用语料片段补充新鲜感。

M20-补丁1 学习触发（L 组）：不再限定 read/surf——任何活动结束时，只要
本轮真的读了网页（fetch_page 成功留档或 browser_navigate/browser_read
成功留档，时间戳 >= 活动开始）就触发；素材库有待处理项时任何触发点都
优先处理素材库。一次活动仍最多学 1 条。

设计要点：
- 成本（红线 4）：每次学习 1 次 LLM 调用（A3：判定与提炼合并）；注入
  零调用（本地加权抽样）；库空/关闭/异常全部静默（红线 5）。
- 六维（A1 硬性）：wording / syntax / thinking / emotion_style /
  interaction / avoid——thinking 与 interaction 是重点。
- 来源分层（A2）：dialogue（1.0）> article（0.5）；素材库来源加
  "人工优选"标记，权重只比 dialogue 略高（manual_weight，默认 1.2，
  内部钳位 [1.0, 1.5]，不设 2 倍以上——I4）。
- 复盘防僵化（K3）：重要度有硬上限（feature_importance_cap，默认 1.5），
  且不超过同类条目中位数的 1.5 倍——好的形成习惯，绝不独霸。
"""

from __future__ import annotations

import hashlib
import json
import random
import re
import statistics
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from astrbot.api import logger

# 六个学习维度（A1）。DIMS_ORDER 决定注入顺序——思维方式与人际互动
# 排在用词前面：主人原话"不是单单模仿一种口癖"。
DIMS_ORDER = (
    "thinking",
    "interaction",
    "syntax",
    "emotion_style",
    "wording",
    "avoid",
)

DIM_LABELS = {
    "wording": "用词",
    "syntax": "句式",
    "thinking": "思维方式",
    "emotion_style": "情绪表达",
    "interaction": "待人接物",
    "avoid": "别这样",
}

# 默认来源权重（A2；style_learning.source_weights 可配覆盖）
DEFAULT_SOURCE_WEIGHTS = {"dialogue": 1.0, "article": 0.5}

# 材料低于该字数不提炼（M20-补丁1 M2：提成配置 min_material_chars，默认不变）
MIN_MATERIAL_CHARS = 80

# 单条材料送进提示词的字符上限（M1：提成配置 material_max_chars，默认不变）
_MATERIAL_MAX_CHARS = 1200

# 单条片段注入时的字符上限（M2：提成配置 item_max_chars，默认不变）
_ITEM_MAX_CHARS = 80

# 旧库文件名（I1：一次性迁移到 style_corpus.json）
LEGACY_POOL_FILENAME = "style_pool.json"

# 沉淀层容量上限（K2：特征条数有界）
FEATURES_LIMIT = 12

# 复盘单次调用数上限（K3：每天最多 1 次复盘 + 最多 1 次归纳 ≤ 3）
REVIEW_MAX_CALLS = 3

# "立即处理"限频（J3：30 秒内只允许一次）
PROCESS_NOW_COOLDOWN_SECONDS = 30

# ---- M19-补丁1 D5：提炼提示词搬上面板（schema 键
# style_learning.prompt_distill）——默认值与搬之前的硬编码拼接逐字一致
# （T11 验证）；占位符 {source_note}（来源备注，代码端兜底 '网页'）与
# {material}（学习材料正文）。判定/解析逻辑不动。
DEFAULT_PROMPT_DISTILL = (
    "下面是你昨天在网上读到的文本片段"
    "（来源备注：{source_note}）。请完成两件事：\n\n"
    "一、判断它更可能是【人写的】还是【AI 生成的】。判断依据：\n"
    "- AI 特征：结构工整对称、排比铺陈、\"首先/其次/最后\"、无口语"
    "碎语、情绪平铺、无具体到细节的个人经历、套话（\"值得注意的是\""
    "\"让我们一起\"\"希望这对你有帮助\"）、标点规范到不像真人、"
    "段落长度均匀\n"
    "- 人味特征：口语与短句跳跃、错别字/口误/语气词、情绪起伏、"
    "跑题与打断、具体生活细节（时间地点人物）、自嘲与黑话\n\n"
    "二、只有判定为\"人写的\"才做：从中提炼值得学习的说话风格。"
    "六个维度都要看一遍（没有的给空串），其中【思维方式】与"
    "【待人接物】是重点——不要只盯着用词，要看这个人怎么想问题、"
    "怎么跟人打交道：\n"
    "- wording：用词口癖（高频词/语气词）\n"
    "- syntax：句式习惯\n"
    "- thinking：思维方式（先看什么、怎么得出结论）\n"
    "- emotion_style：情绪表达方式\n"
    "- interaction：待人接物（怎么接话、怎么打岔、怎么表达不同意）\n"
    "- avoid：反面特征（这类文本里不出现的）\n\n"
    "source 字段：dialogue=多人在互相说话（评论区、回复串、问答"
    "往来）；article=单作者的长文。拿不准按 article。\n\n"
    "严格输出一个 JSON 对象，不要输出任何其他文字：\n"
    '{"kind": "human", "source": "dialogue", "dims": {'
    '"wording": "", "syntax": "", "thinking": "", '
    '"emotion_style": "", "interaction": "", "avoid": ""}}\n'
    "kind 只能是 human / ai / uncertain；kind 是 ai 或 uncertain 时 "
    "dims 给空对象。\n\n"
    "文本片段：\n{material}"
)

# ---- M20-补丁1 K3：每日复盘提示词（可配，占位符 {entries_block} /
# {reactions_block}）。输出 JSON 协议（entries[].id/verdict/reason）是
# 解析依赖，别改结构。
DEFAULT_PROMPT_REVIEW = (
    "下面是「她」说话风格库里最近被取用过的条目，以及她最近和主人的"
    "聊天摘录。请逐条判断：这条说话方式在最近的相处里效果如何？\n\n"
    "判断依据（从聊天摘录里找信号，找不到就给 neutral，不要猜）：\n"
    "- good：主人没有表示反感、聊天氛围正常、或有正面反应\n"
    "- bad：主人表达过「别这样说话」「你怎么突然这个腔调」之类的调整要求，"
    "或这条语气出现后明显冷场\n"
    "- neutral：没有足够信息判断\n\n"
    "严格输出一个 JSON 对象，不要输出任何其他文字：\n"
    '{"entries": [{"id": "条目id", "verdict": "good", "reason": "一句话"}]}\n'
    "只允许对下面给出的条目 id 给判断，不要发明新 id；verdict 只能是 "
    "good / bad / neutral。\n\n"
    "最近被取用的条目：\n{entries_block}\n\n"
    "最近的聊天摘录（只看反应，不要复述）：\n{reactions_block}"
)

# ---- M20-补丁1 K2：沉淀归纳提示词（可配，占位符 {entries_block}）。
# 输出 JSON 协议（features[].note/dims 六键）是解析依赖，别改结构。
DEFAULT_PROMPT_INDUCT = (
    "下面是「她」说话风格库里评分较高、反复被取用的条目。请把它们沉淀成"
    "少数几条稳定的「她自己的说话方式」——不是复制原文，而是把共同点"
    "归纳成通用特征。\n\n"
    "要求：\n"
    "- 每条特征六个维度都看一遍（没有内容的给空串）：wording 用词 / "
    "syntax 句式 / thinking 思维方式 / emotion_style 情绪表达 / "
    "interaction 待人接物 / avoid 别这样\n"
    "- 最多 4 条，每条单维内容不超过 40 字\n"
    "- 只输出 JSON：{\"features\": [{\"note\": \"一句话概括\", "
    "\"dims\": {\"wording\": \"\", \"syntax\": \"\", \"thinking\": \"\", "
    "\"emotion_style\": \"\", \"interaction\": \"\", \"avoid\": \"\"}}]}\n\n"
    "高评分条目：\n{entries_block}"
)

_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)


def _to_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _to_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def strip_json_block(raw: str) -> dict | None:
    """从 LLM 输出里抠出第一个 JSON 对象（容忍 ```json 围栏与前后废话）。

    解析失败返回 None——调用方按"本次没学到"静默处理，不重试不报错。
    """
    text = str(raw or "").strip()
    if not text:
        return None
    m = _JSON_BLOCK_RE.search(text)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def looks_like_code_or_list(text: str) -> bool:
    """纯代码/纯列表启发式：这类文本提炼不出说话风格（A7 跳过依据）。

    宽松判定：代码围栏/常见关键字密度高，或绝大多数行都是列表项/表格行。
    """
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return True
    list_like = sum(
        1
        for ln in lines
        if re.match(r"^([-*+>]|\d+[.)、]|\|)", ln)
    )
    if list_like / len(lines) > 0.8:
        return True
    code_hits = sum(
        1
        for ln in lines
        if re.search(r"[{};=<>]|def |import |return |function |const |var ", ln)
    )
    return code_hits / len(lines) > 0.6


def conversational_score(text: str) -> float:
    """对话形态启发式得分（A7"优先评论区/对话类内容"的取样依据）。

    多行短句、第二/第一人称称呼、回复与 @ 形态密集的文本更像"多人在
    互相说话"——得分越高越优先送去判定。不追求精准：真正的 dialogue/
    article 分层由判定调用给出，这里只决定"先送谁"。
    """
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return 0.0
    short_ratio = sum(1 for ln in lines if len(ln) <= 40) / len(lines)
    mention = len(re.findall(r"@[^\s]|回复|楼主|兄弟|朋友|大佬", text)) / max(
        len(lines), 1
    )
    personal = sum(
        1 for ln in lines if re.search(r"我|你|咱|您", ln)
    ) / len(lines)
    return min(short_ratio * 0.5 + personal * 0.3 + mention * 0.2, 1.0)


def parse_review_time(text: Any) -> tuple[int, int] | None:
    """解析 daily_review_time（"HH:MM"）→ (hour, minute)；非法返回 None。"""
    m = re.match(r"^(\d{1,2}):(\d{2})$", str(text or "").strip())
    if not m:
        return None
    hour, minute = int(m.group(1)), int(m.group(2))
    if hour > 23 or minute > 59:
        return None
    return hour, minute


def _atomic_write_json(path: Path, data: Any) -> None:
    """原子写 JSON（tmp + replace）。

    Windows 实测：实时防护（AV/索引器）可能短暂锁住刚关句柄的 .tmp，
    立刻 replace 会偶发 PermissionError——小睡 50ms 重试一次；仍失败则
    抛给调用方的静默兜底（与旧语义一致）。"""
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    try:
        tmp.replace(path)
    except OSError:
        import time

        time.sleep(0.05)
        tmp.replace(path)


class StyleLearner:
    """风格分层库（语料/素材/调用记录/沉淀层）与学习/取用引擎。所有配置
    热读；任何异常都由调用方兜底（本类方法自身也应保持"失败返回 None/
    空串"的静默语义）。"""

    def __init__(
        self,
        config_getter: Callable[[], Any],
        llm_call: Callable[..., Any] | None,
        pool_path: str = "",
        sample_getter: Callable[[], list[dict]] | None = None,
        rng: random.Random | None = None,
        now_provider: Callable[[], datetime] | None = None,
        search_enabled_getter: Callable[[], Any] | None = None,
        materials_path: str = "",
        usage_path: str = "",
        features_path: str = "",
        browser_reads_getter: Callable[[], list[dict]] | None = None,
    ) -> None:
        self._config_getter = config_getter
        self._llm_call = llm_call  # async (prompt, system) -> str | None
        self._pool_path = str(pool_path or "")
        self._sample_getter = sample_getter  # () -> [{url,title,text,at}]
        self._rng = rng or random.Random()
        self._now = now_provider or datetime.now
        self._search_enabled_getter = search_enabled_getter  # L2：不再拦，参数保留兼容
        # M20-补丁1：分层库路径（素材/调用记录/沉淀层，全部与记忆物理隔离）
        self._materials_path = str(materials_path or "")
        self._usage_path = str(usage_path or "")
        self._features_path = str(features_path or "")
        self._browser_reads_getter = browser_reads_getter
        self._pool: list[dict] = []
        self._loaded = False
        self._dirty = False
        self._materials: list[dict] = []
        self._materials_loaded = False
        self._materials_dirty = False
        self._usage: deque[dict] = deque()
        self._usage_loaded = False
        self._features: list[dict] = []
        self._features_meta: dict = {}
        self._features_loaded = False
        self._features_dirty = False
        # J3 立即处理：处理中标志 + 限频时间戳
        self._processing = False
        self._last_process_at: datetime | None = None

    # ------------------------------------------------------------------
    # 配置（M20-补丁1 M 组：学习参数全部可配，热读 + 钳位）
    # ------------------------------------------------------------------
    def _cfg(self) -> dict:
        try:
            from .conf_path import conf_group

            group = conf_group(self._config_getter() or {}, "style_learning")
            return group if isinstance(group, dict) else {}
        except Exception:
            return {}

    def enabled(self) -> bool:
        """style_learning.enabled（默认 false——主人要先看效果再常开）。"""
        try:
            raw = self._cfg().get("enabled", False)
        except Exception:
            return False
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() in ("true", "1", "on", "yes")

    def max_inject_chars(self) -> int:
        return max(_to_int(self._cfg().get("max_inject_chars"), 300), 60)

    def max_items_per_pick(self) -> int:
        return max(_to_int(self._cfg().get("max_items_per_pick"), 2), 1)

    def pool_limit(self) -> int:
        return max(_to_int(self._cfg().get("pool_limit"), 200), 10)

    def decay_days(self) -> float:
        return max(_to_float(self._cfg().get("decay_days"), 14.0), 1.0)

    def source_weights(self) -> dict:
        raw = self._cfg().get("source_weights")
        if not isinstance(raw, dict) or not raw:
            return dict(DEFAULT_SOURCE_WEIGHTS)
        out = {}
        for kind in ("dialogue", "article"):
            out[kind] = max(
                _to_float(raw.get(kind), DEFAULT_SOURCE_WEIGHTS[kind]), 0.0
            )
        return out

    def material_max_chars(self) -> int:
        """M1：单条材料送提示词的字符上限（默认 1200，热读）。"""
        return max(_to_int(self._cfg().get("material_max_chars"), _MATERIAL_MAX_CHARS), 100)

    def min_material_chars(self) -> int:
        """M2：材料低于该字数不提炼（默认 80）。"""
        return max(_to_int(self._cfg().get("min_material_chars"), MIN_MATERIAL_CHARS), 0)

    def item_max_chars(self) -> int:
        """M2：每条片段每维的字符上限（默认 80）。"""
        return max(_to_int(self._cfg().get("item_max_chars"), _ITEM_MAX_CHARS), 20)

    def manual_weight(self) -> float:
        """I4：人工优选倍率（默认 1.2）。内部钳位 [1.0, 1.5]——主人红线
        "高一点点就可以了"，不设 2 倍以上。"""
        raw = _to_float(self._cfg().get("manual_weight"), 1.2)
        return min(max(raw, 1.0), 1.5)

    def feature_importance_cap(self) -> float:
        """K3：重要度上限倍率（默认 1.5，钳位 [1.0, 2.0]）。"""
        raw = _to_float(self._cfg().get("feature_importance_cap"), 1.5)
        return min(max(raw, 1.0), 2.0)

    def usage_log_limit(self) -> int:
        """K1：调用记录库上限（默认 500）。"""
        return max(_to_int(self._cfg().get("usage_log_limit"), 500), 50)

    def material_pool_limit(self) -> int:
        """I2：素材库上限（默认 100）。"""
        return max(_to_int(self._cfg().get("material_pool_limit"), 100), 10)

    def feature_promote_threshold(self) -> int:
        """K2：沉淀归纳阈值——自上次归纳以来累计"判好"达到该数才归纳
        （默认 3）。"""
        return max(_to_int(self._cfg().get("feature_promote_threshold"), 3), 1)

    def daily_review_time(self) -> tuple[int, int] | None:
        """K3：每日复盘时间（"HH:MM"，默认 04:00）。"""
        return parse_review_time(self._cfg().get("daily_review_time", "04:00"))

    def daily_review_enabled(self) -> bool:
        """K3：每日复盘开关（默认 true；但受 enabled 总闸约束）。"""
        raw = self._cfg().get("daily_review_enabled", True)
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() in ("true", "1", "on", "yes")

    # ------------------------------------------------------------------
    # 语料库读写（A4/I1：style_corpus.json，与记忆完全分开；旧文件迁移）
    # ------------------------------------------------------------------
    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if not self._pool_path:
            return
        try:
            with open(self._pool_path, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                self._pool = [e for e in data if isinstance(e, dict)]
                return
        except FileNotFoundError:
            self._pool = []
        except Exception as e:
            logger.debug(f"[Style] 语料库读取失败（按空库继续）: {e}")
            self._pool = []
            return
        self._pool = []

    def _load_legacy_pool_if_present(self) -> bool:
        """I1：一次性迁移——新库不存在而旧 style_pool.json 存在 → 读旧库。
        返回是否命中迁移；迁移数据在下次写盘时落进新文件（旧文件保留
        不删，主人数据不动）。"""
        if not self._pool_path:
            return False
        corpus = Path(self._pool_path)
        if corpus.exists():
            return False
        legacy = corpus.parent / LEGACY_POOL_FILENAME
        if not legacy.exists():
            return False
        try:
            with open(legacy, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                self._pool = [e for e in data if isinstance(e, dict)]
                self._dirty = True  # 下次写盘落新文件
                logger.info(
                    f"[Style] 语料库已从 {LEGACY_POOL_FILENAME} 迁移到 "
                    f"{corpus.name}（{len(self._pool)} 条；旧文件保留未删）"
                )
                return True
        except Exception as e:
            logger.debug(f"[Style] 旧库迁移失败（按空库继续）: {e}")
        return False

    def _save(self) -> None:
        if not self._pool_path or not self._dirty:
            return
        try:
            path = Path(self._pool_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write_json(path, self._pool)
            self._dirty = False
        except Exception as e:
            logger.debug(f"[Style] 语料库写入失败（不影响主流程）: {e}")

    def entries(self) -> list[dict]:
        """语料条目只读视图（测试/面板/复盘用）。"""
        self._ensure_loaded()
        self._load_legacy_pool_if_present()
        return list(self._pool)

    @staticmethod
    def _content_fp(text: str) -> str:
        return hashlib.md5(text.encode("utf-8")).hexdigest()[:16]

    @staticmethod
    def _new_entry(
        dims: dict, source_kind: str, source_note: str, weight: float,
        now: datetime, content_fp: str,
    ) -> dict:
        return {
            "id": f"{now.strftime('%Y%m%d%H%M%S')}:{content_fp}",
            "dims": dims,
            "source_kind": source_kind,
            "source_note": str(source_note)[:120],
            "content_fp": content_fp,
            "weight": round(weight, 3),
            "base_weight": round(weight, 3),
            "learned_at": now.isoformat(),
            "used_count": 0,
            "last_used_at": now.isoformat(),
            # M20-补丁1 K3/I4：复盘与人工优选字段（默认中性）
            "importance": 1.0,
            "retention": 1.0,
            "review_good": 0,
            "review_bad": 0,
            "manual": False,
        }

    # ------------------------------------------------------------------
    # 素材库（I2：style_materials.json——只由主人经面板写入）
    # ------------------------------------------------------------------
    def _ensure_materials_loaded(self) -> None:
        if self._materials_loaded:
            return
        self._materials_loaded = True
        if not self._materials_path:
            return
        try:
            with open(self._materials_path, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                self._materials = [e for e in data if isinstance(e, dict)]
        except FileNotFoundError:
            self._materials = []
        except Exception as e:
            logger.debug(f"[Style] 素材库读取失败（按空库继续）: {e}")
            self._materials = []

    def _save_materials(self) -> None:
        if not self._materials_path or not self._materials_dirty:
            return
        try:
            path = Path(self._materials_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write_json(path, self._materials)
            self._materials_dirty = False
        except Exception as e:
            logger.debug(f"[Style] 素材库写入失败（不影响主流程）: {e}")

    def materials(self) -> list[dict]:
        self._ensure_materials_loaded()
        return list(self._materials)

    def add_material(self, text: str, note: str = "", now: datetime | None = None) -> dict:
        """I2：主人手动投入素材（只由面板调用；插件自己绝不写）。有界。"""
        now = now or self._now()
        self._ensure_materials_loaded()
        text = str(text or "").strip()
        if not text:
            raise ValueError("素材内容不能为空")
        limit = self.material_pool_limit()
        if len(self._materials) >= limit:
            raise ValueError(f"素材库已满（上限 {limit} 条），请先清理")
        entry = {
            "id": f"m{now.strftime('%Y%m%d%H%M%S')}{self._rng.randrange(1000):03d}",
            "text": text[: self.material_max_chars()],
            "note": str(note or "")[:120],
            "added_at": now.isoformat(),
            "processed": False,
            "processed_at": None,
            "result": "",
            "attempts": 0,
        }
        self._materials.append(entry)
        self._materials_dirty = True
        self._save_materials()
        return entry

    def remove_material(self, material_id: str) -> bool:
        self._ensure_materials_loaded()
        before = len(self._materials)
        self._materials = [
            m for m in self._materials if str(m.get("id")) != str(material_id)
        ]
        if len(self._materials) != before:
            self._materials_dirty = True
            self._save_materials()
            return True
        return False

    def clear_materials(self) -> int:
        self._ensure_materials_loaded()
        count = len(self._materials)
        self._materials = []
        self._materials_dirty = True
        self._save_materials()
        return count

    def _next_unprocessed_material(self) -> dict | None:
        """I3：最早投入的未处理素材（FIFO——主人亲手挑的先提炼）。"""
        self._ensure_materials_loaded()
        for m in self._materials:
            if not m.get("processed"):
                return m
        return None

    # ------------------------------------------------------------------
    # 调用记录库（K1：feature_usage.json——只记取用事实，不进记忆）
    # ------------------------------------------------------------------
    def _ensure_usage_loaded(self) -> None:
        if self._usage_loaded:
            return
        self._usage_loaded = True
        if not self._usage_path:
            return
        try:
            with open(self._usage_path, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                self._usage = deque(
                    (e for e in data if isinstance(e, dict)),
                    maxlen=self.usage_log_limit(),
                )
        except FileNotFoundError:
            self._usage = deque()
        except Exception as e:
            logger.debug(f"[Style] 调用记录读取失败（按空记录继续）: {e}")
            self._usage = deque()

    def _save_usage(self) -> None:
        if not self._usage_path:
            return
        try:
            path = Path(self._usage_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write_json(path, list(self._usage))
        except Exception as e:
            logger.debug(f"[Style] 调用记录写入失败（不影响主流程）: {e}")

    def _log_usage(
        self, corpus_picked: list[dict], feature: dict | None,
        trigger: str, now: datetime,
    ) -> None:
        if not self._usage_path:
            return
        try:
            self._ensure_usage_loaded()
            entry_ids = [str(e.get("id")) for e in corpus_picked]
            if feature is not None:
                entry_ids.insert(0, f"feat:{feature.get('id')}")
            self._usage.append(
                {"ts": now.isoformat(), "entry_ids": entry_ids,
                 "trigger": str(trigger or "对话")[:20]}
            )
            limit = self.usage_log_limit()
            while len(self._usage) > limit:
                self._usage.popleft()
            self._save_usage()
        except Exception as e:
            logger.debug(f"[Style] 调用记录写入异常（忽略）: {e}")

    def usage_records(self, limit: int | None = None) -> list[dict]:
        """调用记录（K1/复盘输入/面板查看）。新的在前。"""
        self._ensure_usage_loaded()
        records = list(self._usage)
        records.reverse()
        if limit is not None:
            records = records[:limit]
        return records

    # ------------------------------------------------------------------
    # 沉淀层（K2：style_features.json——由每日复盘后的归纳产出）
    # ------------------------------------------------------------------
    def _ensure_features_loaded(self) -> None:
        if self._features_loaded:
            return
        self._features_loaded = True
        if not self._features_path:
            return
        try:
            with open(self._features_path, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                features = data.get("features")
                self._features = [
                    e for e in (features or []) if isinstance(e, dict)
                ]
                meta = data.get("meta")
                self._features_meta = dict(meta) if isinstance(meta, dict) else {}
        except FileNotFoundError:
            pass
        except Exception as e:
            logger.debug(f"[Style] 沉淀层读取失败（按空层继续）: {e}")

    def _save_features(self) -> None:
        if not self._features_path or not self._features_dirty:
            return
        try:
            path = Path(self._features_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write_json(
                path, {"features": self._features, "meta": self._features_meta}
            )
            self._features_dirty = False
        except Exception as e:
            logger.debug(f"[Style] 沉淀层写入失败（不影响主流程）: {e}")

    def features(self) -> list[dict]:
        self._ensure_features_loaded()
        return list(self._features)

    def features_meta(self) -> dict:
        self._ensure_features_loaded()
        return dict(self._features_meta)

    def set_features_meta(self, key: str, value: Any) -> None:
        self._ensure_features_loaded()
        self._features_meta[str(key)] = value
        self._features_dirty = True
        self._save_features()

    def replace_features(self, features: list[dict]) -> None:
        """K2：归纳结果落库（有界 FEATURES_LIMIT；超出丢弃）。"""
        self._ensure_features_loaded()
        self._features = [e for e in features if isinstance(e, dict)][
            :FEATURES_LIMIT
        ]
        self._features_dirty = True
        self._save_features()

    def _pick_feature(self, now: datetime) -> dict | None:
        """注入时优先取一条沉淀特征（按重要度加权轮盘；K2 稳定的一面）。"""
        self._ensure_features_loaded()
        if not self._features:
            return None
        weights = [
            max(_to_float(f.get("importance"), 1.0), 0.05)
            for f in self._features
        ]
        total = sum(weights)
        if total <= 0:
            return None
        roll = self._rng.random() * total
        acc = 0.0
        for feature, weight in zip(self._features, weights):
            acc += weight
            if roll < acc:
                return feature
        return self._features[-1]

    # ------------------------------------------------------------------
    # A6 演化：综合分（抽样与淘汰共用一个口径；K3 加留存度/重要度）
    # ------------------------------------------------------------------
    def _importance_of(self, entry: dict) -> float:
        return min(
            max(_to_float(entry.get("importance"), 1.0), 0.5),
            self.feature_importance_cap(),
        )

    def _retention_of(self, entry: dict) -> float:
        return max(_to_float(entry.get("retention"), 1.0), 0.1)

    def _weight_eff(self, entry: dict, now: datetime) -> float:
        """有效权重 = weight × 0.5^floor(超期轮数)。

        衰减纯函数化（从 last_used_at 出发计算，无累积状态）：超过
        decay_days 未用 → 减半；再超一个 decay_days → 再减半。
        """
        weight = max(_to_float(entry.get("weight"), 0.0), 0.0)
        last_raw = entry.get("last_used_at") or entry.get("learned_at")
        try:
            last = datetime.fromisoformat(str(last_raw))
        except (TypeError, ValueError):
            return weight
        age_days = max((now - last).total_seconds() / 86400.0, 0.0)
        decay = self.decay_days()
        halvings = int(age_days / decay) if decay > 0 else 0
        return weight * (0.5 ** halvings)

    def _freshness(self, entry: dict, now: datetime) -> float:
        """新鲜度 [0.1, 1]：线性随"距上次使用"衰减（越近越活）。
        底限 0.1——刚衰减过但还在用的条目不至于完全抽不到。"""
        last_raw = entry.get("last_used_at") or entry.get("learned_at")
        try:
            last = datetime.fromisoformat(str(last_raw))
        except (TypeError, ValueError):
            return 0.5
        age_days = max((now - last).total_seconds() / 86400.0, 0.0)
        span = self.decay_days() * 2.0
        return max(0.1, 1.0 - age_days / span) if span > 0 else 0.5

    def _usage_cool(self, entry: dict) -> float:
        """使用降温（A5：用过的适度降温，避免天天同一句）。"""
        used = max(_to_int(entry.get("used_count"), 0), 0)
        return 1.0 / (1.0 + used * 0.3)

    def entry_score(self, entry: dict, now: datetime) -> float:
        """综合分 = (有效权重 × 重要度，封顶 基础权重×重要度上限)
        × 新鲜度 × 使用降温。

        K3 防僵化硬约束 ①：有效权重 × 重要度绝不超过 base_weight ×
        feature_importance_cap（默认 1.5 倍）——某条被夸得再多，取用
        权重也不会超过基础权重的 1.5 倍。"""
        weight_eff = self._weight_eff(entry, now)
        score = weight_eff * self._importance_of(entry)
        base = max(_to_float(entry.get("base_weight"), 0.0), 0.0)
        if base > 0:
            score = min(score, base * self.feature_importance_cap())
        return score * self._freshness(entry, now) * self._usage_cool(entry)

    def _evolve(self, now: datetime) -> None:
        """A6 演化落地：过期减半由 _weight_eff 在读取侧生效；这里负责
        (1) 有效权重 × 留存度低于淘汰线的移出库（K3 防僵化硬约束 ②：
        判差的留存度低 → 更快淘汰，判好的留存度高 → 留得更久）；
        (2) 超上限按 综合分×留存度 淘汰最低者（判好的不被清出）。
        只在 learn/pick 的写路径里惰性执行。"""
        limit = self.pool_limit()
        survivors = [
            e
            for e in self._pool
            if self._weight_eff(e, now) * self._retention_of(e) >= 0.1
        ]
        if len(survivors) > limit:
            survivors.sort(
                key=lambda e: self.entry_score(e, now) * self._retention_of(e),
                reverse=True,
            )
            dropped = survivors[limit:]
            survivors = survivors[:limit]
            if dropped:
                logger.debug(
                    f"[Style] 语料库超上限，淘汰综合分最低 {len(dropped)} 条"
                )
        if len(survivors) != len(self._pool):
            self._pool = survivors
            self._dirty = True

    # ------------------------------------------------------------------
    # A3+A7：一次调用完成 AI 判定与六维提炼
    # ------------------------------------------------------------------
    def _render_template(self, key: str, default: str, values: dict) -> str:
        from .prompts import read_template, render_template

        return render_template(
            read_template(self._cfg(), key, default),
            values,
            name=f"style_learning.{key}",
            default=default,
        )

    def _distill_prompt(self, material: str, source_note: str) -> str:
        # M19-补丁1 D5：提示词搬面板（默认逐字一致）；来源备注的空值兜底
        # '网页' 仍是代码逻辑（占位符值兜底，而不是模板里写条件）
        return self._render_template(
            "prompt_distill",
            DEFAULT_PROMPT_DISTILL,
            {"source_note": source_note or "网页", "material": material},
        )

    def _parse_distill(self, raw: str) -> tuple[str, str, dict] | None:
        """解析判定调用输出 → (kind, source_kind, dims)；解析失败 None。

        dims 只收六个白名单键（防提示注入塞别的字段），值统一截断。"""
        data = strip_json_block(raw)
        if data is None:
            return None
        kind = str(data.get("kind") or "").strip().lower()
        if kind not in ("human", "ai", "uncertain"):
            return None
        source_kind = str(data.get("source") or "").strip().lower()
        if source_kind not in ("dialogue", "article"):
            source_kind = "article"
        dims: dict = {}
        raw_dims = data.get("dims")
        if isinstance(raw_dims, dict):
            for key in DIMS_ORDER:
                value = str(raw_dims.get(key) or "").strip()
                if value:
                    dims[key] = value[: self.item_max_chars() * 2]
        return kind, source_kind, dims

    def _pick_material(self, started_at: datetime | None = None) -> tuple[str, str] | None:
        """从本轮读到的内容里选学习材料（A7：优先对话形态，如评论区）。

        材料来源（M20-补丁1 L1）：fetch_page 留档 + 浏览器读取留档，
        都只取时间 >= started_at 的"本轮"条目。返回 (材料文本, 来源备注)；
        没有够格的材料返回 None。"""
        candidates: list[tuple[float, str, str]] = []
        for source in self._read_evidence(started_at):
            for s in source:
                text = str(s.get("text") or "").strip()
                if len(text) < self.min_material_chars():
                    continue
                if looks_like_code_or_list(text):
                    continue
                title = str(s.get("title") or "").strip()
                url = str(s.get("url") or "").strip()
                note = title or (url[:80] if url else "")
                candidates.append((conversational_score(text), text, note))
        if not candidates:
            return None
        candidates.sort(key=lambda item: item[0], reverse=True)
        best = candidates[0]
        return best[1][: self.material_max_chars()], best[2]

    def _read_evidence(
        self, started_at: datetime | None = None
    ) -> tuple[list[dict], list[dict]]:
        """L1/L4 读网证据：本轮（时间 >= started_at）的 fetch 留档与
        浏览器读取留档。started_at 为 None 时不过滤时间（取全部留档）。"""
        samples: list[dict] = []
        if self._sample_getter is not None:
            try:
                samples = [
                    s for s in (self._sample_getter() or [])
                    if isinstance(s, dict) and self._after(s.get("at"), started_at)
                ]
            except Exception as e:
                logger.debug(f"[Style] 抓取样本读取失败（跳过）: {e}")
        browser: list[dict] = []
        if self._browser_reads_getter is not None:
            try:
                browser = [
                    s for s in (self._browser_reads_getter() or [])
                    if isinstance(s, dict) and self._after(s.get("at"), started_at)
                ]
            except Exception as e:
                logger.debug(f"[Style] 浏览器留档读取失败（跳过）: {e}")
        return samples, browser

    @staticmethod
    def _after(at_raw: Any, started_at: datetime | None) -> bool:
        """时间戳过滤：at >= started_at 视为"本轮"。
        - started_at 为 None（直调 distill 等场景）→ 不过滤，一律算数；
        - 时间戳缺失/解析不了 → 严格按"非本轮"处理（宁可少学）。"""
        if started_at is None:
            return True
        if not at_raw:
            return False
        try:
            at = datetime.fromisoformat(str(at_raw))
        except (TypeError, ValueError):
            return False
        return at >= started_at

    async def _distill_detailed(
        self, text: str, source_note: str, now: datetime,
        manual: bool = False,
    ) -> tuple[dict | None, str]:
        """判定+提炼+入库主路径（A3：单次 LLM 调用），带失败原因。

        返回 (入库的条目, 原因)；原因 ∈ disabled/no_llm/too_short/
        duplicate/llm_failed/parse_failed/verdict_ai/verdict_uncertain/
        no_dims/learned。素材库处理（I3）按原因决定"标记已处理"还是
        "留着下次重试"。"""
        if not self.enabled():
            return None, "disabled"
        if self._llm_call is None:
            return None, "no_llm"
        text = str(text or "").strip()
        if len(text) < self.min_material_chars():
            return None, "too_short"
        self._ensure_loaded()
        self._load_legacy_pool_if_present()
        fp = self._content_fp(text)
        # A4 幂等：同一来源（活动备注 + 内容指纹）不重复入库
        if any(
            e.get("content_fp") == fp and e.get("source_note") == source_note
            for e in self._pool
        ):
            logger.debug("[Style] 该来源已学过，跳过（幂等）")
            return None, "duplicate"
        try:
            raw = await self._llm_call(
                self._distill_prompt(text[: self.material_max_chars()], source_note),
                None,
            )
        except Exception as e:
            logger.debug(f"[Style] 判定调用失败（本次不学）: {e}")
            return None, "llm_failed"
        parsed = self._parse_distill(str(raw or ""))
        if parsed is None:
            logger.debug("[Style] 判定输出解析失败（本次不学）")
            return None, "parse_failed"
        kind, source_kind, dims = parsed
        if kind in ("ai", "uncertain"):
            # 主人红线：疑似 AI 一律丢弃，宁可少学
            logger.info(f"[Style] 判定 {kind}，整条丢弃（不入库）")
            return None, f"verdict_{kind}"
        if not dims:
            logger.debug("[Style] 人写的但没提炼出可学片段（不入库）")
            return None, "no_dims"
        weights = self.source_weights()
        weight = weights.get(source_kind, 0.5)
        if manual:
            # I4：人工优选只比 dialogue 略高（倍率钳位 ≤1.5，不设 2 倍以上）
            weight = weight * self.manual_weight()
        entry = self._new_entry(dims, source_kind, source_note, weight, now, fp)
        entry["manual"] = manual
        self._pool.append(entry)
        self._dirty = True
        self._evolve(now)
        self._save()
        logger.info(
            f"[Style] 学到一条风格片段（{source_kind}{'，人工优选' if manual else ''}，"
            f"权重 {weight}，维度 {sorted(dims)}），库存 {len(self._pool)}"
        )
        return entry, "learned"

    async def distill(
        self, text: str, source_note: str, now: datetime | None = None
    ) -> dict | None:
        """判定+提炼+入库（兼容入口：返回入库的条目或 None）。"""
        now = now or self._now()
        entry, _reason = await self._distill_detailed(text, source_note, now)
        return entry

    async def _learn_from_material(
        self, material: dict, now: datetime, activity_id: str
    ) -> dict | None:
        """I3：提炼一条主人投入的素材，并按结果回写素材库状态。

        - 成功 → processed=true, result=learned（不删除，主人可看状态）；
        - 稳定结局（判 AI/太短/重复/无可学片段）→ processed=true 并记录
          原因（不重试——结论不会变）；
        - 瞬时失败（LLM 调用/解析失败）→ attempts+1 留着下次重试，
          连续 3 次失败标记 failed（防无限重试烧调用）。"""
        text = str(material.get("text") or "")
        note = str(material.get("note") or "").strip() or "主人手动投入"
        if activity_id and activity_id != "manual":
            note = f"{note}（{activity_id}）"
        entry, reason = await self._distill_detailed(text, note, now, manual=True)
        if entry is not None:
            material["processed"] = True
            material["processed_at"] = now.isoformat()
            material["result"] = "learned"
            self._materials_dirty = True
            self._save_materials()
            return entry
        if reason in (
            "verdict_ai", "verdict_uncertain", "no_dims", "too_short", "duplicate",
        ):
            material["processed"] = True
            material["processed_at"] = now.isoformat()
            material["result"] = reason
            self._materials_dirty = True
            self._save_materials()
            return None
        if reason in ("llm_failed", "parse_failed"):
            material["attempts"] = max(_to_int(material.get("attempts"), 0), 0) + 1
            if material["attempts"] >= 3:
                material["processed"] = True
                material["processed_at"] = now.isoformat()
                material["result"] = f"failed:{reason}"
            self._materials_dirty = True
            self._save_materials()
        return None

    async def on_activity_end(
        self, activity_name: str, now: datetime | None = None,
        activity_id: str = "", started_at: datetime | None = None,
    ) -> dict | None:
        """A7 触发入口（M20-补丁1 L 组扩展）：任何活动结束时调用。

        - 素材库优先（I3/L3）：有待处理素材时，任何活动结束都提炼一条
          （主人亲手挑的不用等活动类型）；
        - 否则要有"本轮真的读了网页"的证据（L1/L4）：本轮有 fetch_page
          成功留档或 browser_navigate/browser_read 成功留档（时间 >=
          started_at）。原"搜索关闭就不学"放宽为"没读到新内容就不学"
          （L2——浏览器不依赖搜索，她仍可能读到东西）；
        - 一次活动最多学 1 条（I3 克制不变）。"""
        now = now or self._now()
        if not self.enabled():
            return None
        started = started_at or now
        material = self._next_unprocessed_material()
        if material is not None:
            return await self._learn_from_material(material, now, activity_id)
        samples, browser = self._read_evidence(started)
        if not samples and not browser:
            logger.debug("[Style] 本轮没有读到新内容（无读网证据），跳过学习")
            return None
        picked = self._pick_material(started)
        if picked is None:
            logger.debug("[Style] 本轮内容不足以提炼（太短/纯代码/纯列表）")
            return None
        text, note = picked
        if activity_id:
            note = f"{activity_id} {note}".strip()
        entry, _reason = await self._distill_detailed(text, note, now)
        return entry

    async def process_now(self) -> dict:
        """J3：主人点"立即处理"——与活动触发共用同一条提炼路径
        （素材 FIFO 头一条）。限频 30s + 处理中拒绝重入。"""
        if self._processing:
            return {"ok": False, "message": "已有处理在进行中，请稍候"}
        now = self._now()
        last = self._last_process_at
        if last is not None and (now - last).total_seconds() < PROCESS_NOW_COOLDOWN_SECONDS:
            return {
                "ok": False,
                "message": f"{PROCESS_NOW_COOLDOWN_SECONDS} 秒内刚处理过，请稍后再试",
            }
        self._processing = True
        try:
            material = self._next_unprocessed_material()
            if material is None:
                return {"ok": False, "message": "素材库里没有待处理的素材"}
            self._last_process_at = now  # 确认真处理了才占限频（空库不占）
            entry = await self._learn_from_material(material, now, "manual")
            if entry is not None:
                return {
                    "ok": True, "message": "已提炼入库",
                    "entry_id": str(entry.get("id") or ""),
                }
            if material.get("processed"):
                result = str(material.get("result") or "")
                friendly = {
                    "verdict_ai": "判定像 AI 写的，没学（红线：宁可少学）",
                    "verdict_uncertain": "拿不准是不是人写的，没学",
                    "no_dims": "是人写的但没提炼出可学片段",
                    "too_short": "内容太短",
                    "duplicate": "语料库里已有同内容条目",
                }.get(result, result or "处理完成，没有学到新内容")
                return {"ok": True, "message": f"已处理：{friendly}"}
            return {
                "ok": False,
                "message": "处理失败（LLM 调用或解析失败），留到下次自动重试",
            }
        finally:
            self._processing = False

    # ------------------------------------------------------------------
    # K3：复盘结果落地（留存度/重要度调整，防僵化上限见 _importance_after）
    # ------------------------------------------------------------------
    def _importance_after(self, entry: dict, delta: float) -> float:
        """重要度调整：Δ 上限/下限钳位 + 同类中位数约束。

        - 硬上限 feature_importance_cap（默认 1.5）；
        - 同类（source_kind 相同）条目重要度中位数 × 1.5 为另一道上限
          ——"不高于同类条目的中位数太多"（K3 主人原话）；
        - 判差下探下限 0.5（差到一定程度就靠留存度淘汰，不再压权重）。"""
        cap = self.feature_importance_cap()
        value = _to_float(entry.get("importance"), 1.0) + delta
        value = min(max(value, 0.5), cap)
        kind = entry.get("source_kind")
        others = sorted(
            self._importance_of(e)
            for e in self._pool
            if e is not entry and e.get("source_kind") == kind
        )
        if others:
            median = statistics.median(others)
            value = min(value, max(median * 1.5, 0.5))
        return round(value, 3)

    def apply_review(self, verdicts: dict, now: datetime) -> dict:
        """K3：复盘判定落地。

        verdicts: {entry_id: {"verdict": "good|bad|neutral", "reason": str}}
        - 留存度（会不会被定期清出去）：判好 ×1.3（上限 2.0），判差
          ×0.6（下限 0.25）→ _evolve 的淘汰线按 有效权重×留存度 判；
        - 重要度（取用权重）：判好 +0.1，判差 -0.15——好条目权重略升
          形成习惯，但被 _importance_after 的两道上限挡住，绝不独霸。"""
        self._ensure_loaded()
        self._load_legacy_pool_if_present()
        changed = good = bad = 0
        for entry in self._pool:
            verdict_data = verdicts.get(str(entry.get("id")))
            if not isinstance(verdict_data, dict):
                continue
            verdict = str(verdict_data.get("verdict") or "").strip().lower()
            if verdict not in ("good", "bad"):
                continue  # neutral 不动
            entry["review_note"] = str(verdict_data.get("reason") or "")[:120]
            entry["review_at"] = now.isoformat()
            retention = self._retention_of(entry)
            if verdict == "good":
                entry["review_good"] = max(_to_int(entry.get("review_good"), 0), 0) + 1
                entry["retention"] = round(min(retention * 1.3, 2.0), 3)
                entry["importance"] = self._importance_after(entry, +0.1)
                good += 1
            else:
                entry["review_bad"] = max(_to_int(entry.get("review_bad"), 0), 0) + 1
                entry["retention"] = round(max(retention * 0.6, 0.25), 3)
                entry["importance"] = self._importance_after(entry, -0.15)
                bad += 1
            changed += 1
        if changed:
            self._dirty = True
            self._save()
        return {"changed": changed, "good": good, "bad": bad}

    # ------------------------------------------------------------------
    # J1：语料库面板管理（编辑六维/删除/清空——只动语料库文件，绝不
    # 触碰记忆库；红线：任何写入都不进记忆）
    # ------------------------------------------------------------------
    def update_entry(self, entry_id: str, dims: dict) -> dict | None:
        self._ensure_loaded()
        self._load_legacy_pool_if_present()
        for entry in self._pool:
            if str(entry.get("id")) == str(entry_id):
                if isinstance(dims, dict):
                    cleaned = {}
                    for key in DIMS_ORDER:
                        value = str(dims.get(key) or "").strip()
                        if value:
                            cleaned[key] = value[: self.item_max_chars() * 2]
                    if cleaned:
                        entry["dims"] = cleaned
                self._dirty = True
                self._save()
                return entry
        return None

    def remove_entry(self, entry_id: str) -> bool:
        self._ensure_loaded()
        self._load_legacy_pool_if_present()
        before = len(self._pool)
        self._pool = [
            e for e in self._pool if str(e.get("id")) != str(entry_id)
        ]
        if len(self._pool) != before:
            self._dirty = True
            self._save()
            return True
        return False

    def clear_entries(self) -> int:
        self._ensure_loaded()
        self._load_legacy_pool_if_present()
        count = len(self._pool)
        self._pool = []
        self._dirty = True
        self._save()
        return count

    # ------------------------------------------------------------------
    # A5：取用与注入文本
    # ------------------------------------------------------------------
    def _entry_line(self, entry: dict) -> str:
        """一条条目 → 注入文本行（六维里挑有内容的，最多 3 段）。"""
        dims = entry.get("dims") or {}
        parts = []
        for key in DIMS_ORDER:
            value = str(dims.get(key) or "").strip()
            if not value:
                continue
            label = DIM_LABELS.get(key, key)
            parts.append(f"{label}：{value[: self.item_max_chars()]}")
        if not parts:
            return ""
        return "；".join(parts[:3])

    def pick(self, now: datetime | None = None, n: int | None = None) -> list[dict]:
        """加权抽样取 1-2 条（weight × 新鲜度 × 使用降温 为权重）。

        A5 情境匹配"能做则做"：条目是风格维度（怎么想/怎么接话）而非
        话题内容，与当前话题没有可靠的关联信号——硬造"相关度"只会是
        噪声，故按随机加权取用（报告已说明）。"""
        now = now or self._now()
        self._ensure_loaded()
        self._load_legacy_pool_if_present()
        if not self._pool:
            return []
        self._evolve(now)
        n = min(n if n is not None else self.max_items_per_pick(), len(self._pool))
        entries = list(self._pool)
        chosen: list[dict] = []
        for _ in range(n):
            weights = [max(self.entry_score(e, now), 0.01) for e in entries]
            total = sum(weights)
            if total <= 0:
                break
            roll = self._rng.random() * total
            acc = 0.0
            for idx, w in enumerate(weights):
                acc += w
                if roll < acc:
                    chosen.append(entries.pop(idx))
                    break
            else:
                chosen.append(entries.pop())
        return chosen

    def record_used(self, entries: list[dict], now: datetime | None = None) -> None:
        """A6：取用记账——used_count 增长、权重缓升（用得顺的慢慢变成
        她的习惯），封顶 base_weight × 1.5（来源层级不被使用率抹平）。"""
        if not entries:
            return
        now = now or self._now()
        self._ensure_loaded()
        changed = False
        ids = {e.get("id") for e in entries if isinstance(e, dict)}
        for entry in self._pool:
            if entry.get("id") not in ids:
                continue
            entry["used_count"] = max(_to_int(entry.get("used_count"), 0), 0) + 1
            entry["last_used_at"] = now.isoformat()
            base = max(_to_float(entry.get("base_weight"), 0.0), 0.1)
            cap = base * 1.5
            entry["weight"] = round(
                min(max(_to_float(entry.get("weight"), base), 0.0) + 0.05, cap), 3
            )
            changed = True
        if changed:
            self._dirty = True
            self._save()

    def inject_block(self, now: datetime | None = None, trigger: str = "对话") -> str:
        """组装注入块（A5：低调、参考语气、硬上限、明确不是身份定义）。

        M20-补丁1 K2：先带沉淀层（稳定的一面，按重要度抽 1 条），再用
        语料库片段补充新鲜感（总条数仍受 max_items_per_pick 约束）；
        沉淀层为空时行为与之前完全一致。
        库空/关闭/异常 → 空串（调用方静默跳过）。取用即记账
        （K1 调用记录 + 演化闭环）。"""
        if not self.enabled():
            return ""
        now = now or self._now()
        try:
            picked_feature = self._pick_feature(now)
            remaining = self.max_items_per_pick() - (1 if picked_feature else 0)
            picked = self.pick(now, n=remaining) if remaining > 0 else []
            if not picked and picked_feature is None:
                return ""
            lines: list[str] = []
            if picked_feature is not None:
                line = self._entry_line(picked_feature)
                if line:
                    lines.append(line)
            for entry in picked:
                line = self._entry_line(entry)
                if line:
                    lines.append(line)
            if not lines:
                return ""
            body = "\n".join(lines)
            budget = self.max_inject_chars()
            block = (
                "（说话语气参考——这是你最近从真人那里学到的一点感觉，"
                "融进语气就好，不是身份设定，不必每句都用、也别硬套：\n"
                f"{body}）"
            )
            # A5 硬上限：整个注入块（含框架文案）不超过 max_inject_chars
            # ——"注入量为硬上限"约束的是主人最终看到的注入总量
            if len(block) > budget:
                block = block[:budget]
            self.record_used(picked, now)
            self._log_usage(picked, picked_feature, trigger, now)
            return block
        except Exception as e:
            logger.debug(f"[Style] 注入块组装失败（静默跳过）: {e}")
            return ""
