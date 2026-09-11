// 归档检索阅读器验收：全部使用 mock API，不创建任务、不访问真实日志或设备。
import assert from "node:assert/strict";
import { mkdir } from "node:fs/promises";
import { resolve } from "node:path";
const imported = await import(process.env.PLAYWRIGHT_MODULE || "playwright");
const { chromium } = imported.default || imported;

const task = { id: "archive-task", name: "归档模拟任务", protocol: "SSH", resourceId: "resource", ip: "192.0.2.8", port: 22,
  status: "COLLECTING", desiredState: "RUNNING", initialCommands: [], scheduledCommands: [] };
const file = { id: "archive-file", status: "READY", bytes: 700000, rawFileName: "fixture.log" };
const lateFile = { ...file, id: "archive-file-late", rawFileName: "late.log" };
const keyword = "中文命中";
const prefix = "无命中前置正文 ".repeat(1000);
const matchedLine = `\u001b[31m前置 ${keyword} 完整匹配行\u001b[0m\n`;
const matchOffset = Buffer.byteLength(prefix);
const raw = prefix + matchedLine + "后续正文 ".repeat(50000);
const rawBytes = Buffer.from(raw);
const browser = await chromium.launch({ headless: true, channel: "chrome" });
const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, timezoneId: "America/New_York" });
await context.addInitScript(() => {
  sessionStorage.setItem("camera-log-record-token", "archive-browser-token");
  const RealDate = Date, fixed = Date.parse("2026-09-10T20:00:00.000Z");
  // 美国时区仍必须以上海日期 2026-09-11 初始化归档目录。
  // @ts-ignore 浏览器侧测试覆盖 Date 构造器的无参路径。
  window.Date = class extends RealDate { constructor(...args) { super(...(args.length ? args : [fixed])); } static now() { return fixed; } };
});
const requests = [];
const contentRequests = [];
let delayMetadata = false;
let searchRequest;

async function assertViewerLayout(page, dialog, viewport, screenshotName) {
  await page.setViewportSize(viewport);
  await dialog.waitFor({ state: "visible" });
  const content = dialog.locator(".file-viewer-content");
  await content.waitFor();
  const layout = await content.evaluate(element => {
    const root = document.documentElement;
    const before = element.scrollTop;
    element.scrollTop = element.scrollHeight;
    return {
      documentWidth: root.scrollWidth,
      viewportWidth: window.innerWidth,
      contentWidth: element.scrollWidth,
      contentViewportWidth: element.clientWidth,
      scrollable: element.scrollHeight > element.clientHeight,
      moved: element.scrollTop > before,
    };
  });
  assert.ok(layout.documentWidth <= layout.viewportWidth, `${screenshotName} 页面不得产生横向溢出`);
  assert.ok(layout.contentWidth <= layout.contentViewportWidth, `${screenshotName} 日志窗口不得产生横向溢出`);
  assert.ok(layout.scrollable && layout.moved, `${screenshotName} 日志窗口必须可纵向滚动`);
  const dialogBox = await dialog.boundingBox();
  assert.ok(dialogBox && dialogBox.y >= 0 && dialogBox.y + dialogBox.height <= viewport.height,
    `${screenshotName} 对话框必须完整位于视口内：${JSON.stringify({ viewport, dialogBox, layout })}`);
  const closeBox = await dialog.getByRole("button", { name: "关闭", exact: true }).boundingBox();
  assert.ok(closeBox && closeBox.y >= 0 && closeBox.y + closeBox.height <= viewport.height,
    `${screenshotName} 页末关闭按钮必须可达：${JSON.stringify({ viewport, dialogBox, closeBox, layout })}`);
  const surface = dialog.locator(".file-viewer-dialog");
  const dialogStyle = await surface.evaluate(element => {
    const style = getComputedStyle(element);
    return { className: element.className, maxHeight: style.maxHeight, marginTop: style.marginTop, marginBottom: style.marginBottom };
  });
  assert.equal(dialogStyle.marginTop, "32px", `${screenshotName} 弹窗顶部余量必须实际生效：${JSON.stringify(dialogStyle)}`);
  assert.equal(dialogStyle.marginBottom, "32px", `${screenshotName} 弹窗底部余量必须实际生效：${JSON.stringify(dialogStyle)}`);
  assert.ok(dialogStyle.maxHeight.includes("- 112px") || dialogStyle.maxHeight === `${viewport.height - 112}px`,
    `${screenshotName} 弹窗最大高度必须保留关闭按钮余量：${JSON.stringify(dialogStyle)}`);
  // 截图保留匹配定位，滚动断言不能让截图停在页末。
  await content.evaluate(element => { element.scrollTop = 0; });
  await content.locator(".log-search-active").scrollIntoViewIfNeeded();
  await page.screenshot({ path: resolve("output/playwright", screenshotName), fullPage: false });
}

async function json(route, body) { await route.fulfill({ contentType: "application/json", body: JSON.stringify(body) }); }
await context.route("**/api/v1/**", async route => {
  const request = route.request();
  const url = new URL(request.url());
  const path = url.pathname;
  requests.push(url);
  if (path === "/api/v1/auth/me") return json(route, { user: { id: "admin", username: "admin", scopes: ["*"], isAdmin: true } });
  if (path === "/api/v1/tasks") return json(route, { items: [task], total: 1, page: 1, pageSize: 100 });
  if (path === "/api/v1/resources") return json(route, { items: [{ id: "resource", name: "模拟设备", kind: "HIKVISION_NETWORK", ip: task.ip }], total: 1, page: 1, pageSize: 20 });
  if (path === `/api/v1/tasks/${task.id}`) return json(route, task);
  if (path === `/api/v1/tasks/${task.id}/log-hours`) return json(route, { items: [{ hourId: "hour", hour: "2026-09-11T00:00:00Z", status: "READY", integrity: "VERIFIED", bytes: file.bytes, archiveBytes: 2, files: [file] }], total: 1, page: 1, pageSize: 24 });
  if (path === "/api/v1/log-searches" && request.method() === "POST") { searchRequest = request.postDataJSON(); return json(route, { id: "search" }); }
  if (path === "/api/v1/log-searches/search") return json(route, { id: "search", status: "SUCCEEDED", progress: 100 });
  if (path === "/api/v1/log-searches/search/results") return json(route, { items: [{ fileId: file.id, offset: matchOffset, text: `\u001b[31m前置 ${keyword} 完整匹配行\u001b[0m` }, { fileId: lateFile.id, offset: 12, text: "迟到元数据匹配行" }], total: 2, page: 1, pageSize: 100 });
  if (path === `/api/v1/log-files/${lateFile.id}`) { await new Promise(resolve => setTimeout(resolve, 400)); return json(route, lateFile); }
  if (path === `/api/v1/log-files/${file.id}/content`) {
    const offset = Number(url.searchParams.get("offset") || 0), limit = Number(url.searchParams.get("limit") || 65536);
    contentRequests.push(offset);
    const source = rawBytes.subarray(offset, offset + limit);
    return json(route, { fileId: file.id, data: source.toString("base64"), nextOffset: offset + source.length });
  }
  if (["/api/v1/command-templates", "/api/v1/nodes", "/api/v1/admin/nodes", "/api/v1/service-tokens", "/api/v1/audit-events", "/api/v1/runtime-events"].includes(path)) return json(route, { items: [], total: 0, page: 1, pageSize: 100 });
  if (path === "/api/v1/platform-settings") return json(route, { retentionDays: 7, version: 1 });
  throw new Error(`未模拟请求：${request.method()} ${path}`);
});
await context.routeWebSocket("**/api/v1/tasks/*/logs*", socket => socket.close());
const page = await context.newPage();
page.setDefaultTimeout(10_000);
try {
  await page.goto(process.env.BASE_URL || "http://127.0.0.1:5173", { waitUntil: "networkidle" });
  await page.getByRole("tab", { name: "日志工作台", exact: true }).click();
  await page.getByRole("button", { name: "选择任务", exact: true }).click();
  const picker = page.getByRole("dialog", { name: "选择日志任务" });
  await picker.getByText(task.name, { exact: true }).click();
  await picker.getByRole("button", { name: "确认切换", exact: true }).click();
  await page.getByRole("tab", { name: "小时归档与检索", exact: true }).click();
  const date = page.getByLabel("归档日期（上海时区）");
  await page.locator(".archive-table .el-table__row").first().waitFor();
  assert.equal(await date.inputValue(), "2026-09-11", "默认日期必须是上海当天");
  assert.ok(requests.some(url => url.pathname.endsWith("/log-hours") && url.searchParams.get("date") === "2026-09-11"));
  await date.fill("2026-09-11");
  await date.press("Enter");
  await page.locator(".archive-table .el-table__row").first().waitFor();
  assert.ok(requests.some(url => url.pathname.endsWith("/log-hours") && url.searchParams.get("date") === "2026-09-11"));
  await page.getByPlaceholder("关键词", { exact: true }).fill(keyword);
  await page.getByRole("button", { name: "检索", exact: true }).click();
  const result = page.getByText(`前置 ${keyword} 完整匹配行`, { exact: true });
  await result.waitFor();
  assert.deepEqual(searchRequest, { taskId: task.id, keyword, start: "2026-09-10T16:00:00.000Z", end: "2026-09-11T16:00:00.000Z" });
  assert.equal(await result.evaluate(element => element.textContent), `前置 ${keyword} 完整匹配行`);
  await page.getByRole("button", { name: "查看具体信息", exact: true }).first().click();
  const dialog = page.getByRole("dialog", { name: "日志片段内容" });
  await dialog.getByText(keyword, { exact: false }).waitFor();
  assert.equal(await dialog.locator(".file-viewer-content").evaluate(element => /\u001b|\^\[\[/.test(element.textContent || "")), false);
  // 命中所在的非整步段先回退到文件开头，再向后恢复到原始锚点。
  await dialog.getByRole("button", { name: "上一段", exact: true }).click();
  await page.waitForTimeout(80);
  await dialog.getByRole("button", { name: "下一段", exact: true }).click();
  await page.waitForTimeout(80);
  const initialRequest = Math.max(0, matchOffset - 8 * 1024 - 128);
  assert.deepEqual(contentRequests.slice(0, 3), [initialRequest, 0, initialRequest], "前后翻段必须恢复命中原始锚点");
  await dialog.getByRole("button", { name: "段内查找", exact: true }).click({ force: true });
  await page.waitForTimeout(100);
  await page.locator(".log-find-bar input").fill(keyword);
  await dialog.locator(".log-search-active").waitFor();
  await mkdir(resolve("output/playwright"), { recursive: true });
  await assertViewerLayout(page, dialog, { width: 1440, height: 900 }, "archive-viewer-content-1440.png");
  await assertViewerLayout(page, dialog, { width: 390, height: 844 }, "archive-viewer-content-390.png");
  await dialog.getByRole("button", { name: "关闭", exact: true }).click();
  delayMetadata = true;
  await page.getByRole("button", { name: "查看具体信息", exact: true }).nth(1).click();
  await date.fill("2026-09-12");
  await date.press("Enter");
  await page.waitForTimeout(450);
  assert.equal(await page.getByRole("dialog", { name: "日志片段内容" }).count(), 0, "迟到文件元数据不得重新打开旧查看器");
  console.log(JSON.stringify({ passed: true, archiveDates: requests.filter(url => url.pathname.endsWith("/log-hours")).map(url => url.searchParams.get("date")) }));
} finally { await context.close(); await browser.close(); }
