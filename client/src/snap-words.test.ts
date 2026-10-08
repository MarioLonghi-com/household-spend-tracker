// @vitest-environment jsdom

/**
 * `/snap`'s words (#56): a small dictionary in snap.js, keyed by language.
 *
 * In English the page is what it was -- the markup keeps its own English, the
 * dictionary says the same thing word for word, and nothing is rewritten. In
 * the en-XA pseudo-locale, chosen in the app on this device, every word the
 * page shows is the dictionary's, accented.
 */

import { beforeAll, describe, expect, it, vi } from "vitest";
import html from "../../app/static/snap/index.html?raw";

const SCRIPT = "../../app/static/snap/snap.js";

type Snap = {
  WORDS: Record<string, Record<string, string | Record<string, string>>>;
  chooseLanguage: (stored: string | null, browser: readonly string[]) => string;
  say: (key: string, values?: Record<string, unknown>, language?: string) => string;
};

/** The markup's own text for each `data-say`, whitespace as a browser shows it. */
function marked(): Map<string, string> {
  const page = new DOMParser().parseFromString(html, "text/html");
  return new Map(
    Array.from(page.querySelectorAll<HTMLElement>("[data-say]")).map((one) => [
      one.dataset.say!,
      one.textContent!.replace(/\s+/g, " ").trim(),
    ]),
  );
}

let snap: Snap;

beforeAll(async () => {
  window.localStorage.setItem("spendtracker.locale", "en-XA");
  document.body.innerHTML = html.slice(html.indexOf("<body"), html.lastIndexOf("</body>"));
  vi.stubGlobal("indexedDB", {
    open: () => {
      const req: { result?: unknown; onsuccess?: () => void } = {};
      req.result = {
        transaction: () => {
          const tx: { oncomplete?: () => void; objectStore: () => unknown } = {
            objectStore: () => ({ getAll: () => ({ result: [] }), put: () => ({}), delete: () => ({}) }),
          };
          setTimeout(() => tx.oncomplete?.(), 0);
          return tx;
        },
      };
      setTimeout(() => req.onsuccess?.(), 0);
      return req;
    },
  });
  vi.stubGlobal("fetch", (url: string) => {
    const json = (body: unknown) => Promise.resolve(new Response(JSON.stringify(body), { status: 200 }));
    if (url === "/api/households") return json([{ id: "house-b", name: "Ours", colours: null }]);
    if (url === "/api/me") return json({ id: "user-b", display_name: "Bea" });
    return json([]);
  });
  snap = (await import(/* @vite-ignore */ new URL(SCRIPT, import.meta.url).href)) as Snap;
});

describe("the dictionary", () => {
  it("says in English exactly what the markup says, for every word the markup carries", () => {
    for (const [key, text] of marked()) expect(snap.say(key, {}, "en"), key).toBe(text);
    expect(snap.say("title", {}, "en")).toBe(html.match(/<title>([^<]+)<\/title>/)![1]);
  });

  it("has the same words in every language it has", () => {
    const keys = Object.keys(snap.WORDS.en).sort();
    for (const words of Object.values(snap.WORDS)) expect(Object.keys(words).sort()).toEqual(keys);
  });

  it("fills in names and counts, and keeps the English it had", () => {
    expect(snap.say("sent", { count: 3 }, "en")).toBe("✓ 3 sent to the inbox");
    expect(snap.say("sent", { count: 1 }, "en")).toBe("✓ 1 sent to the inbox");
    expect(snap.say("noteLabel", { name: "a.jpg" }, "en")).toBe("Receipt notes for a.jpg");
    expect(snap.say("serverSaid", { status: 502 }, "en")).toBe("the server said 502");
  });

  it("speaks the app's choice on this device, then the browser's, and English otherwise", () => {
    expect(snap.chooseLanguage(null, ["sv-SE", "pt-BR"])).toBe("en");
    expect(snap.chooseLanguage(null, ["en-GB"])).toBe("en");
    expect(snap.chooseLanguage("en-XA", ["en-US"])).toBe("en-XA");
    expect(snap.chooseLanguage(null, [])).toBe("en");
  });

  it("accents every letter in en-XA and leaves the names it fills in alone", () => {
    for (const key of Object.keys(snap.WORDS.en)) {
      const text = snap.say(key, { count: 2, name: "Zzz", household: "Zzz", status: 500 }, "en-XA");
      const words = text.replace(/Zzz|Spend Tracker/g, "").match(/[A-Za-z]{2,}/g) ?? [];
      expect(words, key).toEqual([]);
    }
    expect(snap.say("noteLabel", { name: "a.jpg" }, "en-XA")).toMatch(/a\.jpg$/);
  });
});

describe("the page in en-XA", () => {
  it("says every word in the markup in the pseudo-locale, and marks its language", async () => {
    expect(document.documentElement.lang).toBe("en-XA");
    for (const [key] of marked()) {
      const shown = document.querySelector<HTMLElement>(`[data-say="${key}"]`)!.textContent!;
      expect(shown.match(/[A-Za-z]{2,}/g) ?? [], key).toEqual([]);
    }
    await vi.waitFor(() => expect(document.title).toContain("Ours"));
    expect(document.title).toMatch(/^Spend Tracker - /);
    expect(document.title.replace(/Ours|Spend Tracker/g, "").match(/[A-Za-z]{2,}/g) ?? []).toEqual([]);
  });
});
