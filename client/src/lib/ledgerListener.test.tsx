// @vitest-environment jsdom

/**
 * The listener that refreshes the page on a change made elsewhere (#369).
 *
 * Driven with fake timers and a mocked server. The assertions are on what the
 * page shows -- a value from before and the value after somebody else's change
 * -- and on what was asked of the server, never on a mechanism having fired.
 */

import { QueryClient, QueryClientProvider, useMutation, useQuery } from "@tanstack/react-query";
import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("./api", () => ({
  api: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), put: vi.fn(), del: vi.fn(), upload: vi.fn() },
  ApiError: class ApiError extends Error {},
}));

import { api } from "./api";
import { BASE_MS, MAX_MS, nextDelay, shouldRefresh, useLedgerListener } from "./ledgerListener";

const HOME = "h-home";
const AWAY = "h-away";

let visibility: DocumentVisibilityState = "visible";
beforeEach(() => {
  vi.useFakeTimers();
  visibility = "visible";
  Object.defineProperty(document, "visibilityState", { configurable: true, get: () => visibility });
});
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  vi.useRealTimers();
});

/** What the server holds: the latest seq per household, and one row's payee. */
function server() {
  const state = {
    seq: { [HOME]: 10, [AWAY]: 50 } as Record<string, number>,
    by: "nobody" as "nobody" | "elsewhere" | "mine",
    payee: "Mercadona",
  };
  vi.mocked(api.get).mockImplementation(async (path: string) => {
    const latest = path.match(/households\/([^/]+)\/changes\/latest(?:\?since=(\d+))?/);
    if (latest) {
      const seq = state.seq[latest[1]];
      const since = latest[2] === undefined ? null : Number(latest[2]);
      const moved = since !== null && seq > since;
      return { seq, elsewhere: moved && state.by === "elsewhere", mine: moved && state.by === "mine" };
    }
    if (path.endsWith("/transactions/t1")) return { payee_name: state.payee };
    throw new Error(`unexpected ${path}`);
  });
  vi.mocked(api.patch).mockImplementation(async () => {
    state.seq[HOME] += 1;
    state.by = "mine";
    return {};
  });
  return state;
}

function Page({ household }: { household: string }) {
  useLedgerListener(household);
  const row = useQuery({
    queryKey: ["row", household, "t1"],
    queryFn: () => api.get<{ payee_name: string }>(`/households/${household}/transactions/t1`),
  });
  const save = useMutation({ mutationFn: () => api.patch("/transactions/t1", {}) });
  return (
    <>
      <p data-testid="payee">{row.data?.payee_name ?? "…"}</p>
      <button onClick={() => save.mutate()}>save</button>
    </>
  );
}

function mount(household = HOME) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchOnWindowFocus: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <Page household={household} />
    </QueryClientProvider>,
  );
}

const polls = () =>
  vi.mocked(api.get).mock.calls.filter(([path]) => String(path).includes("/changes/latest"));
const shown = () => screen.getByTestId("payee").textContent;

async function tick(ms: number) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

describe("the decisions, on their own", () => {
  it("backs off when idle and resets on a change", () => {
    expect(nextDelay(BASE_MS, false)).toBe(30_000);
    expect(nextDelay(100_000, false)).toBe(MAX_MS);
    expect(nextDelay(MAX_MS, true)).toBe(BASE_MS);
  });

  it("refreshes for somebody else, or for this person's other tab, never for nothing", () => {
    expect(shouldRefresh({ seq: 11, elsewhere: true, mine: false }, 10, true)).toBe(true);
    expect(shouldRefresh({ seq: 11, elsewhere: false, mine: true }, 10, false)).toBe(true);
    expect(shouldRefresh({ seq: 11, elsewhere: false, mine: true }, 10, true)).toBe(false);
    expect(shouldRefresh({ seq: 10, elsewhere: true, mine: true }, 10, false)).toBe(false);
  });
});

describe("an open page", () => {
  it("shows another member's change within one interval", async () => {
    const state = server();
    mount();
    await tick(0);
    expect(shown()).toBe("Mercadona");

    state.seq[HOME] = 11;
    state.by = "elsewhere";
    state.payee = "Mercadona Madrid";
    await tick(BASE_MS);

    expect(shown()).toBe("Mercadona Madrid");
  });

  it("does not refetch for its own save, or for a change in another household", async () => {
    const state = server();
    mount();
    await tick(0);
    const reads = () =>
      vi.mocked(api.get).mock.calls.filter(([path]) => String(path).endsWith("/t1")).length;
    const before = reads();

    // This tab saves: the seq moves, by this person.
    await act(async () => {
      screen.getByRole("button", { name: "save" }).click();
      await vi.advanceTimersByTimeAsync(0);
    });
    // Another household moves too.
    state.seq[AWAY] = 99;
    await tick(BASE_MS);

    expect(reads()).toBe(before);
    expect(polls().every(([path]) => String(path).includes(HOME))).toBe(true);
  });

  it("stops asking while the tab is hidden, and asks again on return", async () => {
    const state = server();
    mount();
    await tick(0);
    const asked = polls().length;

    visibility = "hidden";
    document.dispatchEvent(new Event("visibilitychange"));
    state.seq[HOME] = 12;
    state.by = "elsewhere";
    state.payee = "Lidl";
    await tick(MAX_MS * 3);
    expect(polls().length).toBe(asked);
    expect(shown()).toBe("Mercadona");

    visibility = "visible";
    document.dispatchEvent(new Event("visibilitychange"));
    await tick(0);
    expect(polls().length).toBe(asked + 1);
    expect(shown()).toBe("Lidl");
  });

  it("asks less often while nothing changes", async () => {
    server();
    mount();
    await tick(0);
    const first = polls().length;
    await tick(BASE_MS);
    await tick(30_000);
    await tick(45_000);
    expect(polls().length).toBe(first + 3);
    // The next is 67.5 s away, so nothing more at 60 s.
    await tick(60_000);
    expect(polls().length).toBe(first + 3);
  });
});
