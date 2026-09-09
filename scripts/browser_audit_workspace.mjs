// 隔离浏览器验收：真实 Cookie 会话、三类事件接口和审计排障界面，不使用页面或接口替身。
import { mkdir } from "node:fs/promises";
import { randomBytes } from "node:crypto";

process.loadEnvFile(".env");

const baseUrl = process.env.BROWSER_BASE_URL?.replace(/\/$/, "");
const username = process.env.BROWSER_ADMIN_USERNAME;
const initialPassword = process.env.BROWSER_ADMIN_PASSWORD;
const screenshots = process.env.BROWSER_SCREENSHOTS || "output/playwright";

if (process.env.BROWSER_ISOLATED_AUTH !== "true")
  throw new Error("审计浏览器验收仅允许 BROWSER_ISOLATED_AUTH=true 的隔离环境");
if (!baseUrl || !username || !initialPassword)
  throw new Error("审计浏览器验收需要隔离 URL、管理员用户名和初始密码");

const origin = new URL(baseUrl).origin;
const playwrightModule = await import(process.env.PLAYWRIGHT_MODULE || "playwright");
const { chromium } = playwrightModule.default || playwrightModule;
const browser = await chromium.launch({ headless: true, channel: "chrome" });
const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
const page = await context.newPage();
page.setDefaultTimeout(12_000);

const errors = [];
page.on("pageerror", error => errors.push(`页面异常：${error.message}`));
page.on("console", message => {
  // 首次会话探测的 401 与本验收主动触发的 409 会被 Chrome 输出为资源错误，页面逻辑已显式断言其状态。
  if (message.type() === "error" && !message.text().startsWith("Failed to load resource:"))
    errors.push(`控制台异常：${message.text()}`);
});

function randomPassword() {
  return randomBytes(24).toString("base64url");
}

function isApiResponse(response, path) {
  const url = new URL(response.url());
  return url.origin === origin && url.pathname === path;
}

async function expectCookieResponse(response, path) {
  if (response.status() !== 200) throw new Error(`${path} 返回 HTTP ${response.status()}`);
  const headers = await response.request().allHeaders();
  if ("authorization" in headers) throw new Error(`${path} 不应发送 Authorization 请求头`);
}

async function waitForApi(path, action) {
  const received = page.waitForResponse(response => isApiResponse(response, path));
  await action();
  const response = await received;
  await expectCookieResponse(response, path);
  return response;
}

async function login() {
  await page.goto(baseUrl, { waitUntil: "networkidle" });
  await page.getByLabel("用户名", { exact: true }).fill(username);
  await page.getByLabel("密码", { exact: true }).fill(initialPassword);
  await page.getByRole("button", { name: "登录", exact: true }).click();

  const passwordDialog = page.getByRole("dialog", { name: "修改密码", exact: true });
  const passwordChangeRequired = await passwordDialog.waitFor({ state: "visible", timeout: 3_000 })
    .then(() => true).catch(() => false);
  if (passwordChangeRequired) {
    await passwordDialog.getByLabel("当前密码", { exact: true }).fill(initialPassword);
    await passwordDialog.getByLabel("新密码", { exact: true }).fill(randomPassword());
    await passwordDialog.getByRole("button", { name: "保存新密码", exact: true }).click();
    await page.getByRole("dialog", { name: "确认修改密码", exact: true })
      .getByRole("button", { name: "确认", exact: true }).click();
    await passwordDialog.waitFor({ state: "hidden" });
  }
  await page.getByRole("heading", { name: "设备资源", exact: true }).waitFor();
  const session = await page.evaluate(async () => ({
    status: (await fetch("/api/v1/auth/me", { credentials: "same-origin" })).status,
    token: sessionStorage.getItem("camera-log-record-token"),
  }));
  if (session.status !== 200 || session.token) throw new Error("管理员登录未建立纯 Cookie 会话");
}

async function selectFirstRecord(title) {
  const row = page.locator(".audit-workspace .el-table__row").first();
  await row.waitFor();
  await row.click();
  await page.getByRole("dialog", { name: title, exact: true }).waitFor();
  await page.getByText("原始脱敏 JSON", { exact: true }).click();
  await page.locator(".audit-event-drawer pre").waitFor();
}

async function createConflictRequest() {
  const result = await page.evaluate(async () => {
    const username = `audit-browser-${crypto.randomUUID().slice(0, 8)}`;
    const body = { username, displayName: "隔离验收账号", password: "AuditBrowser!234", isAdmin: false,
      scopes: ["tasks:read"], resourceIds: null, enabled: true };
    const headers = { "Content-Type": "application/json", "X-Requested-With": "XMLHttpRequest" };
    const first = await fetch("/api/v1/users", { method: "POST", credentials: "same-origin", headers, body: JSON.stringify(body) });
    const second = await fetch("/api/v1/users", { method: "POST", credentials: "same-origin", headers, body: JSON.stringify(body) });
    return { first: first.status, second: second.status };
  });
  if (result.first !== 201 || result.second !== 409)
    throw new Error(`隔离冲突请求返回 ${result.first}/${result.second}，期望 201/409`);
}

async function assertNoHorizontalOverflow(viewport) {
  const size = await page.evaluate(() => ({ width: document.documentElement.scrollWidth, viewport: window.innerWidth }));
  if (size.width > size.viewport) throw new Error(`${viewport} 页面出现横向溢出：${size.width}px > ${size.viewport}px`);
}

async function clearTooltipFocus() {
  await page.evaluate(() => {
    if (document.activeElement instanceof HTMLElement) document.activeElement.blur();
  });
  await page.waitForTimeout(250);
}

async function prepareScreenshot() {
  await clearTooltipFocus();
  // 截图仅检查静态布局，隐藏由折叠按钮持续占据焦点的说明浮层，避免遮挡首屏内容。
  await page.addStyleTag({ content: ".el-popper { display: none !important; }" });
}

async function verifyWorkspace() {
  await waitForApi("/api/v1/audit-events", async () => {
    await page.getByRole("tab", { name: "审计与事件", exact: true }).click();
  });
  await page.getByRole("heading", { name: "审计记录", exact: true }).waitFor();
  await page.getByRole("button", { name: "近 24 小时", exact: true }).click();
  await waitForApi("/api/v1/audit-events", async () => {
    await page.getByRole("button", { name: "查询", exact: true }).click();
  });
  await selectFirstRecord("审计记录详情");
  await page.keyboard.press("Escape");
  await page.getByRole("dialog", { name: "审计记录详情", exact: true }).waitFor({ state: "hidden" });

  await waitForApi("/api/v1/runtime-events", async () => {
    await page.getByRole("tab", { name: "运行事件", exact: true }).click();
  });
  await page.getByRole("heading", { name: "运行事件", exact: true }).waitFor();
  await page.getByText("采集连接中断", { exact: true }).waitFor();
  await page.getByText("隔离连接演示任务", { exact: true }).waitFor();
  await selectFirstRecord("运行事件详情");
  await prepareScreenshot();
  await page.screenshot({ path: `${screenshots}/audit-workspace-runtime-detail.png`, fullPage: true });
  await page.keyboard.press("Escape");
  await page.getByRole("dialog", { name: "运行事件详情", exact: true }).waitFor({ state: "hidden" });
  await prepareScreenshot();
  await page.screenshot({ path: `${screenshots}/audit-workspace-runtime-table.png`, fullPage: true });

  await createConflictRequest();
  await waitForApi("/api/v1/request-events", async () => {
    await page.getByRole("tab", { name: "请求记录", exact: true }).click();
  });
  await page.getByRole("heading", { name: "请求记录", exact: true }).waitFor();
  await page.getByLabel("按请求方法筛选", { exact: true }).fill("GET");
  await waitForApi("/api/v1/request-events", async () => {
    await page.getByRole("button", { name: "查询", exact: true }).click();
  });
  await selectFirstRecord("请求记录详情");
  await page.keyboard.press("Escape");
  await page.getByRole("dialog", { name: "请求记录详情", exact: true }).waitFor({ state: "hidden" });
}

try {
  await mkdir(screenshots, { recursive: true });
  await login();
  await verifyWorkspace();

  await page.setViewportSize({ width: 1440, height: 1000 });
  await assertNoHorizontalOverflow("桌面");
  await page.mouse.move(700, 700);
  await prepareScreenshot();
  await page.screenshot({ path: `${screenshots}/audit-workspace-desktop.png`, fullPage: true });
  await page.getByRole("button", { name: "折叠导航栏", exact: true }).click();
  await assertNoHorizontalOverflow("折叠导航栏");
  await page.mouse.move(700, 700);
  await prepareScreenshot();
  await page.screenshot({ path: `${screenshots}/audit-workspace-sidebar-collapsed.png`, fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await assertNoHorizontalOverflow("移动端");
  await page.mouse.move(300, 700);
  await prepareScreenshot();
  await page.screenshot({ path: `${screenshots}/audit-workspace-mobile.png`, fullPage: true });
  if (errors.length) throw new Error(errors.join("\n"));
} finally {
  await browser.close();
}
