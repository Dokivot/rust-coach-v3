const stateElement = (id) => document.getElementById(id);
let stateCatalog;
let currentSnapshot = null;
let currentScope = "";
let currentConversation = null;
let stateBusy = false;
let draftUpdate = null;
let pendingUpdate = null;
const factControls = new Map();
const stateStatus = {fresh: "有效期内 · 玩家自报", stale: "已过期 · 请重新确认", invalidated: "已失效 · 请重新确认"};
const groupNames = {technology: "科技进度", base: "基地状况", session: "当前处境与时间", progress: "所处阶段", goals: "当前目标", preferences: "偏好"};
const choiceNames = {twig: "树枝", wood: "木质", stone: "石质", metal: "金属", armored: "装甲", mixed: "混合", starting: "刚起步", developing: "发展中", established: "已稳定发展", recovering: "重建中", cautious: "稳健", balanced: "均衡", aggressive: "进取"};

function storedValue(key) { try { return localStorage.getItem(key); } catch { return null; } }
function rememberValue(key, value) { try { localStorage.setItem(key, value); } catch {} }

async function stateApi(path, body) {
  const response = await fetch(path, body === undefined ? {} : {
    method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body),
  });
  const result = await response.json();
  if (!response.ok) throw new Error(result.error?.message || "操作失败，请刷新后重试。");
  return result;
}

function announce(message, error = false) {
  stateElement("state-error").hidden = !error;
  stateElement("state-notice").hidden = error || !message;
  stateElement(error ? "state-error" : "state-notice").textContent = message;
}

async function stateAction(action) {
  if (stateBusy) return;
  stateBusy = true;
  document.querySelectorAll("button, select, input, textarea").forEach((node) => { node.disabled = true; });
  announce("");
  try { await action(); }
  catch (error) { announce(error.message, true); }
  finally {
    stateBusy = false;
    document.querySelectorAll("button, select, input, textarea").forEach((node) => { node.disabled = false; });
    for (const control of factControls.values()) updateFactControls(control);
  }
}

function stateOption(value, text) {
  const option = document.createElement("option");
  option.value = value;
  option.textContent = text;
  return option;
}

function stateLabel(text, input) {
  const label = document.createElement("label");
  label.textContent = text;
  label.append(input);
  return label;
}

function addInventoryRow(item = "", amount = 0) {
  if (stateElement("state-inventory-rows").children.length >= 150) return;
  const row = document.createElement("div");
  row.className = "item-row";
  const itemInput = document.createElement("input");
  itemInput.setAttribute("list", "item-ids");
  itemInput.pattern = "[a-zA-Z0-9_.\\-]+";
  itemInput.maxLength = 80;
  itemInput.required = true;
  itemInput.value = item;
  const count = document.createElement("input");
  count.type = "number";
  count.min = 0;
  count.max = 1000000;
  count.step = 1;
  count.required = true;
  count.value = amount;
  const remove = document.createElement("button");
  remove.type = "button";
  remove.className = "secondary-button";
  remove.textContent = "移除";
  remove.addEventListener("click", () => row.remove());
  row.append(stateLabel("物品 ID（支持下拉搜索）", itemInput), stateLabel("当前总数量", count), remove);
  stateElement("state-inventory-rows").append(row);
}

function updateFactControls(control) {
  control.input.disabled = stateBusy || control.mode.value !== "set";
  control.input.required = control.mode.value === "set" && control.definition.input !== "items";
}

function buildFields() {
  const groups = {};
  for (const [key, definition] of Object.entries(stateCatalog.fields)) {
    const group = key.split(".")[0];
    if (!groups[group]) {
      const details = document.createElement("details");
      const summary = document.createElement("summary");
      summary.textContent = groupNames[group];
      details.append(summary);
      stateElement("fact-fields").append(details);
      groups[group] = details;
    }
    const row = document.createElement("div");
    row.className = "state-fact state-fact-row";
    let input;
    if (["boolean", "choice"].includes(definition.input)) {
      input = document.createElement("select");
      input.append(stateOption("", "未记录 / 请选择"));
      const choices = definition.input === "boolean" ? ["true", "false"] : definition.schema.enum;
      for (const choice of choices) input.append(stateOption(choice,
        definition.input === "boolean" ? (choice === "true" ? "是" : "否") : choiceNames[choice] || choice));
    } else {
      input = document.createElement("input");
      input.type = definition.input === "number" ? "number" : "text";
      if (definition.input === "number") {
        input.min = definition.minimum;
        input.max = definition.maximum;
        input.step = 1;
      } else input.maxLength = definition.input === "items" ? 8100 : 500;
      if (definition.input === "items") input.placeholder = "规范 ID，逗号分隔；空列表只表示没有已确认项";
    }
    input.id = `fact-${key}`;
    const mode = document.createElement("select");
    mode.id = `mode-${key}`;
    mode.append(stateOption("keep", "保持不变"), stateOption("set", "本次更新"), stateOption("clear", "清除为未知"));
    const control = {input, mode, definition};
    mode.addEventListener("change", () => updateFactControls(control));
    row.append(stateLabel(definition.label, input), stateLabel("更新方式", mode));
    groups[group].append(row);
    factControls.set(key, control);
    updateFactControls(control);
  }
}

function summaryRow(label, value, note) {
  const row = document.createElement("div");
  row.className = "state-summary";
  const title = document.createElement("div");
  title.textContent = label;
  const content = document.createElement("div");
  content.textContent = value;
  const detail = document.createElement("small");
  detail.textContent = note;
  content.append(detail);
  row.append(title, content);
  stateElement("snapshot-summary").append(row);
}

function showSnapshot(snapshot) {
  currentSnapshot = snapshot;
  stateElement("scope-workspace").hidden = false;
  stateElement("snapshot-meta").textContent = `${snapshot.scope.label} · 服务器 ${snapshot.scope.server_id} · 清档 ${snapshot.scope.wipe_id} · 版本 ${snapshot.revision} · PC 原版`;
  stateElement("snapshot-json").textContent = JSON.stringify(snapshot, null, 2);
  stateElement("history-revision").max = snapshot.revision;
  stateElement("snapshot-summary").replaceChildren();
  for (const [key, record] of Object.entries(snapshot.facts)) {
    const value = Array.isArray(record.value) ? record.value.join("、") || "暂无已确认项" :
      typeof record.value === "boolean" ? (record.value ? "是" : "否") : choiceNames[record.value] || String(record.value);
    summaryRow(stateCatalog.fields[key].label, value,
      `${stateStatus[record.status]}；观测 ${new Date(record.observed_at).toLocaleString()}；来源 ${record.source}`);
  }
  for (const [container, data] of Object.entries(snapshot.retired_containers || {})) {
    summaryRow(`已失效的旧容器 ${container}`, "仅供历史核对，不参与当前计算",
      `失效原因 ${data.invalidated_by}；历史数量见完整状态或历史版本`);
  }
  for (const [container, data] of Object.entries(snapshot.containers)) {
    const coverage = data.coverage === "complete" ? "完整盘点" : "局部记录，其他物品未知";
    summaryRow(`容器 ${container}`, coverage, `${stateStatus[data.status]}；${data.usable_for_exact_calculation ? "可供精确计算选用" : "不可直接用于精确缺口计算"}`);
    for (const [item, record] of Object.entries(data.items)) {
      summaryRow(stateCatalog.items[item] || item, `${record.value} · ${item}`,
        `${stateStatus[record.status]}；观测 ${new Date(record.observed_at).toLocaleString()}`);
    }
    if (!Object.keys(data.items).length) summaryRow("库存", data.coverage === "complete" ? "已报告为空" : "未知", "");
  }
  for (const record of Object.values(snapshot.visual_observations || {})) {
    const observation = record.value;
    summaryRow(`截图局部 · ${observation.label}`, observation.description + (observation.quantity === null ? "" : ` · 图中读数 ${observation.quantity}`),
      `${stateStatus[record.status]}；观测 ${new Date(record.observed_at).toLocaleString()}；${record.source}；草稿 ${record.visual_draft_id}`);
  }
  if (!Object.keys(snapshot.facts).length && !Object.keys(snapshot.containers).length && !Object.keys(snapshot.visual_observations || {}).length) summaryRow("尚未录入", "资源、科技、基地与阶段均未知", "请在下面录入当前观测。");
  for (const [key, control] of factControls) {
    control.mode.value = "keep";
    const record = snapshot.facts[key];
    if (record) control.input.value = Array.isArray(record.value) ? record.value.join(", ") : String(record.value);
    else control.input.value = "";
    updateFactControls(control);
  }
}

async function loadScopes() {
  const scopes = await stateApi("/api/player-state/scopes");
  stateElement("scope-select").replaceChildren(stateOption("", "请选择或新建档案"));
  for (const scope of scopes) stateElement("scope-select").append(stateOption(scope.id,
    `${scope.label} · ${scope.server_id} / ${scope.wipe_id}`));
  return scopes;
}

async function loadScope(identifier) {
  draftUpdate = null;
  pendingUpdate = null;
  stateElement("draft-panel").hidden = true;
  stateElement("history-json").textContent = "";
  stateElement("state-inventory-rows").replaceChildren();
  currentScope = identifier;
  currentSnapshot = null;
  stateElement("scope-select").value = identifier;
  rememberValue("rust-coach-player-scope", identifier);
  stateElement("scope-workspace").hidden = !identifier;
  if (identifier) showSnapshot(await stateApi(`/api/player-state/scopes/${identifier}`));
}

async function showBinding() {
  currentConversation = storedValue("rust-coach-conversation");
  if (!currentConversation) {
    stateElement("binding-note").textContent = "尚无当前对话。可以先录入状态，再返回聊天选择档案。";
    return null;
  }
  try {
    const binding = await stateApi(`/api/conversations/${currentConversation}/player-state`);
    stateElement("binding-note").textContent = binding.snapshot ?
      `当前对话关联：${binding.snapshot.scope.label} · ${binding.snapshot.scope.server_id} / ${binding.snapshot.scope.wipe_id}` : "当前对话未关联状态档案。";
    return binding.scope_id;
  } catch {
    currentConversation = null;
    stateElement("binding-note").textContent = "原对话不可用，请返回聊天创建对话。状态档案仍保留。";
    return null;
  }
}

function requireScope() {
  if (!currentScope || !currentSnapshot) throw new Error("请先选择一个状态档案。");
}

async function saveChanges(changes, prepared = null) {
  requireScope();
  if (prepared) pendingUpdate = prepared;
  else if (!pendingUpdate || JSON.stringify(pendingUpdate.changes) !== JSON.stringify(changes)) {
    pendingUpdate = {observation_id: crypto.randomUUID(), expected_revision: currentSnapshot.revision,
      observed_at: new Date().toISOString(), source: "manual", changes};
  }
  const result = await stateApi(`/api/player-state/scopes/${currentScope}/observations`, pendingUpdate);
  pendingUpdate = null;
  draftUpdate = null;
  stateElement("draft-panel").hidden = true;
  showSnapshot(result);
  announce(`已保存观测，状态版本 ${result.revision}。`);
}

stateElement("scope-select").addEventListener("change", () => stateAction(() => loadScope(stateElement("scope-select").value)));
stateElement("refresh-state").addEventListener("click", () => stateAction(async () => { await loadScope(currentScope); await showBinding(); }));
stateElement("scope-form").addEventListener("submit", (event) => {
  event.preventDefault();
  stateAction(async () => {
    const created = await stateApi("/api/player-state/scopes", {label: stateElement("scope-label").value.trim(),
      server_id: stateElement("server-id").value.trim(), wipe_id: stateElement("wipe-id").value.trim()});
    await loadScopes();
    await loadScope(created.scope.id);
    announce("已创建空白档案；需要时点击关联到当前对话。");
  });
});

for (const [button, detach] of [["bind-state", false], ["unbind-state", true]]) {
  stateElement(button).addEventListener("click", () => stateAction(async () => {
    if (!detach) requireScope();
    await showBinding();
    if (!currentConversation) throw new Error("请先返回聊天创建一个对话。");
    await stateApi(`/api/conversations/${currentConversation}/player-state`, {scope_id: detach ? null : currentScope});
    await showBinding();
    announce(detach ? "已解除关联，档案本身仍保留。" : "已关联当前对话；返回聊天或刷新即可使用。");
  }));
}

stateElement("add-state-item").addEventListener("click", () => addInventoryRow());
stateElement("inventory-form").addEventListener("submit", (event) => {
  event.preventDefault();
  stateAction(async () => {
    const items = {};
    for (const row of stateElement("state-inventory-rows").children) {
      const inputs = row.querySelectorAll("input");
      const item = inputs[0].value.trim();
      if (Object.hasOwn(items, item)) throw new Error("同一物品请合并为一行。");
      items[item] = Number(inputs[1].value);
    }
    const mode = stateElement("inventory-mode").value;
    await saveChanges({inventory: [{container_id: stateElement("container-id").value.trim(), mode,
      complete: mode === "replace", items}]});
  });
});

stateElement("facts-form").addEventListener("submit", (event) => {
  event.preventDefault();
  stateAction(async () => {
    const facts = {};
    const clear_fields = [];
    for (const [key, control] of factControls) {
      if (control.mode.value === "clear") clear_fields.push(key);
      if (control.mode.value !== "set") continue;
      const value = control.input.value.trim();
      facts[key] = control.definition.input === "number" ? Number(value) :
        control.definition.input === "boolean" ? value === "true" :
        control.definition.input === "items" ? value.split(/[,，\s]+/).filter(Boolean) : value;
    }
    if (!Object.keys(facts).length && !clear_fields.length) throw new Error("请选择本次要更新或清除的字段。");
    await saveChanges({facts, clear_fields});
  });
});

stateElement("preview-form").addEventListener("submit", (event) => {
  event.preventDefault();
  stateAction(async () => {
    requireScope();
    stateElement("draft-panel").hidden = true;
    draftUpdate = null;
    const result = await stateApi(`/api/player-state/scopes/${currentScope}/preview`, {message: stateElement("state-description").value});
    if (result.status !== "draft") { announce(result.message); return; }
    draftUpdate = result.update;
    stateElement("draft-json").value = JSON.stringify(draftUpdate.changes, null, 2);
    stateElement("draft-summary").textContent = draftSummary(draftUpdate.changes);
    stateElement("draft-panel").hidden = false;
    announce(result.message);
  });
});

function draftSummary(changes) {
  const lines = [];
  for (const [key, value] of Object.entries(changes.facts || {})) lines.push(`${stateCatalog.fields[key]?.label || key}：${JSON.stringify(value)}`);
  for (const change of changes.inventory || []) {
    lines.push(`容器 ${change.container_id}；${change.complete ? "完整盘点，将替换原库存" : "局部更新，其他物品仍未知"}`);
    for (const [item, amount] of Object.entries(change.items)) lines.push(`  ${stateCatalog.items[item] || item} (${item}) × ${amount}`);
  }
  if (changes.event) lines.push(`事件：${changes.event}，相关状态将失效。`);
  if (changes.clear_fields?.length) lines.push(`清除字段：${changes.clear_fields.join("、")}`);
  if (changes.invalidate_containers?.length) lines.push(`失效容器：${changes.invalidate_containers.join("、")}`);
  return lines.join("\n") || "没有可保存的变更";
}

stateElement("draft-json").addEventListener("input", () => {
  try { stateElement("draft-summary").textContent = draftSummary(JSON.parse(stateElement("draft-json").value)); }
  catch { stateElement("draft-summary").textContent = "JSON 格式尚不合法，请检查后确认。"; }
});
stateElement("confirm-draft").addEventListener("click", () => stateAction(async () => {
  if (!draftUpdate) throw new Error("请先解析一条状态描述。");
  const changes = JSON.parse(stateElement("draft-json").value);
  if (JSON.stringify(changes) !== JSON.stringify(draftUpdate.changes)) {
    draftUpdate = {...draftUpdate, observation_id: crypto.randomUUID(), changes};
  }
  await saveChanges(changes, draftUpdate);
}));
stateElement("cancel-draft").addEventListener("click", () => { draftUpdate = null; stateElement("draft-panel").hidden = true; });
document.querySelectorAll("[data-event]").forEach((button) => button.addEventListener("click", () => stateAction(async () => {
  requireScope();
  if (!window.confirm("将相关观测标记为失效，需重新录入后用于计算。历史记录会保留。")) return;
  await saveChanges({event: button.dataset.event});
})));
stateElement("history-form").addEventListener("submit", (event) => {
  event.preventDefault();
  stateAction(async () => {
    requireScope();
    const history = await stateApi(`/api/player-state/scopes/${currentScope}?revision=${Number(stateElement("history-revision").value)}`);
    stateElement("history-json").textContent = JSON.stringify(history, null, 2);
  });
});

async function initializeState() {
  stateCatalog = await stateApi("/api/player-state/fields");
  for (const [id, name] of Object.entries(stateCatalog.items)) stateElement("item-ids").append(stateOption(id, name));
  buildFields();
  const scopes = await loadScopes();
  const binding = await showBinding();
  const selected = binding || storedValue("rust-coach-player-scope");
  if (scopes.some((scope) => scope.id === selected)) await loadScope(selected);
}

stateAction(initializeState);
