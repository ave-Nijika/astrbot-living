# 任务书 M2：心境状态机 + 决策升级（LLM 驱动的自主选择）

**项目**：astrbot-living（astrbot_plugin_living）——让 AstrBot 在无消息时拥有自己的生活
**前情**：M0（地基）与 M1（最小自主闭环）均已交付并核验，真实 AstrBot v4.28 VM 实测跑通（详见 `docs/archive/m0_report.md`、`docs/archive/m1_report.md`）。本任务书在 M1 已交付代码上继续扩展。
**全局依据**：`docs/项目总纲.md`（设计宪法，最高优先级）。本任务书只在总纲框架内细化 M2 范围。
**工作位置**：`D:\sandbox\astrbot-living`（代码在 `core/`、`main.py`，测试在 `tests/`）。
**交付**：代码 + pytest 全绿 + `docs/m2_report.md`。
**执行者**：zcode。交付后由凛核验。

---

## ⚠️ 关键约束（违反即打回）

1. **不改 AstrBot 本体、不改 LivingMemory 插件任何文件**（AGPL 红线，M0/M1 已守）。
2. **不 push 远程、不打 `.env`**。新依赖一律先列清单说明理由（pytest 已在 venv 中允许使用）。
3. **本任务书只做 M2 范围**：心境状态机 + 决策层 LLM 升级 + 人设接入。**不做** M3 的休眠/吵醒/梦、不做完整自由 agent 循环的复杂度（M2 用"决策 LLM + 现有能力"实现自主选择，活动本身仍走 M1 的能力调用，不接入 tool_loop_agent——那是 M3 后的事）。
4. 所有"次数/间隔/时长/模型"类配置项必须进 `_conf_schema.json`，运行时从配置读取，改配置不重启生效（沿用 M1 惯例）。
5. 代码注释中文，且解释"为什么"而非"是什么"。
6. 被定义"安静"的场景必须真的安静：默认零主动消息，任何活动产出（含日志刷屏）都不能打扰主人。

## 必读现状（M1 交付物）

- `main.py`：LivingPlugin(Star)，五能力 + `LazyMemory` + `LivingGate` + `LivingLoop` 已接线，`initialize/terminate` 幂等。
- `core/living_loop.py`：`LivingLoop`（心跳→闸门→活动周期→记忆双路径→候选分享），`_pick_activity()` 目前是 `random.choice + 避免重复`（这就是 rules 档雏形）。
- `core/activities.py`：`ActivityContext`（searcher/fetcher/sandbox/memory/gate/event/rng/now）、`ActivityOutcome`（name/summary/memory_content/importance）、五个 Activity（surf/read/game/peek/reminisce）+ `default_activities()`。
- `core/living_state.py`：`LivingGate`（SQLite 键值状态 + should_wake 判定链 + 记账）。
- `core/lazy_memory.py`：`LazyMemory`（懒加载探测 LivingMemory，降级 Simple，择机重试）。
- `core/ghost_event.py`：`build_ghost_event()`（M2 决策可能用到，预留）。
- `core/memory_backend.py`：`MemoryBackend` 接口（add/search/close）。
- `tests/`：94 测试全绿（M1 基线，不可回归）。
- `_conf_schema.json`：decision/output_gate/sleep/capabilities/memory/model/misc 七组。model.provider_id 已有占位（M1 未生效）。

---

## 需求 A：心境状态机（core/mood.py）

**目标**：给"它"一个内在状态——**它是谁、现在什么心情、对什么感兴趣**。M1 的活动选择是纯随机的，M2 要让随机变成"有倾向的随机"。

新建 `core/mood.py`，核心类 `MoodState`：

### 数据模型（SQLite 持久化，复用 living_state.db 或独立 mood.db，任选但需一致）

```
mood_valence   float  -1.0~1.0   心情积极/消极（默认 0.2）
mood_arousal   float   0.0~1.0   情绪唤醒度/兴奋度（默认 0.5）
energy         float   0.0~1.0   精力（默认 0.8）
interests      JSON 字段         主题->兴趣度 {str: float 0~1}（默认空，随活动累积）
```

- 提供 `load()/save()`、`update(activity_outcome, ...)`、`get_interests()`、`bump_interest(topic, delta)`、`decay_interests(rate)` 等方法。
- **更新规则**（活动完成后由 LivingLoop 调用）：
  - 活动成功：valence += 0.05，energy -= 0.1
  - 活动失败：valence -= 0.08，energy -= 0.05
  - 冲浪/读文章/玩小游戏成功后，对主题兴趣 +0.15；reminisce 成功后 memory 相关 +0.1
  - 每日兴趣衰减：interests 值 *= 0.9（每天一次，放 MoodState.load 时检查日期）
- 边界钳制：valence/energy/arousal 必须 clamp 在定义域内。

### 接口

```python
class MoodState:
    async def load(self) -> None
    async def save(self) -> None
    async def record_activity(self, activity_name: str, ok: bool, topic: str | None = None) -> None
    def interest_weight(self, topic: str) -> float   # 兴趣度，无记录返回 0.3 中性
```

## 需求 B：决策层 LLM 升级（rules / hybrid / llm 三档）

M1 的 `_pick_activity` 是纯随机。M2 引入决策模式配置 `decision.decision_mode`（已有占位，默认 hybrid），在 `LivingLoop` 或新建 `core/decider.py` 中实现三档：

### rules 档（默认不动，M1 现有逻辑）
`random.choice + 避免重复`——保留作为零成本兜底。

### hybrid 档（默认）——"随机选活动 + LLM 细化怎么做"
- 先用 rules 选一个活动（如 surf），再调 LLM 生成**该活动的具体执行参数**（如冲浪的主题词、读文章的主题词、小游戏的风格），注入 `ActivityContext`。
- LLM 调用用 `context.llm_generate` 或 provider 直接调用（参考 M0 verify_tool_loop.py 里的 provider 加载方式），**模型用 `model.provider_id` 配置，若为空则回退默认聊天 provider**（`context.get_current_chat_provider_id` 或等效）。
- 输出解析：要求 LLM 输出纯 JSON `{"topic": "..."}` 或等效结构，解析失败则回退随机主题（不阻塞活动）。
- **不接入 tool_loop_agent**——hybrid 就是"规则选活动 + LLM 提供参数"，活动执行仍走 M1 能力调用。

### llm 档——"让 LLM 自己选活动 + 给参数"
- 调 LLM 一次，输入：当前人设（需求 C）+ 心境摘要（A）+ 近期记忆（从 memory search 取 3-5 条）+ 可用活动列表与各自一句话描述。
- 输出：`{"activity": "surf|read|game|reminisce|peek", "params": {...}}`。解析失败回退 rules。
- 同样不接入 tool_loop_agent。

**验收**：三档各有至少一条测试（mock LLM：hybrid 档 mock 返回 topic、llm 档 mock 返回 activity 选择、解析失败回退）；`_conf_schema.json` 的 decision_mode 选项已有，确保运行时生效。

## 需求 C：人设接入（复用 AstrBot persona + 插件补充设定）

总纲 D4：**主人格直接复用 AstrBot 已选 persona**，插件只加"生活补充设定"。

### C1 读取当前 persona
- 用 `context.persona_manager`（或等效 API，M1 报告里 proactive_chat 源码证实 `self.context.persona_manager.get_default_persona_v3(umo=...)` 存在）取当前生效人格的 system_prompt。
- M2 决策调用（hybrid/llm 档）时，把 persona prompt 作为 system_prompt 传给 LLM。
- 取不到 persona 时静默跳过（决策仍可用，只是没有性格引导）。

### C2 生活补充设定（persona 的"生活细节"）
- 新增配置 `persona.life_extra`（text 类型，默认给出模板+示例），用户可填作息习惯、兴趣爱好、性格底色、讨厌的事等。
- 决策 prompt 拼接：`persona system_prompt` + `life_extra` + 心境摘要 + 可用活动。

### C3 默认 life_extra 模板（写进 _conf_schema.json 的默认值）
```text
# 生活补充设定（可自由修改，影响它独处时的行为倾向）
- 作息习惯：喜欢安静，通常在深夜有精神
- 兴趣爱好：对技术、宇宙、咖啡、独立游戏感兴趣
- 性格底色：温和、有点慢热，好奇心强
- 不喜欢：被打断、重复劳动
```

**验收**：mock persona_manager 验证读取成功/失败两路径；life_extra 配置生效。

## 需求 D：心境影响活动与记忆

- `LivingLoop` 活动周期中：选活动前调 `MoodState` 加权（`hybrid/llm` 档由 LLM 感知；rules 档可加简单倾向：energy < 0.3 时 reminisc/surf 权重提升，energy > 0.7 时 game 权重提升——可选，有测试即可）。
- 活动结束后 `MoodState.record_activity(...)`（接需求 A）。
- **记忆重要度调节**：`ActivityOutcome.importance` 与 MoodState 交互——valence 低时成功活动的记忆重要度 +0.1（"低谷时的小确幸记得更牢"），有测试。

## 需求 E：main.py 接线 + 配置项

- `LivingPlugin.initialize()` 构造 `MoodState`（注入 db 路径），传给 `LivingLoop`。
- `LivingLoop` 增加 `mood` 参数（可空，空则跳过心境逻辑，向后兼容）。
- 新增/生效配置项：
  - `decision.decision_mode`（已有占位，M2 生效：rules/hybrid/llm）
  - `persona.life_extra`（新增 text）
  - `model.provider_id`（已有占位，M2 生效：决策 LLM 专用，留空回退默认）
- 保持 M1 默认行为不破坏：decision_mode 默认 hybrid，但 hybrid 里 LLM 调用失败必须静默回退 rules——**默认配置下用户不配 provider 也能跑**。

## 测试与交付

- M1 基线 94 条全绿不回归。
- 新增测试覆盖：MoodState（持久化/更新/边界钳制/兴趣衰减/日期翻转）、决策三档（mock LLM 各路径+回退）、persona 读取（成功/失败）、重要度调节、main 接线幂等。预计新增 20-30 条。
- `docs/m2_report.md`：结论速览 / 各需求落地说明 / 测试结果 / 遇到的坑 / 配置项变更清单 / 给 M3 的建议（尤其：llm 档决策的 prompt 效果、是否值得把活动执行也升级为 tool_loop_agent、token 闸门的必要性）。**报告写清楚真实 AstrBot 内验证情况**：若无法启动 AstrBot 测试，说明原因并给出主人可执行的手动验证步骤。

## 红线（打回项）

- ❌ 不接入 tool_loop_agent / 完整自由 agent 循环（M3 后）。
- ❌ 不做休眠/吵醒/梦（疲惫度、睡眠债、起床气）——M3。
- ❌ 不引入新依赖（除非先列清单说明理由）。
- ❌ 不 push 远程、不打 `.env`。
- ❌ 不改 AstrBot 本体 / LivingMemory。

## 汇报节点

1. 开工前若有歧义先问，别猜。
2. 交付后附 `m2_report.md` + 测试结果 + git 提交历史，等凛核验。核验通过前不 push。
