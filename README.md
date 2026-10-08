# astrbot_plugin_living

让 AstrBot 在无消息时拥有自己的生活——闲时由心境与记忆驱动的冲动，自主冲浪、读文章、现场写小游戏、整理回忆。

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![AstrBot](https://img.shields.io/badge/AstrBot-v4.27.5+-orange.svg)](https://github.com/AstrBotDevs/AstrBot)
[![Python](https://img.shields.io/badge/Python-3.10+-blue.svg)](https://www.python.org)

## 这是什么

大多数聊天机器人收到消息才回复，没消息时就是死的。这个插件让你的 AstrBot 在你没找它的时候也"活着"——它会自己决定要不要搜点什么看看、写个小游戏玩玩、翻翻以前的记忆，把经历写进长期记忆。下次你聊到相关话题，它真的"经历过"。

**它不是定时推送器**——每次活动都是它自己的"冲动"，由心境状态机 + 当前记忆 + 人设共同决定。沉默也是它的状态，不是故障。

## 功能

### 自主活动

| 活动 | 做什么 |
|---|---|
| 冲浪（surf） | 挑个感兴趣的主题搜一搜，看看标题 |
| 读文章（read） | 搜到结果后真的点进去读正文 |
| 写游戏（game） | 自己写 Python 代码丢进沙箱跑起来玩 |
| 整理回忆（reminisce） | 翻翻以前的记忆，像人翻旧相册 |

支持三种决策模式（`decision_mode` 配置）：
- **rules**：纯规则零成本，加权随机
- **hybrid**（默认）：规则选活动 + LLM 生成具体参数
- **llm**：LLM 全权决定做什么和怎么做

### agent 循环（M3）

活动可升级为完整 agent 循环——LLM 拿着工具集（搜索/抓取/沙箱/记忆）自己决定怎么完成任务。现场写代码、现场搜资料、玩到一半发现 bug 自己改。带 token 硬闸（`single_run_token_budget`），不会无限烧钱。

### 心境状态机

- **valence**（-1~1）：心情积极/消极，活动成功+，失败-
- **arousal**（0~1）：情绪唤醒度，为 M3 休眠系统预留
- **energy**（0~1）：精力，活动消耗，休眠恢复
- **interests**：主题兴趣度，随活动累积，每日自然衰减

心境影响活动选择的倾向、记忆重要度（低谷时的小确幸记得更牢）。

### 休眠系统

- **作息窗口**（`sleep_window`，默认 00:30-08:00）：该睡就睡，不回复消息
- **疲惫度**：活动消耗，睡眠恢复
- **睡眠债**：被吵醒打断睡眠会累积，影响次日精力上限
- **吵醒**（A+B 机制）：休眠窗内 10 分钟连发 3 条消息 → 唤醒（有起床气概率）
- **清醒待机**：吵醒后 30 分钟内保持可聊（滑动窗口——期间每条消息刷新待机时长）
- **唤醒确认**：吵醒后立即发一条预置消息（可自定义），不等 LLM
- **梦**：醒来后低概率生成一段梦话，写进记忆

### 记忆

- **软依赖** [astrbot_plugin_livingmemory](https://github.com/lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory)（推荐安装）——长期记忆 + 知识图谱 + 混合检索
- 未安装时自动降级为内置 SQLite 后端
- 所有活动记忆第一人称、带日期感、支持 LivingMemory 联动召回

### 模型故障转移链

自主活动的 LLM 调用按顺序尝试多个 provider：专用模型（`model.provider_id`）→ 手动备用链（`model.fallback_chain`）→ 所有已启用的聊天模型自动兜底。404/429/超时自动切换，401 不重试（换模型也没用）。

### WebUI 面板

插件详情页 → Pages → 可视化管理：条目列表拖拽排序、开关、编辑、变量配置、组装预览。

## 安装

1. 在 AstrBot WebUI → 插件管理 → 从仓库安装，填入本仓库地址
2. 或手动克隆到 `data/plugins/astrbot_plugin_living/`
3. 重启 AstrBot

### 可选依赖

- [astrbot_plugin_livingmemory](https://github.com/lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory)：长期记忆 + 知识图谱（不装则降级 SQLite）

### 浏览器能力（可选安装）

AstrBot 的"自由上网"分两层：搜索/读文本开箱即用；**浏览器（真实打开网页、看画面、点页面）依赖 Playwright 的 Chromium 内核**，出于体积考虑不随插件内置，需要时手动安装。

- **需要装什么**：Playwright 的 Chromium 浏览器内核（约 150MB 下载，装一次即可）。
- **有什么作用**：装好后，能力档位 ≥1 时 AstrBot 会多出五件浏览器工具——打开网页、读页面、把看到的画面截图存档、点击元素、填写输入框。截图会真正进入它的"眼睛"：活动模型支持图片输入时它直接看到画面（不支持时可配置转述模型代看）。登录态会保存在它的工作区（browser_state.json），下次接着用。
- **不装会怎样**：五件浏览器工具不挂载（能力档位照常显示），它的自主活动自动退化为"搜索 + 读文本"模式——照样冲浪读文章，只是看不到画面、点不了页面，其余能力（游戏/记忆/搜索等）完全不受影响。
- **怎么安装**：在 AstrBot 的 Python 运行环境里执行 `playwright install chromium`，装完重启 AstrBot（或重载本插件）。
- **怎么卸载**：执行 `playwright uninstall chromium`（或直接删除 Playwright 缓存目录：Windows 为 `%LOCALAPPDATA%\ms-playwright`，Linux/macOS 为 `~/.cache/ms-playwright`）。卸载后它自动回落到无浏览器形态，无需改任何配置。
- **怎么看装没装**：配置面板新手页"浏览器能力"卡片会实时显示"已安装 / 未安装"。

## 配置

所有配置项可在 WebUI 中调整，热生效（改了不用重启）。完整列表见 `_conf_schema.json`。

### 关键配置

| 配置 | 说明 | 默认值 |
|---|---|---|
| `decision.decision_mode` | 决策模式：rules / hybrid / llm | hybrid |
| `decision.impulse_check_interval_minutes` | 心跳间隔（分钟） | 45 |
| `decision.daily_impulse_limit` | 每日活动上限（0 = 不限） | 3 |
| `decision.single_run_token_budget` | 单次活动 token 硬闸 | 20000 |
| `sleep.sleep_window` | 作息窗口 | 00:30-08:00 |
| `sleep.sleep_mute_replies` | 休眠期拦截消息 | true |
| `sleep.awake_standby_minutes` | 清醒待机时长（分钟） | 30 |
| `model.provider_id` | 自主活动专用模型（留空用默认） | 空 |
| `model.allow_chat_fallback` | 失败时允许回退到聊天模型（关 = 缓存保护） | true |

### 为什么建议给 living 单独配一个模型

**结论：强烈建议在 `model.provider_id` 里给 living 配一个独立的 provider，最好用不同的 api key / 账号。**

原因在于大模型服务的**缓存计费机制**：对话缓存按账号（api key）维度存，**一个账号只有一套**。一旦有请求以不同的前缀打进来，之前那套缓存就被冲掉；原先那条链路再请求时，全部历史要按未命中重新计费。

living 在它自主活动时会调用大模型：自主活动（agent 循环）、选活动、主动搭话（两处）、分享改写、晚安（llm 档）、醒来补回复、梦话、约定提取、风格提炼——共九处调用。如果 `model.provider_id` 留空，这些调用**全部与你的聊天共用同一个模型和账号**，于是：

- 它每做一次活动 / 搭一次话 / 发一次分享，你聊天的缓存就被冲掉一次；
- 之后你和它继续聊天时，全部历史按未命中重新计费（对话越长，这次重算越贵）。

配了独立 provider（不同账号）后，它的调用走自己的账号，与你的聊天缓存**互不影响**。

**两个相关开关：**

- `model.allow_chat_fallback`（默认开）：模型故障转移链走完后是否还回退到聊天模型。开了保持旧行为，但失败时会冲聊天缓存；关了则只重试链里配置的 provider，全部失败就按本轮失败处理——**宁可它这次活动失败，也不冲你聊天的缓存**。
- `model.prefix_cache_ttl_minutes`：如果你选择不配独立 provider（共用模型），living 会自动采用**前缀对齐**——它的调用带上与你聊天完全相同的 system 与会话历史，从而命中同一套缓存、不再冲缓存。该配置控制这个缓存快照的时效（默认 360 分钟，0 = 不过期），一般不用改。

`judge.provider_id`（M19 判断模型）同理：它必须与聊天模型分开；未配置时判断模型完全不调用，这本身就是缓存保护。

## 已知限制

- **沙箱隔离是启发式的**（"防呆不防黑客"）——静态 import 白名单 + 超时 + `-I` 隔离模式，挡不住故意构造的逃逸。个人使用无风险，不建议暴露给不受信任的用户
- **休眠期 `sleep_mute_replies=true` 时 AstrBot 对所有消息都不回复**——深夜急事请关闭该配置或使用 `/living_wake`
- **prompt-preset 插件启用时 AstrBot 原生 system_prompt 被覆盖**——安全模式提示等内置内容需要通过 `{{native_system}}` 条目显式保留

## 开发

```bash
# 运行测试
python -m pytest tests/ -q

# 项目结构
core/          # 核心模块（不依赖 astrbot，可独立测试）
  assembler.py     # 组装引擎
  living_loop.py   # 主循环
  living_state.py  # 状态闸门
  mood.py          # 心境状态机
  decider.py       # 决策层
  activities.py    # 活动池
  sleep.py         # 休眠管理
  agent_loop.py    # agent 循环（token 硬闸）
  llm_failover.py  # 模型故障转移链
main.py        # 插件入口
tests/         # 测试套件
```

## License

[MIT](LICENSE)