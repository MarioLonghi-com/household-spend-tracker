/// <reference types="vitest/config" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The client is served by FastAPI in production, out of app/static/dist. In
// development Vite serves it and proxies the API, so cookies stay same-origin
// and the CSRF check behaves exactly as it does in production.
export default defineConfig(({ mode }) => ({
  plugins: [react()],
  build: {
    outDir: "../app/static/dist",
    emptyOutDir: true,
  },
  // Vitest picks up `**/*.spec.ts` by default, which would have it collecting
  // the Playwright suite and failing on `@playwright/test`'s imports. The two
  // runners cover different things and neither should try to run the other's
  // files: `npm test` is units, `npm run e2e` is the browser.
  test: {
    include: ["src/**/*.{test,spec}.{ts,tsx}"],
    exclude: ["e2e/**", "node_modules/**"],
  },
  server: {
    port: 5173,
    // Under vitest only: `/snap`'s script is served by FastAPI, not bundled,
    // and its tests load it from `app/static/snap`. The dev server keeps the
    // default and serves nothing outside `client/`.
    ...(mode === "test" ? { fs: { allow: [".", "../app/static/snap"] } } : {}),
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8848",
        changeOrigin: false,
      },
    },
  },
}));
