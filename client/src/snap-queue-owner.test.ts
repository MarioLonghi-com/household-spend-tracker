// @vitest-environment jsdom

/**
 * `/snap`'s parked queue resumes only the signed-in person's photos (#198).
 *
 * The page boots on import: it asks for the households and `/api/me`, then
 * resumes whatever IndexedDB holds. Two photos are parked here, one taken by
 * somebody else into a household this person is not in, one of their own.
 * What is asserted is where the page actually sent bytes.
 *
 * IndexedDB is a small hand-written stand-in holding those two records: all
 * the page does with it on this path is `open`, `getAll` and `delete`.
 */

import { beforeAll, describe, expect, it, vi } from "vitest";
import html from "../../app/static/snap/index.html?raw";

const SCRIPT = "../../app/static/snap/snap.js";

type Snap = {
  mine: (
    record: { owner?: string | null; household: string },
    who: { id: string } | null,
    theirs: { id: string }[],
  ) => boolean;
};

const PARKED = [
  { id: "1-a", name: "a.jpg", blob: new Blob(["a"]), household: "house-a", owner: "user-a" },
  { id: "2-b", name: "b.jpg", blob: new Blob(["b"]), household: "house-b", owner: "user-b" },
];

function request<T>(result: T) {
  const req: { result: T; onsuccess?: () => void; onerror?: () => void; onupgradeneeded?: () => void } = {
    result,
  };
  return req;
}

function fakeIndexedDb() {
  const rows = new Map(PARKED.map((one) => [one.id, one]));
  const db = {
    transaction: () => {
      const tx: { oncomplete?: () => void; onerror?: () => void; objectStore: () => unknown } = {
        objectStore: () => ({
          getAll: () => request([...rows.values()]),
          put: (record: (typeof PARKED)[number]) => request(rows.set(record.id, record)),
          delete: (id: string) => request(rows.delete(id)),
        }),
      };
      setTimeout(() => tx.oncomplete?.(), 0);
      return tx;
    },
  };
  return {
    open: () => {
      const req = request(db);
      setTimeout(() => req.onsuccess?.(), 0);
      return req;
    },
  };
}

const posted: string[] = [];
let snap: Snap;

beforeAll(async () => {
  document.body.innerHTML = html.slice(html.indexOf("<body"), html.lastIndexOf("</body>"));
  URL.createObjectURL = () => "blob:test";
  URL.revokeObjectURL = () => {};
  vi.stubGlobal("indexedDB", fakeIndexedDb());
  vi.stubGlobal("fetch", (url: string, init?: RequestInit) => {
    const json = (body: unknown, status = 200) =>
      Promise.resolve(new Response(JSON.stringify(body), { status }));
    if (url === "/api/households") return json([{ id: "house-b", name: "Ours", colours: null }]);
    if (url === "/api/me") return json({ id: "user-b", display_name: "Bea" });
    if (init?.method === "POST" && url.endsWith("/receipts")) {
      posted.push(url);
      return json({ receipt: { id: `r-${posted.length}` } }, 201);
    }
    return json({ detail: "not here" }, 404);
  });
  snap = (await import(/* @vite-ignore */ new URL(SCRIPT, import.meta.url).href)) as Snap;
});

describe("the parked queue", () => {
  it("sends this person's photo and leaves somebody else's alone", async () => {
    await vi.waitFor(() => expect(posted.length).toBeGreaterThan(0), { timeout: 3_000 });
    // Give a second send, had there been one, the chance to happen.
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(posted).toEqual(["/api/households/house-b/receipts"]);
  });

  it("decides by owner, and by membership only for a photo parked before the stamp", () => {
    const theirs = [{ id: "house-b" }];
    const bea = { id: "user-b" };
    expect(snap.mine({ owner: "user-a", household: "house-b" }, bea, theirs)).toBe(false);
    expect(snap.mine({ owner: "user-b", household: "house-b" }, bea, theirs)).toBe(true);
    expect(snap.mine({ owner: "user-b", household: "house-b" }, null, theirs)).toBe(false);
    expect(snap.mine({ household: "house-a" }, bea, theirs)).toBe(false);
    expect(snap.mine({ owner: null, household: "house-b" }, bea, theirs)).toBe(true);
  });
});
