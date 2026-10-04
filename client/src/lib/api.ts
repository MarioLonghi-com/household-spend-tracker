/**
 * The one place that talks to the server.
 *
 * Every amount crossing this boundary is an integer of minor units; parsing and
 * formatting live in money.ts and nowhere else.
 */

export class ApiError extends Error {
  status: number;
  retryAfter?: number;
  /**
   * The answer's JSON, for a refusal that carries a fact beside its sentence
   * -- `key_replaced` at the sign-in code step (#287). The sentence is still
   * what a person reads.
   */
  body?: Record<string, unknown>;

  constructor(
    message: string,
    status: number,
    retryAfter?: number,
    body?: Record<string, unknown>,
  ) {
    super(message);
    this.status = status;
    this.retryAfter = retryAfter;
    this.body = body;
  }
}

/**
 * What to do when the server stops recognising us.
 *
 * A session can end mid-use -- it expires, or an owner disables the account,
 * which revokes every session. Without this the next query just fails and the
 * shell renders its empty state, telling someone their ledger is gone. One
 * handler here covers every screen.
 */
let unauthorized: (() => void) | null = null;

export function setUnauthorizedHandler(handler: (() => void) | null): void {
  unauthorized = handler;
}

/**
 * What a caller may add to a request.
 *
 * `signal` is React Query's: a query whose key has moved on (the register's
 * search changed, a filter was cleared) aborts it, and the browser stops
 * downloading and parsing a response nobody will read. Without it a
 * superseded 25k-row register is fetched and parsed in full anyway.
 */
export type RequestOptions = { signal?: AbortSignal };

/**
 * A 401 is the server saying the session is gone -- for uploads as much as for
 * JSON -- unless it says it refused a proof instead (`X-Refused: proof`): a
 * mistyped password or code in a step-up form comes from somebody still signed
 * in, and ending their session for it threw them back to the sign-in page.
 */
function refused(response: Response): void {
  if (response.status !== 401) return;
  if (response.headers.get("x-refused") === "proof") return;
  unauthorized?.();
}

/**
 * Read an answer, whatever it turned out to be.
 *
 * Not every answer is this app's JSON: `tailscale serve` sends an HTML 502
 * while the backend restarts, and a proxy may send an HTML 401. Parsing first
 * threw a SyntaxError before `refused()` ran -- so the session was never ended
 * -- and the screen showed `Unexpected token '<'` (#196). So the status is
 * acted on first, and a body that is not JSON is treated as no body.
 */
async function read<T>(response: Response): Promise<T> {
  refused(response);
  const text = await response.text();
  let payload: unknown = null;
  let parsed = true;
  if (text) {
    try {
      payload = JSON.parse(text);
    } catch {
      parsed = false;
    }
  }
  if (!response.ok) {
    const detail =
      payload && typeof (payload as { detail?: unknown }).detail === "string"
        ? (payload as { detail: string }).detail
        : response.statusText || `the server answered ${response.status}`;
    const retryAfter = Number(response.headers.get("retry-after")) || undefined;
    const body =
      payload && typeof payload === "object" && !Array.isArray(payload)
        ? (payload as Record<string, unknown>)
        : undefined;
    throw new ApiError(detail, response.status, retryAfter, body);
  }
  if (!parsed) {
    throw new ApiError("the server's answer could not be read", response.status);
  }
  return payload as T;
}

async function request<T>(
  method: string,
  path: string,
  body?: unknown,
  extra: RequestOptions = {},
): Promise<T> {
  const options: RequestInit = {
    method,
    headers: {},
    // Same-origin in dev (through the Vite proxy) and in production, so the
    // session cookie rides along and the server's Origin check passes.
    credentials: "same-origin",
  };
  if (extra.signal) options.signal = extra.signal;
  if (body !== undefined) {
    options.headers = { "Content-Type": "application/json" };
    options.body = JSON.stringify(body);
  }

  const response = await fetch(`/api${path}`, options);
  if (response.status === 204) return null as T;
  return read<T>(response);
}

async function upload<T>(path: string, form: FormData): Promise<T> {
  const response = await fetch(`/api${path}`, {
    method: "POST",
    body: form,
    credentials: "same-origin",
  });
  // Before this went through `refused`, a receipt uploaded after the session
  // ended showed an error and left the previous user's screen standing.
  return read<T>(response);
}

export const api = {
  get: <T,>(path: string, options?: RequestOptions) =>
    request<T>("GET", path, undefined, options),
  post: <T,>(path: string, body?: unknown) => request<T>("POST", path, body ?? {}),
  patch: <T,>(path: string, body: unknown) => request<T>("PATCH", path, body),
  // PUT rather than PATCH where the body *is* the whole setting: a payee's
  // categorisation is one answer, not a set of fields to merge.
  put: <T,>(path: string, body: unknown) => request<T>("PUT", path, body),
  del: <T,>(path: string) => request<T>("DELETE", path),
  upload,
};
