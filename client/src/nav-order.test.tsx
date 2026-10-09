// @vitest-environment jsdom

/**
 * The household's section of the menu, in #185's order.
 *
 * What is asserted is the rendered menu, read back in document order: which
 * words are there, which of them are pressable, and which sit under the
 * "Payee" heading. A heading that turned into a button, or a page that fell
 * out of the list, changes what these read.
 *
 * Mocked at `lib/api` the same way as `nav-collapse.test.tsx`: the signed-in
 * user and two households resolve, everything else stays in flight.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";

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

const ALICE: User = {
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

describe("the household's section of the menu", () => {
  it("lists #185's order, with Payee a heading over three pages", async () => {
    boot();
    const section = await screen.findByRole("group", { name: "Alice's house" });

    // Everything under the header, in the order it is drawn: pages and the
    // one heading alike.
    const entries = Array.from(section.querySelectorAll(".nav-child, .nav-subhead")).map(
      (node) => `${node.tagName === "BUTTON" ? "" : "# "}${node.textContent}`,
    );
    expect(entries).toEqual([
      "Accounts",
      "Transfers",
      "Categories",
      "# Payee",
      "Payee Merge",
      "Payee Categorisation",
      "Payee Naming Rules",
    ]);

    // "Payee" is words, not a control: nothing to press, nowhere to go.
    const payee = within(section).getByRole("group", { name: "Payee" });
    expect(within(section).queryByRole("button", { name: "Payee" })).toBeNull();

    // And the three pages are the ones under it, each one level further in.
    const under = within(payee)
      .getAllByRole("button")
      .map((one) => [one.textContent, one.classList.contains("nav-grandchild")]);
    expect(under).toEqual([
      ["Payee Merge", true],
      ["Payee Categorisation", true],
      ["Payee Naming Rules", true],
    ]);
  });

  it("opens the payee list from Payee Merge", async () => {
    boot();
    const merge = await screen.findByRole("button", { name: "Payee Merge" });
    expect(merge.getAttribute("aria-current")).toBeNull();

    fireEvent.click(merge);

    expect(merge.getAttribute("aria-current")).toBe("page");
    expect(await screen.findByRole("heading", { name: "Payees", level: 1 })).toBeTruthy();
  });
});
