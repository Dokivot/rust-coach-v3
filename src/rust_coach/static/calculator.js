const element = (id) => document.getElementById(id);
let catalog;

function option(value, label) {
  const node = document.createElement("option");
  node.value = value;
  node.textContent = label;
  return node;
}

function labeled(label, control) {
  const node = document.createElement("label");
  node.textContent = label;
  node.append(control);
  return node;
}

function numberInput(value, minimum) {
  const node = document.createElement("input");
  node.type = "number";
  node.min = minimum;
  node.max = minimum === 1 ? 10000 : 1000000;
  node.step = 1;
  node.value = value;
  node.required = true;
  return node;
}

function addRow(kind, selected = "") {
  element("result-panel").hidden = true;
  const rows = element(`${kind}-rows`);
  if (rows.children.length >= (kind === "goal" ? 20 : 100)) return;
  const row = document.createElement("div");
  row.className = `item-row ${kind === "inventory" ? "inventory-row" : ""}`;
  const select = document.createElement("select");
  const ids = kind === "goal" ? catalog.craftable : Object.keys(catalog.items);
  for (const id of ids) select.append(option(id, `${catalog.items[id].name} · ${id}`));
  if (selected) select.value = selected;
  row.append(labeled("物品", select), labeled("数量", numberInput(1, kind === "goal" ? 1 : 0)));
  if (kind === "inventory") row.append(labeled("保留", numberInput(0, 0)));
  const remove = document.createElement("button");
  remove.type = "button";
  remove.className = "remove-row";
  remove.textContent = "移除";
  remove.addEventListener("click", () => {
    row.remove();
    element("result-panel").hidden = true;
  });
  row.append(remove);
  rows.append(row);
}

function readRows(kind) {
  const quantities = {};
  const reserves = {};
  for (const row of element(`${kind}-rows`).children) {
    const item = row.querySelector("select").value;
    const amounts = row.querySelectorAll("input");
    if (Object.hasOwn(quantities, item)) throw new Error("同一物品请合并为一行，避免重复计算库存。");
    quantities[item] = Number(amounts[0].value);
    if (kind === "inventory" && Number(amounts[1].value)) reserves[item] = Number(amounts[1].value);
  }
  return [quantities, reserves];
}

function updateOperation() {
  const raid = element("operation").value === "raid";
  element("raid-fields").hidden = !raid;
  element("craft-fields").hidden = raid;
  element("raid-fields").querySelectorAll("input, select").forEach((node) => { node.disabled = !raid; });
  element("craft-fields").querySelectorAll("input, select").forEach((node) => { node.disabled = raid; });
}

function updateTarget() {
  const target = catalog.targets[element("target").value];
  element("hp").max = target.hp;
  element("hp").placeholder = `留空按满血 ${target.hp}`;
  element("method").replaceChildren(option("", "比较已收录的单一方案"));
  for (const method of Object.keys(target.methods)) element("method").append(option(method, catalog.items[method].name));
}

element("operation").addEventListener("change", updateOperation);
for (const eventName of ["input", "change"]) {
  element("calculation-form").addEventListener(eventName, () => {
    element("result-panel").hidden = true;
    element("calculation-error").hidden = true;
  });
}
element("target").addEventListener("change", updateTarget);
element("add-goal").addEventListener("click", () => addRow("goal"));
element("add-inventory").addEventListener("click", () => addRow("inventory"));
element("inventory-known").addEventListener("change", () => {
  element("inventory-fields").hidden = !element("inventory-known").checked;
  element("inventory-fields").querySelectorAll("input, select").forEach((node) => { node.disabled = !element("inventory-known").checked; });
});

element("calculation-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  element("calculation-error").hidden = true;
  element("result-panel").hidden = true;
  try {
    const operation = element("operation").value;
    const [inventory, reserve] = element("inventory-known").checked ? readRows("inventory") : [null, {}];
    const request = {operation, inventory, reserve,
      workbench: element("workbench").value === "" ? null : Number(element("workbench").value),
      gunpowder_recipe: element("gunpowder-recipe").value,
      expand_fuel: element("expand-fuel").checked};
    if (operation === "raid") Object.assign(request, {
      target: element("target").value, count: Number(element("target-count").value), side: element("side").value,
      hp: element("hp").value === "" ? null : Number(element("hp").value),
      methods: element("method").value ? [element("method").value] : [],
    });
    else request.goals = readRows("goal")[0];
    element("calculation-fields").disabled = true;
    const response = await fetch("/api/calculations", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(request)});
    const body = await response.json();
    if (!response.ok) throw new Error(body.error?.message || "计算失败，请检查输入。");
    element("calculation-result").textContent = body.text;
    element("calculation-json").textContent = JSON.stringify(body, null, 2);
    element("result-panel").hidden = false;
    element("result-panel").scrollIntoView({behavior: "smooth", block: "start"});
  } catch (error) {
    element("calculation-error").textContent = error.message || "无法连接计算服务。";
    element("calculation-error").hidden = false;
  } finally { element("calculation-fields").disabled = false; }
});

async function initialize() {
  try {
    const response = await fetch("/api/calculations/catalog");
    if (!response.ok) throw new Error("规则目录暂时不可用。");
    catalog = await response.json();
    for (const [id, target] of Object.entries(catalog.targets)) element("target").append(option(id, target.name));
    element("target").value = "building:base-wall:stone";
    element("snapshot-note").textContent = `依据 RustData 快照 ${catalog.provenance.retrieved_at}。第三方数据，未核验游戏 build 或游戏内实测；不保证适用后续补丁。`;
    addRow("goal", "explosive.timed");
    updateTarget();
    updateOperation();
    element("calculation-fields").disabled = false;
  } catch (error) {
    element("calculation-error").textContent = error.message;
    element("calculation-error").hidden = false;
  }
}

initialize();
