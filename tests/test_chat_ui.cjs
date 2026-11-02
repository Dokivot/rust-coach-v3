const assert = require("node:assert/strict");
const {readFileSync} = require("node:fs");
const {test} = require("node:test");
const vm = require("node:vm");

class Element {
  constructor(tagName) {
    this.tagName = tagName;
    this.children = [];
    this.textContent = "";
    this.events = {};
    this.value = "";
    this.style = {};
    this.classList = {add() {}, remove() {}};
  }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  addEventListener(name, callback) { this.events[name] = callback; }
  setAttribute() {}
  focus() {}
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

function deferred() {
  let resolve;
  const promise = new Promise(complete => { resolve = complete; });
  return {promise, resolve};
}

const flush = () => new Promise(resolve => setImmediate(resolve));
const response = (body, status = 200) => ({ok: status < 400, status, json: async () => body});
const attachment = identifier => ({id: identifier, thumbnail_url: "/thumbnail/" + identifier, width: 100, height: 100});

function turnFor(payload) {
  return {request_id: payload.request_id, user: payload.message, assistant: "已分析。", visual: {
    request_identity: {attachment_ids: payload.attachment_ids},
    attachments: payload.attachment_ids.map(attachment),
  }};
}

async function sendingUI({upload, send} = {}) {
  const elements = new Map();
  const element = selector => {
    if (!elements.has(selector)) elements.set(selector, new Element("div"));
    return elements.get(selector);
  };
  const calls = {uploads: [], messages: []};
  const urls = {created: [], revoked: []};
  let requestCount = 0;
  let conversationCount = 0;
  const context = vm.createContext({
    document: {
      querySelector: element, querySelectorAll: () => [],
      createElement: tagName => new Element(tagName), addEventListener() {},
    },
    URL: {
      createObjectURL() { const url = "blob:preview-" + urls.created.length; urls.created.push(url); return url; },
      revokeObjectURL: url => urls.revoked.push(url),
    },
    FormData: class { append(name, blob) { this.blob = blob; } },
    crypto: {randomUUID: () => "request-" + ++requestCount},
    localStorage: {getItem: () => null, setItem() {}},
    requestAnimationFrame: callback => callback(),
    fetch: async (path, options) => {
      if (path === "/api/config") return response({configured: true, vision: {enabled: true}});
      if (path === "/api/conversations") return response({id: "conversation-" + ++conversationCount, turns: []});
      if (path.endsWith("/attachments")) {
        calls.uploads.push({path, blob: options.body.blob});
        return upload ? upload(calls.uploads.length) : response(attachment("image-" + calls.uploads.length));
      }
      if (path.endsWith("/messages")) {
        const payload = JSON.parse(options.body);
        calls.messages.push(payload);
        return send ? send(payload, calls.messages.length) : response(turnFor(payload));
      }
      throw new Error("Unexpected request: " + path);
    },
  });
  context.window = context;
  vm.runInContext(readFileSync("src/rust_coach/static/vision.js", "utf8"), context);
  vm.runInContext(readFileSync("src/rust_coach/static/app.js", "utf8"), context);
  await flush();
  return {
    element, context, calls, urls,
    addImages(count = 1) {
      const files = Array.from({length: count}, () => ({type: "image/png", size: 100}));
      element("#vision-files").onchange({target: {files, value: "screenshot.png"}});
      return files;
    },
    submit(text) {
      if (text !== undefined) element("#message-input").value = text;
      return element("#chat-form").events.submit({preventDefault() {}});
    },
    rerender() { vm.runInContext("renderConversation();", context); },
  };
}

for (const message of ["", "看看这张图"]) {
  test("sending moves images immediately and replaces previews with saved images: " + (message || "image only"), async () => {
    const uploading = deferred();
    const replying = deferred();
    const ui = await sendingUI({upload: () => uploading.promise,
      send: (payload, attempt) => attempt === 1 ? replying.promise : response(turnFor(payload))});
    ui.addImages();
    const submission = ui.submit(message);
    assert.equal(ui.element("#message-input").value, "");
    assert.equal(ui.element("#vision-pending").hidden, true);
    assert.equal(ui.element("#vision-pending").children.length, 0);
    assert.equal(ui.element("#send").disabled, true);
    assert.equal(ui.element("#new-chat").disabled, true);
    assert.equal(ui.element("#vision-add").disabled, true);
    assert.equal(ui.element("#welcome").hidden, true);
    assert.equal(ui.element("#messages").children[0].children[2].src, ui.urls.created[0]);
    assert.equal(ui.element("#send-status").textContent, "正在上传图片…");
    assert.deepEqual(ui.urls.revoked, []);
    ui.rerender();
    assert.equal(ui.element("#messages").children.length, 2);
    assert.equal(ui.element("#messages").children[0].children[2].src, ui.urls.created[0]);
    await ui.submit();
    ui.addImages();
    assert.equal(ui.urls.created.length, 1);
    uploading.resolve(response(attachment("image-1")));
    await flush();
    assert.equal(ui.calls.messages.length, 1);
    assert.deepEqual(ui.calls.messages[0].attachment_ids, ["image-1"]);
    assert.equal(ui.element("#send-status").textContent, "正在思考…");
    assert.deepEqual(ui.urls.revoked, []);
    replying.resolve(response(turnFor(ui.calls.messages[0])));
    await submission;
    assert.equal(ui.element("#messages").children.length, 2);
    assert.equal(ui.element("#messages").children[0].children[2].src, "/thumbnail/image-1");
    assert.equal(ui.element("#vision-pending").hidden, true);
    assert.deepEqual(ui.urls.revoked, ui.urls.created);
    assert.equal(ui.element("#send").disabled, true);
    await ui.submit("接着解释一下");
    assert.deepEqual(ui.calls.messages[1].attachment_ids, []);
    assert.equal(ui.calls.uploads.length, 1);
    assert.equal(ui.element("#messages").children[0].children[2].src, "/thumbnail/image-1");
    assert.equal(ui.element("#messages").children[2].children.length, 2);
  });
}

for (const failure of ["server", "network"]) {
  test(failure + " failure restores the exact draft and retries with the same request and uploads", async () => {
    const ui = await sendingUI({send(payload, attempt) {
      if (attempt > 1) return response(turnFor(payload));
      if (failure === "network") throw new Error("Response connection lost");
      return response({error: {message: "服务暂不可用"}}, 503);
    }});
    ui.addImages(2);
    const text = "  帮我看一下\n这些材料  ";
    await ui.submit(text);
    assert.equal(ui.element("#message-input").value, text);
    assert.equal(ui.element("#vision-pending").hidden, false);
    assert.equal(ui.element("#vision-pending").children.length, 2);
    assert.equal(ui.element("#messages").children.length, 0);
    assert.equal(ui.element("#error").hidden, false);
    assert.equal(ui.element("#send").disabled, false);
    assert.deepEqual(ui.urls.revoked, []);
    await ui.submit();
    assert.deepEqual(ui.calls.messages[1], ui.calls.messages[0]);
    assert.equal(ui.calls.uploads.length, 2);
    assert.equal(ui.element("#messages").children.length, 2);
    assert.equal(ui.element("#messages").children[0].children.length, 4);
    assert.equal(ui.element("#error").hidden, true);
    assert.equal(ui.element("#message-input").value, "");
    assert.deepEqual(ui.urls.revoked, ui.urls.created);
  });
}

test("partial upload failure restores all files and retries only the missing upload", async () => {
  const ui = await sendingUI({upload: attempt => attempt === 2
    ? response({error: {message: "上传失败"}}, 500) : response(attachment("image-" + attempt))});
  const files = ui.addImages(2);
  await ui.submit();
  assert.equal(ui.calls.messages.length, 0);
  assert.equal(ui.element("#vision-pending").children.length, 2);
  assert.deepEqual(ui.urls.revoked, []);
  assert.equal(ui.element("#send").disabled, false);
  await ui.submit();
  assert.equal(ui.calls.uploads.length, 3);
  assert.equal(ui.calls.uploads[2].blob, files[1]);
  assert.deepEqual(ui.calls.messages[0].attachment_ids, ["image-1", "image-3"]);
  assert.deepEqual(ui.urls.revoked, ui.urls.created);
});

test("editing restored attachments creates a new request and releases only discarded previews", async () => {
  const ui = await sendingUI({send: (payload, attempt) => attempt === 1
    ? response({error: {message: "失败"}}, 500) : response(turnFor(payload))});
  ui.addImages(2);
  await ui.submit("看看这些");
  ui.element("#vision-pending").children[1].children[1].events.click();
  assert.deepEqual(ui.urls.revoked, [ui.urls.created[1]]);
  ui.addImages();
  await ui.submit();
  assert.notEqual(ui.calls.messages[1].request_id, ui.calls.messages[0].request_id);
  assert.deepEqual(ui.calls.messages[1].attachment_ids, ["image-1", "image-3"]);
  assert.equal(ui.calls.uploads.length, 3);
  assert.deepEqual([...ui.urls.revoked].sort(), [...ui.urls.created].sort());
});

test("editing restored text creates a new request without reuploading images", async () => {
  const ui = await sendingUI({send: (payload, attempt) => attempt === 1
    ? response({error: {message: "失败"}}, 500) : response(turnFor(payload))});
  ui.addImages();
  await ui.submit("原问题");
  await ui.submit("新问题");
  assert.notEqual(ui.calls.messages[1].request_id, ui.calls.messages[0].request_id);
  assert.equal(ui.calls.messages[1].message, "新问题");
  assert.equal(ui.calls.uploads.length, 1);
});

test("starting a new conversation releases a restored draft without carrying its attachments", async () => {
  const ui = await sendingUI({send: (payload, attempt) => attempt === 1
    ? response({error: {message: "失败"}}, 500) : response(turnFor(payload))});
  ui.addImages();
  await ui.submit("原问题");
  await ui.element("#new-chat").events.click();
  assert.equal(ui.element("#vision-pending").hidden, true);
  assert.equal(ui.element("#message-input").value, "");
  assert.deepEqual(ui.urls.revoked, ui.urls.created);
  await ui.submit("原问题");
  assert.deepEqual(ui.calls.messages[1].attachment_ids, []);
  assert.notEqual(ui.calls.messages[1].request_id, ui.calls.messages[0].request_id);
  assert.equal(ui.calls.uploads.length, 1);
});
