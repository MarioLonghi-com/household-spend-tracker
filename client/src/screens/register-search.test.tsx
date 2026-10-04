// @vitest-environment jsdom

/**
 * The register's search, as requests rather than as text in a box.
 *
 * The raw input used to be the query key, so typing "tesco" asked the server
 * for the whole filtered register five times -- up to 25,000 rows each -- and
 * none of the four superseded answers was cancelled. These count the requests
 * that actually went out and check the one that was overtaken was aborted.
 * Issue #107.
 *
 * `lib/api` is mocked, and every register request stays in flight until its
 * signal aborts it, which is exactly what a slow 25k-row answer looks like.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render } from "@testing-library/react";

vi.mock("../lib/api", () => ({
  api: {
    get: vi.fn(),
    post: vi.fn(),
    patch: vi.fn(),
    put: vi.fn(),
    del: vi.fn(),
    upload: vi.fn(),
  },
  ApiError: class ApiError extends Error {},
}));

import { api } from "../lib/api";
import { DEBOUNCE_MS } from "../lib/useDebounced";
import { Register } from "./Register";
import type { Household } from "../lib/types";

afterEach(cleanup);

const HOUSEHOLD = {
  id: "house-1",
  name: "Ours",
  base_currency: "EUR",
  date_format: "YYYY-MM-DD",
  note: null,
  theme: "default",
  accent: null,
  receipts_keep_original: false,
  colours: null,
} as unknown as Household;

type Sent = { search: string | null; signal: AbortSignal | undefined };
let sent: Sent[] = [];

beforeEach(() => {
  // The register remembers its filters (#143); each test starts from none.
  window.localStorage.clear();
  sent = [];
  vi.useFakeTimers({ shouldAdvanceTime: true });
  vi.mocked(api.get).mockReset();
  vi.mocked(api.get).mockImplementation(((path: string, options?: { signal?: AbortSignal }) => {
    // The badge's count beside Needs a category (#188) asks for one row;
    // it is not the register's request.
    if (path.includes("/transactions?") && !path.includes("limit=1")) {
      const search = new URLSearchParams(path.split("?")[1]).get("search");
      sent.push({ search, signal: options?.signal });
      return new Promise((_resolve, reject) => {
        options?.signal?.addEventListener("abort", () =>
          reject(new DOMException("aborted", "AbortError")),
        );
      });
    }
    if (path.endsWith("/reports/currencies")) return Promise.resolve({ currencies: ["EUR"] });
    return Promise.resolve([]);
  }) as typeof api.get);
});

afterEach(() => {
  vi.useRealTimers();
});

function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const view = render(
    <QueryClientProvider client={client}>
      <Register household={HOUSEHOLD} />
    </QueryClientProvider>,
  );
  const box = view.container.querySelector<HTMLInputElement>(
    'input[placeholder^="payee, memo"]',
  )!;
  return { box };
}

async function type(box: HTMLInputElement, text: string) {
  for (let end = 1; end <= text.length; end += 1) {
    fireEvent.change(box, { target: { value: text.slice(0, end) } });
    // Typing speed: well inside the pause.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(60);
    });
  }
}

async function pause() {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(DEBOUNCE_MS + 50);
  });
}

describe("the register search", () => {
  it("sends one request for a word typed at speed, not one per keystroke", async () => {
    const { box } = mount();
    await pause();
    const before = sent.length;

    await type(box, "tesco");
    await pause();

    const searches = sent.slice(before).map((one) => one.search);
    expect(searches).toEqual(["tesco"]);
  });

  it("aborts the request a later search overtook", async () => {
    const { box } = mount();
    await pause();

    await type(box, "tes");
    await pause();
    const overtaken = sent.find((one) => one.search === "tes")!;
    expect(overtaken.signal).toBeDefined();
    expect(overtaken.signal!.aborted).toBe(false);

    await type(box, "tesco");
    await pause();

    expect(sent.some((one) => one.search === "tesco")).toBe(true);
    expect(overtaken.signal!.aborted).toBe(true);
  });
});
