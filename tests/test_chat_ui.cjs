const assert = require("node:assert/strict");
const {readFileSync} = require("node:fs");
const {test} = require("node:test");
const vm = require("node:vm");

class Element {
  constructor(tagName) {
    this.tagName = tagName;
    this.children = [];
    this.textContent = "";
  }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  addEventListener() {}
  set innerHTML(value) { throw new Error("Untrusted content must use textContent"); }
}

function render(turns) {
  const elements = new Map();
  const context = vm.createContext({
    document: {
      querySelector(selector) {
        if (!elements.has(selector)) elements.set(selector, new Element("div"));
        return elements.get(selector);
      },
      createElement: tagName => new Element(tagName),
    },
    VisionUI: {init() {}},
    fetch: () => new Promise(() => {}),
    requestAnimationFrame: callback => callback(),
    fixture: {turns},
  });
  vm.runInContext(readFileSync("src/rust_coach/static/app.js", "utf8"), context);
  vm.runInContext("conversation = fixture; renderConversation();", context);
  return {context, messages: elements.get("#messages")};
}

test("details expose reports and sources as inert text after the short answer", () => {
  const source = "<img src=x onerror=alert(1)>\nhttps://example.test/source";
  const {context, messages} = render([{
    user: "合成还差多少？", assistant: "legacy full report",
    presentation: {context: {language: "zh"}, blocks: [
      {placement: "main", text: "还差 100 硫磺。"},
      {placement: "detail", text: "计算过程：100 + 100 = 200"},
      {placement: "detail", text: source},
    ]},
  }]);
  const assistant = messages.children[1];
  assert.equal(assistant.children[1].textContent, "还差 100 硫磺。");
  const details = assistant.children[2];
  assert.equal(details.tagName, "details");
  assert.equal(details.open, undefined);
  assert.equal(details.children[0].tagName, "summary");
  assert.match(details.children[0].textContent, /查看/);
  assert.equal(details.children[2].textContent, source);
  assert.equal(details.children[2].children.length, 0);
  vm.runInContext("renderConversation();", context);
  assert.equal(messages.children.length, 2);
  assert.equal(messages.children[1].children[2].children[2].textContent, source);
});

test("legacy and detailed answers avoid empty or duplicate disclosures", () => {
  const {messages} = render([
    {user: "old", assistant: "legacy answer"},
    {user: "new", assistant: "fallback", presentation: {blocks: [
      {placement: "main", text: "Full report and sources"},
    ]}},
  ]);
  assert.equal(messages.children[1].children[1].textContent, "legacy answer");
  assert.equal(messages.children[1].children.length, 2);
  assert.equal(messages.children[3].children[1].textContent, "Full report and sources");
  assert.equal(messages.children[3].children.length, 2);
});

test("English answers use an English disclosure label", () => {
  const {messages} = render([{user: "Hi", assistant: "Answer", presentation: {
    context: {language: "en"}, blocks: [{placement: "detail", text: "Source"}],
  }}]);
  assert.equal(messages.children[1].children[1].textContent, "Answer");
  assert.equal(messages.children[1].children[2].children[0].textContent, "View calculations, observations and sources");
});
