// @vitest-environment jsdom

/**
 * Answers that arrive after the screen has moved on (#445).
 *
 * - The account guessed from a file arrives after the request. It must not
 *   override an account picked by hand meanwhile, nor land for a file that is
 *   no longer the one chosen.
 * - A failed line save leaves its editor open with the error, and "Import
 *   these" stays held until that line is finished.
 * - A failed import of one preview is not shown over the next one.
 *
 * Asserted on the account the picker holds, on the button, and on the banner.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

vi.mock("../lib/api", () => ({
  api: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), put: vi.fn(), del: vi.fn(), upload: vi.fn() },
  ApiError: class ApiError extends Error {},
}));

import { api } from "../lib/api";
import { Import } from "./Import";
import type { StagedImport } from "./Import";
import type { Household, ImportLine, ImportPreview } from "../lib/types";

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

const ACCOUNTS = [
  { id: "acct-1", name: "Current", currency: "EUR", type: "checking" },
  { id: "acct-2", name: "Card", currency: "GBP", type: "credit_card" },
];

const TRAVEL = { id: "cat-travel", name: "Travel", full_name: "Quality of Life: Travel" };

function line(n: number): ImportLine {
  return {
    id: `line-${n}`, line_no: n, raw: `raw ${n}`,
    parsed: { date: "2026-01-05", payee: `Payee ${n}`, amount: -100 * n },
    outcome: "created", transaction_id: null, reason: null, category_id: null,
    category_name: null, category_chosen: false, category_uncategorised: false, similar_lines: 0,
  } as ImportLine;
}

function preview(batch: string): ImportPreview {
  return {
    batch_id: batch, filename: `${batch}.csv`, account_id: "acct-1", sha256: batch,
    detected: {}, warnings: [], counts: { created: 2 }, lines: [line(1), line(2)],
  };
}

const STAGED: StagedImport = {
  batch_id: "batch-1", filename: "batch-1.csv", account_id: "acct-1", account_name: "Current",
  actor_name: "Jane", staged_at: "2026-09-20T09:00:00Z", row_count: 2, sha256: "batch-1",
};

/** Recognising answers when told to, one promise per file chosen. */
let guesses: ((answer: object) => void)[] = [];

beforeEach(() => {
  guesses = [];
  vi.stubGlobal("IntersectionObserver", class { observe() {} disconnect() {} });
  vi.mocked(api.get).mockImplementation(((path: string) => {
    if (path.endsWith("/imports")) return Promise.resolve([STAGED]);
    if (path.endsWith("/imports/batch-1")) return Promise.resolve(preview("batch-1"));
    if (path.endsWith("/accounts")) return Promise.resolve(ACCOUNTS);
    if (path.includes("/categories"))
      return Promise.resolve([{ id: "g1", name: "Quality of Life", categories: [TRAVEL] }]);
    return Promise.resolve([]);
  }) as typeof api.get);
  vi.mocked(api.upload).mockImplementation(((path: string) =>
    path.endsWith("/imports/recognise")
      ? new Promise((resolve) => guesses.push(resolve as (answer: object) => void))
      : Promise.resolve(preview("batch-2"))) as typeof api.upload);
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  vi.unstubAllGlobals();
});

function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <Import household={HOUSEHOLD} />
    </QueryClientProvider>,
  );
}

const picker = () => screen.getByLabelText("Into which account") as HTMLSelectElement;
function choose(container: HTMLElement, name: string) {
  const input = container.querySelector<HTMLInputElement>('input[type="file"]')!;
  const file = new File(["a;b\n"], name, { type: "text/csv" });
  fireEvent.change(input, { target: { files: [file] } });
}
const guessed = (account: string) => ({
  account_id: account,
  account_name: ACCOUNTS.find((one) => one.id === account)!.name,
  how: "its IBAN",
});

describe("the account guessed from a file", () => {
  it("still picks the account when nobody has", async () => {
    const { container } = mount();
    await screen.findByRole("option", { name: /Card/ });
    choose(container, "card.csv");
    guesses[0](guessed("acct-2"));
    await waitFor(() => expect(picker().value).toBe("acct-2"));
  });

  it("does not override an account picked by hand while it was being made", async () => {
    const { container } = mount();
    await screen.findByRole("option", { name: /Card/ });
    choose(container, "statement.csv");
    fireEvent.change(picker(), { target: { value: "acct-2" } });

    guesses[0](guessed("acct-1"));
    // Said beside the picker, not applied to it.
    expect(await screen.findByText(/not the account chosen above/)).toBeTruthy();
    expect(picker().value).toBe("acct-2");
  });

  it("is dropped when another file has been chosen since", async () => {
    const { container } = mount();
    await screen.findByRole("option", { name: /Card/ });
    choose(container, "first.csv");
    choose(container, "second.csv");

    guesses[1](guessed("acct-2"));
    await waitFor(() => expect(picker().value).toBe("acct-2"));
    guesses[0](guessed("acct-1"));
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(picker().value).toBe("acct-2");
  });
});

async function openPreview() {
  mount();
  fireEvent.click(await screen.findByRole("button", { name: "Open" }));
  await screen.findByLabelText("Include line 2");
}

describe("stale import errors", () => {
  it("keep Import these held while a failed line save is still on screen", async () => {
    vi.mocked(api.patch).mockRejectedValue(new Error("that line could not be saved"));
    await openPreview();
    const importThese = () => screen.getByRole("button", { name: "Import these" }) as HTMLButtonElement;
    expect(importThese().disabled).toBe(false);

    const row = screen.getByLabelText("Include line 2").closest("tr")!;
    fireEvent.click(row.querySelector<HTMLButtonElement>('button[title="Where this line will land"]')!);
    const box = screen.getByRole("combobox", { name: "Category" });
    fireEvent.change(box, { target: { value: "Travel" } });
    fireEvent.keyDown(box, { key: "Enter" });

    expect(await screen.findByText("that line could not be saved")).toBeTruthy();
    expect(importThese().disabled).toBe(true);
  });

  it("do not carry a failed import over to the next file", async () => {
    vi.mocked(api.post).mockRejectedValue(new Error("the import was refused"));
    const { container } = mount();
    fireEvent.click(await screen.findByRole("button", { name: "Open" }));
    await screen.findByLabelText("Include line 2");
    fireEvent.click(screen.getByRole("button", { name: "Import these" }));
    expect(await screen.findByText("the import was refused")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    choose(container, "next.csv");
    guesses[0]({ account_id: null });
    fireEvent.change(picker(), { target: { value: "acct-2" } });
    fireEvent.click(screen.getByRole("button", { name: "Read the file" }));

    await screen.findByLabelText("Include line 2");
    await waitFor(() => expect(vi.mocked(api.upload)).toHaveBeenCalledTimes(2));
    expect(screen.queryByText("the import was refused")).toBeNull();
  });
});
