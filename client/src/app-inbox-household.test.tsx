// @vitest-environment jsdom

/**
 * The household in `?open=…&household=…` is checked against the member's own
 * households before any request is built from it (#197).
 *
 * What is asserted is the paths the shell actually asked the API for: none
 * walks out of `/households/<id>`, and the inbox badge asked about a
 * household this person is in.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";

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
  setUnauthorizedHandler: () => {},
}));

import { api } from "./lib/api";
import { App } from "./App";
import type { Household, User } from "./lib/types";

const ALICE = {
  id: "user-a",
  email: "a@example.test",
  display_name: "Alice",
  role: "owner",
  disabled_at: null,
} as User;

const HOUSEHOLDS = [
  { id: "household-a", name: "Alice's house", colours: null },
  { id: "household-b", name: "The flat", colours: null },
] as unknown as Household[];

function boot(search: string) {
  window.history.replaceState(null, "", search);
  vi.mocked(api.get).mockImplementation((path: string) => {
    if (path === "/health") return Promise.resolve({ setup_required: false });
    if (path === "/me") return Promise.resolve(ALICE);
    if (path === "/households") return Promise.resolve(HOUSEHOLDS);
    return new Promise(() => {});
  });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <App />
    </QueryClientProvider>,
  );
}

const paths = () => vi.mocked(api.get).mock.calls.map(([path]) => path);
const inboxPaths = () => paths().filter((path) => path.includes("unattached"));

beforeEach(() => {
  vi.mocked(api.get).mockReset();
});

afterEach(() => {
  cleanup();
  window.history.replaceState(null, "", "/");
  window.localStorage.clear();
});

describe("the household a link names", () => {
  it("never reaches an API path when it is not an id", { timeout: 15_000 }, async () => {
    boot("/?open=accounts&household=..%2Fme%3Fx%3D");
    await screen.findByRole("group", { name: "Alice's house" }, { timeout: 5_000 });
    await waitFor(() => expect(inboxPaths().length).toBeGreaterThan(0));

    expect(paths().filter((path) => path.includes(".."))).toEqual([]);
    expect(new Set(inboxPaths())).toEqual(
      new Set(["/households/household-a/receipts?unattached=true"]),
    );
  });

  it("falls back to one of the member's own when it is not one of theirs", { timeout: 15_000 }, async () => {
    boot("/?open=accounts&household=someone-elses");
    await screen.findByRole("group", { name: "Alice's house" }, { timeout: 5_000 });
    await waitFor(() => expect(inboxPaths().length).toBeGreaterThan(0));

    expect(paths().filter((path) => path.includes("someone-elses"))).toEqual([]);
    expect(new Set(inboxPaths())).toEqual(
      new Set(["/households/household-a/receipts?unattached=true"]),
    );
  });

  it("opens the named household when it is the member's", { timeout: 15_000 }, async () => {
    boot("/?open=accounts&household=household-b");
    await screen.findByRole("group", { name: "The flat" }, { timeout: 5_000 });
    await waitFor(() => expect(inboxPaths().length).toBeGreaterThan(0));

    expect(new Set(inboxPaths())).toEqual(
      new Set(["/households/household-b/receipts?unattached=true"]),
    );
  });
});
