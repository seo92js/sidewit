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
});
