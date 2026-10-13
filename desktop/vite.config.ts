/// <reference types="vitest/config" />
import { defineConfig } from "vite";
import babel from "@rolldown/plugin-babel";
import { lingui, linguiTransformerBabelPreset } from "@lingui/vite-plugin";
import { fileURLToPath } from "node:url";

// Every Lingui piece -- the plugin, the Babel preset, the macro inside it --
// looks for its config from the working directory; the shell's is the client's.
process.env.LINGUI_CONFIG = fileURLToPath(new URL("../client/lingui.config.ts", import.meta.url));

/**
 * The shell's main process and its preload, bundled for Electron's Node (#61).
 *
 * Bundled rather than compiled file by file, for the same reason the client
 * is: the shell's words are Lingui messages (#62), and the `msg` macro and the
 * `.po` catalogs it reads are build-time transforms. The catalogs are the
 * client's own -- `client/lingui.config.ts` extracts from `desktop/src` too --
 * so a menu item is translated in the same file as the screen it opens.
 *
 * Everything is bundled but Electron itself and Node's own modules, so the
 * packaged app needs no `node_modules`. CommonJS, because a sandboxed preload
 * cannot be an ES module.
 */
// Two builds, because a sandboxed preload can `require` nothing of its own: a
// chunk the two entries shared would be a file it cannot load. `vite build`
// is the main process; `vite build --mode preload` the preload, into the same
// folder.
export default defineConfig(({ mode }) => ({
  plugins: [
    lingui(),
    babel({ presets: [linguiTransformerBabelPreset()] }),
  ],
  build: {
    outDir: "build",
    emptyOutDir: mode !== "preload",
    target: "node22",
    ssr: true,
    minify: false,
    sourcemap: true,
    rollupOptions: {
      input: (mode === "preload" ? { preload: "src/preload.ts" } : { main: "src/main.ts" }) as Record<string, string>,
      external: ["electron", /^node:/],
      output: { format: "cjs", entryFileNames: "[name].cjs" },
    },
  },
  ssr: { noExternal: true },
  test: {
    include: ["test/**/*.test.ts"],
  },
}));
