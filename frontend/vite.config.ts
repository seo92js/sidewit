/// <reference types="vitest/config" />
import path from "node:path";

import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: { "@": path.resolve(import.meta.dirname, "src") },
  },
  build: {
    // The lazily loaded Monaco editor chunk is ~4 MB; everything else stays small.
    chunkSizeWarningLimit: 4500,
  },
  server: {
    proxy: {
      "/api": "http://localhost:8000",
    },
  },
  test: {
    deps: {
      optimizer: {
        // These barrels load thousands of modules per test file unless pre-bundled.
        client: { enabled: true, include: ["lucide-react", "radix-ui"] },
      },
    },
    // Page-level renders are CPU-bound and slow down under machine load.
    testTimeout: 15_000,
  },
});
