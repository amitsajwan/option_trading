import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  build: {
    rollupOptions: {
      output: {
        manualChunks(id) {
          if (!id.includes("node_modules")) return undefined;
          if (id.includes("echarts") || id.includes("zrender") || id.includes("echarts-for-react")) {
            return "charts";
          }
          if (id.includes("@tanstack/react-table")) {
            return "tables";
          }
          if (id.includes("@tanstack/react-query") || id.includes("@stomp/stompjs")) {
            return "data";
          }
          return "vendor";
        },
      },
    },
  },
  server: {
    host: "0.0.0.0",
    port: 8011,
  },
  preview: {
    host: "0.0.0.0",
    port: 8011,
  },
});
