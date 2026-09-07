# M0 验证报告：astrbot_plugin_living 地基验证

**日期**：2026-09-07　**执行**：zcode　**任务书**：`docs/task_M0_地基验证.md`
**结论速览**：R0 ✅ 通过（路线 C 幽灵事件）｜骨架 ✅｜五能力 ✅（全部真实跑通）｜测试 **46 通过 / 1 跳过**｜无红线违反

---

## 1. R0 风险验证：无事件自主 agent 循环（生死题）

**结论：通过。生产路径 = 构造"幽灵 AstrMessageEvent"（路线 C）。**
**总纲 §5 的预判"runner 对 event 仅 getattr 兜底、event=None 理论可跑"被实测推翻**——runner 那层确实兜底了，但工具执行层还有一道硬检查。这正是 M0 要先验证的价值。

### 四条路线实测记录（按任务书要求的尝试顺序）

| 路线 | 做法 | 结果 | 证据 |
|---|---|---|---|
| A | `tool_loop_agent(event=None, ...)` | ❌ FAIL | `AstrAgentContext` 是 pydantic dataclass，`event: AstrMessageEvent` 字段校验直接拒绝 None：`Input should be an instance of AstrMessageEvent`。失败发生在任何 LLM 调用之前，零 token 消耗 |
| B | `agent_context=AstrAgentContext(context=ctx, event=None)` | ❌ FAIL | 同上，卡在我们自己的构造处 |
| B2 | `object.__new__` 绕过校验构造 `event=None` 上下文 | ❌ FAIL（关键发现） | 循环**能起**、LLM **能响应**、能拿到 `LLMResponse`，但工具执行时 `FunctionToolExecutor._execute_local`（astr_agent_tool_exec.py:632）抛 `ValueError: Event must be provided for local function tools.`，错误字符串被返回给 LLM，骰子工具**从未被真实执行**。若只看"有没有抛异常"会误判为通过，故判定标准以"工具被真实调用"为准 |
| C | 构造最小 `GhostEvent(AstrMessageEvent)`（仅填 message_obj/platform_meta/session_id 等必需字段） | ✅ PASS | 循环起 ✓ 工具被真实调用 ✓（`roll_dice` 执行返回 5 点）最终 `LLMResponse` ✓（文本"骰子掷出的结果是 **5** 点！"），全程 12.2s |

### R0 路径关键代码

```python
class GhostEvent(AstrMessageEvent):
    """仅满足 AstrAgentContext 类型校验的最小事件。"""

platform_meta = PlatformMetadata(name="living", description="...", id="living_ghost")
message_obj = AstrBotMessage()
message_obj.type = MessageType.FRIEND_MESSAGE
# ...填 session_id/message_id/message=[] 等必需字段

event = GhostEvent(message_str=..., message_obj=message_obj,
                   platform_meta=platform_meta, session_id="living_autonomous")
resp = await context.tool_loop_agent(
    event=event,                      # 幽灵事件，非 None
    chat_provider_id=provider_id,
    prompt="请调用 roll_dice 工具掷一次骰子，然后把结果告诉我。",
    tools=ToolSet(tools=[dice_tool]),
    max_steps=5,
)
```

生产实现已固化在 `core/ghost_event.py`（`build_ghost_event()`），并有回归测试
`tests/test_ghost_event.py` 锁定该结论——若未来 AstrBot 上游移除了工具层的
event 检查，测试会提示 M1 重新评估 event=None 路线。

### 对 M1 的直接影响

1. 自主循环每轮活动需调一次 `build_ghost_event()`，uwo 形如 `living_ghost:FriendMessage:living_autonomous`，可作为识别"自主活动流量"的标记；
2. 工具若返回 `MessageEventResult` 会触发 `event.set_result`/`event.send` 路径——我们的工具一律返回**字符串**，避开该路径（幽灵事件上 send 无意义）；
3. handoff（子代理）路径会直接取 `event.unified_msg_origin` 并查配置，M1 若用 handoff 需另行验证幽灵事件兼容性。

---

## 2. 五能力组件状态

| 组件 | 文件 | 状态 | 说明 |
|---|---|---|---|
| C1 搜索 | `core/search.py` | ✅ 通过 | 兜底方案直连博查 API（内置工具的 `call()` 需要 event 上下文，与组件化使用不匹配，任务书允许此兜底）。key 运行时读 AstrBot 配置，兼容 list 轮换。真实调"Next.js 16 新特性"返回 5 条含 url 结果（经本机系统代理 127.0.0.1:7892） |
| C2 抓取 | `core/fetcher.py` | ✅ 通过 | example.com 实测：title 非空、正文含关键词；2MB 流式截断；charset-normalizer 编码容错；正文提取去 script/style/nav 并压缩空行 |
| C3 沙箱 | `core/sandbox.py` | ✅ 通过 | 合法脚本执行 ✓（7*6+random）；`import os`/`from subprocess import run`/`import json, os`（逗号混合）等全部拒绝 ✓；`while True` 3s 超时杀树 ✓ 且沙箱可复用；超长 stdout 截断到 8KB 上限且不挂死（持续排空管道防死锁） |
| C4 记忆 | `core/memory_backend.py` | ✅ 通过（探测单测覆盖） | SimpleBackend 全流程（增/查/持久化/关闭）实测通过；LivingMemory 探测逻辑用假对象逐级验证 5 种失败原因均给出可诊断信息。**真实引擎联测需 AstrBot 运行时**（注册表在脚本环境为空，测试自动 skip），手动验证步骤见 §5 |
| C5 发消息 | `core/sender.py` | ✅ 通过（按任务书不真发） | message_chain 构造、API 调用参数、False/异常/非法 session 三类失败路径均有测试。真发属 M1 |

## 3. 插件骨架状态

- `main.py`：Star 插件（现代模式，不用已废弃的 `@register` 装饰器），`__init__` 轻量构造五组件，`initialize()`（AstrBot 自动调用）里做记忆后端探测，`terminate()` 释放 httpx 客户端与 SQLite 连接。
- `metadata.yaml`：五字段齐全（name/author/version/description/repo）。
- `_conf_schema.json`：总纲 §6 的七组骨架（决策/输出闸门/休眠/能力开关/记忆/模型/杂项），值用总纲默认值，M1 补细项。
- **导入冒烟通过**：`scripts/smoke_import_plugin.py` 以合成包上下文真实 import main.py 成功（相对导入、astrbot.api 依赖、`__init__(context, config)` 签名均验证）。
- **WebUI 可见性验收需主人配合**（脚本环境无法验证插件加载器）：把本目录整体复制（或 symlink）为 `AstrBot根\data\plugins\astrbot_plugin_living\`，在 WebUI 插件页重载，应出现 `astrbot_plugin_living` 且日志有 `astrbot_plugin_living M0 骨架加载完成` 与 `记忆后端: ...` 两行。已知风险点：`initialize()` 里 `get_astrbot_plugin_data_path()` 会创建 `data/plugin_data/astrbot_plugin_living/` 目录（我们插件自己的数据目录，符合插件惯例，不碰本体文件）。

## 4. 遇到的坑（对 M1 有用的）

1. **工具执行层的 event 硬检查**（R0 核心发现，见 §1）。
2. **`astrbot.api` 循环导入**：独立脚本 import astrbot 必须以 `astrbot.api` 为入口（它第 4 行先定义 `sp` 再触发 star 链）；直接 import `star.context`/`kb_mgr` 会撞环。已写进 verify 脚本注释。
3. **AstrBot 以源码树运行**：venv 里没有 pip 安装 astrbot，外部脚本/tests 必须把 AstrBot 根加进 sys.path 并设 `ASTRBOT_ROOT` 环境变量（`get_astrbot_root()` 优先读它），否则路径解析指向 cwd。
4. **pydantic dataclass 校验在 `__init__`**：`object.__new__` 可以绕过（B2 路线证明了循环本身不依赖 event），但绕过没意义——工具层照样拦。
5. **asyncio 子进程管道死锁**：子进程输出超过管道缓冲且不被读取时 `proc.wait()` 会永久挂起；解法是独立 task 持续排空管道、只保留上限字节，其余丢弃。
6. **运行实例在跑**：验证脚本全程未启动 lifecycle/dashboard/平台适配器、未调 `db.initialize()`（SQLite 引擎惰性连接），与运行中的 AstrBot 无冲突；代价是测试会向 AstrBot 的 data/logs 追加少量日志行。
7. **博查 key 配置格式**：`websearch_bocha_key` 是 list（多 key 轮换），适配时按"取第一个可用值"处理。

## 5. 给凛的核验清单 & 手动验证步骤

**自动可核验**（AstrBot venv 下 `python -m pytest tests/ -v`，46 通过/1 跳过）：
- 跳过项 `test_probe_against_real_astrbot_context` 属预期：真实插件注册表只在 AstrBot 进程内存在。

**LivingMemory 真实引擎联测（需 AstrBot 运行，建议 M1 开工时顺手做）**：
1. 在插件目录放一个临时命令 handler（或用 M1 的调试命令）里执行：
   `backend, note = await create_backend(context, "auto", db_path)`；
2. 预期 note 为"使用 LivingMemory 引擎"（LivingMemory 已装且激活）；
3. `await backend.add("测试", 0.5)` → `await backend.search("测试")` 应返回该条；
4. 若走到降级分支，note 会带具体不可用原因（未安装/未激活/initializer 不存在/引擎签名不匹配），按原因排查。

**插件加载验收（需重启/重载 AstrBot）**：见 §3 第三条。

## 6. 新装依赖清单（任务书红线 3）

| 包 | 版本 | 理由 |
|---|---|---|
| pytest | 9.1.1 | 任务书允许（装进 AstrBot venv）；未装 pytest-asyncio，测试用 `asyncio.run` 包裹，零额外依赖 |

未新增任何运行时依赖（httpx/aiohttp/charset-normalizer/aiosqlite 均为 venv 已有）。

## 7. 红线自检

- ✅ 全项目无 `import astrbot_plugin_livingmemory` / `from astrbot_plugin_livingmemory`（grep 证实，只有字符串常量做运行时探测）
- ✅ 无对 AstrBot 本体 / LivingMemory / 其他插件文件的写操作（只读配置；写入仅限我们插件自己的 `data/plugin_data/astrbot_plugin_living/`、系统临时目录、tests 的 tmp_path）
- ✅ 未复制 LivingMemory 任何源码（记忆后端只有两个防御性调用签名来自总纲 §4；沙箱/抓取/搜索全部自写）
- ✅ metadata.yaml 五字段齐全（有测试锁定）
- ✅ R0 通过后才写 R1-R5 实现（提交历史可查）
- ✅ 沙箱隔离级别如实声明（`core/sandbox.py` 模块 docstring：防呆不防黑客，M4 做 Job Object 强化）
- ✅ 未 push 未发布；本地 git 已按 R0/骨架+组件/测试 三笔逻辑提交

## 8. 给 M1 的建议

1. 主循环直接复用 `core/ghost_event.py`；每次活动独立 session_id（如 `living_20260907_153000`）便于日志追踪；
2. 决策层 prompt 拼接时把"能力清单+预算余额"作为系统提示注入，工具轮数用 `tool_loop_agent(max_steps=配置值)` 控制而非自旋；
3. provider 强制独立配置的闸门（总纲 §6 model.provider_id）建议在 M1 就做"未配置则拒绝启动 llm 档"的硬检查，防烧聊天模型；
4. `tool_loop_agent` 的 `max_steps` 触顶时会正常返回已有响应（不抛异常），闸门层需要另外统计 token 消耗（LLMResponse.usage）；
5. LivingMemory 真实联测（§5）建议 M1 第一天做掉，失败也不阻塞 M1（auto 会降级 SimpleBackend）。
