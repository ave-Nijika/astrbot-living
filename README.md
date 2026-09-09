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