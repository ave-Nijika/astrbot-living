/* astrbot_plugin_living —— 配置面板逻辑（M5 补丁 1）
 * 运行在 AstrBot Plugin Pages 受限 iframe 中，通过 window.AstrBotPluginPage
 * bridge 调用后端（自动携带 dashboard 鉴权）。bridge 仅提供 GET/POST。
 * 注意：Pages 沙箱忽略 window.confirm/alert/prompt——确认动作用页内弹层。 */

const KNOB_ORDER = [
  // M6-补丁1：preset_sleep_style 随 sleep_mode 配置键移除
  "preset_activity_level", "preset_talk_frequency",
  "preset_capability_tier", "preset_write_level", "preset_topic_taste",
  "preset_free_activity", "preset_decision_mode", "preset_model",
];

const GROUP_LABELS = {
  autonomy: "能力档位", decision: "决策", output_gate: "输出闸门",
  initiative: "主动搭话", sleep: "休眠", style_learning: "风格学习",
  capabilities: "能力参数", memory: "记忆", model: "模型", judge: "判断模型",
};

/* M19-补丁1 F1：枚举字段的中文标签（显示标签、保存原始值）。
 * 选项本体来自 schema 的 options（F4：有 options 一律渲染下拉）；
 * 这里只管"值 → 看得懂的中文"。 */
const OPTION_LABELS = {
  "decision.decision_mode": { rules: "规则", hybrid: "混合", llm: "大模型" },
  "sleep.farewell_mode": {
    probability: "投骰子", llm: "她自己斟酌", off: "不说",
  },
  "sleep.wake_source": { all: "所有人", owner_only: "只算主人" },
  "capabilities.agent_tools_mode": {
    off: "关闭", persona: "跟随人格", custom: "自定义白名单",
  },
  "memory.backend": { auto: "自动", livingmemory: "记忆库", simple: "简易" },
  "judge.mode": { off: "关闭", local: "本地（未实现）", api: "云端 API" },
  "judge.output_action": { log_only: "只记录", rewrite: "允许打回重写" },
};

/* M23-补丁1 A5：能力档旋钮的中文按钮标签（存的值仍是 watch/home/full/shell）。
 * 第 4 档必须让人一眼看到"命令行"——给命令行这件事无处隐藏。 */
const KNOB_OPTION_LABELS = {
  preset_capability_tier: {
    watch: "看",
    home: "玩",
    full: "做（不含命令行）",
    shell: "命令行（本机权限）",
  },
};

/* M19-补丁1 F2：agent_activities 的多选选项（6 个活动 + 中文说明——
 * 主人原话"没有说明用户肯定不知道怎么写"）。 */
const AGENT_ACTIVITY_CHOICES = [
  { value: "surf", label: "surf 上网冲浪" },
  { value: "read", label: "read 读文章" },
  { value: "game", label: "game 写小游戏" },
  { value: "peek", label: "peek 看留言" },
  { value: "reminisce", label: "reminisce 翻记忆" },
  { value: "free", label: "free 自由活动" },
];

/* M19-补丁1 F3：source_weights 只有两个固定键——做成两个数字输入比
 * 裸 JSON 文本框友好（任务书 F3 建议）。 */
const SOURCE_WEIGHT_FIELDS = [
  { key: "dialogue", label: "实战对话（评论区/回复串）权重" },
  { key: "article", label: "单作者文章权重" },
];

/* M20-补丁1 F3：initiative.sources 的取值固定（两个来源）——改多选。 */
const INITIATIVE_SOURCE_CHOICES = [
  { value: "random_miss", label: "random_miss：没回她消息时，她可能会惦记" },
  { value: "open_topic", label: "open_topic：从最近的聊天内容里找话题" },
];

/* 任务书 2.2 的危险项清单（advanced 组内，带醒目警告）。
 * M18-补丁1 D1：补入 daily_impulse_limit——与 token 预算/工具轮数叠加时
 * 同样影响成本（0 = 不限制，叠加其他开关时可能放量）。 */
const DANGER_KEYS = new Set([
  "sleep.weights", "sleep.fatigue_rate_per_hour",
  "decision.recent_topic_penalty", "decision.single_run_token_budget",
  "decision.max_tool_rounds", "decision.max_run_seconds",
  "decision.daily_impulse_limit",
]);

/* M18-补丁1 D2：新手档位旋钮映射的目标键（与 core/config_knobs.py 的
 * KNOB_PRESETS + preset_model 直通一一对应）——专家区的手改会在新手区
 * 切换对应档位时被覆盖，专家组里标出"档位联动"提示。 */
const KNOB_MAPPED_KEYS = new Set([
  "decision.impulse_check_interval_minutes", "decision.activity_probability",
  "decision.daily_impulse_limit", "decision.interest_daily_decay",
  "decision.recent_topic_window", "decision.exploration_trigger",
  "decision.free_activity_enabled", "decision.decision_mode",
  "output_gate.daily_message_limit", "output_gate.message_min_interval_minutes",
  "autonomy.tier", "autonomy.write_level", "model.provider_id",
]);

/* Pages 沙箱（opaque origin）禁用 localStorage —— 直接访问会抛 SecurityError。
 * 必须全程 try/catch：否则模块顶层就抛错，整个面板脚本不执行（M5 补丁1 实测缺陷）。
 * 降级为内存态：抽屉展开状态仅存活于本次会话。 */
function loadExpanded() {
  try {
    return JSON.parse(localStorage.getItem("living-panel-expanded") || "{}");
  } catch (e) {
    return {};
  }
}

function saveExpanded(obj) {
  try {
    localStorage.setItem("living-panel-expanded", JSON.stringify(obj));
  } catch (e) {
    /* 沙箱禁用：忽略，仅本次会话有效 */
  }
}

const state = {
  values: { knobs: {}, advanced: {} }, // 当前编辑值（切换视图不丢）
  loaded: { knobs: {}, advanced: {} }, // 加载时的原始快照（保存时做差量）
  schema: null,
  dirty: false,
  expanded: loadExpanded(),
};

/* M20-补丁1 F1/A2/A4：provider 类字段的统一控件。
 * - 下拉选项从"已启用的 provider"动态生成（数据源 GET /config 的 providers）；
 * - 留空选项（当前=聊天模型）+ A2 缓存警告 + F1-b 手填兜底（填了不存在
 *   的 id 明确提示，不静默回退）；
 * - getter/setter 抽象：expert 的 model.provider_id 写 advanced，novice
 *   的 preset_model 写 knobs，同一控件两处复用。 */
function providerPickerControl({ value, onChange, emptyLabel }) {
  const wrap = document.createElement("div");
  wrap.className = "provider-picker";
  const providers = state.providers || [];
  const MANUAL = "__manual__";
  const isManual = !!value && !providers.includes(value);

  const select = document.createElement("select");
  select.className = "enum-select";
  const emptyOpt = document.createElement("option");
  emptyOpt.value = "";
  emptyOpt.textContent = emptyLabel || "（留空 = 与聊天共用模型）";
  select.appendChild(emptyOpt);
  for (const pid of providers) {
    const optEl = document.createElement("option");
    optEl.value = pid;
    optEl.textContent = pid;
    select.appendChild(optEl);
  }
  const manualOpt = document.createElement("option");
  manualOpt.value = MANUAL;
  manualOpt.textContent = "手动填写 provider id…";
  select.appendChild(manualOpt);
  select.value = isManual ? MANUAL : (value || "");

  const manualInput = document.createElement("input");
  manualInput.type = "text";
  manualInput.placeholder = "provider id（需与 provider 管理页里的 id 一致）";
  manualInput.value = isManual ? value : "";
  if (!isManual) manualInput.classList.add("hidden");

  const status = document.createElement("div");
  status.className = "control-status";
  const warn = document.createElement("div");
  warn.className = "control-warn";

  const refresh = () => {
    const current = select.value === MANUAL ? manualInput.value.trim() : select.value;
    if (!current) {
      status.textContent = "当前生效：聊天模型（共用账号）";
      warn.textContent = "⚠ 与聊天共用模型：她做活动/搭话/分享的任何一次调用都会打断你聊天的缓存，聊天全部历史将按未命中重新计费。建议单独配一个 provider（最好用不同的 api key）。";
    } else if (!providers.includes(current)) {
      status.textContent = `当前生效：${current}（不在已启用列表中）`;
      warn.textContent = "⚠ 该 provider id 不在已启用的 provider 列表里，调用时会按回退处理并在日志留痕。请核对拼写，或到 provider 管理页启用它。";
    } else {
      status.textContent = `当前生效：${current}（独立 provider）`;
      warn.textContent = "";
    }
  };

  select.addEventListener("change", () => {
    if (select.value === MANUAL) {
      manualInput.classList.remove("hidden");
      manualInput.focus();
      refresh();
    } else {
      manualInput.classList.add("hidden");
      onChange(select.value);
      refresh();
    }
  });
  manualInput.addEventListener("input", refresh); // 输入中实时校验提示
  manualInput.addEventListener("change", () => {
    onChange(manualInput.value.trim());
    refresh();
  });
  refresh();
  wrap.append(select, manualInput, status, warn);
  return wrap;
}

/* M20-补丁1 F3：时间窗（HH:MM-HH:MM）→ 两个时间选择器。
 * 值不是标准格式（手改过的自定义串）时退回文本框，绝不静默改写已存值。 */
function timeWindowControl({ value, onChange }) {
  const wrap = document.createElement("div");
  wrap.className = "time-window";
  const text = String(value ?? "");
  const match = text.match(/^(\d{1,2}:\d{2})\s*-\s*(\d{1,2}:\d{2})$/);
  if (text && !match) {
    const input = document.createElement("input");
    input.type = "text";
    input.value = text;
    input.addEventListener("change", () => onChange(input.value.trim()));
    const hint = document.createElement("div");
    hint.className = "control-warn";
    hint.textContent = "⚠ 当前值不是 HH:MM-HH:MM 标准格式，已按原文展示（改动会整体覆盖）。";
    wrap.append(input, hint);
    return wrap;
  }
  const start = document.createElement("input");
  start.type = "time";
  start.value = match ? match[1] : "23:00";
  const end = document.createElement("input");
  end.type = "time";
  end.value = match ? match[2] : "07:00";
  const commit = () => {
    if (start.value && end.value) onChange(`${start.value}-${end.value}`);
  };
  start.addEventListener("change", commit);
  end.addEventListener("change", commit);
  wrap.append(start, document.createTextNode(" 至 "), end);
  return wrap;
}

/* M20-补丁1 F2：工作区目录可视化选择——服务端列目录（fs_list 端点，
 * 只读、限根），点选回填绝对路径。不用浏览器原生 directory input
 * （拿不到服务器真实路径）。 */
function workspaceDirControl({ value, onChange }) {
  const wrap = document.createElement("div");
  wrap.className = "workspace-picker";
  const row = document.createElement("div");
  row.className = "workspace-row";
  const input = document.createElement("input");
  input.type = "text";
  input.placeholder = "留空 = 用插件数据目录下的 workspace/（推荐）";
  input.value = value ?? "";
  input.addEventListener("change", () => onChange(input.value.trim()));
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "link-button";
  btn.textContent = "浏览…";
  const browserEl = document.createElement("div");
  browserEl.className = "fs-browser hidden";

  const hide = () => browserEl.classList.add("hidden");
  const toggle = () => {
    if (browserEl.classList.contains("hidden")) list(input.value.trim());
    else hide();
  };

  function parentWithinRoots(path, roots) {
    for (const root of roots) {
      if (path === root) return null; // 已是根，无上级
      if (path.startsWith(root + "/") || path.startsWith(root + "\\")) {
        const parent = path.replace(/[\\/]+$/, "").replace(/[\\/][^\\/]*$/, "");
        if (parent.startsWith(root)) return parent;
      }
    }
    return null;
  }

  function renderBrowser(data) {
    browserEl.innerHTML = "";
    const title = document.createElement("div");
    title.className = "fs-path";
    title.textContent = "当前目录：" + data.path;
    browserEl.appendChild(title);
    const parent = parentWithinRoots(data.path, data.roots || []);
    const nav = document.createElement("div");
    nav.className = "fs-actions";
    if (parent) {
      const up = document.createElement("button");
      up.type = "button";
      up.className = "link-button";
      up.textContent = "上一级";
      up.addEventListener("click", () => list(parent));
      nav.appendChild(up);
    }
    const pickHere = document.createElement("button");
    pickHere.type = "button";
    pickHere.className = "link-button";
    pickHere.textContent = "选择当前目录";
    pickHere.addEventListener("click", () => {
      onChange(data.path);
      input.value = data.path;
      hide();
      toast("已选择工作区目录（保存后生效）");
    });
    nav.appendChild(pickHere);
    browserEl.appendChild(nav);
    if (!(data.dirs || []).length) {
      const empty = document.createElement("div");
      empty.className = "hint";
      empty.textContent = "（该目录下没有子目录）";
      browserEl.appendChild(empty);
    }
    for (const dir of data.dirs || []) {
      const line = document.createElement("div");
      line.className = "fs-row";
      const name = document.createElement("span");
      name.textContent = "📁 " + dir.name;
      const enter = document.createElement("button");
      enter.type = "button";
      enter.className = "link-button";
      enter.textContent = "进入";
      enter.addEventListener("click", () => list(dir.path));
      line.append(name, enter);
      browserEl.appendChild(line);
    }
  }

  async function list(path) {
    browserEl.classList.remove("hidden");
    browserEl.textContent = "读取中…";
    try {
      const resp = await bridge.apiPost("fs_list", { path: path || "" });
      if (resp && resp.status === "error") {
        browserEl.textContent = "⚠ " + (resp.message || "读取失败");
        return;
      }
      const data = resp && resp.data ? resp.data : resp;
      if (!data || !data.path) {
        browserEl.textContent = "⚠ 读取失败：响应为空";
        return;
      }
      renderBrowser(data);
    } catch (e) {
      browserEl.textContent = "⚠ 读取失败：" + e;
    }
  }

  btn.addEventListener("click", toggle);
  row.append(input, btn);
  wrap.append(row, browserEl);
  return wrap;
}

const $ = (sel) => document.querySelector(sel);

/* ---------------- bridge 引导 ---------------- */

async function waitBridge(timeoutMs = 8000) {
  const start = Date.now();
  while (!window.AstrBotPluginPage) {
    if (Date.now() - start > timeoutMs) {
      showError("加载失败：未找到 AstrBot Pages bridge SDK。");
      throw new Error("bridge SDK not found");
    }
    await new Promise((r) => setTimeout(r, 50));
  }
  await window.AstrBotPluginPage.ready();
  return window.AstrBotPluginPage;
}

/* ---------------- 基础 UI 工具 ---------------- */

function toast(message, isError = false) {
  const el = $("#toast");
  el.textContent = message;
  el.classList.toggle("error", isError);
  el.classList.remove("hidden");
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => el.classList.add("hidden"), 2800);
}

function showError(text) {
  const el = $("#load-error");
  el.textContent = text;
  el.classList.remove("hidden");
}

function setDirty(dirty) {
  state.dirty = dirty;
  $("#save-state").textContent = dirty ? "有未保存的改动" : "";
  $("#btn-save").classList.toggle("attention", dirty);
}

function confirmModal(text) {
  return new Promise((resolve) => {
    $("#modal-text").textContent = text;
    $("#modal").classList.remove("hidden");
    const done = (answer) => {
      $("#modal").classList.add("hidden");
      $("#modal-ok").onclick = null;
      $("#modal-cancel").onclick = null;
      resolve(answer);
    };
    $("#modal-ok").onclick = () => done(true);
    $("#modal-cancel").onclick = () => done(false);
  });
}

/* ---------------- 数据加载 ---------------- */

async function load() {
  const data = await bridge.apiGet("config");
  const payload = data && data.data ? data.data : data;
  state.schema = payload.schema;
  state.providers = payload.providers || []; // M19-补丁1 F2/E3：provider 下拉数据源
  state.agent_tools = payload.agent_tools || []; // M20-补丁1 F3：本体工具多选数据源
  state.values.knobs = { ...(payload.knobs || {}) };
  state.values.advanced = JSON.parse(JSON.stringify(payload.advanced || {}));
  state.loaded = JSON.parse(JSON.stringify(state.values));
  setDirty(false);
  renderNovice();
  renderExpert();
}

/* 差量提取：只提交用户真正改动过的键（防止页面快照旧值覆盖后端新值） */
function diffSection(current, loaded) {
  const out = {};
  for (const [name, value] of Object.entries(current || {})) {
    if (JSON.stringify(value) !== JSON.stringify((loaded || {})[name])) {
      out[name] = value;
    }
  }
  return out;
}

function buildSavePayload() {
  const knobs = diffSection(state.values.knobs, state.loaded.knobs);
  const advanced = {};
  for (const [group, keys] of Object.entries(state.values.advanced)) {
    const changed = diffSection(keys, (state.loaded.advanced || {})[group] || {});
    if (Object.keys(changed).length) advanced[group] = changed;
  }
  const payload = {};
  if (Object.keys(knobs).length) payload.knobs = knobs;
  if (Object.keys(advanced).length) payload.advanced = advanced;
  return payload;
}

/* ---------------- 新手视图 ---------------- */

function knobHint(schemaItem) {
  return schemaItem && schemaItem.hint ? schemaItem.hint : "";
}

function renderNovice() {
  const presetSchema = state.schema.preset.items;
  const grid = $("#novice-cards");
  grid.innerHTML = "";
  for (const name of KNOB_ORDER) {
    const item = presetSchema[name];
    if (!item) continue;
    const card = document.createElement("div");
    card.className = "knob-card";

    const title = document.createElement("h3");
    title.textContent = item.description || name;
    card.appendChild(title);

    const hint = document.createElement("p");
    hint.className = "hint";
    hint.textContent = knobHint(item);
    card.appendChild(hint);

    if (name === "preset_model") {
      // M20-补丁1 F1/A2：新手卡"它独处时用哪个 AI 大脑"改下拉（更不能
      // 让新手手打 id）+ 留空缓存警告 + 当前生效显示（providerPickerControl）
      card.appendChild(providerPickerControl({
        value: state.values.knobs[name] ?? "",
        onChange: (v) => { state.values.knobs[name] = v; setDirty(true); },
        emptyLabel: "（留空 = 与聊天共用模型）",
      }));
    } else {
      const options = item.options || [];
      const wrap = document.createElement("div");
      wrap.className = "option-row";
      for (const opt of options) {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "option";
        // M23-补丁1 A5：有能力档中文标签映射时显示中文（值不变）
        btn.textContent = (KNOB_OPTION_LABELS[name] || {})[opt] || opt;
        if (state.values.knobs[name] === opt) btn.classList.add("selected");
        btn.addEventListener("click", () => {
          state.values.knobs[name] = opt;
          setDirty(true);
          wrap.querySelectorAll(".option").forEach((el) => el.classList.remove("selected"));
          btn.classList.add("selected");
        });
        wrap.appendChild(btn);
      }
      card.appendChild(wrap);
    }
    grid.appendChild(card);
  }
  grid.appendChild(initiativeCard()); // 主动搭话卡（M14-补丁2 F3）：新手卡之后、起床约定卡之前
  grid.appendChild(scheduleCard()); // 起床约定卡（M5-补丁4）
  // M15-补丁1 F1：新后端键全部配面板入口（铁律 4b），接在睡眠/能力相关卡之后
  grid.appendChild(farewellCard()); // 晚安消息（三档 + 概率滑块）
  grid.appendChild(chatGuardCard()); // 聊天时不睡觉
  grid.appendChild(wakeRandomCard()); // 随机吵醒（M17-补丁1 C1）
  grid.appendChild(pendingReplyCard()); // 醒来补回复（M17-补丁1 C2）
  grid.appendChild(styleLearningCard()); // 风格学习（M17-补丁1 A5）
  grid.appendChild(styleDataCard()); // M20-补丁1 J：语料与素材（添加/立即处理）
  grid.appendChild(browserCard()); // 浏览器能力说明 + 实时状态
  grid.appendChild(workspaceCard()); // 她的文件夹（M23-补丁1 B4）：路径 + 状态
  grid.appendChild(searchToggleCard()); // 联网搜索开关
  grid.appendChild(agentToolsCard()); // 本体工具开关
  grid.appendChild(judgeCard()); // M19-补丁1 A5/E3：判断模型（三档+状态+记录）
  renderLifeExtra(presetSchema);
}

/* ---------------- M19-补丁1：判断模型新手卡（A5/E3/E1） ----------------
 * 读写走 advanced.judge 差量保存流（与 farewellCard 同款）；三档选择 +
 * provider 下拉（api 档时显示）+ 当前状态行（E3）+ 最近判断记录回看
 * （E1，独立端点 judge_records，不走配置差量流）。 */
function judgeCard() {
  if (!state.values.advanced.judge) state.values.advanced.judge = {};
  const judgeValues = state.values.advanced.judge;
  const card = document.createElement("div");
  card.className = "knob-card judge-card";

  const title = document.createElement("h3");
  title.textContent = "判断模型";
  card.appendChild(title);

  const hint = document.createElement("p");
  hint.className = "hint";
  hint.textContent =
    "外挂一个很小的\"大脑\"帮你把关：你发消息时先判断该用什么方式回" +
    "（详细/简短/带情绪），她回复后再检查一次有没有越回越啰嗦——对抗" +
    "输出惯性。判断用的模型应该是又小又快的（便宜），跟聊天模型分开。" +
    "判断结果用完就丢，不会进她的记忆。";
  card.appendChild(hint);

  const MODES = [
    ["关闭", "off"],
    ["本地（未实现）", "local"],
    ["云端 API", "api"],
  ];
  const row = document.createElement("div");
  row.className = "option-row";
  const currentMode = judgeValues.mode || "off";
  for (const [label, value] of MODES) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "option";
    btn.textContent = label;
    if (currentMode === value) btn.classList.add("selected");
    btn.addEventListener("click", () => {
      judgeValues.mode = value;
      setDirty(true);
      row.querySelectorAll(".option").forEach((el) => el.classList.remove("selected"));
      btn.classList.add("selected");
      refreshJudgeExtras();
    });
    row.appendChild(btn);
  }
  card.appendChild(row);

  const localNote = document.createElement("p");
  localNote.className = "hint judge-local-note";
  localNote.textContent =
    "本地小模型推理还没有实现（权重下载等后续版本再说），选中它只是先占个" +
    "位——判断模型现在不会工作。目前请用「云端 API」档。";

  const providerWrap = document.createElement("div");
  providerWrap.className = "judge-provider-wrap";
  const providerLabel = document.createElement("span");
  providerLabel.className = "hint";
  providerLabel.textContent = "判断用 provider：";
  const providerSelect = document.createElement("select");
  providerSelect.className = "enum-select";
  const currentValue = judgeValues.provider_id || "";
  const blank = document.createElement("option");
  blank.value = "";
  blank.textContent = "（不判断）";
  providerSelect.appendChild(blank);
  const seen = new Set(state.providers || []);
  for (const pid of state.providers || []) {
    const opt = document.createElement("option");
    opt.value = pid;
    opt.textContent = pid;
    if (currentValue === pid) opt.selected = true;
    providerSelect.appendChild(opt);
  }
  // 已存值不在列表里（老配置/该 provider 已停用）——原样保留，不规范化
  if (currentValue && !seen.has(currentValue)) {
    const opt = document.createElement("option");
    opt.value = currentValue;
    opt.textContent = `${currentValue}（当前不可用）`;
    opt.selected = true;
    providerSelect.appendChild(opt);
  }
  providerSelect.addEventListener("change", () => {
    judgeValues.provider_id = providerSelect.value;
    setDirty(true);
    refreshStatus();
  });
  providerWrap.append(providerLabel, providerSelect);

  const statusLine = document.createElement("p");
  statusLine.className = "hint judge-status";
  const refreshStatus = () => {
    const mode = judgeValues.mode || "off";
    if (mode === "off") statusLine.textContent = "当前状态：关闭（零调用，行为与从前完全一致）";
    else if (mode === "local") statusLine.textContent = "当前状态：本地（未实现，暂不工作）";
    else {
      const pid = judgeValues.provider_id || "";
      statusLine.textContent = pid
        ? `当前状态：云端 API（${pid}）`
        : "当前状态：云端 API（还没选 provider，暂不判断）";
    }
  };
  const refreshJudgeExtras = () => {
    const mode = judgeValues.mode || "off";
    localNote.classList.toggle("hidden", mode !== "local");
    providerWrap.classList.toggle("hidden", mode !== "api");
    refreshStatus();
  };
  refreshJudgeExtras();
  card.append(localNote, providerWrap, statusLine);

  // E1：最近判断记录回看（按钮拉取，独立端点）
  const recordsWrap = document.createElement("div");
  recordsWrap.className = "judge-records hidden";
  const recordsBtn = document.createElement("button");
  recordsBtn.type = "button";
  recordsBtn.className = "link-button";
  recordsBtn.textContent = "查看最近判断记录";
  recordsBtn.addEventListener("click", async () => {
    if (!recordsWrap.classList.contains("hidden")) {
      recordsWrap.classList.add("hidden");
      return;
    }
    recordsBtn.textContent = "加载中…";
    try {
      const data = await bridge.apiGet("judge_records");
      const body = data && data.data ? data.data : data;
      const records = (body && body.records) || [];
      recordsWrap.innerHTML = "";
      if (!records.length) {
        const empty = document.createElement("p");
        empty.className = "hint";
        empty.textContent = "还没有判断记录（开启 API 档并聊几句之后就有了）。";
        recordsWrap.appendChild(empty);
      }
      for (const rec of records.slice(0, 10)) {
        const line = document.createElement("p");
        line.className = "judge-record-line";
        const tag = rec.side === "input" ? "输入" : rec.side === "output" ? "输出" : "打回";
        line.textContent =
          `[${rec.ts}] ${tag}${rec.injected ? "·已注入" : ""}` +
          `${rec.rewrote ? "·已重写" : ""}：${rec.input_summary} → ${rec.verdict}`;
        recordsWrap.appendChild(line);
      }
      recordsWrap.classList.remove("hidden");
    } catch (e) {
      toast(`判断记录读取失败：${e && e.message ? e.message : e}`, true);
    }
    recordsBtn.textContent = "查看最近判断记录";
  });
  card.append(recordsBtn, recordsWrap);
  return card;
}

/* ---------------- M15-补丁1 新手卡 ----------------
 * 读写全部走 advanced 组既有差量保存流（与 scheduleCard 同款），
 * 不另开提交通道；键名与 _conf_schema.json 逐字对应（F4 源码断言锚点）。 */

/* 通用选项卡：options=[[label, value]...]，读写经 get/set 访问器 */
function optionCard(title, hint, options, get, set) {
  const card = document.createElement("div");
  card.className = "knob-card";
  const h = document.createElement("h3");
  h.textContent = title;
  card.appendChild(h);
  const p = document.createElement("p");
  p.className = "hint";
  p.textContent = hint;
  card.appendChild(p);
  const row = document.createElement("div");
  row.className = "option-row";
  const current = get();
  for (const [label, value] of options) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "option";
    btn.textContent = label;
    if (current === value) btn.classList.add("selected");
    btn.addEventListener("click", () => {
      set(value);
      row.querySelectorAll(".option").forEach((el) => el.classList.remove("selected"));
      btn.classList.add("selected");
    });
    row.appendChild(btn);
  }
  card.appendChild(row);
  return card;
}

/* 晚安消息卡：不说 / 随机说（显示概率滑块）/ 她自己斟酌着说。
 * 读写 advanced.sleep.farewell_mode + farewell_probability（A 组）。 */
function farewellCard() {
  if (!state.values.advanced.sleep) state.values.advanced.sleep = {};
  const sleepValues = state.values.advanced.sleep;
  const card = document.createElement("div");
  card.className = "knob-card farewell-card";

  const title = document.createElement("h3");
  title.textContent = "晚安消息";
  card.appendChild(title);

  const hint = document.createElement("p");
  hint.className = "hint";
  hint.textContent =
    "她去睡觉时要不要跟你说声晚安。「她自己斟酌着说」会让她睡前看一眼" +
    "今天你们聊得怎么样，再决定说不说、怎么说——聊得开心自然道晚安，" +
    "还在气头上可以不说，想和好也可以借这句说点什么。";
  card.appendChild(hint);

  const MODES = [
    ["不说", "off"],
    ["随机说", "probability"],
    ["她自己斟酌着说", "llm"],
  ];
  const mode = sleepValues.farewell_mode || "probability";
  const row = document.createElement("div");
  row.className = "option-row";
  const sliderWrap = document.createElement("div");
  sliderWrap.className = "schedule-slider hidden";
  for (const [label, value] of MODES) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "option";
    btn.textContent = label;
    if (mode === value) btn.classList.add("selected");
    btn.addEventListener("click", () => {
      sleepValues.farewell_mode = value;
      setDirty(true);
      row.querySelectorAll(".option").forEach((el) => el.classList.remove("selected"));
      btn.classList.add("selected");
      sliderWrap.classList.toggle("hidden", value !== "probability");
    });
    row.appendChild(btn);
  }
  card.appendChild(row);

  sliderWrap.classList.toggle("hidden", mode !== "probability");
  const sliderLabel = document.createElement("p");
  sliderLabel.className = "slider-label";
  const slider = document.createElement("input");
  slider.type = "range";
  slider.min = "0";
  slider.max = "100";
  const prob = typeof sleepValues.farewell_probability === "number"
    ? sleepValues.farewell_probability : 0.5;
  slider.value = String(Math.round(prob * 100));
  const updateLabel = () => {
    sliderLabel.textContent = `晚安概率 ${slider.value}%`;
  };
  updateLabel();
  slider.addEventListener("input", () => {
    sleepValues.farewell_probability = Number(slider.value) / 100;
    updateLabel();
    setDirty(true);
  });
  sliderWrap.append(sliderLabel, slider);
  card.appendChild(sliderWrap);
  return card;
}

/* 聊天保护卡：advanced.sleep.standby_blocks_sleep（B 组，默认开） */
function chatGuardCard() {
  if (!state.values.advanced.sleep) state.values.advanced.sleep = {};
  const sleepValues = state.values.advanced.sleep;
  return optionCard(
    "聊天时不睡觉",
    "她陪你聊天的时候不会当场睡着——你安静半小时后她才恢复入睡评估。" +
      "关掉则回到旧行为（睡意到了可能聊着聊着就睡着）。",
    [["开", true], ["关", false]],
    () => sleepValues.standby_blocks_sleep !== false,
    (v) => {
      sleepValues.standby_blocks_sleep = v;
      setDirty(true);
    },
  );
}

/* 随机吵醒卡：advanced.sleep.wake_random_enabled（M17-补丁1 C1，默认开） */
function wakeRandomCard() {
  if (!state.values.advanced.sleep) state.values.advanced.sleep = {};
  const sleepValues = state.values.advanced.sleep;
  return optionCard(
    "随机吵醒",
    "每次入睡时随机抽定「连发几条能吵醒她」（1-3 条中按睡眠深浅加权：" +
      "刚入睡偏难叫醒，快天亮时偏容易叫醒），同一次睡觉内不变。" +
      "关掉则回到固定阈值（默认 3 条）。",
    [["开", true], ["关", false]],
    () => sleepValues.wake_random_enabled !== false,
    (v) => {
      sleepValues.wake_random_enabled = v;
      setDirty(true);
    },
  );
}

/* 醒来补回复卡：advanced.sleep.pending_reply_enabled（M17-补丁1 C2，默认关） */
function pendingReplyCard() {
  if (!state.values.advanced.sleep) state.values.advanced.sleep = {};
  const sleepValues = state.values.advanced.sleep;
  return optionCard(
    "醒来补回复",
    "她睡着时你发的消息（没吵醒她的），她醒来后会自己看看要不要回：" +
      "可能认真回，可能轻描淡写接一句（\"昨晚睡着了，你说的那个我看看哈\"），" +
      "也可能觉得不用回就不回。默认关，先看效果再决定常开。",
    [["开", true], ["关", false]],
    () => sleepValues.pending_reply_enabled === true,
    (v) => {
      sleepValues.pending_reply_enabled = v;
      setDirty(true);
    },
  );
}

/* 风格学习卡：advanced.style_learning.enabled（M17-补丁1 A5，默认关） */
function styleLearningCard() {
  if (!state.values.advanced.style_learning) {
    state.values.advanced.style_learning = {};
  }
  const styleValues = state.values.advanced.style_learning;
  return optionCard(
    "风格学习",
    "她在读文章、冲浪的时候，会从真人写的东西里学说话风格——句式、" +
      "思维方式、待人接物，不只是口癖。学到的味道会在她说话时低调度参考，" +
      "用得顺的变成习惯，久不用自然淡出。判定像 AI 写的语料绝不学。" +
      "默认关，先看效果再决定常开（细项在专家组「风格学习」）。",
    [["开", true], ["关", false]],
    () => styleValues.enabled === true,
    (v) => {
      styleValues.enabled = v;
      setDirty(true);
    },
  );
}

/* 浏览器能力说明块（C2/C3）：与 README「浏览器能力（可选安装）」同源
 * 措辞（装什么/作用/不装会怎样/安装/卸载）；状态行经 browser_status
 * 端点实时拉取，取不到就隐藏（说明块仍在）。 */
function browserCard() {
  const card = document.createElement("div");
  card.className = "knob-card browser-card";
  const h = document.createElement("h3");
  h.textContent = "浏览器能力（可选安装）";
  card.appendChild(h);
  const p = document.createElement("p");
  p.className = "hint";
  p.textContent =
    "她的浏览器工具（打开网页、看画面、点击、输入）依赖 Playwright 的 " +
    "Chromium 内核（约 150MB 下载），不随插件内置。装好后能力档位 ≥1 时" +
    "她能真正浏览网页并把看到的画面截图存档；不装则浏览器工具不挂载，" +
    "她的活动退化为「搜索 + 读文本」，其余能力不受影响。" +
    "安装：在 AstrBot 的 Python 环境执行 playwright install chromium，装完重启。" +
    "卸载：playwright uninstall chromium（或删除 ms-playwright 缓存目录），" +
    "自动回落，无需改配置。";
  card.appendChild(p);
  const status = document.createElement("p");
  status.className = "hint browser-status";
  status.textContent = "浏览器能力：检测中…";
  card.appendChild(status);
  refreshBrowserStatus(status);
  return card;
}

async function refreshBrowserStatus(el) {
  try {
    const res = await bridge.apiGet("browser_status");
    const data = res && res.data ? res.data : res;
    const installed = !!(data && data.installed);
    el.textContent = installed
      ? "浏览器能力：已安装"
      : "浏览器能力：未安装（浏览器工具不可用）";
  } catch (e) {
    el.textContent = ""; // 状态取不到就不显示，说明块仍在
  }
}

/* 她的文件夹（M23-补丁1 B4）：工作区实际路径与状态实时展示。
 * 让人一眼知道自己的 workspace_dir 配置有没有生效（就绪/不存在/被文件
 * 占用/创建失败），失败时带原因，不静默。 */
function workspaceCard() {
  const card = document.createElement("div");
  card.className = "knob-card workspace-card";
  const h = document.createElement("h3");
  h.textContent = "她的文件夹";
  card.appendChild(h);
  const p = document.createElement("p");
  p.className = "hint";
  p.textContent =
    "这是她的专属工作区：能力档「玩」及以上时，她的文件读写、小程序都" +
    "在这个目录里；开到「命令行」档时那也是命令的默认工作目录。路径来自" +
    "专家配置 autonomy.workspace_dir，留空则用插件数据目录下的默认位置，" +
    "插件启动时会自动创建。如果下面显示创建失败，检查路径是否合法、磁盘" +
    "是否可写。";
  card.appendChild(p);
  const status = document.createElement("p");
  status.className = "hint workspace-status";
  status.textContent = "她的文件夹：读取中…";
  card.appendChild(status);
  refreshWorkspaceStatus(status);
  return card;
}

async function refreshWorkspaceStatus(el) {
  try {
    const res = await bridge.apiGet("workspace_status");
    const data = res && res.data ? res.data : res;
    if (!data || !data.path) {
      el.textContent = "";
      return;
    }
    const stateText =
      data.state === "ready"
        ? "已就绪"
        : data.state === "missing"
          ? "不存在"
          : data.message || data.state;
    el.textContent = `她的文件夹：${data.path}（${stateText}）`;
  } catch (e) {
    el.textContent = ""; // 状态取不到就不显示
  }
}

/* 联网搜索卡：advanced.capabilities.web_search_enabled（E 组，默认开） */
function searchToggleCard() {
  if (!state.values.advanced.capabilities) state.values.advanced.capabilities = {};
  const cap = state.values.advanced.capabilities;
  return optionCard(
    "联网搜索",
    "允许她自主活动时联网搜索（博查网页搜索）。你聊天时的搜索不受影响" +
      "（那是 AstrBot 自己的搜索）。关掉后冲浪、读文章两项活动也会从她的" +
      "活动池里退场，其余能力照旧。",
    [["开", true], ["关", false]],
    () => cap.web_search_enabled !== false,
    (v) => {
      cap.web_search_enabled = v;
      setDirty(true);
    },
  );
}

/* 本体工具卡：advanced.capabilities.agent_tools_mode 的 persona/off 两态
 * （D 组，默认 off=不允许；custom 白名单在专家区「能力参数」调） */
function agentToolsCard() {
  if (!state.values.advanced.capabilities) state.values.advanced.capabilities = {};
  const cap = state.values.advanced.capabilities;
  return optionCard(
    "本体工具",
    "允许她使用你给本体（AstrBot）配置的工具——按人格设定里勾选的工具" +
      "筛选，含 MCP 工具。她自带的搜索、抓取、沙箱、记忆能力不受影响。",
    [["允许", "persona"], ["不允许", "off"]],
    () => (cap.agent_tools_mode === "persona" ? "persona" : "off"),
    (v) => {
      cap.agent_tools_mode = v;
      setDirty(true);
    },
  );
}

/* 主动搭话卡（M14-补丁2 F3）：读写 advanced.initiative 的键，复用既有
 * 差量保存；总开关关闭时隐藏明细。新手界面不暴露裸概率——三档映射
 * INITIATIVE_LEVELS 是纯数据，initiativeLevelToProbability 是纯函数
 * （tests/test_m14_patch2.py 对映射表做边界值断言）。 */

/* 档位 → base_probability 映射（主人 10-03 定稿：0.08 / 0.18 / 0.35） */
const INITIATIVE_LEVELS = [
  ["quiet", "安静", 0.08],
  ["moderate", "适中", 0.18],
  ["active", "主动", 0.35],
];

function initiativeLevelToProbability(level) {
  for (const [key, _label, probability] of INITIATIVE_LEVELS) {
    if (key === level) return probability;
  }
  return 0.18; // 未知档位回落默认（与 schema 默认一致）
}

function initiativeCard() {
  if (!state.values.advanced.initiative) state.values.advanced.initiative = {};
  const ini = state.values.advanced.initiative;
  const card = document.createElement("div");
  card.className = "knob-card initiative-card";

  const title = document.createElement("h3");
  title.textContent = "主动搭话";
  card.appendChild(title);

  const hint = document.createElement("p");
  hint.className = "hint";
  hint.textContent =
    "除了分享活动和说梦话，她也会自己起念头找你说话——可能没有事由，" +
    "也可能接上你们最近聊的话题。她睡着时绝不会打扰；每天能说多少句、" +
    "间隔多久，和活动分享共用同一个额度。";
  card.appendChild(hint);

  const enabled = ini.enabled !== false;
  const detail = document.createElement("div");
  detail.className = enabled ? "initiative-detail" : "initiative-detail hidden";

  const row = document.createElement("div");
  row.className = "option-row";
  const enabledLabel = document.createElement("p");
  enabledLabel.className = "slider-label";
  enabledLabel.textContent = "总开关——关掉就回到只有活动分享的状态";
  for (const [label, value] of [["开", true], ["关", false]]) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "option";
    btn.textContent = label;
    if (enabled === value) btn.classList.add("selected");
    btn.addEventListener("click", () => {
      ini.enabled = value;
      setDirty(true);
      row.querySelectorAll(".option").forEach((el) => el.classList.remove("selected"));
      btn.classList.add("selected");
      detail.classList.toggle("hidden", value === false);
    });
    row.appendChild(btn);
  }
  detail.append(enabledLabel, row);

  // 主动程度三档（写 base_probability，不暴露裸数字）
  const levelLabel = document.createElement("p");
  levelLabel.className = "slider-label";
  levelLabel.textContent = "主动程度——她多常主动找你说话";
  const levelRow = document.createElement("div");
  levelRow.className = "option-row";
  const currentP = typeof ini.base_probability === "number"
    ? ini.base_probability : 0.18;
  for (const [key, label] of INITIATIVE_LEVELS) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "option";
    btn.textContent = label;
    if (Math.abs(currentP - initiativeLevelToProbability(key)) < 1e-9) {
      btn.classList.add("selected");
    }
    btn.addEventListener("click", () => {
      ini.base_probability = initiativeLevelToProbability(key);
      setDirty(true);
      levelRow.querySelectorAll(".option").forEach((el) => el.classList.remove("selected"));
      btn.classList.add("selected");
    });
    levelRow.appendChild(btn);
  }
  detail.append(levelLabel, levelRow);

  // 未回应收敛开关（主人定稿文案）
  const backoffLabel = document.createElement("p");
  backoffLabel.className = "slider-label";
  backoffLabel.textContent = "你不理她时，她会慢慢安静下来（不会完全不理你）";
  const backoffRow = document.createElement("div");
  backoffRow.className = "option-row";
  const backoff = ini.unanswered_backoff !== false;
  for (const [label, value] of [["开", true], ["关", false]]) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "option";
    btn.textContent = label;
    if (backoff === value) btn.classList.add("selected");
    btn.addEventListener("click", () => {
      ini.unanswered_backoff = value;
      setDirty(true);
      backoffRow.querySelectorAll(".option").forEach((el) => el.classList.remove("selected"));
      btn.classList.add("selected");
    });
    backoffRow.appendChild(btn);
  }
  detail.append(backoffLabel, backoffRow);

  card.appendChild(detail);
  return card;
}

/* 起床约定卡（M5-补丁4 D2）——总开关 + 自觉性滑块。
 * 读写 advanced.sleep 的两个键，复用既有差量保存；关闭时隐藏滑块 */
function scheduleCard() {
  if (!state.values.advanced.sleep) state.values.advanced.sleep = {};
  const sleepValues = state.values.advanced.sleep;
  const card = document.createElement("div");
  card.className = "knob-card schedule-card";

  const title = document.createElement("h3");
  title.textContent = "起床约定";
  card.appendChild(title);

  const hint = document.createElement("p");
  hint.className = "hint";
  hint.textContent =
    "感知你的起床约定——你说\"明早 8 点起\"，他会记在心里。像人一样：" +
    "说了早起可能早睡，也可能熬夜睡过头。";
  card.appendChild(hint);

  const enabled = sleepValues.schedule_reminder_enabled !== false;
  const row = document.createElement("div");
  row.className = "option-row";
  const sliderWrap = document.createElement("div");
  sliderWrap.className = enabled ? "schedule-slider" : "schedule-slider hidden";
  for (const [label, value] of [["开", true], ["关", false]]) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "option";
    btn.textContent = label;
    if (enabled === value) btn.classList.add("selected");
    btn.addEventListener("click", () => {
      sleepValues.schedule_reminder_enabled = value;
      setDirty(true);
      row.querySelectorAll(".option").forEach((el) => el.classList.remove("selected"));
      btn.classList.add("selected");
      sliderWrap.classList.toggle("hidden", value === false);
    });
    row.appendChild(btn);
  }
  card.appendChild(row);

  const sliderLabel = document.createElement("p");
  sliderLabel.className = "slider-label";
  const discipline = typeof sleepValues.schedule_discipline === "number"
    ? sleepValues.schedule_discipline : 0.6;
  const slider = document.createElement("input");
  slider.type = "range";
  slider.min = "0";
  slider.max = "100";
  slider.value = String(Math.round(discipline * 100));
  const updateLabel = () => {
    sliderLabel.textContent =
      `自觉性 ${slider.value}%——调高更守时，调低更容易熬夜睡过头`;
  };
  updateLabel();
  slider.addEventListener("input", () => {
    sleepValues.schedule_discipline = Number(slider.value) / 100;
    updateLabel();
    setDirty(true);
  });
  sliderWrap.append(sliderLabel, slider);
  card.appendChild(sliderWrap);
  return card;
}

function renderLifeExtra(presetSchema) {
  const life = $("#novice-life");
  life.innerHTML = "";
  const item = presetSchema.life_extra;
  const title = document.createElement("h3");
  title.textContent = (item && item.description) || "它的性格与兴趣底色";
  const hint = document.createElement("p");
  hint.className = "hint";
  hint.textContent = knobHint(item);
  const ta = document.createElement("textarea");
  ta.rows = 6;
  ta.value = state.values.knobs.life_extra ?? "";
  ta.addEventListener("input", () => {
    state.values.knobs.life_extra = ta.value;
    setDirty(true);
  });
  life.append(title, hint, ta);
}

/* ---------------- 专家视图 ---------------- */

function isDanger(group, key) {
  return DANGER_KEYS.has(`${group}.${key}`);
}

function buildControl(group, key, item, container) {
  const value = (state.values.advanced[group] || {})[key];
  const danger = isDanger(group, key);
  const set = (v) => {
    if (!state.values.advanced[group]) state.values.advanced[group] = {};
    state.values.advanced[group][key] = v;
    setDirty(true);
  };
  const t = item.type;
  if (t === "bool") {
    const sw = document.createElement("input");
    sw.type = "checkbox";
    sw.className = "switch";
    sw.checked = value === true;
    sw.addEventListener("change", () => set(sw.checked));
    container.appendChild(sw);
  } else if (t === "int" || t === "float") {
    const input = document.createElement("input");
    input.type = "number";
    input.step = t === "float" ? "any" : "1";
    input.value = value ?? "";
    input.addEventListener("change", () => {
      const num = Number(input.value);
      set(Number.isNaN(num) ? input.value : (t === "int" ? Math.trunc(num) : num));
    });
    container.appendChild(input);
  } else if (t === "string" && Array.isArray(item.options) && item.options.length) {
    // M19-补丁1 F1/F4：schema 带 options 的枚举字段一律下拉（显示中文
    // 标签、保存原始值）——不再出现"有选项却渲染成空白文本框"。
    // F4 兼容：若当前值不在选项里（老配置/手改值），保留一个额外选项
    // 让它原样显示——绝不"顺手规范化"用户已存的值。
    const select = document.createElement("select");
    select.className = "enum-select";
    const labels = OPTION_LABELS[`${group}.${key}`] || {};
    const choices = item.options.slice();
    if (value !== undefined && value !== null && !choices.includes(value)) {
      choices.push(value);
    }
    for (const opt of choices) {
      const optEl = document.createElement("option");
      optEl.value = opt;
      optEl.textContent = labels[opt] || opt;
      if (value === opt) optEl.selected = true;
      select.appendChild(optEl);
    }
    select.addEventListener("change", () => set(select.value));
    container.appendChild(select);
  } else if (group === "decision" && key === "agent_activities") {
    // M19-补丁1 F2：活动白名单改多选（6 个活动带中文说明）
    const current = Array.isArray(value) ? value : [];
    const wrap = document.createElement("div");
    wrap.className = "multi-choices";
    for (const choice of AGENT_ACTIVITY_CHOICES) {
      const row = document.createElement("label");
      row.className = "multi-choice";
      const box = document.createElement("input");
      box.type = "checkbox";
      box.checked = current.includes(choice.value);
      box.addEventListener("change", () => {
        // F4：保存数组（类型与内容形态不变），勾选集合按固定顺序输出
        const next = AGENT_ACTIVITY_CHOICES
          .map((c) => c.value)
          .filter((v) => (v === choice.value ? box.checked : current.includes(v)));
        set(next);
        current.length = 0;
        current.push(...next);
      });
      row.append(box, document.createTextNode(choice.label));
      wrap.appendChild(row);
    }
    container.appendChild(wrap);
  } else if (group === "model" && key === "fallback_chain") {
    // M19-补丁1 F2：故障转移链 = 多选 + 顺序（顺序有语义：按序尝试）。
    // 顺序表达方式选"已选列表可上下移动"——比"按勾选先后"更可靠
    // （checkbox 勾选顺序难以回显）。
    const stateList = Array.isArray(value) ? [...value] : [];
    const wrap = document.createElement("div");
    wrap.className = "fallback-editor";
    const pool = state.providers.filter((p) => !stateList.includes(p));
    const addSelect = document.createElement("select");
    addSelect.className = "enum-select";
    const placeholder = document.createElement("option");
    placeholder.value = "";
    placeholder.textContent = state.providers.length
      ? "添加 provider…"
      : "（没有已启用的聊天 provider）";
    addSelect.appendChild(placeholder);
    for (const pid of pool) {
      const optEl = document.createElement("option");
      optEl.value = pid;
      optEl.textContent = pid;
      addSelect.appendChild(optEl);
    }
    addSelect.addEventListener("change", () => {
      if (!addSelect.value) return;
      stateList.push(addSelect.value);
      set([...stateList]);
      renderRows();
    });
    const rowsEl = document.createElement("div");
    const renderRows = () => {
      rowsEl.innerHTML = "";
      stateList.forEach((pid, idx) => {
        const row = document.createElement("div");
        row.className = "fallback-row";
        const name = document.createElement("span");
        name.className = "fallback-name";
        name.textContent = `${idx + 1}. ${pid}`;
        const up = document.createElement("button");
        up.type = "button";
        up.textContent = "↑";
        up.disabled = idx === 0;
        up.title = "上移（更早尝试）";
        up.addEventListener("click", () => {
          [stateList[idx - 1], stateList[idx]] = [stateList[idx], stateList[idx - 1]];
          set([...stateList]);
          renderRows();
        });
        const down = document.createElement("button");
        down.type = "button";
        down.textContent = "↓";
        down.disabled = idx === stateList.length - 1;
        down.title = "下移（更晚尝试）";
        down.addEventListener("click", () => {
          [stateList[idx + 1], stateList[idx]] = [stateList[idx], stateList[idx + 1]];
          set([...stateList]);
          renderRows();
        });
        const del = document.createElement("button");
        del.type = "button";
        del.textContent = "×";
        del.title = "移出故障转移链";
        del.addEventListener("click", () => {
          stateList.splice(idx, 1);
          set([...stateList]);
          renderRows();
        });
        row.append(name, up, down, del);
        rowsEl.appendChild(row);
      });
    };
    renderRows();
    wrap.append(addSelect, rowsEl);
    container.appendChild(wrap);
  } else if (group === "style_learning" && key === "source_weights") {
    // M19-补丁1 F3：权重表只有两个固定键 → 两个数字输入（保存仍为
    // object，F4 类型不变）。用户键里的额外键原样保留。
    // （本分支必须在下方通用 list/object 之前——object 类型先被特判。）
    const current = (value && typeof value === "object" && !Array.isArray(value))
      ? { ...value } : {};
    const wrap = document.createElement("div");
    wrap.className = "weight-fields";
    for (const field of SOURCE_WEIGHT_FIELDS) {
      const row = document.createElement("label");
      row.className = "weight-field";
      const input = document.createElement("input");
      input.type = "number";
      input.step = "any";
      input.value = current[field.key] ?? "";
      input.addEventListener("change", () => {
        const num = Number(input.value);
        const next = { ...current };
        if (Number.isNaN(num)) next[field.key] = input.value;
        else next[field.key] = num;
        set(next);
      });
      row.append(document.createTextNode(field.label), input);
      wrap.appendChild(row);
    }
    container.appendChild(wrap);
  } else if (group === "model" && key === "provider_id") {
    // M20-补丁1 F1/A2/A4：自主活动 provider 改下拉 + 手填兜底 + 缓存警告
    container.appendChild(providerPickerControl({
      value: value ?? "",
      onChange: (v) => set(v),
      emptyLabel: "（留空 = 与聊天共用模型）",
    }));
  } else if (group === "autonomy" && key === "workspace_dir") {
    // M20-补丁1 F2：工作区目录可视化选择（服务端列目录，只读限根）
    container.appendChild(workspaceDirControl({ value: value ?? "", onChange: (v) => set(v) }));
  } else if (group === "initiative" && key === "sources") {
    // M20-补丁1 F3：念头来源改多选（取值固定，逗号串形态保持不变）
    const current = String(value ?? "").split(",").map((s) => s.trim()).filter(Boolean);
    const wrap = document.createElement("div");
    wrap.className = "multi-choices";
    for (const choice of INITIATIVE_SOURCE_CHOICES) {
      const row = document.createElement("label");
      row.className = "multi-choice";
      const box = document.createElement("input");
      box.type = "checkbox";
      box.checked = current.includes(choice.value);
      box.addEventListener("change", () => {
        const selected = INITIATIVE_SOURCE_CHOICES
          .map((c) => c.value)
          .filter((v) => (v === choice.value ? box.checked : current.includes(v)));
        set(selected.join(","));
        current.length = 0;
        current.push(...selected);
      });
      row.append(box, document.createTextNode(choice.label));
      wrap.appendChild(row);
    }
    container.appendChild(wrap);
  } else if (group === "capabilities" && key === "agent_tools") {
    // M20-补丁1 F3：本体工具白名单改多选（选项=本体已注册工具，动态）。
    // 注册表里没有但已配置的值原样保留显示（F4：不静默规范化用户已存值）
    const current = String(value ?? "").split(",").map((s) => s.trim()).filter(Boolean);
    const registry = state.agent_tools || [];
    const extras = current.filter((v) => !registry.includes(v));
    const wrap = document.createElement("div");
    wrap.className = "multi-choices";
    const names = [...registry, ...extras];
    if (!names.length) {
      const hint = document.createElement("div");
      hint.className = "hint";
      hint.textContent = "（本体当前没有已注册的工具——custom 档将得到空工具集）";
      wrap.appendChild(hint);
    }
    for (const name of names) {
      const row = document.createElement("label");
      row.className = "multi-choice";
      const box = document.createElement("input");
      box.type = "checkbox";
      box.checked = current.includes(name);
      box.addEventListener("change", () => {
        const selected = names.filter((v) => (v === name ? box.checked : current.includes(v)));
        set(selected.join(","));
        current.length = 0;
        current.push(...selected);
      });
      row.append(box, document.createTextNode(extras.includes(name) ? `${name}（注册表中没有，请核对）` : name));
      wrap.appendChild(row);
    }
    container.appendChild(wrap);
  } else if (group === "sleep" && key === "circadian_hint") {
    // M20-补丁1 F3：昼夜节律提示窗改两个时间选择器（非标准格式退回文本框）
    container.appendChild(timeWindowControl({ value: value ?? "", onChange: (v) => set(v) }));
  } else if (t === "list" || t === "object") {
    const ta = document.createElement("textarea");
    ta.rows = t === "object" ? 4 : 3;
    ta.spellcheck = false;
    ta.value = t === "object" ? JSON.stringify(value ?? {}, null, 1) : (value || []).join("\n");
    ta.addEventListener("change", () => {
      if (t === "object") {
        try { set(JSON.parse(ta.value || "{}")); }
        catch { toast(`保存失败：${group}.${key} 不是合法 JSON`, true); }
      } else {
        set(ta.value.split("\n").map((s) => s.trim()).filter(Boolean));
      }
    });
    container.appendChild(ta);
  } else if (t === "text") {
    const ta = document.createElement("textarea");
    ta.rows = 3;
    ta.value = value ?? "";
    ta.addEventListener("change", () => set(ta.value));
    container.appendChild(ta);
  } else {
    const input = document.createElement("input");
    input.type = "text";
    input.value = value ?? "";
    input.addEventListener("change", () => set(input.value));
    container.appendChild(input);
  }
  if (danger) container.classList.add("danger-zone");
}

function renderExpert() {
  const list = $("#expert-groups");
  list.innerHTML = "";
  const advancedSchema = state.schema.advanced.items;
  for (const [group, body] of Object.entries(advancedSchema)) {
    const keys = Object.entries(body.items || {});
    const drawer = document.createElement("div");
    drawer.className = "drawer";

    const head = document.createElement("button");
    head.type = "button";
    const label = GROUP_LABELS[group] || group;
    const expanded = !!state.expanded[group];
    head.className = "drawer-head" + (expanded ? " open" : "");
    head.innerHTML = `<span class="arrow">${expanded ? "▾" : "▸"}</span> ` +
      `${label} <span class="group-name">${group}</span>` +
      `<span class="count">${keys.length} 项</span>`;
    const body_el = document.createElement("div");
    body_el.className = "drawer-body" + (expanded ? "" : " hidden");

    head.addEventListener("click", () => {
      const next = !state.expanded[group];
      state.expanded[group] = next;
      saveExpanded(state.expanded);
      head.classList.toggle("open", next);
      head.querySelector(".arrow").textContent = next ? "▾" : "▸";
      body_el.classList.toggle("hidden", !next);
    });

    for (const [key, item] of keys) {
      const row = document.createElement("div");
      row.className = "key-row";
      const labelEl = document.createElement("div");
      labelEl.className = "key-label";
      const nameEl = document.createElement("span");
      nameEl.className = "key-name";
      nameEl.textContent = item.description || key;
      const keyEl = document.createElement("code");
      keyEl.className = "key-code";
      keyEl.textContent = `${group}.${key}`;
      const hintEl = document.createElement("div");
      hintEl.className = "hint";
      hintEl.textContent = item.hint || "";
      labelEl.append(nameEl, keyEl, hintEl);
      if (isDanger(group, key)) {
        const chip = document.createElement("span");
        chip.className = "danger-chip";
        chip.textContent = "⚠ 危险";
        chip.title = "改错会导致它作息失序或烧钱，不确定就别动";
        nameEl.prepend(chip);
        row.classList.add("danger-row");
      }
      // M18-补丁1 D2：旋钮映射目标键——新手区切档位会覆盖专家区的手改
      if (KNOB_MAPPED_KEYS.has(`${group}.${key}`)) {
        const chip = document.createElement("span");
        chip.className = "mapped-chip";
        chip.textContent = "档位联动";
        chip.title = "新手区对应的档位旋钮会写入这个键；在新手区切换档位时，" +
          "你在这里改的值会被档位映射覆盖";
        nameEl.prepend(chip);
      }
      const ctrl = document.createElement("div");
      ctrl.className = "key-control";
      buildControl(group, key, item, ctrl);
      row.append(labelEl, ctrl);
      body_el.appendChild(row);
    }

    // M20-补丁1 J：语料库/素材库管理挂在 style_learning 抽屉末尾
    if (group === "style_learning") {
      body_el.appendChild(styleLibraryAdmin());
    }

    drawer.append(head, body_el);
    list.appendChild(drawer);
  }
}

/* ---------------- 心境与兴趣（M9-补丁1 B5：专家视图专属） ----------------
 * 运行时状态（mood.db），不是配置键——读写走独立端点，逐项即时提交
 * （weight 改动/删除/清空都立刻生效，不进顶部"保存改动"的配置差量流）。
 * 提交方式二选一里选了逐项即时：兴趣是诊断级运行状态，改一项立即生效
 * 比攒着一起保存更直观，也不与配置保存的热重载互相干扰。
 *
 * M14-补丁2 E：请求层 bridge 化（更正 M9-补丁2 的历史结论）。AstrBot 4.28
 * 的插件页 iframe sandbox 属性为 "allow-scripts allow-forms allow-downloads"
 * ——没有 allow-same-origin，页面 origin 是 opaque：相对路径 fetch 必然
 * 抛异常、localStorage 被禁。当年"bridge 转发失配、同源 fetch 始终正常"
 * 的结论与现状硬证据矛盾——配置区 bridge.apiGet("config") 一直正常工作
 * （bridge 本身可用），当时"未找到该路由"极可能是 endpoint 传了完整
 * /api/v1/... 路径被父窗口二次拼接所致。bridge 契约：apiGet/apiPost 的
 * endpoint 用插件内相对路径（"mood"/"mood/interests"，不带 /api/v1 前缀，
 * 父窗口自动拼 /api/v1/plugins/extensions/<插件名>/<endpoint> 并自带
 * 鉴权代发）。策略：bridge 优先，fetch 同源直连保留为回退（老版本
 * AstrBot / 非 sandbox 环境）；两路共用同一套错误文案（含 401 专用）。 */

const PLUGIN_API_BASE = "/api/v1/plugins/extensions/astrbot_plugin_living";

function authToken() {
  try {
    return localStorage.getItem("token") || "";
  } catch (e) {
    return ""; // 沙箱禁用 localStorage：裸请求，401 文案兜底
  }
}

/* 心境端点统一请求：bridge 优先（M14-补丁2 E1）+ fetch 回退（E2）；
 * 401 专用文案与其余服务端 message 两路共用。 */
async function moodRequest(path, options = {}) {
  const page = window.AstrBotPluginPage;
  const endpoint = String(path || "").replace(/^\//, ""); // 插件内相对路径
  const isPost = (options.method || "GET").toUpperCase() !== "GET";
  const apiFn = isPost ? page && page.apiPost : page && page.apiGet;
  if (typeof apiFn === "function") {
    try {
      const raw = isPost
        ? await apiFn.call(page, endpoint,
            options.body ? JSON.parse(options.body) : {})
        : await apiFn.call(page, endpoint);
      // 兼容已解包/未解包两种 bridge 形态，避免二次解包错位
      const payload = raw && raw.data ? raw.data : raw;
      if (payload && payload.status === "error") {
        throw Object.assign(
          new Error(payload.message || "请求失败"), { definitive: true }
        );
      }
      // 与 fetch 分支同契约：返回带 data 字段的响应体
      return { status: "ok", data: payload };
    } catch (e) {
      if (e && e.definitive) throw e; // 服务端明确报错：如实上抛，不回退
      /* bridge 失败（SDK 缺方法/鉴权抛错/解析失败）→ 落到下方 fetch 回退 */
    }
  }
  const headers = { ...(options.headers || {}) };
  const token = authToken();
  if (token) headers["Authorization"] = `Bearer ${token}`;
  let resp;
  try {
    resp = await fetch(`${PLUGIN_API_BASE}${path}`, { ...options, headers });
  } catch (e) {
    throw new Error("网络错误：无法连接 dashboard");
  }
  let body = null;
  try {
    body = await resp.json();
  } catch (e) { /* 空响应体按 null 处理 */ }
  if (resp.status === 401) {
    throw new Error("登录已过期，请重新登录 dashboard");
  }
  if (!resp.ok || (body && body.status === "error")) {
    throw new Error(
      (body && body.message) || `请求失败（HTTP ${resp.status}）`
    );
  }
  return body;
}

const MOOD_STATS = [
  // [字段, 标签, 格式化]；fatigue/sleep_debt 是 0-100，其余 0-1（valence -1~1）
  ["energy", "精力", (v) => v.toFixed(2)],
  ["fatigue", "疲惫", (v) => `${v.toFixed(1)}/100`],
  ["valence", "心情", (v) => `${v >= 0 ? "+" : ""}${v.toFixed(2)}`],
  ["arousal", "唤醒度", (v) => v.toFixed(2)],
  ["sleep_debt", "睡眠债", (v) => `${v.toFixed(1)}/100`],
];

/* 清空按钮的内联确认态：第一次点变"确认清空？"，3 秒未点自动恢复
 * （Pages 沙箱没有 confirm()）；期间再点才真正执行。 */
function armInlineConfirm(btn, onConfirm) {
  let armed = false;
  let timer = null;
  const disarm = () => {
    armed = false;
    clearTimeout(timer);
    btn.textContent = "清空全部";
    btn.classList.remove("armed");
  };
  btn.addEventListener("click", () => {
    if (!armed) {
      armed = true;
      btn.textContent = "确认清空？";
      btn.classList.add("armed");
      timer = setTimeout(disarm, 3000);
      return;
    }
    disarm();
    onConfirm();
  });
}

async function renderMoodSection() {
  const host = $("#mood-section");
  let snap;
  try {
    const body = await moodRequest("/mood");
    snap = body && body.data ? body.data : null;
    if (!snap) throw new Error("响应缺少 data");
  } catch (e) {
    // 心境读取失败不影响上面的配置抽屉，只在区块内提示
    host.innerHTML = "";
    const err = document.createElement("p");
    err.className = "mood-error";
    err.textContent = `心境状态读取失败：${e && e.message ? e.message : e}`;
    host.appendChild(err);
    return;
  }

  const drawer = document.createElement("div");
  drawer.className = "drawer mood-drawer";

  const interests = Object.entries(snap.interests || {}).sort(
    (a, b) => b[1] - a[1]
  );
  const head = document.createElement("button");
  head.type = "button";
  const expanded = !!state.expanded.__mood;
  head.className = "drawer-head" + (expanded ? " open" : "");
  head.innerHTML = `<span class="arrow">${expanded ? "▾" : "▸"}</span> ` +
    `心境与兴趣 <span class="group-name">mood</span>` +
    `<span class="count">${interests.length} 项兴趣</span>`;
  const body_el = document.createElement("div");
  body_el.className = "drawer-body" + (expanded ? "" : " hidden");
  head.addEventListener("click", () => {
    const next = !state.expanded.__mood;
    state.expanded.__mood = next;
    saveExpanded(state.expanded);
    head.classList.toggle("open", next);
    head.querySelector(".arrow").textContent = next ? "▾" : "▸";
    body_el.classList.toggle("hidden", !next);
  });

  // 只读区：五项状态（该由活动和睡眠自然涨落，不做编辑入口）
  const stats = document.createElement("div");
  stats.className = "mood-stats";
  for (const [key, label, fmt] of MOOD_STATS) {
    const item = document.createElement("div");
    item.className = "mood-stat";
    const lab = document.createElement("div");
    lab.className = "stat-label";
    lab.textContent = label;
    const val = document.createElement("div");
    val.className = "stat-value";
    val.textContent = fmt(Number(snap[key]) || 0);
    item.append(lab, val);
    stats.appendChild(item);
  }
  body_el.appendChild(stats);

  const moodHint = document.createElement("p");
  moodHint.className = "hint";
  moodHint.textContent =
    "兴趣权重影响他自主选话题的倾向。改动立即生效（无需保存）；清空后" +
    "会随活动和时间重新自然积累。";
  body_el.appendChild(moodHint);

  // 兴趣权重表：topic 只读 + weight 可编辑 + 删除；逐项即时提交
  const list = document.createElement("div");
  list.className = "mood-interests";
  if (!interests.length) {
    const empty = document.createElement("p");
    empty.className = "mood-empty";
    empty.textContent = "暂无兴趣记录。";
    list.appendChild(empty);
  }
  for (const [topic, weight] of interests) {
    const row = document.createElement("div");
    row.className = "mood-interest-row";

    const name = document.createElement("span");
    name.className = "mood-topic";
    name.textContent = topic;
    name.title = topic;

    const input = document.createElement("input");
    input.type = "number";
    input.className = "mood-weight";
    input.min = "0";
    input.max = "1";
    input.step = "0.01";
    input.value = String(weight);
    input.addEventListener("change", async () => {
      const num = Number(input.value);
      if (input.value === "" || Number.isNaN(num) || num < 0 || num > 1) {
        toast(`权重需在 0~1 之间（${topic}）`, true);
        input.value = String(weight);
        return;
      }
      try {
        const resp = await moodRequest("/mood/interests", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ action: "set", topic, weight: num }),
        });
        toast(`已保存：${topic}`);
        input.value = String(num);
      } catch (e) {
        toast(`保存失败：${e && e.message ? e.message : e}`, true);
        input.value = String(weight);
      }
    });

    const del = document.createElement("button");
    del.type = "button";
    del.className = "btn mood-del";
    del.textContent = "删除";
    del.addEventListener("click", async () => {
      try {
        await moodRequest("/mood/interests", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ action: "delete", topic }),
        });
        toast(`已删除：${topic}`);
        renderMoodSection(); // 重拉：计数/空态/排序一并刷新
      } catch (e) {
        toast(`删除失败：${e && e.message ? e.message : e}`, true);
      }
    });

    row.append(name, input, del);
    list.appendChild(row);
  }
  body_el.appendChild(list);

  // 底部清空（有内容才出现；内联二次确认）
  if (interests.length) {
    const clearBtn = document.createElement("button");
    clearBtn.type = "button";
    clearBtn.className = "btn danger mood-clear";
    clearBtn.textContent = "清空全部";
    armInlineConfirm(clearBtn, async () => {
      try {
        await moodRequest("/mood/interests", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ action: "clear" }),
        });
        toast("已清空全部兴趣");
        renderMoodSection();
      } catch (e) {
        toast(`清空失败：${e && e.message ? e.message : e}`, true);
      }
    });
    body_el.appendChild(clearBtn);
  }

  drawer.append(head, body_el);
  host.innerHTML = "";
  host.appendChild(drawer);
}

/* ---------------- 保存 / 重置 / 切换 ---------------- */

async function save() {
  try {
    const payload = buildSavePayload();
    if (!Object.keys(payload).length) {
      toast("没有需要保存的改动");
      return;
    }
    const result = await bridge.apiPost("config", payload);
    const body = result && result.data ? result : result;
    if (body && body.status === "error") {
      toast(body.message || "保存失败", true);
      return;
    }
    toast((body && body.message) || "已保存");
    setDirty(false);
    // 保存可能触发插件热重载（后端短暂失联）——延迟重拉一次最新值
    setTimeout(async () => {
      try {
        const data = await bridge.apiGet("config");
        const fresh = data && data.data ? data.data : data;
        state.values.knobs = { ...(fresh.knobs || {}) };
        state.values.advanced = JSON.parse(JSON.stringify(fresh.advanced || {}));
        state.loaded = JSON.parse(JSON.stringify(state.values));
        renderNovice();
        renderExpert();
        setDirty(false);
      } catch { /* 失联重试失败不打扰用户，当前编辑仍在 */ }
    }, 1500);
  } catch (e) {
    toast(`保存失败：${e && e.message ? e.message : e}`, true);
  }
}

async function reset() {
  const ok = await confirmModal(
    "确定恢复全部默认值吗？你在新手和专家区的所有修改（含高级调参）都会被重置。"
  );
  if (!ok) return;
  try {
    const result = await bridge.apiPost("config/reset", {});
    const body = result && result.data ? result : result;
    if (body && body.status === "error") {
      toast(body.message || "恢复失败", true);
      return;
    }
    toast("已恢复默认值");
    await load();
  } catch (e) {
    toast(`恢复失败：${e && e.message ? e.message : e}`, true);
  }
}

function switchView(view) {
  $("#view-novice").classList.toggle("hidden", view !== "novice");
  $("#view-expert").classList.toggle("hidden", view !== "expert");
  $("#tab-novice").classList.toggle("active", view === "novice");
  $("#tab-expert").classList.toggle("active", view === "expert");
  // M9-补丁1 B5：心境是运行时状态，每次进专家视图都重拉最新快照
  if (view === "expert") renderMoodSection();
}

/* ---------------- 入口 ---------------- */

const bridge = await waitBridge();

/* ---------------- M20-补丁1 J：语料库与素材库管理 ----------------
 * 读写走独立端点（style_data/style_corpus/style_materials/style_process），
 * 即时提交，不进顶部"保存改动"的配置差量流。红线：这些库与记忆库物理
 * 隔离，面板的任何写入都不触碰记忆。 */

async function fetchStyleData() {
  const resp = await bridge.apiGet("style_data");
  if (resp && resp.status === "error") throw new Error(resp.message || "读取失败");
  const data = resp && resp.data ? resp.data : resp;
  return {
    corpus: (data && data.corpus) || [],
    materials: (data && data.materials) || [],
    features: (data && data.features) || [],
    usage: (data && data.usage) || [],
    meta: (data && data.meta) || {},
  };
}

const STYLE_RESULT_LABELS = {
  learned: "已提炼入库",
  verdict_ai: "判定像 AI 写的，没学",
  verdict_uncertain: "拿不准，没学",
  no_dims: "没提炼出可学片段",
  too_short: "内容太短",
  duplicate: "语料库已有同内容",
};

function styleResultBadge(material) {
  const span = document.createElement("span");
  span.className = "style-badge";
  if (!material.processed) {
    span.textContent = "未处理";
    span.classList.add("pending");
  } else if (material.result === "learned") {
    span.textContent = "已处理 → 入库";
    span.classList.add("ok");
  } else {
    span.textContent = "已处理：" + (STYLE_RESULT_LABELS[material.result] || material.result || "未学到");
    span.classList.add("done");
  }
  return span;
}

function styleAddForm(onChanged) {
  const wrap = document.createElement("div");
  wrap.className = "style-add";
  const ta = document.createElement("textarea");
  ta.rows = 3;
  ta.placeholder = "粘贴你希望她学的说话语料（评论区、聊天记录、一段文字…）";
  const note = document.createElement("input");
  note.type = "text";
  note.placeholder = "备注（可选，比如来源）";
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "link-button";
  btn.textContent = "添加素材";
  btn.addEventListener("click", async () => {
    if (!ta.value.trim()) { toast("素材内容不能为空", true); return; }
    btn.disabled = true;
    try {
      const resp = await bridge.apiPost("style_materials", {
        action: "add", text: ta.value, note: note.value,
      });
      if (resp && resp.status === "error") { toast(resp.message || "添加失败", true); return; }
      ta.value = ""; note.value = "";
      toast("素材已添加（活动结束或点「立即处理」时提炼）");
      if (onChanged) onChanged();
    } catch (e) {
      toast("添加失败：" + e, true);
    } finally {
      btn.disabled = false;
    }
  });
  wrap.append(ta, note, btn);
  return wrap;
}

function styleProcessButton(onChanged) {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "link-button";
  btn.textContent = "立即处理";
  btn.title = "不等下一次活动，立刻提炼素材库里的待处理素材（30 秒限频）";
  btn.addEventListener("click", async () => {
    btn.disabled = true;
    btn.textContent = "处理中…";
    try {
      const resp = await bridge.apiPost("style_process", {});
      const message = resp && resp.message ? resp.message : "完成";
      toast(message, resp && resp.status === "error");
      if (onChanged) onChanged();
    } catch (e) {
      toast("处理失败：" + e, true);
    } finally {
      btn.disabled = false;
      btn.textContent = "立即处理";
    }
  });
  return btn;
}

function styleMaterialsList(materials, onChanged) {
  const wrap = document.createElement("div");
  wrap.className = "style-list";
  if (!materials.length) {
    const empty = document.createElement("p");
    empty.className = "hint";
    empty.textContent = "素材库还是空的。把你希望她学的语料粘贴进去（上面的输入框）。";
    wrap.appendChild(empty);
    return wrap;
  }
  for (const m of materials) {
    const row = document.createElement("div");
    row.className = "style-row";
    const excerpt = document.createElement("div");
    excerpt.className = "style-excerpt";
    excerpt.textContent = String(m.text || "").slice(0, 80) + (String(m.text || "").length > 80 ? "…" : "");
    const meta = document.createElement("div");
    meta.className = "style-meta";
    const noteText = m.note ? `备注：${m.note}` : "";
    meta.append(styleResultBadge(m));
    if (noteText) {
      const noteSpan = document.createElement("span");
      noteSpan.textContent = " " + noteText;
      meta.appendChild(noteSpan);
    }
    const del = document.createElement("button");
    del.type = "button";
    del.className = "link-button danger";
    del.textContent = "删除";
    del.addEventListener("click", async () => {
      const resp = await bridge.apiPost("style_materials", { action: "delete", id: m.id });
      if (resp && resp.status === "error") { toast(resp.message || "删除失败", true); return; }
      toast("已删除");
      if (onChanged) onChanged();
    });
    row.append(excerpt, meta, del);
    wrap.appendChild(row);
  }
  return wrap;
}

function styleCorpusList(corpus, { editable = false, onChanged = null } = {}) {
  const wrap = document.createElement("div");
  wrap.className = "style-list";
  if (!corpus.length) {
    const empty = document.createElement("p");
    empty.className = "hint";
    empty.textContent = "语料库还是空的——她读完网页学到第一条风格后，会出现在这里。";
    wrap.appendChild(empty);
    return wrap;
  }
  for (const entry of corpus) {
    const row = document.createElement("div");
    row.className = "style-row";
    const dims = entry.dims || {};
    const dimText = Object.entries(dims)
      .map(([k, v]) => `${k}: ${v}`)
      .join("；");
    const main = document.createElement("div");
    main.className = "style-excerpt";
    main.textContent = dimText || "（无内容）";
    const meta = document.createElement("div");
    meta.className = "style-meta";
    const kind = entry.manual ? "人工优选" : (entry.source_kind === "dialogue" ? "实战对话" : "文章");
    const reviewNote = entry.review_note ? `，最近判定：${entry.review_note}` : "";
    meta.appendChild(document.createTextNode(
      `${kind} · 学于 ${(entry.learned_at || "").slice(0, 10)} · 取用 ${entry.used_count || 0} 次 · 重要度 ${entry.importance ?? 1} · 留存度 ${entry.retention ?? 1}${reviewNote}`
    ));
    row.append(main, meta);
    if (editable) {
      const actions = document.createElement("div");
      actions.className = "style-actions";
      const editBtn = document.createElement("button");
      editBtn.type = "button";
      editBtn.className = "link-button";
      editBtn.textContent = "编辑";
      editBtn.addEventListener("click", () => {
        if (row.querySelector("textarea")) return; // 已在编辑
        const ta = document.createElement("textarea");
        ta.rows = 4;
        ta.value = JSON.stringify(dims, null, 1);
        const saveBtn = document.createElement("button");
        saveBtn.type = "button";
        saveBtn.className = "link-button";
        saveBtn.textContent = "保存";
        saveBtn.addEventListener("click", async () => {
          let dimsNext;
          try { dimsNext = JSON.parse(ta.value || "{}"); }
          catch { toast("不是合法 JSON", true); return; }
          const resp = await bridge.apiPost("style_corpus", {
            action: "update", id: entry.id, dims: dimsNext,
          });
          if (resp && resp.status === "error") { toast(resp.message || "保存失败", true); return; }
          toast("已保存");
          if (onChanged) onChanged();
        });
        const cancelBtn = document.createElement("button");
        cancelBtn.type = "button";
        cancelBtn.className = "link-button";
        cancelBtn.textContent = "取消";
        cancelBtn.addEventListener("click", () => {
          editor.remove();
          actions.querySelectorAll("button").forEach((b) => (b.disabled = false));
        });
        const editor = document.createElement("div");
        editor.className = "style-editor";
        editor.append(ta, saveBtn, cancelBtn);
        row.appendChild(editor);
        actions.querySelectorAll("button").forEach((b) => (b.disabled = b !== editBtn ? true : b.disabled));
        editBtn.disabled = true;
      });
      const delBtn = document.createElement("button");
      delBtn.type = "button";
      delBtn.className = "link-button danger";
      delBtn.textContent = "删除";
      delBtn.addEventListener("click", async () => {
        const resp = await bridge.apiPost("style_corpus", { action: "delete", id: entry.id });
        if (resp && resp.status === "error") { toast(resp.message || "删除失败", true); return; }
        toast("已删除");
        if (onChanged) onChanged();
      });
      actions.append(editBtn, delBtn);
      row.appendChild(actions);
    }
    wrap.appendChild(row);
  }
  return wrap;
}

function styleFeaturesList(features) {
  const wrap = document.createElement("div");
  wrap.className = "style-list";
  if (!features.length) {
    const empty = document.createElement("p");
    empty.className = "hint";
    empty.textContent = "沉淀层还没有内容——等每日复盘累计足够好评后，会归纳出她的稳定说话方式。";
    wrap.appendChild(empty);
    return wrap;
  }
  for (const feature of features) {
    const row = document.createElement("div");
    row.className = "style-row";
    const dims = feature.dims || {};
    const main = document.createElement("div");
    main.className = "style-excerpt";
    main.textContent = Object.entries(dims).map(([k, v]) => `${k}: ${v}`).join("；");
    const meta = document.createElement("div");
    meta.className = "style-meta";
    meta.textContent = `沉淀于 ${(feature.created_at || "").slice(0, 10)} · 重要度 ${feature.importance ?? 1}`;
    row.append(main, meta);
    wrap.appendChild(row);
  }
  return wrap;
}

function styleClearButton(label, endpoint, onChanged) {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "link-button danger";
  btn.textContent = label;
  btn.addEventListener("click", async () => {
    const yes = await confirmModal(`确定要${label}吗？此操作不可恢复。`);
    if (!yes) return;
    const resp = await bridge.apiPost(endpoint, { action: "clear", confirm: true });
    if (resp && resp.status === "error") { toast(resp.message || "操作失败", true); return; }
    toast(resp.message || "已清空");
    if (onChanged) onChanged();
  });
  return btn;
}

/* 专家库管理：挂在 style_learning 抽屉末尾（查看/编辑/删除/清空全量） */
function styleLibraryAdmin() {
  const box = document.createElement("div");
  box.className = "key-row style-admin";
  const render = async () => {
    box.innerHTML = "";
    const label = document.createElement("div");
    label.className = "key-label";
    const name = document.createElement("span");
    name.className = "key-name";
    name.textContent = "语料库 / 素材库管理";
    const hint = document.createElement("div");
    hint.className = "hint";
    hint.textContent = "语料库=她学到的风格片段；素材库=你手动投入的原始语料；调用记录=取用事实（不进记忆）。";
    label.append(name, hint);
    const ctrl = document.createElement("div");
    ctrl.className = "key-control style-admin-body";
    try {
      const data = await fetchStyleData();
      const corpusHead = document.createElement("h4");
      corpusHead.textContent = `语料库（${data.corpus.length} 条）`;
      ctrl.append(
        corpusHead,
        styleCorpusList(data.corpus, { editable: true, onChanged: render }),
        styleClearButton("清空语料库", "style_corpus", render),
        document.createElement("h4"),
      );
      ctrl.lastChild.textContent = `素材库（${data.materials.length} 条）`;
      ctrl.append(
        styleAddForm(render),
        styleMaterialsList(data.materials, render),
        styleProcessButton(render),
        styleClearButton("清空素材库", "style_materials", render),
        document.createElement("h4"),
      );
      ctrl.lastChild.textContent = `沉淀层（${data.features.length} 条）`;
      ctrl.append(
        styleFeaturesList(data.features),
        document.createElement("h4"),
      );
      ctrl.lastChild.textContent = `最近取用（${data.usage.length} 条）`;
      const usageList = document.createElement("div");
      usageList.className = "style-list";
      for (const record of data.usage.slice(0, 20)) {
        const line = document.createElement("div");
        line.className = "style-meta";
        line.textContent = `${(record.ts || "").slice(0, 16)} [${record.trigger}] ${record.entry_ids.join("、")}`;
        usageList.appendChild(line);
      }
      ctrl.append(usageList);
    } catch (e) {
      const err = document.createElement("div");
      err.className = "control-warn";
      err.textContent = "读取失败：" + e;
      ctrl.appendChild(err);
    }
    box.append(label, ctrl);
  };
  render();
  return box;
}

/* 新手卡：语料与素材（查看 / 添加 / 立即处理） */
function styleDataCard() {
  const card = document.createElement("div");
  card.className = "knob-card style-card";
  const title = document.createElement("h3");
  title.textContent = "语料与素材（她想学谁的说话方式，你说了算）";
  card.appendChild(title);
  const hint = document.createElement("p");
  hint.className = "hint";
  hint.textContent = "把你想让她学的语料丢进素材库，她下次活动结束（或你点「立即处理」）就会提炼成自己的说话方式。学到的都存在语料库里，随时可看可删。";
  card.appendChild(hint);

  const body = document.createElement("div");
  card.appendChild(body);
  const render = async () => {
    body.innerHTML = "";
    body.appendChild(styleAddForm(render));
    try {
      const data = await fetchStyleData();
      const pending = data.materials.filter((m) => !m.processed).length;
      const head = document.createElement("div");
      head.className = "style-head-row";
      const summary = document.createElement("span");
      summary.textContent = `素材库 ${data.materials.length} 条（待处理 ${pending}） · 语料库 ${data.corpus.length} 条`;
      head.append(summary, styleProcessButton(render));
      body.appendChild(head);
      body.appendChild(styleMaterialsList(data.materials, render));
      const corpusHead = document.createElement("h4");
      corpusHead.textContent = `她学到的语料（${data.corpus.length} 条）`;
      body.appendChild(corpusHead);
      body.appendChild(styleCorpusList(data.corpus, { editable: false }));
    } catch (e) {
      const err = document.createElement("div");
      err.className = "control-warn";
      err.textContent = "读取失败：" + e;
      body.appendChild(err);
    }
  };
  render();
  return card;
}

async function boot() {
  $("#tab-novice").addEventListener("click", () => switchView("novice"));
  $("#tab-expert").addEventListener("click", () => switchView("expert"));
  $("#btn-save").addEventListener("click", save);
  $("#btn-reset").addEventListener("click", reset);
  try {
    await load();
  } catch (e) {
    showError(`配置读取失败：${e && e.message ? e.message : e}（请刷新重试）`);
  }
}

boot();
