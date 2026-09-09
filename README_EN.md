# astrbot_plugin_living

Give your AstrBot a life of its own — autonomous web surfing, article reading, self-coded mini-games, and memory journaling during idle time, driven by a mood state machine and long-term memory.

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![AstrBot](https://img.shields.io/badge/AstrBot-v4.27.5+-orange.svg)](https://github.com/AstrBotDevs/AstrBot)

## What is this

Most chatbots only respond when messaged. This plugin gives your AstrBot something to do when you're not around — it autonomously decides whether to search for something interesting, write a mini-game to play, or revisit old memories, then journals the experience into long-term memory.

**It is not a scheduled pusher** — every activity is driven by the bot's own "impulse", determined by a mood state machine + recent memories + persona. Silence is also a valid state, not a bug.

## Features

- **Mood state machine** (valence/arousal/energy/interests) — activities affect mood, mood affects future choices
- **Autonomous activities** (surf/read/game/reminisce) — agent loop with LLM-written code in sandbox
- **Sleep system** — sleep window, fatigue, sleep debt, wake-by-spam (A+B), wake acknowledgment, farewell on sleep return
- **Decision modes** — rules (zero cost) / hybrid / llm (full LLM decision)
- **Token hard cap** — agent loop budget enforced per activity
- **Model failover chain** — automatic provider switching on 404/429/timeout
- **LivingMemory integration** — memories stored with session context for graph extraction

## Installation

Requires AstrBot v4.27.5+. Optional: [astrbot_plugin_livingmemory](https://github.com/lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory) for knowledge graph support.

## Configuration

All settings are hot-reloadable via WebUI. See `_conf_schema.json` for full list.

## Security notes

- Sandbox isolation is heuristic ("prevents accidents, not hackers") — static import whitelist + timeout + isolated mode
- When `sleep_mute_replies=true`, AstrBot will not reply to ANY message during sleep window — use `/living_wake` or disable for emergencies

## License

[MIT](LICENSE)