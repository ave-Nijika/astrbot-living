/* astrbot_plugin_living —— 配置面板逻辑（M5 补丁 1）
 * 运行在 AstrBot Plugin Pages 受限 iframe 中，通过 window.AstrBotPluginPage
 * bridge 调用后端（自动携带 dashboard 鉴权）。bridge 仅提供 GET/POST。
 * 注意：Pages 沙箱忽略 window.confirm/alert/prompt——确认动作用页内弹层。 */

const KNOB_ORDER = [
  "preset_sleep_style", "preset_activity_level", "preset_talk_frequency",
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
