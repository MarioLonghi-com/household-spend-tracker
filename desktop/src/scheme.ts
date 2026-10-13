/**
 * `app://localhost`: every request the window makes, forwarded to the backend.
 *
 * Every path, the SPA's own files included, so FastAPI's headers, CSP and
 * content types apply exactly as on a server, and the shell needs no copy of
 * any of them (#49). Nothing listens on a port.
 *
 * Two things the shell adds, both found in the spike (#59):
 *
 * - **`Sec-Fetch-Site: same-origin`.** Electron gives `protocol.handle` no
 *   `Origin` and no `Sec-Fetch-*`, so the server's CSRF check refused every
 *   write. Only this app's own pages can make an `app://` request -- the
 *   window refuses to navigate anywhere else (`main.ts`) -- so the shell says
 *   so, and only when the request did not say anything itself.
 * - **The cookie jar** (`cookies.ts`): Chromium keeps no cookie from these
 *   responses.
 *
 * A request that arrives while the backend is being restarted (a restore)
 * waits for the new one.
 */

import type { Backend } from "./backend";
import type { CookieJar } from "./cookies";

export const ORIGIN = "app://localhost";

/** Statuses whose response has no body, which `Response` refuses to be given one. */
const EMPTY = new Set([101, 204, 205, 304]);

export function forwardedHeaders(request: Request, jar: CookieJar): [string, string][] {
  const headers: [string, string][] = [];
  request.headers.forEach((value, key) => {
    if (key !== "host" && key !== "cookie") headers.push([key, value]);
  });
  const cookie = jar.header();
  if (cookie) headers.push(["cookie", cookie]);
  headers.push(["host", "localhost"]);
  if (!headers.some(([key]) => key === "origin" || key === "sec-fetch-site")) {
    headers.push(["sec-fetch-site", "same-origin"]);
  }
  return headers;
}

export function handler(current: () => Promise<Backend>, jar: CookieJar) {
  return async (request: Request): Promise<Response> => {
    const url = new URL(request.url);
    if (url.host !== "localhost") return new Response(null, { status: 404 });
    const body = request.body ? Buffer.from(await request.arrayBuffer()) : Buffer.alloc(0);
    const backend = await current();
    const answer = await backend.send(
      { method: request.method, path: url.pathname, query: url.search.slice(1), headers: forwardedHeaders(request, jar) },
      body,
    );
    if (answer === null) {
      return new Response("The ledger is not running.", { status: 503, headers: { "content-type": "text/plain; charset=utf-8" } });
    }
    const out = new Headers();
    const setCookies: string[] = [];
    for (const [key, value] of answer.head.headers as [string, string][]) {
      if (key.toLowerCase() === "set-cookie") setCookies.push(value);
      else out.append(key, value);
    }
    jar.take(setCookies);
    const status = Number(answer.head.status);
    const empty = EMPTY.has(status) || request.method === "HEAD";
    return new Response(empty ? null : new Uint8Array(answer.body), { status, headers: out });
  };
}
