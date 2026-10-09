// @vitest-environment jsdom

/**
 * The desktop menu folds to its rail and stays folded (#161).
 *
 * What is asserted is where the state *ends up* -- the value in
 * `localStorage`, the class on the shell, what the button now says -- and
 * then that a fresh mount reads it back. A click handler that ran and
 * changed nothing would pass a test that only checked it ran.
 *
 * The server is mocked at `lib/api`, as in `session-cache.test.tsx`. The
 * signed-in user and two households resolve -- the shell draws nothing but
 * "Loading…" until it has a household -- and every other request stays in
 * flight, because the rail belongs to the shell and not to anything the
 * screens inside it load.
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

const KEY = "spendtracker.shell.navCollapsed";

const ALICE: User = {
  id: "user-a",
  email: "a@example.test",
  display_name: "Alice",
  role: "owner",
  disabled_at: null,
} as User;

// Two, because fixtures have two of everything -- and the key the rail writes
// carries no household, so neither of them owns the choice.
const HOUSEHOLDS = [
  { id: "household-a", name: "Alice's house", colours: null },
  { id: "household-b", name: "The flat", colours: null },
] as unknown as Household[];

function boot() {
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

beforeEach(() => {
  vi.mocked(api.get).mockReset();
  window.localStorage.clear();
});

afterEach(() => {
  cleanup();
  window.localStorage.clear();
});

describe("the desktop menu's rail", () => {
  it("folds the menu, remembers it, and comes back folded", async () => {
    const first = boot();
    const rail = await screen.findByRole("button", { name: "Hide the menu" });
    const shell = first.container.querySelector(".shell")!;

    // Expanded is the default, and nothing is stored until somebody chooses.
    expect(window.localStorage.getItem(KEY)).toBeNull();
    expect(shell.classList.contains("nav-collapsed")).toBe(false);
    expect(rail.getAttribute("aria-expanded")).toBe("true");

    fireEvent.click(rail);

    expect(window.localStorage.getItem(KEY)).toBe("true");
    expect(shell.classList.contains("nav-collapsed")).toBe(true);
    expect(rail.getAttribute("aria-expanded")).toBe("false");
    expect(rail.getAttribute("aria-label")).toBe("Show the menu");
    expect(screen.queryByRole("button", { name: "Hide the menu" })).toBeNull();

    // A new page load in the same browser: the menu is still folded.
    first.unmount();
    const second = boot();
    const again = await screen.findByRole("button", { name: "Show the menu" });
    expect(again.getAttribute("aria-expanded")).toBe("false");
    expect(second.container.querySelector(".shell")!.classList.contains("nav-collapsed")).toBe(true);

    // And unfolding it is remembered the same way.
    fireEvent.click(again);
    expect(window.localStorage.getItem(KEY)).toBe("false");
    expect(second.container.querySelector(".shell")!.classList.contains("nav-collapsed")).toBe(false);
  });
});
