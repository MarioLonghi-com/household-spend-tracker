/**
 * The two things `api.ts` owes every screen besides the request itself.
 *
 * - A 401 from an upload ends the session the same way a 401 from anything
 *   else does. Before #106 only `request()` called the handler, so a receipt
 *   sent after the session expired left the previous user's screen standing.
 * - The caller's abort signal reaches `fetch`. Without it React Query could
 *   cancel a superseded register query and the browser would still download
 *   and parse every row of it (#107).
 *
 * `fetch` itself is the mock here: what matters is what `api.ts` hands it and
 * what it does with the answer.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, api, setUnauthorizedHandler } from "./api";

function answer(status: number, body: unknown): Response {
  return new Response(body === null ? null : JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

const fetchMock = vi.fn<typeof fetch>();

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  setUnauthorizedHandler(null);
  vi.unstubAllGlobals();
});

describe("a 401 that refused a proof", () => {
  it("leaves the session alone: a mistyped password in a step-up form is not a sign-out", async () => {
    const ended = vi.fn();
    setUnauthorizedHandler(ended);
    fetchMock.mockResolvedValue(
      new Response(JSON.stringify({ detail: "that password or code is not right" }), {
        status: 401,
        headers: { "Content-Type": "application/json", "X-Refused": "proof" },
      }),
    );
    await expect(api.post("/me/step-up", { password: "x", code: "1" })).rejects.toMatchObject({
      status: 401,
      message: "that password or code is not right",
    });
    expect(ended).not.toHaveBeenCalled();
  });
});

describe("a refusal that carries a fact beside its sentence", () => {
  it("hands the body to the caller: the sign-in screen acts on key_replaced (#287)", async () => {
    fetchMock.mockResolvedValue(
      answer(401, { detail: "this server's secret key has been replaced", key_replaced: true }),
    );
    await expect(api.post("/session/code", { code: "123456" })).rejects.toMatchObject({
      status: 401,
      message: "this server's secret key has been replaced",
      body: { key_replaced: true },
    });
  });
});

describe("a 401", () => {
  it("from an upload ends the session", async () => {
    const ended = vi.fn();
    setUnauthorizedHandler(ended);
    fetchMock.mockResolvedValue(answer(401, { detail: "not signed in" }));

    const sent = api.upload("/households/h/receipts", new FormData());

    await expect(sent).rejects.toMatchObject({ status: 401 });
    await expect(sent).rejects.toBeInstanceOf(ApiError);
    expect(ended).toHaveBeenCalledTimes(1);
  });

  it("from a JSON request ends the session too", async () => {
    const ended = vi.fn();
    setUnauthorizedHandler(ended);
    fetchMock.mockResolvedValue(answer(401, { detail: "not signed in" }));

    await expect(api.get("/households")).rejects.toMatchObject({ status: 401 });
    expect(ended).toHaveBeenCalledTimes(1);
  });

  it("is the only refusal that does", async () => {
    const ended = vi.fn();
    setUnauthorizedHandler(ended);
    fetchMock.mockResolvedValue(answer(403, { detail: "no" }));

    await expect(api.upload("/x", new FormData())).rejects.toMatchObject({ status: 403 });
    expect(ended).not.toHaveBeenCalled();
  });
});

describe("the abort signal", () => {
  it("is handed to fetch, and aborting it aborts the request", async () => {
    const controller = new AbortController();
    fetchMock.mockImplementation(
      (_url, init) =>
        new Promise<Response>((_resolve, reject) => {
          init?.signal?.addEventListener("abort", () =>
            reject(new DOMException("aborted", "AbortError")),
          );
        }),
    );

    const pending = api.get("/households/h/transactions", { signal: controller.signal });
    expect(fetchMock.mock.calls[0][1]?.signal).toBe(controller.signal);

    controller.abort();
    await expect(pending).rejects.toMatchObject({ name: "AbortError" });
  });
});

describe("an answer that is not JSON (#196)", () => {
  function html(status: number, statusText: string): Response {
    return new Response("<html><body>Bad gateway</body></html>", {
      status,
      statusText,
      headers: { "Content-Type": "text/html" },
    });
  }

  it("still ends the session on a 401, and says the status rather than the body", async () => {
    const ended = vi.fn();
    setUnauthorizedHandler(ended);
    fetchMock.mockResolvedValue(html(401, "Unauthorized"));

    const sent = api.get("/households");
    await expect(sent).rejects.toBeInstanceOf(ApiError);
    await expect(sent).rejects.toMatchObject({ status: 401, message: "Unauthorized" });
    expect(ended).toHaveBeenCalledTimes(1);
  });

  it("does the same for an upload", async () => {
    const ended = vi.fn();
    setUnauthorizedHandler(ended);
    fetchMock.mockResolvedValue(html(401, "Unauthorized"));

    await expect(api.upload("/households/h/receipts", new FormData())).rejects.toMatchObject({
      status: 401,
      message: "Unauthorized",
    });
    expect(ended).toHaveBeenCalledTimes(1);
  });

  it("names the status when there is no status text either", async () => {
    fetchMock.mockImplementation(async () => html(502, ""));

    await expect(api.get("/households")).rejects.toMatchObject({
      status: 502,
      message: "the server answered 502",
    });
    await expect(api.upload("/x", new FormData())).rejects.toMatchObject({
      status: 502,
      message: "the server answered 502",
    });
  });

  it("refuses a 200 it cannot read rather than handing back nothing", async () => {
    fetchMock.mockResolvedValue(new Response("<html></html>", { status: 200 }));

    await expect(api.get("/households")).rejects.toMatchObject({
      status: 200,
      message: "the server's answer could not be read",
    });
  });
});
