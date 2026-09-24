"""LivingLoop——主循环：心跳 → 闸门 → 活动周期 → 记忆 → （过闸门）分享。

安静纪律（任务书 M1 约束 6）：
  - 心跳间隔默认 45 分钟，且闸门不过就什么都不做；
  - 活动产出默认只写记忆；只有配置了 output_gate.target_sessions 才可能
    真正发消息，且每条都过消息闸门（上限/间隔/静默时段）；
  - 日志：关键生命周期用 INFO，判定与"本可发送"一律 DEBUG。

异常哲学：活动失败 ≠ 进程崩溃。活动周期整体 try/except，任何异常只记
日志，主循环必须活到下一轮心跳。

M3 双事件（任务书 A1）：睡眠可被两个事件打断——
  - config_event：配置变更 → **只重置定时器，不触发判定**（频繁改配置
    = 定时器反复重置，零判定零活动零 LLM 调用）；
  - wake_event：手动/吵醒唤醒 → 触发一次 force 判定（仍过闸门其余约束）。
"""

from __future__ import annotations

import asyncio
import json
import random
from contextlib import suppress
from datetime import datetime, timedelta
from typing import Any, Callable

from astrbot.api import logger

from .activities import Activity, ActivityContext, ActivityOutcome, default_activities
from .ghost_event import build_ghost_event
from .llm_failover import looks_like_llm_error_output
from .secrets_redact import redact_secrets

DEFAULT_CHECK_INTERVAL_MIN = 45.0
DEFAULT_MAX_RUN_SECONDS = 300.0
# 记忆写入单独限时：LivingMemory 引擎可能走嵌入 API，不能让它拖死活动周期
MEMORY_WRITE_TIMEOUT = 30.0
CONFIG_POLL_SECONDS = 5.0
DEFAULT_AGENT_ACTIVITIES = ("surf", "read", "game")
DREAM_MAX_CHARS = 120
# M7-补丁1 A1：分享文本最小长度。正常分享文案（梦、活动总结、睡过头交代）
# 都远超 4 字；空壳/占位通常 0-3 字。低于下限视为空产物——不调改写器、
# 不发送（根因：LLM 面对空材料会生成"你倒是发过来"式回应措辞）。
MIN_SHARE_TEXT_LEN = 4


def _to_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _conf_group(config: Any, group: str) -> dict:
    """配置组读取（补丁 XVIII 起代理到 conf_path：advanced 嵌套优先、
    平铺兜底）。保留本别名以最小化调用点改动。"""
    from .conf_path import conf_group

    try:
        return conf_group(config, group)
    except Exception:
        return {}


def derive_admin_identity(global_config: Any) -> dict:
    """从 AstrBot 全局配置（context.astrbot_config，即 cmd_config.json）提取
    管理员与平台信息（M9-补丁1 A2）——主人身份自动认领的数据源。

    返回 {"admins_id": [去空白后的管理员列表], "platform_id": 第一个
    enable=True 的适配器 id 或 None}；任何取不到的情形返回空列表/None，
    调用方据此走三段优先级的第 ③ 段（现状回退）。纯函数：只读不改。
    """
    cfg = global_config if isinstance(global_config, dict) else {}
    admins: list[str] = []
    for aid in cfg.get("admins_id") or []:
        text = str(aid).strip()
        if text:
            admins.append(text)
    platform_id = None
    for adapter in cfg.get("platform") or []:
        if (
            isinstance(adapter, dict)
            and adapter.get("enable")
            and str(adapter.get("id") or "").strip()
        ):
            platform_id = str(adapter["id"]).strip()
            break
    return {"admins_id": admins, "platform_id": platform_id}


class LivingLoop:
    """自主生活主循环。start/stop 幂等，可安全应对插件热重载。"""

    def __init__(
        self,
        gate: Any,
        memory_getter: Callable[[], Any],
        config_getter: Callable[[], Any],
        abilities: dict | None = None,
        activities: list[Activity] | None = None,
        sender: Any = None,
        rng: random.Random | None = None,
        sleep_func: Callable[[float], Any] | None = None,
        mood: Any = None,
        decider: Any = None,
        sleep_manager: Any = None,
        agent_loop: Any = None,
        dream_llm_call: Callable[..., Any] | None = None,
        persona_id_getter: Callable[..., Any] | None = None,
        bot_identity_getter: Callable[..., Any] | None = None,
        share_rewriter: Any = None,
        schedule: Any = None,
        global_config_getter: Callable[[], Any] | None = None,
    ) -> None:
        self._gate = gate
        self._get_memory = memory_getter
        self._config_getter = config_getter
        self._abilities = abilities or {}
        self._activities = activities if activities is not None else default_activities()
        self._sender = sender
        self._rng = rng or random.Random()
        # 可注入的 sleep：测试里换成即时返回，不用真等 45 分钟
        self._sleep = sleep_func or asyncio.sleep
        # M2：心境与决策层均可空——空则跳过相应逻辑（向后兼容）
        self._mood = mood
        self._decider = decider
        # M3：休眠结算器、agent 循环、梦生成 LLM，均可空
        self._sleep_manager = sleep_manager
        self._agent_loop = agent_loop
        self._dream_llm_call = dream_llm_call
        # M3 补丁：记忆写入时携带 persona id（问题 3，图谱参与者边的原料）
        self._persona_id_getter = persona_id_getter
        # M3 补丁 IV-B2：bot 身份（图谱 participant_identities 的原料），
        # 由 main 注入动态提取函数，loop 自己不碰平台配置
        self._bot_identity_getter = bot_identity_getter
        # M3 补丁 VIII：分享角色化改写器（None = 直发原文，向后兼容）
        self._share_rewriter = share_rewriter
        # M5-补丁4：起床约定（ScheduleManager）——睡过头认知/催醒加重的数据源
        self._schedule = schedule
        # M9-补丁1 A1：AstrBot 全局配置（context.astrbot_config）的动态读取——
        # 主人身份自动认领（owner_id / target_sessions 派生）的数据源。
        # None = 不派生，维持旧的手填语义（向后兼容）
        self._global_config_getter = global_config_getter
        # M3 补丁 IV-B1：活动周期互斥锁——心跳与 /living do 可能并发进入
        # 周期，双周期同时写记忆/同时调 LLM 既浪费 token 又可能数据竞争
        self._cycle_lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._watcher_task: asyncio.Task | None = None
        self._last_activity_name: str | None = None
        # /living pause：暂停的是"判定与活动"，心跳任务和定时器继续跑——
        # 这样 resume 立刻生效，也不会丢掉配置变更等事件
        self._paused = False
        # 双事件（任务书 A1）：配置变更重置定时器；手动/吵醒唤醒触发判定
        self._config_event = asyncio.Event()
        self._wake_event = asyncio.Event()
        # 睡眠状态跟踪（任务书 B2/B5）：入睡写回顾，醒来掷梦
        self._pending_dream: bool = False

    # ------------------------------------------------------------------
    # 对外事件入口（main.py 的命令/消息监听调用）
    # ------------------------------------------------------------------
    def notify_config_changed(self) -> None:
        """配置变更：只重置定时器（不触发判定）。"""
        self._config_event.set()

    def request_wake(self) -> None:
        """请求一次判定（force 语义仍受闸门其余约束）。"""
        self._wake_event.set()

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        """启动心跳任务。重复 start 幂等（已在跑就直接返回）。"""
        if self.running:
            return
        # 初始哈希在 start 里同步采集：若留给 watcher 首帧采集，启动慢时
        # 第一次哈希可能已经落在配置修改之后，变更会被当成初始值漏检
        self._last_config_hash = self._config_hash()
        self._task = asyncio.create_task(self._run(), name="living-loop")
        self._watcher_task = asyncio.create_task(
            self._config_watcher(), name="living-config-watcher"
        )
        logger.info("[LivingLoop] 主循环已启动")

    async def stop(self) -> None:
        """停止心跳任务。重复 stop 幂等。"""
        for name in ("_task", "_watcher_task"):
            task = getattr(self, name, None)
            if task is None:
                continue
            setattr(self, name, None)
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        logger.info("[LivingLoop] 主循环已停止")

    async def pause(self) -> None:
        """/living pause：暂停判定与活动（幂等）。心跳任务保持运行，
        定时器照常转，唤醒/配置事件照常接收——只是不判定不活动。"""
        if self._paused:
            return
        self._paused = True
        logger.info("[LivingLoop] 已暂停（心跳保持，判定跳过）")

    async def resume(self) -> None:
        """/living resume：恢复判定（幂等）。"""
        if not self._paused:
            return
        self._paused = False
        logger.info("[LivingLoop] 已恢复")

    @property
    def paused(self) -> bool:
        return self._paused

    async def _config_watcher(self) -> None:
        """配置变更兜底检测（任务书 A4）：每 5 秒序列化比对一次。

        为什么用哈希快照而不是保存配置对象引用：AstrBotConfig 是原地修改
        对象，存引用永远等于自己；json 序列化哈希每次取的是当前状态。
        纯内存比对，零 LLM 零网络。若 AstrBot 未来提供配置变更钩子，
        main 可直接调 notify_config_changed()，本 watcher 留作兜底。
        """
        last = getattr(self, "_last_config_hash", None) or self._config_hash()
        while True:
            await asyncio.sleep(CONFIG_POLL_SECONDS)
            try:
                current = self._config_hash()
            except asyncio.CancelledError:
                raise
            except Exception:
                continue
            if current != last:
                last = current
                logger.debug("[LivingLoop] 检测到配置变更 → 重置定时器")
                self.notify_config_changed()

    def _config_hash(self) -> str:
        return json.dumps(
            self._config_getter() or {},
            sort_keys=True,
            ensure_ascii=False,
            default=str,
        )

    async def _run(self) -> None:
        while True:
            interval_min = min(
                max(
                    _to_float(
                        _conf_group(self._config_getter(), "decision").get(
                            "impulse_check_interval_minutes"
                        ),
                        DEFAULT_CHECK_INTERVAL_MIN,
                    ),
                    1.0,
                ),
                1440.0,
            )
            # 双事件等待（任务书 A1）：任一事件或超时都会打断睡眠。
            # 独立审计项 3：asyncio.wait 自身被取消（如 stop()）时**不会**
            # 自动取消内层 task——必须在这里兜底取消，否则两个 event.wait
            # 协程会以 pending 状态泄漏到事件循环关闭
            config_wait = asyncio.ensure_future(self._config_event.wait())
            wake_wait = asyncio.ensure_future(self._wake_event.wait())
            try:
                done, pending = await asyncio.wait(
                    {config_wait, wake_wait},
                    timeout=interval_min * 60,
                    return_when=asyncio.FIRST_COMPLETED,
                )
            except asyncio.CancelledError:
                for task in (config_wait, wake_wait):
                    task.cancel()
                await asyncio.gather(
                    config_wait, wake_wait, return_exceptions=True
                )
                raise
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

            if self._paused:
                # /living pause：定时器照常转，判定与活动全部跳过。
                # 事件照常消费（清掉），避免 resume 后旧事件突然触发
                if wake_wait in done:
                    self._wake_event.clear()
                if config_wait in done:
                    self._config_event.clear()
                logger.info("[LivingLoop] 暂停中，跳过本轮判定")
                continue

            if wake_wait in done:
                # 手动/吵醒唤醒：触发一次 force 判定（概率豁免、约束保留）
                self._wake_event.clear()
                logger.info("[LivingLoop] 被唤醒（wake_event），执行 force 判定")
                try:
                    await self.heartbeat_once(force=True)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("[LivingLoop] 强制心跳异常（主循环继续）")
                continue
            if config_wait in done:
                # 配置变更：只重置定时器，**不判定**（任务书 A1 定稿语义）
                self._config_event.clear()
                logger.debug("[LivingLoop] 配置变更，定时器已重置（本轮不判定）")
                continue
            # 超时 → 正常心跳
            try:
                await self.heartbeat_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("[LivingLoop] 心跳异常（主循环继续）")

    # ------------------------------------------------------------------
    # 心跳与活动周期
    # ------------------------------------------------------------------
    async def heartbeat_once(
        self, now: datetime | None = None, force: bool = False
    ) -> bool:
        """一次冲动检查。返回是否真的进入了活动周期。

        force=True（任务书 A2）：豁免概率掷点，仍受休眠窗/上限/冷却约束；
        休眠窗内的强制唤醒会触发吵醒结算（起床气/睡眠债）。
        """
        awake, _reason, _activity = await self.heartbeat_once_detailed(now, force)
        return awake

    async def heartbeat_once_detailed(
        self, now: datetime | None = None, force: bool = False
    ) -> tuple[bool, str, str | None]:
        """带详情的心跳：/living_wake 命令用反馈给主人（唤醒/拦截原因）。"""
        now = now or datetime.now()

        # 清醒待机（补丁 II 三）：刚过期则清除（M6-补丁1：fixed 翻转段
        # 已随 fixed 机制移除，待机过期处理保留——待机是两模式共用机制）
        try:
            standby_expired = await self._gate.consume_standby_expiry(now)
        except Exception:
            standby_expired = False
        if standby_expired:
            logger.info("[LivingLoop] 清醒待机结束")

        # M3 补丁 X：自主作息——到点自然醒（结算恢复）与白天小睡。
        # M6-补丁1：fixed 机制已移除，autonomous 是唯一睡眠行为。
        await self._autonomous_sleep_tick(now)

        allow, reason = await self._gate.should_wake(now, force=force)
        if not allow:
            return False, reason, None

        if reason == "woken_from_sleep":
            # 吵醒结算（任务书 B3）：起床气 + 睡眠债，然后带着情绪醒来；
            # 随后进入清醒待机（补丁 II 一）并立刻回主人一句确认（补丁 II 二）
            if self._sleep_manager is not None:
                # M6-补丁1：fixed 结算已随机制移除，自主结算是唯一路径
                state = self._gate.sleep_state(now)
                fell = state.get("fell_asleep_at") or now
                actual_h = max((now - fell).total_seconds() / 3600.0, 0.0)
                planned_h = await self._planned_sleep_hours()
                kind = state.get("kind") or "long"
                # M5-补丁4 C2：存在已过期未兑现的起床约定（明知有约还
                # 睡过头被催醒）→ 起床气概率 ×2
                grouchy_boost = False
                if self._schedule is not None:
                    try:
                        grouchy_boost = (
                            await self._schedule.overdue_unfulfilled(now)
                        ) is not None
                    except Exception:
                        grouchy_boost = False
                try:
                    settle = await self._sleep_manager.apply_woken_from_autonomous(
                        self._mood, actual_h, planned_h, kind=kind,
                        grouchy_boost=grouchy_boost,
                    )
                    logger.info(
                        f"[LivingLoop] 自主睡眠被吵醒（实睡 {actual_h:.1f}h/"
                        f"预计 {planned_h:.1f}h），起床气={settle['grouchy']} "
                        f"债务+{settle['debt_added']}"
                    )
                except Exception as e:
                    logger.warning(f"[LivingLoop] 自主吵醒结算失败: {e}")
                # 立即退出自主睡眠状态——不退出的话 is_asleep_now 继续
                # 返回 True → 静默拦截持续生效（补丁 XI-A.1 根因）
                await self._gate.exit_autonomous_sleep(now)
                try:
                    minutes = await self._sleep_manager.begin_standby(now)
                    logger.info(
                        f"[LivingLoop] 被连续消息唤醒，进入清醒待机 {minutes:.0f} 分钟"
                    )
                except Exception as e:
                    logger.warning(f"[LivingLoop] 进入待机失败: {e}")
                await self._send_wake_ack()
            self._pending_dream = True

        result = await self.run_activity_cycle(now=now)
        activity_name = result.get("activity") if isinstance(result, dict) else None

        if self._pending_dream:
            self._pending_dream = False
            await self._maybe_dream(now)
        return True, reason, activity_name

    async def _send_wake_ack(self) -> None:
        """唤醒确认消息（补丁 II 二）：零延迟回主人一句，纯 sender 零 token。

        发往触发吵醒的最后一个会话；留空配置/无会话/发送失败一律静默
        （WARNING），不影响后续活动周期。
        """
        ack = str(
            _conf_group(self._config_getter(), "sleep").get("wake_ack_message", "")
            or ""
        ).strip()
        if not ack:
            return
        session = (
            self._sleep_manager.last_wake_session
            if self._sleep_manager is not None
            else None
        )
        if not session or self._sender is None:
            return
        try:
            sent = await self._sender.send(session, ack)
            if not sent:
                logger.warning("[LivingLoop] 唤醒确认消息未送达（无匹配平台）")
        except Exception as e:
            logger.warning(f"[LivingLoop] 唤醒确认消息发送失败: {e}")

    async def _send_sleep_farewell(self, now: datetime) -> None:
        """入睡告别消息（M6-补丁1 C1：触发源从 fixed 翻转迁移至 autonomous
        长睡入睡；小睡不发送）。

        `sleep_farewell_message` 非空才发送；发往待机期最后活跃会话
        （无会话/发送失败一律静默，不影响入睡）。纯 sender 零 token。
        """
        farewell = str(
            _conf_group(self._config_getter(), "sleep").get(
                "sleep_farewell_message", ""
            )
            or ""
        ).strip()
        if not farewell:
            return
        session = (
            self._sleep_manager.last_active_session
            if self._sleep_manager is not None
            else None
        )
        if not session or self._sender is None:
            return
        try:
            sent = await self._sender.send(session, farewell)
            if not sent:
                logger.warning("[LivingLoop] 入睡告别消息未送达（无匹配平台）")
        except Exception as e:
            logger.warning(f"[LivingLoop] 入睡告别消息发送失败: {e}")

    async def _autonomous_sleep_tick(self, now: datetime) -> None:
        """自主作息心跳（补丁 X）：到点自然醒结算 / 不在睡时评估入睡与小睡。

        M6-补丁1：fixed 机制已移除，本 tick 是唯一的睡眠行为；
        manager 或 mood 未注入（早期构造/局部替身）时短路返回——
        入睡评估与结算都依赖心境。
        """
        if self._sleep_manager is None or self._mood is None:
            return
        state = self._gate.sleep_state(now)

        # 1) 到点自然醒：结算恢复（debt 按实睡比例、energy 恢复）+ 日志。
        #    M5-补丁2：原条件 `state["asleep"] and now >= until` 恒为 False
        #    （asleep 本身即 now < until）——自然醒结算是死代码：小睡记账
        #    （A8）永不发生、长睡醒来不恢复精力（VM 上 energy 恒保底 0.05
        #    的真正根源）。改为"存在已过期的睡眠记录即结算"。
        expired = (
            state["until"] is not None
            and state["fell_asleep_at"] is not None
            and now >= state["until"]
        )
        if expired:
            kind = state["kind"] or "long"
            fell = state.get("fell_asleep_at") or now
            actual_h = max((now - fell).total_seconds() / 3600.0, 0.0)
            if self._mood is not None:
                from .mood import apply_nap_effects, restore_after_sleep  # 相对导入：插件以包形式加载，绝对导入 core.* 在运行时解析不到

                if kind == "long":
                    planned = await self._planned_sleep_hours()
                    detail = await restore_after_sleep(
                        self._mood, actual_h, planned
                    )
                    logger.info(
                        f"[Sleep] 自然醒（实睡 {actual_h:.1f} 小时），"
                        f"能量恢复 {detail['energy']:.2f}，"
                        f"债务剩余 {detail['debt_remaining']:.0f}"
                    )
                    # M5-补丁4 C1/C3：核对起床约定——迟到写自我认知记忆
                    # （幂等：结算即消费该约定），0.5 概率主动交代
                    await self._handle_oversleep_commitment(now)
                else:
                    detail = await apply_nap_effects(self._mood, actual_h * 60.0)
                    logger.info(
                        f"[Sleep] 白天小睡 {actual_h * 60:.0f} 分钟"
                        f"（精力 {detail['energy']:.2f}）"
                    )
            await self._gate.exit_autonomous_sleep(now)
            self._pending_dream = True  # 自然醒掷梦（沿用补丁 II 链路）
            return

        if state["asleep"]:
            return  # 还在睡（静默/计数/紧急唤醒由既有链路处理）

        # 2.5) 醒着时的连续结算（M5-补丁2 A4/C3）：睡眠债按清醒经过时长
        # 累积、兴趣按经过时长衰减——心跳级推进，替代旧的跨日一次性结算
        if self._mood is not None:
            try:
                fatigue_rate = _to_float(
                    _conf_group(self._config_getter(), "sleep").get(
                        "fatigue_rate_per_hour"
                    ),
                    4.0,
                )
                await self._mood.accrue_sleep_debt(now, rate_per_hour=fatigue_rate)
                daily_decay = _to_float(
                    _conf_group(self._config_getter(), "decision").get(
                        "interest_daily_decay"
                    ),
                    0.9,
                )
                await self._mood.decay_interests_elapsed(
                    now, daily_decay=daily_decay
                )
            except Exception as e:
                logger.warning(f"[LivingLoop] 清醒结算失败（不影响心跳）: {e}")

        # 3) 先判长睡（M5-补丁2 A5 顺序反转），睡意达阈值即长睡；
        #    未达阈值才考虑小睡（受冷却/每日上限/夜间禁睡/min_awake 约束）。
        #    旧顺序"先小睡后长睡"让小睡分支恒先命中并 return，长睡不可达。
        result = await self._sleep_manager.begin_autonomous_sleep(
            self._mood, now
        )
        if result.get("asleep"):
            logger.info(
                f"[Living] 进入自主睡眠，预计 "
                f"{result['until'].strftime('%H:%M')} 自然醒"
            )
            # M5-补丁3 B2：autonomous 的睡前回顾由长睡入睡触发
            # M6-补丁1 C1：入睡告别同点触发（fixed 翻转段已随机制移除；
            # 小睡两者都不触发）
            await self._write_bedtime_review(now)
            await self._send_sleep_farewell(now)
            return

        nap = self._sleep_manager.should_nap(self._mood, now) if self._mood is not None else (False, 0.0)
        if nap[0]:
            minutes = nap[1]
            until = now + timedelta(minutes=minutes)
            await self._gate.enter_autonomous_sleep(until, "nap", now)
            logger.info(f"[Sleep] 白天小睡 {minutes:.0f} 分钟（精力不足，补觉）")

    async def _planned_sleep_hours(self) -> float:
        """本次入睡时记录的预计时长（供醒来比例结算）。"""
        try:
            state = self._gate.sleep_state()
            fell = state.get("fell_asleep_at")
            until = state.get("until")
            if fell and until:
                return max((until - fell).total_seconds() / 3600.0, 0.1)
        except Exception:
            pass
        return 8.0

    async def _handle_oversleep_commitment(self, wake_time: datetime) -> bool:
        """自然醒后核对起床约定（M5-补丁4 C1/C3）。

        consume_due_wake 取走 target 已到的约定（无论守时与否都清除——
        C3 兑现或过期后从存储清除；清除同时是幂等保证：同一约定只结算
        一次）。迟到超过 15 分钟容差 → 写第一人称"睡过头"认知记忆
        （importance 0.5、身份注入，按 M5-补丁2 后写入规范），并以 0.5
        概率经既有 _maybe_share 主动向主人交代。任何失败只 WARNING。"""
        if self._schedule is None:
            return False
        try:
            commitment = await self._schedule.consume_due_wake(wake_time)
        except Exception as e:
            logger.warning(f"[Schedule] 约定核对失败（跳过）: {e}")
            return False
        if commitment is None:
            return False
        try:
            target = datetime.fromisoformat(str(commitment["target_time"]))
        except (TypeError, ValueError, KeyError):
            return False
        late_minutes = (wake_time - target).total_seconds() / 60.0
        if late_minutes <= 15.0:
            logger.info(
                f"[Schedule] 守时：约定 {target:%H:%M}，实际 {wake_time:%H:%M} 起床"
            )
            return False
        note = (
            f"我睡过头了。本来约好 {target:%H:%M} 起床，结果一觉睡到 "
            f"{wake_time:%H:%M}，晚了 {late_minutes:.0f} 分钟。"
        )
        try:
            memory = await self._get_memory()
        except Exception as e:
            logger.warning(f"[Schedule] 记忆不可用，睡过头认知未写入: {e}")
            return False
        identity = await self._bot_identity()
        metadata: dict = {"topics": ["睡过头"]}
        if identity:
            metadata["participant_identities"] = [identity]
        try:
            await memory.add(
                note,
                importance=0.5,
                metadata=metadata,
                session_id=self._session_id(None),
                persona_id=await self._persona_id(),
            )
            logger.info(
                f"[Schedule] 睡过头 {late_minutes:.0f} 分钟，已写入自我认知记忆"
            )
        except Exception as e:
            logger.warning(f"[Schedule] 睡过头认知写入失败: {e}")
            return False
        # 分享掷点：rng 兼容 Random 实例与裸函数两种注入形态
        rng = self._rng
        roll = rng.random() if hasattr(rng, "random") else rng()
        if roll < 0.5:
            try:
                await self._maybe_share(note, wake_time)
            except Exception as e:
                logger.warning(f"[Schedule] 睡过头交代发送失败: {e}")
        return True

    @staticmethod
    def _is_bedtime_review(row: Any) -> bool:
        """判断一条记忆是否是"睡前回顾"自身（M5-补丁2 B1）。

        以 metadata.topics 标记为准（兼容 list / 字符串 / 缺失三种形态）。
        """
        if not isinstance(row, dict):
            return False
        meta = row.get("metadata") or {}
        if not isinstance(meta, dict):
            return False
        topics = meta.get("topics") or []
        if isinstance(topics, str):
            topics = [topics]
        return any(str(t) == "睡前回顾" for t in topics)

    async def _write_bedtime_review(self, now: datetime) -> None:
        """睡前回顾（任务书 B2）：把今天的活动记忆聚成一句话存起来。

        用脚本聚合而非 LLM：回顾的价值在"记下来了"，不在辞藻——省下的
        token 留给梦。

        M5-补丁2 质检：B1 检索结果过滤掉回顾自身（旧的日期前缀正文会被
        自己检索命中，回声层层嵌套）；B2 同一天幂等（一天只写一条）；
        B3 正文不再以日期字符串开头（缩小自我命中面）。
        """
        try:
            memory = await self._get_memory()
        except Exception as e:
            logger.warning(f"[LivingLoop] 睡前回顾：记忆不可用，跳过（{e}）")
            return
        try:
            # B2：先查今天是否已写过（新格式正文含"今天想了想"，可被检索命中）
            recent = await memory.search("今天想了想", k=10)
            for row in recent or []:
                if not self._is_bedtime_review(row):
                    continue
                meta = row.get("metadata") or {}
                if str(meta.get("review_date") or "") == now.date().isoformat():
                    logger.info("[LivingLoop] 睡前回顾今日已写，跳过（幂等）")
                    return
        except Exception as e:
            logger.debug(f"[LivingLoop] 睡前回顾幂等检查失败（继续）: {e}")

        date_key = f"{now.month}月{now.day}日"
        try:
            rows = await memory.search(date_key, k=8)
        except Exception as e:
            logger.debug(f"[LivingLoop] 睡前回顾检索失败: {e}")
            rows = []
        # B1：过滤掉回顾自身——把回声当作"今天做的事"再写一遍会层层嵌套
        rows = [r for r in (rows or []) if not self._is_bedtime_review(r)]
        contents = [str(r.get("content", "")).strip() for r in rows]
        contents = [c for c in contents if c][:3]
        if contents:
            review = f"今天想了想：{'；'.join(c[:40] for c in contents)}。该睡了，晚安。"
        else:
            review = "今天是安静的一天，没做成什么事。该睡了，晚安。"
        # 补丁 XX：带上 bot 身份——否则这类记忆在图谱里没有 person 节点，
        # 会形成孤立分量（补丁 IV 引入 participant_identities 时漏了本路径）
        identity = await self._bot_identity()
        review_metadata: dict = {"topics": ["睡前回顾"], "review_date": now.date().isoformat()}
        if identity:
            review_metadata["participant_identities"] = [identity]
        try:
            await memory.add(
                review,
                importance=0.6,
                metadata=review_metadata,
                session_id=self._session_id(None),
                persona_id=await self._persona_id(),
            )
            logger.info("[LivingLoop] 已写入睡前回顾")
        except Exception as e:
            logger.warning(f"[LivingLoop] 睡前回顾写入失败: {e}")

    async def _maybe_dream(self, now: datetime) -> None:
        """梦（任务书 B5）：醒来后的低概率彩蛋，任何失败都静默。"""
        if self._dream_llm_call is None:
            return
        try:
            from .living_state import _conf_group

            probability = _to_float(
                _conf_group(self._config_getter(), "sleep").get("dream_probability"),
                0.3,
            )
        except Exception:
            probability = 0.3
        if self._rng.random() >= max(probability, 0.0):
            return

        try:
            memory = await self._get_memory()
            rows = await memory.search("", k=3)
        except Exception as e:
            logger.debug(f"[LivingLoop] 梦的素材取不到，今晚不做梦: {e}")
            return
        fragments = [str(r.get("content", "")).strip()[:60] for r in rows or []]
        fragments = [f for f in fragments if f]
        if not fragments:
            return
        prompt = (
            "你刚从睡梦中醒来，还带着睡意。下面是你最近的记忆碎片：\n"
            + "\n".join(f"- {f}" for f in fragments)
            + "\n\n请说一句你刚才做的梦，80 字以内，第一人称，语气朦胧含糊，"
            "把碎片搅在一起也没关系，梦本来就是不讲道理的。只输出梦话本身。"
        )
        try:
            text = await self._dream_llm_call(prompt, None)
        except Exception as e:
            logger.debug(f"[LivingLoop] 梦生成失败（梦丢了就丢了）: {e}")
            return
        dream = str(text or "").strip()[:DREAM_MAX_CHARS]
        if not dream:
            return
        date_key = f"{now.month}月{now.day}日"
        # 补丁 XX：梦同样要带身份与 topic（否则图谱里是孤立节点）
        identity = await self._bot_identity()
        dream_metadata: dict = {"topics": ["梦"]}
        if identity:
            dream_metadata["participant_identities"] = [identity]
        try:
            await memory.add(
                f"{date_key}我做了个梦：{dream}",
                importance=0.2,
                metadata=dream_metadata,
                session_id=self._session_id(None),
                persona_id=await self._persona_id(),
            )
        except Exception as e:
            logger.debug(f"[LivingLoop] 梦的记忆写入失败: {e}")
            return
        logger.info("[LivingLoop] 醒来做了个梦（已写入记忆）")
        await self._maybe_share(f"我好像做了个梦：{dream}", now)

    async def run_activity_cycle(
        self,
        now: datetime | None = None,
        force_activity: str | None = None,
        force_topic: str | None = None,
    ) -> dict:
        """一次完整活动周期：起念 → 活动 → 记忆（双路径）→ 收账 → 候选分享。

        force_activity（/living do）：跳过决策与随机选择，直接执行指定活动；
        force_topic 覆盖主题词。跳过概率与冷却（主人说了就做），但**每日
        上限照拦**（红线：防刷）——拦下时不消耗配额。

        M3 补丁 IV-B1：全程持有互斥锁——心跳与 /living do 并发调用时排队
        串行，杜绝双周期同时写记忆/同时调 LLM。
        """
        async with self._cycle_lock:
            return await self._run_activity_cycle_locked(
                now=now,
                force_activity=force_activity,
                force_topic=force_topic,
            )

    async def _run_activity_cycle_locked(
        self,
        now: datetime | None = None,
        force_activity: str | None = None,
        force_topic: str | None = None,
    ) -> dict:
        now = now or datetime.now()
        activity_id = now.strftime("%Y%m%d_%H%M%S")
        # 幽灵事件（M0-R0 结论）：M1 活动直接调能力用不到它，但它是 M2 接入
        # tool_loop_agent 的唯一合法事件形态，构造好放进活动上下文。
        ghost_event = build_ghost_event(session_id=f"living_{activity_id}")

        forced = None
        if force_activity:
            effective = self._effective_activities()
            forced = next(
                (a for a in effective if a.name == force_activity), None
            )
            if forced is None:
                known = "/".join(a.name for a in effective)
                return {
                    "activity": force_activity, "ok": False,
                    "error": f"未知活动 {force_activity!r}（可选: {known}）",
                }
            # 每日上限是 force 也碰不了的红线（任务书：防刷）
            reached, count, limit = await self._gate.daily_limit_info(now)
            if reached:
                logger.info(
                    f"[LivingLoop] /living do 被每日上限拦下（{count}/{limit}）"
                )
                return {
                    "activity": force_activity, "ok": False,
                    "error": f"daily_limit（{count}/{limit}）",
                }

        # 记忆后端先就位：它挂了的话活动没法写记忆，这轮直接放弃（不耗配额）
        try:
            memory = await self._get_memory()
        except Exception as e:
            logger.error(f"[LivingLoop] 记忆后端不可用，本轮放弃: {e}")
            return {"activity": None, "ok": False, "error": "memory_unavailable"}

        await self._gate.note_activity_started(now)
        if forced is not None:
            activity = forced
            params = {"topic": force_topic} if force_topic else {}
        else:
            activity, params = await self._choose_activity(now)
        logger.info(f"[LivingLoop] 活动开始 name={activity.name} id={activity_id}")

        ctx = ActivityContext(
            searcher=self._abilities.get("searcher"),
            fetcher=self._abilities.get("fetcher"),
            sandbox=self._abilities.get("sandbox"),
            memory=memory,
            gate=self._gate,
            event=ghost_event,
            rng=self._rng,
            now=now,
            params=params,
            agent=self._agent_callable(activity.name),
            # M3 补丁 VII：近期话题与兴趣加权数据通道（执行层去偏执兜底）
            recent_topics=(
                (lambda: self._mood.recent_topics_list())()
                if self._mood is not None
                and callable(getattr(self._mood, "recent_topics_list", None))
                else []
            ),
            interest_penalty_table=tuple(
                self._recent_topic_penalty_table()
            ),
            mood=self._mood,
        )

        outcome = None
        error_note: str | None = None
        real_start = datetime.now()  # 疲惫按真实耗时折算，不用注入的 now
        # 非正值/脏值回默认；上限 1h 防止配置手滑把一次活动拖成半天
        max_run = min(
            _to_float(
                _conf_group(self._config_getter(), "decision").get("max_run_seconds"),
                DEFAULT_MAX_RUN_SECONDS,
            ),
            3600.0,
        )
        if max_run <= 0:
            max_run = DEFAULT_MAX_RUN_SECONDS
        try:
            outcome = await asyncio.wait_for(
                activity.run(ctx), timeout=max_run
            )
        except asyncio.TimeoutError:
            error_note = f"活动 {activity.name} 超时（>{max_run:.0f}s），被强杀"
            logger.error(f"[LivingLoop] {error_note}")
        except asyncio.CancelledError:
            # 主循环停止：不吞（让 stop() 语义成立），但把账记平
            await self._gate.note_activity_finished()
            raise
        except Exception as e:
            error_note = f"活动 {activity.name} 失败: {e}"
            logger.error(f"[LivingLoop] {error_note}")

        # 任务书问题 2：LLM 错误文本不进记忆。VM 实测里 provider 全挂时
        # agent 的"产出"就是 "All chat models failed: ..." 这类错误串——
        # 当成果写进记忆会污染记忆库、拖垮后续决策 prompt 的质量。
        model_failure = False
        if outcome is not None and looks_like_llm_error_output(
            f"{outcome.summary or ''} {outcome.memory_content or ''}"
        ):
            logger.warning(
                f"[LivingLoop] 活动 {activity.name} 的产出是 LLM 错误信息，按失败处理"
            )
            model_failure = True
            error_note = "LLM 错误信息，已拦截不入记忆"
            outcome = None  # 失败路径：心境记失败、不分享、用专属失败文案

        # 心境演化与记忆重要度调节（M2-D；M3 起疲惫按活动耗时折算）
        duration_seconds = (datetime.now() - real_start).total_seconds()
        importance_adjust = await self._update_mood(
            activity, outcome, params, duration_seconds
        )
        # 记忆双路径：无论成败都写（任务书 D）
        failure_text = (
            f"{ctx.date_prefix()}我想做{activity.name}来着，"
            "但脑子转不动（模型全挂了）。"
            if model_failure
            else None
        )
        await self._write_memory(
            activity, outcome, error_note, ctx, importance_adjust,
            failure_text=failure_text,
        )
        await self._gate.note_activity_finished()
        logger.info(f"[LivingLoop] 活动结束 name={activity.name}")

        # 候选分享（内部过输出闸门）。M7-补丁1 A2：空/纯空白 summary 不进
        # 分享——与 _maybe_share 入口防线（A1）相互独立，双防线
        if outcome is not None and outcome.summary and str(outcome.summary).strip():
            await self._maybe_share(outcome.summary, now)
        return {
            "activity": activity.name,
            "ok": error_note is None,
            "error": error_note,
        }

    @property
    def activity_names(self) -> list[str]:
        return [a.name for a in self._effective_activities()]

    def _effective_activities(self) -> list[Activity]:
        """活动池（补丁 XV 清单3）：decision.free_activity_enabled=false 时
        摘除 free。现读配置——开关热生效，覆盖随机选择与 /living do 指名。"""
        try:
            raw = _conf_group(self._config_getter(), "decision").get(
                "free_activity_enabled"
            )
            if raw is not None and not bool(raw):
                return [a for a in self._activities if a.name != "free"]
        except Exception:
            pass
        return self._activities

    def _recent_topic_penalty_table(self) -> tuple:
        """重复惩罚表（配置 recent_topic_penalty，缺省 0.5/0.3/0.15）。"""
        try:
            raw = _conf_group(self._config_getter(), "decision").get(
                "recent_topic_penalty"
            )
            values = [float(v) for v in (raw or [])]
            if values:
                return tuple(values)
        except Exception:
            pass
        return (0.5, 0.3, 0.15)

    def _recent_topic_window(self) -> int:
        return max(
            int(
                _to_float(
                    _conf_group(self._config_getter(), "decision").get(
                        "recent_topic_window"
                    ),
                    6,
                )
            ),
            1,
        )

    def _agent_callable(self, activity_name: str) -> Callable[..., Any] | None:
        """按配置 decision.agent_activities 决定该活动是否走 agent 模式
        （任务书 C1）。返回 None = 脚本模式。"""
        if self._agent_loop is None:
            return None
        try:
            enabled = (
                _conf_group(self._config_getter(), "decision").get("agent_activities")
                or list(DEFAULT_AGENT_ACTIVITIES)
            )
        except Exception:
            enabled = list(DEFAULT_AGENT_ACTIVITIES)
        if isinstance(enabled, str):
            enabled = [enabled]
        if activity_name not in list(enabled):
            return None
        return self._agent_loop.run

    async def _choose_activity(self, now: datetime) -> tuple[Activity, dict]:
        """M2：决策层优先（rules/hybrid/llm 三档），未接线时退回 M1 随机。"""
        if self._decider is not None:
            try:
                decision = await self._decider.decide(now)
                return decision.activity, dict(decision.params or {})
            except Exception as e:
                # 决策器自身抛异常：退回内部随机，生活照常
                logger.warning(f"[LivingLoop] 决策器异常，回退随机选择: {e}")
        return self._pick_activity(), {}

    async def _update_mood(
        self,
        activity: Activity,
        outcome: Any,
        params: dict | None,
        duration_seconds: float = 0.0,
    ) -> float:
        """活动结束后更新心境；返回记忆重要度调节量。

        低谷时的小确幸记得更牢：valence < 0 时成功活动的记忆重要度 +0.1
        （任务书 M2-D）。M3 起疲惫按活动实际耗时折算（fatigue_rate_per_hour
        配置热读）。心境更新失败不影响活动记账。
        """
        if self._mood is None:
            return 0.0
        ok = outcome is not None
        valence_before = self._mood.valence
        topic = (params or {}).get("topic")
        fatigue_rate = _to_float(
            _conf_group(self._config_getter(), "sleep").get("fatigue_rate_per_hour"),
            4.0,
        )
        try:
            await self._mood.record_activity(
                activity.name,
                ok,
                topic=topic if isinstance(topic, str) else None,
                duration_seconds=duration_seconds,
                fatigue_rate_per_hour=fatigue_rate,
            )
        except Exception as e:
            logger.warning(f"[LivingLoop] 心境更新失败（活动仍算完成）: {e}")
        # 近期话题追踪（补丁 VII 需求 2）：记录活动实际使用的主题——
        # outcome.topics 是执行层回填的真实主题，比决策 params 更可信
        try:
            used_topics = list(getattr(outcome, "topics", None) or [])
            if not used_topics and isinstance(topic, str) and topic.strip():
                used_topics = [topic.strip()]
            if used_topics:
                recorder = getattr(self._mood, "record_recent_topics", None)
                if callable(recorder):
                    recorder(used_topics, window=self._recent_topic_window())
        except Exception as e:
            logger.warning(f"[LivingLoop] 近期话题记录失败（不影响）: {e}")
        if ok and valence_before < 0:
            return 0.1
        return 0.0

    async def _write_memory(
        self,
        activity: Activity,
        outcome: Any,
        error_note: str | None,
        ctx: ActivityContext,
        importance_adjust: float = 0.0,
        failure_text: str | None = None,
    ) -> None:
        if outcome is not None and outcome.memory_content:
            content = outcome.memory_content
            importance = outcome.importance
        else:
            # 失败也是生活的一部分；模型故障用专属文案（任务书问题 2 定稿）
            detail = f"（{error_note}）" if error_note else ""
            content = failure_text or (
                f"{ctx.date_prefix()}我想{activity.name}来着，没成{detail}。"
            )
            importance = 0.2
        # 心境调节后的重要度仍要钳在合理区间
        importance = max(0.0, min(1.0, importance + importance_adjust))
        # 独立审计项 4：失败详情可能带密钥形态的敏感串（openai 异常携带的
        # 请求信息等），写记忆前统一脱敏——记忆库会进决策 prompt，泄漏进去
        # 就是长期隐患
        content = redact_secrets(content)
        # 任务书 M3 补丁 III：topics 随 metadata 进 LivingMemory——图谱提取
        # 器（_extract_legacy）靠它生成 topic 节点与 describes 边；失败路径
        # outcome 为 None → 无 topics → 裸 fact（失败记忆低价值，孤立可接受）
        metadata: dict = {}
        if outcome is not None and outcome.topics:
            metadata["topics"] = outcome.topics
        # M3 补丁 V 问题 4：key_facts 让图谱提取器生成独立的 fact 节点，
        # topic 节点通过 describes 边连接到它——没有它就只有孤立的
        # topic→content 关系对，成不了"主题-事实"完整结构
        if outcome is not None and outcome.memory_content:
            metadata["key_facts"] = [outcome.memory_content]
        # M3 补丁 IV-B2：participant_identities——给记忆挂上 bot 的 person
        # 节点原料，EntityResolver 会把它与原生对话记忆的同名身份映射到
        # 同一节点，插件记忆集群由此桥接进主图谱（不再孤立）
        identity = await self._bot_identity()
        if identity:
            metadata["participant_identities"] = [identity]
        try:
            memory = await self._get_memory()
            await asyncio.wait_for(
                memory.add(
                    content,
                    importance=importance,
                    metadata=metadata,
                    # 任务书问题 3：带上会话与人格上下文——LivingMemory 的
                    # 图谱提取器靠它们生成参与者边，传 None 只会得到孤立节点。
                    # 幽灵事件的 uwo 是自主活动记忆在图谱里的"家"
                    session_id=self._session_id(ctx),
                    persona_id=await self._persona_id(),
                ),
                timeout=MEMORY_WRITE_TIMEOUT,
            )
        except Exception as e:
            # 记忆失败只记 WARN：活动本身已经完成，不能因为记账失败翻脸
            logger.warning(f"[LivingLoop] 记忆写入失败（活动仍算完成）: {e}")

    def _session_id(self, ctx: ActivityContext | None) -> str | None:
        """记忆归属会话：自主活动统一落在幽灵会话里（图谱上的自留地）。"""
        if ctx is not None and getattr(ctx, "event", None) is not None:
            try:
                return ctx.event.unified_msg_origin
            except Exception:
                pass
        try:
            return build_ghost_event().unified_msg_origin
        except Exception:
            return None

    async def _bot_identity(self) -> dict | None:
        """bot 自己的身份（participant_identities 原料，任务书 M3 补丁 IV-B2）。

        身份由 main 注入的动态提取函数从平台配置取得（换人设/换平台自动
        适配，不硬编码）；未注入或提取失败返回 None——记忆照写，只是图谱
        里暂时没有参与者边（与补丁 III 之前的行为一致）。
        """
        if self._bot_identity_getter is None:
            return None
        try:
            identity = await self._bot_identity_getter()
        except Exception as e:
            logger.warning(f"[LivingLoop] bot 身份提取失败（跳过参与者边）: {e}")
            return None
        return identity if isinstance(identity, dict) and identity else None

    async def _persona_id(self) -> str:
        """当前生效 persona 的 id；任何失败都回 "default"（任务书问题 3）。"""
        if self._persona_id_getter is None:
            return "default"
        try:
            pid = await self._persona_id_getter()
        except Exception:
            return "default"
        text = str(pid or "").strip()
        return text or "default"

    # ------------------------------------------------------------------
    # 分享（输出闸门链路，任务书 E）
    # ------------------------------------------------------------------
    def _resolve_target_sessions(self) -> tuple[list[str], str]:
        """分享目标会话（M9-补丁1 A3 三段优先级）：手填 > 派生 > 空。

        - ① 用户显式配置非空 → 用显式值（派生不覆盖）；
        - ② 显式为空 + AstrBot 管理员非空 → 派生全部管理员的私聊会话；
        - ③ 都为空 → 空（不主动发，现状语义）。

        派生为读取时计算（A4）：每次现读全局配置，管理员改动热生效，
        绝不把派生值写回配置文件。返回 (sessions, source)，source 用于
        DEBUG 留底（"手填" / "派生" / "空"）。群聊派生不了（管理员配置
        只有 QQ 号）——需要群聊请手动填 target_sessions。
        """
        explicit = [
            s.strip()
            for s in str(
                _conf_group(self._config_getter(), "output_gate").get(
                    "target_sessions", ""
                )
            ).splitlines()
            if s.strip()
        ]
        if explicit:
            return explicit, "手填"
        getter = self._global_config_getter
        if getter is not None:
            try:
                info = derive_admin_identity(getter())
            except Exception as e:
                logger.debug(f"[LivingLoop] 管理员信息读取失败（按未派生处理）: {e}")
                info = {"admins_id": [], "platform_id": None}
            admins, platform_id = info["admins_id"], info["platform_id"]
            if admins and platform_id:
                sessions = [
                    f"{platform_id}:FriendMessage:{aid}" for aid in admins
                ]
                return sessions, "派生"
        return [], "空"

    async def _maybe_share(self, text: str, now: datetime) -> None:
        # M7-补丁1 A1：空产物防护——低于下限直接静默返回（不打分享日志、
        # 不浪费闸门掷点，更不进改写流水线）。M9-补丁1 A5：本检查保持在
        # sessions 计算之前，顺序不变。
        if len(str(text or "").strip()) < MIN_SHARE_TEXT_LEN:
            return
        sessions, source = self._resolve_target_sessions()
        if not sessions:
            # 默认安静：内容只在 DEBUG 里留底，不打扰任何人
            logger.debug(f"[LivingLoop] 本可发送的内容（未配置 target_sessions）：{text}")
            return
        if source == "派生":
            logger.debug(
                f"[LivingLoop] target_sessions 未手填，自动派生自管理员私聊"
                f"（{len(sessions)} 个会话）"
            )
        allow, reason = await self._gate.should_send_message(now)
        if not allow:
            logger.info(
                f"[LivingLoop] 想说话但被闸门拦下 reason={reason}：{text[:50]}"
            )
            return
        if self._sender is None:
            logger.warning("[LivingLoop] 已配置 target_sessions 但 sender 未注入")
            return

        # M3 补丁 VIII：角色化改写——把工作报告转成聊天口吻。闸门通过后
        # 才改写（拦下就别浪费 token）。M9-补丁4（主人 2026-09-24 拍板）：
        # 改写失败/未产出 → **整条分享静默跳过**（不再降级发送原文）——
        # 原文是工作汇报体，发进聊天框就是 OOC；宁可这次不说也不说错话。
        # 汇报内容不会丢：它已写入活动记忆（memory_content），主人翻记忆
        # 随时能看到，只是不占聊天窗。改写器未注入（None）时保持直发原文
        # （向后兼容 M3 补丁 VIII 的开关语义）。
        if self._share_rewriter is None:
            text_to_send = text  # 未注入：直发原文（向后兼容）
        elif not self._share_rewriter.enabled():
            text_to_send = text  # 用户主动关闭改写开关：直发原文（M3-补丁VIII 开关语义）
        else:
            mood_digest = ""
            if self._mood is not None:
                mood_digest = getattr(self._mood, "digest", lambda: "")()
            try:
                rewritten = await self._share_rewriter.rewrite(text, mood_digest)
            except Exception as e:
                logger.warning(f"[LivingLoop] 分享改写异常，本次分享跳过: {e}")
                return
            if not rewritten:
                logger.warning(
                    f"[LivingLoop] 分享改写未产出（内容已留活动记忆），静默跳过发送: {text[:50]}"
                )
                return
            text_to_send = rewritten

        for session in sessions:
            try:
                sent = await self._sender.send(session, text_to_send)
            except Exception as e:
                logger.warning(f"[LivingLoop] 发送到 {session} 异常: {e}")
                continue
            if sent:
                # 只有真发出去才记账，失败的会话不消耗配额
                await self._gate.note_message_sent(now)

    def _pick_activity(self) -> Activity:
        """随机选活动，避免和上次相同（连着两回干一样的事就不像生活了）。"""
        effective = self._effective_activities()
        pool = [
            a for a in effective if a.name != self._last_activity_name
        ] or effective
        chosen = self._rng.choice(pool)
        self._last_activity_name = chosen.name
        return chosen
