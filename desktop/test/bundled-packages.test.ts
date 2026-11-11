import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { describe, expect, it } from "vitest";
import { packageOf } from "../../client/bundled-packages.ts";

describe("the packages a build bundled (#63)", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "st-bundled-"));
  const react = path.join(root, "node_modules", "react");
  fs.mkdirSync(path.join(react, "cjs"), { recursive: true });
  fs.writeFileSync(path.join(react, "package.json"), JSON.stringify({ name: "react", version: "19.3.1" }));
  const scoped = path.join(root, "node_modules", "@lingui", "core", "dist");
  fs.mkdirSync(scoped, { recursive: true });
  fs.writeFileSync(path.join(scoped, "..", "package.json"), JSON.stringify({ name: "@lingui/core", version: "6.11.0" }));
  const posix = root.replace(/\\/g, "/");

  it("finds the package.json above a module, scoped or not, and ignores a query string", () => {
    expect(packageOf(`${posix}/node_modules/react/cjs/react.production.js?commonjs-proxy`)).toEqual({ name: "react", version: "19.3.1", path: `${posix}/node_modules/react` });
    expect(packageOf(`${posix}/node_modules/@lingui/core/dist/index.mjs`)).toEqual({ name: "@lingui/core", version: "6.11.0", path: `${posix}/node_modules/@lingui/core` });
  });

  it("reads a Windows id the same, whichever slashes it came with", () => {
    const windows = `${posix.replace(/\//g, "\\")}\\node_modules\\react\\cjs\\react.production.js`;
    expect(packageOf(windows)?.name).toBe("react");
  });

  it("is null for the app's own files and virtual modules", () => {
    expect(packageOf(`${posix}/src/main.ts`)).toBeNull();
    expect(packageOf("\0vite/modulepreload-polyfill.js")).toBeNull();
  });
});
