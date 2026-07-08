import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5000,
    host: true,
  },
  build: {
    rollupOptions: {
      output: {
        // Split heavy vendor libraries out of the main chunk so first paint is lighter and
        // these rarely-changing bundles cache independently. (mermaid is loaded via dynamic
        // import(), so it and its diagram engines already become their own lazy chunks.)
        manualChunks(id) {
          if (!id.includes("node_modules")) return;
          if (id.includes("highlight.js")) return "vendor-highlight";
          if (id.includes("katex")) return "vendor-katex";
          if (
            /react-markdown|remark-|rehype-|micromark|mdast|hast-|unist-|unified|property-information|vfile/.test(
              id,
            )
          ) {
            return "vendor-markdown";
          }
          if (id.includes("@tanstack")) return "vendor-query";
          if (/[\\/]react-dom[\\/]|[\\/]react-router|[\\/]scheduler[\\/]/.test(id)) {
            return "vendor-react";
          }
        },
      },
    },
  },
});
