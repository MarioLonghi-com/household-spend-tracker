// @vitest-environment jsdom

/**
 * `/snap` letting go of the photos it has finished with.
 *
 * `snap.js` is a plain script the server hands the phone, not part of this
 * bundle, but it runs in a browser like everything here, so it is tested here.
 * An object URL pins its blob in memory until it is revoked, and a phone at a
 * till does not reload the page between receipts. Two leaks (#108):
 *
 * - the landed list kept every receipt of the session, each holding its
 *   photo's bytes; it is now capped and the ones that fall off are revoked;
 * - the queue's thumbnail was revoked only on `onload`, so a photo the browser
 *   could not decode held its bytes for good, once per re-render.
 *
 * The page's own HTML is loaded first, because the script finds its elements
 * at import time. `fetch` refuses, which is the "no connection" path: the
 * script settles without touching the network or IndexedDB.
 */

import { beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import html from "../../app/static/snap/index.html?raw";

// Through a variable, so the type checker leaves a plain JavaScript file alone
// and the bundler does not try to pull it into the app.
const SCRIPT = "../../app/static/snap/snap.js";

type Snap = {
  LANDED_KEPT: number;
  land: (entry: Record<string, unknown>) => void;
  showOnce: (img: HTMLImageElement, blob: Blob) => void;
};
let snap: Snap;

const created: string[] = [];
const revoked: string[] = [];

beforeAll(async () => {
  document.body.innerHTML = html.slice(html.indexOf("<body"), html.lastIndexOf("</body>"));
  vi.stubGlobal("fetch", () => Promise.reject(new TypeError("offline")));
  let serial = 0;
  URL.createObjectURL = () => {
    const url = `blob:test/${(serial += 1)}`;
    created.push(url);
    return url;
  };
  URL.revokeObjectURL = (url: string) => {
    revoked.push(url);
  };
  snap = (await import(/* @vite-ignore */ new URL(SCRIPT, import.meta.url).href)) as Snap;
});

beforeEach(() => {
  created.length = 0;
  revoked.length = 0;
});

function receipt(n: number) {
  return {
    id: `receipt-${n}`,
    name: `IMG_${n}.jpg`,
    note: "",
    saved: "",
    pending: null,
    state: "in",
    url: URL.createObjectURL(new Blob([String(n)])),
  };
}

describe("the landed list", () => {
  it("keeps the newest few and frees the photos of the ones that fall off", () => {
    const extra = 3;
    const urls: string[] = [];
    for (let n = 1; n <= snap.LANDED_KEPT + extra; n += 1) {
      const one = receipt(n);
      urls.push(one.url);
      snap.land(one);
    }

    const rows = document.querySelectorAll("#landed li");
    expect(rows).toHaveLength(snap.LANDED_KEPT);
    // Newest first, so the oldest three are the ones gone -- and exactly those.
    expect(revoked).toEqual(urls.slice(0, extra));
    expect(rows[0].textContent).toContain(`IMG_${snap.LANDED_KEPT + extra}.jpg`);
  });
});

describe("a queue thumbnail", () => {
  it("is freed once it has loaded", () => {
    const img = document.createElement("img");
    snap.showOnce(img, new Blob(["x"]));
    expect(img.src).toBe(created[0]);
    expect(revoked).toEqual([]);

    img.onload!(new Event("load"));
    expect(revoked).toEqual([created[0]]);
  });

  it("is freed when the browser cannot decode it, too", () => {
    const img = document.createElement("img");
    snap.showOnce(img, new Blob(["not a picture"]));

    (img.onerror as (event: Event) => void)(new Event("error"));
    expect(revoked).toEqual([created[0]]);
  });
});
