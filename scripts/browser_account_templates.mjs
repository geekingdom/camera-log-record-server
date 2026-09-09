// 账户、令牌和模板共享验收：全部平台 API 使用内存 mock，不写入真实用户或设备数据。
import assert from "node:assert/strict";
import { mkdir } from "node:fs/promises";

const imported = await import(process.env.PLAYWRIGHT_MODULE || "playwright");
const { chromium } = imported.default || imported;
const screenshots = process.env.BROWSER_SCREENSHOTS || "output/playwright";
const browser = await chromium.launch({ headless: true, channel: "chrome" });
const baseScopes = ["tasks:read", "logs:read", "logs:download", "templates:read", "templates:write", "service-tokens:read"];

const pageOf = (items, page = 1, pageSize = 100) => ({ items, total: items.length, page, pageSize });
await mkdir(screenshots, { recursive: true });
try {
  for (const width of [1440, 390]) {
    let adminMode = true;
    const users = [
      { id: "owner", username: "owner", displayName: "模板创建者", enabled: true, isAdmin: false, builtin: false, scopes: [], mustChangePassword: false, version: 1 },
      { id: "share-a", username: "share-a", displayName: "共享用户甲", enabled: true, isAdmin: false, builtin: false, scopes: [], mustChangePassword: false, version: 1 },
      { id: "share-b", username: "share-b", displayName: "共享用户乙", enabled: true, isAdmin: false, builtin: false, scopes: [], mustChangePassword: false, version: 1 },
    ];
    const tokens = [
      { id: "token-1", name: "现有令牌", userId: "owner", version: 1, revoked: false, expiresAt: "2099-01-01T00:00:00+08:00", createdAt: "2026-09-09T00:00:00+08:00", effectiveStatus: "ACTIVE", user: users[0] },
      { id: "token-disabled", name: "停用用户令牌", userId: "disabled-user", version: 1, revoked: false, expiresAt: null, createdAt: "2026-09-09T00:00:00+08:00", effectiveStatus: "USER_DISABLED", user: { id: "disabled-user", username: "disabled", displayName: "已停用用户", enabled: false } },
      { id: "token-own", name: "我的服务账号", userId: "share-a", version: 1, revoked: false, expiresAt: null, createdAt: "2026-09-09T00:00:00+08:00", effectiveStatus: "ACTIVE", user: users[1] },
      { id: "token-concurrent", name: "并发变化令牌", userId: "owner", version: 1, revoked: false, expiresAt: null, createdAt: "2026-09-09T00:00:00+08:00", effectiveStatus: "ACTIVE", user: users[0] },
    ];
    const templates = [{ id: "shared-template", name: "共享模板", description: "只可复制", version: 1, createdBy: "owner", createdByName: "模板创建者", sharedWith: ["share-a"], sharedWithAll: false, initialCommands: [], scheduledCommands: [] }];
    const requests = { users: [], tokenCreates: [], tokenUpdates: [], tokenReveals: [], tokenRotates: [], templateCreates: [], templateReads: [], templateDeletes: [] };
    const context = await browser.newContext({ viewport: { width, height: 900 } });
    await context.route("**/api/v1/**", async route => {
      const request = route.request();
      const url = new URL(request.url());
      const path = url.pathname;
      const method = request.method();
      const actor = adminMode
        ? { id: "admin", username: "admin", displayName: "管理员", isAdmin: true, builtin: true, scopes: ["*"], enabled: true, mustChangePassword: false, version: 1 }
        : { ...users[1], scopes: baseScopes };
      let body = pageOf([]);
      if (path === "/api/v1/auth/me") body = { user: actor };
      else if (path === "/api/v1/tasks") body = pageOf([]);
      else if (path === "/api/v1/nodes") body = pageOf([]);
      else if (path === "/api/v1/users/permissions") body = { scopes: [
        { value: "tasks:read", label: "查看设备与任务" }, { value: "logs:read", label: "查看日志" }, { value: "logs:download", label: "下载日志" },
        { value: "templates:read", label: "查看模板" }, { value: "templates:write", label: "管理模板" }, { value: "commands:send", label: "发送命令" },
      ] };
      else if (path === "/api/v1/users/share-targets") body = pageOf(users.map(({ id, username, displayName }) => ({ id, username, displayName })));
      else if (path === "/api/v1/users" && method === "GET") body = pageOf(users);
      else if (path === "/api/v1/users" && method === "POST") { const input = request.postDataJSON(); requests.users.push(input); body = { ...input, id: "new-user", version: 1, mustChangePassword: true }; }
      else if (path === "/api/v1/service-tokens" && method === "GET") body = pageOf(adminMode ? tokens : tokens.filter(token => token.userId === actor.id));
      else if (path === "/api/v1/service-tokens" && method === "POST") { const input = request.postDataJSON(); requests.tokenCreates.push(input); body = { id: "token-new", ...input, version: 1, revoked: false, expiresAt: input.expiresInDays === null ? null : "2099-01-01T00:00:00+08:00", createdAt: "2026-09-09T00:00:00+08:00", effectiveStatus: "ACTIVE", user: users.find(user => user.id === input.userId), token: "one-time-token" }; }
      else if (path === "/api/v1/service-tokens/token-1" && method === "PATCH") { const input = request.postDataJSON(); requests.tokenUpdates.push(input); body = { ...tokens[0], ...input, expiresAt: input.expiresInDays === null ? null : tokens[0].expiresAt, version: 2, user: users.find(user => user.id === input.userId) }; }
      else if (path === "/api/v1/service-tokens/token-1/reveal" && method === "POST") { requests.tokenReveals.push("token-1"); await route.fulfill({ status: 409, contentType: "application/json", body: JSON.stringify({ error: { code: "SERVICE_TOKEN_LEGACY_SECRET", message: "旧口令无法还原，请重新生成" } }) }); return; }
      else if (path === "/api/v1/service-tokens/token-1/rotate" && method === "POST") { requests.tokenRotates.push(request.postDataJSON()); body = { token: "rotated-admin-token" }; }
      else if (path === "/api/v1/service-tokens/token-concurrent/reveal" && method === "POST") { requests.tokenReveals.push("token-concurrent"); await route.fulfill({ status: 409, contentType: "application/json", body: JSON.stringify({ error: { code: "SERVICE_TOKEN_CHANGED", message: "服务令牌版本已变化，请刷新" } }) }); return; }
      else if (path === "/api/v1/service-tokens/token-own/reveal" && method === "POST") { requests.tokenReveals.push("token-own"); body = { token: "operator-token" }; }
      else if (path === "/api/v1/users/creators") body = pageOf(users.map(({ id, username, displayName }) => ({ id, username, displayName })));
      else if (path === "/api/v1/command-templates" && method === "GET") { requests.templateReads.push({ actor: actor.id, createdBy: url.searchParams.get("createdBy"), includeDeleted: url.searchParams.get("includeDeleted") }); body = pageOf(templates); }
      else if (path === "/api/v1/command-templates" && method === "POST") { const input = request.postDataJSON(); requests.templateCreates.push(input); body = { id: `template-${requests.templateCreates.length}`, version: 1, createdBy: actor.id, createdByName: actor.displayName, ...input }; templates.push(body); }
      else if (path.startsWith("/api/v1/command-templates/") && method === "GET") body = templates.find(item => path.endsWith(item.id));
      else if (path.startsWith("/api/v1/command-templates/") && method === "DELETE") {
        const id = path.split("/").at(-1); requests.templateDeletes.push({ id, version: url.searchParams.get("version") });
        if (id === "shared-template") { await route.fulfill({ status: 409, contentType: "application/json", body: JSON.stringify({ error: { message: "版本已变化" } }) }); return; }
        const item = templates.find(template => template.id === id); if (item) item.deletedAt = "2026-09-09T03:00:00+08:00";
        await route.fulfill({ status: 204 }); return;
      }
      await route.fulfill({ contentType: "application/json", body: JSON.stringify(body) });
    });
    const page = await context.newPage();
    page.setDefaultTimeout(5_000);
    const errors = [];
    page.on("pageerror", error => errors.push(error.message));
    await page.goto(process.env.BASE_URL || "http://127.0.0.1:5173");

    await page.getByRole("tab", { name: "账号管理", exact: true }).click();
    await page.getByRole("button", { name: "新建用户", exact: true }).click();
    const userDialog = page.getByRole("dialog", { name: "新建用户" });
    await userDialog.getByLabel("用户名", { exact: true }).fill("operator-new");
    await userDialog.getByLabel("显示名称", { exact: true }).fill("新操作员");
    await userDialog.getByLabel("初始密码", { exact: true }).fill("password-for-fixture");
    await userDialog.getByRole("button", { name: "保存", exact: true }).click();
    await page.waitForTimeout(100);
    assert.equal(requests.users.length, 1, "管理员必须能创建用户");
    assert.equal(requests.users[0].resourceIds, undefined, "用户请求不能再提交资源范围");

    await page.getByRole("tab", { name: "第三方服务账号", exact: true }).click();
    await page.getByRole("button", { name: "新建服务账号", exact: true }).click();
    const tokenDialog = page.getByRole("dialog", { name: "新建第三方服务账号" });
    await tokenDialog.getByLabel("账号名称", { exact: true }).fill("浏览器令牌");
    await tokenDialog.locator(".el-switch").click();
    await tokenDialog.locator(".el-select").click();
    await page.getByText("模板创建者 · owner", { exact: true }).click();
    await tokenDialog.getByRole("button", { name: "创建并显示口令", exact: true }).click();
    await page.getByRole("dialog", { name: "服务账号口令" }).waitFor();
    assert.deepEqual(requests.tokenCreates[0], { name: "浏览器令牌", userId: "owner", expiresInDays: null });
    assert.equal("scopes" in requests.tokenCreates[0], false);
    assert.equal("taskIds" in requests.tokenCreates[0], false);
    await page.getByRole("button", { name: "关闭", exact: true }).click();
    await page.getByRole("button", { name: "编辑服务账号", exact: true }).first().click();
    const editDialog = page.getByRole("dialog", { name: "编辑第三方服务账号" });
    await editDialog.locator(".el-select").click();
    await page.getByText("共享用户甲 · share-a", { exact: true }).click();
    await editDialog.getByText("永久", { exact: true }).click();
    await editDialog.getByRole("button", { name: "保存", exact: true }).click();
    const confirm = page.getByRole("dialog", { name: "确认保存服务令牌" });
    await confirm.waitFor();
    await confirm.getByRole("button", { name: "确认保存", exact: true }).click();
    await page.waitForTimeout(100);
    assert.deepEqual(requests.tokenUpdates[0], { version: 1, name: "现有令牌", userId: "share-a", expiresInDays: null });
    await page.getByText("停用用户令牌", { exact: true }).waitFor();
    assert.equal(await page.getByText("永久", { exact: true }).count() > 0, true, "永久令牌应在列表中明确标识");
    assert.equal(await page.getByText("所属用户已停用", { exact: true }).count(), 1, "停用用户的令牌应显示动态状态");
    await page.getByRole("button", { name: "查看服务账号口令", exact: true }).first().click();
    const rotateConfirm = page.getByRole("dialog", { name: "旧口令无法查看" });
    await rotateConfirm.getByRole("button", { name: "重新生成", exact: true }).click();
    await page.getByRole("dialog", { name: "服务账号口令" }).waitFor();
    assert.deepEqual(requests.tokenReveals, ["token-1"]);
    assert.deepEqual(requests.tokenRotates, [{ version: 1 }], "旧口令不可还原时仅经确认后重新生成");
    await page.getByRole("button", { name: "关闭", exact: true }).click();
    await page.getByRole("dialog", { name: "服务账号口令" }).waitFor({ state: "hidden" });
    await page.locator(".el-table__row", { hasText: "并发变化令牌" }).getByRole("button", { name: "查看服务账号口令" }).click();
    await page.waitForTimeout(100);
    assert.equal(await page.getByRole("dialog", { name: "旧口令无法查看" }).count(), 0, "并发冲突不能诱导管理员重新生成口令");
    assert.deepEqual(requests.tokenRotates, [{ version: 1 }], "非 legacy 409 不得调用重新生成");
    await page.screenshot({ path: `${screenshots}/account-tokens-admin-${width}.png`, fullPage: true, animations: "disabled" });

    await page.getByRole("tab", { name: "命令模板", exact: true }).click();
    await page.getByRole("button", { name: "新建模板", exact: true }).click();
    const templateDrawer = page.getByRole("dialog", { name: "新建命令模板" });
    await templateDrawer.getByLabel("模板名称", { exact: true }).fill("定向共享模板");
    await templateDrawer.locator(".template-share-recipients .el-select").click();
    await page.getByText("共享用户甲 · share-a", { exact: true }).click();
    await templateDrawer.locator(".template-share-recipients .el-select").click();
    await page.getByText("共享用户乙 · share-b", { exact: true }).click();
    await templateDrawer.getByRole("button", { name: "保存模板", exact: true }).click();
    const confirmTemplate = page.getByRole("dialog", { name: "确认保存模板" });
    await confirmTemplate.getByRole("button", { name: "确认", exact: true }).click();
    await page.waitForTimeout(100);
    assert.deepEqual(requests.templateCreates[0].sharedWith, ["share-a", "share-b"]);
    assert.equal(requests.templateCreates[0].sharedWithAll, false);
    await page.getByRole("button", { name: "新建模板", exact: true }).click();
    const globalDrawer = page.getByRole("dialog", { name: "新建命令模板" });
    await globalDrawer.getByLabel("模板名称", { exact: true }).fill("管理员全局模板");
    await globalDrawer.locator(".template-share-recipients .el-checkbox").click();
    await globalDrawer.getByRole("button", { name: "保存模板", exact: true }).click();
    await page.getByRole("dialog", { name: "确认保存模板" }).getByRole("button", { name: "确认", exact: true }).click();
    await page.waitForTimeout(100);
    assert.equal(requests.templateCreates[1].sharedWithAll, true, "管理员可创建全局共享模板");
    const sharedRow = page.getByRole("row", { name: /^选择当前行 共享模板 / });
    const directedRow = page.getByRole("row", { name: /^选择当前行 定向共享模板 / });
    await sharedRow.locator(".el-checkbox").click();
    await directedRow.locator(".el-checkbox").click();
    await page.getByRole("button", { name: /删除已选 \(2\)/ }).click();
    const batchConfirm = page.getByRole("dialog", { name: "确认批量删除模板" });
    await batchConfirm.getByRole("button", { name: "确认", exact: true }).click();
    await batchConfirm.waitFor({ state: "hidden" });
    await page.getByText("删除失败：", { exact: false }).waitFor();
    assert.deepEqual(requests.templateDeletes, [
      { id: "shared-template", version: "1" },
      { id: "template-1", version: "1" },
    ], "模板批量删除必须按勾选顺序串行，单项冲突后继续后续项");
    await page.screenshot({ path: `${screenshots}/account-templates-admin-${width}.png`, fullPage: true, animations: "disabled" });

    adminMode = false;
    await page.reload();
    assert.equal(await page.getByRole("tab", { name: "账号管理", exact: true }).count(), 0, "非管理员没有账户管理入口");
    await page.getByRole("tab", { name: "我的服务账号", exact: true }).click();
    await page.getByText("我的服务账号", { exact: true }).last().waitFor();
    assert.equal(await page.getByText("现有令牌", { exact: true }).count(), 0, "普通用户只能读取绑定到自己的服务账号");
    assert.equal(await page.getByRole("button", { name: "新建服务账号", exact: true }).count(), 0, "普通用户不能创建服务账号");
    assert.equal(await page.getByRole("button", { name: "编辑服务账号", exact: true }).count(), 0, "普通用户不能编辑服务账号");
    assert.equal(await page.getByRole("button", { name: "撤销服务账号", exact: true }).count(), 0, "普通用户不能撤销服务账号");
    await page.getByRole("button", { name: "查看服务账号口令", exact: true }).click();
    await page.getByRole("dialog", { name: "服务账号口令" }).waitFor();
    assert.deepEqual(requests.tokenReveals, ["token-1", "token-concurrent", "token-own"], "普通用户可以查看自己的服务账号口令");
    await page.getByRole("button", { name: "关闭", exact: true }).click();
    await page.getByRole("dialog", { name: "服务账号口令" }).waitFor({ state: "hidden" });
    await page.screenshot({ path: `${screenshots}/account-tokens-operator-${width}.png`, fullPage: true, animations: "disabled" });
    await page.getByRole("tab", { name: "命令模板", exact: true }).click();
    await page.getByText("共享模板", { exact: true }).waitFor();
    assert.equal(requests.templateReads.at(-1)?.createdBy, "share-a", "普通用户默认只请求自己的模板");
    assert.equal(await page.getByRole("button", { name: "编辑模板", exact: true }).count(), 0, "共享非创建者不能编辑模板");
    assert.equal(await page.getByRole("button", { name: "删除模板", exact: true }).count(), 0, "共享非创建者不能删除模板");
    await page.getByRole("button", { name: "新建模板", exact: true }).click();
    const operatorDrawer = page.getByRole("dialog", { name: "新建命令模板" });
    await operatorDrawer.waitFor({ state: "visible" });
    await page.waitForTimeout(300);
    assert.equal(await operatorDrawer.getByRole("checkbox", { name: "共享给全部用户" }).count(), 0, "非管理员不显示全局共享开关");
    const layout = await page.evaluate(() => ({ scrollWidth: document.documentElement.scrollWidth, clientWidth: document.documentElement.clientWidth }));
    assert.ok(layout.scrollWidth <= layout.clientWidth + 1, `页面不应横向溢出：${JSON.stringify({ width, ...layout })}`);
    await page.screenshot({ path: `${screenshots}/account-templates-operator-${width}.png`, fullPage: true, animations: "disabled" });
    assert.deepEqual(errors, []);
    await context.close();
  }
  console.log(JSON.stringify({ passed: true, screenshots: `${screenshots}/account-templates-{admin,operator}-{1440,390}.png` }));
} finally {
  await browser.close();
}
