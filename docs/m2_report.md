# M2 验证报告：心境状态机 + 决策升级

**日期**：2026-09-08　**执行**：zcode　**任务书**：`docs/task_M2_心境与决策升级.md`
**结论速览**：需求 A-E 全部落地 ✅｜测试 **143 通过 / 1 跳过**（M1 基线 94 条无回归，新增 49 条）｜**决策 LLM 已在真实 AstrBot 组件上端到端验证通过**（真实 provider/persona/博查，证据 `scripts/m2_llm_verify.json`）｜无新依赖｜默认配置零打扰不破坏

---

## 1. 各需求落地说明

### 需求 A：心境状态机（`core/mood.py`）

- `MoodState`：valence(-1~1，默认 0.2) / arousal(0~1，默认 0.5) / energy(0~1，默认 0.8) / interests(JSON)，独立 `mood.db` 键值存储（与 living_state.db 同目录分文件——"该不该动"与"什么状态"生命周期不同）。
- `record_activity` 按任务书规则：成功 valence+0.05/energy-0.1，失败 valence-0.08/energy-0.05；surf/read/game 成功给主题兴趣 +0.15；reminisce 成功给"记忆"兴趣 +0.1（回味本身也是兴趣）。
- 跨日兴趣衰减 ×0.9 锚在 `load()`（注入时钟可测）；所有数值边界钳制；库中脏数据（非数字/坏 JSON）回默认值不崩溃。
- `digest()` 产出人话摘要给决策 LLM："心情平静（valence=0.20）；精力一般；最近对这些有兴趣：宇宙探索(0.80)、咖啡(0.60)"。
- arousal M2 只持久化与暴露、不发明动力学——它是 M3 与休眠系统联动的天然挂点。

### 需求 B：决策三档（`core/decider.py`）

- **rules**：加权随机 + 避免连续重复。零 LLM 成本（有测试断言 llm_call 零调用）。
- **hybrid**（默认）：rules 选活动 → LLM 出 `{"topic": …}` / `{"style": …}` 参数注入 `ActivityContext.params`；LLM 失败/解析失败 → 参数为空，活动内部回退随机主题，**不阻塞**。
- **llm**：一次调用，输入 = persona + life_extra + 心境摘要 + 近期记忆（memory.search("") 取 5 条）+ 活动清单（name: description）；输出 `{"activity": …, "params": {…}}`；选了不存在的活动/解析失败/LLM 挂 → 回退 rules（note 记录原因）。
- **未接入 tool_loop_agent**（红线遵守）：活动执行仍走 M1 能力调用。
- JSON 解析宽容（剥 ```json 围栏、容忍客套话），参数截断 30 字——主题词不是文章。
- LLM 调用经注入的 `llm_call` 异步函数，decider 本身不依赖 AstrBot provider——测试全 mock，生产由 main.py 用 `context.llm_generate` 实现。

### 需求 C：人设接入

- **C1**：`main._persona_prompt()` 走 `persona_manager.get_default_persona_v3(umo)`，取 `Personality["prompt"]`（TypedDict，防御性兼容属性式对象）；任何失败静默返回 None（有测试：缺失/异常/空 prompt 三路径）。
- **C2**：决策 prompt 拼接 = persona（system_prompt）+ life_extra + 心境摘要 + 可用活动（hybrid/llm 两档都带，有测试断言各成分在 prompt 里）。
- **C3**：`persona.life_extra` 配置默认值即任务书模板原文。

### 需求 D：心境影响活动与记忆

- rules 档倾向：energy<0.3 → reminisce/surf 权重 ×2；energy>0.7 → game ×2。有心境时走加权路径、无心情时等权 choice（兼容 M1 行为），权重点位有确定性测试。
- 活动周期结束调 `MoodState.record_activity(name, ok, topic=决策主题)`；心境更新失败不影响活动记账（有测试）。
- **重要度调节**：valence<0 时成功活动记忆重要度 +0.1（"低谷时的小确幸记得更牢"），钳制 [0,1]；失败不加成、心情好不加成（三条路径都有测试）。

### 需求 E：接线与配置

- `initialize()`：`MoodState` 构造（`__init__`）+ `load()`（initialize）+ `ActivityDecider` 组装（注入 llm_call/persona_getter/life_extra_getter/memory_getter）→ 传入 LivingLoop。
- `LivingLoop(mood=None, decider=None)` 可空：空则完全退回 M1 行为（有回归测试）。
- 决策 LLM provider：`model.provider_id` 配置优先；留空回退 `get_current_chat_provider_id(幽灵事件 uwo)`；再失败静默回退——**默认配置没配专用 provider 也能跑**（真实环境已验证此回退链，见 §2）。
- 配置生效：`decision.decision_mode`（rules/hybrid/llm，默认 hybrid）、`persona.life_extra`（新增）、`model.provider_id` 转生效，全部热读。

---

## 2. 真实 AstrBot 内验证情况（本报告重点）

**组件级真实验证：已做，通过**（`scripts/verify_decider_llm.py`，M0 最小 Context 同法 + 合成包加载生产 main.py 代码路径，只读不改运行中实例）：

| 步骤 | 结果 |
|---|---|
| provider 回退路径（provider_id 留空 → get_current_chat_provider_id） | ✅ 解析到 `Jasper/glm-5.3-flash`（真实 acm/sp 链路） |
| 真实 persona 读取 | ✅ 命中生产人格「你是凛，一位冷静、温柔、干练的女仆…」 |
| hybrid 档真实 LLM 出参数 | ✅ game + style=「慢节奏文字冒险：驾驶小飞船在星图间巡航探索，顺路在各个太空站」（30 字截断生效） |
| llm 档真实 LLM 全权选择 | ✅ 自选 `read` + topic=「系外行星」——**主题取自注入的近期记忆**，JSON 服从度良好 |
| 用决策参数真实跑活动 | ✅ game 活动真实执行返回结果（summary 里 83 猜了 7 次） |

证据留档 `scripts/m2_llm_verify.json`。脚本运行 4 次小 LLM 调用 + 1 次博查搜索（约几千 token），与运行中 AstrBot 无冲突。

**完整插件运行验证：未做**（重启/热载运行中的 AstrBot 超出 zcode 权限，同 M1）。主人手动验证步骤：

1. 部署插件目录 → WebUI 重载（同 M1 步骤）；
2. 观察 hybrid 档（默认）：把 `decision.impulse_check_interval_minutes=1`、`activity_probability=1.0`、`capabilities.cooldown_between_activities_hours=0`，等一次活动周期，DEBUG 日志（`misc.log_level=DEBUG`）可见 `[Decider]` 相关调用与 `[LivingLoop] 活动开始`；活动记忆里的主题词应来自 LLM 而非固定池（对比：池里没有"系外行星"这类词）；
3. 切 llm 档：`decision.decision_mode=llm`，观察日志中活动选择是否随心境/记忆变化（如把 energy 调低后是否更常选 reminisce/surf）；
4. 恢复默认配置。观察期注意：hybrid/llm 档每次活动多 1 次 LLM 调用（小 prompt，千级 token），每日活动 3 次时增量约万级 token/天，符合总纲成本预估。

## 3. 测试结果

```
143 passed, 1 skipped (15s)
```
- M1 基线 94 条 + 1 跳过 **无回归**；
- 新增 49 条：MoodState 10（持久化/规则/钳制/衰减/脏数据）、决策三档 18（含 prompt 组装/回退/参数截断）、活动 params 5、循环集成 10（心境记录/重要度三路径/决策器接线/向后兼容）、persona 与 LLM 接线 5、schema 1。

## 4. 遇到的坑

1. **`create_backend` 式"降级掩蔽"再次出现**：差点在 LazyMemory 里复用 `create_backend(auto)`，它内部降级返回 SimpleBackend 会骗过"探测成功"判定（M1 已踩过）——M2 虽没直接踩，但 persona 读取的"空 prompt"返回 `''` 而非 None 曾骗过测试，归一化后修复。教训固化：**对外接口"失败"必须显式（None/异常），不要用空值冒充成功**。
2. **`acm.get_conf` 有前置条件**：`AstrBotConfigManager` 必须 `initialize()` 后才能 `get_conf`（内部读 abconf 映射）。`get_current_chat_provider_id` 的回退链依赖它——生产 initialize 由 AstrBot 完成无感，验证脚本需自行调用（只读）。
3. **Windows 控制台 GBK 乱码**：验证脚本打印子进程中文 stdout 时乱码，JSON 证据文件 UTF-8 正常——功能无损，观察日志时建议 Windows Terminal + UTF-8。
4. **测试基建**：`asyncio.run` 不能包裹同步方法（`rules_pick` 是同步的）；aiosqlite 连接跨事件循环会挂死——新测试一律"一个连接一个 asyncio.run"。

## 5. 配置项变更清单

| 组 | 键 | 变更 | 默认 |
|---|---|---|---|
| decision | decision_mode | M0 占位 → **M2 生效**（rules/hybrid/llm） | hybrid |
| model | provider_id | M0 占位 → **M2 生效**（决策 LLM 专用，留空回退默认） | 空 |
| persona | life_extra | **新增**（text，含任务书 C3 默认模板） | 模板原文 |

## 6. 给 M3 的建议

1. **把活动执行升级为 tool_loop_agent 已经有充分理由**：真实验证里 LLM 给出的 style 是"驾驶小飞船在星图间巡航探索"——很有生活气息，但 M1 硬编码模板池只有两个小游戏，style 匹配不上只能落回随机。**决策的想象力已经超过执行力的上限**，这正是 M3 开放 agent 循环（LLM 现场写代码/现场搜）的最佳论据。接入点已留好：`ActivityContext.event`（幽灵事件）+ `decider` 产出的 params 可直接作为 agent prompt。
2. **token 闸门应随 agent 循环一起上**：llm 档目前单次活动仅 1 次决策调用（千级 token）；一旦接 agent 循环，单活动会变成 3-6 轮工具调用（万级 token）。建议 M3 第一优先做 `decision.single_run_token_budget` 硬闸（从 `LLMResponse.usage` 累计），先于趣味性。
3. **arousal 是休眠系统的现成挂点**：M2 已持久化 arousal 但未给动力学——建议 M3 入睡判定直接消费它（如活动成功 +0.05、睡前自然回落），与疲惫度共同构成"困了"的信号。
4. **决策 prompt 调参建议给凛**：llm 档真实输出质量初看不错（会从记忆里找线索），但样本量只有个位数；建议 M3 跑 3 天后回看 `mood.db` 的 interests 分布——如果兴趣涨得快的主题总被选（正反馈锁死），可在决策 prompt 里加"探索新事物"倾向或给 decay 加权。
5. **hybrid 档的心境注入可以更深**：当前 hybrid 只在 prompt 里带心境摘要，rules 选活动时也用了 energy 倾向；若凛观察到 hybrid 档主题与心境脱节，可把 interests top-3 直接写进 hybrid prompt（一行改动）。
