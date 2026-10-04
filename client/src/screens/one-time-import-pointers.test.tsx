// @vitest-environment jsdom

/**
 * Where the One-time Import shows up outside its own wizard (#183).
 *
 * The Import screen points at it until one has been done in the household --
 * an undone one does not count -- and a row it brought in says so in the
 * register's "Where did this come from?", rather than "entered by hand".
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

vi.mock("../lib/api", () => ({
  api: {
    get: vi.fn(),
    post: vi.fn(),
    patch: vi.fn(),
    put: vi.fn(),
    del: vi.fn(),
    upload: vi.fn(),
  },
  ApiError: class ApiError extends Error {},
}));

import { api } from "../lib/api";
import { OneTimeImportNote } from "./Import";
import { Origin } from "./Register";
import type { Household, OneTimePriorImport, TransactionOrigin } from "../lib/types";

afterEach(cleanup);

const HOUSEHOLD = { id: "house-1", name: "Ours", base_currency: "EUR" } as unknown as Household;

function prior(status: string): OneTimePriorImport {
  return {
    batch_id: `batch-${status}`,
    at: "2026-09-20T09:00:00Z",
    workflow: "ynab",
    workflow_name: "YNAB",
    via: "csv",
    filename: "export.zip",
    plan_name: null,
    status,
  };
}

function wrap(node: ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={client}>{node}</QueryClientProvider>);
}

function answer(imports: OneTimePriorImport[]) {
  vi.mocked(api.get).mockReset();
  vi.mocked(api.get).mockImplementation(((path: string) =>
    path === "/households/house-1/one-time-import/history"
      ? Promise.resolve({ imports })
      : Promise.reject(new Error(`unexpected ${path}`))) as typeof api.get);
}

describe("the Import screen's pointer to the One-time Import", () => {
  it("is shown while none has been done, and goes to the household page", async () => {
    answer([]);
    const onGo = vi.fn();
    wrap(<OneTimeImportNote household={HOUSEHOLD} onGo={onGo} />);

    const note = await screen.findByRole("note", { name: "One-time Import" });
    expect(note.textContent).toContain("Coming from another budgeting app?");
    expect(note.textContent).toContain("on the Ours page");

    fireEvent.click(screen.getByRole("button", { name: "One-time Import" }));
    expect(onGo).toHaveBeenCalledWith("household");
  });

  it("is still shown when the only one was undone", async () => {
    answer([prior("undone")]);
    wrap(<OneTimeImportNote household={HOUSEHOLD} onGo={() => {}} />);
    expect(await screen.findByRole("note", { name: "One-time Import" })).toBeTruthy();
  });

  it("is hidden once one has been done", async () => {
    answer([prior("applied"), prior("undone")]);
    wrap(<OneTimeImportNote household={HOUSEHOLD} onGo={() => {}} />);
    await waitFor(() => expect(vi.mocked(api.get)).toHaveBeenCalled());
    // Let the answer land, then check nothing was drawn.
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(screen.queryByRole("note", { name: "One-time Import" })).toBeNull();
  });
});

describe("where a row came from", () => {
  const BASE: TransactionOrigin = {
    kind: "one_time_import",
    imported_at: "2026-09-20T09:00:00Z",
    filename: null,
    line_no: null,
    raw: null,
    bank: null,
    payee_original: "CARD 0001 CORNER SHOP TESTVILLE",
    import_id: "ynab:abc",
    batch_id: "batch-1",
    via: null,
    workflow: "YNAB",
    workflow_via: "csv",
    plan_name: null,
  };

  function origin(found: TransactionOrigin) {
    vi.mocked(api.get).mockReset();
    vi.mocked(api.get).mockImplementation(((path: string) =>
      path === "/transactions/txn-1/origin"
        ? Promise.resolve(found)
        : Promise.reject(new Error(`unexpected ${path}`))) as typeof api.get);
    wrap(<Origin transactionId="txn-1" />);
    fireEvent.click(screen.getByRole("button", { name: "Where did this come from?" }));
  }

  // Braces: mockReset returns the mock, and a hook that returns a function
  // has it called as its cleanup.
  beforeEach(() => {
    vi.mocked(api.get).mockReset();
  });

  it("names the One-time Import, the workflow, the file and the date", async () => {
    origin({ ...BASE, filename: "export.zip" });
    const said = await screen.findByText(/Brought in by the/);
    expect(said.textContent).toContain("One-time Import from YNAB, via its CSV export");
    expect(said.textContent).toContain("reading the file export.zip");
    expect(said.textContent).toMatch(/on .*2026/);
    expect(screen.queryByText(/entered by hand/)).toBeNull();
    expect(screen.queryByText(/line/)).toBeNull();
    expect(screen.getByText(/The bank called it/).textContent).toBe(
      "The bank called it CARD 0001 CORNER SHOP TESTVILLE, as YNAB kept it.",
    );
  });

  it("names the plan when it came through the API", async () => {
    origin({ ...BASE, workflow_via: "api", plan_name: "Household Plan" });
    const said = await screen.findByText(/Brought in by the/);
    expect(said.textContent).toContain("from YNAB, via the YNAB API, reading the plan Household Plan");
  });

  it("says nothing of the bank's text when the source had none (#265)", async () => {
    origin({ ...BASE, filename: "export.zip", payee_original: null });
    await screen.findByText(/Brought in by the/);
    expect(screen.queryByText(/called/)).toBeNull();
  });

  it("still says a statement line for a statement", async () => {
    origin({
      ...BASE,
      kind: "statement",
      filename: "march.csv",
      line_no: 12,
      raw: "a,b,c",
      workflow: null,
      workflow_via: null,
    });
    const said = await screen.findByText(/Imported from/);
    expect(said.textContent).toContain("march.csv, line 12");
  });
});
