// 用户会话浏览器验收：所有 API 都由路由替身响应，不访问真实设备或服务数据。
import assert from "node:assert/strict";
import { dashboardFixture } from "./dashboard_fixture.mjs";
import { mkdir } from "node:fs/promises";

const baseUrl = process.env.BROWSER_BASE_URL || "http://127.0.0.1:5173";
const output = process.env.BROWSER_SCREENSHOTS || "output/playwright";
const playwrightModule = await import(
  process.env.PLAYWRIGHT_MODULE || "playwright"
);
const { chromium } = playwrightModule.default || playwrightModule;
const browser = await chromium.launch({ headless: true, channel: "chrome" });
const context = await browser.newContext({
  viewport: { width: 1440, height: 1000 },
});
let current = null;
const admin = {
  id: "admin",
  username: "admin",
  displayName: "系统管理员",
  isAdmin: true,
  role: "SUPER_ADMIN",
  effectiveRole: "SUPER_ADMIN",
  scopes: ["*"],
  resourceIds: null,
  enabled: true,
  mustChangePassword: false,
  builtin: true,
  version: 1,
};
const force = {
  id: "force",
  username: "force",
  displayName: "需改密用户",
  isAdmin: false,
  role: "USER",
  effectiveRole: "USER",
  scopes: ["tasks:read"],
  resourceIds: ["resource"],
  enabled: true,
  mustChangePassword: true,
};
const reader = {
  id: "reader",
  username: "reader",
  displayName: "只读用户",
  isAdmin: false,
  role: "ADMIN",
  effectiveRole: "USER",
  permissionsExpiresOn: "2026-10-13",
  permissionsExpired: true,
  scopes: ["tasks:read", "logs:read"],
  resourceIds: ["resource"],
  enabled: true,
  mustChangePassword: false,
};
const expiredResources = [
  { id: "reader-resource", name: "到期本人资源", kind: "SERIAL_SERVER", ip: "192.0.2.61", version: 1,
    createdBy: "reader", createdByName: "只读用户", sharedUserIds: [] },
  { id: "shared-resource", name: "到期共享资源", kind: "SERIAL_SERVER", ip: "192.0.2.62", version: 1,
    createdBy: "owner", createdByName: "资源所有者", sharedUserIds: ["reader"] },
];
const expiredTasks = [
  { id: "reader-task", name: "到期本人任务", ip: "192.0.2.61", port: 10002, version: 1,
    status: "STOPPED", desiredState: "STOPPED", protocol: "TELNET_SERIAL", resourceId: "reader-resource",
    createdBy: "reader", createdByName: "只读用户", resourceSharedUserIds: [] },
  { id: "shared-task", name: "到期共享任务", ip: "192.0.2.62", port: 10002, version: 1,
    status: "STOPPED", desiredState: "STOPPED", protocol: "TELNET_SERIAL", resourceId: "shared-resource",
    createdBy: "owner", createdByName: "资源所有者", resourceSharedUserIds: ["reader"] },
];
const expiredMutations = [];
let ipPolicy = { version: 1, enabled: false, clientIp: "127.0.0.1", rules: [] };

async function json(route, body, status = 200, headers = {}) {
  await route.fulfill({
    status,
    contentType: "application/json",
    headers,
    body: JSON.stringify(body),
  });
}
await context.route("**/api/v1/**", async (route) => {
  const request = route.request(),
    url = new URL(request.url()),
    path = url.pathname,
    method = request.method(),
    payload = request.postDataJSON?.() ?? {};
  if (method !== "GET")
    assert.equal(
      request.headers()["x-requested-with"],
      "XMLHttpRequest",
      `${method} ${path} 缺少 CSRF 请求头`,
    );
  if (path === "/api/v1/auth/login" && method === "POST") {
    current =
      payload.username === "force"
        ? force
        : payload.username === "reader"
          ? reader
          : admin;
    return json(route, { user: current });
  }
  if (path === "/api/v1/auth/me")
    return current
      ? json(route, { user: current })
      : json(route, { detail: "登录会话已失效" }, 401, {
          "X-Auth-Required": "true",
        });
  if (path === "/api/v1/auth/password") {
    current = { ...current, mustChangePassword: false };
    return json(route, { user: current });
  }
  if (path === "/api/v1/auth/logout") {
    current = null;
    return route.fulfill({ status: 204 });
  }
  if (path === "/api/v1/dashboard")
    return json(route, dashboardFixture(undefined, 7, Boolean(current?.isAdmin), current?.scopes.includes("*") || current?.scopes.includes("logs:read")));
  if (path === "/api/v1/tasks")
    return json(route, {
      items: current?.id === "reader" ? expiredTasks : [
        {
          id: "task",
          name: "只读任务",
          ip: "127.0.0.1",
          port: 23,
          status: "STOPPED",
          desiredState: "STOPPED",
          protocol: "TELNET_SERIAL",
          resourceId: "resource",
        },
      ],
      total: current?.id === "reader" ? expiredTasks.length : 1,
      page: 1,
      pageSize: 20,
    });
  if (path === "/api/v1/resources")
    return json(route, {
      items: current?.id === "reader" ? expiredResources : [
        {
          id: "resource",
          name: "授权资源",
          kind: "SERIAL_SERVER",
          ip: "127.0.0.1",
          version: 1,
        },
      ],
      total: current?.id === "reader" ? expiredResources.length : 1,
      page: 1,
      pageSize: 20,
    });
  if (path.startsWith("/api/v1/resources/") && method === "GET") {
    const resource = expiredResources.find(item => item.id === path.split("/").pop());
    return json(route, resource || { detail: "not found" }, resource ? 200 : 404);
  }
  if (path === "/api/v1/resources/reader-resource" && method === "PATCH") {
    expiredMutations.push({ method, path, payload });
    Object.assign(expiredResources[0], payload, { version: 2 });
    return json(route, expiredResources[0]);
  }
  if (path === "/api/v1/tasks/reader-task" && method === "GET")
    return json(route, expiredTasks[0]);
  if (path === "/api/v1/command-templates" || path === "/api/v1/nodes")
    return json(route, { items: [], total: 0, page: 1, pageSize: 100 });
  if (path === "/api/v1/users/permissions")
    return json(route, {
      scopes: [
        { value: "tasks:read", label: "查看设备与任务", fixed: true },
        { value: "logs:read", label: "查看日志与 Coredump", fixed: true },
        { value: "logs:download", label: "下载日志与 Coredump", fixed: true },
        { value: "templates:read", label: "查看命令模板", fixed: true },
        { value: "templates:write", label: "创建命令模板", fixed: true },
        { value: "service-tokens:read", label: "查看我的服务账号", fixed: true },
        { value: "tasks:write", label: "编辑任务", fixed: false },
        { value: "commands:send", label: "发送命令", fixed: true },
      ],
    });
  if (path === "/api/v1/users/share-targets")
    return json(route, {
      items: [{ id: "reader", username: "reader", displayName: "只读用户" }],
      total: 1,
      page: 1,
      pageSize: 100,
    });
  if (path === "/api/v1/users")
    return json(route, {
      items: [admin, reader],
      total: 2,
      page: 1,
      pageSize: 20,
    });
  if (path === "/api/v1/users/creators") return json(route, { items: [], total: 0, page: 1, pageSize: 100 });
  if (path === "/api/v1/admin/ip-policy" && method === "GET")
    return json(route, ipPolicy);
  if (path === "/api/v1/admin/ip-policy" && method === "PATCH") {
    ipPolicy = {
      ...payload,
      version: ipPolicy.version + 1,
      clientIp: ipPolicy.clientIp,
    };
    return json(route, ipPolicy);
  }
  if (path === "/api/v1/service-tokens")
    return json(route, { items: [], total: 0, page: 1, pageSize: 20 });
  throw new Error(`未模拟 API：${method} ${path}`);
});
const page = await context.newPage();
page.setDefaultTimeout(10_000);
const errors = [];
page.on("pageerror", (error) => errors.push(error.message));
async function login(username) {
  await page.getByLabel("用户名", { exact: true }).fill(username);
  await page.getByLabel("密码", { exact: true }).fill("asdf!234");
  await page.getByRole("button", { name: "登录", exact: true }).click();
}

// Element Plus 标签有短暂的缩放淡入动画；截图必须等到最终尺寸，避免把过渡帧误判为空白数据。
async function waitForUserTags() {
  await page.waitForFunction(() => {
    const tags = [...document.querySelectorAll(".user-manager .el-tag")];
    return (
      tags.length > 0 &&
      tags.every(tag => {
        const style = getComputedStyle(tag);
        const rect = tag.getBoundingClientRect();
        return Number(style.opacity) >= 0.99 && rect.width > 20 && rect.height >= 20;
      })
    );
  });
}
try {
  await mkdir(output, { recursive: true });
  await page.goto(`${baseUrl.replace(/\/$/, "")}/#resources`, { waitUntil: "networkidle" });
  await login("admin");
  await page.getByRole("tab", { name: "设备资源", exact: true }).click();
  await page.getByRole("heading", { name: "设备资源", exact: true }).waitFor();
  await page.getByRole("tab", { name: "账号管理", exact: true }).click();
  await page.getByRole("tab", { name: "用户账号", exact: true }).click();
  await page.getByText("全部权限", { exact: true }).waitFor();
  await page.getByText("超级管理员", { exact: true }).waitFor();
  await page.getByText("管理员", { exact: true }).waitFor();
  await page.getByText("当前：普通用户", { exact: true }).waitFor();
  await page.getByText("已到期", { exact: true }).waitFor();
  await waitForUserTags();
  for (const label of ["查看设备与任务", "查看日志与 Coredump", "下载日志与 Coredump", "查看命令模板", "创建命令模板", "查看我的服务账号", "发送命令"])
    assert.equal(await page.getByText(label, { exact: true }).count() > 0, true, `到期账号应保留固定基础权限：${label}`);
  assert.equal(await page.getByRole("button", { name: "编辑用户" }).nth(1).isDisabled(), false, "超级管理员应能管理非内建管理员");
  await page.getByRole("button", { name: "新建用户", exact: true }).click();
  const userDialog = page.getByRole("dialog", { name: "新建用户", exact: true });
  await userDialog.getByText("普通用户", { exact: true }).waitFor();
  await userDialog.getByText("长期", { exact: true }).waitFor();
  await userDialog.getByText("指定截止日", { exact: true }).click();
  await userDialog.getByPlaceholder("选择截止日期").waitFor();
  await userDialog.getByRole("button", { name: "取消", exact: true }).click();
  await page.getByRole("button", { name: "新建用户", exact: true }).waitFor();
  await page.screenshot({
    path: `${output}/user-auth-admin-desktop.png`,
    fullPage: true,
  });
  await page.getByRole("tab", { name: "IP 访问控制", exact: true }).click();
  await page.getByRole("button", { name: "新增规则", exact: true }).click();
  await page.getByLabel("规则名称").fill("本机管理");
  await page.getByLabel("客户端 IP / 网段").fill("127.0.0.1/32");
  await page.getByRole("button", { name: "保存策略", exact: true }).click();
  const saveConfirmation = page.getByRole("dialog", {
    name: "确认保存",
    exact: true,
  });
  await saveConfirmation
    .getByRole("button", { name: "确认", exact: true })
    .click();
  await saveConfirmation.waitFor({ state: "hidden" });
  await page.getByText("IP 访问控制已保存", { exact: true }).waitFor();
  assert.equal(ipPolicy.rules[0].network, "127.0.0.1/32");
  await page.screenshot({
    path: `${output}/user-auth-ip-policy-desktop.png`,
    fullPage: true,
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({
    path: `${output}/user-auth-ip-policy-mobile.png`,
    fullPage: true,
  });
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.getByRole("button", { name: "退出", exact: true }).click();
  await login("force");
  const passwordDialog = page.getByRole("dialog", { name: "修改密码" });
  await passwordDialog.getByLabel("当前密码", { exact: true }).fill("asdf!234");
  await passwordDialog
    .getByLabel("新密码", { exact: true })
    .fill("new-password-123");
  await passwordDialog
    .getByRole("button", { name: "保存新密码", exact: true })
    .click();
  await page
    .getByRole("dialog", { name: "确认修改密码", exact: true })
    .getByRole("button", { name: "确认", exact: true })
    .click();
  await page.getByRole("tab", { name: "设备资源", exact: true }).click();
  await page.getByRole("heading", { name: "设备资源", exact: true }).waitFor();
  await page.getByRole("button", { name: "退出", exact: true }).click();
  await login("reader");
  await page.getByRole("tab", { name: "设备资源", exact: true }).click();
  await page.getByRole("heading", { name: "设备资源", exact: true }).waitFor();
  assert.equal(
    await page.getByRole("tab", { name: "账号管理", exact: true }).count(),
    0,
  );
  assert.equal(
    await page.getByRole("tab", { name: "审计与事件", exact: true }).count(),
    0,
  );
  assert.equal(
    await page.getByRole("button", { name: "新建资源", exact: true }).count(),
    0,
  );
  const ownResourceRow = page.getByRole("row").filter({ hasText: "到期本人资源" });
  const sharedResourceRow = page.getByRole("row").filter({ hasText: "到期共享资源" });
  await ownResourceRow.getByRole("button", { name: "编辑资源", exact: true }).click();
  await page.getByRole("dialog", { name: "编辑设备资源", exact: true }).waitFor();
  await page.getByLabel("资源名称", { exact: true }).fill("到期本人资源已编辑");
  await page.getByRole("button", { name: "保存资源", exact: true }).click();
  await page.getByRole("dialog", { name: "确认编辑资源", exact: true }).getByRole("button", { name: "确认", exact: true }).click();
  await page.getByText("资源已保存", { exact: true }).waitFor();
  assert.equal(expiredMutations.length, 1, "到期本人资源应可保存");
  assert.equal(await sharedResourceRow.getByRole("button", { name: "编辑资源", exact: true }).count(), 0);
  assert.equal(await sharedResourceRow.getByRole("button", { name: "删除资源", exact: true }).count(), 0);
  await page.getByRole("tab", { name: "采集任务", exact: true }).click();
  const ownTaskRow = page.getByRole("row").filter({ hasText: "到期本人任务" });
  const sharedTaskRow = page.getByRole("row").filter({ hasText: "到期共享任务" });
  await ownTaskRow.getByRole("button", { name: "编辑任务", exact: true }).waitFor();
  await ownTaskRow.getByRole("button", { name: "删除采集任务", exact: true }).waitFor();
  for (const name of ["编辑任务", "删除采集任务", "启动任务", "停止任务"])
    assert.equal(await sharedTaskRow.getByRole("button", { name, exact: true }).count(), 0, `到期共享任务不应显示${name}`);
  await page.screenshot({
    path: `${output}/user-auth-reader-desktop.png`,
    fullPage: true,
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({
    path: `${output}/user-auth-reader-mobile.png`,
    fullPage: true,
  });
  assert.deepEqual(errors, []);
  console.log(JSON.stringify({ passed: true, screenshots: output }));
} finally {
  await browser.close();
}
