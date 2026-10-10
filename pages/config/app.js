/* astrbot_plugin_living —— 配置面板逻辑（M5 补丁 1）
 * 运行在 AstrBot Plugin Pages 受限 iframe 中，通过 window.AstrBotPluginPage
 * bridge 调用后端（自动携带 dashboard 鉴权）。bridge 仅提供 GET/POST。
 * 注意：Pages 沙箱忽略 window.confirm/alert/prompt——确认动作用页内弹层。 */

/* M25-补丁1 C4-C8：状态引擎（四态/生效链/出口行/归组）是纯函数模块，
 * pytest 经 node 桥（tests/js/status_engine_harness.mjs）行为级测试同一份代码。 */
import {
  STATUS_META,
  computeCounts,
  computeExitLines,
  computeKeyStatus,
  computeNodeStatus,
  novicePlan,
  renderChain,
  runnableActivities,
} from "./status-engine.js";
import { LIVING_HELP } from "./help-content.js"; // M28-补丁1：内置说明书内容（纯数据）

/* M25-补丁1 配套 a：新手旋钮的展示顺序不再由本文件的 KNOB_ORDER 硬编码
 * （已随重排移除）——归组与顺序统一来自 panel_layout.json（后端透传
 * payload.layout），数据单一权威，前端不再两处维护。 */

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
    probability: "投骰子", llm: "AstrBot 自己斟酌", off: "不说",
  },
  "sleep.wake_source": { all: "所有人", owner_only: "只算用户" },
  "capabilities.agent_tools_mode": {
    off: "关闭", persona: "跟随人格", custom: "自定义白名单",
  },
  "memory.backend": { auto: "自动", livingmemory: "记忆库", simple: "简易" },
  "judge.mode": { off: "关闭", local: "本地（未实现）", api: "云端 API" },
  "judge.output_action": {
    log_only: "只记录", negotiate: "协商改稿", rewrite: "代笔替换（不推荐）",
  },
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
 * 用户反馈原话"没有说明用户肯定不知道怎么写"）。 */
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

/* M27-补丁1 7.3：本体工具白名单的中文说明（此前只列英文工具名，用户看
 * 不懂 recall_long_term_memory 这类名字）。映射表只收确定含义的工具名，
 * 注册表里的其它工具原样列名；注册表里没有但已配置的值保留显示并标注
 * （不静默丢弃用户已存的值）。保存格式不变：仍是逗号分隔字符串。 */
const AGENT_TOOL_DESCRIPTIONS = {
  recall_long_term_memory: "回忆长期记忆",
  web_search: "联网搜索",
  fetch_page: "抓取网页内容",
};

/* M20-补丁1 F3：initiative.sources 的取值固定（两个来源）——改多选。 */
const INITIATIVE_SOURCE_CHOICES = [
  { value: "random_miss", label: "random_miss：你没回消息时，AstrBot 可能会惦记" },
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

/* M34-补丁1 B 组：档位一致性判定（只提示，绝不回写——回写会被旋钮监视
 * 当成"用户改档"再正向覆盖，把专家页的手工微调抹掉）。
 * 数据源是 payload 下发的映射表（knob_presets / knob_direct，同一份来源
 * core/config_knobs.py），前端不做第二份硬编码。 */
function knobEffectiveValue(group, key) {
  /* 底层键的当前值：编辑值优先，键不在存储里时退 schema 默认（未设置 =
  按默认生效）；连 schema 定义都没有才返回 undefined（无从判定）。 */
  const adv = state.values.advanced[group];
  if (adv && typeof adv === "object" && adv[key] !== undefined && adv[key] !== null) {
    return adv[key];
  }
  const item = ((((state.schema || {}).advanced || {}).items[group] || {}).items || {})[key];
  return item && item.default !== undefined ? item.default : undefined;
}

/* 单个旋钮：新手页显示值与实际生效配置是否不一致。
 * 返回 true=不一致（亮提示）/ false=一致（不打扰）/ null=无法判定（不亮）。
 * 档位类整组比对——多对一旋钮只要有一个键没命中就算不一致；直通类
 * （preset_model）比较旋钮值与 model.provider_id。值经 String() 规范化，
 * 数值 0.8 与 "0.8" 视为相等；布尔与数字靠字符串形态区分。 */
function knobMismatch(name) {
  if (!state.knobPresets || typeof state.knobPresets !== "object") return null;
  if (name === state.knobDirect) {
    const actual = knobEffectiveValue("model", "provider_id");
    if (actual === undefined) return null;
    return String(state.values.knobs[name] ?? "") !== String(actual);
  }
  const shown = state.values.knobs[name];
  const defs = (state.knobPresets[name] || {})[shown];
  if (!defs) return null; // 显示值不在预设表（空/未知档）：无从比对
  for (const [group, keys] of Object.entries(defs)) {
    for (const [key, defined] of Object.entries(keys)) {
      const actual = knobEffectiveValue(group, key);
      if (actual === undefined) return true; // 实际值无从得知：按不一致（保守亮出）
      if (String(actual) !== String(defined)) return true;
    }
  }
  return false;
}

/* 专家页单键：该键当前值是否仍在"新手页显示档"的定义值内。
 * 返回 "in"=仍在档内 / "off"=已脱离 / null=无法判定（保持通用文案）。 */
function mappedKeyKnobStatus(group, key) {
  if (!state.knobPresets || typeof state.knobPresets !== "object") return null;
  if (`${group}.${key}` === "model.provider_id") {
    const actual = knobEffectiveValue(group, key);
    if (actual === undefined) return null;
    return String(state.values.knobs[state.knobDirect] ?? "") === String(actual)
      ? "in" : "off";
  }
  for (const [name, tiers] of Object.entries(state.knobPresets)) {
    const defs = (tiers || {})[state.values.knobs[name]];
    if (!defs) continue; // 该旋钮当前显示档无从比对——换下一个旋钮
    const defined = (defs[group] || {})[key];
    if (defined === undefined) continue; // 这个键不归当前旋钮管
    const actual = knobEffectiveValue(group, key);
    if (actual === undefined) return null;
    return String(actual) === String(defined) ? "in" : "off";
  }
  return null;
}

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
  layout: null, // M25-补丁1 C2：payload.layout（panel_layout.json，可 null → 回退扁平渲染）
  runtime: {}, // M25-补丁1：运行时谓词（browser_installed/workspace_ready/providers；拉取失败保持 undefined → 状态引擎按"未知"处理，不猜）
  statusKeys: new Set(), // M27-补丁1 7.2：状态相关键集合（load 时从 layout 收集）
  dirty: false,
  expanded: loadExpanded(),
};

/* M27-补丁1 7.2：状态相关键集合——layout 声明里被 switch / requires /
 * 出口条件引用到的键。只有这些键的值变化才触发状态点重算（整体重渲染），
 * 普通调参（预算数字、开关细项）不重渲染，避免无谓的全页重建。 */
function statusRelevantKeys(layout) {
  const keys = new Set();
  const walkCond = (cond) => {
    if (!cond || typeof cond !== "object") return;
    if (cond.key) keys.add(cond.key);
    for (const sub of cond.of || []) walkCond(sub);
  };
  for (const sec of (layout || {}).sections || []) {
    for (const g of sec.groups || []) {
      if (g.switch) keys.add(g.switch);
      for (const r of g.requires || []) walkCond(r);
    }
  }
  for (const e of (((layout || {}).exits || {}).items) || []) {
    walkCond(e.cond);
    for (const r of e.requires || []) walkCond(r);
  }
  return keys;
}

/* M27-补丁1 7.2：受影响键的值提交后实时重算状态点——整体重渲染专家视图
 * 与全局状态行（顺带刷新新手卡上的常驻生效链）。滚动位置重渲染前捕获、
 * 渲染后恢复；抽屉展开态持久化在 state.expanded（localStorage），树渲染
 * 逐组读取，天然保持。改值→重渲染同步发生在 change 提交点，不碰保存链路
 * （buildSavePayload/diffSection 零改动）。 */
function refreshStatusViews(fullKey) {
  if (!state.statusKeys || !state.statusKeys.has(fullKey)) return;
  const y = typeof window.scrollY === "number" ? window.scrollY : 0;
  renderNovice();
  renderExpert();
  renderGlobalStatus();
  if (typeof window.scrollTo === "function") window.scrollTo(0, y);
}

/* M20-补丁1 F1/A2/A4：provider 类字段的统一控件。
 * - 下拉选项从"已启用的 provider"动态生成（数据源 GET /config 的 providers）；
 * - 留空选项（语义随调用方不同）+ 缓存警告 + F1-b 手填兜底（填了不存在
 *   的 id 明确提示，不静默回退）；
 * - getter/setter 抽象：expert 的 model.provider_id 写 advanced，novice
 *   的 preset_model 写 knobs，同一控件两处复用；
 * - M27-补丁1 7.1：judge.provider_id 复用同款控件，留空的语义不同
 *   （model 留空 = 回退聊天模型；judge 留空 = 整条判断链不工作）——
 *   emptyStatus/emptyWarn/missingWarn 可按调用方覆盖，文案必须准确。 */
function providerPickerControl({ value, onChange, emptyLabel, emptyStatus, emptyWarn, missingWarn, effectiveValue }) {
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
    /* M34-补丁1 C 组：effectiveValue（getter）由调用方提供真正的实际生效值
     * （读 advanced 存储）——状态行如实显示它，而不是控件当前编辑值。
     * 缺省时显示编辑值：专家页两处控件编辑的正是该存储本身，语义等同。
     * 警告行始终跟随编辑中的选择（正在改什么就提醒什么）。 */
    const shown = typeof effectiveValue === "function"
      ? String(effectiveValue() ?? "") : current;
    if (!shown) {
      status.textContent = emptyStatus || "当前实际生效：聊天模型（共用账号）";
    } else if (!providers.includes(shown)) {
      status.textContent = `当前实际生效：${shown}（不在已启用列表中）`;
    } else {
      status.textContent = `当前实际生效：${shown}（独立 provider）`;
    }
    if (!current) {
      warn.textContent = emptyWarn
        || "⚠ 与聊天共用模型：AstrBot 做活动/搭话/分享的任何一次调用都会打断你聊天的缓存，聊天全部历史将按未命中重新计费。建议单独配一个 provider（最好用不同的 api key）。";
    } else if (!providers.includes(current)) {
      warn.textContent = missingWarn
        || "⚠ 该 provider id 不在已启用的 provider 列表里，调用时会按回退处理并在日志留痕。请核对拼写，或到 provider 管理页启用它。";
    } else {
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
  state.layout = payload.layout || null; // M25-补丁1 C2（null → 配套 h 回退扁平渲染）
  state.statusKeys = statusRelevantKeys(state.layout); // M27-补丁1 7.2
  state.providers = payload.providers || []; // M19-补丁1 F2/E3：provider 下拉数据源
  state.agent_tools = payload.agent_tools || []; // M20-补丁1 F3：本体工具多选数据源
  // M34-补丁1 A 组：旋钮映射表（payload 下发，与后端同一份来源）——档位
  // 一致性判定用；缺失（旧后端）时判定函数全部返回 null，面板行为与从前一致
  state.knobPresets = payload.knob_presets || null;
  state.knobDirect = payload.knob_direct || "preset_model";
  state.values.knobs = { ...(payload.knobs || {}) };
  state.values.advanced = JSON.parse(JSON.stringify(payload.advanced || {}));
  state.loaded = JSON.parse(JSON.stringify(state.values));
  setDirty(false);
  await loadRuntime(); // M25-补丁1：运行时谓词（失败按未知处理，不阻塞面板）
  renderNovice();
  renderExpert();
  renderGlobalStatus();
}

/* M25-补丁1：运行时谓词拉取——复用既有端点（browser_status /
 * workspace_status），零新增轮询；失败时对应值保持 undefined，
 * 状态引擎按"未知"处理（宁可标未知也不猜）。 */
async function loadRuntime() {
  const rt = { providers: state.providers || [] };
  try {
    const res = await bridge.apiGet("browser_status");
    const d = res && res.data ? res.data : res;
    rt.browser_installed = !!(d && d.installed);
  } catch (e) { /* 保持 undefined = 未知 */ }
  try {
    const res = await bridge.apiGet("workspace_status");
    const d = res && res.data ? res.data : res;
    rt.workspace_ready = !!(d && d.state === "ready");
  } catch (e) { /* 保持 undefined = 未知 */ }
  state.runtime = rt;
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

/* M25-补丁1 配套 a：新手面板重排——第一层只留 8 张旋钮（按 preset 键的
 * section 一级 id 归组、组间小标题）+ life_extra；13 张功能卡进「细项」
 * 折叠区（按新一级栏目归组）。归组数据来自 panel_layout.json（novicePlan，
 * 纯函数可测）；卡片的挂载保持逐张 grid.appendChild 形态。 */
function noviceSectionHead(title, sectionId) {
  const head = document.createElement("div");
  head.className = "novice-section-head";
  head.dataset.section = sectionId || "";
  head.textContent = title;
  return head;
}

function renderNovice() {
  const presetSchema = state.schema.preset.items;
  const grid = $("#novice-cards");
  grid.innerHTML = "";
  const plan = novicePlan(state.layout, presetSchema);

  // 首层：旋钮按一级栏目归组（AstrBot 怎么安排生活 / AstrBot 什么时候开口 / …）
  for (const g of plan.knobGroups) {
    grid.appendChild(noviceSectionHead(g.title, g.id));
    for (const name of g.knobs) {
      const item = presetSchema[name];
      if (!item) continue;
      const card = buildKnobCard(name, item);
      grid.appendChild(card);
    }
  }

  // 细项折叠开关（状态记忆在 state.expanded）
  const detailOpen = !!state.expanded.__novice_detail;
  grid.classList.toggle("show-detail", detailOpen);
  const toggleRow = document.createElement("div");
  toggleRow.className = "novice-detail-toggle";
  const toggleBtn = document.createElement("button");
  toggleBtn.type = "button";
  toggleBtn.className = "link-button";
  toggleBtn.textContent = detailOpen
    ? "▲ 收起细项设置"
    : `▼ 细项设置（${plan.cardGroups.reduce((n, g) => n + g.cards.length, 0)} 张功能卡，按栏目归组）`;
  toggleBtn.addEventListener("click", () => {
    const next = !grid.classList.contains("show-detail");
    grid.classList.toggle("show-detail", next);
    state.expanded.__novice_detail = next;
    saveExpanded(state.expanded);
    toggleBtn.textContent = next
      ? "▲ 收起细项设置"
      : `▼ 细项设置（${plan.cardGroups.reduce((n, g) => n + g.cards.length, 0)} 张功能卡，按栏目归组）`;
  });
  toggleRow.appendChild(toggleBtn);
  grid.appendChild(toggleRow);

  // 折叠区：13 张功能卡按新一级栏目归组 + 组间小标题（默认折叠）
  // 返工修正（M25-补丁1）：
  //   ① 组标题与该组卡片相邻显示——原实现先 append 全部标题再 append 全部
  //     卡，导致"标题全在前、卡片全在后"；现按组穿插（detailHeads 从
  //     plan.cardGroups 构建，与卡片工厂的显式调用次序一一对应）。
  //   ② novice-detail 标记只作用于 toggle 之后的功能卡（选择器
  //     .novice-detail-toggle ~ .knob-card），首层 preset 旋钮（buildKnobCard
  //     产出、在 toggle 之前）不被标记——修复新手页 8 个 preset 旋钮 +
  //     13 张功能卡全被 .novice-detail{display:none} 藏住、只剩小标题的 bug。
  const detailHeads = plan.cardGroups.map((g) => {
    const h = noviceSectionHead(g.title, g.id);
    h.classList.add("novice-detail");
    return h;
  });
  let hi = 0;
  grid.appendChild(detailHeads[hi++]); // A AstrBot 的大脑：判断模型
  grid.appendChild(judgeCard());
  grid.appendChild(detailHeads[hi++]); // B AstrBot 的手脚
  grid.appendChild(browserCard());
  grid.appendChild(workspaceCard());
  grid.appendChild(searchToggleCard());
  grid.appendChild(agentToolsCard());
  grid.appendChild(detailHeads[hi++]); // D AstrBot 什么时候开口
  grid.appendChild(initiativeCard()); // 主动搭话卡（M14-补丁2 F3）：D 组先于 E 组
  grid.appendChild(detailHeads[hi++]); // E AstrBot 的作息
  grid.appendChild(scheduleCard()); // 起床约定卡（M5-补丁4）
  grid.appendChild(farewellCard()); // 晚安消息（三档 + 概率滑块）
  grid.appendChild(chatGuardCard()); // 聊天时不睡觉
  grid.appendChild(wakeRandomCard()); // 随机吵醒（M17-补丁1 C1）
  grid.appendChild(pendingReplyCard()); // 醒来补回复（M17-补丁1 C2）
  grid.appendChild(detailHeads[hi++]); // F AstrBot 学你说话
  grid.appendChild(styleLearningCard()); // 风格学习（M17-补丁1 A5，卡上带生效链）
  grid.appendChild(styleDataCard()); // M20-补丁1 J：语料与素材（卡上带生效链）
  // 仅标记 toggle 之后的功能卡（.novice-detail-toggle ~ .knob-card）为
  // novice-detail；首层 preset 旋钮在 toggle 之前、不被标记、默认可见
  for (const card of grid.querySelectorAll(".novice-detail-toggle ~ .knob-card")) {
    if (!card.classList.contains("novice-detail")) {
      card.classList.add("novice-detail");
    }
  }
  renderLifeExtra(presetSchema);
}

/* 旋钮卡构建（M25-补丁1 从 renderNovice 内联提出，逻辑原样） */
function buildKnobCard(name, item) {
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
    // 让新手手打 id）+ 留空缓存警告。M34-补丁1 C 组：状态行经
    // effectiveValue 改绑真正的实际生效值（advanced 存储里的
    // model.provider_id）——专家页单独改过之后不再顶着"实际生效"报旋钮旧值
    card.appendChild(providerPickerControl({
      value: state.values.knobs[name] ?? "",
      onChange: (v) => { state.values.knobs[name] = v; setDirty(true); },
      emptyLabel: "（留空 = 与聊天共用模型）",
      effectiveValue: () => knobEffectiveValue("model", "provider_id") ?? "",
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
  // M34-补丁1 B 组：显示值与实际生效配置不一致时给只读提示（一致时
  // 不打扰；无法判定也不亮）。只提示不回写——回写会被旋钮监视当成
  // "用户改档"再正向覆盖，把专家页的手工微调抹掉
  if (knobMismatch(name)) {
    const note = document.createElement("p");
    note.className = "hint knob-mismatch-note";
    note.textContent =
      "⚠ 这里亮着的不是当前实际生效的设置——只代表你上次在这里选的，" +
      "实际值以专家页为准。想按这里的档位重新生效：先选一下别的，再选回来。";
    card.appendChild(note);
  }
  return card;
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
    "（详细/简短/带情绪），AstrBot 回复后再检查一次有没有越回越啰嗦——对抗" +
    "输出惯性。判断用的模型应该是又小又快的（便宜），跟聊天模型分开。" +
    "判断结果用完就丢，不会进它的记忆。";
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
      refreshStatusViews("judge.mode"); // M27-补丁1 7.2：A2 组头状态点跟着变
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
    refreshStatusViews("judge.provider_id"); // M27-补丁1 7.2
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

/* 晚安消息卡：不说 / 随机说（显示概率滑块）/ AstrBot 自己斟酌着说。
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
    "AstrBot 去睡觉时要不要跟你说声晚安。「AstrBot 自己斟酌着说」会让它睡前看一眼" +
    "今天你们聊得怎么样，再决定说不说、怎么说——聊得开心自然道晚安，" +
    "还在气头上可以不说，想和好也可以借这句说点什么。";
  card.appendChild(hint);

  const MODES = [
    ["不说", "off"],
    ["随机说", "probability"],
    ["AstrBot 自己斟酌着说", "llm"],
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
      refreshStatusViews("sleep.farewell_mode"); // M27-补丁1 7.2（E4 档位）
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
    "AstrBot 陪你聊天的时候不会当场睡着——你安静半小时后它才恢复入睡评估。" +
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
    "每次入睡时随机抽定「连发几条能吵醒它」（1-3 条中按睡眠深浅加权：" +
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
    "它睡着时你发的消息（没吵醒它的），它醒来后会自己看看要不要回：" +
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

/* M25-补丁1 配套 b：素材总结的生效链（用户点名的痛点——面板必须能看出
 * "素材库不是一直开着的"）。链声明取 layout 的 F2 组（语料与素材），
 * 与专家区 F2 组头同源；判定走状态引擎（总闸 × 自主大脑两环）。 */
function styleChainBlock() {
  const wrap = document.createElement("div");
  wrap.className = "chain-block";
  const sec = ((state.layout || {}).sections || []).find((s) => s.id === "F");
  const grp = sec && (sec.groups || []).find((g) => g.id === "F2");
  if (!grp) return wrap; // 无布局（配套 h 回退）时不渲染链，卡片其余部分不受影响
  const chain = renderChain("素材总结的生效链", grp, expertBuildCtx());
  const title = document.createElement("div");
  title.className = "chain-title";
  title.textContent = chain.title;
  wrap.appendChild(title);
  for (const line of chain.lines) {
    const row = document.createElement("div");
    row.className = "chain-line";
    row.textContent = line;
    wrap.appendChild(row);
  }
  const conclusion = document.createElement("div");
  conclusion.className = `chain-conclusion ${STATUS_META[chain.status]?.cls || ""}`;
  conclusion.textContent = chain.conclusion;
  wrap.appendChild(conclusion);
  return wrap;
}

/* 风格学习卡：advanced.style_learning.enabled（M17-补丁1 A5，默认关） */
function styleLearningCard() {
  if (!state.values.advanced.style_learning) {
    state.values.advanced.style_learning = {};
  }
  const styleValues = state.values.advanced.style_learning;
  const card = optionCard(
    "风格学习",
    "AstrBot 在读文章、冲浪的时候，会从真人写的东西里学说话风格——句式、" +
      "思维方式、待人接物，不只是口癖。学到的味道会在它说话时低调度参考，" +
      "用得顺的变成习惯，久不用自然淡出。判定像 AI 写的语料绝不学。" +
      "默认关，先看效果再决定常开（细项在专家组「风格学习」）。",
    [["开", true], ["关", false]],
    () => styleValues.enabled === true,
    (v) => {
      styleValues.enabled = v;
      setDirty(true);
      refreshStatusViews("style_learning.enabled"); // M27-补丁1 7.2（F 区总闸）
    },
  );
  card.classList.add("style-card");
  card.appendChild(styleChainBlock()); // M25-补丁1 配套 b：卡上直接显示生效链
  return card;
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
    "AstrBot 的浏览器工具（打开网页、看画面、点击、输入）依赖 Playwright 的 " +
    "Chromium 内核（约 150MB 下载），不随插件内置。装好后能力档位 ≥1 时" +
    "它能真正浏览网页并把看到的画面截图存档；不装则浏览器工具不挂载，" +
    "它的活动退化为「搜索 + 读文本」，其余能力不受影响。" +
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

/* AstrBot 的文件夹（M23-补丁1 B4）：工作区实际路径与状态实时展示。
 * 让人一眼知道自己的 workspace_dir 配置有没有生效（就绪/不存在/被文件
 * 占用/创建失败），失败时带原因，不静默。 */
function workspaceCard() {
  const card = document.createElement("div");
  card.className = "knob-card workspace-card";
  const h = document.createElement("h3");
  h.textContent = "AstrBot 的文件夹";
  card.appendChild(h);
  const p = document.createElement("p");
  p.className = "hint";
  p.textContent =
    "这是 AstrBot 的专属工作区：能力档「玩」及以上时，它的文件读写、小程序都" +
    "在这个目录里；开到「命令行」档时那也是命令的默认工作目录。路径来自" +
    "专家配置 autonomy.workspace_dir，留空则用插件数据目录下的默认位置，" +
    "插件启动时会自动创建。如果下面显示创建失败，检查路径是否合法、磁盘" +
    "是否可写。";
  card.appendChild(p);
  const status = document.createElement("p");
  status.className = "hint workspace-status";
  status.textContent = "AstrBot 的文件夹：读取中…";
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
    el.textContent = `AstrBot 的文件夹：${data.path}（${stateText}）`;
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
    "允许 AstrBot 自主活动时联网搜索（博查网页搜索）。你聊天时的搜索不受影响" +
      "（那是 AstrBot 自己的搜索）。关掉后冲浪、读文章两项活动也会从它的" +
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
    "允许它使用你给本体（AstrBot）配置的工具——按人格设定里勾选的工具" +
      "筛选，含 MCP 工具。它自带的搜索、抓取、沙箱、记忆能力不受影响。",
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

/* 档位 → base_probability 映射（10-03 定稿：0.08 / 0.18 / 0.35） */
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
    "除了分享活动和说梦话，AstrBot 也会自己起念头找你说话——可能没有事由，" +
    "也可能接上你们最近聊的话题。它睡着时绝不会打扰；每天能说多少句、" +
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
  levelLabel.textContent = "主动程度——它多常主动找你说话";
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

  // 未回应收敛开关（定稿文案）
  const backoffLabel = document.createElement("p");
  backoffLabel.className = "slider-label";
  backoffLabel.textContent = "你不理它时，它会慢慢安静下来（不会完全不理你）";
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
    "感知你的起床约定——你说\"明早 8 点起\"，它会记在心里。像人一样：" +
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
      refreshStatusViews("sleep.schedule_reminder_enabled"); // M27-补丁1 7.2（E6 档位）
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
    refreshStatusViews(`${group}.${key}`); // M27-补丁1 7.2：状态点实时重算
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
  } else if (group === "judge" && key === "provider_id") {
    // M27-补丁1 7.1：判断模型 provider 改下拉（此前没有分支、落到默认
    // text 手填）。留空语义与 model 不同：不是回退聊天模型，而是整条判断
    // 链不工作——emptyStatus/emptyWarn/missingWarn 用 judge 专属文案。
    container.appendChild(providerPickerControl({
      value: value ?? "",
      onChange: (v) => set(v),
      emptyLabel: "（留空 = 不判断）",
      emptyStatus: "当前实际生效：不判断（输入建议与输出检查都不运行）",
      emptyWarn: "⚠ 还没选 provider：判断链整条不工作（日志记 WARNING，不影响聊天）。建议选一个便宜快速的小模型 provider。",
      missingWarn: "⚠ 该 provider id 不在已启用的 provider 列表里，判断时会跳过本轮并在日志留痕。请核对拼写，或到 provider 管理页启用它。",
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
    // M27-补丁1 7.3：每行附中文说明；未知工具标注（当前注册表里没有，请核对）
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
    const labelOf = (name) => {
      if (extras.includes(name)) return `${name}（当前注册表里没有，请核对）`;
      const desc = AGENT_TOOL_DESCRIPTIONS[name];
      return desc ? `${name}（${desc}）` : name;
    };
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
      row.append(box, document.createTextNode(labelOf(name)));
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

/* ---------------- M25-补丁1：状态引擎视图层 ----------------
 * expertBuildCtx / 状态点 / 生效链面板 / 键行构建 / 树渲染 / 全局状态行。
 * 判定全部走 status-engine.js 纯函数（node 桥行为级测试同一份代码）。 */

function expertBuildCtx() {
  return { values: state.values, runtime: state.runtime || {} };
}

/* 四态状态点（默认只露一个点，链路点击才展开——设计纪律） */
function statusDotEl(status, labelOverride) {
  const meta = STATUS_META[status] || STATUS_META.unknown;
  const el = document.createElement("span");
  el.className = `status-dot ${meta.cls}`;
  el.textContent = `${meta.dot} ${labelOverride || meta.label}`;
  el.title =
    status === "active" ? "开关打开且前置满足，真的在生效"
      : status === "off" ? "显式关闭"
        : status === "blocked" ? "开关打开但前置不满足（点「生效链」看卡在哪）"
          : status === "dead" ? "被上游显式关闭级联——这里的键改了也不生效"
            : status === "unimpl" ? "该档位尚未实现，选中仅占位"
              : "运行时状态未知（数据未就绪），不猜";
  return el;
}

/* 生效链面板（C5）：点开弹一条文本链路，每环带状态与原因 */
function chainPanelEl(title, decl) {
  const panel = document.createElement("div");
  panel.className = "chain-panel hidden";
  const chain = renderChain(title, decl, expertBuildCtx());
  const head = document.createElement("div");
  head.className = "chain-title";
  head.textContent = chain.title;
  panel.appendChild(head);
  for (const line of chain.lines) {
    const row = document.createElement("div");
    row.className = "chain-line";
    row.textContent = line;
    panel.appendChild(row);
  }
  const conclusion = document.createElement("div");
  conclusion.className = `chain-conclusion ${STATUS_META[chain.status]?.cls || ""}`;
  conclusion.textContent = chain.conclusion;
  panel.appendChild(conclusion);
  if (chain.note) {
    const note = document.createElement("div");
    note.className = "chain-note";
    note.textContent = chain.note;
    panel.appendChild(note);
  }
  return panel;
}

function chainToggleBtn(panel) {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "link-button chain-toggle";
  btn.textContent = "生效链 ▸";
  btn.addEventListener("click", () => {
    const hidden = panel.classList.toggle("hidden");
    btn.textContent = hidden ? "生效链 ▸" : "生效链 ▾";
  });
  return btn;
}

/* 单个键行（从旧 renderExpert 提出：label + 危险/联动徽章 + 控件）。
 * M25-补丁1 新增：键级状态点（组不生效或键自带 requires 时显示）与
 * 键行「生效链」按钮（仅自带 requires 的键，避免密度回潮）。 */
function buildKeyRow(group, key, item, keyStatus, showDot) {
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
  // M18-补丁1 D2 / M25-补丁1 配套 f：旋钮映射目标键的 mapped-chip 随重排保留
  if (KNOB_MAPPED_KEYS.has(`${group}.${key}`)) {
    const chip = document.createElement("span");
    chip.className = "mapped-chip";
    // M34-补丁1 B 组：chip 增强——按当前值与新手页显示档比对，"已脱离"
    // （值≠档位定义）或"仍在档内"，与新手页提示同口径；无法判定时保持
    // 通用文案
    const knobStatus = mappedKeyKnobStatus(group, key);
    if (knobStatus === "off") {
      chip.textContent = "档位联动·已脱离";
      chip.title = "新手页当前选中的档位与这里的值不一致（这里的值已经不等于档位默认值）；" +
        "在新手页切换档位时，这里的值会被档位映射覆盖";
    } else if (knobStatus === "in") {
      chip.textContent = "档位联动";
      chip.title = "这里的值与新手页当前选中的档位一致；在新手页切换档位时，这里的值会被档位映射覆盖";
    } else {
      chip.textContent = "档位联动";
      chip.title = "新手区对应的档位旋钮会写入这个键；在新手区切换档位时，你在这里改的值会被档位映射覆盖";
    }
    nameEl.prepend(chip);
  }
  if (showDot && keyStatus) {
    labelEl.insertBefore(statusDotEl(keyStatus), keyEl);
    if (keyStatus === "dead") row.classList.add("dimmed");
  }
  // M26-补丁1：key-side（生效链按钮）仍仅 requires 键显示（密度纪律），
  // 但 label 的挂载必须在分支外——M25 提取本函数时把挂载误圈进了 if，
  // 导致 121 个无 requires 键的标签整体丢失（回归，修前必红见 test_m26_patch1）。
  // M27-补丁1 2.1：chain-panel 必须与按钮一起挂进 DOM——此前只 append 了
  // 按钮，面板是游离节点，点击切换的是文档外节点（按钮永远"没反应"）。
  let side = null;
  if (Array.isArray(item.requires) && item.requires.length) {
    side = document.createElement("div");
    side.className = "key-side";
    const panel = chainPanelEl(`${item.description || key} 的生效链`, item);
    side.append(chainToggleBtn(panel), panel); // 次序：按钮 → 面板
  }
  const ctrl = document.createElement("div");
  ctrl.className = "key-control";
  buildControl(group, key, item, ctrl);
  row.appendChild(labelEl);          // 不变量：label 永远挂载（与 3174dba 旧版一致）
  if (side) row.appendChild(side);   // 生效链按钮仅 requires 键显示
  row.appendChild(ctrl);
  return row;
}

/* 二级组渲染（四态 + 生效 x/y + 生效链 + 级联置灰 + 因果行） */
function buildGroup(secDecl, gDecl, entries, ctx) {
  const ctxWrap = document.createElement("div");
  ctxWrap.className = "expert-group";

  const grpResult = computeNodeStatus(gDecl, ctx);
  const switchKey = gDecl.switch || "";
  const keyStatuses = entries.map((e) => computeKeyStatus(grpResult.status, {
    requires: e.item.requires,
    isGroupSwitch: switchKey === `${e.group}.${e.key}`,
  }, ctx));
  const counts = computeCounts(keyStatuses);

  const gkey = `grp:${secDecl.id}|${gDecl.id}`;
  const expanded = !!state.expanded[gkey];
  const head = document.createElement("button");
  head.type = "button";
  head.className = "drawer-head group-head" + (expanded ? " open" : "");
  const arrow = document.createElement("span");
  arrow.className = "arrow";
  arrow.textContent = expanded ? "▾" : "▸";
  const dot = statusDotEl(grpResult.status, grpResult.statusLabel);
  const title = document.createElement("span");
  title.textContent = gDecl.title || gDecl.id;
  const count = document.createElement("span");
  count.className = "count";
  count.textContent = `${entries.length} 项 · 生效 ${counts.active}/${counts.total}`;
  head.append(arrow, dot, title, count);

  const body = document.createElement("div");
  body.className = "drawer-body group-body" + (expanded ? "" : " hidden");

  // M26-补丁1 配套 a：组级一句话说明（panel_layout.json 的 summary 此前
  // 是无通路死数据，前端从未读取）——组头下首行；无 summary 的组不渲染空行
  if (gDecl.summary) {
    const summaryEl = document.createElement("div");
    summaryEl.className = "group-summary";
    summaryEl.textContent = gDecl.summary;
    body.appendChild(summaryEl);
  }

  // C6：被上游显式关闭级联（哑）→ 整组置灰 + 头部一行因果说明
  if (grpResult.status === "dead" && grpResult.failing) {
    body.classList.add("dimmed");
    const ring = grpResult.chain[grpResult.failing.index];
    const cause = document.createElement("div");
    cause.className = "cascade-note";
    cause.textContent =
      `⇒ 被「${ring.label}」级联休眠：这里的键改了也不生效。${ring.failHint || ""}`;
    body.appendChild(cause);
  }
  if (grpResult.status === "unimpl" && grpResult.hint) {
    const note = document.createElement("div");
    note.className = "cascade-note";
    note.textContent = `⇒ ${grpResult.hint}`;
    body.appendChild(note);
  }

  // C5：组级生效链（组头按钮，默认折叠）。M27-补丁1 2.1：面板与按钮一起
  // 挂载（此前只挂按钮，面板游离在文档外，点击无反应——与键级同一根因）。
  if ((gDecl.requires || []).length) {
    const chainWrap = document.createElement("div");
    chainWrap.className = "group-chain";
    const panel = chainPanelEl(`${gDecl.title || gDecl.id} 的生效链`, gDecl);
    chainWrap.append(chainToggleBtn(panel), panel); // 次序：按钮 → 面板
    body.appendChild(chainWrap);
  }

  // C3 活动池：当前可跑的活动注记（镜像 activities.py，验收 4 的展示面）
  if (secDecl.id === "C" && gDecl.id === "C3") {
    const adv = state.values.advanced || {};
    const runnable = runnableActivities(
      (adv.decision || {}).agent_activities,
      (adv.decision || {}).free_activity_enabled,
      (adv.capabilities || {}).web_search_enabled,
    );
    const poolNote = document.createElement("div");
    poolNote.className = "pool-note";
    poolNote.textContent =
      `当前可跑的活动：${runnable.length ? runnable.join(" / ") : "（空）"}` +
      (runnable.length ? "" : "——检查联网搜索与活动池配置");
    body.appendChild(poolNote);
  }

  entries.forEach((e, i) => {
    // 键行状态点：仅组不生效或键自带 requires 时显示（密度纪律）
    const showDot = grpResult.status !== "active"
      || (Array.isArray(e.item.requires) && e.item.requires.length);
    body.appendChild(buildKeyRow(e.group, e.key, e.item, keyStatuses[i], showDot));
  });

  head.addEventListener("click", () => {
    const next = !state.expanded[gkey];
    state.expanded[gkey] = next;
    saveExpanded(state.expanded);
    head.classList.toggle("open", next);
    arrow.textContent = next ? "▾" : "▸";
    body.classList.toggle("hidden", !next);
  });

  ctxWrap.append(head, body);
  return { el: ctxWrap, body, keyStatuses };
}

/* 配套 h：兼容回退——schema 无元数据（无 section/无 layout）时按旧扁平
 * 分组渲染（不崩、走旧路径），不显示四态与链。 */
function renderExpertFlat() {
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
      body_el.appendChild(buildKeyRow(group, key, item, "active", false));
    }

    if (group === "style_learning") {
      body_el.appendChild(styleLibraryAdmin());
    }

    drawer.append(head, body_el);
    list.appendChild(drawer);
  }
}

/* C3/C7/C8 主渲染：遍历 _layout 树收叶子（一级域 / 二级功能 / 三级键，
 * 不设四级）；键按 section 落位，未收录键进兜底区（配套 d，不丢键）。 */
function renderExpert() {
  const list = $("#expert-groups");
  list.innerHTML = "";
  const layout = state.layout;
  const advancedSchema = state.schema.advanced.items;
  if (!layout || !Array.isArray(layout.sections) || !layout.sections.length) {
    renderExpertFlat(); // 配套 h：旧 schema 回退扁平渲染
    return;
  }
  const ctx = expertBuildCtx();

  // 按 section 收集键；同时记录未收录键（兜底）
  const placed = new Set();
  const byGroup = new Map();
  const uncovered = [];
  for (const [group, body] of Object.entries(advancedSchema)) {
    for (const [key, item] of Object.entries(body.items || {})) {
      const sec = Array.isArray(item.section) ? item.section : null;
      if (!sec) {
        uncovered.push({ group, key, item, order: 0 });
        // 配套 d：布局未覆盖 → console.warn + 归入「其他参数」区（不丢键）
        console.warn(`[living] _layout 未收录键 ${group}.${key}，已归入「其他参数」区`);
        continue;
      }
      placed.add(`${group}.${key}`);
      const gid = `${sec[0]}|${sec[1]}`;
      if (!byGroup.has(gid)) byGroup.set(gid, []);
      byGroup.get(gid).push({
        group, key, item,
        order: typeof sec[2] === "number" ? sec[2] : 0,
      });
    }
  }

  const skey = (id) => `sec:${id}`;
  for (const secDecl of layout.sections) {
    const isFallback = !!secDecl.fallback;
    const groupsRendered = [];
    const secStatuses = [];
    let moodHost = null;
    for (let gi = 0; gi < (secDecl.groups || []).length; gi++) {
      const gDecl = secDecl.groups[gi];
      const entries = isFallback ? uncovered : (byGroup.get(`${secDecl.id}|${gDecl.id}`) || []);
      if (!entries.length) continue;
      entries.sort((a, b) => a.order - b.order);
      const built = buildGroup(secDecl, gDecl, entries, ctx);
      groupsRendered.push(built.el);
      secStatuses.push(...built.keyStatuses);
      // （附）运行时管理卡：挂在区的最后一个有键的组的末尾
      const isLastGroupWithKeys =
        !secDecl.groups.slice(gi + 1).some((g2) =>
          (isFallback ? uncovered : (byGroup.get(`${secDecl.id}|${g2.id}`) || [])).length);
      if (isLastGroupWithKeys && secDecl.id === "F") {
        built.body.appendChild(styleLibraryAdmin());
      }
      if (isLastGroupWithKeys && secDecl.id === "G") {
        moodHost = document.createElement("div");
        moodHost.id = "mood-section";
        built.body.appendChild(moodHost);
      }
    }
    if (isFallback && !groupsRendered.length) continue; // 无未收录键不渲染兜底区
    if (!groupsRendered.length) continue;

    const counts = computeCounts(secStatuses);
    const secOpen = state.expanded[skey(secDecl.id)]
      ?? !(secDecl.collapsed === true);
    const secEl = document.createElement("div");
    secEl.className = "expert-section";
    const secHead = document.createElement("button");
    secHead.type = "button";
    secHead.className = "drawer-head section-head" + (secOpen ? " open" : "");
    const secArrow = document.createElement("span");
    secArrow.className = "arrow";
    secArrow.textContent = secOpen ? "▾" : "▸";
    const secTitle = document.createElement("span");
    secTitle.className = "section-title";
    secTitle.textContent = `${secDecl.id}. ${secDecl.title}`;
    const secCount = document.createElement("span");
    secCount.className = "count section-count";
    secCount.textContent = `本区 ${counts.active}/${counts.total} 项生效`;
    secHead.append(secArrow, secTitle, secCount);
    const secBody = document.createElement("div");
    secBody.className = "drawer-body section-body" + (secOpen ? "" : " hidden");
    if (secDecl.subtitle) {
      const sub = document.createElement("div");
      sub.className = "section-subtitle";
      sub.textContent = secDecl.subtitle;
      secBody.appendChild(sub);
    }
    for (const el of groupsRendered) secBody.appendChild(el);
    secHead.addEventListener("click", () => {
      const next = !state.expanded[skey(secDecl.id)];
      state.expanded[skey(secDecl.id)] = next;
      saveExpanded(state.expanded);
      secHead.classList.toggle("open", next);
      secArrow.textContent = next ? "▾" : "▸";
      secBody.classList.toggle("hidden", !next);
    });
    secEl.append(secHead, secBody);
    list.appendChild(secEl);
    if (moodHost && !$("#view-expert").classList.contains("hidden")) {
      renderMoodSection(); // 心境与兴趣（G 区（附））：专家视图可见时立即填充
    }
  }
}

/* C8 全局状态行：AstrBot 现在会主动做的事（七条出口各带状态）。4 条过闸门
 * （活动分享/梦话/睡过头交代共用 _maybe_share + 主动搭话）+ 3 条直发
 * （晚安/唤醒确认/醒来补回复）——文案逐字来自 panel_layout.json。 */
function renderGlobalStatus() {
  const host = $("#global-status");
  if (!host) return;
  const exits = ((state.layout || {}).exits || {}).items || [];
  if (!exits.length) {
    host.classList.add("hidden");
    return;
  }
  const lines = computeExitLines(exits, expertBuildCtx());
  host.innerHTML = "";
  const lead = document.createElement("span");
  lead.className = "global-status-lead";
  lead.textContent = "AstrBot 现在会主动做的事：";
  host.appendChild(lead);
  lines.forEach((item, i) => {
    const meta = STATUS_META[item.status] || STATUS_META.unknown;
    const seg = document.createElement("span");
    seg.className = `exit-seg ${meta.cls}`;
    seg.textContent =
      `${item.label} ${meta.dot}${item.status === "on" ? "" : meta.label}` +
      (item.detail ? `（${item.detail}）` : "");
    host.appendChild(seg);
    if (i < lines.length - 1) {
      host.appendChild(document.createTextNode("｜"));
    }
  });
  host.classList.remove("hidden");
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
    "兴趣权重影响它自主选话题的倾向。改动立即生效（无需保存）；清空后" +
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
        await loadRuntime();
        renderNovice();
        renderExpert();
        renderGlobalStatus();
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
  ta.placeholder = "粘贴你希望它学的说话语料（评论区、聊天记录、一段文字…）";
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
    empty.textContent = "素材库还是空的。把你希望它学的语料粘贴进去（上面的输入框）。";
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
    empty.textContent = "语料库还是空的——它读完网页学到第一条风格后，会出现在这里。";
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
    empty.textContent = "沉淀层还没有内容——等每日复盘累计足够好评后，会归纳出它的稳定说话方式。";
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
    hint.textContent = "语料库=它学到的风格片段；素材库=你手动投入的原始语料；调用记录=取用事实（不进记忆）。";
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
  title.textContent = "语料与素材（它想学谁的说话方式，你说了算）";
  card.appendChild(title);
  const hint = document.createElement("p");
  hint.className = "hint";
  hint.textContent = "把你想让它学的语料丢进素材库，它下次活动结束（或你点「立即处理」）就会提炼成自己的说话方式。学到的都存在语料库里，随时可看可删。";
  card.appendChild(hint);
  card.appendChild(styleChainBlock()); // M25-补丁1 配套 b：素材卡上直接显示生效链

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
      corpusHead.textContent = `它学到的语料（${data.corpus.length} 条）`;
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

/* ---------------- M28-补丁1：内置说明书（模态照 prompt_preset 形态） ----------------
 * 内容来源 help-content.js 的 LIVING_HELP（纯数据）。安全路线：全程
 * textContent / createElement 构节点——数据里的 < & " 等字符按字面显示，
 * 不存在拼 HTML 的注入面（因此不需要 escapeHtml）。 */

function renderHelpModal() {
  const body = $("#help-body");
  body.textContent = ""; // 重建（说明书内容随版本可能变）
  for (const section of LIVING_HELP) {
    const sec = document.createElement("section");
    sec.className = "help-section";
    const h3 = document.createElement("h3");
    h3.textContent = section.title;
    sec.appendChild(h3);
    for (const block of section.blocks || []) {
      if (block.t === "list") {
        const ul = document.createElement("ul");
        for (const item of block.items || []) {
          const li = document.createElement("li");
          li.textContent = item;
          ul.appendChild(li);
        }
        sec.appendChild(ul);
      } else if (block.t === "table") {
        const wrap = document.createElement("div");
        wrap.className = "help-table-wrap";
        const table = document.createElement("table");
        table.className = "help-table";
        const thead = document.createElement("thead");
        const headRow = document.createElement("tr");
        for (const cell of block.head || []) {
          const th = document.createElement("th");
          th.textContent = cell;
          headRow.appendChild(th);
        }
        thead.appendChild(headRow);
        table.appendChild(thead);
        const tbody = document.createElement("tbody");
        for (const row of block.rows || []) {
          const tr = document.createElement("tr");
          for (const cell of row) {
            const td = document.createElement("td");
            td.textContent = cell;
            tr.appendChild(td);
          }
          tbody.appendChild(tr);
        }
        table.appendChild(tbody);
        wrap.appendChild(table);
        sec.appendChild(wrap);
      } else {
        // 默认按一段话（t === "p" 或未标注）
        const p = document.createElement("p");
        p.className = "help-block";
        p.textContent = block.text;
        sec.appendChild(p);
      }
    }
    body.appendChild(sec);
  }
}

function openHelpModal() {
  renderHelpModal();
  $("#help-mask").classList.remove("hidden");
}

function closeHelpModal() {
  $("#help-mask").classList.add("hidden");
}

async function boot() {
  $("#tab-novice").addEventListener("click", () => switchView("novice"));
  $("#tab-expert").addEventListener("click", () => switchView("expert"));
  $("#btn-save").addEventListener("click", save);
  $("#btn-reset").addEventListener("click", reset);
  // M28-补丁1：说明书三开两关——按钮开；关闭按钮、点遮罩（只认遮罩本体，
  // 点卡片不关）、Escape（仅弹层开着时）关。新手/专家两视图共用工具栏按钮。
  $("#btn-help").addEventListener("click", openHelpModal);
  $("#help-close").addEventListener("click", closeHelpModal);
  $("#help-mask").addEventListener("click", (e) => {
    if (e.target === e.currentTarget) closeHelpModal();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !$("#help-mask").classList.contains("hidden")) {
      closeHelpModal();
    }
  });
  try {
    await load();
  } catch (e) {
    showError(`配置读取失败：${e && e.message ? e.message : e}（请刷新重试）`);
  }
}

boot();
