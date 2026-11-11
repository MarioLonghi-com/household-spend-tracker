/**
 * Opening the app with Touch ID (#432): the shell's half.
 *
 * A convenience on top of the password, never instead of it. The backend
 * issues a secret for this one purpose (`app/auth/unlock.py`) and keeps only
 * its hash; this module keeps the secret itself, encrypted by the operating
 * system (`safeStorage`, which on a Mac rests on the keychain), in a file of
 * its own beside the ledger -- never in `shell.json`, and never the password.
 *
 * **How the secret gets here, without the page ever holding it.** The page
 * turns it on from Profile with `POST /api/me/device-unlock`, and the backend
 * answers with the secret in one response header. Every answer passes
 * through the shell's scheme handler on its way to the window (`scheme.ts`),
 * and `observe` takes that header off there -- the way the cookie jar takes
 * `Set-Cookie` -- so what reaches the page is the status alone. No IPC
 * carries it, and nothing the page can do asks the shell for it.
 *
 * **What ends it.** The backend's row goes when it is turned off, when the
 * password changes, when the recovery code replaces a forgotten password, on
 * a restore and on an account reset. `observe` forgets the file on the three
 * of those it can see pass through, so the next launch does not prompt for a
 * secret the backend will refuse; a secret refused at launch all the same --
 * revoked some other way -- is forgotten then. The biometric prompt gates the
 * secret's *use* (`openWithPrompt`), not its storage.
 */

import fs from "node:fs";

/** The header the backend puts the secret in, once, lower-cased as `Headers` gives it. */
export const HEADER = "x-spend-tracker-device-unlock";

/** Electron's `safeStorage`, as much of it as this needs. */
export type Crypt = {
  available(): boolean;
  encrypt(text: string): Buffer;
  decrypt(data: Buffer): string;
};

/** The encrypted secret on disk: written whole through a rename, read leniently. */
export class UnlockStore {
  constructor(
    readonly file: string,
    private readonly crypt: Crypt,
    private readonly log: (line: string) => void = () => undefined,
  ) {}

  /** Whether a secret is kept here. Says nothing about whether the backend still takes it. */
  held(): boolean {
    return fs.existsSync(this.file);
  }

  /** Keep `secret`. False, and nothing kept, where the operating system cannot encrypt it. */
  take(secret: string): boolean {
    if (!this.crypt.available()) {
      this.log("unlock: not kept, the operating system cannot encrypt it here");
      return false;
    }
    const temporary = `${this.file}.writing`;
    try {
      fs.writeFileSync(temporary, this.crypt.encrypt(secret), { mode: 0o600 });
      fs.renameSync(temporary, this.file);
    } catch (problem) {
      this.log(`unlock: not kept: ${String(problem)}`);
      fs.rmSync(temporary, { force: true });
      return false;
    }
    return true;
  }

  /** The secret, or null when there is none or it cannot be read back. */
  read(): string | null {
    if (!this.held()) return null;
    try {
      return this.crypt.decrypt(fs.readFileSync(this.file)) || null;
    } catch (problem) {
      this.log(`unlock: not read: ${String(problem)}`);
      return null;
    }
  }

  forget(): void {
    fs.rmSync(this.file, { force: true });
  }
}

export type Passing = { method: string; path: string; status: number };

/** The answers that end the secret on the backend, which the shell can see go by. */
const FORGETS: readonly Passing[] = [
  { method: "DELETE", path: "/api/me/device-unlock", status: 204 },
  { method: "POST", path: "/api/me/password", status: 200 },
  { method: "POST", path: "/api/session/recover", status: 200 },
];

/**
 * One answer on its way to the window: keep or forget the secret as it says,
 * and return the headers the page may see. The header carrying the secret
 * never passes, whatever the request was.
 */
export function observe(store: UnlockStore, passing: Passing, headers: readonly [string, string][]): [string, string][] {
  const kept: [string, string][] = [];
  let secret: string | null = null;
  for (const [key, value] of headers) {
    if (key.toLowerCase() === HEADER) secret = value;
    else kept.push([key, value]);
  }
  const method = passing.method.toUpperCase();
  if (method === "POST" && passing.path === "/api/me/device-unlock" && passing.status === 200 && secret) {
    store.take(secret);
  } else if (FORGETS.some((one) => one.method === method && one.path === passing.path && one.status === passing.status)) {
    store.forget();
  }
  return kept;
}

export type Opened = "opened" | "nothing-held" | "no-sensor" | "cancelled" | "refused" | "failed";

/**
 * The launch: if a secret is held and this computer can prompt, ask for Touch
 * ID, and on success sign in with the secret. Anything else leaves the
 * password screen to do its job -- a cancelled or failed prompt most of all,
 * with nothing counted. A secret the backend refuses is forgotten, so the
 * next launch does not ask again for nothing.
 */
export async function openWithPrompt(options: {
  store: UnlockStore;
  canPrompt: () => boolean;
  prompt: () => Promise<void>;
  signIn: (secret: string) => Promise<number>;
  log?: (line: string) => void;
}): Promise<Opened> {
  const { store, log = () => undefined } = options;
  if (!store.held()) return "nothing-held";
  if (!options.canPrompt()) return "no-sensor";
  try {
    await options.prompt();
  } catch (problem) {
    log(`unlock: the prompt did not pass: ${String(problem)}`);
    return "cancelled";
  }
  const secret = store.read();
  if (secret === null) {
    store.forget();
    return "nothing-held";
  }
  let status: number;
  try {
    status = await options.signIn(secret);
  } catch (problem) {
    log(`unlock: the sign-in did not go through: ${String(problem)}`);
    return "failed";
  }
  if (status === 200) return "opened";
  if (status === 401) {
    log("unlock: the secret was refused, so it is forgotten");
    store.forget();
    return "refused";
  }
  log(`unlock: the sign-in answered ${status}`);
  return "failed";
}
