# 任务书 M3：休眠系统 + 热生效双事件 + agent 循环接入

**项目**：astrbot-living（astrbot_plugin_living）——让 AstrBot 在无消息时拥有自己的生活
**前情**：M0/M1/M2 已交付并核验（幽灵事件、五能力、闸门、主循环、心境、决策三档、persona 接入；真实 AstrBot v4.28 VM 实测通过，见 `docs/archive/m1_report.md`、`docs/archive/m2_report.md`）。本任务书在 M2 已交付代码上继续扩展。
**全局依据**：`docs/项目总纲.md`（设计宪法，最高优先级）。本任务书只在总纲框架内细化 M3 范围。
**工作位置**：`D:\sandbox\astrbot-living`（代码在 `core/`、`main.py`，测试在 `tests/`）。
**交付**：代码 + pytest 全绿 + `docs/m3_report.md`。**允许分批交付**（见"分批交付"节）。
**执行者**：zcode。交付后由凛核验。

---

## ⚠️ 关键约束（违反即打回）

1. **不改 AstrBot 本体、不改 LivingMemory 插件任何文件**（AGPL 红线，M0-M2 已守）。
2. **不 push 远程、不打 `.env`**。新依赖一律先列清单说明理由。
3. **token 硬闸先于 agent 循环上线**——agent 循环（需求 C）必须在 token 闸门保护下工作，两者属于同一交付。没有 token 闸的 agent 循环不许出现在交付里。
4. 沙箱白名单**不放宽**（M0 定稿：random/math/time/datetime/json/re/itertools/collections）。
5. 所有"次数/间隔/时长/概率"类配置项必须进 `_conf_schema.json`，运行时热读（沿用 M1/M2 惯例）。
6. 代码注释中文，且解释"为什么"而非"是什么"。
7. 默认配置必须保守：休眠期静默回复默认开启、吵醒阈值默认 3 条、token 预算默认值按 M2 报告预估（万级/次）收紧。

## 必读现状（M2 交付物）

- `core/living_loop.py`：`LivingLoop._run` 是 `sleep(interval) → heartbeat_once()` 单线程循环。**睡眠不可打断**——这是本次需求 A 要解决的。
- `core/living_state.py`：`LivingGate`（should_wake 判定链：sleep_window→daily_limit→cooldown→probability；should_send_message 输出闸门；记账 note_*）。
- `core/mood.py`：`MoodState`（valence/arousal/energy/interests 持久化，digest() 人话摘要）。**arousal 已持久化未接动力学**——M3 挂点。
- `core/activities.py`：`ActivityContext.event` 已是幽灵事件（M0-R0 结论），M2 预留 tool_loop_agent 接入口。
- `core/lazy_memory.py`：`LazyMemory`（LivingMemory 懒探测，VM 实测晚加载场景修复成功）。
- `main.py`：`_decision_llm_call()`（provider 回退链：model.provider_id → get_current_chat_provider_id → None）、`_persona_prompt()`（persona_manager.get_default_persona_v3(umo)）。
- `scripts/verify_decider_llm.py`：M2 真实环境验证脚本（hybrid/llm 档真跑通过）。
- `reference/proactive_chat/core/message_events.py`：消息事件监听参考实现。
- AstrBot `astrbot/core/agent/hooks.py`：`BaseAgentRunHooks`——token 闸的调研挂点（见需求 C）。

---

## 需求 A：热生效双事件（主人 2026-09-08 03:07 定稿的改动）

**现状问题**：主循环 `_run` 是 `sleep(N) → 判定` 的单线程循环。配置改动（如间隔 45→10 分钟）只能等当前 sleep 自然结束才生效；也没有任何手段让主循环"现在就判一次"。

**目标语义（主人定稿）**：
- **配置改动 = 只重置定时器，不触发判定**。改间隔 90→10 分钟：当前等待立即作废，下次判定 = 现在 + 10 分钟。频繁改配置 = 定时器反复重置，零判定零活动零 LLM 调用。
- **手动唤醒命令 = 触发一次判定**（过闸门），与配置改动完全分离。

### A1 双事件打断睡眠（core/living_loop.py 改造）

`_run` 的 sleep 改为可被两个事件打断的等待：

```python
self._config_event = asyncio.Event()   # 配置变更 → 重置定时器
self._wake_event = asyncio.Event()     # 手动/吵醒唤醒 → 触发判定
# _run 循环内:
done, pending = await asyncio.wait(
    [task(config_event.wait), task(wake_event.wait)],
    timeout=interval_min * 60, return_when=asyncio.FIRST_COMPLETED,
)
# 清理 pending、clear 已触发事件
# 若 config_event 触发 → 重新读 interval，回到循环顶（不判定）
# 若 wake_event 触发 → 走一次 heartbeat_once(force=True)（见 A2）
# 若超时 → 正常心跳 heartbeat_once()
```

### A2 强制唤醒语义（手动命令）

`heartbeat_once(force=True)`：豁免**概率掷点**（activity_probability），但**仍受**休眠窗口/每日上限/冷却约束——手动唤醒在睡眠窗内触发需求 B 的吵醒流程（见 B3），其余时段直接进入活动周期。

### A3 手动唤醒命令（main.py 注册）

- 命令 `/living_wake`（AstrBot `@filter.command` 机制，参考 LivingMemory 的命令注册）：`set wake_event`，回复主人「收到，判定中…」+ 判定结果一句话（唤醒成功进入活动 / 被闸门拦下原因）。
- 命令处理器直接在事件上下文里 await `heartbeat_once(force=True)` 并把结果反馈给主人——不走 wake_event 也行（二选一，zcode 按实现复杂度定，报告里说明）。

### A4 配置变更检测

- 优先：调研 AstrBot 是否提供插件配置变更钩子（WebUI 保存配置时的回调）。有则挂钩子 set config_event。
- 兜底：轻量哈希轮询——后台协程每 5 秒对 config 序列化哈希一次，变更即 set config_event（纯内存比对，零 LLM 零网络）。
- 实现说明：**AstrBotConfig 是原地修改对象**（M1 报告已知风险），哈希比对时注意深拷贝快照再比对，避免引用相同导致永远相等。

### A5 验收

- 测试：改间隔（45→10）→ config_event 触发 → sleep 立即结束且**未**执行 heartbeat_once（有断言）。
- 测试：wake_event → heartbeat_once(force=True) 执行且跳过概率掷点（rng mock 断言未被调用或被忽略）。
- 测试：连续改配置 5 次 → 5 次定时器重置、0 次活动、0 次 LLM 调用。

---

## 需求 B：休眠系统（"像人一样真正睡着"）

### B1 疲惫度与睡眠债（mood.py 扩展）

- `fatigue`（0~100）进 MoodState 持久化：每次活动周期结束 +`fatigue_rate_per_hour` 按活动实际耗时折算（已有配置占位 fatigue_rate_per_hour）；每日自然恢复。
- `sleep_debt`（0~100）睡眠债：睡眠窗内被吵醒/手动唤醒打断 → 按打断时距自然醒点的时间比例累积。次日 energy 上限 = 1 - sleep_debt/200（睡眠债高时精力上限压低）。消退：每完整睡眠一夜 -sleep_debt_decay_per_day（已有配置占位）。
- arousal 接动力学（M2 挂点兑现）：活动成功 arousal +0.05，睡眠期自然回落。

### B2 入睡判定与真挂起

- `should_wake` 的 sleeping 判定已存在（M1，VM 实测 02:26 生效）。M3 增强：睡眠窗**进入时**主动把主循环置入"深睡"状态——心跳照常跑但闸门必拒（现状已是），且**睡眠期收到的消息事件被拦截不回复**（见 B3）。
- **睡前回顾**：进入睡眠窗的第一个心跳（或睡眠窗开始事件）写一条"今日回顾"记忆（用当日活动记忆聚合，一句话，第一人称）。

### B3 吵醒机制（A+B 定稿）

- **消息计数器**：插件监听所有消息事件（AstrBot `@filter.event_message_type(EventMessageType.ALL)` 或等效；参考 `reference/proactive_chat/core/message_events.py` 与 LivingMemory `core/passive_group_capture.py` 的写法），维护 10 分钟滑动窗内的消息时间戳队列（`wake_window_minutes` 可配）。
- **吵醒判定**：窗内消息数 ≥ `sleep.wake_n_messages`（默认 3）→ set wake_event（force=True）→ 主循环醒来进入活动周期。
- **起床气**：若唤醒发生在睡眠窗内且距自然醒点还早 → 按 `sleep.grouchiness_percent`（默认 20）概率给心境 valence 额外 -0.15、energy -0.1，且当次活动记忆追加「被吵醒了，有点烦」语气词（由决策 prompt 的心境摘要自然带出，不硬编码文案）。
- **睡眠债**：被吵醒时按剩余睡眠时长比例累加 sleep_debt。
- **只计数主人的消息还是所有消息**：默认所有消息都计数（群聊里别人聊天也算吵），配置 `sleep.wake_source` 可选 "all"/"owner_only"（owner 用配置的 owner_id 判定，新增配置）。

### B4 睡眠期静默回复（主人 2026-09-07 定稿"真正陷入休息"）

- 新配置 `sleep.sleep_mute_replies`（bool，默认 true）：睡眠窗内收到消息 → 除吵醒计数外，**拦截事件传播**（AstrMessageEvent.stop_event() 或等效 API，zcode 对源码确认拦截点）——AstrBot 本体与所有插件都不回复。
- **例外**：吵醒判定达阈值的那批消息不拦截（第 3 条消息正常走回复管线——被吵醒了就该回应）；`/living_wake` 等本插件命令不拦截。
- 拦截实现放 **handler 优先级最前**（AstrBot 的 filter 优先级机制，zcode 调研；LivingMemory 的 passive capture 是参考）。
- ⚠️ 拦截是有副作用的操作（主人深夜急事找 astrbot 办事会被静音）——这是主人明确要的"真正休息"，但 `sleep_mute_replies` 必须可关（false = 休眠期照常回复，只是插件自己不活动）。

### B5 梦（醒来后的低频彩蛋）

- 唤醒后（自然醒或吵醒均可）按 `sleep.dream_probability`（新增，默认 0.3）概率生成一条"梦"：
  - 素材：取睡眠窗前最后 N 条活动记忆 + 随机一条旧记忆，调决策 LLM（用 `_decision_llm_call` 同款通道）生成一段含糊的第一人称梦话（80 字内，语气朦胧）。
  - 产出：写记忆（importance 0.2）+ 走分享闸门（默认不发，日志留底）。
  - 失败静默（梦丢了就丢了，不影响任何主流程）。

### B6 验收

- 测试：睡前回顾写入；吵醒 3 条打断睡眠 + 起床气概率（mock rng）+ 睡眠债累积；sleep_mute_replies=true 时事件被拦、命令不拦；梦概率与失败静默；睡眠债次日压低 energy 上限。

---

## 需求 C：agent 循环接入（"决策的想象力接上执行力"）+ token 硬闸

**背景**（M2 报告 §6）：真实验证里 LLM 给出的 style 是「驾驶小飞船在星图间巡航探索」，但 M1 硬编码小游戏模板只有两个，style 用不上只能落回随机——**决策的想象力超过执行力上限**。M3 解锁。

### C1 活动执行升级：tool_loop_agent 模式

- `core/activities.py` 的活动增加可选 agent 执行路径：`ActivityContext.event`（幽灵事件）+ `tool_loop_agent(event=ctx.event, chat_provider_id=…, tools=ToolSet(生活工具集), system_prompt=persona+life_extra+心境+意图, max_steps=decision.max_tool_rounds)`。
- **生活工具集**（把五能力包装成 FunctionTool，模式参考 AstrBot 内置 `web_search_tools.py` 与 LivingMemory `tools/memory_search_tool.py`）：
  - `web_search(query)` → C1 搜索
  - `fetch_page(url)` → C2 抓取（返回正文前 N 字）
  - `run_python(code)` → C3 沙箱（LLM 现场写代码玩——填字游戏/文字冒险都行）
  - `remember(text, importance)` → 记忆写入
- system_prompt 组成：persona + life_extra + 心境摘要 + 本次意图（decider 产出的 params）。
- **M2 的 hybrid/llm 决策产物（activity + params）直接作为 agent 的任务描述**——决策层决定"玩什么"，agent 层决定"怎么玩"。
- 每个活动声明 `use_agent: bool`（默认 False 走 M1 脚本模式，配置 `decision.agent_activities` 可覆盖哪些活动走 agent 模式）。M3 默认 surf/read/game 三个开 agent 模式（`decision.agent_activities` 默认值 ["surf","read","game"]），peek/reminisce 保持脚本模式。
- 失败回退：agent 循环异常/超时 → 回退该活动的脚本模式（M1 实现，保底不空转）。

### C2 token 硬闸（先于 C1 上线）

- `BaseAgentRunHooks` 调研：`astrbot/core/agent/hooks.py` 有 agent 运行钩子（on_step 之类）——在钩子里累计 `LLMResponse.usage` 的 token 数，超 `decision.single_run_token_budget`（已有占位，默认 20000）→ 中断循环（具体中断手段 zcode 对源码确认：hooks 抛异常 / agent_context 置标志，任选可靠者；报告里说明）。
- 循环自然结束（max_steps 触顶）也统计实际消耗，写进活动记忆（"这次玩了很久"）。
- 超预算中断的活动：记忆照写（失败路径），mood 记录，不算 crash。

### C3 验收

- 测试：mock provider 多轮工具调用累计 token、超预算中断、自然结束统计；agent 模式活动（mock）产出 summary/memory；失败回退脚本模式。
- **真实环境**（主人配合）：VM 上跑一次 agent 模式活动，观察 LLM 现场用工具（搜索/写代码）的日志。脚本可参照 `scripts/verify_decider_llm.py` 的方式扩展。

---

## 需求 D：元数据与文案收尾（M1/M2 遗留）

- `main.py` 加载日志文案「M1 加载完成」→「M3 加载完成（心境+休眠+agent 循环）」（zcode 两轮均漏更新文案，本次修掉并进核验清单）。
- 确认 `metadata.yaml` 为 0.3.0（凛已同步 0.2.0，本次 M3 交付升 0.3.0）+ 描述含新能力关键词（休眠/梦/agent）。

---

## 分批交付（允许，按序）

| 批次 | 内容 | 理由 |
|---|---|---|
| 第一批 | 需求 A（双事件+手动命令）+ 需求 D | 小而独立，先解决"热生效"痛点 |
| 第二批 | 需求 B（休眠系统）+ 需求 C 的 C2 token 闸 | 休眠与 token 闸联动（吵醒打断睡眠、token 闸保 agent） |
| 第三批 | 需求 C1/C3（agent 循环接入） | 在 token 闸保护下上线 |

每批单独 commit + 报告章节，凛按批核验。若一批内资源吃紧，宁可批次延后也不砍测试。

## 测试与交付

- M2 基线 143 条全绿不回归。
- 新增测试：双事件（A5）、休眠（B6）、agent 循环与 token 闸（C3）、命令注册、消息计数器滑动窗。预计新增 30-40 条。
- `docs/m3_report.md`：结论速览 / 各需求落地说明 / **真实 AstrBot 验证情况（诚实声明 + 主人手动步骤）** / 遇到的坑 / 配置变更清单 / 给 M4 的建议。

## 红线（打回项）

- ❌ 无 token 闸的 agent 循环交付（红线 3）。
- ❌ 沙箱白名单放宽。
- ❌ 休眠期拦截范围失控（只拦 LLM 回复管线，不拦本插件命令；`sleep_mute_replies=false` 时必须完全不拦）。
- ❌ 改 AstrBot 本体 / LivingMemory。
- ❌ schedule 式定时推送（总纲 §2 D4 永久禁令）。
- ❌ 引入浏览器自动化/重依赖。

## 汇报节点

1. 开工前若有歧义先问（尤其 C2 的 hooks 中断手段，调研结论先报再写）。
2. 每批交付附报告章节 + 测试结果，等凛按批核验。
3. 全部完成后交 `m3_report.md`，等凛核验 + 主人 VM 实测。