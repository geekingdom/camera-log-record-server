// 实时终端工作台浏览器验收：所有 API、WebSocket、命令均为本地 mock，绝不连接设备。
import assert from "node:assert/strict";
import { mkdir } from "node:fs/promises";

const imported = await import(process.env.PLAYWRIGHT_MODULE || "playwright");
const { chromium } = imported.default || imported;
const baseUrl = process.env.BASE_URL || "http://127.0.0.1:5173";
const output = "output/playwright";
const token = "synthetic-live-terminal-token";
const task = { id: "terminal-task", name: "专业终端模拟任务", resourceId: "terminal-resource", protocol: "SSH", ip: "192.0.2.230", port: 22, status: "COLLECTING", desiredState: "RUNNING", initialCommands: [], scheduledCommands: [], createdBy: "terminal-user", version: 1 };
const otherTask = { ...task, id: "terminal-task-next", name: "切换后的模拟任务", version: 2 };
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
const debugError = "调试解密服务返回了不可恢复的认证失败；为保护正在持续写入的日志，普通命令保持阻断，等待设备侧调试会话恢复后再次尝试。".repeat(24) + "【调试解密错误详情结束】";
let admin = true;
const pageOf = items => ({ items, total: items.length, page: 1, pageSize: 100 });
const json = (route, body, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
const browser = await chromium.launch({ headless: true, channel: "chrome" });

async function assertLayout(page, width, label) {
  const metrics = await page.evaluate(() => ({ width: innerWidth, scrollWidth: document.documentElement.scrollWidth, terminal: document.querySelector(".terminal-console")?.getBoundingClientRect().toJSON(), workbench: document.querySelector(".logs-workbench")?.getBoundingClientRect().toJSON() }));
  assert.ok(metrics.scrollWidth <= width + 1, `${label} 不得横向溢出：${JSON.stringify(metrics)}`);
  assert.ok(metrics.terminal?.height > 180 && metrics.workbench?.height >= metrics.terminal?.height, `${label} 终端未获得可用高度：${JSON.stringify(metrics)}`);
}

async function assertDebugNotification(page, width, state) {
  const metrics = await page.evaluate(() => {
    const box = selector => document.querySelector(selector)?.getBoundingClientRect().toJSON();
    return {
      width: innerWidth,
      scrollWidth: document.documentElement.scrollWidth,
      runtime: box(".runtime-terminal"),
      terminal: box(".terminal-console"),
      command: document.querySelector('input[aria-label="输入手工命令"]')?.getBoundingClientRect().toJSON(),
      notice: box(".el-notification.live-debug-notification"),
      noticeScrollHeight: document.querySelector(".el-notification.live-debug-notification .el-notification__content")?.scrollHeight,
      noticeClientHeight: document.querySelector(".el-notification.live-debug-notification .el-notification__content")?.clientHeight,
    };
  });
  assert.ok(metrics.scrollWidth <= width + 1, `${width}px 长错误不得横向溢出：${JSON.stringify(metrics)}`);
  assert.ok(metrics.runtime && metrics.terminal && metrics.command && metrics.notice, `${width}px 缺少通知布局元素：${JSON.stringify(metrics)}`);
  assert.ok(metrics.terminal.bottom <= metrics.command.y + 1, `${width}px 日志不得遮挡手工命令：${JSON.stringify(metrics)}`);
  assert.ok(metrics.notice.x >= -1 && metrics.notice.y >= -1 && metrics.notice.x + metrics.notice.width <= width + 1, `${width}px ${state} 通知必须位于右上可视区：${JSON.stringify(metrics)}`);
  const notice = page.locator(".el-notification.live-debug-notification");
  const command = page.getByRole("textbox", { name: "输入手工命令", exact: true });
  const description = notice.locator(".el-notification__content");
  assert.match(await description.textContent(), /调试解密错误详情结束/, `${width}px ${state} 通知不得丢失错误末行`);
  assert.ok(metrics.noticeScrollHeight > metrics.noticeClientHeight, `${width}px ${state} 长错误应在通知内容区内部滚动：${JSON.stringify(metrics)}`);
  await description.evaluate(element => { element.scrollTop = element.scrollHeight; });
  const [noticeBox, descriptionBox] = await Promise.all([notice.boundingBox(), description.boundingBox()]);
  assert.ok(noticeBox && descriptionBox && descriptionBox.y + descriptionBox.height <= noticeBox.y + noticeBox.height, `${width}px ${state} 长错误末行必须在通知内部可读：${JSON.stringify(metrics)}`);
  await command.scrollIntoViewIfNeeded();
  const visible = await page.evaluate(() => {
    const rect = element => element?.getBoundingClientRect();
    const noticeBox = rect(document.querySelector(".el-notification.live-debug-notification"));
    const commandBox = rect(document.querySelector('input[aria-label="输入手工命令"]'));
    return Boolean(noticeBox && commandBox && noticeBox.top >= 0 && noticeBox.bottom <= innerHeight && commandBox.top >= 0 && commandBox.bottom <= innerHeight);
  });
  assert.ok(visible, `${width}px ${state} 通知和手工命令必须可访问：${JSON.stringify(metrics)}`);
}

try {
  await mkdir(output, { recursive: true });
  for (const width of [1440, 390, 320]) {
    const commandRequestStart = commandRequests.length;
    let taskReads = 0;
    let showDebugError = false;
    const context = await browser.newContext({ viewport: { width, height: 900 } });
    await context.addInitScript(currentToken => {
      sessionStorage.setItem("camera-log-record-token", currentToken);
      localStorage.setItem("camera-log-sidebar-collapsed", "false");
      window.__terminalExecuted = false;
    }, token);
    await context.route("**/api/v1/**", async route => {
      const request = route.request(), path = new URL(request.url()).pathname;
      if (path === "/api/v1/auth/me") return json(route, { user: { id: admin ? "terminal-user" : "viewer", username: "fixture", displayName: "终端验收", isAdmin: admin, enabled: true, mustChangePassword: false, scopes: admin ? ["*"] : ["logs:read"] } });
      if (request.method() === "GET" && path === "/api/v1/tasks") return json(route, pageOf([task, otherTask]));
      if (request.method() === "GET" && path === "/api/v1/resources") return json(route, pageOf([resource]));
      if (request.method() === "GET" && [task.id, otherTask.id].some(id => path === `/api/v1/tasks/${id}`)) {
        taskReads += 1;
        return json(route, path.endsWith(task.id) && showDebugError ? {
          ...task,
          commandBlocked: showDebugError === "blocked",
          debugError,
        } : task);
      }
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
    showDebugError = "blocked";
    const blockedNotice = page.locator(".el-notification.live-debug-notification");
    await blockedNotice.getByText("命令通道尚未恢复，日志采集继续", { exact: true }).waitFor();
    await page.waitForTimeout(350);
    await assertDebugNotification(page, width, "命令阻断");
    await page.waitForTimeout(3200);
    assert.equal(await blockedNotice.count(), 1, `${width}px 状态轮询不得重复创建相同调试通知`);
    await page.screenshot({ path: `${output}/live-terminal-${width}-debug-error-blocked.png`, fullPage: true, animations: "disabled" });
    await blockedNotice.locator(".el-notification__closeBtn").click();
    await blockedNotice.waitFor({ state: "hidden" });
    await page.waitForTimeout(3200);
    assert.equal(await blockedNotice.count(), 0, `${width}px 手动关闭后同一状态轮询不得重新弹出通知`);
    showDebugError = "recovered";
    await blockedNotice.getByText("调试切换失败，普通命令与日志采集继续", { exact: true }).waitFor();
    await page.waitForTimeout(350);
    await assertDebugNotification(page, width, "命令恢复");
    assert.equal(await command.isDisabled(), false, "命令恢复后保留调试错误时手工命令必须可用");
    await page.screenshot({ path: `${output}/live-terminal-${width}-debug-error-recovered.png`, fullPage: true, animations: "disabled" });
    if (width === 1440) {
      // Element Plus 悬停通知会暂停倒计时；手动关闭按钮正位于新通知上方，先移出再验自动关闭。
      await page.mouse.move(0, 0);
      await blockedNotice.waitFor({ state: "hidden", timeout: 12_000 });
      assert.equal(await blockedNotice.count(), 0, "通知必须在10秒后自动消失且轮询不得重新创建");
    }
    if (width === 390) {
      // 窄屏长通知暂时覆盖顶部按钮；键盘切换保留通知开启状态，以验证卸载清理。
      const switchTask = page.getByRole("button", { name: "切换任务", exact: true });
      await switchTask.focus();
      await switchTask.press("Enter");
      const nextPicker = page.getByRole("dialog", { name: "选择日志任务", exact: true });
      await nextPicker.getByText(otherTask.name, { exact: true }).click();
      await nextPicker.getByRole("button", { name: "确认切换", exact: true }).click();
      await nextPicker.waitFor({ state: "hidden" });
      await blockedNotice.waitFor({ state: "hidden" });
      assert.equal(await blockedNotice.count(), 0, "切换任务时必须关闭前一个任务的调试通知");
    }
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
