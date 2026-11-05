const assert = require("node:assert/strict");
const {readFileSync} = require("node:fs");
const {test} = require("node:test");
const vm = require("node:vm");

class Element {
  constructor(tag) { this.tagName = tag; this.children = []; this.events = {}; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  addEventListener(name, callback) { this.events[name] = callback; }
  set innerHTML(value) { throw new Error("Use textContent"); }
}

test("diagnostics show failed and unfinished runs as inert text and report loading errors", async () => {
  const elements = new Map();
  const context = vm.createContext({document: {
    querySelector(selector) {
      if (!elements.has(selector)) elements.set(selector, new Element("div"));
      return elements.get(selector);
    },
    createElement: tag => new Element(tag),
  }, fetch: async () => ({ok: true, json: async () => ({total_runs: 101, truncated: true,
    summary: {recording_status: "incomplete"}, runs: [{started_at: "fixture", kind: "chat",
      status: "failed", duration_ms: 25, summary: {model_calls: 1}, error_code: "<script>untrusted</script>"},
    {started_at: "fixture", kind: "chat", status: "running", duration_ms: null, summary: {model_calls: 1}}]})})});
  vm.runInContext(readFileSync("src/rust_coach/static/performance.js", "utf8"), context);
  await new Promise(resolve => setImmediate(resolve));
  assert.match(elements.get("#status").textContent, /仅展示最近 100 条/);
  assert.match(elements.get("#status").textContent, /incomplete/);
  const records = elements.get("#runs").children;
  assert.match(records[0].children[0].textContent, /尚未结束/);
  assert.match(records[1].children[1].textContent, /<script>untrusted/);
  assert.equal(records[1].children[1].children.length, 0);
  context.fetch = async () => ({ok: false});
  await elements.get("#refresh").events.click();
  assert.match(elements.get("#status").textContent, /无法读取/);
  assert.equal(elements.get("#refresh").disabled, false);
});
