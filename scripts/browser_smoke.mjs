// 浏览器验收脚本：验证模板、任务、实时日志、归档和真实下载链路。
// 凭据仅注入 sessionStorage；脚本输出、截图文件名和控制台均不包含令牌。
import { mkdir } from "node:fs/promises";

process.loadEnvFile(".env");
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
await context.addInitScript(
  (token) => sessionStorage.setItem("camera-log-record-token", token),
  process.env.BOOTSTRAP_TOKEN,
);

const page = await context.newPage();
// 让验收脚本在某个 UI 契约失配时快速失败，而不是用 Playwright 默认值长时间挂起。
page.setDefaultTimeout(8000);
const errors = [];
const screenshots = "output/playwright";
const name = `浏览器验收-${Date.now()}`;
let createdTemplateId;
page.on("pageerror", (error) => errors.push(error.message));
page.on("console", (message) => {
  if (message.type() === "error") errors.push(message.text());
});

// 精确匹配表格单元格，避免 toast、下拉选项和表格中的同名文本触发 strict locator 错误。
function taskRow(taskName) {
  return page
    .getByRole("row")
    .filter({ has: page.getByRole("cell", { name: taskName, exact: true }) });
}

// 在已认证的浏览器上下文中选择确有小时归档的任务，保证下载验收不会依赖固定测试设备。
async function exportableTaskId() {
  return page.evaluate(async () => {
    const token = sessionStorage.getItem("camera-log-record-token");
    const headers = { Authorization: `Bearer ${token}` };
    const tasks = await fetch("/api/v1/tasks?page=1&pageSize=100", {
      headers,
    }).then((response) => response.json());
    for (const task of tasks.items) {
      // 实时验收必须选正在采集的任务，停止任务的历史归档不能证明实时订阅正常。
      if (task.status !== "COLLECTING") continue;
      const hours = await fetch(
        `/api/v1/tasks/${encodeURIComponent(task.id)}/log-hours`,
        { headers },
      ).then((response) => (response.ok ? response.json() : { items: [] }));
      if (hours.items?.length) return task.id;
    }
    throw new Error("没有可用于下载验收的小时归档任务");
  });
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
  await page.goto("http://127.0.0.1:5173", { waitUntil: "networkidle" });
  await page.getByRole("heading", { name: "设备资源", exact: true }).waitFor();
  await page.screenshot({ path: `${screenshots}/resources-desktop.png`, fullPage: true });
  await page.getByRole("tab", { name: "采集任务", exact: true }).click();
  await page.getByRole("button", { name: "编辑任务" }).first().waitFor();
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

  const taskId = await exportableTaskId();
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
      await context.request.delete(`http://127.0.0.1:5173/api/v1/command-templates/${createdTemplateId}?version=1`, {
        headers: { Authorization: `Bearer ${process.env.BOOTSTRAP_TOKEN}` },
      });
    }
  } finally { await browser.close(); }
}
