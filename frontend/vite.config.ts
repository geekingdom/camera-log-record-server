// 开发代理保留正式 API 路径，并代理同一路径下的实时 WebSocket 连接。
import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";
import Components from "unplugin-vue-components/vite";
import { ElementPlusResolver } from "unplugin-vue-components/resolvers";
import { existsSync, readdirSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, join } from "node:path";

const require = createRequire(import.meta.url);
const componentRoot = join(dirname(require.resolve("element-plus/package.json")), "es/components");
const componentStyles = readdirSync(componentRoot)
  .filter(name => existsSync(join(componentRoot, name, "style/css.mjs")))
  .map(name => `element-plus/es/components/${name}/style/css`);

export default defineConfig({
  plugins: [
    vue(),
    Components({
      dts: false,
      directives: true,
      resolvers: [ElementPlusResolver({ importStyle: "css" })],
    }),
  ],
  // 懒加载页面的样式由组件插件注入，提前预优化以免首次导航触发整页刷新。
  optimizeDeps: {
    include: ["element-plus/es", ...componentStyles],
  },
  server: {
    proxy: {
      "/api": { target: "http://127.0.0.1:8000", ws: true },
    },
  },
});
