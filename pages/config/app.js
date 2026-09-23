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
  sleep: "休眠", capabilities: "能力参数", memory: "记忆", model: "模型",
};

/* 任务书 2.2 的危险项清单（advanced 组内，带醒目警告） */
const DANGER_KEYS = new Set([
  "sleep.weights", "sleep.fatigue_rate_per_hour",
  "decision.recent_topic_penalty", "decision.single_run_token_budget",
  "decision.max_tool_rounds", "decision.max_run_seconds",
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
      const input = document.createElement("input");
      input.type = "text";
      input.placeholder = "provider id（留空用聊天模型）";
      input.value = state.values.knobs[name] ?? "";
      input.addEventListener("input", () => {
        state.values.knobs[name] = input.value;
        setDirty(true);
      });
      card.appendChild(input);
    } else {
      const options = item.options || [];
      const wrap = document.createElement("div");
      wrap.className = "option-row";
      for (const opt of options) {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "option";
        btn.textContent = opt;
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
  grid.appendChild(scheduleCard()); // 第 10 张卡：起床约定（M5-补丁4）
  renderLifeExtra(presetSchema);
}

/* 第 10 张卡：起床约定（M5-补丁4 D2）——总开关 + 自觉性滑块。
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
      const ctrl = document.createElement("div");
      ctrl.className = "key-control";
      buildControl(group, key, item, ctrl);
      row.append(labelEl, ctrl);
      body_el.appendChild(row);
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
 * M9-补丁2 B1/B2：绕过 bridge，直接同源 fetch——bridge（打包 dist）的
 * 转发路径与端点注册失配（连 M5 的 config 旧端点也失配，实测"未找到该
 * 路由"），而同源 /api/v1/plugins/extensions/<plugin>/<route> 始终正常。
 * 鉴权与 dashboard 前端同款：localStorage['token'] + Authorization Bearer
 * （dashboard/src/api/http.ts getToken 同款键）。沙箱防御：localStorage
 * 与 fetch 全程 try/catch，取不到 token 就裸请求（401 时给出专用文案）。 */

const PLUGIN_API_BASE = "/api/v1/plugins/extensions/astrbot_plugin_living";

function authToken() {
  try {
    return localStorage.getItem("token") || "";
  } catch (e) {
    return ""; // 沙箱禁用 localStorage：裸请求，401 文案兜底
  }
}

/* 心境端点统一请求：401 专用文案（B3），其余透传服务端 message。 */
async function moodRequest(path, options = {}) {
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
