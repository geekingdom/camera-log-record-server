// 浏览器验收脚本：只验证显式指定的合成任务，不接触真实设备采集任务。
// Bootstrap 令牌仅用于创建和回收临时账号；页面及业务 API 全程使用 HttpOnly cookie 会话。
import { mkdir } from "node:fs/promises";
import { randomBytes } from "node:crypto";

process.loadEnvFile(".env");
const baseUrl = process.env.BROWSER_BASE_URL || "http://127.0.0.1:5173";
const bootstrapToken = process.env.BOOTSTRAP_TOKEN;
const smokeTaskId = process.env.BROWSER_SMOKE_TASK_ID;
const smokeMarker = /(?:协议压测|容器验收|浏览器验收|browser[-_ ]?smoke|synthetic|合成)/i;
const operatorScopes = [
  "tasks:read", "tasks:write", "resources:create", "resources:write",
  "tasks:create", "tasks:control", "logs:read", "logs:download",
  "commands:send", "templates:read", "templates:write",
];
if (!bootstrapToken)
  throw new Error("浏览器验收需要 BOOTSTRAP_TOKEN 来创建临时账号");
if (!smokeTaskId)
  throw new Error("浏览器验收需要显式设置 BROWSER_SMOKE_TASK_ID 为合成任务 ID");
const playwrightModule = await import(
  process.env.PLAYWRIGHT_MODULE || "playwright"
);
// 兼容 ESM 包名导入与通过 PLAYWRIGHT_MODULE 指向 CommonJS entry 的本地运行方式。
const { chromium } = playwrightModule.default || playwrightModule;
const browser = await chromium.launch({ headless: true, channel: "chrome" });
const context = await browser.newContext({
  viewport: { width: 1440, height: 1000 },
  acceptDownloads: true,
});

const page = await context.newPage();
// 让验收脚本在某个 UI 契约失配时快速失败，而不是用 Playwright 默认值长时间挂起。
page.setDefaultTimeout(8000);
const errors = [];
const screenshots = process.env.BROWSER_SCREENSHOTS || "output/playwright";
const name = `浏览器验收-${Date.now()}`;
let createdTemplateId;
let temporaryUser;
page.on("pageerror", (error) => errors.push(error.message));
page.on("console", (message) => {
  if (message.type() === "error") errors.push(message.text());
});

function password() {
  return randomBytes(24).toString("base64url");
}

function assertResponse(response, action) {
  if (!response.ok()) throw new Error(`${action} 失败：HTTP ${response.status()}`);
}

async function createTemporaryUser() {
  const suffix = randomBytes(8).toString("hex");
  const initialPassword = password();
  const response = await context.request.post(`${baseUrl}/api/v1/users`, {
    headers: {
      Authorization: `Bearer ${bootstrapToken}`,
      "X-Requested-With": "XMLHttpRequest",
    },
    data: {
      username: `browser-smoke-${suffix}`,
      displayName: "浏览器验收临时账号",
      password: initialPassword,
      scopes: operatorScopes,
      resourceIds: null,
    },
  });
  assertResponse(response, "创建浏览器验收临时账号");
  const user = await response.json();
  return { id: user.id, version: user.version, username: user.username, initialPassword };
}

async function loginTemporaryUser(user) {
  await page.goto(baseUrl, { waitUntil: "networkidle" });
  await page.getByLabel("用户名", { exact: true }).fill(user.username);
  await page.getByLabel("密码", { exact: true }).fill(user.initialPassword);
  await page.getByRole("button", { name: "登录", exact: true }).click();
  const passwordDialog = page.getByRole("dialog", { name: "修改密码", exact: true });
  await passwordDialog.waitFor();
  await passwordDialog.getByLabel("当前密码", { exact: true }).fill(user.initialPassword);
  await passwordDialog.getByLabel("新密码", { exact: true }).fill(password());
  await passwordDialog.getByRole("button", { name: "保存新密码", exact: true }).click();
  await page.getByRole("dialog", { name: "确认修改密码", exact: true })
    .getByRole("button", { name: "确认", exact: true }).click();
  await page.getByRole("heading", { name: "设备资源", exact: true }).waitFor();
  // 密码轮换会递增账号版本；finally 必须使用新版本才能软删除临时账号。
  user.version += 1;
}

async function explicitSyntheticTask() {
  const taskResponse = await context.request.get(
    `${baseUrl}/api/v1/tasks/${encodeURIComponent(smokeTaskId)}`,
  );
  assertResponse(taskResponse, "读取显式合成任务");
  const task = await taskResponse.json();
  const resourceResponse = await context.request.get(
    `${baseUrl}/api/v1/resources/${encodeURIComponent(task.resourceId)}`,
  );
  assertResponse(resourceResponse, "读取显式合成任务资源");
  const resource = await resourceResponse.json();
  if (task.status !== "COLLECTING" || !smokeMarker.test(`${task.name} ${resource.name}`))
    throw new Error("BROWSER_SMOKE_TASK_ID 必须指向名称含合成验收标识的采集中任务");
  const hoursResponse = await context.request.get(
    `${baseUrl}/api/v1/tasks/${encodeURIComponent(task.id)}/log-hours?page=1&pageSize=1`,
  );
  assertResponse(hoursResponse, "读取显式合成任务归档");
  if (!(await hoursResponse.json()).items?.length)
    throw new Error("显式合成任务没有可用于下载验收的小时归档");
  return task.id;
}

// 截图前直接检查抽屉和当前工作区标题的几何边界，防止 overflow:hidden 掩盖内容被推到屏外。
async function assertMobileDrawer(heading) {
  await page.waitForTimeout(250);
  const drawer = await page.locator(".el-drawer.open").boundingBox();
  const title = await page
    .getByRole("heading", { name: heading, exact: true })
    .boundingBox();
  const viewport = page.viewportSize();
  if (
    !drawer ||
    !title ||
    !viewport ||
    drawer.x < 0 ||
    drawer.width > viewport.width ||
    title.x < 0 ||
    title.x + title.width > viewport.width
  ) {
    throw new Error(
      `mobile drawer clipped: drawer=${JSON.stringify(drawer)}, heading=${JSON.stringify(title)}, viewport=${JSON.stringify(viewport)}`,
    );
  }
}

try {
  await mkdir(screenshots, { recursive: true });
  temporaryUser = await createTemporaryUser();
  await loginTemporaryUser(temporaryUser);
  await page.screenshot({ path: `${screenshots}/resources-desktop.png`, fullPage: true });
  // 临时操作员不具备管理员权限；管理员菜单由独立的 browser_user_auth.mjs 验收。
  for (const tab of ["服务节点", "服务账号", "审计与事件", "后台配置"])
    if (await page.getByRole("tab", { name: tab, exact: true }).count())
      throw new Error(`临时操作员不应看到管理员菜单：${tab}`);
  await page.getByRole("tab", { name: "采集任务", exact: true }).click();
  // 历史资源的任务仍可查询日志，但不允许编辑；第一页不保证存在可编辑任务。
  await page.locator(".task-filters").waitFor();
  await page.screenshot({
    path: `${screenshots}/tasks-desktop.png`,
    fullPage: true,
  });

  await page.getByRole("tab", { name: "命令模板", exact: true }).click();
  await page.getByRole("button", { name: "新建模板", exact: true }).click();
  await page.getByLabel("模板名称", { exact: true }).fill(name);
  await page.getByLabel("新增初始化命令", { exact: true }).fill("prtHardInfo");
  await page.getByLabel("新增初始化命令", { exact: true }).press("Enter");
  await page.getByLabel("初始化命令 1", { exact: true }).waitFor();
  const templateCreated = page.waitForResponse(response => new URL(response.url()).pathname === "/api/v1/command-templates" && response.request().method() === "POST");
  await page.getByRole("button", { name: "保存模板", exact: true }).click();
  await page.getByRole("dialog", { name: "确认保存模板", exact: true }).getByRole("button", { name: "确认", exact: true }).click();
  createdTemplateId = (await (await templateCreated).json()).id;
  await page
    .getByRole("row")
    .filter({ has: page.getByRole("cell", { name, exact: true }) })
    .waitFor();

  await page.getByRole("tab", { name: "采集任务", exact: true }).click();
  await page.getByRole("button", { name: "新建任务", exact: true }).click();
  await page.getByLabel("任务名称", { exact: true }).fill(name);
  await page
    .locator(".el-form-item")
    .filter({ hasText: "连接协议" })
    .locator(".el-select")
    .click();
  await page.getByRole("option", { name: "Telnet 串口", exact: true }).click();
  await page.getByLabel("串口服务器 IP", { exact: true }).fill("127.0.0.1");
  await page.getByLabel("端口", { exact: true }).fill("65530");
  await page.locator(".section-heading .el-select").click();
  await page.getByRole("option", { name, exact: true }).click();
  await page.getByLabel("初始化命令 1", { exact: true }).waitFor();
  await page.screenshot({
    path: `${screenshots}/task-form-desktop.png`,
    fullPage: true,
  });
  // 仅验证模板快照已进入任务草稿；关闭抽屉避免烟测长期留下无法删除的任务数据。
  await page.getByRole("button", { name: "关闭", exact: true }).click();
  await page.getByRole("button", { name: "刷新列表", exact: true }).click();

  // 仅使用调用者明确指定、名称带合成验收标识的任务，绝不扫描或误用真实采集任务。
  const taskId = await explicitSyntheticTask();
  const existing = page
    .locator(".el-table__body tbody tr")
    .filter({ hasText: taskId })
    .first();
  await existing.waitFor({ timeout: 20000 });
  await existing.getByRole("button", { name: "编辑任务" }).click();
  await page.getByRole("tab", { name: "实时打印", exact: true }).click();
  // 真实日志验收必须等到渲染行和时间前缀，不能只等待 WebSocket 连接或固定延时。
  await page
    .locator(".log-line")
    .filter({ hasText: /\d{4}[-/]\d{1,2}[-/]\d{1,2}/ })
    .first()
    .waitFor({ timeout: 15000 });
  // 以下按钮仅操作浏览器本地日志视图，不调用任务暂停/继续生命周期接口。
  await page.getByRole("button", { name: "暂停视图" }).click();
  await page.getByRole("button", { name: "继续视图" }).waitFor();
  await page.getByRole("button", { name: "清空本地视图" }).click();
  await page.locator(".log-line").waitFor({ state: "detached" });
  await page.getByRole("button", { name: "继续视图" }).click();
  await page
    .locator(".log-line")
    .filter({ hasText: /\d{4}[-/]\d{1,2}[-/]\d{1,2}/ })
    .first()
    .waitFor({ timeout: 15000 });
  await page.getByRole("button", { name: "跟随最新日志" }).click();
  await page.screenshot({
    path: `${screenshots}/live-desktop.png`,
    fullPage: true,
  });
  await page.getByRole("tab", { name: "小时归档", exact: true }).click();
  const archive = page.locator(".el-drawer .el-table").first();
  await archive.locator(".el-table__row").first().waitFor();
  await archive
    .locator(".el-table__header-wrapper .el-checkbox")
    .first()
    .check();
  // 下载必须走浏览器事件确认，不以“点击成功”替代后端作业完成与文件响应校验。
  const downloadPromise = page.waitForEvent("download", { timeout: 70000 });
  await page.getByRole("button", { name: "下载选中小时", exact: true }).click();
  const download = await downloadPromise;
  await download.saveAs(`${screenshots}/${download.suggestedFilename()}`);
  await page.setViewportSize({ width: 390, height: 844 });
  await assertMobileDrawer("小时归档");
  await page.screenshot({
    path: `${screenshots}/archives-mobile.png`,
    fullPage: true,
  });
  await page.getByRole("tab", { name: "任务配置", exact: true }).click();
  await assertMobileDrawer("基本连接");
  await page.screenshot({
    path: `${screenshots}/task-form-mobile.png`,
    fullPage: true,
  });

  // 独立工作台必须能从任务名称进入，不依赖编辑抽屉中的隐藏页签。
  await page.getByRole("button", { name: "关闭", exact: true }).click();
  await page.setViewportSize({ width: 1440, height: 1000 });
  await existing.locator(".task-name").click();
  await page.locator(".log-line").first().waitFor({ timeout: 15000 });
  await page.screenshot({ path: `${screenshots}/logs-workspace-desktop.png`, fullPage: true });
  await page.getByRole("tab", { name: "小时归档与检索", exact: true }).click();
  await page.locator(".archive-table .el-table__expand-icon").first().click();
  await page.getByRole("button", { name: "查看内容", exact: true }).first().click();
  const content = page.getByRole("dialog", { name: "日志片段内容" });
  await page.waitForFunction(() => {
    const text = document.querySelector(".file-viewer-content")?.textContent?.trim();
    return Boolean(text && text !== "此范围没有内容");
  });
  await content.getByRole("button", { name: "关闭", exact: true }).click();
  await page.getByPlaceholder("关键词", { exact: true }).fill(`acceptance-no-match-${Date.now()}`);
  const completedSearch = page.waitForResponse(async response => {
    if (!/\/log-searches\/[^/]+$/.test(new URL(response.url()).pathname) || response.request().method() !== "GET") return false;
    return (await response.json()).status === "SUCCEEDED";
  }, { timeout: 60000 });
  await page.getByRole("button", { name: "检索", exact: true }).click();
  await completedSearch;
  await page.screenshot({ path: `${screenshots}/archives-workspace-desktop.png`, fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({ path: `${screenshots}/archives-workspace-mobile.png`, fullPage: true });

  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth > window.innerWidth,
  );
  if (overflow || errors.length)
    throw new Error(
      `browser acceptance failed: overflow=${overflow}, consoleErrors=${errors.length}`,
    );
  console.log(JSON.stringify({ passed: true, screenshots, consoleErrors: 0 }));
} finally {
  try {
    if (createdTemplateId) {
      const response = await context.request.delete(
        `${baseUrl}/api/v1/command-templates/${createdTemplateId}?version=1`,
        { headers: { "X-Requested-With": "XMLHttpRequest" } },
      );
      assertResponse(response, "删除浏览器验收模板");
    }
    if (temporaryUser) {
      const response = await context.request.delete(
        `${baseUrl}/api/v1/users/${encodeURIComponent(temporaryUser.id)}?version=${temporaryUser.version}`,
        {
          headers: {
            Authorization: `Bearer ${bootstrapToken}`,
            "X-Requested-With": "XMLHttpRequest",
          },
        },
      );
      assertResponse(response, "删除浏览器验收临时账号");
    }
  } finally { await browser.close(); }
}
