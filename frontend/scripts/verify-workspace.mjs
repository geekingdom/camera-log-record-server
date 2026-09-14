// 启动仅用于验收的生产预览，复用布局测试，结束后关闭监听而不影响开发服务。
import { execFile } from "node:child_process";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";
import { preview } from "vite";

const root = fileURLToPath(new URL("..", import.meta.url));
const repository = fileURLToPath(new URL("../..", import.meta.url));
// 所有脚本在独立生产预览端口执行；新增工作台功能不得依赖本机5173开发服务。
const scripts = [
  "browser_workspace_layout", "browser_api_reference", "browser_account_templates",
  "browser_template_navigation", "browser_default_resource_navigation",
  "browser_resource_bulk_delete", "browser_task_bulk_operations",
  "browser_coredump_shared_monitor", "browser_blocked_recovery", "browser_command_history",
  "browser_resource_authentication_records", "browser_node_dashboard",
  "browser_live_terminal_workbench", "browser_archive_viewer", "browser_live_ranges", "browser_resource_metrics",
  "browser_resource_monitor_settings",
  "browser_ssh_target_editor",
  "browser_resource_manual_authentication",
  "browser_resource_identity_filters",
];
const server = await preview({ root, preview: { host: "127.0.0.1", port: 0, strictPort: true } });
try {
  const address = server.httpServer.address();
  const environment = { ...process.env, BASE_URL: `http://127.0.0.1:${address.port}` };
  for (const script of scripts) {
    const result = await promisify(execFile)(process.execPath, [`scripts/${script}.mjs`], {
      cwd: repository,
      env: environment,
      timeout: 180_000,
      maxBuffer: 4 * 1024 * 1024,
    });
    process.stdout.write(result.stdout);
    process.stderr.write(result.stderr);
  }
} finally {
  await new Promise((resolve, reject) => server.httpServer.close(error => error ? reject(error) : resolve()));
}
