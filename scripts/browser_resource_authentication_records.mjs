// 认证记录浏览器验收：所有 API 均由浏览器路由模拟，不连接真实设备或读取凭据。
import assert from "node:assert/strict";
import { mkdir } from "node:fs/promises";

const imported = await import(process.env.PLAYWRIGHT_MODULE || "playwright");
const { chromium } = imported.default || imported;
const baseUrl = process.env.BASE_URL || process.env.BROWSER_BASE_URL || "http://127.0.0.1:5173";
const screenshots = process.env.BROWSER_SCREENSHOTS || "output/playwright";
const resourceItems = [
  { id: "camera", name: "模拟海康", kind: "HIKVISION_NETWORK", ip: "192.0.2.10", healthStatus: "ONLINE" },
  { id: "serial", name: "模拟串口", kind: "SERIAL_SERVER", ip: "192.0.2.20" },
];
const requests = [];
let failNextPage = false;
const records = [
  {
    id: "record-change",
    createdAt: "2026-09-10T00:20:00.000Z",
    source: "HEALTH_CHECK",
    result: "ERROR",
    modelBefore: "DS-2CD2143G2",
    modelAfter: "DS-2CD2143G2-I",
    serialBefore: "OLD-001",
    serialAfter: "NEW-001",
    identityChanged: true,
    initialAuthentication: false,
    message: "设备返回异常状态",
  },
  {
    id: "record-initial",
    createdAt: "2026-09-09T00:20:00.000Z",
    source: "CREATE",
    result: "SUCCESS",
    modelAfter: "DS-2CD2143G2",
    serialAfter: "FIRST-001",
    identityChanged: false,
    initialAuthentication: true,
  },
];
const pageOf = (items, page = 1) => ({ items, total: 30, page, pageSize: 20 });

async function assertViewport(page, width) {
  const geometry = await page.evaluate(() => ({ scrollWidth: document.documentElement.scrollWidth, clientWidth: document.documentElement.clientWidth }));
  assert.ok(geometry.scrollWidth <= geometry.clientWidth + 1, `${width}px 出现横向溢出：${JSON.stringify(geometry)}`);
}

async function assertTableScrollsInsideDrawer(drawer, width) {
  const geometry = await drawer.locator(".authentication-table .el-scrollbar__wrap").evaluate(element => ({
    clientWidth: element.clientWidth,
    scrollWidth: element.scrollWidth,
  }));
  assert.ok(geometry.scrollWidth > geometry.clientWidth,
    `${width}px 认证表格列未在抽屉内形成横向滚动区域：${JSON.stringify(geometry)}`);
}

const browser = await chromium.launch({ headless: true, channel: "chrome" });
try {
  await mkdir(screenshots, { recursive: true });
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  await context.addInitScript(() => sessionStorage.setItem("camera-log-record-token", "authentication-history"));
  await context.route("**/api/v1/**", async route => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    const json = body => route.fulfill({ contentType: "application/json", body: JSON.stringify(body) });
    if (path === "/api/v1/auth/me") return json({ user: { id: "admin", username: "admin", displayName: "验收管理员", isAdmin: true, enabled: true, mustChangePassword: false, scopes: ["*"] } });
    if (path === "/api/v1/resources") return json(pageOf(resourceItems));
    if (path === "/api/v1/resources/camera/authentication-records") {
      requests.push(Object.fromEntries(url.searchParams));
      const cursor = url.searchParams.get("cursor");
      assert.notEqual(cursor, null, "首屏也必须显式传递空游标，避免全量计数");
      assert.ok(!url.searchParams.has("page") || url.searchParams.get("page") === "1");
      if (cursor && failNextPage) {
        failNextPage = false;
        return route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ error: { message: "模拟分页失败" } }) });
      }
      assert.ok(cursor === "" || cursor === "next-page", "翻页必须使用服务端返回的游标");
      return json({ items: cursor === "" ? records : [{ ...records[0], id: "record-page-2", message: "第二页认证记录" }],
        total: null, page: 1, pageSize: 20, hasMore: cursor === "", nextCursor: cursor === "" ? "next-page" : null });
    }
    if (path === "/api/v1/resources/camera/coredump-monitor") return json({ active: false, ownerTask: null, mountStatus: null });
    if (["/api/v1/tasks", "/api/v1/command-templates", "/api/v1/users/creators", "/api/v1/nodes"].includes(path)) return json(pageOf([]));
    throw new Error(`未模拟接口：${request.method()} ${path}`);
  });

  const page = await context.newPage();
  page.setDefaultTimeout(10000);
  const errors = [];
  page.on("pageerror", error => errors.push(error.message));
  await page.goto(baseUrl, { waitUntil: "networkidle" });
  const cameraRow = page.getByRole("row").filter({ hasText: "模拟海康" });
  const serialRow = page.getByRole("row").filter({ hasText: "模拟串口" });
  await cameraRow.getByRole("button", { name: "查看认证记录" }).waitFor();
  assert.equal(await serialRow.getByRole("button", { name: "查看认证记录" }).count(), 0, "串口资源不应显示认证记录入口");
  await cameraRow.getByRole("button", { name: "查看认证记录" }).click();
  const drawer = page.getByRole("dialog", { name: "模拟海康 · 认证记录", exact: true });
  await drawer.getByText("初始认证", { exact: true }).waitFor();
  await drawer.getByText("设备身份变更", { exact: true }).waitFor();
  await drawer.getByText("型号：DS-2CD2143G2 → DS-2CD2143G2-I", { exact: true }).waitFor();
  await drawer.getByText("序列号：OLD-001 → NEW-001", { exact: true }).waitFor();
  assert.equal(requests.at(-1).cursor, "");
  assert.equal(await drawer.getByRole("button", { name: "上一页", exact: true }).isDisabled(), true);

  await drawer.locator(".el-select").nth(0).click();
  await page.getByRole("option", { name: "认证异常", exact: true }).click();
  await drawer.locator(".el-select").nth(1).click();
  await page.getByRole("option", { name: "已变更", exact: true }).click();
  await drawer.getByRole("button", { name: "筛选", exact: true }).click();
  await page.waitForTimeout(100);
  const filtered = requests.at(-1);
  assert.equal(filtered.result, "ERROR");
  assert.equal(filtered.identityChanged, "true");

  const dateInputs = drawer.locator(".el-date-editor input");
  await dateInputs.nth(0).fill("2026-09-09 08:00:00");
  await dateInputs.nth(1).fill("2026-09-10 08:00:00");
  await dateInputs.nth(1).press("Tab");
  await drawer.getByRole("button", { name: "筛选", exact: true }).click();
  await page.waitForTimeout(100);
  const ranged = requests.at(-1);
  assert.match(ranged.start || "", /^\d{4}-\d{2}-\d{2}T.*Z$/, "start 必须发送 UTC ISO 时间");
  assert.match(ranged.end || "", /^\d{4}-\d{2}-\d{2}T.*Z$/, "end 必须发送 UTC ISO 时间");

  failNextPage = true;
  await drawer.getByRole("button", { name: "下一页", exact: true }).click();
  await page.getByText("模拟分页失败", { exact: true }).waitFor();
  await drawer.getByText("初始认证", { exact: true }).waitFor();
  assert.equal(await drawer.getByRole("button", { name: "上一页", exact: true }).isDisabled(), true);
  await drawer.getByRole("button", { name: "下一页", exact: true }).click();
  await drawer.getByText("第二页认证记录", { exact: true }).waitFor();
  assert.equal(requests.at(-1).cursor, "next-page");
  assert.equal(await drawer.getByRole("button", { name: "下一页", exact: true }).isDisabled(), true);
  await drawer.getByRole("button", { name: "上一页", exact: true }).click();
  await drawer.getByText("初始认证", { exact: true }).waitFor();
  assert.equal(requests.at(-1).cursor, "");
  await drawer.getByRole("button", { name: "下一页", exact: true }).click();
  await drawer.getByText("第二页认证记录", { exact: true }).waitFor();
  await drawer.getByRole("button", { name: "刷新认证记录", exact: true }).click();
  await drawer.getByText("初始认证", { exact: true }).waitFor();
  assert.equal(requests.at(-1).cursor, "", "刷新必须从最新首屏重新建立游标");
  await page.getByText("模拟分页失败", { exact: true }).waitFor({ state: "hidden" });
  await drawer.locator(".el-drawer__title").click();
  await page.mouse.move(0, 0);
  await page.waitForTimeout(400);
  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: 844 });
    await assertViewport(page, width);
    await assertTableScrollsInsideDrawer(drawer, width);
    await page.screenshot({ path: `${screenshots}/resource-authentication-records-${width}.png` });
  }
  assert.deepEqual(errors, []);
  await context.close();
  console.log("资源认证记录浏览器验收通过，已检查空游标首屏、筛选、失败重试、前后翻页、刷新与 1440px/390px 截图");
} finally {
  await browser.close();
}
