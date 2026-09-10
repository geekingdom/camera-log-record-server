// Coredump 浏览器验收使用本地 API 路由模拟，不访问真实设备、NFS 或运行中 Worker。
import { mkdir } from "node:fs/promises";
// 与其他浏览器验收脚本一致，允许通过 PLAYWRIGHT_MODULE 注入本机的 Playwright 安装。
const playwrightModule = await import(process.env.PLAYWRIGHT_MODULE || "playwright");
const { chromium } = playwrightModule.default || playwrightModule;

const baseUrl = process.env.BROWSER_BASE_URL || "http://127.0.0.1:5173";
const screenshots = process.env.BROWSER_SCREENSHOTS || "output/playwright";
const resources = [
  { id: "camera-a", name: "验收网络设备 A", kind: "HIKVISION_NETWORK", ip: "192.0.2.10", version: 1 },
  { id: "camera-b", name: "验收网络设备 B", kind: "HIKVISION_NETWORK", ip: "192.0.2.11", version: 1 },
];
const files = {
  "camera-a": [
    { id: "core-frozen", resourceId: "camera-a", nodeId: "node-a", name: "core-frozen.bin", size: 8192,
      receivedAt: "2026-09-10T01:00:00+00:00", status: "FROZEN", version: 1 },
    { id: "core-receiving", resourceId: "camera-a", nodeId: "node-a", name: "core-receiving.bin", size: 4096,
      receivedAt: "2026-09-10T00:00:00+00:00", status: "RECEIVING", version: 1 },
  ],
  "camera-b": [{ id: "core-b", resourceId: "camera-b", nodeId: "node-b", name: "core-b.bin", size: 1024,
    receivedAt: "2026-09-10T02:00:00+00:00", status: "FROZEN", version: 1 }],
};
let exportNumber = 0;
const calls = { list: [], cancel: [], ticket: [], content: [] };
const cancelledExports = new Set();
let singleTicketStarted, releaseSingleTicket, exportThreeStarted, releaseExportThree;
const singleTicketPending = new Promise(resolve => { singleTicketStarted = resolve; });
const singleTicketRelease = new Promise(resolve => { releaseSingleTicket = resolve; });
const exportThreePending = new Promise(resolve => { exportThreeStarted = resolve; });
const exportThreeRelease = new Promise(resolve => { releaseExportThree = resolve; });
const browser = await chromium.launch({ headless: true, channel: "chrome" });
const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, acceptDownloads: true });
const page = await context.newPage();
page.setDefaultTimeout(8000);
const downloads = [];
page.on("download", download => downloads.push(download.url()));

function json(route, value, status = 200, headers = {}) {
  return route.fulfill({ status, contentType: "application/json", headers, body: JSON.stringify(value) });
}
await page.route("**/api/v1/**", async route => {
  const request = route.request(), url = new URL(request.url()), { pathname, searchParams } = url;
  if (pathname === "/api/v1/auth/me") return json(route, { user: { id: "reviewer", username: "reviewer", displayName: "验收用户", isAdmin: false,
    enabled: true, mustChangePassword: false, scopes: ["tasks:read", "resources:write", "resources:create", "logs:read", "logs:download"] } });
  if (pathname === "/api/v1/resources") return json(route, { items: resources, total: resources.length, page: 1, pageSize: 20 });
  if (pathname.startsWith("/api/v1/resources/") && pathname.endsWith("/coredumps")) {
    const resourceId = decodeURIComponent(pathname.split("/")[4]);
    calls.list.push({ resourceId, name: searchParams.get("name"), receivedFrom: searchParams.get("receivedFrom") });
    return json(route, { items: files[resourceId] ?? [], total: (files[resourceId] ?? []).length, page: Number(searchParams.get("page")), pageSize: Number(searchParams.get("pageSize")) });
  }
  if (pathname === "/api/v1/coredump-exports" && request.method() === "POST") {
    exportNumber += 1;
    return json(route, { id: `export-${exportNumber}`, status: "QUEUED", kind: "COREDUMP_EXPORT" }, 202);
  }
  if (pathname.startsWith("/api/v1/coredump-exports/") && pathname.endsWith("/browser-session")) {
    const id = pathname.split("/")[4]; calls.ticket.push(id);
    return json(route, { url: `/api/v1/coredump-exports/${id}/content` }, 200, { "set-cookie": "download_access=mock; HttpOnly; Path=/api/v1/coredump-exports/" });
  }
  if (pathname.startsWith("/api/v1/coredumps/") && pathname.endsWith("/browser-session")) {
    const id = pathname.split("/")[4]; calls.ticket.push(id);
    singleTicketStarted(); await singleTicketRelease;
    return json(route, { url: `/api/v1/coredumps/${id}/content` });
  }
  if (pathname.includes("/content")) {
    calls.content.push(pathname);
    return route.fulfill({ status: 200, headers: { "content-type": "application/octet-stream", "content-disposition": "attachment; filename=core.bin" }, body: "fixture" });
  }
  if (pathname.startsWith("/api/v1/coredump-exports/") && request.method() === "DELETE") {
    const id = pathname.split("/")[4];
    calls.cancel.push(id); cancelledExports.add(id); return route.fulfill({ status: 204 });
  }
  if (pathname.startsWith("/api/v1/coredump-exports/")) {
    const id = pathname.split("/")[4];
    if (cancelledExports.has(id)) return json(route, { id, status: "CANCELLED" });
    if (id === "export-1") return json(route, { id, status: "RUNNING" });
    if (id === "export-3") {
      exportThreeStarted(); await exportThreeRelease;
      return json(route, { id, status: "SUCCEEDED", filename: "late.zip", bytes: 1 });
    }
    return json(route, { id, status: "SUCCEEDED", filename: "coredumps.zip", bytes: 8192 });
  }
  return json(route, {});
});

await mkdir(screenshots, { recursive: true });
await page.goto(baseUrl, { waitUntil: "networkidle" });
await page.getByRole("tab", { name: "设备资源", exact: true }).click();
await page.getByLabel("查看 coredump 文件").first().click();
const drawer = page.getByRole("dialog", { name: /验收网络设备 A.*Coredump 文件/ });
await drawer.waitFor();
await drawer.getByPlaceholder("按文件名筛选").fill("frozen");
await drawer.getByRole("button", { name: "筛选", exact: true }).click();
if (!calls.list.some(call => call.resourceId === "camera-a" && call.name === "frozen")) throw new Error("coredump 文件名筛选未发出正确请求");
await page.screenshot({ path: `${screenshots}/coredump-desktop.png`, fullPage: true });
const frozenRow = drawer.getByRole("row").filter({ hasText: "core-frozen.bin" });
await frozenRow.locator(".el-checkbox").first().click();
await drawer.getByRole("button", { name: "导出所选", exact: true }).click();
await page.getByRole("dialog", { name: "确认导出 coredump", exact: true }).getByRole("button", { name: "确认", exact: true }).click();
await drawer.getByRole("button", { name: "取消导出", exact: true }).click();
await page.getByRole("dialog", { name: "确认取消导出", exact: true }).getByRole("button", { name: "确认", exact: true }).click();
if (!calls.cancel.includes("export-1")) throw new Error("未请求取消 coredump 导出");
await page.waitForTimeout(1100);
if (await page.getByText("导出状态：已取消", { exact: true }).count()) throw new Error("取消导出被错误地显示为失败提示");
const nativeDownload = page.waitForEvent("download");
await drawer.getByRole("button", { name: "导出所选", exact: true }).click();
await page.getByRole("dialog", { name: "确认导出 coredump", exact: true }).getByRole("button", { name: "确认", exact: true }).click();
const downloaded = await nativeDownload;
if (!calls.ticket.includes("export-2")) throw new Error("成功导出未请求浏览器下载票据");
if (!downloaded.url().includes("/api/v1/coredump-exports/export-2/content")) throw new Error("成功导出未导航到浏览器下载票据地址");
await page.getByRole("dialog", { name: "确认导出 coredump", exact: true }).waitFor({ state: "hidden" });
await drawer.getByText("导出完成", { exact: false }).waitFor();
await page.waitForTimeout(1000);
await page.setViewportSize({ width: 390, height: 844 });
await page.screenshot({ path: `${screenshots}/coredump-mobile.png`, fullPage: true });
// 单文件票据和轮询中的旧导出在关闭并切换资源后返回，均不能触发下载。
const downloadsBeforeSwitch = downloads.length;
const singleClick = frozenRow.getByLabel("下载 coredump 文件").click();
await singleTicketPending;
await drawer.getByRole("button", { name: "导出所选", exact: true }).click();
await page.getByRole("dialog", { name: "确认导出 coredump", exact: true }).getByRole("button", { name: "确认", exact: true }).click();
await exportThreePending;
await drawer.getByRole("button", { name: "关闭", exact: true }).click();
await page.getByLabel("查看 coredump 文件").nth(1).click();
await page.getByRole("dialog", { name: /验收网络设备 B.*Coredump 文件/ }).waitFor();
releaseSingleTicket(); releaseExportThree();
await singleClick;
await page.waitForTimeout(500);
if (calls.ticket.includes("export-3")) throw new Error("切换资源后旧导出仍触发下载票据");
if (downloads.length !== downloadsBeforeSwitch) throw new Error(`切换资源后旧单文件或导出仍触发原生下载：${downloads.join(", ")}`);
if (!calls.list.some(call => call.resourceId === "camera-b")) throw new Error("切换资源未读取新 coredump 目录");
await browser.close();
console.log(`coredump UI 验收通过，截图：${screenshots}/coredump-desktop.png 与 ${screenshots}/coredump-mobile.png`);
