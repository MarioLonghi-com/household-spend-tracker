/// <reference types="vitest/config" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import babel from "@rolldown/plugin-babel";
import { lingui, linguiTransformerBabelPreset } from "@lingui/vite-plugin";

// The client is served by FastAPI in production, out of app/static/dist. In
// development Vite serves it and proxies the API, so cookies stay same-origin
// and the CSRF check behaves exactly as it does in production.
export default defineConfig(({ mode }) => ({
  // Lingui (#53): `lingui()` compiles a `.po` catalog when it is imported, and
  // the Babel preset turns the `t`, `msg`, `plural` and `<Trans>` macros into
  // message lookups. Babel runs only on the files the preset's filter selects --
  // the ones importing a Lingui macro -- so the rest of the build stays on
  // Vite's own transform. The Lingui 6 plugin has no `macroTransform` option;
  // `@rolldown/plugin-babel` is the documented route, not a fallback.
  plugins: [react(), lingui(), babel({ presets: [linguiTransformerBabelPreset()] })],
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
    // Pins the formatting locale to en-US, so a test's expected "€1,234.56"
    // does not depend on the machine running it (#52).
    setupFiles: ["./src/test-setup.ts"],
    // A deadline for a test that has hung, not a claim about speed: twice the
    // longest wait (`WAIT_DEADLINE` in `src/test-setup.ts`, which says why it
    // is generous). vitest's default of five seconds failed tests that take a
    // fraction of that alone, whenever the machine was busy with other work.
    testTimeout: 60_000,
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
