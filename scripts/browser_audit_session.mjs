// 隔离浏览器验收：仅在临时 API、MongoDB 和日志目录中验证管理员 Cookie 会话与审计工作区。
import { mkdir } from "node:fs/promises";
import { randomBytes } from "node:crypto";

process.loadEnvFile(".env");

const baseUrl = process.env.BROWSER_BASE_URL?.replace(/\/$/, "");
const isolatedAuth = process.env.BROWSER_ISOLATED_AUTH;
const username = process.env.BROWSER_ADMIN_USERNAME;
const initialPassword = process.env.BROWSER_ADMIN_PASSWORD;
const screenshots = process.env.BROWSER_SCREENSHOTS || "output/playwright";

if (isolatedAuth !== "true")
  throw new Error("浏览器审计验收仅允许 BROWSER_ISOLATED_AUTH=true 的隔离环境");
if (!baseUrl)
  throw new Error("浏览器审计验收需要显式设置 BROWSER_BASE_URL，不能回退到开发服务");
if (!username || !initialPassword)
  throw new Error("浏览器审计验收需要隔离内置管理员用户名和初始密码");

let origin;
try {
  origin = new URL(baseUrl).origin;
} catch {
  throw new Error("BROWSER_BASE_URL 必须是有效的绝对 URL");
}

const playwrightModule = await import(process.env.PLAYWRIGHT_MODULE || "playwright");
const { chromium } = playwrightModule.default || playwrightModule;
const browser = await chromium.launch({ headless: true, channel: "chrome" });
const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
const page = await context.newPage();
page.setDefaultTimeout(10_000);

const errors = [];
let unauthenticatedMeChecks = 0;
page.on("pageerror", error => errors.push(`页面异常：${error.message}`));
page.on("response", response => {
  const url = new URL(response.url());
  if (url.origin === origin && url.pathname === "/api/v1/auth/me" && response.status() === 401)
    unauthenticatedMeChecks += 1;
});

function password() {
  return randomBytes(24).toString("base64url");
}

function isApiResponse(response, path) {
  const url = new URL(response.url());
  return url.origin === origin && url.pathname === path;
}

async function requireCookieOnly(response, description) {
  if (response.status() !== 200)
    throw new Error(`${description} 返回 HTTP ${response.status()}`);
  const headers = await response.request().allHeaders();
  if ("authorization" in headers)
    throw new Error(`${description} 不应发送 Authorization 请求头`);
}

async function waitForApi(path, action) {
  const received = page.waitForResponse(response => isApiResponse(response, path));
  await action();
  const response = await received;
  await requireCookieOnly(response, path);
  return response;
}

async function loginAndChangeInitialPassword() {
  await page.goto(baseUrl, { waitUntil: "networkidle" });
  if (unauthenticatedMeChecks !== 1)
    throw new Error(`首次未登录 auth/me 应恰有一次 401，实际为 ${unauthenticatedMeChecks} 次`);

  await page.getByLabel("用户名", { exact: true }).fill(username);
  await page.getByLabel("密码", { exact: true }).fill(initialPassword);
  await page.getByRole("button", { name: "登录", exact: true }).click();

  const passwordDialog = page.getByRole("dialog", { name: "修改密码", exact: true });
  await passwordDialog.waitFor();
  await passwordDialog.getByLabel("当前密码", { exact: true }).fill(initialPassword);
  await passwordDialog.getByLabel("新密码", { exact: true }).fill(password());
  await passwordDialog.getByRole("button", { name: "保存新密码", exact: true }).click();
  await page.getByRole("dialog", { name: "确认修改密码", exact: true })
    .getByRole("button", { name: "确认", exact: true }).click();
  await passwordDialog.waitFor({ state: "hidden" });
  await page.getByRole("heading", { name: "设备资源", exact: true }).waitFor();
}

async function assertAuthenticatedCookieSession() {
  const result = await page.evaluate(async () => {
    const response = await fetch("/api/v1/auth/me", { credentials: "same-origin" });
    return { status: response.status, token: sessionStorage.getItem("camera-log-record-token") };
  });
  if (result.status !== 200) throw new Error(`改密后 auth/me 返回 HTTP ${result.status}`);
  if (result.token) throw new Error("Cookie 会话下 sessionStorage 不应保存服务 Token");
}

async function verifyAuditWorkspace() {
  await waitForApi("/api/v1/audit-events", async () => {
    await page.getByRole("tab", { name: "审计与事件", exact: true }).click();
  });
  await page.getByRole("heading", { name: "审计记录", exact: true }).waitFor();

  // 初始改密产生本次隔离环境的审计记录，筛选后可验证表格和详情没有依赖预置设备数据。
  await page.getByLabel("按操作筛选", { exact: true }).fill("change_password");
  await waitForApi("/api/v1/audit-events", async () => {
    await page.getByRole("button", { name: "查询", exact: true }).click();
  });
  const row = page.locator(".audit-workspace .el-table__row").first();
  await row.waitFor();
  await row.click();
  const details = page.locator(".event-details");
  await details.waitFor();
  if (!(await details.textContent())?.includes("change_password"))
    throw new Error("审计记录详情未显示筛选结果");

  await waitForApi("/api/v1/runtime-events", async () => {
    await page.getByRole("tab", { name: "运行事件", exact: true }).click();
  });
  await page.getByRole("heading", { name: "运行事件", exact: true }).waitFor();
}

try {
  await mkdir(screenshots, { recursive: true });
  await loginAndChangeInitialPassword();
  await assertAuthenticatedCookieSession();
  await verifyAuditWorkspace();

  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.screenshot({ path: `${screenshots}/audit-session-desktop.png`, fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({ path: `${screenshots}/audit-session-mobile.png`, fullPage: true });

  if (errors.length) throw new Error(errors.join("\n"));
} finally {
  await browser.close();
}
