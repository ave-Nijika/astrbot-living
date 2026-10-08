"""StyleReviewer——每日复盘与沉淀归纳（任务书 M20-补丁1 K 组）。

每天凌晨（style_learning.daily_review_time，默认 04:00）在后台跑一次：
- 输入：调用记录库（feature_usage.json 里最近用了哪些特征）+ 用户反应
  （近期聊天摘录里的称赞/调整要求/冷场等信号）；
- 用**判断模型**（judge.provider_id，经 main._judge_llm_call 注入；未配置
  则本次跳过并记日志——它本身就是保护，不回退聊天模型）逐条判断
  "这条语料好不好"；
- 输出：对每个条目的**留存度**与**重要度**调整（公式与防僵化上限在
  StyleLearner.apply_review / _importance_after，见 style_learning.py）。

成本（任务书 K3）：每天最多 1 次；每次复盘最多 2 次 LLM 调用（1 次汇总
判定 + 达到阈值时 1 次归纳）≤ 3 上限；无取用记录则**零调用**。
后台进行（红线 4）：绝不占用用户对话路径；任何失败只记日志，明天再试。

红线 1：复盘的任何产物（留存度/重要度/沉淀层）都只进 style_*.json
（与记忆库物理隔离），不写 livingmemory / 会话存储 / 图谱。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Callable

from astrbot.api import logger

from .prompts import render_template
from .style_learning import (
    DEFAULT_PROMPT_INDUCT,
    DEFAULT_PROMPT_REVIEW,
    DIMS_ORDER,
    DIM_LABELS,
    REVIEW_MAX_CALLS,
    strip_json_block,
)


def _entry_block_line(entry: dict) -> str:
    """复盘/归纳输入里的一条条目摘要（只带维度文本与统计，不带聊天内容）。"""
    dims = entry.get("dims") or {}
    dim_parts = []
    for key in DIMS_ORDER:
        value = str(dims.get(key) or "").strip()
        if value:
            dim_parts.append(f"{DIM_LABELS.get(key, key)}：{value[:60]}")
    manual = "，人工优选" if entry.get("manual") else ""
    used = entry.get("used_count") or 0
    good = entry.get("review_good") or 0
    bad = entry.get("review_bad") or 0
    head = (
        f"- id={entry.get('id')} [来源 {entry.get('source_kind') or 'article'}{manual}]"
        f" 取用 {used} 次，历史评价 好{good}/差{bad}"
    )
    body = "；".join(dim_parts[:3]) if dim_parts else "（无维度内容）"
    return f"{head}\n  {body}"


class StyleReviewer:
    """每日复盘调度与执行。依赖全部注入（learner/llm_call/reactions），
    可独立测试；任何异常都由调用方兜底（run_review 自身也保持静默语义）。"""

    def __init__(
        self,
        learner: Any,
        llm_call: Callable[..., Any] | None,
        config_getter: Callable[[], Any],
        reactions_getter: Callable[..., Any] | None = None,
        now_provider: Callable[[], datetime] | None = None,
    ) -> None:
        self._learner = learner
        self._llm_call = llm_call  # async (prompt, system) -> str | None（判断模型）
        self._config_getter = config_getter
        self._reactions_getter = reactions_getter  # async () -> list[str]
        self._now = now_provider or datetime.now

    # ------------------------------------------------------------------
    # 配置
    # ------------------------------------------------------------------
    def _style_cfg(self) -> dict:
        try:
            from .conf_path import conf_group

            group = conf_group(self._config_getter() or {}, "style_learning")
            return group if isinstance(group, dict) else {}
        except Exception:
            return {}

    def review_enabled(self) -> bool:
        """daily_review_enabled（默认 true）且 style_learning 总闸开启。"""
        raw = self._style_cfg().get("daily_review_enabled", True)
        if isinstance(raw, bool):
            enabled = raw
        else:
            enabled = str(raw).strip().lower() in ("true", "1", "on", "yes")
        try:
            return enabled and bool(self._learner.enabled())
        except Exception:
            return False

    def review_time(self) -> tuple[int, int]:
        """复盘时间（HH:MM，默认 04:00；非法回落 04:00）。"""
        from .style_learning import parse_review_time

        parsed = parse_review_time(self._style_cfg().get("daily_review_time", "04:00"))
        return parsed or (4, 0)

    def due(self, now: datetime | None = None) -> bool:
        """现在是否该跑（到点 + 今天还没跑过）。"""
        now = now or self._now()
        if not self.review_enabled():
            return False
        meta = self._learner.features_meta()
        if str(meta.get("last_review_date") or "") == now.strftime("%Y-%m-%d"):
            return False
        hour, minute = self.review_time()
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        return now >= target

    def next_run_at(self, now: datetime | None = None) -> datetime:
        """下次运行时间（调度展示/测试用）。"""
        now = now or self._now()
        hour, minute = self.review_time()
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if now >= target:
            target = target + timedelta(days=1)
        return target

    # ------------------------------------------------------------------
    # 复盘主流程
    # ------------------------------------------------------------------
    async def run_review(self, now: datetime | None = None) -> dict:
        """跑一次复盘（判定 → 落地 → 达阈值则归纳）。

        返回摘要 dict；任何跳过/失败都以 {"skipped": 原因} 或抛异常外的
        静默形态表达（调用方只记日志）。"""
        now = now or self._now()
        if not self.review_enabled():
            # 红线 3：总开关关闭 → 零调用零写入（调度层有 due() 双保险）
            return {"skipped": "disabled"}
        today = now.strftime("%Y-%m-%d")
        meta = self._learner.features_meta()
        if str(meta.get("last_review_date") or "") == today:
            return {"skipped": "already_done_today"}

        usage = self._learner.usage_records()
        since = str(meta.get("last_review_date") or "")
        recent = [
            r for r in usage
            if str(r.get("ts") or "")[:10] > since
            or (not since and str(r.get("ts") or "")[:10] <= today)
        ]
        used_counts: dict[str, int] = {}
        for record in recent:
            for eid in record.get("entry_ids") or []:
                eid = str(eid).replace("feat:", "")
                used_counts[eid] = used_counts.get(eid, 0) + 1
        if not used_counts:
            # K3：无取用记录 → 零调用（只落日期，明天再看）
            self._learner.set_features_meta("last_review_date", today)
            return {"skipped": "no_usage"}

        entries_by_id = {
            str(e.get("id")): e
            for e in self._learner.entries()
            if str(e.get("id")) in used_counts
        }
        if not entries_by_id:
            # 记录里的条目都被清掉了：无对象可判 → 零调用
            self._learner.set_features_meta("last_review_date", today)
            return {"skipped": "no_entries"}

        # 一次汇总判定（调用数 1；归纳另有 1 次，合计 ≤ REVIEW_MAX_CALLS）
        entries_lines = []
        for eid, entry in entries_by_id.items():
            entries_lines.append(
                _entry_block_line(entry) + f"（本期取用 {used_counts[eid]} 次）"
            )
        reactions = []
        if self._reactions_getter is not None:
            try:
                result = self._reactions_getter()
                if hasattr(result, "__await__"):
                    result = await result
                reactions = [str(x) for x in (result or [])]
            except Exception as e:
                logger.debug(f"[StyleReview] 聊天摘录获取失败（只按取用判断）: {e}")
        prompt = self._review_prompt(
            "\n".join(entries_lines), "\n".join(reactions) or "（无摘录）"
        )
        try:
            raw = await self._llm_call(prompt, None)
        except Exception as e:
            logger.debug(f"[StyleReview] 判定调用失败（明天再试）: {e}")
            return {"skipped": "llm_failed"}
        if not str(raw or "").strip():
            self._learner.set_features_meta("last_review_date", today)
            return {"skipped": "llm_empty"}

        verdicts = self._parse_review(str(raw), set(entries_by_id))
        counts = self._learner.apply_review(verdicts, now)

        # 归纳（K2）：自上次归纳以来累计"判好"达到阈值才触发
        meta = self._learner.features_meta()
        good_since = _to_int(meta.get("good_since_induction"), 0) + counts["good"]
        self._learner.set_features_meta("good_since_induction", good_since)
        inducted = 0
        threshold = self._learner.feature_promote_threshold()
        if good_since >= threshold:
            try:
                inducted = await self._induct(now)
            except Exception as e:
                logger.debug(f"[StyleReview] 归纳失败（不影响复盘结果）: {e}")

        self._learner.set_features_meta("last_review_date", today)
        return {
            "judged": counts["changed"],
            "good": counts["good"],
            "bad": counts["bad"],
            "good_since_induction": good_since,
            "inducted": inducted,
        }

    def _review_prompt(self, entries_block: str, reactions_block: str) -> str:
        from .conf_path import conf_group
        from .prompts import read_template

        cfg = conf_group(self._config_getter() or {}, "style_learning")
        return render_template(
            read_template(cfg, "prompt_review", DEFAULT_PROMPT_REVIEW),
            {"entries_block": entries_block, "reactions_block": reactions_block},
            name="style_learning.prompt_review",
            default=DEFAULT_PROMPT_REVIEW,
        )

    def _parse_review(self, raw: str, allowed_ids: set[str]) -> dict:
        """解析判定输出 → {id: {verdict, reason}}；越界/不认识的 id 丢弃。"""
        data = strip_json_block(raw)
        if not isinstance(data, dict):
            return {}
        verdicts: dict = {}
        raw_entries = data.get("entries")
        if not isinstance(raw_entries, list):
            return {}
        for item in raw_entries:
            if not isinstance(item, dict):
                continue
            eid = str(item.get("id") or "").strip()
            verdict = str(item.get("verdict") or "").strip().lower()
            if eid not in allowed_ids:
                continue  # 不发明新 id
            if verdict not in ("good", "bad", "neutral"):
                continue
            verdicts[eid] = {
                "verdict": verdict,
                "reason": str(item.get("reason") or "")[:120],
            }
        return verdicts

    # ------------------------------------------------------------------
    # 沉淀归纳（K2）
    # ------------------------------------------------------------------
    async def _induct(self, now: datetime) -> int:
        """把高分条目归纳成沉淀特征（1 次调用）；返回入库条数。"""
        entries = self._learner.entries()

        def rank(e: dict):
            return (
                _to_int(e.get("review_good"), 0),
                _to_int(e.get("used_count"), 0),
                _to_float(e.get("weight"), 0.0),
            )

        top = sorted(entries, key=rank, reverse=True)[:8]
        top = [e for e in top if e.get("review_good") or e.get("used_count")]
        if len(top) < 2:
            # 证据不足不硬归纳（K2：要有"足够的新证据"）
            self._learner.set_features_meta("good_since_induction", 0)
            return 0
        from .conf_path import conf_group
        from .prompts import read_template

        cfg = conf_group(self._config_getter() or {}, "style_learning")
        prompt = render_template(
            read_template(cfg, "prompt_induct", DEFAULT_PROMPT_INDUCT),
            {"entries_block": "\n".join(_entry_block_line(e) for e in top)},
            name="style_learning.prompt_induct",
            default=DEFAULT_PROMPT_INDUCT,
        )
        raw = await self._llm_call(prompt, None)
        features = self._parse_induct(str(raw or ""))
        if not features:
            return 0
        built = []
        for i, feature in enumerate(features):
            source_ids = [str(e.get("id")) for e in top]
            built.append(
                {
                    "id": f"f{now.strftime('%Y%m%d%H%M%S')}:{i}",
                    "dims": feature["dims"],
                    "note": feature["note"],
                    "source_entry_ids": source_ids,
                    "created_at": now.isoformat(),
                    # 新特征重要度：来源越多略高（仍受上限与中位数口径约束的
                    # 同款精神——只略高，不独霸）
                    "importance": round(min(1.0 + 0.05 * len(source_ids), 1.3), 3),
                }
            )
        self._learner.replace_features(built)
        self._learner.set_features_meta("good_since_induction", 0)
        self._learner.set_features_meta("last_induction_date", now.strftime("%Y-%m-%d"))
        logger.info(f"[StyleReview] 沉淀归纳完成（{len(built)} 条特征）")
        return len(built)

    def _parse_induct(self, raw: str) -> list[dict]:
        data = strip_json_block(raw)
        if not isinstance(data, dict):
            return []
        raw_features = data.get("features")
        if not isinstance(raw_features, list):
            return []
        out = []
        for item in raw_features[:4]:
            if not isinstance(item, dict):
                continue
            dims = {}
            raw_dims = item.get("dims")
            if isinstance(raw_dims, dict):
                for key in DIMS_ORDER:
                    value = str(raw_dims.get(key) or "").strip()
                    if value:
                        dims[key] = value[:40]
            if not dims:
                continue
            out.append({"note": str(item.get("note") or "")[:80], "dims": dims})
        return out


def _to_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _to_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
