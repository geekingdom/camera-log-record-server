// 启动仅用于验收的生产预览，复用布局测试，结束后关闭监听而不影响开发服务。
import { execFile } from "node:child_process";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";
import { preview } from "vite";

const root = fileURLToPath(new URL("..", import.meta.url));
const repository = fileURLToPath(new URL("../..", import.meta.url));
const server = await preview({ root, preview: { host: "127.0.0.1", port: 0, strictPort: true } });
try {
  const address = server.httpServer.address();
  const result = await promisify(execFile)(process.execPath, ["scripts/browser_workspace_layout.mjs"], {
    cwd: repository,
    env: { ...process.env, BASE_URL: `http://127.0.0.1:${address.port}` },
    timeout: 120_000,
    maxBuffer: 4 * 1024 * 1024,
  });
  process.stdout.write(result.stdout);
  process.stderr.write(result.stderr);
} finally {
  await new Promise((resolve, reject) => server.httpServer.close(error => error ? reject(error) : resolve()));
}
