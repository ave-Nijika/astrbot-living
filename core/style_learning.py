"""StyleLearner——风格学习系统（任务书 M17-补丁1 A 组）。

她在 read/surf 活动里读到人类文本 → **一次** LLM 调用同时判定"是不是人写的"
（ai / uncertain 整条丢弃，主人红线：宁可少学也不让疑似 AI 的语料进来）并
提炼六维风格片段 → 存进独立素材库 style_pool.json（与记忆/图谱/会话存储
完全分开，不参与记忆召回——红线 2）→ 说话时按情境取 1-2 条低调注入（
"参考语气"框架，硬上限字符数——红线 1）→ 用得多权重缓升成习惯、久不用
自然衰减淘汰（A6 演化：她的说话方式随最近读到的内容漂移，而不是固定一套）。

设计要点：
- 成本（红线 4）：每次学习 1 次 LLM 调用（A3：判定与提炼合并，禁止一条
  文本调两次）；注入零调用（本地加权抽样）；池空/关闭/异常全部静默
  （红线 5），绝不影响正常回复与活动主流程。
- 六维（A1 硬性，不只口癖）：wording / syntax / thinking / emotion_style /
  interaction / avoid——thinking 与 interaction 是主人点名的重点，提示词
  显式要求，解析端校验六键齐全。
- 来源分层（A2，实战权重更高）：dialogue（评论/回复串等实战对话，默认
  权重 1.0）> article（单作者文章，0.5）> ai（0，不学）。source_kind 由
  同一次判定调用给出，不需要单独的对话检测。
"""

from __future__ import annotations

import hashlib
import json
import random
import re
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

# 材料低于该字数不提炼（太短/纯代码/纯列表撑不起六维观察，A7）
MIN_MATERIAL_CHARS = 80

# 单条材料送进提示词的字符上限（控制 token，不追全文）
_MATERIAL_MAX_CHARS = 1200

# 单条片段注入时的字符上限（六维里挑出来展示的每条）
_ITEM_MAX_CHARS = 80

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


class StyleLearner:
    """风格素材库与学习/取用引擎。所有配置热读；任何异常都由调用方
    兜底（本类方法自身也应保持"失败返回 None/空串"的静默语义）。"""

    def __init__(
        self,
        config_getter: Callable[[], Any],
        llm_call: Callable[..., Any] | None,
        pool_path: str = "",
        sample_getter: Callable[[], list[dict]] | None = None,
        rng: random.Random | None = None,
        now_provider: Callable[[], datetime] | None = None,
        search_enabled_getter: Callable[[], Any] | None = None,
    ) -> None:
        self._config_getter = config_getter
        self._llm_call = llm_call  # async (prompt, system) -> str | None
        self._pool_path = str(pool_path or "")
        self._sample_getter = sample_getter  # () -> [{url,title,text,at}]
        self._rng = rng or random.Random()
        self._now = now_provider or datetime.now
        self._search_enabled_getter = search_enabled_getter
        self._pool: list[dict] = []
        self._loaded = False
        self._dirty = False

    # ------------------------------------------------------------------
    # 配置
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

    # ------------------------------------------------------------------
    # 素材库读写（A4：style_pool.json，与记忆完全分开）
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
        except FileNotFoundError:
            self._pool = []
        except Exception as e:
            logger.debug(f"[Style] 素材库读取失败（按空库继续）: {e}")
            self._pool = []

    def _save(self) -> None:
        if not self._pool_path or not self._dirty:
            return
        try:
            path = Path(self._pool_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".json.tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._pool, f, ensure_ascii=False, indent=1)
            tmp.replace(path)
            self._dirty = False
        except Exception as e:
            logger.debug(f"[Style] 素材库写入失败（不影响主流程）: {e}")

    def entries(self) -> list[dict]:
        """素材条目只读视图（测试/面板用）。"""
        self._ensure_loaded()
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
        }

    # ------------------------------------------------------------------
    # A6 演化：综合分（抽样与淘汰共用一个口径）
    # ------------------------------------------------------------------
    def _weight_eff(self, entry: dict, now: datetime) -> float:
        """有效权重 = weight × 0.5^floor(超期轮数)。

        衰减纯函数化（从 last_used_at 出发计算，无累积状态）：超过
        decay_days 未用 → 减半；再超一个 decay_days → 再减半。低于
        淘汰线（0.1）由 _evolve 真删。
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
        """综合分 = 有效权重 × 新鲜度 × 使用降温（A4 淘汰依据 / A5 抽样权重）。"""
        return (
            self._weight_eff(entry, now)
            * self._freshness(entry, now)
            * self._usage_cool(entry)
        )

    def _evolve(self, now: datetime) -> None:
        """A6 演化落地：过期减半由 _weight_eff 在读取侧生效；这里负责
        (1) 有效权重低于淘汰线的移出库；(2) 超上限按综合分淘汰最低者。
        只在 learn/pick 的写路径里惰性执行。"""
        limit = self.pool_limit()
        survivors = [
            e for e in self._pool if self._weight_eff(e, now) >= 0.1
        ]
        if len(survivors) > limit:
            survivors.sort(key=lambda e: self.entry_score(e, now), reverse=True)
            dropped = survivors[limit:]
            survivors = survivors[:limit]
            if dropped:
                logger.debug(
                    f"[Style] 素材库超上限，淘汰综合分最低 {len(dropped)} 条"
                )
        if len(survivors) != len(self._pool):
            self._pool = survivors
            self._dirty = True

    # ------------------------------------------------------------------
    # A3+A7：一次调用完成 AI 判定与六维提炼
    # ------------------------------------------------------------------
    def _distill_prompt(self, material: str, source_note: str) -> str:
        # M19-补丁1 D5：提示词搬面板（默认逐字一致）；来源备注的空值兜底
        # '网页' 仍是代码逻辑（占位符值兜底，而不是模板里写条件）
        from .prompts import read_template, render_template

        return render_template(
            read_template(self._cfg(), "prompt_distill", DEFAULT_PROMPT_DISTILL),
            {
                "source_note": source_note or "网页",
                "material": material,
            },
            name="style_learning.prompt_distill",
            default=DEFAULT_PROMPT_DISTILL,
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
                    dims[key] = value[:_ITEM_MAX_CHARS * 2]
        return kind, source_kind, dims

    def _pick_material(self) -> tuple[str, str] | None:
        """从本轮抓取样本里选学习材料（A7：优先对话形态，如评论区）。

        返回 (材料文本, 来源备注)；没有够格的材料返回 None。"""
        if self._sample_getter is None:
            return None
        try:
            samples = self._sample_getter() or []
        except Exception as e:
            logger.debug(f"[Style] 抓取样本读取失败（跳过学习）: {e}")
            return None
        candidates: list[tuple[float, str, str]] = []
        for s in samples:
            if not isinstance(s, dict):
                continue
            text = str(s.get("text") or "").strip()
            if len(text) < MIN_MATERIAL_CHARS:
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
        return best[1][:_MATERIAL_MAX_CHARS], best[2]

    async def distill(
        self, text: str, source_note: str, now: datetime | None = None
    ) -> dict | None:
        """判定+提炼+入库主路径（A3：单次 LLM 调用）。

        返回入库的条目；丢弃/解析失败/关库/无 LLM 一律返回 None。
        独立成方法便于测试与未来扩展学习触发点。"""
        if not self.enabled() or self._llm_call is None:
            return None
        now = now or self._now()
        text = str(text or "").strip()
        if len(text) < MIN_MATERIAL_CHARS:
            return None
        self._ensure_loaded()
        fp = self._content_fp(text)
        # A4 幂等：同一来源（活动备注 + 内容指纹）不重复入库
        if any(
            e.get("content_fp") == fp and e.get("source_note") == source_note
            for e in self._pool
        ):
            logger.debug("[Style] 该来源已学过，跳过（幂等）")
            return None
        try:
            raw = await self._llm_call(
                self._distill_prompt(text[:_MATERIAL_MAX_CHARS], source_note), None
            )
        except Exception as e:
            logger.debug(f"[Style] 判定调用失败（本次不学）: {e}")
            return None
        parsed = self._parse_distill(str(raw or ""))
        if parsed is None:
            logger.debug("[Style] 判定输出解析失败（本次不学）")
            return None
        kind, source_kind, dims = parsed
        if kind in ("ai", "uncertain"):
            # 主人红线：疑似 AI 一律丢弃，宁可少学
            logger.info(f"[Style] 判定 {kind}，整条丢弃（不入库）")
            return None
        if not dims:
            logger.debug("[Style] 人写的但没提炼出可学片段（不入库）")
            return None
        weights = self.source_weights()
        weight = weights.get(source_kind, 0.5)
        entry = self._new_entry(dims, source_kind, source_note, weight, now, fp)
        self._pool.append(entry)
        self._dirty = True
        self._evolve(now)
        self._save()
        logger.info(
            f"[Style] 学到一条风格片段（{source_kind}，权重 {weight}，"
            f"维度 {sorted(dims)}），库存 {len(self._pool)}"
        )
        return entry

    async def on_activity_end(
        self, activity_name: str, now: datetime | None = None,
        activity_id: str = "",
    ) -> dict | None:
        """A7 触发入口：read/surf 活动结束时调用。一次活动最多学 1 条。

        搜索关闭时 read/surf 本就不进活动池（M15-补丁1 E3），不会走到这里；
        本入口再做一道显式判断（/living do 指名等强制路径），只 DEBUG 说明。"""
        now = now or self._now()
        if not self.enabled():
            return None
        if activity_name not in ("read", "surf"):
            return None
        if self._search_enabled_getter is not None:
            try:
                on = self._search_enabled_getter()
            except Exception:
                on = True  # 快照取不到按"开"处理（保守：不因读取翻脸关学习）
            if not on:
                logger.debug("[Style] 搜索已关闭，本轮没有新读到的内容，跳过学习")
                return None
        material = self._pick_material()
        if material is None:
            logger.debug("[Style] 本轮内容不足以提炼（太短/纯代码/纯列表）")
            return None
        text, note = material
        if activity_id:
            note = f"{activity_id} {note}".strip()
        return await self.distill(text, note, now)

    # ------------------------------------------------------------------
    # A5：取用与注入文本
    # ------------------------------------------------------------------
    def pick(self, now: datetime | None = None, n: int | None = None) -> list[dict]:
        """加权抽样取 1-2 条（weight × 新鲜度 × 使用降温 为权重）。

        A5 情境匹配"能做则做"：条目是风格维度（怎么想/怎么接话）而非
        话题内容，与当前话题没有可靠的关联信号——硬造"相关度"只会是
        噪声，故按随机加权取用（报告已说明）。"""
        now = now or self._now()
        self._ensure_loaded()
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

    def inject_block(self, now: datetime | None = None) -> str:
        """组装注入块（A5：低调、参考语气、硬上限、明确不是身份定义）。

        库空/关闭/异常 → 空串（调用方静默跳过）。取用即记账（演化闭环）。"""
        if not self.enabled():
            return ""
        now = now or self._now()
        try:
            picked = self.pick(now)
            if not picked:
                return ""
            lines: list[str] = []
            for entry in picked:
                dims = entry.get("dims") or {}
                parts = []
                for key in DIMS_ORDER:
                    value = str(dims.get(key) or "").strip()
                    if not value:
                        continue
                    label = DIM_LABELS.get(key, key)
                    parts.append(f"{label}：{value[:_ITEM_MAX_CHARS]}")
                if parts:
                    lines.append("；".join(parts[:3]))
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
            return block
        except Exception as e:
            logger.debug(f"[Style] 注入块组装失败（静默跳过）: {e}")
            return ""
