// @vitest-environment jsdom

/**
 * How receipts are stored, on the Household page (#162).
 *
 * What is pinned: both answers show their size before anything is chosen;
 * choosing the heavy one shows the warning, and Save sends
 * `receipts_keep_original: true` to *this* household and no other; choosing
 * back sends `false`; and when the operator has forced it on, both radios are
 * locked, the heavy one reads as chosen, and Save sends `null` so a stale tab
 * cannot be the thing that changes it.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

vi.mock("../lib/api", () => ({
  api: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), put: vi.fn(), del: vi.fn(), upload: vi.fn() },
  ApiError: class ApiError extends Error {},
}));

import { api } from "../lib/api";
import type { Household, User } from "../lib/types";
import { HouseholdPage } from "./Household";

// Two households, so a PATCH that went to the wrong id would show.
const HOME: Household = {
  id: "hh-home",
  name: "Home",
  base_currency: "EUR",
  date_format: "YYYY-MM-DD",
  note: null,
  theme: "default",
  accent: null,
  colours: null,
  receipts_keep_original: false,
  receipts_keep_original_forced: false,
  receipts_with_original: 0,
};
const FLAT: Household = { ...HOME, id: "hh-flat", name: "Flat", base_currency: "GBP" };

const USER: User = {
  id: "u-1",
  email: "someone@example.test",
  display_name: "Someone",
  role: "owner",
  disabled_at: null,
};

beforeEach(() => {
  // The palettes answer at once; the counts and the members never do, which
  // leaves those two sections on their loading line and out of the way.
  vi.mocked(api.get).mockImplementation((path: string) =>
    path === "/themes" ? Promise.resolve([]) : new Promise(() => {}),
  );
  vi.mocked(api.patch).mockImplementation((_path: string, body: unknown) =>
    Promise.resolve(body),
  );
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

function mount(household: Household) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <HouseholdPage household={household} user={USER} onChanged={vi.fn()} />
    </QueryClientProvider>,
  );
}

const light = () => screen.getByRole("radio", { name: /Readable copies only/ }) as HTMLInputElement;
const heavy = () =>
  screen.getByRole("radio", { name: /Also keep the original file/ }) as HTMLInputElement;
// The sizes are written with a no-break space so "33 KB" never splits across
// a line; read them back with an ordinary one.
const text = (radio: HTMLInputElement) =>
  (radio.closest("label")?.textContent ?? "").replace(/\u00a0/g, " ");
const warning = () => screen.queryByText(/70× more space/);

async function saved(): Promise<[string, Record<string, unknown>]> {
  fireEvent.click(screen.getByRole("button", { name: "Save" }));
  await waitFor(() => expect(api.patch).toHaveBeenCalledTimes(1));
  const [path, body] = vi.mocked(api.patch).mock.calls[0];
  return [path as string, body as Record<string, unknown>];
}

describe("receipts storage", () => {
  it("offers both answers in one group, each with its size", () => {
    mount(HOME);
    const group = screen.getByRole("radiogroup", { name: "Receipts" });
    expect(group).toBeTruthy();
    expect(text(light())).toContain("33 KB per receipt");
    expect(text(light())).toContain("Recommended");
    expect(text(heavy())).toContain("2 MB per receipt");
    expect(text(heavy())).toContain("70× the space");

    // Off by default, and nothing to warn about while it is.
    expect(light().checked).toBe(true);
    expect(heavy().checked).toBe(false);
    expect(warning()).toBeNull();
    expect(screen.queryByText(/Originals are not kept/)).toBeNull();
  });

  it("warns on the heavy answer and saves true to this household", async () => {
    mount(FLAT);
    fireEvent.click(heavy());

    expect(heavy().checked).toBe(true);
    expect(light().checked).toBe(false);
    expect(heavy().closest("label")?.className).toContain("chosen");
    expect(light().closest("label")?.className).not.toContain("chosen");
    expect(warning()?.closest(".banner")?.textContent).toContain("Existing receipts are not changed");

    const [path, body] = await saved();
    expect(path).toBe("/households/hh-flat");
    expect(body.receipts_keep_original).toBe(true);
  });

  it("saves false when a household that keeps originals goes back", async () => {
    mount({ ...HOME, receipts_keep_original: true, receipts_with_original: 3 });
    expect(heavy().checked).toBe(true);
    expect(warning()).not.toBeNull();

    fireEvent.click(light());
    expect(warning()).toBeNull();
    // The ones already stored are said to stay, since this is the moment
    // somebody would wonder.
    expect(
      screen.getByText(/The 3 receipts that already have an original keep it/),
    ).toBeTruthy();

    const [path, body] = await saved();
    expect(path).toBe("/households/hh-home");
    expect(body.receipts_keep_original).toBe(false);
  });

  it("locks both answers when the operator has forced it, and sends null", async () => {
    mount({ ...HOME, receipts_keep_original: false, receipts_keep_original_forced: true });

    expect(light().disabled).toBe(true);
    expect(heavy().disabled).toBe(true);
    // Shown as what the server will do, not as what this household last saved.
    expect(heavy().checked).toBe(true);
    expect(light().checked).toBe(false);
    expect(screen.getByText(/Turned on for every household by whoever runs this server/)).toBeTruthy();
    expect(warning()).toBeNull();

    const [, body] = await saved();
    expect(body.receipts_keep_original).toBeNull();
  });
});
