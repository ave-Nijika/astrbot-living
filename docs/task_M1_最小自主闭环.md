# 任务书 M1：最小自主闭环

**项目**：astrbot-living（astrbot_plugin_living）——让 AstrBot 在无消息时拥有自己的生活
**前情**：M0 已完成并核验（五能力组件/插件骨架/沙箱隔离/幽灵事件，见 `docs/m0_report.md`）。本任务书在 M0 已交付代码上继续扩展。
**全局依据**：`docs/项目总纲.md`（设计宪法，最高优先级）。本任务书只在总纲框架内细化 M1 范围。
**工作位置**：`D:\sandbox\astrbot-living`（代码在 `core/`、`main.py`，测试在 `tests/`）。
**交付**：代码 + pytest 全绿 + `docs/m1_report.md`。
**执行者**：zcode。交付后由凛核验。

---

## ⚠️ 关键约束（违反即打回）

1. **不改 AstrBot 本体、不改 LivingMemory 插件任何文件**（AGPL 红线，M0 已守）。
2. **不 push 远程、不打 `.env`**。新依赖一律先列清单说明理由（pytest 已在 venv 中，允许使用）。
3. **本任务书只做 M1 范围**：主循环 + 状态闸门 + 最小决策 + 记忆写入 + 弱开关。**不做** M2 的心境状态机、M3 的休眠/吵醒/梦、不做完整对话式决策 prompt。
4. 所有"次数/间隔/时长"类配置项必须进 `_conf_schema.json`（M0 已有骨架，M1 补全缺项），运行时从配置读取，改配置不重启生效。
5. 代码注释中文，且解释"为什么"而非"是什么"。
6. 被定义"安静"的场景必须真的安静：**任何活动产出（包括日志刷屏）都不能打扰主人**。日志用 DEBUG 级别，仅 `[LivingLoop]` 级关键事件用 INFO。

## 必读现状（M0 交付物）

- `main.py`：LivingPlugin(Star)，初始化五能力（searcher/fetcher/sandbox/sender + 记忆后端懒初始化），`initialize()` 目前会在插件加载时探测记忆后端——**这是要改的地方**（见需求 A）。
- `core/ghost_event.py`：`build_ghost_event(session_id)`——R0 验证出的自主活动事件（工具循环必需）。
- `core/memory_backend.py`：`create_backend(context, mode, simple_db_path)`，MemoryBackend 接口（add/search/close）。
- `core/`：search.py（博查）、fetcher.py（抓取）、sandbox.py（沙箱）、sender.py（发消息）。
- `tests/`：46 测试全绿（M0 基线，不可回归）。
- `_conf_schema.json`：decision/output_gate/sleep/capabilities/memory/model/misc 七组骨架，M1 补全。

---

## 需求 A：记忆后端懒加载探测（修复 M0 遗留已知问题）

**问题**：插件加载时 LivingMemory 尚未注册进 AstrBot 插件表，`create_backend()` 探测失败降级 SimpleBackend——即使后来 LivingMemory 已就绪也不重试。主人已在另一台机器上实测复现。

**改动**：
- `main.py` 的 `initialize()` 里**不要**主动探测后端；改为"首次使用时才探测"：
  - 加一个 `async def _get_memory(self) -> MemoryBackend`（或类似懒 getter）：首次调用时 `create_backend(...)`，成功则缓存；失败则降级 SimpleBackend 并记录原因（允许 M1 内保留成功后缓存、不重复探测）。
  - 记录探测结果到 `self.memory_note`，用 `logger.info` 输出一次。
- 注意线程/事件循环：探测在 asyncio 上下文内进行即可（主循环本来就是异步）。

**验收**：
- 单元测试：mock 一个"首次探测失败、第二次成功"的 context/注册表，验证懒 getter 能重试且成功后缓存。
- 集成（可选，有运行环境时）：真实 AstrBot 里 LivingMemory 晚加载，首次使用记忆时仍能拿到引擎。

## 需求 B：LivingGate（状态闸门，决定"此刻该不该动"）

新建 `core/living_state.py`，核心类 `LivingGate`：

- **状态持久化**：SQLite（`data/plugin_data/astrbot_plugin_living/living_state.db`，沿用 M0 的 `_simple_db_path()` 风格），表 `living_state` 存键值：`today_activity_count`（今日活动次数）、`last_activity_at`（上次活动时间戳）、`today_message_count`（今日主动消息数）、`last_message_at`（上次主动消息时间戳）、`date`（当日标识，跨日自动清零）。
- **判定方法** `should_wake(now)`：返回 `(allow: bool, reason: str)`，按顺序短路：
  1. 休眠窗口内（`sleep.sleep_window`，默认 00:30-08:00）→ `(False, "sleeping")`
  2. 今日活动次数 ≥ `decision.daily_impulse_limit`（默认 3，0=不限制）→ `(False, "daily_limit")`
  3. 距 `last_activity_at` < `capabilities.cooldown_between_activities_hours`（新增配置，默认 2 小时）→ `(False, "cooldown")`
  4. 随机概率：`random.random() < decision.activity_probability`（新增，默认 0.8）→ 通过；否则 `(False, "rolled_off")`
  5. 通过 → `(True, "ok")`
- **记账**：`note_activity_started()` / `note_activity_finished()` 更新 today_activity_count/last_activity_at；发消息走 `note_message_sent()` 更新 today_message_count/last_message_at。
- **配置读取**：从插件 config（`_conf_schema`）热读，每次 `should_wake` 重新读（改配置即生效）。
- **日志纪律**：每次判定输出一条 DEBUG：`[LivingGate] 判定 reason=XXX allow=YYY（今日活动 2/3，上次活动 12:31，冷却- 距上次 1.8h）`。

**新增配置项**（进 `_conf_schema.json` 的 decision/capabilities 组）：
- `decision.activity_probability`（float，默认 0.8）
- `capabilities.cooldown_between_activities_hours`（float，默认 2.0）

**验收（单元测试 ≥5 条）**：
- 休眠窗口内 → False 且 reason=sleeping（用假时间注入）
- 活动数达上限 → False reason=daily_limit
- 冷却未过 → False reason=cooldown
- 概率命中 → True reason=ok；概率落空 → False reason=rolled_off（mock random）
- 跨日清零：date 变化后 today_activity_count 归零

## 需求 C：主循环 + 最小决策（能"自己决定干点什么"）

新建 `core/living_loop.py`，类 `LivingLoop`：

- **启动/停止**：`await start()` 创建 `asyncio` 后台任务；`await stop()` 取消任务并清理。挂到 `main.py` 的 `initialize()`（start）与 `terminate()`（stop）。**保证重复 start/stop 幂等**。
- **心跳**：后台循环每 `decision.impulse_check_interval_minutes`（默认 45 分钟）唤醒一次，调用 `LiveGate.should_wake()`；False → 睡到下一轮（换算 seconds）、不应有任何活动；True → 进入"一次活动周期"。
- **活动周期**（M1 最小版，不引入完整心境）：
  1. `note_activity_started()`
  2. 用伪随机从活动池选一个活动（活动池见需求 D）
  3. 执行活动（调用对应能力）
  4. 无论活动结果成败，写一条记忆（见需求 D-记忆）
  5. `note_activity_finished()`
- **异常兜底**：整个活动周期包 try/except，任何异常只记 ERROR 日志，不中断主循环（活动失败≠进程崩溃）。单次活动周期设总超时（`decision.max_run_seconds`，默认 300），超时强杀。
- **幽灵事件**：活动周期内若需调 `tool_loop_agent` 或任何需要 event 的接口，使用 `build_ghost_event(session_id=f"living_{活动id}")`（M0 结论，工具循环必需）。M1 阶段活动可以不真正调 agent 循环（活动直接调用能力方法即可），但必须**保留 GhostEvent 的接入点**并在报告里说明 M2 才接入完整 agent 循环。

## 需求 D：活动池（五种能力的"闲时用法"）+ 记忆写入

M1 的活动池写死在代码里（M1 不做自由涌现——那是 M2/M3 用决策 prompt 实现），但活动**必须是"有生活气息"的，不是死板轮询**：

| 活动 | 调用 | 产出 | 记忆 |
|---|---|---|---|
| 冲浪（search） | `searcher.search(主题词)` | 结果 1-N 条 | 「今天逛了逛，看了 XX，觉得 YY」 |
| 读文章（fetch） | 从 search 结果取一条 URL `fetcher.fetch(url)` | 标题+正文摘要 | 「读了《XX》，记了要点」 |
| 玩小游戏（sandbox） | 生成一段秒级小游戏 Python（如猜数字/掷骰子）`sandbox.run(code)` | stdout 结果 | 「写了个小游戏试玩，结果/心得」 |
| 看评价（sender 弱触发） | 暂不主动发（M2 再开），只 `note_message_sent` 逻辑空走验证闸门 | 无 | 不写 |
| 整理（memory） | `backend.search("")` 随机捞一段旧记忆 | 旧记忆 | 「翻到以前的回忆：……」 |

- 活动选择的随机性：`random.choice`（M1 弱加权即可，权重视为 1）。可加"避免连续两次同活动"的最小逻辑。
- **记忆写入（鲁棒）**：**每个活动结束后必须写记忆**（调 `_get_memory().add(...)`），失败不抛出（记忆失败只记 WARN，活动仍算完成）。记忆 content 用一句话、中文、第一人称、带日期感——示例：「今天看了《Next.js 16 新特性》，注意到它有 xxx」。
- 主题词来源：M1 用固定候选池（如 ["人工智能", "独立游戏开发", "效率工具", "宇宙探索", "咖啡 ]" 等 5-8 个），随机取——不接 LLM 生成主题（那是 M2）。

**验收**：
- 每个活动可独立测试（fake 能力注入或 mock）。
- 记忆写入在活动成功/失败两种路径下都发生（失败路径测试）。

## 需求 E：主动发消息（受闸门约束，M1 最小版）

- `sender.send(session, text)` 已存在。M1 只做一件事：**验证"闸门放了才发"的链路**，默认不主动发（配置 `output_gate.target_sessions` 留空 = 不发给任何人，仅日志记录"本可发送的内容"）。
- 若用户配置了 `target_sessions`，则按以下规则：
  - `LivingGate.should_wake()` 已通过（活动周期内）
  - 距 `last_message_at` ≥ `output_gate.message_min_interval_minutes`（默认 30）
  - 今日消息数 < `output_gate.daily_message_limit`（默认 10）
  - 静默时段（`output_gate.quiet_hours`）内不发
- 内容：活动周期结束后，把活动摘要作为"想说的话"（一句话），发到配置的会话。

**验收**：mock sender，验证闸门链路（未配置 target 时零发送、配置后按规则放行/拦截）。

## 需求 F：main.py 接线

- `initialize()`：构造 `LivingLoop`（注入 context、config、gate、_get_memory、各能力）→ `await loop.start()`
- `terminate()`：`await loop.stop()`
- 保证重复 `initialize/terminate` 安全（AstrBot 可能热重载插件）。

## 测试与交付

- 保持 M0 的 46 条测试全绿（基线不回归）。
- 新增测试覆盖需求 A（懒加载重试）、B（gate ≥5 条）、C（主循环启动/停止幂等、异常兜底）、D（各活动可测、记忆双路径）、E（发消息闸门）。预计新增 15-20 条。
- `docs/m1_report.md`：结构沿用 M0 报告风格——结论速览 / 各需求落地说明 / 测试结果 / 遇到的坑 / 配置项变更清单 / 给 M2 的建议。**报告必须写清楚**：主循环在真实 AstrBot 里能否跑起来的验证情况（若无法启动 AstrBot 测试，说明原因并给出主人可执行的手动验证步骤）。

## 红线（打回项）

- ❌ 不在 M1 引入完整心境状态机（心情/兴趣维度的持久演化）——在 M2。
- ❌ 不做休眠/吵醒/梦（疲惫度、睡眠债、起床气）——在 M3。
- ❌ 不写"每 N 分钟必发消息"式的无脑推送——所有输出必须过闸门。
- ❌ 不推远程、不打 `.env`、不加非必要依赖。
- ❌ 不改 AstrBot 本体 / LivingMemory。

## 汇报节点

1. 开工前若有歧义先问，别猜。
2. 交付后附 `m1_report.md` + 测试结果 + git 提交历史，等凛核验。核验通过前不 push。