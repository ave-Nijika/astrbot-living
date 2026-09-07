# 任务书 M0：astrbot-living 地基验证

**项目**：astrbot-living——让 AstrBot 在无消息时自主"生活"的插件
**项目总纲**：`D:\sandbox\astrbot-living\docs\项目总纲.md`（**开工前必读**，本任务书只覆盖 M0，全局设计以总纲为准）
**工作目录**：`D:\sandbox\astrbot-living`（代码就在这里，不要放别处）
**交付**：插件骨架 + 五个能力组件 + 风险验证结论 + 每项的测试。不发布、不接真 QQ 群发。

---

## ⚠️ 先读懂这个项目是什么（30 秒版）

AstrBot 平时是"收到消息 → LLM 回复"的工具。这个插件让它**在没有消息的时候也有自己的动机**：后台循环定期"想一想"，觉得无聊就自己决定干点什么（搜索、看文章、写代码），像人一样有生活轨迹。**M0 不做动机和循环**，只验证并搭好它需要的全部地基。

**最重要的一条工作纪律**：
> 本任务书有大量"先验证再写"的步骤。**验证失败≠任务失败**。遇到"预期能跑通但实测不行"的情况，停下来把失败现场（报错全文、复现脚本）写进交付报告，**不要绕过验证硬写功能**。M0 的价值一半在"证明哪些路走得通"。

---

## 环境事实（已核实，直接用）

| 项 | 值 |
|---|---|
| AstrBot 版本 | **v4.27.5** |
| AstrBot 根目录 | `D:\astrbot\AstrBotLauncher-0.3.0\AstrBotLauncher-0.3.0\AstrBot` |
| AstrBot venv | `<AstrBot根>\venv`，Python **3.14.6**（很新，写代码注意 API 兼容） |
| 本机系统 | Windows 11，**无 docker**（沙箱做基础隔离即可，见 R3） |
| 已装可用库 | httpx / aiohttp / requests / aiofiles / aiosqlite ✓；pytest ✗（需装到 venv 或用 unittest） |
| 博查搜索 key | 已在 AstrBot 配置（`data/cmd_config.json` → `provider_settings.websearch_bocha_key`，该文件带 UTF-8 BOM） |
| 参考插件 | `data/plugins/astrbot_plugin_livingmemory`（AGPL，只运行时调用不复制代码！） |

## 环境红线

1. **不改动 AstrBot 本体和 LivingMemory 的任何文件**。本插件只读取它们。
2. **不复制 LivingMemory 源码**（AGPL 传染）。只允许运行时 `import` 调用其公开接口，或参考 AstrBot 内置代码的**调用方式**。
3. 新依赖安装进 AstrBot venv 前先在报告里列出清单和理由（pytest 允许装）。
4. `metadata.yaml` 格式参照 LivingMemory 的极简五字段（name/author/version/description/repo）。

---

## R0【最高优先】风险验证：无事件自主 agent 循环

这是全项目的生死题，**先做这个，通过才继续往下**。

**目标**：不经过任何消息事件，直接以代码发起一次带工具调用的 LLM agent 循环。

**已核实的情报**（可直接相信，但需实测确认）：
- `Context.tool_loop_agent` 签名（`astrbot/core/star/context.py`）：
  ```python
  async def tool_loop_agent(self, *, event: AstrMessageEvent, chat_provider_id: str,
      prompt=None, image_urls=None, audio_urls=None, tools: ToolSet = None,
      system_prompt=None, contexts=None, max_steps=30, tool_call_timeout=120, **kwargs)
  ```
- runner 源码（`astrbot/core/agent/runners/tool_loop_agent_runner.py`）里 event 仅用于 `getattr(context, "event", None)` 取 persona 错误信息，**None 有兜底**——理论上 `event=None` 可跑
- kwargs 支持 `agent_context`（可自构 `AstrAgentContext(context=self, event=None)`）
- 工具注册参考 LivingMemory `main.py` 的 `_register_agent_tools_if_needed`：`self.context.add_llm_tools(*tools)`，工具类继承 `astrbot.core.agent.tool.FunctionTool`

**验证步骤**：
1. 写最小脚本（可用 `scripts/verify_tool_loop.py`，在 AstrBot venv 下运行）构造一个假 FunctionTool（如"掷骰子"），不传 event（或 event=None），调 `tool_loop_agent`，prompt 要求它"调用工具掷一次骰子并报告结果"
2. 验证三件事：循环能起、工具被真实调用、能拿到最终 `LLMResponse`
3. 如果 `event=None` 报错，尝试自构最小 `AstrAgentContext(event=None)`；再不行，尝试构造一个"幽灵 AstrMessageEvent"（仅填 unified_msg_origin 等必需字段）——按尝试顺序记录每条路的结论

**判定**：任一路跑通 → R0 通过，在报告里写明用的哪条路；全部失败 → **停止后续任务**，交报告。

---

## R1 插件骨架

- 目录结构：`main.py` + `metadata.yaml` + `_conf_schema.json` + `core/`（能力组件）+ `tests/`
- `main.py`：标准 Star 插件，`__init__` 里暂只做组件初始化；预留 `async def terminate()`
- `_conf_schema.json` 先写 §6 配置清单的骨架（组+关键项，值用总纲默认值），M1 再补全细项
- 插件名：`astrbot_plugin_living`（工作代号，主人可后改）
- 验收：AstrBot 重载后插件出现在插件列表且无报错（这步需要主人配合重启 AstrBot 看 WebUI，或在交付说明里写清自查步骤）

## R2 五个能力组件（core/ 下各一文件）

统一约定：每个能力一个类，异步方法，可独立 import 测试，构造时传入 `Context` 或所需对象。每个能力必须带测试。

### C1 搜索（core/search.py）
- 优先方案：运行时从 AstrBot 工具管理器拿内置 `BochaWebSearchTool` 复用（`astrbot/core/tools/web_search_tools.py`），直接调用其执行逻辑（研究该工具类如何被 FunctionToolExecutor 执行，取其核心 HTTP 调用逻辑包一层亦可——**只写调用适配，不复制它的代码文件**）
- 兜底方案：直接 HTTP 调博查 API（key 从 AstrBot 配置读取），参数对齐 Bocha 官方文档（query/freshness/summary/count）
- 接口：`async def search(query, count=5, summary=True) -> list[dict]`（title/url/summary）
- 测试：真实调一次"Next.js 16 新特性"，断言返回 ≥1 条且含 url 字段

### C2 网页抓取（core/fetcher.py）
- `async def fetch(url) -> dict`（title/text/status）：httpx 异步 + 超时 15s + UA 伪装常规浏览器 + 响应大小上限 2MB
- 正文提取：去 script/style/nav，按行保留文本，压缩空行（简单启发式即可，不引重型依赖）
- 编码容错（requests 兜底自动检测的思路或 charset-normalizer）
- 测试：抓一个稳定页面（如 example.com 或维基百科条目），断言 title 非空、正文含关键词

### C3 沙箱执行（core/sandbox.py）——M0 从简，安全红线从紧
Windows 本机无 docker。M0 实现基础隔离：
- 子进程执行（`asyncio.create_subprocess_exec` 用 AstrBot venv python）
- 硬超时（默认 10s，超时杀进程树）、stdout/stderr 截断（各 8KB）
- **执行前静态扫描**：源码包含以下即拒绝——`import os`/`import subprocess`/`import socket`/`import shutil`/`import sys`/`open(`/`__import__`/`eval(`/`exec(`/`ctypes`；白名单思路：仅允许标准库的 `random/math/time/datetime/json/re` 与纯逻辑
- 工作目录用临时目录，执行完清理
- 接口：`async def run(code, timeout=10) -> {ok, stdout, stderr, timed_out}`
- 明确在代码注释里写：**此隔离是启发式的，防呆不防黑客；Windows Job Object 强化在 M4**
- 测试：①合法脚本（算 7*6 + random）通过并返回 stdout；②含 `import os` 的脚本被拒；③`while True: pass` 超时被杀
- ⚠️ C3 是 M1 后 zcode 安全审核的第一重点，注释里写清已知局限

### C4 记忆后端（core/memory_backend.py）
- 定义抽象基类 `MemoryBackend`：`async add(content, importance=0.5, metadata=None) -> int`、`async search(query, k=5) -> list[dict]`
- `LivingMemoryBackend`：运行时 `context.get_registered_star("astrbot_plugin_livingmemory")` → `star_cls.initializer.memory_engine`，`hasattr` 逐级探测，调 `add_memory` / `search_memories`（签名见总纲 §4）。任何一步探测失败 → 报告"不可用原因"
- `SimpleBackend`：aiosqlite 单表（id/content/importance/metadata_json/created_at），search 用 LIKE 关键词（M0 够用）
- 提供工厂 `create_backend(context) -> MemoryBackend`（自动探测优先，配置可强制）
- 测试：SimpleBackend 全流程；LivingMemoryBackend 写一个探测测试（环境里已装该插件，直接实例化验证；若需 AstrBot 运行时环境，写清手动验证步骤）

### C5 主动发消息（core/sender.py）
- 薄封装 `context.send_message(session: str, message_chain)`（已核实该 API 支持无事件发送，session=unified_msg_origin 字符串）
- `async def send(session, text)`：组 MessageChain 发送，异常捕获返回 bool
- 测试：写法上验证 message_chain 构造正确；真发消息属 M1（避免 M0 阶段乱发）

---

## R5 自验与交付

- 测试：装 pytest 进 AstrBot venv（或 unittest，二选一，报告里说明），`tests/` 下每组件至少 2 条用例，全部通过
- **验证报告**：`docs/m0_report.md` 必须包含——R0 结论（走的哪条路+关键代码片段+输出证据）、五组件各自状态（通过/受限/失败+原因）、遇到的坑、新装依赖清单、给 M1 的建议
- 自检 grep：全项目无 `from astrbot_plugin_livingmemory`（必须运行时探测而非 import）、无对 AstrBot 本体文件的写操作、`metadata.yaml` 五字段齐全
- **不 push 不发布**。git init 本地仓库、commit 分好逻辑提交即可

## 红线

- 不改 AstrBot 本体 / LivingMemory / 其他插件任何文件
- 不复制 LivingMemory 代码（AGPL）；不引入其源码片段
- R0 未通过前不写 R1-R5 的实现代码（可以写目录骨架）
- 沙箱不做"真安全"的虚假声明——注释如实写隔离级别
- 不引入重型依赖（浏览器自动化/selenium/playwright 一律禁止，见总纲 D8）

## 汇报节点

1. R0 结果一出立即回报（通过/失败+证据），**这是第一优先的回报点**
2. 全部完成后交 `m0_report.md` + 代码，等凛核验
