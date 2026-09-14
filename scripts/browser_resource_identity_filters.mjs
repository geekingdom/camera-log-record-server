// 资源身份筛选浏览器验收：全部 API 路由由本地 mock 响应，校验页面与查询参数同步。
import assert from "node:assert/strict";
import { mkdir } from "node:fs/promises";

const imported = await import(process.env.PLAYWRIGHT_MODULE || "playwright");
const { chromium } = imported.default || imported;
const screenshots = process.env.BROWSER_SCREENSHOTS || "output/playwright";
const baseUrl = process.env.BASE_URL || "http://127.0.0.1:5173";
const browser = await chromium.launch({ headless: true, channel: "chrome" });
const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
const requests = [];
const resources = Array.from({ length: 25 }, (_, index) => ({
  id: `resource-${index + 1}`,
  name: index === 20 ? "边缘入口" : `设备资源 ${index + 1}`,
  kind: index % 2 ? "SERIAL_SERVER" : "HIKVISION_NETWORK",
  ip: `192.0.2.${index + 1}`,
  model: index === 20 ? "DS-2CD2047G2" : index % 2 ? "串口网关" : "DS-2CD1023G0",
  subSerialNumber: index === 20 ? "SN-EDGE-2047" : `SN-${String(index + 1).padStart(3, "0")}`,
  createdBy: index === 20 ? "creator-b" : "creator-a",
  createdByName: index === 20 ? "乙创建者" : "甲创建者",
  deletedAt: index === 22 ? "2026-09-14T00:00:00Z" : null,
  version: 1,
}));

async function json(route, body, status = 200) {
  await route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
}

function filtered(url) {
  const search = url.searchParams.get("search") || "";
  const model = url.searchParams.get("model") || "";
  const serial = url.searchParams.get("subSerialNumber") || "";
  const kind = url.searchParams.get("kind") || "";
  const createdBy = url.searchParams.get("createdBy") || "";
  const includeDeleted = url.searchParams.get("includeDeleted") === "true";
  return resources.filter(resource =>
    (!search || `${resource.name} ${resource.ip}`.includes(search)) &&
    (!model || resource.model.includes(model)) &&
    (!serial || resource.subSerialNumber.includes(serial)) &&
    (!kind || resource.kind === kind) &&
    (!createdBy || resource.createdBy === createdBy) &&
    (includeDeleted || !resource.deletedAt),
  );
}

await context.route("**/api/v1/**", async route => {
  const request = route.request();
  const url = new URL(request.url());
  const { pathname } = url;
  if (request.method() === "GET" && pathname === "/api/v1/auth/me") return json(route, { user: {
    id: "administrator", username: "administrator", displayName: "模拟管理员", isAdmin: true,
    enabled: true, mustChangePassword: false,
    scopes: ["tasks:read", "resources:read", "resources:write", "resources:create", "tasks:control"],
  } });
  if (request.method() === "GET" && pathname === "/api/v1/resources") {
    requests.push(Object.fromEntries(url.searchParams));
    const page = Number(url.searchParams.get("page") || "1");
    const pageSize = Number(url.searchParams.get("pageSize") || "20");
    const items = filtered(url);
    return json(route, { items: items.slice((page - 1) * pageSize, page * pageSize), total: items.length, page, pageSize });
  }
  if (request.method() === "GET" && pathname === "/api/v1/users/creators") return json(route, {
    items: [
      { id: "creator-a", username: "creator-a", displayName: "甲创建者" },
      { id: "creator-b", username: "creator-b", displayName: "乙创建者" },
    ], total: 2, page: 1, pageSize: 20,
  });
  if (request.method() === "GET" && ["/api/v1/tasks", "/api/v1/command-templates", "/api/v1/nodes"].includes(pathname))
    return json(route, { items: [], total: 0, page: 1, pageSize: 20 });
  if (request.method() === "GET" && pathname === "/api/v1/platform-settings") return json(route, { retentionDays: 7, version: 1 });
  throw new Error(`未模拟接口：${request.method()} ${pathname}`);
});

const page = await context.newPage();
page.setDefaultTimeout(12_000);
const errors = [];
page.on("pageerror", error => errors.push(error.message));
page.on("console", message => { if (message.type() === "error") errors.push(message.text()); });

async function latestRequest(expected) {
  await page.waitForTimeout(100);
  assert.deepEqual(requests.at(-1), expected);
}

async function clickFilter(expected) {
  await page.getByRole("button", { name: "筛选", exact: true }).click();
  await latestRequest(expected);
}

async function assertToolbarGeometry(width, collapsed = false) {
  const collapseButton = page.getByRole("button", { name: collapsed ? "折叠导航栏" : "展开导航栏", exact: true });
  if (await collapseButton.count()) await collapseButton.click();
  await page.waitForTimeout(120);
  const controls = await page.locator(".resource-toolbar [aria-label], .resource-toolbar .el-checkbox, .resource-toolbar .el-button").evaluateAll(elements =>
    elements.map(element => {
      const rect = element.getBoundingClientRect();
      return {
        label: element.getAttribute("aria-label") || element.textContent?.trim() || element.className,
        left: rect.left, top: rect.top, right: rect.right, bottom: rect.bottom,
        clipped: element.scrollWidth > element.clientWidth + 1 || element.scrollHeight > element.clientHeight + 1,
      };
    }).filter(rect => rect.right > rect.left && rect.bottom > rect.top),
  );
  for (const control of controls)
    assert.equal(control.clipped, false, `${width}px${collapsed ? " 折叠侧栏" : ""}控件内部文字被裁剪：${control.label}`);
  for (let index = 0; index < controls.length; index += 1) {
    for (let other = index + 1; other < controls.length; other += 1) {
      const first = controls[index], second = controls[other];
      const overlaps = first.left < second.right && second.left < first.right && first.top < second.bottom && second.top < first.bottom;
      assert.equal(overlaps, false, `${width}px${collapsed ? " 折叠侧栏" : ""}工具栏控件重叠：${first.label} / ${second.label}`);
    }
  }
  const geometry = await page.evaluate(() => ({ scrollWidth: document.documentElement.scrollWidth, clientWidth: window.innerWidth }));
  assert.ok(geometry.scrollWidth <= geometry.clientWidth + 1, `${width}px 不得横向溢出：${JSON.stringify(geometry)}`);
}

try {
  await mkdir(screenshots, { recursive: true });
  await page.goto(baseUrl, { waitUntil: "networkidle" });
  await page.getByRole("heading", { name: "设备资源", exact: true }).waitFor();
  await latestRequest({ page: "1", pageSize: "20" });

  await page.getByLabel("搜索资源", { exact: true }).fill("边缘");
  await page.getByLabel("搜索资源", { exact: true }).press("Enter");
  await latestRequest({ page: "1", pageSize: "20", search: "边缘" });
  await page.getByRole("row").filter({ hasText: "边缘入口" }).waitFor();

  await page.getByLabel("设备型号", { exact: true }).fill("DS-2CD2047");
  await page.getByLabel("设备型号", { exact: true }).press("Enter");
  await latestRequest({ page: "1", pageSize: "20", search: "边缘", model: "DS-2CD2047" });
  await page.getByLabel("设备序列号", { exact: true }).fill("SN-EDGE");
  await clickFilter({ page: "1", pageSize: "20", search: "边缘", model: "DS-2CD2047", subSerialNumber: "SN-EDGE" });

  const modelInput = page.getByLabel("设备型号", { exact: true });
  const modelControl = modelInput.locator("xpath=ancestor::div[contains(@class, 'el-input')][1]");
  await modelControl.hover();
  await modelControl.locator(".el-input__clear").click();
  await latestRequest({ page: "1", pageSize: "20", search: "边缘", subSerialNumber: "SN-EDGE" });
  assert.equal(await modelInput.inputValue(), "", "清除型号必须保留其余筛选条件");

  const kindSelect = page.locator(".resource-toolbar .el-select").first();
  await kindSelect.click();
  await page.getByRole("option", { name: "海康网络设备", exact: true }).click();
  await latestRequest({ page: "1", pageSize: "20", search: "边缘", subSerialNumber: "SN-EDGE", kind: "HIKVISION_NETWORK" });
  await page.getByText("包含已删除资源", { exact: true }).click();
  await latestRequest({ page: "1", pageSize: "20", search: "边缘", subSerialNumber: "SN-EDGE", kind: "HIKVISION_NETWORK", includeDeleted: "true" });

  const creatorSelect = page.locator(".resource-toolbar .creator-filter .el-select");
  await creatorSelect.click();
  await page.getByRole("option", { name: /乙创建者 · creator-b/ }).click();
  await latestRequest({ page: "1", pageSize: "20", search: "边缘", subSerialNumber: "SN-EDGE", kind: "HIKVISION_NETWORK", includeDeleted: "true", createdBy: "creator-b" });
  await page.getByText("边缘入口", { exact: true }).waitFor();

  await page.getByLabel("搜索资源", { exact: true }).fill("");
  await page.getByLabel("设备序列号", { exact: true }).fill("");
  await page.getByLabel("设备型号", { exact: true }).fill("");
  await kindSelect.hover();
  await kindSelect.locator(".el-select__clear").click();
  await page.getByText("包含已删除资源", { exact: true }).click();
  await creatorSelect.hover();
  await creatorSelect.locator(".el-select__clear").click();
  await clickFilter({ page: "1", pageSize: "20" });

  await page.locator(".el-pagination .btn-next:not([disabled])").click();
  await latestRequest({ page: "2", pageSize: "20" });
  await page.locator(".resource-table .el-checkbox__input").nth(1).click();
  await page.locator(".resource-table .el-checkbox__input").nth(2).click();
  assert.equal(await page.locator(".resource-table .el-checkbox__input.is-checked").count(), 2, "第二页应保留多选状态");
  const batchDelete = page.getByRole("button", { name: /删除已选/ });
  assert.equal(await batchDelete.isDisabled(), false, "第二页选中资源后批量删除按钮必须可用");
  assert.equal(await page.locator(".bulk-delete-result").count(), 0, "未提交删除时不得展示过期批量结果");
  await page.getByLabel("设备型号", { exact: true }).fill("DS-2CD2047");
  await page.getByLabel("设备型号", { exact: true }).press("Enter");
  await latestRequest({ page: "1", pageSize: "20", model: "DS-2CD2047" });
  assert.equal(await page.locator(".resource-table .el-checkbox__input.is-checked").count(), 0, "筛选回第一页必须清空多选");
  assert.equal(await batchDelete.isDisabled(), true, "筛选清空多选后批量删除按钮必须禁用");
  assert.equal(await page.locator(".bulk-delete-result").count(), 0, "筛选后不得残留批量删除结果");

  for (const width of [1920, 1440, 1024, 390, 320]) {
    await page.setViewportSize({ width, height: 844 });
    await assertToolbarGeometry(width);
    await page.screenshot({ path: `${screenshots}/resource-identity-filters-${width}.png`, fullPage: true, animations: "disabled" });
  }
  for (const width of [1920, 1440, 1024]) {
    await page.setViewportSize({ width, height: 844 });
    await assertToolbarGeometry(width, true);
    await page.screenshot({ path: `${screenshots}/resource-identity-filters-${width}-sidebar-collapsed.png`, fullPage: true, animations: "disabled" });
  }
  assert.deepEqual(errors, []);
  console.log(JSON.stringify({ passed: true, requests: requests.length, screenshots: `${screenshots}/resource-identity-filters-{1920,1440,1024,390,320}.png` }));
} finally {
  await context.close();
  await browser.close();
}
