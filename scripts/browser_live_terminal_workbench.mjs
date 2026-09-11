// 实时终端工作台浏览器验收：所有 API、WebSocket、命令均为本地 mock，绝不连接设备。
import assert from "node:assert/strict";
import { mkdir } from "node:fs/promises";

const imported = await import(process.env.PLAYWRIGHT_MODULE || "playwright");
const { chromium } = imported.default || imported;
const baseUrl = process.env.BASE_URL || "http://127.0.0.1:5173";
const output = "output/playwright";
const token = "synthetic-live-terminal-token";
const task = { id: "terminal-task", name: "专业终端模拟任务", resourceId: "terminal-resource", protocol: "SSH", ip: "192.0.2.230", port: 22, status: "COLLECTING", desiredState: "RUNNING", initialCommands: [], scheduledCommands: [], createdBy: "terminal-user", version: 1 };
const resource = { id: task.resourceId, name: "专业终端模拟设备", kind: "HIKVISION_NETWORK", ip: task.ip, version: 1 };
const text = [
  "INFO connection ready\n",
  "WARN disk threshold\n",
  "ERROR target-match one\n",
  "DEBUG target-match two\n",
  "TRACE target-match three\n",
  "<img src=x onerror=window.__terminalExecuted=true> literal HTML\n",
  "\u001b[31mFATAL ansi target-match four\u001b[0m\n",
  ...Array.from({ length: 80 }, (_, index) => `INFO filler-${index} target-match\n`),
].join("");
const commandRequests = [];
let admin = true;
const pageOf = items => ({ items, total: items.length, page: 1, pageSize: 100 });
const json = (route, body, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
const browser = await chromium.launch({ headless: true, channel: "chrome" });

async function assertLayout(page, width, label) {
  const metrics = await page.evaluate(() => ({ width: innerWidth, scrollWidth: document.documentElement.scrollWidth, terminal: document.querySelector(".terminal-console")?.getBoundingClientRect().toJSON(), workbench: document.querySelector(".logs-workbench")?.getBoundingClientRect().toJSON() }));
  assert.ok(metrics.scrollWidth <= width + 1, `${label} 不得横向溢出：${JSON.stringify(metrics)}`);
  assert.ok(metrics.terminal?.height > 180 && metrics.workbench?.height >= metrics.terminal?.height, `${label} 终端未获得可用高度：${JSON.stringify(metrics)}`);
}

try {
  await mkdir(output, { recursive: true });
  for (const width of [1440, 390, 320]) {
    const commandRequestStart = commandRequests.length;
    const context = await browser.newContext({ viewport: { width, height: 900 } });
    await context.addInitScript(currentToken => {
      sessionStorage.setItem("camera-log-record-token", currentToken);
      localStorage.setItem("camera-log-sidebar-collapsed", "false");
      window.__terminalExecuted = false;
    }, token);
    await context.route("**/api/v1/**", async route => {
      const request = route.request(), path = new URL(request.url()).pathname;
      if (path === "/api/v1/auth/me") return json(route, { user: { id: admin ? "terminal-user" : "viewer", username: "fixture", displayName: "终端验收", isAdmin: admin, enabled: true, mustChangePassword: false, scopes: admin ? ["*"] : ["logs:read"] } });
      if (request.method() === "GET" && path === "/api/v1/tasks") return json(route, pageOf([task]));
      if (request.method() === "GET" && path === "/api/v1/resources") return json(route, pageOf([resource]));
      if (request.method() === "GET" && path === `/api/v1/tasks/${task.id}`) return json(route, task);
      if (request.method() === "POST" && path === `/api/v1/tasks/${task.id}/commands`) { commandRequests.push(request.postDataJSON()); return json(route, { id: `command-${commandRequests.length}`, status: "QUEUED" }); }
      if (request.method() === "GET" && ["/api/v1/command-templates", "/api/v1/nodes", "/api/v1/admin/nodes", "/api/v1/service-tokens", "/api/v1/audit-events", "/api/v1/runtime-events"].includes(path)) return json(route, pageOf([]));
      if (request.method() === "GET" && path === "/api/v1/platform-settings") return json(route, { retentionDays: 7, version: 1 });
      if (request.method() === "GET" && path.endsWith("/log-hours")) return json(route, pageOf([]));
      throw new Error(`未模拟的请求：${request.method()} ${path}`);
    });
    await context.routeWebSocket("**/api/v1/tasks/*/logs", socket => {
      socket.onMessage(() => socket.send(JSON.stringify({ type: "data", data: Buffer.from(text).toString("base64"), fileId: "terminal-file", sessionId: "terminal-session", offset: 0, cursor: "terminal-cursor" })));
    });
    const page = await context.newPage();
    const errors = [];
    page.on("pageerror", error => errors.push(error.message));
    page.on("console", message => { if (message.type() === "error") errors.push(message.text()); });
    await page.goto(`${baseUrl}/#logs`, { waitUntil: "networkidle" });
    await page.getByRole("button", { name: "选择任务", exact: true }).click();
    const picker = page.getByRole("dialog", { name: "选择日志任务", exact: true });
    await picker.getByText(task.name, { exact: true }).click();
    await picker.getByRole("button", { name: "确认切换", exact: true }).click();
    await picker.waitFor({ state: "hidden" });
    const terminal = page.locator(".terminal-console");
    await terminal.waitFor();
    await page.waitForTimeout(300);
    await terminal.evaluate(element => { element.scrollTop = 0; });
    await terminal.locator(".log-line").filter({ hasText: "FATAL ansi target-match four" }).waitFor();
    await assertLayout(page, width, `${width}px 初始`);
    await page.screenshot({ path: `${output}/live-terminal-${width}-initial.png`, fullPage: true, animations: "disabled" });
    const semantic = await terminal.evaluate(element => ({
      html: element.querySelectorAll("img").length,
      executed: window.__terminalExecuted === true,
      controls: /\u001b|\^\[\[/.test(element.textContent || ""),
      error: element.querySelectorAll(".log-level-error").length,
      warning: element.querySelectorAll(".log-level-warning").length,
      info: element.querySelectorAll(".log-level-info").length,
      debug: element.querySelectorAll(".log-level-debug").length,
      trace: element.querySelectorAll(".log-level-trace").length,
    }));
    assert.equal(semantic.html, 0, "设备 HTML 必须作为文本渲染");
    assert.equal(semantic.executed, false, "设备 HTML 不得执行");
    assert.equal(semantic.controls, false, "终端控制码不得出现在展示层");
    for (const [tone, count] of Object.entries(semantic).filter(([key]) => ["error", "warning", "info", "debug", "trace"].includes(key))) assert.ok(count > 0, `缺少 ${tone} 语义着色`);

    await page.getByRole("button", { name: "查找日志", exact: true }).click();
    const find = page.getByRole("textbox", { name: "查找日志", exact: true });
    await find.fill("target-match");
    await page.getByText(/1\/\d+/, { exact: false }).waitFor();
    const before = await terminal.evaluate(element => element.scrollTop);
    await page.getByRole("button", { name: "下一个匹配", exact: true }).click();
    const after = await terminal.evaluate(element => element.scrollTop);
    assert.ok(after >= before, "下一个匹配必须定位虚拟行");
    await page.getByRole("button", { name: "上一个匹配", exact: true }).click();
    assert.ok(await terminal.locator(".log-search-active").count() >= 1, "活动匹配必须有独立高亮");

    const command = page.getByRole("textbox", { name: "输入手工命令", exact: true });
    const imeEvents = [];
    await command.evaluate(element => {
      for (const type of ["compositionstart", "compositionend", "keydown"])
        element.addEventListener(type, event => window.__terminalImeEvents.push({ type, isComposing: event.isComposing, keyCode: event.keyCode, key: event.key }));
    });
    await page.evaluate(() => { window.__terminalImeEvents = []; });
    await command.fill("show version");
    await command.press("Enter");
    await page.getByText("命令已提交", { exact: true }).waitFor();
    await command.fill("show");
    await page.getByRole("option", { name: "show version", exact: true }).waitFor();
    await command.press("ArrowUp");
    assert.equal(await command.inputValue(), "show version", "向上键必须回填本任务历史");
    await command.evaluate(element => element.dispatchEvent(new CompositionEvent("compositionstart", { bubbles: true, data: "中" })));
    await page.waitForTimeout(0);
    await command.press("Enter");
    await command.evaluate(element => element.dispatchEvent(new CompositionEvent("compositionend", { bubbles: true, data: "中" })));
    imeEvents.push(...await page.evaluate(() => window.__terminalImeEvents));
    assert.ok(imeEvents.some(event => event.type === "compositionstart"), "IME 验收必须实际触发原生 compositionstart");
    assert.equal(commandRequests.length, commandRequestStart + 1, "IME 组合期间 Enter 不得重复发送");
    const suggestion = page.locator(".command-suggestions");
    const suggestionBox = await suggestion.boundingBox();
    const terminalBox = await terminal.boundingBox();
    assert.ok(suggestionBox && terminalBox && suggestionBox.y >= terminalBox.y && suggestionBox.x >= 0 && suggestionBox.x + suggestionBox.width <= width + 1, "命令候选必须在可见工作台范围内");
    if (width > 700) {
      const beforeCollapse = await terminal.boundingBox();
      await page.getByRole("button", { name: "折叠导航栏", exact: true }).click();
      const afterCollapse = await terminal.boundingBox();
      assert.ok(afterCollapse.width > beforeCollapse.width, "折叠侧栏后终端必须扩宽");
      await page.getByRole("button", { name: "展开导航栏", exact: true }).click();
    }
    await assertLayout(page, width, `${width}px 交互后`);
    await page.screenshot({ path: `${output}/live-terminal-${width}-interactive.png`, fullPage: true, animations: "disabled" });
    assert.deepEqual(errors, [], `浏览器错误：${JSON.stringify(errors)}`);
    await context.close();
  }
  // 权限检查独立上下文，保证 canSend=false 时键盘 Enter 无法绕过。
  const context = await browser.newContext({ viewport: { width: 390, height: 844 } });
  admin = false;
  await context.addInitScript(currentToken => sessionStorage.setItem("camera-log-record-token", currentToken), token);
  await context.route("**/api/v1/**", async route => {
    const request = route.request(), path = new URL(request.url()).pathname;
    if (path === "/api/v1/auth/me") return json(route, { user: { id: "viewer", username: "viewer", displayName: "只读验收", scopes: ["logs:read", "tasks:read"], isAdmin: false, mustChangePassword: false } });
    if (request.method() === "GET" && path === "/api/v1/tasks") return json(route, pageOf([task]));
    if (request.method() === "GET" && path === "/api/v1/resources") return json(route, pageOf([resource]));
    if (request.method() === "GET" && path === `/api/v1/tasks/${task.id}`) return json(route, task);
    if (request.method() === "GET" && ["/api/v1/command-templates", "/api/v1/nodes", "/api/v1/admin/nodes", "/api/v1/service-tokens", "/api/v1/audit-events", "/api/v1/runtime-events"].includes(path)) return json(route, pageOf([]));
    if (request.method() === "GET" && path === "/api/v1/platform-settings") return json(route, { retentionDays: 7, version: 1 });
    if (request.method() === "GET" && path.endsWith("/log-hours")) return json(route, pageOf([]));
    throw new Error(`无权限验收未模拟请求：${request.method()} ${path}`);
  });
  await context.routeWebSocket("**/api/v1/tasks/*/logs", socket => socket.onMessage(() => socket.send(JSON.stringify({ type: "data", data: Buffer.from("INFO readonly\\n").toString("base64"), fileId: "readonly-file", sessionId: "readonly-session", offset: 0, cursor: "readonly-cursor" }))));
  const page = await context.newPage();
  await page.goto(`${baseUrl}/#logs`, { waitUntil: "networkidle" });
  await page.getByRole("heading", { name: "日志工作台", exact: true }).waitFor();
  await page.getByRole("button", { name: "选择任务", exact: true }).click();
  const picker = page.getByRole("dialog", { name: "选择日志任务", exact: true });
  await picker.getByText(task.name, { exact: true }).click();
  await picker.getByRole("button", { name: "确认切换", exact: true }).click();
  const command = page.getByRole("textbox", { name: "输入手工命令", exact: true });
  await command.waitFor();
  assert.equal(await command.isDisabled(), true, "无 commands:send 权限时手工命令输入必须禁用");
  const beforeForbiddenEnter = commandRequests.length;
  await command.press("Enter");
  assert.equal(commandRequests.length, beforeForbiddenEnter, "无 commands:send 权限时 Enter 不得发出命令");
  await context.close();
  admin = true;
  console.log(JSON.stringify({ passed: true, commandRequests: commandRequests.length, screenshots: [1440, 390, 320].flatMap(width => [`${output}/live-terminal-${width}-initial.png`, `${output}/live-terminal-${width}-interactive.png`]) }));
} finally {
  await browser.close();
}
