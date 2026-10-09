// @vitest-environment jsdom

/**
 * What a browser still holds once somebody signs out of it (#198).
 *
 * Asserted on the storage itself: the personal filters and `/snap`'s queue
 * and household are gone, the device's own settings are still there.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";

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

const PERSONAL = {
  "spendtracker.register.household-a.search": JSON.stringify("pharmacy"),
  "spendtracker.register.household-b.amount": JSON.stringify("42.00"),
  "spendtracker.reimbursements.household-a.range": JSON.stringify({ from: "2026-01-01" }),
};
const DEVICE = {
  "spendtracker.appearance": JSON.stringify("dark"),
  "spendtracker.register.columns": JSON.stringify({ payee: 200 }),
  "spendtracker.shell.navCollapsed": JSON.stringify(true),
};

const deleted: string[] = [];

beforeEach(() => {
  deleted.length = 0;
  window.localStorage.clear();
  vi.stubGlobal("indexedDB", {
    deleteDatabase: (name: string) => {
      deleted.push(name);
      return {};
    },
  });
  vi.mocked(api.get).mockReset();
  vi.mocked(api.get).mockImplementation((path: string) => {
    if (path === "/health") return Promise.resolve({ setup_required: false });
    if (path === "/me") return Promise.resolve(ALICE);
    if (path === "/households") return Promise.resolve(HOUSEHOLDS);
    return new Promise(() => {});
  });
  vi.mocked(api.del).mockResolvedValue(null);
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  window.localStorage.clear();
});

describe("signing out", () => {
  it("forgets the personal filters and /snap's queue, and keeps the device's settings", async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <App />
      </QueryClientProvider>,
    );
    await screen.findByRole("group", { name: "Alice's house" });
    // Written after the shell is up, as the screens would have.
    for (const [key, value] of Object.entries({ ...PERSONAL, ...DEVICE }))
      window.localStorage.setItem(key, value);
    window.localStorage.setItem("snap.household", "household-a");

    fireEvent.click(screen.getByRole("button", { name: "Sign out" }));
    await screen.findByRole("button", { name: "Sign in" });

    for (const key of Object.keys(PERSONAL)) expect(window.localStorage.getItem(key)).toBeNull();
    expect(window.localStorage.getItem("snap.household")).toBeNull();
    expect(deleted).toEqual(["snap"]);
    for (const [key, value] of Object.entries(DEVICE))
      expect(window.localStorage.getItem(key)).toBe(value);
  });
});
