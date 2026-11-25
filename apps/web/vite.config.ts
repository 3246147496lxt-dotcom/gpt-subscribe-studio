import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// 仅监听回环地址；/api 反代到本机后端，避免跨域。
export default defineConfig({
  plugins: [react()],
  server: {
    host: "127.0.0.1",
    port: 5173,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8080",
        changeOrigin: true,
      },
    },
  },
  build: {
    rollupOptions: {
      output: {
        manualChunks(id) {
          if (!id.includes("node_modules")) return undefined;
          if (id.includes("/@tabler/icons-react/")) {
            return "vendor-icons";
          }
          if (id.includes("/@mantine/")) {
            return "vendor-mantine";
          }
          if (id.includes("/react/") || id.includes("/react-dom/") || id.includes("/scheduler/")) {
            return "vendor-react";
          }
          return undefined;
        },
      },
    },
  },
});
