import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig(({ command }) => ({
  plugins: [react()],
  base: command === "build" ? "/frontend/" : "/",
  build: {
    outDir: "../app/static/react",
    emptyOutDir: true,
  },
  server: {
    port: 5173,
    proxy: {
      "/cases": "http://localhost:8000",
      "/pipeline": "http://localhost:8000",
      "/health": "http://localhost:8000",
    },
  },
}));
