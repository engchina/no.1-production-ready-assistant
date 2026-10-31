import { resolve } from "node:path";

import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// ライブラリモード: React コンポーネント群を ESM で出力する。
// react / react-dom / react-router 等のランタイムは消費側に委ねる（peer/external）。
export default defineConfig({
  plugins: [react()],
  build: {
    lib: {
      entry: resolve(import.meta.dirname, "src/index.ts"),
      formats: ["es"],
      fileName: () => "index.js",
    },
    rollupOptions: {
      external: [
        "react",
        "react/jsx-runtime",
        "react-dom",
        "react-dom/client",
        "lucide-react",
        "zustand",
        "zustand/middleware",
      ],
    },
    sourcemap: true,
    emptyOutDir: true,
  },
});
