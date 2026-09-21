import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  base: "/app/",  // 由后端挂在 /app 路径下，同源访问 /api
  server: {
    port: 5178,
    strictPort: true,
    proxy: {
      "/api": "http://127.0.0.1:18780",
    },
  },
  build: { outDir: "dist" },
});
