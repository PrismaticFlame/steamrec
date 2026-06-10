import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";
import path from "path";

// https://vite.dev/config/
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, path.resolve(__dirname, ".."), "");

  return {
    plugins: [react()],
    server: {
      proxy: {
        "/api/health": {
          target: "http://localhost:8000",
          rewrite: (path) => "/",
        },
        "/api/apps": {
          target: "http://localhost:8000",
          rewrite: (path) => path.replace(/^\/api/, ""),
        },
        "/api": "http://localhost:8000",
        "/llm": "http://localhost:8000",
      },
    },
  };
});
