/**
 * Which npm packages a build actually bundled (#63).
 *
 * The desktop app's Licences window lists the third-party code it ships, and
 * "production dependencies" from package.json is the wrong list for that:
 * Vite bundles only what the source imports, and the declared graph reaches
 * build tooling it never ships -- `@lingui/core` declares the Babel macro
 * plugin as a dependency, which drags in Babel and Jest. So this plugin
 * writes the packages whose modules ended up in the output chunks, each with
 * its version and folder, and `scripts/third_party_notices.py` turns that
 * into the notices with a licence tool over the same `node_modules`.
 */

import fs from "node:fs";
import path from "node:path";
import type { Plugin } from "vite";

export type Bundled = { name: string; version: string; path: string };

const NODE_MODULES = `${path.sep}node_modules${path.sep}`;

/** The package.json nearest above a bundled module, when the module came from node_modules. */
function packageOf(id: string): Bundled | null {
  const file = id.split("?")[0];
  const at = file.lastIndexOf(NODE_MODULES);
  if (file.startsWith("\0") || at === -1) return null;
  let folder = path.dirname(file);
  const floor = file.slice(0, at + NODE_MODULES.length - 1);
  while (folder.length > floor.length) {
    const manifest = path.join(folder, "package.json");
    if (fs.existsSync(manifest)) {
      const read = JSON.parse(fs.readFileSync(manifest, "utf8")) as { name?: string; version?: string };
      if (read.name && read.version) return { name: read.name, version: read.version, path: folder };
    }
    folder = path.dirname(folder);
  }
  return null;
}

/** Writes `out` (JSON, a sorted array of {name, version, path}) when the build's chunks are known. */
export function bundledPackages(out: string): Plugin {
  return {
    name: "bundled-packages",
    apply: "build",
    generateBundle(_options, bundle) {
      const found = new Map<string, Bundled>();
      for (const chunk of Object.values(bundle)) {
        if (chunk.type !== "chunk") continue;
        for (const id of Object.keys(chunk.modules)) {
          const one = packageOf(id);
          if (one) found.set(`${one.name}@${one.version}`, one);
        }
      }
      const list = [...found.values()].sort((a, b) => a.name.localeCompare(b.name) || a.version.localeCompare(b.version));
      fs.mkdirSync(path.dirname(out), { recursive: true });
      fs.writeFileSync(out, `${JSON.stringify(list, null, 2)}\n`);
    },
  };
}
