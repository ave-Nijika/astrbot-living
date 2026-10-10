# astrbot_plugin_living

让 AstrBot 在无消息时拥有自己的生活——闲时由心境与记忆驱动的冲动，自主冲浪、读文章、现场写小游戏、整理回忆，还会主动找你说话。

[![version](https://img.shields.io/badge/version-1.0.0-blue)](CHANGELOG.md)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![AstrBot](https://img.shields.io/badge/AstrBot-v4.27.5+-orange.svg)](https://github.com/AstrBotDevs/AstrBot)
[![Python](https://img.shields.io/badge/Python-3.10+-blue.svg)](https://www.python.org)

## 这是什么

大多数聊天机器人收到消息才回复，没消息时就是死的。这个插件让你的 AstrBot 在你没找它的时候也"活着"——它自己决定要不要搜点什么看看、写个小游戏玩玩、翻翻以前的记忆，把经历写进长期记忆。下次你聊到相关话题，它真的"经历过"。

**它不是定时推送器**——每次活动都是它自己的"冲动"，由当前状态 + 记忆 + 人设共同决定。它也不会一直找你说话：安静是它的正常状态，不是故障。

## 快速开始

1. **安装**：AstrBot WebUI → 插件管理 → 从仓库安装，填入本仓库地址；或手动克隆到 `data/plugins/astrbot_plugin_living/`，重启 AstrBot
2. **最小配置**：什么都不用改，默认值即可跑起来。建议只做一件事——在面板"新手"页给它配一个**独立的模型 provider**（原因见[模型配置与缓存保护](#模型配置与缓存保护)）
3. **看到它开始活动**：默认每 5 分钟醒一次做判定（「安静/正常/活泼」新手档会把间隔调成 90 / 45 / 20 分钟），但每日活动上限默认只 3 次，所以它不会真的每次都动手。发 `/living` 看它现在的状态；过一阵它的主动消息会出现在你配置的目标会话里
4. **想调它**：插件详情页 → Pages → 「living 配置面板」。新手页是常用开关，专家页是全部细项；改完记得「💾 保存改动」（热生效，不用重启）。右上角「📖 说明书」是给普通用户的内置手册

可选搭配：[astrbot_plugin_livingmemory](https://github.com/lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory)（长期记忆 + 知识图谱，不装则降级内置 SQLite 后端）；Playwright 的 Chromium 内核（真实浏览器能力，见[浏览器能力（可选安装）](#浏览器能力可选安装)）。

## 它怎么运作

### 自主活动（心跳驱动）

插件按配置的间隔反复醒来检查一次（"心跳"）：睡醒了没、今天额度用了多少、当前状态想不想动。决定动，就从下面几样里挑一件（选择受兴趣与心境影响）：

| 活动 | 做什么 |
|---|---|
| 冲浪（surf） | 挑个感兴趣的主题搜一搜，看看标题 |
| 读文章（read） | 搜到结果后真的点进去读正文 |
| 写游戏（game） | 自己写 Python 代码丢进沙箱跑起来玩 |
| 翻回忆（reminisce） | 翻以前的记忆，像人翻旧相册 |
| 看留言（peek） | 回头看一眼"上次主动说的话对方回了没有"，写成经历；只看，不发消息 |

另有"自由发挥"（free）开关：打开后它偶尔完全自己决定干什么（LLM 拿着当前可用工具现场想）。活动可以升级为完整 agent 循环——模型带着工具集（搜索/抓取/沙箱/记忆/浏览器）自己决定怎么做，带单次 token 硬闸（`decision.single_run_token_budget`）。

### 心境（六维）

- **valence**（-1~1）：心情正负，活动成功 +
- **arousal**（0~1）：情绪唤醒度
- **energy**（0~1）：精力，活动消耗、睡眠恢复
- **fatigue**：疲惫，随清醒时间积累
- **sleep_debt**：睡眠债，被吵醒会累积
- **interests**：主题兴趣表，随活动累积、每日衰减

心境影响活动选择倾向与记忆重要度（低谷时的小确幸记得更牢）。面板里前五项数值只读（由经历自然涨落，不做编辑入口），兴趣表可管理。

### 睡眠与作息

- **自主作息**：没有固定的睡觉时间窗——睡意按清醒时长、昼夜节律、起床约定等自然积累，攒够了就睡；一觉时长、白天小睡都由状态涌现
- **昼夜节律时段**（`sleep.circadian_hint`，默认 23:00-07:00）：这个时段它天然更困、睡意攒得更快。一旦真睡下，`sleep.sleep_mute_replies`（默认开）会拦截消息不回复
- **吵醒**：休眠中短时间连发多条消息会把它吵醒（阈值可随机浮动，睡得深要多几条），有起床气概率；醒后 30 分钟清醒待机（期间每条消息刷新待机）
- **唤醒确认**：被吵醒立即回一条预置消息，不等模型
- **梦**：睡醒后低概率说一段"梦话"，写进记忆
- **起床约定**：睡前说"明早 7 点叫我"这类话，它会记着按时醒，睡过头会交代

### 主动出口（七条）

它会主动找你说话的路一共七条，面板顶部状态行逐条显示现状：

| 出口 | 触发 | 受"每日上限/最小间隔"管 |
|---|---|---|
| 分享 | 活动完觉得有意思，改写成它的口吻说给你听 | 是 |
| 搭话 | 隔一阵主动开个头 | 是 |
| 梦话 | 睡醒后低概率 | 是 |
| 睡过头交代 | 睡过头了交代一声（概率） | 是 |
| 晚安 | 睡前道晚安（按概率 / 模型自己斟酌 / 关） | 否（由晚安档位管） |
| 唤醒确认 | 被吵醒立即应一声 | 否（即时一条） |
| 醒来补回复 | 睡觉期间你发的消息，醒来补上 | 否 |

前四条走输出闸门（`output_gate.daily_message_limit` 每日上限 + `message_min_interval_minutes` 最小间隔），后三条直发。

### 记忆

所有活动经历以第一人称、带日期感写入记忆库；推荐装 LivingMemory 联动（长期记忆 + 知识图谱 + 混合检索），未装时自动降级内置 SQLite 后端。它说话、做梦、分享的每句成功发出的内容也会落库，保证"发出 == 可回读"。

## 能力档位与写权限

两个旋钮决定它的"手脚"有多大（新手页可直接调）：

**能力档位**（`autonomy.tier`，共 5 档）：

| 档 | 名字 | 能碰到什么 |
|---|---|---|
| 0 | 静养 | 只思考不动手 |
| 1 | 观看（watch，默认） | 上网浏览、搜索；装了浏览器内核另有五件浏览器工具 |
| 2 | 居家（home） | + 在自己的专属工作区里读写文件、写小程序 |
| 3 | 自由（full） | 文件能力的顶格（同 2，**不含命令行**） |
| 4 | 命令行（shell） | + 在这台电脑上执行命令——**等于把本机命令行交给它**，确认信任后再开 |

老配置的 tier=3 升级后会自动失去命令行（第 4 档独占），启动日志会说明。

**写权限**（`autonomy.write_level`，0-3 整数，面板档位标签为 read / browse / comment / full）：只管它在网络上的动作，**不影响本机文件读写与命令行**（那是能力档位的事）。管它在网上动不动手——0（read）只看 → 1（browse）能点链接/翻页/填表单但不提交 → 2（comment）可点赞/评论/提交表单 → 3（full）发帖/私信/下单都行。越权动作按白名单保守拒绝。

## 风格学习

让它说话更像某个人/某种风格（`style_learning.enabled` 总开关）：

- **四层库**：素材库（你从面板投喂的原文）→ 语料库（提炼出的六维风格片段）→ 沉淀（反复被取用的高分片段归纳合并）→ 调用记录（何时取用了哪条）
- **怎么用**：面板「语料与素材」卡把原文整段丢进素材库；它下次活动结束自动消化（或点「立即处理」），从网页读到合适语料时也会自动学习
- **每日复盘**（`style_learning.daily_review_enabled`，默认凌晨 4 点）：用判断模型回看最近哪些语料好用，调整留存度与重要度——**需要配置 `judge.provider_id`**，不配置则复盘自动跳过
- **前提**：提炼和复盘都是 LLM 调用，需要能用的模型（建议独立 provider）；从网页学语料需要它能读到网页

## 判断模型（小大脑）

给 AstrBot 配一个独立的"审查模型"，让它主动说话更稳（`judge.mode` 三档）：

- **off**（默认）：整条不启动，零开销
- **local**：本地推理，预留档，明确未实现、不会静默降级
- **api**：走 `judge.provider_id` 独立调用
  - **输入判断**：它主动开口前自查一次（说得好不好、像不像它平时的样子），结论只提醒不指挥
  - **输出检查**：聊天回复与它主动说的话（分享/搭话/晚安/梦话）说出口前后再查一遍，`judge.output_action=rewrite` 时允许打回重写一次（带长度护栏，失败放行原文）
  - **参考人设**（`judge.include_persona`，默认关）：把关时带上角色设定原文，判断更贴"平时的它"；人格作为参考资料进判断上下文、带防串角锚定，代价是每次判断多一段人格体量的输入
  - 每次判断记录可在面板新手卡回看（自主产出质检的来源标为 share / initiative / farewell / dream）

**注意**：`judge.provider_id` 必须与聊天模型分开配置；**不配置 = 相关功能整条不工作**（不是降级），这本身就是花费保护。

## 模型配置与缓存保护

**强烈建议在 `model.provider_id` 给 living 配一个独立的 provider（最好用不同的 api key / 账号）**。原因：大模型服务的对话缓存按账号维度存，一套前缀一旦被不同内容打断，原链路再请求就全部按未命中重新计费。living 独处时有九类 LLM 调用（活动、选题、搭话、分享改写、晚安、梦话、醒来补回复、约定提取、风格提炼），全与聊天共用账号的话，每调用一次就冲掉一次你聊天的缓存。

- `model.fallback_chain`：手动备用链，404/429/超时自动按序切换（401 不重试，换模型也没用）
- `model.allow_chat_fallback`（默认开）：链全失败后是否还回退聊天模型；**关了 = 宁可这次活动失败，也不冲聊天缓存**
- `model.prefix_cache_ttl_minutes`（默认 360）：如果你选择共用模型，living 自动"前缀对齐"——带上与你聊天完全相同的 system 与会话历史，命中同一套缓存不再冲缓存；这是缓解不是根治，时效过期后仍可能重算
- `judge.provider_id` 同理必须独立

## 命令速查

| 命令 | 作用 | 示例 |
|---|---|---|
| `/living` | 简要状态 | `/living` |
| `/living status` | 详细状态 | `/living status` |
| `/living mood` | 心境数值与兴趣 | `/living mood` |
| `/living memories [n]` | 最近 n 条记忆 | `/living memories 10` |
| `/living pause` / `resume` | 暂停 / 恢复自主活动 | `/living pause` |
| `/living wake` | 触发一次判定（仍受休眠/额度约束） | `/living wake` |
| `/living sleep` | 手动让它入睡 | `/living sleep` |
| `/living do <activity> [topic]` | 强制执行一个活动（额度内） | `/living do surf 科技新闻` |
| `/living config <key> <value>` | 改配置（写入并回读验证后生效） | `/living config sleep.dream_probability 0.3` |
| `/living debug` | 判定链与 token 统计 | `/living debug` |
| `/living help` | 子命令帮助 | `/living help` |
| `/living_wake` | 手动唤醒判定 | `/living_wake` |
| `/living_wake_now` | **紧急唤醒**：立刻终止本次休眠 | `/living_wake_now` |

## 配置说明

全部配置在 WebUI 热生效（改了不用重启）。完整列表见 `_conf_schema.json`；新手页 9 个旋钮会自动映射到底层键（映射表在 `core/config_knobs.py`）。速览：

| 配置 | 说明 | 默认值 |
|---|---|---|
| `decision.decision_mode` | 决策模式：rules / hybrid / llm | hybrid |
| `decision.impulse_check_interval_minutes` | 心跳间隔（分钟） | 5 |
| `decision.daily_impulse_limit` | 每日活动上限（0 = 不限） | 3 |
| `decision.single_run_token_budget` | 单次活动 token 硬闸 | 20000 |
| `output_gate.daily_message_limit` | 每日主动消息上限 | 10 |
| `output_gate.message_min_interval_minutes` | 两条主动消息最小间隔（分钟） | 30 |
| `sleep.circadian_hint` | 昼夜节律时段 | 23:00-07:00 |
| `sleep.sleep_mute_replies` | 休眠期拦截消息 | true |
| `sleep.standby_blocks_sleep` | 聊天中不入睡 | true |
| `sleep.wake_source` | 吵醒计数来源：all / owner_only | all |
| `autonomy.tier` | 能力档位 0-4 | 1（观看） |
| `autonomy.write_level` | 网络写权限 0-3（0=只看，见上） | 0（只看） |
| `capabilities.web_search_enabled` | 允许联网搜索（关 → 冲浪/读文章改用浏览器直接逛；没装浏览器内核才停） | true |
| `capabilities.agent_tools_mode` | 复用本体工具：off / persona / custom | off |
| `style_learning.enabled` | 风格学习总开关 | false |
| `style_learning.daily_review_enabled` | 每日复盘（需判断模型） | true |
| `judge.mode` | 判断模型档位：off / local / api | off |
| `judge.provider_id` | 判断模型 provider（不配 = 不工作） | 空 |
| `judge.include_persona` | 判断时参考角色设定（多花一段人格体量输入） | false |
| `model.provider_id` | 自主活动专用 provider（留空用默认） | 空 |
| `model.allow_chat_fallback` | 失败时回退聊天模型 | true |
| `memory.backend` | 记忆后端：auto / livingmemory / simple | auto |

## 浏览器能力（可选安装）

"自由上网"分两层：搜索/读文本开箱即用；**真实浏览器（打开网页、看画面、点页面）依赖 Playwright 的 Chromium 内核**，出于体积不随插件内置。

- **安装**：在 AstrBot 的 Python 运行环境里执行 `playwright install chromium`，装完重启 AstrBot（或重载本插件）
- **自定义位置**：内核装在非默认目录时，设置机器级环境变量 `PLAYWRIGHT_BROWSERS_PATH` 指向该目录——插件经 Playwright 自身的路径解析做探测，尊重该变量
- **装好后**：能力档位 ≥1 时多出五件浏览器工具——打开网页、读页面、截图存档、点击元素、填写输入框。截图会真正进入它的"眼睛"：活动模型支持图片输入时直接看画面（不支持时可配转述模型代看）。登录态保存在工作区（`browser_state.json`），下次接着用
- **不装**：五件工具不挂载（fail-closed，探测不到绝不挂），自主活动自动回落到"搜索 + 读文本"形态——照样冲浪读文章，其余能力（游戏/记忆/搜索）完全不受影响
- **卸载**：`playwright uninstall chromium`，或直接删除 Playwright 缓存目录（Windows `%LOCALAPPDATA%\ms-playwright`，Linux/macOS `~/.cache/ms-playwright`）
- **怎么看装没装**：面板新手页「浏览器能力」卡实时显示

## 工作区

AstrBot 的专属文件夹（`autonomy.workspace_dir`，默认 `<AstrBot数据目录>/data/plugin_data/astrbot_plugin_living_home`）：档位 2+ 的文件读写、小游戏产物、浏览器截图与登录态都放在这里；第 4 档命令行的默认工作目录也是它。

- **启动自愈**：目录不存在会自动创建（幂等，绝不覆盖既有内容）；配置路径被文件占用则明确报错、不静默换位置
- **面板状态**：新手页实时显示工作区目录是否就绪（ready / missing / failed 及原因）

## 面板说明

插件详情页 → Pages → 「living 配置面板」。零依赖纯前端，跑在 AstrBot Pages 沙箱内：

- **新手 / 专家双视图**：新手页 9 个常用旋钮 + 功能卡；专家页按栏目树渲染全部键，默认值即保守安全值，危险项有 ⚠ 标记
- **全局状态行**：顶部实时显示七条主动出口各自的现状
- **四态状态点**：`● 开` 在生效 / `● 关` 显式关闭 / `▲ 卡着` 开了但缺前置 / `● 哑` 被上游连带关闭——改值即实时重算，不用保存
- **生效链**：带前置条件的键/组旁有「生效链 ▸」按钮，展开看"要哪几个条件都满足才真的起作用"
- **级联置灰**：被上游关闭的键会置灰并说明因果
- **语料与素材**：风格学习的素材投喂、语料查看编辑、立即处理都在面板里
- **📖 说明书**：右上角按钮，弹出的内置手册面向普通用户（有什么功能 / 怎么用 / 开了会怎样 / 出问题怎么排查）

## 已知限制

- **沙箱隔离是启发式的**（"防呆不防黑客"）——静态 import 白名单 + 超时 + `-I` 隔离模式，挡不住故意构造的逃逸。个人使用无风险，不建议把第 4 档（命令行）开在不受信任的环境
- **休眠期 `sleep_mute_replies=true` 时 AstrBot 对所有消息都不回复**——深夜急事请关闭该配置或使用 `/living_wake_now`
- **prompt-preset 插件启用时 AstrBot 原生 system_prompt 被覆盖**——安全模式提示等内置内容需要通过 `{{native_system}}` 条目显式保留
- **看留言（peek）只读不写**——看完不会因此发消息，主动发言永远走七条出口的闸门
- 浏览器内核约 150MB，需手动安装（见上节）

## 开发

```bash
# 运行测试
python -m pytest tests/ -q

# 项目结构
core/            # 核心模块（不依赖 astrbot 的部分可独立测试）
  assembler.py     # 组装引擎
  living_loop.py   # 主循环
  living_state.py  # 状态闸门
  mood.py          # 心境状态机
  decider.py       # 决策层
  activities.py    # 活动池
  sleep.py         # 休眠管理
  agent_loop.py    # agent 循环（token 硬闸）
  llm_failover.py  # 模型故障转移链
  judge.py         # 判断模型（小大脑）
  style_learning.py / style_review.py  # 风格学习与每日复盘
  autonomy.py      # 能力档位与写权限
  living_tools.py  # 工具装配
  panel_api.py     # 面板 API 纯逻辑层
main.py          # 插件入口（命令、事件挂接、Web API 注册）
pages/config/    # 配置面板（纯原生 JS ES module，零构建）
tests/           # 测试套件
scripts/panel_dev_server.py  # 面板浏览器实测用 mock 服务器
```

## 版本与更新日志

版本号遵循语义化版本，唯一事实源是 `metadata.yaml`（`panel_layout.json` 里的 `version` 是面板布局结构版本，属另一语义）。全部变更见
[CHANGELOG.md](CHANGELOG.md)——当前版本 **1.0.0**（2026-10-08）。

## License

[MIT](LICENSE)
