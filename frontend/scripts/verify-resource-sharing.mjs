// 资源共享生产浏览器验收：路由替身覆盖创建、编辑、撤销和共享用户的入口权限。
import assert from "node:assert/strict";
import { mkdir } from "node:fs/promises";
import { dashboardFixture } from "../../scripts/dashboard_fixture.mjs";

const imported = await import(process.env.PLAYWRIGHT_MODULE || "playwright");
const { chromium } = imported.default || imported;
const baseUrl = process.env.BASE_URL || "http://127.0.0.1:5173";
const screenshots = process.env.BROWSER_SCREENSHOTS || "output/playwright";
const browser = await chromium.launch({ headless: true, channel: "chrome" });
const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });

const shareTargets = [
  { id: "share-a", username: "share-a", displayName: "共享用户甲" },
  { id: "share-b", username: "share-b", displayName: "共享用户乙" },
];
const ownerResource = {
  id: "owner-resource", name: "所有者资源", kind: "SERIAL_SERVER", ip: "192.0.2.101",
  createdBy: "owner", createdByName: "资源所有者", sharedUserIds: ["share-a"], version: 3,
};
const sharedResource = {
  id: "shared-resource", name: "共享网络资源", kind: "HIKVISION_NETWORK", ip: "192.0.2.102",
  username: "device", authType: "DIGEST", healthStatus: "ONLINE", createdBy: "owner",
  createdByName: "资源所有者", sharedUserIds: ["share-user"], version: 4,
};
const writes = [];
let mode = "owner";
let navigationGeneration = 0;
let currentUser = {
  id: "owner", username: "owner", displayName: "资源所有者", isAdmin: false,
  scopes: ["tasks:read", "resources:create", "resources:write", "tasks:control", "logs:read", "commands:send"],
  enabled: true, mustChangePassword: false, hasActiveServiceToken: true,
};

async function json(route, body, status = 200) {
  await route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
}

function resources() {
  return mode === "owner" ? [ownerResource] : [sharedResource];
}

await context.route("**/api/v1/**", async route => {
  const request = route.request();
  const url = new URL(request.url());
  const { pathname: path } = url;
  const method = request.method();
  const payload = request.postData() ? request.postDataJSON() : {};
  if (method === "GET" && path === "/api/v1/auth/me") return json(route, { user: currentUser });
  if (method === "GET" && path === "/api/v1/dashboard") return json(route, dashboardFixture(undefined, 7, currentUser.isAdmin, currentUser.scopes.includes("logs:read")));
  if (method === "GET" && path === "/api/v1/resources") return json(route, { items: resources(), total: resources().length, page: 1, pageSize: 20 });
  if (method === "GET" && path === "/api/v1/users/share-targets") return json(route, { items: shareTargets, total: shareTargets.length, page: 1, pageSize: 100 });
  if (method === "GET" && path === `/api/v1/resources/${ownerResource.id}`) return json(route, ownerResource);
  if (method === "GET" && path === `/api/v1/resources/${sharedResource.id}`) return json(route, sharedResource);
  if (method === "POST" && path === "/api/v1/resources") {
    writes.push({ method, path, payload });
    return json(route, { ...payload, id: "created-resource", version: 1, createdBy: currentUser.id, sharedUserIds: payload.sharedUserIds ?? [] }, 201);
  }
  if (method === "PATCH" && path === `/api/v1/resources/${ownerResource.id}`) {
    writes.push({ method, path, payload });
    Object.assign(ownerResource, payload, { version: ownerResource.version + 1 });
    return json(route, ownerResource);
  }
  if (method === "POST" && path === `/api/v1/resources/${sharedResource.id}/authenticate`) return json(route, { model: "fixture", subSerialNumber: "fixture", softwareVersion: "fixture" });
  if (method === "GET" && ["/api/v1/tasks", "/api/v1/command-templates", "/api/v1/nodes", "/api/v1/users/creators"].includes(path))
    return json(route, { items: [], total: 0, page: 1, pageSize: 20 });
  if (method === "GET" && path === "/api/v1/api-reference") return json(route, { operations: [], generatedAt: "2026-10-26T00:00:00Z" });
  throw new Error(`未模拟接口：${method} ${path}`);
});

const page = await context.newPage();
page.setDefaultTimeout(12_000);
const errors = [];
page.on("pageerror", error => errors.push(error.message));
page.on("console", message => { if (message.type() === "error") errors.push(message.text()); });

async function reload(hash = "resources") {
  navigationGeneration += 1;
  await page.goto(`${baseUrl.replace(/\/$/, "")}/?fixture=${navigationGeneration}#${hash}`, { waitUntil: "networkidle" });
}

async function selectRecipients(ids) {
  const select = page.locator(".resource-share-recipients .el-select");
  for (const id of ids) {
    await select.click();
    const target = shareTargets.find(item => item.id === id);
    await page.getByText(`${target.displayName} · ${target.username}`, { exact: true }).last().click();
  }
}

async function selectSerialResourceKind() {
  await page.locator(".el-form-item").filter({ hasText: "资源类型" }).locator(".el-select").click();
  await page.getByText("串口服务器", { exact: true }).last().click();
}

try {
  await mkdir(screenshots, { recursive: true });
  await reload();
  await page.getByRole("heading", { name: "设备资源", exact: true }).waitFor();

  // 默认不共享的单台创建及一人、多人的选择都必须将显式 ID 发送给 API。
  await page.getByRole("button", { name: "新建资源", exact: true }).click();
  await page.getByRole("dialog", { name: "新建设备资源", exact: true }).waitFor();
  await page.getByLabel("资源名称", { exact: true }).fill("默认不共享资源");
  await page.getByLabel("IP 地址", { exact: true }).fill("192.0.2.110");
  await selectSerialResourceKind();
  await page.getByRole("button", { name: "保存资源", exact: true }).click();
  await page.getByText("资源已保存", { exact: true }).last().waitFor();
  assert.deepEqual(writes.at(-1).payload.sharedUserIds, [], "新资源默认必须显式保存为空共享名单");

  await page.getByRole("button", { name: "新建资源", exact: true }).click();
  await page.getByRole("dialog", { name: "新建设备资源", exact: true }).waitFor();
  await page.getByLabel("资源名称", { exact: true }).fill("多人共享资源");
  await page.getByLabel("IP 地址", { exact: true }).fill("192.0.2.111");
  await selectSerialResourceKind();
  await selectRecipients(["share-a", "share-b"]);
  await page.getByRole("button", { name: "保存资源", exact: true }).click();
  await page.getByText("资源已保存", { exact: true }).last().waitFor();
  assert.deepEqual(writes.at(-1).payload.sharedUserIds, ["share-a", "share-b"], "多人选择必须保留所有用户 ID");

  // 编辑加载已有名单，确认保存时保持；移除唯一标签后显式发送空数组撤销共享。
  const ownerRow = page.getByRole("row").filter({ hasText: ownerResource.name });
  await ownerRow.getByRole("button", { name: "编辑资源" }).click();
  await page.getByRole("dialog", { name: "编辑设备资源", exact: true }).waitFor();
  await page.getByRole("dialog", { name: "编辑设备资源", exact: true })
    .getByText("共享用户甲 · share-a", { exact: true }).waitFor();
  await page.getByRole("button", { name: "保存资源", exact: true }).click();
  await page.getByRole("dialog", { name: "确认编辑资源", exact: true }).getByRole("button", { name: "确认", exact: true }).click();
  await page.getByText("资源已保存", { exact: true }).last().waitFor();
  assert.deepEqual(writes.at(-1).payload.sharedUserIds, ["share-a"], "编辑省略操作时必须保留原共享名单");

  await ownerRow.getByRole("button", { name: "编辑资源" }).click();
  await page.getByRole("dialog", { name: "编辑设备资源", exact: true }).waitFor();
  await page.locator(".resource-share-recipients .el-tag__close").click();
  await page.getByRole("button", { name: "保存资源", exact: true }).click();
  await page.getByRole("dialog", { name: "确认编辑资源", exact: true }).getByRole("button", { name: "确认", exact: true }).click();
  await page.getByText("资源已保存", { exact: true }).last().waitFor();
  assert.deepEqual(writes.at(-1).payload.sharedUserIds, [], "移除所有共享用户必须显式撤销共享");

  // 共享用户可编辑和认证，但资源删除继续仅对所有者或管理员显示。
  mode = "shared";
  currentUser = { ...currentUser, id: "share-user", username: "share-user", displayName: "共享用户", isAdmin: false, hasActiveServiceToken: true };
  await reload();
  const sharedRow = page.getByRole("row").filter({ hasText: sharedResource.name });
  const sharedActions = sharedRow.locator(".resource-actions");
  await sharedActions.getByRole("button", { name: "编辑资源" }).waitFor();
  await sharedActions.getByRole("button", { name: "立即认证设备" }).waitFor();
  assert.equal(await sharedActions.getByRole("button", { name: "删除资源" }).count(), 0, "共享用户不得看到资源删除按钮");

  // 缺少资源写入 scope 时，即使被共享也不显示编辑或认证按钮。
  currentUser = { ...currentUser, scopes: ["tasks:read", "tasks:control", "logs:read"], hasActiveServiceToken: true };
  await reload();
  const scopeLimitedRow = page.getByRole("row").filter({ hasText: sharedResource.name });
  const scopeLimitedActions = scopeLimitedRow.locator(".resource-actions");
  assert.equal(await scopeLimitedActions.getByRole("button", { name: "编辑资源" }).count(), 0, "缺少 resources:write 时不得编辑共享资源");
  assert.equal(await scopeLimitedActions.getByRole("button", { name: "立即认证设备" }).count(), 0, "缺少 resources:write 时不得认证共享资源");

  // API 文档入口必须同时服从会话资格和哈希直达保护。
  currentUser = { ...currentUser, scopes: ["tasks:read"], hasActiveServiceToken: false };
  await reload("api-reference");
  assert.equal(await page.getByRole("tab", { name: "API 文档", exact: true }).count(), 0, "无有效服务账号不得显示 API 文档入口");
  assert.equal(page.url().endsWith("#dashboard"), true, "无资格直达 API 文档必须回退默认工作区");
  currentUser = { ...currentUser, hasActiveServiceToken: true };
  await reload("api-reference");
  await page.getByRole("tab", { name: "API 文档", exact: true }).waitFor();

  for (const width of [1440, 390]) {
    mode = "shared";
    currentUser = { ...currentUser, id: "share-user", scopes: ["tasks:read", "resources:write", "tasks:control", "logs:read"], hasActiveServiceToken: true };
    await page.setViewportSize({ width, height: 844 });
    await reload();
    await page.locator(".resource-table .el-scrollbar__wrap").evaluate(element => { element.scrollLeft = element.scrollWidth; });
    await sharedRow.getByRole("button", { name: "编辑资源" }).waitFor();
    const geometry = await page.evaluate(() => ({ scrollWidth: document.documentElement.scrollWidth, clientWidth: document.documentElement.clientWidth }));
    assert.ok(geometry.scrollWidth <= geometry.clientWidth + 1, `${width}px 资源共享工作区不得横向溢出：${JSON.stringify(geometry)}`);
    await page.screenshot({ path: `${screenshots}/resource-sharing-${width}.png`, fullPage: true, animations: "disabled" });
  }
  assert.deepEqual(errors, []);
  console.log(JSON.stringify({ passed: true, writes: writes.length, screenshots: `${screenshots}/resource-sharing-{1440,390}.png` }));
} finally {
  await context.close();
  await browser.close();
}
