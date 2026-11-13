// @vitest-environment jsdom

/**
 * The transaction panel's autosave, when edits and saves overlap (#445).
 *
 * - A field left while a save is in flight is saved when that one settles,
 *   and the first save's answer does not put the old value back in it.
 * - A save sends only what differs from the row as last confirmed, so a date
 *   edit does not put back a memo or category somebody else changed.
 * - Escape sends what is still unsaved rather than dropping it.
 * - A category picked is saved then, alone, once; the options the arrows pass
 *   over are not (#462).
 *
 * Asserted on the bodies that reached the server and on what the fields hold.
 */

import { useState } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";

vi.mock("../lib/api", () => ({
  api: { get: vi.fn(async () => []), post: vi.fn(), patch: vi.fn(), put: vi.fn(), del: vi.fn(), upload: vi.fn() },
  ApiError: class ApiError extends Error {},
}));

import { api } from "../lib/api";
import { TransactionPanel, panelChanges } from "./Register";
import type { CategoryGroup, Household, Transaction } from "../lib/types";

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

const ROW: Transaction = {
  id: "t1", account_id: "a1", date: "2026-07-03", amount: -1850, payee_id: "p1",
  payee_name: "Corner Shop", category_id: "c1", category_name: "Groceries", memo: "weekly shop",
  cleared: "uncleared", transfer_account_id: null, transfer_transaction_id: null,
  import_id: null, split_id: null, running_balance: null, has_receipt: false,
  reimbursement: null, reimbursed_by_id: null,
};

const GROUPS: CategoryGroup[] = [
  {
    id: "g1", name: "Everyday", sort_order: 0,
    categories: [
      { id: "c1", group_id: "g1", name: "Groceries", full_name: "Everyday: Groceries", sort_order: 0, archived: false, used_by: 1 },
      { id: "c2", group_id: "g1", name: "Travel", full_name: "Everyday: Travel", sort_order: 1, archived: false, used_by: 0 },
    ],
  },
];

function Harness({ onClose, start = ROW }: { onClose: () => void; start?: Transaction }) {
  const [txn] = useState(start);
  return (
    <TransactionPanel
      txn={txn}
      household={{ id: "h1" } as Household}
      accountNameOf={() => "Current"}
      householdId="h1"
      currency="EUR"
      accountName="Current"
      payees={[]}
      groups={GROUPS}
      onClose={onClose}
      onChanged={() => undefined}
      onSaved={() => undefined}
    />
  );
}

function mount(start: Transaction = ROW) {
  const onClose = vi.fn();
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <Harness onClose={onClose} start={start} />
    </QueryClientProvider>,
  );
  return onClose;
}

/** A PATCH that answers when told to. */
function held() {
  const answers: ((row: Transaction) => void)[] = [];
  vi.mocked(api.patch).mockImplementation(
    () => new Promise((resolve) => answers.push(resolve as (row: Transaction) => void)),
  );
  return answers;
}

const memo = () => screen.getByLabelText("Memo") as HTMLTextAreaElement;
const cleared = () => screen.getByLabelText("Cleared") as HTMLSelectElement;
const category = () => screen.getByLabelText("Category") as HTMLInputElement;
const option = (name: string) => screen.getByRole("option", { name });
const bodies = () => vi.mocked(api.patch).mock.calls.map((call) => call[1]);

describe("what a panel save sends", () => {
  it("is only what differs from the row as last confirmed", () => {
    const fields = {
      date: "2026-07-04", payee: "Corner Shop", outflow: "18.50", inflow: "",
      memo: "weekly shop", cleared: "uncleared" as const, categoryId: "c1",
    };
    expect(panelChanges(ROW, fields, "EUR", false)).toEqual({ date: "2026-07-04" });
    expect(panelChanges(ROW, { ...fields, date: ROW.date, memo: " ", categoryId: null }, "EUR", false))
      .toEqual({ clear_memo: true, clear_category: true });
    // Nothing about the category on a transfer leg.
    expect(panelChanges(ROW, { ...fields, date: ROW.date, categoryId: null }, "EUR", true)).toEqual({});
  });
});

describe("an edit made while a save is in flight", () => {
  it("is sent when that save settles, and stays in its field", async () => {
    const answers = held();
    mount();

    fireEvent.change(memo(), { target: { value: "first" } });
    fireEvent.blur(memo());
    await vi.waitFor(() => expect(api.patch).toHaveBeenCalledTimes(1));

    fireEvent.change(cleared(), { target: { value: "cleared" } });
    fireEvent.blur(cleared());
    // Nothing goes beside the save in flight.
    expect(api.patch).toHaveBeenCalledTimes(1);

    // The first answer still says uncleared: it was sent before the edit.
    answers[0]({ ...ROW, memo: "first" });
    await vi.waitFor(() => expect(api.patch).toHaveBeenCalledTimes(2));

    expect(bodies()).toEqual([{ memo: "first" }, { cleared: "cleared" }]);
    expect(cleared().value).toBe("cleared");
    expect(memo().value).toBe("first");
  });

  it("is sent when the panel is closed with Escape while the save is out", async () => {
    const answers = held();
    const onClose = mount();

    fireEvent.change(memo(), { target: { value: "first" } });
    fireEvent.blur(memo());
    await vi.waitFor(() => expect(api.patch).toHaveBeenCalledTimes(1));
    fireEvent.change(cleared(), { target: { value: "cleared" } });
    fireEvent.keyDown(document, { key: "Escape" });

    expect(onClose).toHaveBeenCalledTimes(1);
    await vi.waitFor(() => expect(api.patch).toHaveBeenCalledTimes(2));
    expect(bodies()[1]).toMatchObject({ cleared: "cleared" });
    answers.forEach((answer) => answer({ ...ROW, memo: "first", cleared: "cleared" }));
  });
});

describe("a field nobody touched here", () => {
  it("takes the server's answer, and is not sent back with the next edit", async () => {
    const answers = held();
    mount();

    fireEvent.change(memo(), { target: { value: "first" } });
    fireEvent.blur(memo());
    await vi.waitFor(() => expect(api.patch).toHaveBeenCalledTimes(1));
    // Another member changed the category meanwhile; the answer carries it.
    answers[0]({ ...ROW, memo: "first", category_id: "c2", category_name: "Travel" });
    await vi.waitFor(() => expect(screen.queryByText("saving…")).toBeNull());

    fireEvent.change(cleared(), { target: { value: "cleared" } });
    fireEvent.blur(cleared());
    await vi.waitFor(() => expect(api.patch).toHaveBeenCalledTimes(2));

    // Before #445 this carried category_id "c1" and put the old one back.
    expect(bodies()[1]).toEqual({ cleared: "cleared" });
    answers[1]({ ...ROW, memo: "first", category_id: "c2", cleared: "cleared" });
  });
});

describe("a locked row", () => {
  it("saves being set back to cleared, alone, and nothing else", async () => {
    vi.mocked(api.patch).mockResolvedValue({ ...ROW, cleared: "cleared" });
    mount({ ...ROW, cleared: "reconciled" });

    fireEvent.change(memo(), { target: { value: "edited while locked" } });
    fireEvent.blur(memo());
    expect(api.patch).not.toHaveBeenCalled();

    fireEvent.change(memo(), { target: { value: ROW.memo } });
    fireEvent.change(cleared(), { target: { value: "cleared" } });
    fireEvent.blur(cleared());
    await vi.waitFor(() => expect(api.patch).toHaveBeenCalledTimes(1));
    expect(bodies()).toEqual([{ cleared: "cleared" }]);
  });
});

describe("a category picked in the panel", () => {
  it("is saved when it is clicked, alone and once, with no field left", async () => {
    vi.mocked(api.patch).mockResolvedValue({ ...ROW, category_id: "c2", category_name: "Travel" });
    mount();

    fireEvent.focus(category());
    fireEvent.mouseDown(option("Everyday: Travel"));
    await vi.waitFor(() => expect(api.patch).toHaveBeenCalledTimes(1));
    expect(bodies()).toEqual([{ category_id: "c2" }]);

    // Leaving the field afterwards commits the same choice: not a second save.
    fireEvent.blur(category());
    await vi.waitFor(() => expect(category().value).toBe("Everyday: Travel"));
    expect(api.patch).toHaveBeenCalledTimes(1);
  });

  it("is not saved for each option the arrows pass over, only for the one taken", async () => {
    vi.mocked(api.patch).mockResolvedValue({ ...ROW, category_id: "c2", category_name: "Travel" });
    mount();

    fireEvent.focus(category());
    fireEvent.keyDown(category(), { key: "ArrowDown" });
    fireEvent.keyDown(category(), { key: "ArrowDown" });
    fireEvent.keyDown(category(), { key: "ArrowUp" });
    fireEvent.keyDown(category(), { key: "ArrowDown" });
    expect(api.patch).not.toHaveBeenCalled();

    fireEvent.keyDown(category(), { key: "Enter" });
    await vi.waitFor(() => expect(api.patch).toHaveBeenCalledTimes(1));
    fireEvent.blur(category());
    expect(bodies()).toEqual([{ category_id: "c2" }]);
  });

  it("is sent after a save in flight rather than beside it or not at all", async () => {
    const answers = held();
    mount();

    fireEvent.change(memo(), { target: { value: "first" } });
    fireEvent.blur(memo());
    await vi.waitFor(() => expect(api.patch).toHaveBeenCalledTimes(1));

    fireEvent.focus(category());
    fireEvent.mouseDown(option("Everyday: Travel"));
    expect(api.patch).toHaveBeenCalledTimes(1);

    // The first answer still carries c1: it was sent before the pick.
    answers[0]({ ...ROW, memo: "first" });
    await vi.waitFor(() => expect(api.patch).toHaveBeenCalledTimes(2));
    expect(bodies()).toEqual([{ memo: "first" }, { category_id: "c2" }]);
    expect(category().value).toBe("Everyday: Travel");
    answers[1]({ ...ROW, memo: "first", category_id: "c2", category_name: "Travel" });
  });

  it("emptied and left clears it, once", async () => {
    vi.mocked(api.patch).mockResolvedValue({ ...ROW, category_id: null, category_name: null });
    mount();

    fireEvent.change(category(), { target: { value: "" } });
    fireEvent.blur(category());
    await vi.waitFor(() => expect(api.patch).toHaveBeenCalledTimes(1));
    expect(bodies()).toEqual([{ clear_category: true }]);
  });
});
