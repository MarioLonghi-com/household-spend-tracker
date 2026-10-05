// @vitest-environment jsdom

/**
 * The New account panel asks for the bank and a note (#12).
 *
 * The API always took both on create; the panel never collected them, so the
 * only way to record the bank was to make the account and open it again. What
 * is pinned here is the request body: what was typed reaches `institution` and
 * `note`, trimmed, and a blank field is sent as null rather than "".
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

vi.mock("../lib/api", () => ({
  api: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), put: vi.fn(), del: vi.fn(), upload: vi.fn() },
  ApiError: class ApiError extends Error {},
}));

import { api } from "../lib/api";
import { Accounts } from "./Accounts";
import type { Household } from "../lib/types";

afterEach(cleanup);

const HOUSEHOLD: Household = {
  id: "house-1",
  name: "Ours",
  base_currency: "GBP",
  date_format: "YYYY-MM-DD",
  note: null,
  theme: "default",
  accent: null,
  receipts_keep_original: false,
  receipts_keep_original_forced: false,
  receipts_with_original: 0,
  colours: null,
};

function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <Accounts household={HOUSEHOLD} />
    </QueryClientProvider>,
  );
}

async function openPanel(name: string) {
  mount();
  fireEvent.click(await screen.findByRole("button", { name: "Add an account" }));
  fireEvent.change(screen.getByLabelText("Name"), { target: { value: name } });
}

function created(): Record<string, unknown> {
  const calls = vi.mocked(api.post).mock.calls;
  expect(calls).toHaveLength(1);
  expect(calls[0][0]).toBe("/households/house-1/accounts");
  return calls[0][1] as Record<string, unknown>;
}

describe("New account panel", () => {
  beforeEach(() => {
    vi.mocked(api.get).mockReset().mockResolvedValue([] as never);
    vi.mocked(api.post)
      .mockReset()
      .mockResolvedValue({ id: "made" } as never);
  });

  it("sends the bank and the note it was given, trimmed", async () => {
    await openPanel("Joint current");
    fireEvent.change(screen.getByLabelText("Bank or institution"), {
      target: { value: "  Example Bank " },
    });
    fireEvent.change(screen.getByLabelText("Note"), {
      target: { value: "The one the salaries land in\n" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Create" }));

    await waitFor(() => expect(api.post).toHaveBeenCalled());
    const body = created();
    expect(body.name).toBe("Joint current");
    expect(body.institution).toBe("Example Bank");
    expect(body.note).toBe("The one the salaries land in");
  });

  it("sends null, not an empty string, for a blank bank or note", async () => {
    await openPanel("Pounds");
    // Whitespace alone is blank too.
    fireEvent.change(screen.getByLabelText("Bank or institution"), { target: { value: "   " } });
    fireEvent.click(screen.getByRole("button", { name: "Create" }));

    await waitFor(() => expect(api.post).toHaveBeenCalled());
    const body = created();
    expect(body.name).toBe("Pounds");
    expect(body.institution).toBeNull();
    expect(body.note).toBeNull();
  });
});
