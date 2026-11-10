/**
 * Electron's fuses on the store builds (#63).
 *
 * A fuse is a switch baked into the Electron binary itself, read before any
 * of our code runs, so nothing started with the app can turn it back. The
 * five set here close the ways a packaged Electron app can be made to run
 * somebody else's code:
 *
 * - `RunAsNode` off: `ELECTRON_RUN_AS_NODE=1` no longer turns the app into a
 *   plain Node, able to run any script it is given;
 * - `EnableNodeOptionsEnvironmentVariable` off: `NODE_OPTIONS` no longer
 *   loads a module into the process;
 * - `EnableNodeCliInspectArguments` off: `--inspect` and its kin no longer
 *   open a debugger port on the main process;
 * - `OnlyLoadAppFromAsar` on: the shell is loaded from `app.asar` only, never
 *   from a folder somebody put beside it;
 * - `EnableEmbeddedAsarIntegrityValidation` on: that asar's header must match
 *   the hash electron-builder wrote into the binary (Info.plist on a Mac, a
 *   resource of the .exe on Windows), so a changed asar does not start.
 *
 * Run as electron-builder's `afterPack` hook (electron-builder.yml), which is
 * the one moment the flip can happen: after the app folder is complete and
 * before anything is signed or packed into an MSIX. The flip rewrites the
 * Electron binary, so a signature made earlier would be invalid, and the
 * MSIX is made from this folder afterwards. On a Mac the ad hoc signature is
 * reset on the way; `scripts/sign-mas.sh` signs over it with `--force`.
 *
 * `SPENDTRACKER_FUSES` decides, and `scripts/desktop_build.py` always sets
 * it: `on` for the `store` and `win` targets, `off` for `mac` and `mas`
 * unless asked otherwise. Off, because the end-to-end smoke test drives the
 * packaged app with Playwright, which needs `--inspect` -- the third fuse --
 * and so cannot run against a fused build. CI proves both: the smoke test on
 * an unfused build, and `check` below on a fused build of the same target.
 *
 *     node scripts/fuses.cjs check <app or exe> on|off
 *
 * reads the fuses back from the built binary and exits 1 if any of the five
 * is not where that mode puts it. `off` is checked too, so the build the
 * smoke test runs is proven to be the one it can drive.
 *
 * CommonJS, because electron-builder `require`s a hook named by path; the
 * library itself is ES modules only, hence the `import()`.
 */

"use strict";

const path = require("node:path");

/** Each fuse and where a fused build puts it. Electron ships every one the other way. */
const WANTED = {
  RunAsNode: false,
  EnableNodeOptionsEnvironmentVariable: false,
  EnableNodeCliInspectArguments: false,
  OnlyLoadAppFromAsar: true,
  EnableEmbeddedAsarIntegrityValidation: true,
};

/** "on" or "off", from SPENDTRACKER_FUSES. Unset is off; anything else is a mistake. */
function mode(env) {
  const value = (env.SPENDTRACKER_FUSES ?? "").trim().toLowerCase();
  if (value === "on" || value === "off") return value;
  if (value === "") return "off";
  throw new Error(`SPENDTRACKER_FUSES is "${env.SPENDTRACKER_FUSES}"; it is "on" or "off"`);
}

/** The state each of the five fuses should be in, for a mode. */
function expected(which) {
  const fused = which === "on";
  return Object.fromEntries(Object.entries(WANTED).map(([name, wanted]) => [name, fused ? wanted : !wanted]));
}

/** What electron-builder left in `appOutDir`: the .app on a Mac, the .exe on Windows. */
function binary(appOutDir, electronPlatformName, productFilename) {
  const extension = { darwin: ".app", mas: ".app", win32: ".exe" }[electronPlatformName];
  if (extension === undefined) throw new Error(`no fuses for ${electronPlatformName}`);
  return path.join(appOutDir, `${productFilename}${extension}`);
}

async function flip(target, electronPlatformName) {
  const { flipFuses, FuseV1Options, FuseVersion } = await import("@electron/fuses");
  const config = { version: FuseVersion.V1, resetAdHocDarwinSignature: electronPlatformName !== "win32" };
  for (const [name, wanted] of Object.entries(WANTED)) config[FuseV1Options[name]] = wanted;
  await flipFuses(target, config);
}

/** The fuses as the binary has them, by name, true for enabled. Throws on a state that is neither. */
async function read(target) {
  const { getCurrentFuseWire, FuseV1Options, FuseState } = await import("@electron/fuses");
  const wire = await getCurrentFuseWire(target);
  const found = {};
  for (const name of Object.keys(WANTED)) {
    const state = wire[FuseV1Options[name]];
    if (state === FuseState.ENABLE) found[name] = true;
    else if (state === FuseState.DISABLE) found[name] = false;
    else throw new Error(`${name} is in state ${state}, neither enabled nor disabled`);
  }
  return found;
}

/** Every fuse that is not where `which` puts it, as "name: is X, should be Y" lines. */
function wrong(found, which) {
  const should = expected(which);
  const word = (state) => (state ? "on" : "off");
  return Object.keys(WANTED)
    .filter((name) => found[name] !== should[name])
    .map((name) => `${name}: is ${word(found[name])}, should be ${word(should[name])}`);
}

/** electron-builder's afterPack hook. */
async function afterPack(context) {
  const which = mode(process.env);
  const target = binary(context.appOutDir, context.electronPlatformName, context.packager.appInfo.productFilename);
  if (which === "off") {
    process.stderr.write(`  • fuses left as Electron ships them (SPENDTRACKER_FUSES=off)  ${target}\n`);
    return;
  }
  await flip(target, context.electronPlatformName);
  const problems = wrong(await read(target), "on");
  if (problems.length) throw new Error(`the fuses did not take:\n  ${problems.join("\n  ")}`);
  process.stderr.write(`  • fuses set  ${Object.keys(WANTED).join(", ")}  ${target}\n`);
}

async function main(argv) {
  const [command, target, which] = argv;
  if (command !== "check" || !target || (which !== "on" && which !== "off")) {
    process.stderr.write("usage: node scripts/fuses.cjs check <app or exe> on|off\n");
    return 2;
  }
  const found = await read(target);
  for (const [name, state] of Object.entries(found)) process.stdout.write(`${name}: ${state ? "on" : "off"}\n`);
  const problems = wrong(found, which);
  if (problems.length) {
    process.stderr.write(`fuses not as a build with SPENDTRACKER_FUSES=${which} has them:\n  ${problems.join("\n  ")}\n`);
    return 1;
  }
  process.stdout.write(`fuses ${which}: ${target}\n`);
  return 0;
}

module.exports = { afterPack, WANTED, mode, expected, binary, read, wrong };

if (require.main === module) {
  main(process.argv.slice(2)).then(
    (code) => process.exit(code),
    (problem) => {
      process.stderr.write(`${problem.message}\n`);
      process.exit(1);
    },
  );
}
