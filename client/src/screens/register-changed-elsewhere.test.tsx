// @vitest-environment jsdom

/**
 * The register's panel when its row changes underneath it (#369).
 *
 * The register refetches on a change made elsewhere and hands the open panel
 * the newer row. With nothing typed, the fields follow it. With edits in hand,
 * the panel says so and saves nothing until the person chooses -- asserted on
 * what the fields hold and on what reached the server.
 */

import { useState } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";

vi.mock("../lib/api", () => ({
  api: { get: vi.fn(async () => []), post: vi.fn(), patch: vi.fn(), put: vi.fn(), del: vi.fn(), upload: vi.fn() },
  ApiError: class ApiError extends Error {},
}));

import { api } from "../lib/api";
import { TransactionPanel, sameFacts } from "./Register";
import type { Household, Transaction } from "../lib/types";

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

const ROW: Transaction = {
  id: "t1", account_id: "a1", date: "2026-07-03", amount: -1850, payee_id: "p1",
  payee_name: "Mercadona", category_id: null, category_name: null, memo: "weekly shop",
  cleared: "uncleared", transfer_account_id: null, transfer_transaction_id: null,
  import_id: null, split_id: null, running_balance: null, has_receipt: false,
  reimbursement: null, reimbursed_by_id: null,
};

let push: (next: Transaction) => void = () => undefined;

function Harness() {
  const [txn, setTxn] = useState(ROW);
  push = setTxn;
  return (
    <TransactionPanel
      txn={txn}
      household={{ id: "h1" } as Household}
      accountNameOf={() => "Santander"}
      householdId="h1"
      currency="EUR"
      accountName="Santander"
      payees={[]}
      groups={[]}
      onClose={() => undefined}
      onChanged={() => undefined}
      onSaved={() => undefined}
    />
  );
}

function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <Harness />
    </QueryClientProvider>,
  );
}

const memo = () => screen.getByLabelText("Memo") as HTMLInputElement;

describe("a row changed elsewhere while its panel is open", () => {
  it("compares only what the panel edits", () => {
    expect(sameFacts(ROW, { ...ROW, has_receipt: true, running_balance: 5 })).toBe(true);
    expect(sameFacts(ROW, { ...ROW, memo: "other" })).toBe(false);
  });

  it("follows the newer row when nothing was typed here", () => {
    mount();
    act(() => push({ ...ROW, memo: "split with Sam" }));

    expect(memo().value).toBe("split with Sam");
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("holds edits in hand, says so, and saves nothing until told", async () => {
    vi.mocked(api.patch).mockResolvedValue({ ...ROW, memo: "my words" });
    mount();
    fireEvent.change(memo(), { target: { value: "my words" } });

    act(() => push({ ...ROW, memo: "their words" }));
    fireEvent.blur(memo());

    expect(screen.getByRole("alert").textContent).toContain("changed somewhere else");
    expect(memo().value).toBe("my words");
    expect(api.patch).not.toHaveBeenCalled();

    // Keeping mine saves mine, on top of theirs.
    fireEvent.click(screen.getByRole("button", { name: "Keep my changes" }));
    await vi.waitFor(() => expect(api.patch).toHaveBeenCalledTimes(1));
    expect(vi.mocked(api.patch).mock.calls[0][1]).toMatchObject({ memo: "my words" });
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("shows the new version on request, dropping what was typed", () => {
    mount();
    fireEvent.change(memo(), { target: { value: "my words" } });
    act(() => push({ ...ROW, memo: "their words" }));

    fireEvent.click(screen.getByRole("button", { name: "Show the new version" }));

    expect(memo().value).toBe("their words");
    fireEvent.blur(memo());
    expect(api.patch).not.toHaveBeenCalled();
  });
});
