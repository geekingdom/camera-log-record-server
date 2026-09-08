// 开发代理保留正式 API 路径，并代理同一路径下的实时 WebSocket 连接。
import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";
import Components from "unplugin-vue-components/vite";
import { ElementPlusResolver } from "unplugin-vue-components/resolvers";

export default defineConfig({
  plugins: [
    vue(),
    Components({
      dts: false,
      directives: true,
      resolvers: [ElementPlusResolver({ importStyle: "css" })],
    }),
  ],
  server: {
    proxy: {
      "/api": { target: "http://127.0.0.1:8000", ws: true },
    },
  },
});
