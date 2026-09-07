# M1 验证报告：最小自主闭环

**日期**：2026-09-07　**执行**：zcode　**任务书**：`docs/task_M1_最小自主闭环.md`
**结论速览**：需求 A-F 全部落地 ✅｜测试 **94 通过 / 1 跳过**（M0 基线 46 条无回归，新增 48 条）｜安静纪律落实（默认零发送）｜真实 AstrBot 内的主循环运行验证**未做**（见 §2 原因与主人手动验证步骤）

---

## 1. 各需求落地说明

### 需求 A：记忆后端懒加载（修复 M0 遗留）

- 新组件 `core/lazy_memory.py`（`LazyMemory`），`main.py` 的 `_get_memory()` 委托给它；`initialize()` 不再探测。
- 语义：首次 `get()` 才探测；成功 → 永久缓存；失败 → 返回**可复用的** SimpleBackend 实例，但保留重试机会（下次 get() 重新探测，等 LivingMemory 晚加载就绪）。
- **实现中发现的关键坑（单测抓住）**：最初复用 M0 的 `create_backend(auto)`，但它内部降级后返回 SimpleBackend，懒加载器无法区分"探测成功"与"降级顶替"，会把降级结果当成功永久缓存——**需求 A 要解决的问题会在新代码里复发**。改为直接调 `LivingMemoryBackend.probe()`，两种结局分开处理。对应测试 `test_lazy_falls_back_then_retries_then_caches`。
- 强制 `memory.backend=livingmemory` 且不可用时不再抛异常（M0 行为），降级 Simple 并在 note 里说明——插件可靠性优先。

### 需求 B：LivingGate 状态闸门（`core/living_state.py`）

- 状态持久化：SQLite 键值表（`data/plugin_data/astrbot_plugin_living/living_state.db`，表 `living_state`），五键齐全。
- `should_wake(now)` 短路链：sleeping → daily_limit → cooldown → 概率掷点 → ok，顺序有测试锁定（`test_evaluation_order_sleep_beats_limit`）。
- 记账语义（任务书未明说，取如下解释并写入注释）：`note_activity_started` 做**计数 +1 并盖开始时间戳**（起念即耗配额，活动中途崩了也算花掉一次冲动）；`note_activity_finished` 刷新时间戳——**冷却从"干完活"起算**而非起念。
- 跨日：清零今日计数、保留绝对时间戳（深夜活动后次日凌晨冷却仍有效，有专门测试 `test_cross_day_keeps_cooldown_timestamp`）。
- 配置热读：每次判定重新读 config；脏配置（类型错/空）回默认值，解析失败的时间窗按无窗口处理并 WARNING。
- 新增配置：`decision.activity_probability`（0.8）、`capabilities.cooldown_between_activities_hours`（2.0）。

### 需求 C：主循环（`core/living_loop.py`）

- `start()/stop()` 幂等（重复调用安全，热重载场景有测试）；心跳任务命名 `living-loop`。
- 心跳：先睡 `impulse_check_interval_minutes` 再检查——**插件刚加载不会立刻活蹦乱跳**；间隔钳制 [1, 1440] 分钟。
- 活动周期：起念记账 → 选活动（随机、避免连续两次同活动）→ 执行（`max_run_seconds` 总超时强杀，非正值/脏值回默认 300s，上限 3600s）→ **记忆双路径**（成败都写）→ 收账 → 候选分享。
- 异常兜底：活动抛任何异常只记 ERROR；记忆后端整体不可用时**整轮放弃且不耗配额**（note_started 之前拦截，有测试）。
- **幽灵事件接入点**：每周期构造 `build_ghost_event(session_id=f"living_{时间戳}")` 并放入 `ActivityContext.event`。M1 活动直接调用能力方法、不走 agent 循环，因此 event 暂未被消费；**M2 接入 `tool_loop_agent(event=ctx.event, ...)` 时无需改周期结构**（R0 结论：这是唯一合法的事件形态）。
- 记忆写入单独限时 30s（LivingMemory 引擎可能走嵌入 API，不能拖死活动周期）；写失败只 WARN，活动仍算完成。

### 需求 D：活动池（`core/activities.py`）

五个活动全部实现且可独立测试（fake 能力注入）：

| 活动 | 行为 | 记忆示例 |
|---|---|---|
| 冲浪 surf | 随机主题搜 5 条 | 「9月7日我搜了「宇宙探索」，看到《…》，有点好奇后面讲了什么。」 |
| 读文章 read | 搜索→挑一条 URL→fetch 正文 | 「9月7日我读了《…》（搜「咖啡」找到的），印象最深的是：…」 |
| 小游戏 game | 二分猜数字/掷骰子统计模板→真沙箱试玩（模板已过静态扫描，有测试） | 「9月7日我写了个二分猜数字的小游戏自己玩，结果：猜中了！…」 |
| 看评价 peek | **空走消息闸门**（should_send_message 干跑，结果留 DEBUG）；无产出不写记忆 | 不写（按任务书表格） |
| 整理 reminisce | `memory.search("")` 随机捞旧记忆 | 「9月7日我翻了翻以前的记忆，翻到一条：…」；空库时记「发现还空得很，得多经历点事」 |

- 主题池 7 个固定候选；活动产出全部第一人称、中文、带日期感（`%-m` 在 Windows 不可用，用手拼月份）。
- 配套改动：`SimpleBackend.search("")` 语义改为"随机捞 k 条"（原返回空列表），有测试。
- **记忆双路径有测试**：`test_activity_failure_still_writes_memory_and_loop_survives`（失败记忆含"没成"字样，且循环存活）。

### 需求 E：主动发消息闸门链路

- 默认（`target_sessions` 空）：**零发送**，"本可发送的内容"只进 DEBUG 日志——安静是默认态。
- 配置后：活动摘要 → `should_send_message`（每日上限/最小间隔/静默时段）→ 放行才 `sender.send`；**发送成功才 `note_message_sent`**（发送失败的会话不耗配额）。四个场景均有测试（未配置零发送/放行发送/闸门拦截/发送失败不记账）。

### 需求 F：main.py 接线

- `initialize()`：先停旧循环（热重载双保险）→ 构造 LivingGate + LivingLoop（注入五能力与懒记忆 getter）→ `loop.start()`。
- `terminate()`：停循环 → 关闸门 DB → 关记忆后端与网络客户端；重复调用安全。
- `test_initialize_terminate_idempotent`：连续两次 initialize、terminate 后再 initialize 均通过。

---

## 2. 真实 AstrBot 里的主循环验证情况（诚实声明）

**未做。** 原因：本机 AstrBot 正在运行且归主人使用，重启/热载插件属于改变主人运行环境的操作，超出 zcode 权限；M0 同类验收（WebUI 可见性）也按任务书留给主人。已做的替代验证：

1. **组件级**：主循环/闸门/活动全部用 fake 注入做了行为测试（94 条全绿），含启动/停止/异常/超时路径；
2. **导入级**：`scripts/smoke_import_plugin.py` 以合成包上下文真实 import main.py 通过（相对导入、astrbot.api 依赖、`__init__(context, config)` 签名）；
3. **版本面**：M0 骨架已在 VM 的 AstrBot v4.28.0-beta.1 加载成功（总纲记录）；M1 新增代码只用了 v4.27.5 已核实的 API 面（aiosqlite/asyncio/现有能力组件），无新 AstrBot API 依赖。

**主人手动验证步骤**（建议按序）：

1. 部署：将 `D:\sandbox\astrbot-living` 内容复制（或 junction）为 `AstrBot根\data\plugins\astrbot_plugin_living\`（`.git`、`tests`、`reference` 不参与运行，可不拷）；
2. 重启 AstrBot（或 WebUI 里重载该插件），确认日志出现：
   - `[astrbot_plugin_living] M1 加载完成（闭环组件就绪）`
   - `[LivingLoop] 主循环已启动`
3. **快速观察一次活动**（可选，约 2 分钟）：WebUI 改配置 `decision.impulse_check_interval_minutes=1`、`decision.activity_probability=1.0`、`capabilities.cooldown_between_activities_hours=0`（改配置即生效，无需重启），1-2 分钟内日志应出现 `[LivingLoop] 活动开始 name=…` / `活动结束`（INFO）；随后查 `data/plugin_data/astrbot_plugin_living/` 下 `living_state.db` 与 `living_memory_simple.db` 有数据；
4. **发送验证**（可选，会真发消息）：`output_gate.target_sessions` 填一个 unified_msg_origin（如 `aiocqhttp:GroupMessage:群号`），等活动周期结束后应收到活动摘要；连发会被 30 分钟间隔拦下（DEBUG 可见"想说说话但被闸门拦下"）；
5. 收尾：把三处配置改回默认值。**注意**：`misc.log_level` 需为 DEBUG 才能看到 `[LivingGate] 判定` 日志；观察期建议保持默认 INFO，避免刷屏。

**已知风险**：热生效依赖"WebUI 改配置时 AstrBot 原地修改同一 config 对象"（v4.27.x 行为如此）；若某版本改为整体替换对象，则改配置需重载一次插件——不丢数据（状态在 SQLite）。

---

## 3. 测试结果

```
94 passed, 1 skipped (18s)
```

- M0 基线 46 条 + 1 跳过（真实注册表探测，需 AstrBot 进程，预期跳过）——**无回归**；
- M1 新增 48 条：闸门 17（含参数化边界）、活动 11、主循环 12、懒加载与接线 5、Schema/记忆组件 3；
- 新增红线自检：全项目仍无 `import astrbot_plugin_livingmemory`（grep 通过）；`_conf_schema.json` 合法性有测试锁定（含新配置项默认值）。

## 4. 遇到的坑

1. **`create_backend` 掩蔽探测失败**（§1-A，最重要的一个）：auto 模式内部降级返回 SimpleBackend，懒加载器会把降级误判为成功——需求 A 的原始问题差点在新实现里复活。教训：**"降级成功"与"探测成功"必须分层返回**。
2. **测试名撞车**：`from main import …` 解析到 AstrBot 本体的 `main.py`（conftest 把 AstrBot 根放进了 sys.path）。用合成包上下文加载插件 main.py 解决，并把顶层 `core` 包别名进合成包，避免双份模块树导致 isinstance 失效。
3. **aiosqlite 连接跨事件循环会挂死**：单测曾把同一 gate 的连接用两次 `asyncio.run` 访问，第二个循环里 future 永不完成。写测试时保证"一个连接一个 asyncio.run"。
4. **Windows strftime 不支持 `%-m`**：记忆的日期感改用手拼 `f"{now.month}月{now.day}日"`。
5. **`asyncio.wait_for` 超时下限**：最初给 max_run_seconds 钳了 10s 下限，导致超时测试要真等 10s；改为"非正值回默认、上限 3600"。

## 5. 歧义与取舍（供凛核验时重点看）

1. **started/finished 记账语义**：计数在 started（起念即耗配额）、冷却锚点在 finished（干完活才开算冷却）。另一种解释是 finished 才计数——若凛认为"活动失败不该算次数"，改动点在 `LivingGate.note_activity_started/finished`。
2. **peek 活动的"note_message_sent 逻辑空走"**：实现为干跑 `should_send_message`、**不**调 note_message_sent（没有真发就不该耗配额）。
3. **心跳先睡后查**：插件加载后第一个判定在 N 分钟后而不是立刻——避免加载/重载瞬间就活动。若希望"加载后先查一次"，改动点在 `LivingLoop._run`。
4. **整理活动的空库兜底**：LivingMemory 的 `search_memories("")` 按其源码会返回空列表，此时活动产出"空白"记忆而非报错。

## 6. 配置项变更清单

| 组 | 键 | 变更 | 默认 |
|---|---|---|---|
| decision | activity_probability | **新增** | 0.8 |
| capabilities | cooldown_between_activities_hours | **新增** | 2.0 |
| decision | impulse_check_interval_minutes / daily_impulse_limit / max_run_seconds | M0 占位 → **M1 生效** | 45 / 3 / 300 |
| output_gate | target_sessions / daily_message_limit / message_min_interval_minutes / quiet_hours | M0 占位 → **M1 生效** | 空 / 10 / 30 / 空 |
| sleep | sleep_window | **时间窗判定生效**（疲惫度等仍 M3） | 00:30-08:00 |
| memory | backend | M1 生效（经 LazyMemory 懒探测） | auto |

未动 M2/M3 占位项（token 预算、工具轮数、疲惫/吵醒参数）。

## 7. 给 M2 的建议

1. **反"营销号"的根基已经埋好**：每个活动都写第一人称记忆（竞品 proactive_chat 恰因无记忆写入被批"没有内核"）。M2 决策 prompt 注入近期记忆时，建议用 `memory.search("", k=…)` 或按时间取最近的易碎记忆做"今天想做什么"的引子。
2. **agent 循环接入**：`ActivityContext.event` 已是现成的幽灵事件，M2 把"活动"从"直接调能力"升级为 `tool_loop_agent(event=ctx.event, tools=…)` 时，闸门/记账/记忆双路径结构无需动——只需换掉 `Activity.run` 内部实现。
3. **token 闸门**：`decision.single_run_token_budget` 已占位；接入 agent 循环后从 `LLMResponse.usage` 累计并在超预算时中断，建议作为 M2 第一优先（烧钱风险先于趣味性）。
4. **决策三档**：M1 的 `_pick_activity`（random.choice + 避免重复）就是 rules 档的雏形，M2 的 llm 档可以直接替换这一个方法，`LivingLoop` 其他部分不用动。
5. **心境层挂点**：`ActivityOutcome.importance` 已随活动语义区分（整理 0.4/失败 0.2），M2 心境对记忆重要度的调节可以直接接管这个字段。
