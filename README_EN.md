# astrbot_plugin_living

Give your AstrBot a life of its own — autonomous web surfing, article reading, self-coded mini-games, and memory journaling during idle time, driven by its own state and long-term memory. It will even reach out to you on its own.

> **Note**: the English version may lag behind the Chinese one — when in doubt, the Chinese README is authoritative.

[![version](https://img.shields.io/badge/version-1.0.0-blue)](CHANGELOG.md)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![AstrBot](https://img.shields.io/badge/AstrBot-v4.27.5+-orange.svg)](https://github.com/AstrBotDevs/AstrBot)
[![Python](https://img.shields.io/badge/Python-3.10+-blue.svg)](https://www.python.org)

## What is this

Most chatbots only respond when messaged. This plugin gives your AstrBot something to do when you're not around — it autonomously decides whether to search for something interesting, write a mini-game to play, or revisit old memories, then journals the experience into long-term memory.

**It is not a scheduled pusher** — every activity is driven by the bot's own "impulse", determined by its current state + memories + persona. It won't keep pinging you either: silence is a normal state, not a bug.

## Quick start

1. **Install**: AstrBot WebUI → Plugin Management → Install from repo, fill in this repo URL; or clone into `data/plugins/astrbot_plugin_living/` and restart AstrBot
2. **Minimal config**: defaults work out of the box. One thing is strongly recommended — give it a **dedicated model provider** in the panel's novice view (why: see [Model config & cache protection](#model-config--cache-protection))
3. **See it move**: by default it wakes every 5 minutes to check whether to do something (the "quiet/normal/active" activity level maps the interval to 90 / 45 / 20 minutes), but the daily activity cap is 3 by default — so it won't actually act on every heartbeat. Send `/living` to check its status; its proactive messages will appear in your configured target sessions
4. **Tune it**: plugin details → Pages → the living config panel. Novice view has the common knobs; expert view has everything. Remember to hit "Save" — changes are hot-reloaded, no restart needed. The "📖 Help" button opens a built-in user manual

Optional: [astrbot_plugin_livingmemory](https://github.com/lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory) for long-term memory + knowledge graph (falls back to a built-in SQLite backend without it); Playwright's Chromium for real browser capabilities (see [Browser capabilities](#browser-capabilities)).

## How it works

### Autonomous activities (heartbeat-driven)

On each heartbeat it checks: am I awake, how much quota is left, do I feel like doing anything. If yes, it picks one:

| Activity | What it does |
|---|---|
| Surf (surf) | Pick a topic of interest and search for it |
| Read (read) | Actually open a search result and read the article |
| Mini-game (game) | Write Python code and run it in its sandbox |
| Reminisce (reminisce) | Browse its own old memories |
| Peek (peek) | Look back at "did they reply to my last proactive message" and journal it; read-only, never sends |

Plus a "free play" switch (free): when on, it occasionally decides entirely by itself what to do. Activities can run as a full agent loop — the model drives tools (search/fetch/sandbox/memory/browser) on its own, under a per-run token hard cap (`decision.single_run_token_budget`).

### Mood (six dimensions)

valence (mood), arousal, energy, fatigue, sleep_debt, and an interests table. Mood biases activity choice and memory importance. The first five are read-only in the panel; interests are manageable.

### Sleep & daily rhythm

No fixed sleep window — sleepiness accumulates naturally (awake time, circadian hints, wake-up appointments) and it sleeps when it's enough. A late-night stretch (`sleep.circadian_hint`, default 23:00-07:00) is when it naturally feels sleepier and sleepiness builds up faster. Once it actually falls asleep, `sleep.sleep_mute_replies` (on by default) intercepts messages without replying. Receiving several messages in a short span can wake it up (threshold randomly varies with sleep depth); after waking it stays talkative for 30 minutes. It may send a wake acknowledgment instantly, mutter a "dream" after waking, and honors "wake me at 7" style appointments — oversleeping gets an apology.

### Proactive outputs (seven exits)

| Exit | Trigger | Subject to daily limit / min interval |
|---|---|---|
| Share | Wants to tell you about an activity | Yes |
| Initiative | Starts a conversation on its own | Yes |
| Dream | Random on waking | Yes |
| Oversleep note | Apologizes for oversleeping (probability) | Yes |
| Goodnight | Before sleep (probability / model-decided / off) | No |
| Wake ack | Instant reply when woken | No |
| Pending-reply catch-up | Replies to messages received while asleep | No |

The first four pass the output gate (`output_gate.daily_message_limit` + `message_min_interval_minutes`); the last three send directly. On top of that, a conversation-avoid window: if you have spoken in its target session recently, it holds proactive chatter and shares for `initiative.avoid_after_user_minutes` (default 30, 0=off) — no interrupting an ongoing chat; activities still run and experiences are still recorded, it just stays quiet.

### Memory

Experiences are journaled in first person with a sense of date. LivingMemory integration is recommended (long-term memory + knowledge graph + hybrid retrieval); without it the plugin falls back to a built-in SQLite backend. Every message it actually sends is also journaled.

## Capability tiers & write access

Two knobs decide how far its hands reach (both on the novice view):

**Capability tier** (`autonomy.tier`, 0-4):

| Tier | Name | What it can touch |
|---|---|---|
| 0 | Rest | Think only |
| 1 | Watch (default) | Web browsing & search; five browser tools if Chromium is installed |
| 2 | Home | + read/write files and run mini-programs in its own workspace |
| 3 | Full | Top of file abilities (same as 2, **no shell**) |
| 4 | Shell | + execute commands on this machine — **local shell access**; enable only if you trust it |

**Write access** (`autonomy.write_level`, integer 0-3 — the panel labels them read / browse / comment / full) governs **online actions only** — it does not affect local file writes or the shell (those follow the capability tier): 0 (read, look only) → 1 (browse, click/paginate/fill forms, no submit) → 2 (comment, like/comment/submit forms) → 3 (full, post/DM/order). Out-of-whitelist actions are conservatively rejected.

## Style learning

Make it sound like a given person/style (`style_learning.enabled`):

- **Four layers**: materials (raw text you feed from the panel) → corpus (six-dimension style fragments) → distillation (high-score, often-used fragments merged) → usage log
- **Usage**: paste raw text into the "Materials" card; it digests after its next activity (or click "Process now"). It can also learn from web articles it reads
- **Daily review** (`style_learning.daily_review_enabled`, default 04:00): the judge model re-scores recent corpus — requires `judge.provider_id`, skipped automatically without it
- **Prerequisites**: LLM access for distillation/review (dedicated provider recommended); web reading for corpus harvesting

## Judge model ("little brain")

An independent reviewer model for its proactive messages (`judge.mode`): **off** (default, zero cost) / **local** (reserved, unimplemented) / **api** (uses `judge.provider_id`). Input-side checks before it speaks (advisory only); output-side checks cover both chat replies and its proactive messages (shares / initiative / goodnight / dream talk / oversleep note / wake-up catch-up reply), with three levels (`judge.output_action`): `log_only` (default) records only; `negotiate` holds the reply, the judge only offers an opinion, and the chat model itself decides — accept and rewrite its own reply, or reject with a reason and the original passes through (the judge can never override the chat model; the whole exchange is invisible to outsiders, with a total timeout that releases the current version); `rewrite` (not recommended) = small model ghostwrites the replacement. Negotiation only applies to chat replies; its proactive messages are record-only at every level. Optionally reference the persona during review (`judge.include_persona`, default off — persona text enters the judge context as quoted reference material with anti-roleplay anchors, at the cost of a persona-sized input per call). Every judgment is viewable in the panel (proactive QC sources tagged share / initiative / farewell / dream / oversleep / pending_reply).

`judge.provider_id` must be separate from your chat model; **without it these features simply don't run** (no silent fallback — that is the cost protection).

## Model config & cache protection

**Strongly recommend a dedicated provider for living in `model.provider_id` (ideally a different account/api key).** LLM providers cache conversations per account: once a different prefix hits the same account, the old chain is re-billed as cache misses. Living makes ~9 kinds of LLM calls while idle (activities, topic choice, initiative, share rewriting, goodnight, dreams, wake replies, appointments, style distillation) — sharing your chat account means each of them flushes your chat cache.

- `model.fallback_chain`: manual fallback list; 404/429/timeouts switch automatically (401 doesn't retry)
- `model.allow_chat_fallback` (default on): whether to fall back to the chat model after the chain fails; **off = rather fail this activity than flush your chat cache**
- `model.prefix_cache_ttl_minutes` (default 360): if you do share a model, living aligns its prompts to your chat prefix to reuse the same cache — a mitigation, not a cure

## Command reference

| Command | Effect | Example |
|---|---|---|
| `/living` | Brief status | `/living` |
| `/living status` | Detailed status | `/living status` |
| `/living mood` | Mood values & interests | `/living mood` |
| `/living memories [n]` | Recent n memories | `/living memories 10` |
| `/living pause` / `resume` | Pause / resume autonomy | `/living pause` |
| `/living wake` | Trigger one heartbeat decision | `/living wake` |
| `/living sleep` | Put it to sleep manually | `/living sleep` |
| `/living do <activity> [topic]` | Force an activity (within quota) | `/living do surf tech news` |
| `/living config <key> <value>` | Change config (write + verify) | `/living config sleep.dream_probability 0.3` |
| `/living debug` | Decision chain & token stats | `/living debug` |
| `/living help` | Sub-command help | `/living help` |
| `/living_wake` | Manual wake decision | `/living_wake` |
| `/living_wake_now` | **Emergency wake**: end current sleep now | `/living_wake_now` |

## Configuration

Everything is hot-reloadable from the WebUI. Full list in `_conf_schema.json`; the 9 novice knobs map onto the underlying keys via `core/config_knobs.py`. Quick overview:

| Key | Meaning | Default |
|---|---|---|
| `decision.decision_mode` | rules / hybrid / llm | hybrid |
| `decision.impulse_check_interval_minutes` | Heartbeat interval (minutes) | 5 |
| `decision.daily_impulse_limit` | Daily activity cap (0 = unlimited) | 3 |
| `decision.single_run_token_budget` | Per-activity token cap | 20000 |
| `output_gate.daily_message_limit` | Daily proactive message cap | 10 |
| `sleep.circadian_hint` | Circadian (late-night) stretch | 23:00-07:00 |
| `sleep.sleep_mute_replies` | Mute replies while asleep | true |
| `autonomy.tier` | Capability tier 0-4 | 1 (watch) |
| `autonomy.write_level` | Network write access, 0-3 (0 = look only) | 0 (look only) |
| `capabilities.web_search_enabled` | Web search on/off (off → surf/read browse with the built-in browser instead; dropped only if no browser kernel is installed) | true |
| `style_learning.enabled` | Style learning on/off | false |
| `judge.mode` | Judge model: off / local / api | off |
| `judge.provider_id` | Judge provider (unset = disabled) | empty |
| `judge.include_persona` | Reference the persona during review (extra persona-sized input per call) | false |
| `initiative.avoid_after_user_minutes` | How long it stays quiet after you speak before initiating (minutes, 0=off) | 30 |
| `judge.output_action` | Output check level: log_only / negotiate / rewrite | log_only |
| `judge.negotiate_timeout_seconds` | Negotiation total timeout (seconds; releases current version on expiry) | 20 |
| `judge.negotiate_recheck` | Re-check the rewritten reply in negotiate mode (stricter, slower) | false |
| `model.provider_id` | Dedicated autonomy provider | empty |
| `model.allow_chat_fallback` | Fall back to chat model | true |
| `memory.backend` | auto / livingmemory / simple | auto |

## Browser capabilities

Search/read-text work out of the box; a **real browser (open pages, see the screen, click) needs Playwright's Chromium**, not bundled for size.

- **Install**: run `playwright install chromium` in AstrBot's Python environment, then restart AstrBot (or reload the plugin)
- **Custom location**: if the browser lives in a non-default directory, point the machine-level `PLAYWRIGHT_BROWSERS_PATH` environment variable at it — detection uses Playwright's own path resolution and honors it
- **Once installed**: five browser tools mount (open/read/screenshot/click/type) at tier ≥ 1. Screenshots go to its "eyes": shown directly if the activity model accepts images, otherwise a captioner model can be configured. Login state persists in the workspace (`browser_state.json`)
- **Without it**: the five tools never mount (fail-closed); autonomy degrades to "search + read text", everything else unaffected
- **Uninstall**: `playwright uninstall chromium`, or delete Playwright's cache dir (Windows `%LOCALAPPDATA%\ms-playwright`, Linux/macOS `~/.cache/ms-playwright`)

## Workspace

AstrBot's own folder (`autonomy.workspace_dir`, default `<AstrBot data dir>/data/plugin_data/astrbot_plugin_living_home`): file reads/writes at tier 2+, mini-game artifacts, browser screenshots and login state; also the default working directory for the shell tier.

- **Startup self-healing**: the folder is created automatically if missing (idempotent, never overwrites); a mis-configured path occupied by a file is a loud error, not a silent fallback
- **Panel status**: the novice view shows live workspace state (ready / missing / failed, with reasons)

## WebUI panel

Plugin details → Pages → the living config panel. Dependency-free vanilla JS ES modules inside the AstrBot Pages sandbox:

- **Novice / expert dual view**: novice = 9 common knobs + feature cards; expert = every key in a section tree with conservative defaults and ⚠ danger marks
- **Global status line**: live state of all seven proactive exits
- **Four-state dots**: ● active / ● off / ▲ blocked (missing prerequisite) / ● dead (cascaded off) — recomputed as you edit, no save needed
- **Effective chain**: the "chain ▸" button next to gated keys/groups shows exactly which conditions must hold
- **Cascade dimming**: keys disabled by upstream are dimmed with the reason
- **Corpus & materials**: feed materials, view/edit corpus, process now
- **📖 Help**: a built-in user manual (what it does / how to use / what to expect / troubleshooting)

## Known limitations

- **Sandbox isolation is heuristic** ("prevents accidents, not hackers") — static import whitelist + timeout + `-I` isolated mode; don't enable the shell tier in untrusted environments
- **With `sleep_mute_replies=true`, AstrBot replies to NOTHING during the sleep window** — turn it off or use `/living_wake_now` for emergencies
- **prompt-preset overrides AstrBot's native system_prompt** — keep built-in content via a `{{native_system}}` entry
- **Peek is read-only** — it never sends because of peeking; proactive speech always goes through the seven exits' gates
- Chromium is ~150MB and installed manually (see above)

## Development

```bash
# Run tests
python -m pytest tests/ -q
```

`core/` holds the core modules (loop, mood, activities, sleep, agent loop, failover, judge, style learning, autonomy, tools, panel API); `main.py` is the plugin entry (commands, event hooks, web API); `pages/config/` is the dependency-free config panel; `tests/` the suite; `scripts/panel_dev_server.py` a mock server for browser-testing the panel.

## Versioning & changelog

The single source of truth for the version is `metadata.yaml` (the `version` in `panel_layout.json` is the panel layout schema version — a different thing). See [CHANGELOG.md](CHANGELOG.md) — current version **1.0.0** (2026-10-08).

## License

[MIT](LICENSE)
