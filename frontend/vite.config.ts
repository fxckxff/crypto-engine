import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Бэкенд по умолчанию слушает 127.0.0.1:8000 (configs/default.yaml → api.port).
// В деве UI идёт с 5173, поэтому REST и WS проксируются на бэкенд.
const backend = process.env.CLE_API_ORIGIN ?? "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    strictPort: true,
    proxy: {
      "/api": { target: backend, changeOrigin: true },
      "/ws": { target: backend, changeOrigin: true, ws: true },
    },
  },
  build: {
    outDir: "dist",
    sourcemap: true,
  },
});
