// @vitest-environment jsdom

/**
 * The query cache belongs to whoever filled it.
 *
 * `["households"]` is the same key for every user, so what the cache holds is
 * the last signed-in person's ledger. These assert that it is *gone* -- not
 * that a handler ran -- at the two moments somebody else can reach it: a
 * session ending underneath the page (a 401), and a different person signing
 * in at the same tab. Issue #106.
 *
 * The server is mocked at `lib/api`. Every request the shell makes after
 * sign-in is left pending, so the only data in the cache is what the test put
 * there, and nothing can refill it behind the assertion's back.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

const handler: { current: (() => void) | null } = { current: null };

vi.mock("./lib/api", () => ({
  api: {
    get: vi.fn(),
    post: vi.fn(),
    patch: vi.fn(),
    put: vi.fn(),
    del: vi.fn(),
    upload: vi.fn(),
  },
  ApiError: class ApiError extends Error {},
  setUnauthorizedHandler: (next: (() => void) | null) => {
    handler.current = next;
  },
}));

import { api } from "./lib/api";
import { App } from "./App";
import type { User } from "./lib/types";

afterEach(cleanup);

const ALICE: User = {
  id: "user-a",
  email: "a@example.test",
  display_name: "Alice",
  role: "owner",
  disabled_at: null,
} as User;
const BOB: User = { ...ALICE, id: "user-b", email: "b@example.test", display_name: "Bob" };

const ALICES_HOUSEHOLDS = [{ id: "household-a", name: "Alice's house" }];

function boot(signedIn: User | null) {
  vi.mocked(api.get).mockImplementation((path: string) => {
    if (path === "/health") return Promise.resolve({ setup_required: false });
    if (path === "/me")
      return signedIn ? Promise.resolve(signedIn) : Promise.reject(new Error("401"));
    // Everything the shell asks for stays in flight.
    return new Promise(() => {});
  });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <App />
    </QueryClientProvider>,
  );
  return client;
}

// The whole shell mounts behind these; on a slow machine that is past the
// one-second default.
const WAIT = { timeout: 5_000 };

beforeEach(() => {
  vi.mocked(api.get).mockReset();
  vi.mocked(api.post).mockReset();
  handler.current = null;
});

describe("the query cache across a change of user", () => {
  it("is emptied when the session ends underneath the page", async () => {
    const client = boot(ALICE);
    await waitFor(() => expect(handler.current).not.toBeNull(), WAIT);
    await waitFor(() =>
      expect(client.getQueryCache().find({ queryKey: ["households"] })).toBeDefined(),
    WAIT);
    act(() => client.setQueryData(["households"], ALICES_HOUSEHOLDS));
    expect(client.getQueryData(["households"])).toEqual(ALICES_HOUSEHOLDS);

    act(() => handler.current!());

    expect(client.getQueryData(["households"])).toBeUndefined();
    expect(client.getQueryCache().getAll()).toHaveLength(0);
    // And the page is back at the door, not showing an empty shell.
    expect(await screen.findByRole("button", { name: "Sign in" }, WAIT)).toBeTruthy();
  });

  it("is emptied when a different person signs in", async () => {
    vi.mocked(api.post).mockResolvedValue({ authenticated: true, user: BOB });
    const client = boot(null);
    const button = await screen.findByRole("button", { name: "Sign in" }, WAIT);

    // Somebody else's ledger, still cached from before.
    act(() => client.setQueryData(["households"], ALICES_HOUSEHOLDS));

    const container = document.body;
    fireEvent.change(container.querySelector('input[type="email"]')!, {
      target: { value: BOB.email },
    });
    fireEvent.change(container.querySelector('input[type="password"]')!, {
      target: { value: "correct horse" },
    });
    fireEvent.click(button);

    await waitFor(() =>
      expect(client.getQueryCache().find({ queryKey: ["households"] })).toBeDefined(),
    WAIT);
    // The shell is up and asking for Bob's households; Alice's are not there
    // to be shown while it waits.
    expect(client.getQueryData(["households"])).toBeUndefined();
    expect(screen.queryByText("Alice's house")).toBeNull();
  });
});
