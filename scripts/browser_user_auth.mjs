// 用户会话浏览器验收：所有 API 都由路由替身响应，不访问真实设备或服务数据。
import assert from "node:assert/strict";
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
  scopes: ["tasks:read", "logs:read"],
  resourceIds: ["resource"],
  enabled: true,
  mustChangePassword: false,
};
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
  if (path === "/api/v1/tasks")
    return json(route, {
      items: [
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
      total: 1,
      page: 1,
      pageSize: 20,
    });
  if (path === "/api/v1/resources")
    return json(route, {
      items: [
        {
          id: "resource",
          name: "授权资源",
          kind: "SERIAL_SERVER",
          ip: "127.0.0.1",
          version: 1,
        },
      ],
      total: 1,
      page: 1,
      pageSize: 20,
    });
  if (path === "/api/v1/command-templates" || path === "/api/v1/nodes")
    return json(route, { items: [], total: 0, page: 1, pageSize: 100 });
  if (path === "/api/v1/users/permissions")
    return json(route, {
      scopes: [{ value: "tasks:read", label: "查看任务" }],
    });
  if (path === "/api/v1/users")
    return json(route, {
      items: [admin, reader],
      total: 2,
      page: 1,
      pageSize: 20,
    });
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
  await page.goto(baseUrl, { waitUntil: "networkidle" });
  await login("admin");
  await page.getByRole("heading", { name: "设备资源", exact: true }).waitFor();
  await page.getByRole("tab", { name: "账号管理", exact: true }).click();
  await page.getByRole("tab", { name: "用户账号", exact: true }).click();
  await page.getByText("全部权限", { exact: true }).waitFor();
  await page.getByText("启用", { exact: true }).first().waitFor();
  await waitForUserTags();
  assert.deepEqual(
    await page
      .locator(".user-manager .el-tag")
      .allTextContents(),
    ["全部权限", "启用", "查看任务", "logs:read", "启用"],
  );
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
  await page.getByRole("heading", { name: "设备资源", exact: true }).waitFor();
  await page.getByRole("button", { name: "退出", exact: true }).click();
  await login("reader");
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
