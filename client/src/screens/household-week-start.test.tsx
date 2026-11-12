// @vitest-environment jsdom

/**
 * The first day of the week, on the Household page (#452).
 *
 * What is pinned: the select shows the household's own day, offers exactly
 * the three the server accepts, names them in the language the words are in
 * (by `Intl`, not by a catalog), and Save sends the chosen day to *this*
 * household and no other.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

vi.mock("../lib/api", () => ({
  api: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), put: vi.fn(), del: vi.fn(), upload: vi.fn() },
  ApiError: class ApiError extends Error {},
}));

import { api } from "../lib/api";
import { activate } from "../lib/i18n";
import type { Household, User } from "../lib/types";
import { HouseholdPage } from "./Household";

// Two households on different days, so a PATCH to the wrong id would show.
const HOME: Household = {
  id: "hh-home",
  name: "Home",
  base_currency: "EUR",
  date_format: "YYYY-MM-DD",
  week_start: "monday",
  note: null,
  theme: "default",
  accent: null,
  colours: null,
  receipts_keep_original: false,
  receipts_keep_original_forced: false,
  receipts_with_original: 0,
};
const FLAT: Household = { ...HOME, id: "hh-flat", name: "Flat", base_currency: "BRL", week_start: "sunday" };

const USER: User = {
  id: "u-1",
  email: "someone@example.test",
  display_name: "Someone",
  role: "member",
  disabled_at: null,
};

beforeEach(async () => {
  await activate("en");
  vi.mocked(api.get).mockImplementation((path: string) =>
    path === "/themes" ? Promise.resolve([]) : new Promise(() => {}),
  );
  vi.mocked(api.patch).mockImplementation((_path: string, body: unknown) => Promise.resolve(body));
});

afterEach(async () => {
  cleanup();
  vi.clearAllMocks();
  await activate("en");
});

function mount(household: Household) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <HouseholdPage household={household} user={USER} onChanged={vi.fn()} />
    </QueryClientProvider>,
  );
}

const select = (name: string | RegExp) => screen.getByRole("combobox", { name }) as HTMLSelectElement;
const options = (el: HTMLSelectElement) => Array.from(el.options).map((o) => [o.value, o.textContent]);

describe("first day of the week", () => {
  it("shows each household's own day, out of the three the server takes", () => {
    mount(HOME);
    expect(select("First day of the week").value).toBe("monday");
    expect(options(select("First day of the week"))).toEqual([
      ["monday", "Monday"],
      ["sunday", "Sunday"],
      ["saturday", "Saturday"],
    ]);
    cleanup();

    mount(FLAT);
    expect(select("First day of the week").value).toBe("sunday");
  });

  it("names the days in the language the words are in", async () => {
    await activate("pt-BR");
    mount(FLAT);
    const field = select("Primeiro dia da semana");
    expect(options(field)).toEqual([
      ["monday", "Segunda-feira"],
      ["sunday", "Domingo"],
      ["saturday", "Sábado"],
    ]);
  });

  it("saves the chosen day to this household", async () => {
    mount(FLAT);
    fireEvent.change(select("First day of the week"), { target: { value: "saturday" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(api.patch).toHaveBeenCalledTimes(1));

    const [path, body] = vi.mocked(api.patch).mock.calls[0] as [string, Record<string, unknown>];
    expect(path).toBe("/households/hh-flat");
    expect(body.week_start).toBe("saturday");
  });
});
