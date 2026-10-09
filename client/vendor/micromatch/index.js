"use strict";
/**
 * Stands in for micromatch under @lingui/cli (#270), through `overrides` in
 * client/package.json.
 *
 * micromatch pulls in `braces`, and every release of braces up to 3.0.3 -- the
 * last there is -- carries GHSA-vfj7-8cjw-p6xm (stack exhaustion on deeply
 * nested patterns). @lingui/cli calls micromatch in exactly two places:
 * `capture` to read the locale out of a catalog path, and `any` to skip
 * ignored files in watch mode. Both are picomatch underneath, and picomatch
 * expands braces itself, so this is micromatch's own code for those two
 * functions, with nothing else and no braces.
 *
 * If a Lingui upgrade starts calling anything else, `npm run extract` fails
 * on the missing function rather than quietly matching differently; and
 * `micromatch-shim.test.ts` holds both functions to what micromatch answered.
 */
const picomatch = require("picomatch");

const windows = (options) => (options && options.windows !== undefined ? options.windows : process.platform === "win32");
const posix = (input) => input.replace(/\\/g, "/");

function isMatch(str, patterns, options) {
  return picomatch(patterns, options)(str);
}

function capture(glob, input, options) {
  const regex = picomatch.makeRe(String(glob), { ...options, capture: true });
  const match = regex.exec(windows(options) ? posix(input) : input);
  if (match) return match.slice(1).map((v) => (v === undefined ? "" : v));
  return undefined;
}

module.exports = { isMatch, any: isMatch, capture };
module.exports.default = module.exports;
