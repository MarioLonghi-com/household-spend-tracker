import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { describe, expect, it, vi } from "vitest";
import { CookieJar } from "../src/cookies";
import { handler } from "../src/scheme";
import { HEADER, observe, openWithPrompt, UnlockStore, type Crypt } from "../src/unlock";

/** `safeStorage` stood in for: reversible, and visibly not the plaintext. */
function crypt(available = true): Crypt {
  return {
    available: () => available,
    encrypt: (text) => Buffer.from(`sealed:${[...text].reverse().join("")}`, "utf8"),
    decrypt: (data) => {
      const text = data.toString("utf8");
      if (!text.startsWith("sealed:")) throw new Error("not sealed by this crypt");
      return [...text.slice("sealed:".length)].reverse().join("");
    },
  };
}

function fresh(available = true): { store: UnlockStore; file: string; said: string[] } {
  const file = path.join(fs.mkdtempSync(path.join(os.tmpdir(), "st-unlock-")), "unlock.secret");
  const said: string[] = [];
  return { store: new UnlockStore(file, crypt(available), (line) => said.push(line)), file, said };
}

describe("the Touch ID secret on disk (#432)", () => {
  it("is kept encrypted, in its own file, mode 0600, and read back", () => {
    const { store, file } = fresh();
    expect(store.held()).toBe(false);
    expect(store.take("s3cret-value")).toBe(true);
    expect(store.held()).toBe(true);
    expect(fs.readFileSync(file, "utf8")).not.toContain("s3cret-value");
    if (process.platform !== "win32") expect(fs.statSync(file).mode & 0o777).toBe(0o600);
    expect(fs.existsSync(`${file}.writing`)).toBe(false);
    expect(store.read()).toBe("s3cret-value");
    store.forget();
    expect(store.held()).toBe(false);
    expect(store.read()).toBeNull();
  });

  it("keeps nothing where the operating system cannot encrypt it", () => {
    const { store, said } = fresh(false);
    expect(store.take("s3cret-value")).toBe(false);
    expect(store.held()).toBe(false);
    expect(said.join("\n")).toContain("cannot encrypt");
  });

  it("a file this crypt did not write reads as nothing", () => {
    const { store, file } = fresh();
    fs.writeFileSync(file, "planted");
    expect(store.held()).toBe(true);
    expect(store.read()).toBeNull();
  });
});

describe("what passes through on the way to the window", () => {
  it("takes the secret off the answer that turned it on, and the page never sees the header", () => {
    const { store } = fresh();
    const shown = observe(store, { method: "POST", path: "/api/me/device-unlock", status: 200 }, [
      ["content-type", "application/json"],
      ["X-Spend-Tracker-Device-Unlock", "issued-by-the-backend"],
    ]);
    expect(shown).toEqual([["content-type", "application/json"]]);
    expect(store.read()).toBe("issued-by-the-backend");
  });

  it("keeps nothing from a refusal, and strips the header from any other answer", () => {
    const { store } = fresh();
    observe(store, { method: "POST", path: "/api/me/device-unlock", status: 401 }, [[HEADER, "never-issued"]]);
    expect(store.held()).toBe(false);
    const shown = observe(store, { method: "GET", path: "/api/me", status: 200 }, [[HEADER, "planted"], ["x-other", "1"]]);
    expect(shown).toEqual([["x-other", "1"]]);
    expect(store.held()).toBe(false);
  });

  it.each([
    ["DELETE", "/api/me/device-unlock", 204],
    ["POST", "/api/me/password", 200],
    ["POST", "/api/session/recover", 200],
  ])("forgets the secret when %s %s answers %i", (method, path, status) => {
    const { store } = fresh();
    store.take("standing");
    observe(store, { method, path, status }, []);
    expect(store.held()).toBe(false);
  });

  it("keeps it when one of those is refused", () => {
    const { store } = fresh();
    store.take("standing");
    observe(store, { method: "POST", path: "/api/me/password", status: 401 }, []);
    observe(store, { method: "DELETE", path: "/api/me/device-unlock", status: 401 }, []);
    expect(store.read()).toBe("standing");
  });

  it("runs inside the scheme handler, before the cookie jar and the page", async () => {
    const { store } = fresh();
    const jar = new CookieJar();
    const backend = {
      send: vi.fn(async () => ({
        head: { status: 200, headers: [["set-cookie", "st_session=abc"], [HEADER, "from-the-handler"], ["content-type", "application/json"]] },
        body: Buffer.from('{"enabled":true}'),
      })),
    };
    const serve = handler(async () => backend as never, jar, (passing, headers) => observe(store, passing, headers));
    const answer = await serve(new Request("app://localhost/api/me/device-unlock", { method: "POST", body: "{}" }));
    expect(answer.status).toBe(200);
    expect(answer.headers.get(HEADER)).toBeNull();
    expect(answer.headers.get("content-type")).toBe("application/json");
    expect(jar.header()).toBe("st_session=abc");
    expect(store.read()).toBe("from-the-handler");
  });
});

describe("the launch", () => {
  function launch(store: UnlockStore, overrides: Partial<Parameters<typeof openWithPrompt>[0]> = {}) {
    const prompt = vi.fn(async () => undefined);
    const signIn = vi.fn(async () => 200);
    const said: string[] = [];
    const run = () => openWithPrompt({ store, canPrompt: () => true, prompt, signIn, log: (line) => said.push(line), ...overrides });
    return { prompt, signIn, said, run };
  }

  it("prompts, then signs in with the secret, when one is held and there is a sensor", async () => {
    const { store } = fresh();
    store.take("standing");
    const { prompt, signIn, run } = launch(store);
    expect(await run()).toBe("opened");
    expect(prompt).toHaveBeenCalledTimes(1);
    expect(signIn).toHaveBeenCalledWith("standing");
    expect(store.held()).toBe(true);
  });

  it("never prompts with nothing held, nor without a sensor", async () => {
    const { store } = fresh();
    const none = launch(store);
    expect(await none.run()).toBe("nothing-held");
    store.take("standing");
    const blind = launch(store, { canPrompt: () => false });
    expect(await blind.run()).toBe("no-sensor");
    expect(none.prompt).not.toHaveBeenCalled();
    expect(blind.prompt).not.toHaveBeenCalled();
    expect(blind.signIn).not.toHaveBeenCalled();
    expect(store.held()).toBe(true);
  });

  it("a cancelled prompt sends nothing and keeps the secret: the password screen, no counter", async () => {
    const { store } = fresh();
    store.take("standing");
    const { signIn, run } = launch(store, { prompt: async () => { throw new Error("User canceled"); } });
    expect(await run()).toBe("cancelled");
    expect(signIn).not.toHaveBeenCalled();
    expect(store.read()).toBe("standing");
  });

  it("a secret the backend refuses is forgotten, so the next launch does not ask", async () => {
    const { store } = fresh();
    store.take("revoked-elsewhere");
    const { run, said } = launch(store, { signIn: async () => 401 });
    expect(await run()).toBe("refused");
    expect(store.held()).toBe(false);
    expect(said.join("\n")).toContain("forgotten");
  });

  it("a backend that is not there leaves the secret for next time", async () => {
    const { store } = fresh();
    store.take("standing");
    const { run } = launch(store, { signIn: async () => 0 });
    expect(await run()).toBe("failed");
    expect(store.read()).toBe("standing");
  });
});
