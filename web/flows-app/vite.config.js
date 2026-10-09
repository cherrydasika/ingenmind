// Builds the canvas as one ES module + one stylesheet into web/static/flows,
// which the vanilla UI loads with a dynamic import (no CDN, no dev server).
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  define: { "process.env.NODE_ENV": JSON.stringify("production") },
  build: {
    outDir: "../static/flows",
    emptyOutDir: true,
    cssCodeSplit: false,
    minify: true,
    // Library mode keeps ES output readable; the bundle is an app, so minify it fully.
    rolldownOptions: { output: { minify: true } },
    lib: { entry: "src/main.jsx", formats: ["es"], fileName: () => "flows.js", cssFileName: "flows" },
  },
});
