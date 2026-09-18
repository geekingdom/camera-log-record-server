// 任务创建默认值验收：所有 API 使用受控内存响应，覆盖凭据和模板异步响应的旧表单隔离。
import assert from "node:assert/strict";
import { mkdir } from "node:fs/promises";

const imported = await import(process.env.PLAYWRIGHT_MODULE || "playwright");
const { chromium } = imported.default || imported;
const baseUrl = process.env.BROWSER_BASE_URL || "http://127.0.0.1:5173";
const screenshots = process.env.BROWSER_SCREENSHOTS || "output/playwright";
const resource = {
  id: "network-resource",
  name: "凭据默认值设备",
  kind: "HIKVISION_NETWORK",
  ip: "192.0.2.41",
  username: "public-device-user",
  authType: "BASIC",
  version: 1,
  createdBy: "owner",
  healthStatus: "ONLINE",
};
const offlineResource = {
  ...resource,
  id: "offline-network-resource",
  name: "离线串口任务设备",
  ip: "192.0.2.42",
  healthStatus: "OFFLINE",
};
const initialCommands = [
  { command: "first-command", newline: "\n", delaySeconds: 0, prompt: null, timeoutSeconds: 30 },
  { command: "second-command", newline: "\n", delaySeconds: 0, prompt: null, timeoutSeconds: 30 },
];
const scheduledCommands = [{ command: "scheduled-command", totalExecutions: 3, intervalSeconds: 15 }];
const pageOf = (items) => ({ items, total: items.length, page: 1, pageSize: 100 });
const deferred = () => {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
};
const browser = await chromium.launch({ headless: true, channel: "chrome" });

try {
  await mkdir(screenshots, { recursive: true });
  const credentialResponses = [];
  const templateResponses = [];
  const templateCreates = [];
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  await context.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    const method = request.method();
    const json = (body, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
    if (path === "/api/v1/auth/me") return json({ user: {
      id: "owner", username: "owner", displayName: "资源所有者", isAdmin: false,
      enabled: true, mustChangePassword: false,
      scopes: ["tasks:read", "tasks:create", "tasks:write", "resources:write", "templates:read", "templates:write"],
    } });
    if (path === "/api/v1/resources" && method === "GET") return json(pageOf([resource, offlineResource]));
    if (path === "/api/v1/tasks" && method === "GET") return json(pageOf([]));
    if (path === "/api/v1/nodes" && method === "GET") return json(pageOf([]));
    if (path === "/api/v1/command-templates" && method === "GET") return json(pageOf([]));
    if ([resource.id, offlineResource.id].some((id) => path === `/api/v1/resources/${id}/credential`) && method === "POST") {
      const response = deferred();
      credentialResponses.push(response);
      return response.promise.then((outcome) => json(outcome.body ?? outcome, outcome.status ?? 200));
    }
    if (path === "/api/v1/command-templates" && method === "POST") {
      const body = request.postDataJSON();
      templateCreates.push(body);
      const response = deferred();
      templateResponses.push(response);
      return response.promise.then((body) => json(body));
    }
    return json(pageOf([]));
  });
  const page = await context.newPage();
  page.setDefaultTimeout(8_000);
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto(baseUrl, { waitUntil: "domcontentloaded" });
  await page.getByRole("row", { name: /凭据默认值设备/ }).waitFor();

  // 离线网络资源仍可从快捷入口创建 Telnet 串口任务，SSH 和 Telnet 设备选项必须禁用。
  const offlineRow = page.getByRole("row", { name: /离线串口任务设备/ });
  await offlineRow.getByLabel("新建采集任务").click();
  await page.getByRole("dialog", { name: "新建采集任务" }).waitFor();
  await page.locator(".el-form-item").filter({ hasText: "连接协议" }).locator(".el-select").click();
  assert.equal(await page.getByRole("option", { name: "SSH", exact: true }).getAttribute("aria-disabled"), "true");
  assert.equal(await page.getByRole("option", { name: "Telnet 设备", exact: true }).getAttribute("aria-disabled"), "true");
  assert.notEqual(await page.getByRole("option", { name: "Telnet 串口", exact: true }).getAttribute("aria-disabled"), "true");
  assert.equal(await page.getByText("当前认证未成功，只能创建 Telnet 串口任务", { exact: true }).count(), 1);
  await page.getByRole("option", { name: "Telnet 串口", exact: true }).click();
  credentialResponses.shift().resolve({ username: "owner-device-user", password: "owner-secret" });
  await page.getByRole("dialog", { name: "新建采集任务" }).getByRole("button", { name: "关闭", exact: true }).click();
  await page.getByRole("dialog", { name: "新建采集任务" }).waitFor({ state: "hidden" });

  async function openTaskEditor() {
    await page.getByRole("row", { name: /凭据默认值设备/ }).getByLabel("新建采集任务").click();
    await page.getByRole("dialog", { name: "新建采集任务" }).waitFor();
    await page.getByLabel("用户名", { exact: true }).waitFor();
  }
  async function closeTaskEditor() {
    await page.getByRole("dialog", { name: "新建采集任务" }).getByRole("button", { name: "关闭", exact: true }).click();
    await page.getByRole("dialog", { name: "新建采集任务" }).waitFor({ state: "hidden" });
  }

  // 首次打开由资源选择触发凭据读取，响应前只展示公开用户名，不能预先泄露密码。
  await openTaskEditor();
  await page.waitForTimeout(100);
  assert.equal(credentialResponses.length, 1, "打开网络资源任务必须请求一次专用凭据接口");
  assert.equal(await page.getByLabel("用户名", { exact: true }).inputValue(), resource.username);
  assert.equal(await page.getByLabel("密码", { exact: true }).inputValue(), "");
  credentialResponses.shift().resolve({ username: "owner-device-user", password: "owner-secret" });
  await page.waitForTimeout(100);
  assert.equal(await page.getByLabel("用户名", { exact: true }).inputValue(), "owner-device-user");
  const password = page.getByLabel("密码", { exact: true });
  assert.equal(await password.inputValue(), "owner-secret", "资源所有者应获得已保存密码的回填");
  await password.locator("xpath=..").locator(".el-input__suffix").click();
  await page.waitForTimeout(50);
  assert.equal(await password.getAttribute("type"), "text", "密码显示控件必须能够显示明文");
  await page.screenshot({ path: `${screenshots}/task-creation-credential-reveal.png`, fullPage: true, animations: "disabled" });
  await closeTaskEditor();

  // 延迟响应到达期间的手工输入必须优先，不能被自动回填覆盖。
  await openTaskEditor();
  await page.waitForTimeout(100);
  assert.equal(credentialResponses.length, 1, "第二次打开应发起新的凭据请求");
  await page.getByLabel("用户名", { exact: true }).fill("manual-user");
  await page.getByLabel("密码", { exact: true }).fill("manual-password");
  credentialResponses.shift().resolve({ username: "late-user", password: "late-password" });
  await page.waitForTimeout(150);
  assert.equal(await page.getByLabel("用户名", { exact: true }).inputValue(), "manual-user");
  assert.equal(await page.getByLabel("密码", { exact: true }).inputValue(), "manual-password");

  // 从实际命令组件填充两类命令，另存为模板请求必须保留数组顺序和私有默认值。
  const firstDraft = page.getByLabel("新增初始化命令", { exact: true });
  await firstDraft.fill(initialCommands[0].command);
  await firstDraft.press("Enter");
  await firstDraft.fill(initialCommands[1].command);
  await firstDraft.press("Enter");
  await page.getByRole("button", { name: "添加定时命令", exact: true }).click();
  const scheduledRow = page.locator(".schedule-edit-row");
  await scheduledRow.getByLabel("命令", { exact: true }).fill(scheduledCommands[0].command);
  await scheduledRow.getByLabel("执行总次数", { exact: true }).fill(String(scheduledCommands[0].totalExecutions));
  await scheduledRow.getByLabel("间隔（秒）", { exact: true }).fill(String(scheduledCommands[0].intervalSeconds));
  await page.getByRole("button", { name: "另存为模板", exact: true }).click();
  const prompt = page.getByRole("dialog", { name: "保存为命令模板" });
  await prompt.getByRole("textbox").fill("快捷私有模板");
  await prompt.getByRole("button", { name: "保存", exact: true }).click();
  await page.waitForTimeout(100);
  assert.equal(templateResponses.length, 1, "快捷保存应提交一次模板创建请求");
  assert.deepEqual(templateCreates[0], {
    name: "快捷私有模板", description: "", initialCommands, scheduledCommands,
    sharedWith: [], sharedWithAll: false,
  }, "快捷保存必须按当前顺序复制初始和定时命令，并默认私有");
  templateResponses.shift().resolve({ id: "saved-template", version: 3, createdBy: "owner", name: "快捷私有模板", description: "", initialCommands, scheduledCommands, sharedWith: [], sharedWithAll: false });
  await page.getByText("命令模板已保存", { exact: true }).waitFor();
  await page.screenshot({ path: `${screenshots}/task-creation-template-saved.png`, fullPage: true, animations: "disabled" });

  // 专用接口失败同样不能清除在等待期间已输入的独立凭据。
  await closeTaskEditor();
  await openTaskEditor();
  await page.waitForTimeout(100);
  await page.getByLabel("用户名", { exact: true }).fill("failure-manual-user");
  await page.getByLabel("密码", { exact: true }).fill("failure-manual-password");
  credentialResponses.shift().resolve({ status: 403, body: { detail: "无权读取资源凭据" } });
  await page.waitForTimeout(150);
  assert.equal(await page.getByLabel("用户名", { exact: true }).inputValue(), "failure-manual-user");
  assert.equal(await page.getByLabel("密码", { exact: true }).inputValue(), "failure-manual-password");

  // 已进入保存请求的旧抽屉关闭后，响应不得把模板关联到后来打开的任务。
  await closeTaskEditor();
  await openTaskEditor();
  await page.waitForTimeout(100);
  credentialResponses.shift().resolve({ username: "owner-device-user", password: "owner-secret" });
  await page.getByLabel("新增初始化命令", { exact: true }).fill("old-form-command");
  await page.getByLabel("新增初始化命令", { exact: true }).press("Enter");
  await page.getByRole("button", { name: "另存为模板", exact: true }).click();
  const racePrompt = page.getByRole("dialog", { name: "保存为命令模板" });
  await racePrompt.getByRole("textbox").fill("旧抽屉模板");
  await racePrompt.getByRole("button", { name: "保存", exact: true }).click();
  await page.waitForTimeout(100);
  assert.equal(templateResponses.length, 1, "旧抽屉的模板请求应已被挂起");
  await closeTaskEditor();
  await openTaskEditor();
  await page.waitForTimeout(100);
  credentialResponses.shift().resolve({ username: "owner-device-user", password: "owner-secret" });
  templateResponses.shift().resolve({ id: "late-template", version: 1, createdBy: "owner", name: "旧抽屉模板", description: "", initialCommands: [{ ...initialCommands[0], command: "old-form-command" }], scheduledCommands: [], sharedWith: [], sharedWithAll: false });
  await page.waitForTimeout(150);
  assert.equal(await page.getByLabel("初始化命令 1", { exact: true }).count(), 0, "旧抽屉的模板响应不得修改重开后的命令草稿");
  assert.equal(await page.locator(".section-heading .el-select input").inputValue(), "", "旧模板响应不得绑定到重开后的任务");
  assert.deepEqual(errors, [], "页面不应产生未处理异常");
  await page.screenshot({ path: `${screenshots}/task-creation-race-isolation.png`, fullPage: true, animations: "disabled" });
  await page.setViewportSize({ width: 390, height: 844 });
  const mobileDraft = page.getByLabel("新增初始化命令", { exact: true });
  await mobileDraft.fill("mobile-template-command");
  await mobileDraft.press("Enter");
  const quickSave = page.getByRole("button", { name: "另存为模板", exact: true });
  await quickSave.scrollIntoViewIfNeeded();
  const mobileBounds = await quickSave.evaluate((button) => {
    const drawer = button.closest(".el-drawer")?.getBoundingClientRect();
    const box = button.getBoundingClientRect();
    return { drawer: drawer && { x: drawer.x, width: drawer.width }, button: { x: box.x, right: box.right, y: box.y, bottom: box.bottom },
      pageWidth: document.documentElement.scrollWidth, viewportWidth: window.innerWidth };
  });
  assert.ok(mobileBounds.drawer && mobileBounds.button.x >= mobileBounds.drawer.x &&
    mobileBounds.button.right <= mobileBounds.drawer.x + mobileBounds.drawer.width + 1 &&
    mobileBounds.button.y >= 0 && mobileBounds.button.bottom <= 844,
  `移动端快捷保存按钮必须在抽屉内可达：${JSON.stringify(mobileBounds)}`);
  assert.ok(mobileBounds.pageWidth <= mobileBounds.viewportWidth + 1,
    `移动端任务编辑器不能产生页面横向溢出：${JSON.stringify(mobileBounds)}`);
  await page.screenshot({ path: `${screenshots}/task-creation-mobile.png`, fullPage: true, animations: "disabled" });
  await context.close();
  console.log("任务创建资源凭据默认、手工输入保护和快捷模板异步隔离：通过");
} finally {
  await browser.close();
}
