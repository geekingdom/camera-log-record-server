// 资源批量删除与创建人范围验收：浏览器中的全部 API 都由本地路由模拟。
import assert from "node:assert/strict";
import { mkdir } from "node:fs/promises";

const imported = await import(process.env.PLAYWRIGHT_MODULE || "playwright");
const { chromium } = imported.default || imported;
const screenshots = process.env.BROWSER_SCREENSHOTS || "output/playwright";
const browser = await chromium.launch({ headless: true, channel: "chrome" });
const resources = [
  { id: "first", name: "资源甲", kind: "HIKVISION_NETWORK", ip: "192.0.2.101", version: 1, createdBy: "owner", createdByName: "创建者", createdAt: "2026-09-09T00:00:00.000Z", taskCount: 1, activeTaskCount: 1 },
  { id: "second", name: "资源乙", kind: "HIKVISION_NETWORK", ip: "192.0.2.102", version: 2, createdBy: "owner", createdByName: "创建者", createdAt: "2026-09-09T01:00:00.000Z", taskCount: 2, activeTaskCount: 1 },
  { id: "third", name: "资源丙", kind: "SERIAL_SERVER", ip: "192.0.2.103", version: 3, createdBy: "other", createdByName: "其他创建者", createdAt: "2026-09-09T02:00:00.000Z", taskCount: 0, activeTaskCount: 0 },
  { id: "foreign", name: "外部资源", kind: "SERIAL_SERVER", ip: "192.0.2.105", version: 5, createdBy: "other", createdByName: "其他创建者", createdAt: "2026-09-09T02:30:00.000Z", taskCount: 0, activeTaskCount: 0 },
  { id: "deleted", name: "历史资源", kind: "SERIAL_SERVER", ip: "192.0.2.104", version: 4, createdBy: "owner", createdByName: "创建者", createdAt: "2026-09-08T00:00:00.000Z", deletedAt: "2026-09-08T01:00:00.000Z", taskCount: 1, activeTaskCount: 0 },
];
const requests = { lists: [], deletes: [], creators: 0 };
let operatorMode = false;
let controlAllowed = true;
const pageOf = (items) => ({ items, total: items.length, page: 1, pageSize: 20 });
async function json(route, body, status = 200) {
  await route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
}
function actor() {
  return operatorMode
    ? { id: "owner", username: "owner", displayName: "创建者", isAdmin: false, enabled: true, mustChangePassword: false, version: 1, scopes: controlAllowed ? ["tasks:read", "resources:write", "tasks:control"] : ["tasks:read", "resources:write"] }
    : { id: "admin", username: "admin", displayName: "管理员", isAdmin: true, enabled: true, mustChangePassword: false, version: 1, scopes: ["*"] };
}
try {
  await mkdir(screenshots, { recursive: true });
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  await context.route("**/api/v1/**", async route => {
    const request = route.request(); const url = new URL(request.url()); const { pathname: path } = url; const method = request.method();
    if (method === "GET" && path === "/api/v1/auth/me") return json(route, { user: actor() });
    if (method === "GET" && path === "/api/v1/resources") {
      requests.lists.push(Object.fromEntries(url.searchParams));
      const creator = url.searchParams.get("createdBy");
      const includeDeleted = url.searchParams.get("includeDeleted") === "true";
      return json(route, pageOf(resources.filter(item => (includeDeleted || !item.deletedAt) && (!creator || item.createdBy === creator))));
    }
    if (method === "GET" && path === "/api/v1/users/creators") {
      requests.creators += 1;
      return json(route, pageOf([{ id: "owner", username: "owner", displayName: "创建者" }, { id: "other", username: "other", displayName: "其他创建者" }]));
    }
    if (method === "DELETE" && path.startsWith("/api/v1/resources/")) {
      const id = path.split("/").at(-1); requests.deletes.push(id);
      if (id === "second") return json(route, { error: { message: "资源版本已变化" } }, 409);
      const item = resources.find(resource => resource.id === id);
      Object.assign(item, { deletedAt: "2026-09-09T03:00:00.000Z" });
      return json(route, item, 202);
    }
    if (method === "GET" && ["/api/v1/tasks", "/api/v1/command-templates", "/api/v1/nodes"].includes(path)) return json(route, pageOf([]));
    throw new Error(`未模拟的 API 请求：${method} ${path}`);
  });
  const page = await context.newPage(); page.setDefaultTimeout(10_000);
  const errors = []; page.on("pageerror", error => errors.push(error.message)); page.on("console", message => {
    if (message.type() === "error" && !message.text().includes("status of 409")) errors.push(message.text());
  });
  const row = name => page.getByRole("row").filter({ hasText: name });
  const select = async name => row(name).locator(".el-checkbox").click();

  await page.goto(process.env.BASE_URL || "http://127.0.0.1:5173", { waitUntil: "networkidle" });
  await page.getByRole("heading", { name: "设备资源", exact: true }).waitFor();
  assert.equal(requests.lists.at(-1).createdBy, undefined, "管理员默认查看全部资源");
  assert.equal(await page.getByText("创建时间（北京时间）", { exact: true }).count(), 1);
  assert.equal(await page.getByText("删除时间（北京时间）", { exact: true }).count(), 1);
  await page.getByRole("combobox", { name: "按创建用户筛选" }).click();
  await page.getByText("创建者 · owner", { exact: true }).click();
  await page.waitForTimeout(100);
  assert.equal(requests.creators > 0, true, "创建用户目录必须经 mock 端点加载");
  assert.equal(requests.lists.at(-1).createdBy, "owner", "创建人筛选必须传给资源列表接口");
  await page.getByText("查看全部", { exact: true }).click();
  await page.waitForTimeout(100);
  assert.equal(requests.lists.at(-1).createdBy, "admin", "关闭查看全部后管理员也只查询本人资源");
  await page.getByText("查看全部", { exact: true }).click();
  await page.waitForTimeout(100);
  await page.getByText("包含已删除资源", { exact: true }).click();
  await row("历史资源").waitFor();

  await select("资源甲"); await select("资源乙"); await select("资源丙");
  await page.getByRole("button", { name: /删除已选 \(3\)/ }).click();
  await page.getByRole("dialog", { name: "确认批量删除设备资源" }).getByRole("button", { name: "取消", exact: true }).click();
  assert.deepEqual(requests.deletes, [], "取消确认不得发送 DELETE");
  await page.getByRole("button", { name: /删除已选 \(3\)/ }).click();
  await page.getByRole("dialog", { name: "确认批量删除设备资源" }).getByRole("button", { name: "确认", exact: true }).click();
  await page.getByText("删除失败：", { exact: false }).waitFor();
  assert.deepEqual(requests.deletes, ["first", "second", "third"], "失败项目后仍须按选择顺序继续提交");
  assert.match(await page.getByText("删除失败：", { exact: false }).textContent(), /资源乙.*资源版本已变化/);
  assert.equal(await row("资源乙").locator(".el-checkbox.is-checked").count(), 1, "失败资源应保留选择以便重试");
  assert.equal(await row("历史资源").locator("input[type=checkbox]").isDisabled(), true, "已删除资源不可批量选择");
  await page.screenshot({ path: `${screenshots}/resource-bulk-delete-1440.png`, fullPage: true, animations: "disabled" });
  await page.setViewportSize({ width: 390, height: 844 });
  const layout = await page.evaluate(() => ({ scrollWidth: document.documentElement.scrollWidth, clientWidth: document.documentElement.clientWidth }));
  assert.ok(layout.scrollWidth <= layout.clientWidth + 1, "移动端资源页不得横向溢出");
  await page.screenshot({ path: `${screenshots}/resource-bulk-delete-390.png`, fullPage: true, animations: "disabled" });

  operatorMode = true; controlAllowed = true; await page.reload({ waitUntil: "networkidle" });
  await page.getByRole("heading", { name: "设备资源", exact: true }).waitFor();
  assert.equal(requests.lists.at(-1).createdBy, "owner", "普通用户默认只查询本人资源");
  assert.equal(await page.getByRole("row").filter({ hasText: "外部资源" }).count(), 0, "普通用户默认范围不得显示他人资源");
  await page.getByText("查看全部", { exact: true }).click(); await page.waitForTimeout(100);
  assert.equal(requests.lists.at(-1).createdBy, undefined, "普通用户可切换至全部可见资源范围");
  assert.equal(await row("外部资源").locator("input[type=checkbox]").isDisabled(), true, "非创建者不得被批量选择");
  controlAllowed = false; await page.reload({ waitUntil: "networkidle" });
  await page.getByRole("heading", { name: "设备资源", exact: true }).waitFor();
  assert.equal(await row("资源乙").locator("input[type=checkbox]").isDisabled(), true, "无 tasks:control 权限不得批量选择");
  assert.deepEqual(errors, []);
  await context.close();
  console.log(JSON.stringify({ passed: true, deletes: requests.deletes.length, creatorRequests: requests.creators, screenshots: `${screenshots}/resource-bulk-delete-{1440,390}.png` }));
} finally {
  await browser.close();
}
