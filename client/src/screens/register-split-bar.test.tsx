// @vitest-environment jsdom

/**
 * The split panel's bar, its typed amounts and its discard question (#28).
 *
 * Every check reads the figures the panel would send, not that a handler ran.
 * Two currencies, one with cents and one without, so a bar that only ever
 * worked in hundredths cannot pass; two categories, so a part's category
 * cannot survive a drag by being the only one there is.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

vi.mock("../lib/api", () => ({
  api: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), put: vi.fn(), del: vi.fn(), upload: vi.fn() },
  ApiError: class ApiError extends Error {},
}));

import { api } from "../lib/api";
import { SplitPanel } from "./Register";
import type { CategoryGroup, Transaction } from "../lib/types";

afterEach(() => {
  cleanup();
  vi.mocked(api.post).mockReset();
});

beforeAll(() => {
  // jsdom has no pointer capture and no layout. The bar is 442px wide from 0,
  // so a pixel is a minor unit of the EUR row.
  Element.prototype.setPointerCapture = () => {};
  Element.prototype.hasPointerCapture = () => true;
});

const GROUPS = [
  {
    id: "g-1",
    name: "Everyday",
    sort_order: 0,
    categories: [
      { id: "cat-food", group_id: "g-1", name: "Eating out", full_name: "Everyday: Eating out" },
      { id: "cat-drink", group_id: "g-1", name: "Drinks", full_name: "Everyday: Drinks" },
    ],
  },
] as unknown as CategoryGroup[];

function txn(amount: number): Transaction {
  return {
    id: "t-1",
    account_id: "acc-1",
    date: "2026-09-28",
    amount,
    payee_name: "Corner cafe",
    category_id: "cat-food",
    memo: null,
  } as unknown as Transaction;
}

function open(amount: number, currency: string) {
  const onClose = vi.fn();
  const client = new QueryClient();
  render(
    <QueryClientProvider client={client}>
      <SplitPanel
        txn={txn(amount)}
        currency={currency}
        groups={GROUPS}
        onClose={onClose}
        onDone={() => {}}
      />
    </QueryClientProvider>,
  );
  const bar = document.querySelector(".split-bar") as HTMLElement;
  bar.getBoundingClientRect = () =>
    ({ left: 0, width: Math.abs(amount), top: 0, height: 40, right: 0, bottom: 0 }) as DOMRect;
  return { onClose };
}

const amounts = () =>
  Array.from(document.querySelectorAll<HTMLInputElement>(".split-part input[inputmode='decimal']")).map(
    (one) => one.value,
  );
const seams = () => screen.getAllByRole("slider");

async function sent() {
  vi.mocked(api.post).mockResolvedValue([]);
  fireEvent.click(screen.getByRole("button", { name: /^Split into/ }));
  await waitFor(() => expect(api.post).toHaveBeenCalled());
  return (vi.mocked(api.post).mock.calls[0][1] as { parts: unknown[] }).parts;
}

describe("the split bar", () => {
  it("drags a seam and sticks it at a half", async () => {
    open(-442, "EUR");
    const seam = seams()[0];
    fireEvent.keyDown(seam, { key: "ArrowLeft", shiftKey: true });
    expect(amounts()).toEqual(["2.11", "2.31"]);

    fireEvent.pointerDown(seam, { pointerId: 1 });
    fireEvent.pointerMove(seam, { pointerId: 1, clientX: 230 });
    expect(amounts()).toEqual(["2.21", "2.21"]);

    fireEvent.pointerMove(seam, { pointerId: 1, clientX: 380 });
    expect(amounts()).toEqual(["3.80", "0.62"]);
    expect(await sent()).toEqual([
      { amount: -380, category_id: "cat-food", memo: null },
      { amount: -62, category_id: null, memo: null },
    ]);
  });

  it("moves a seam one minor unit per arrow and ten with shift, without snapping", () => {
    open(-442, "EUR");
    const seam = seams()[0];
    fireEvent.keyDown(seam, { key: "ArrowRight" });
    expect(amounts()).toEqual(["2.22", "2.20"]);
    fireEvent.keyDown(seam, { key: "ArrowLeft", shiftKey: true });
    expect(amounts()).toEqual(["2.12", "2.30"]);
  });

  it("works in a currency with no minor digits", async () => {
    open(-9000, "JPY");
    fireEvent.click(screen.getByRole("button", { name: "Add a part" }));
    expect(amounts()).toEqual(["3000", "3000", "3000"]);
    const [, second] = seams();
    fireEvent.keyDown(second, { key: "ArrowRight", shiftKey: true });
    expect(amounts()).toEqual(["3000", "3010", "2990"]);
    expect((await sent()).map((one) => (one as { amount: number }).amount)).toEqual([
      -3000, -3010, -2990,
    ]);
  });

  it("keeps each part's category and memo through a drag", async () => {
    open(-442, "EUR");
    const selects = document.querySelectorAll<HTMLSelectElement>(".split-part select");
    fireEvent.change(selects[1], { target: { value: "cat-drink" } });
    const memos = document.querySelectorAll<HTMLInputElement>(".split-part input:not([inputmode])");
    fireEvent.change(memos[1], { target: { value: "Shared bottle" } });

    fireEvent.pointerDown(seams()[0], { pointerId: 1 });
    fireEvent.pointerMove(seams()[0], { pointerId: 1, clientX: 60 });

    expect(await sent()).toEqual([
      { amount: -60, category_id: "cat-food", memo: null },
      { amount: -382, category_id: "cat-drink", memo: "Shared bottle" },
    ]);
  });

  it("gives way to typed figures through the neighbour, and the parts still add up", () => {
    open(-442, "EUR");
    const [first] = document.querySelectorAll<HTMLInputElement>(".split-part input[inputmode='decimal']");
    fireEvent.change(first, { target: { value: "" } });
    fireEvent.change(first, { target: { value: "4" } });
    expect(amounts()).toEqual(["4", "0.42"]);
    expect(screen.getByText("It adds up")).toBeTruthy();
  });

  it("keeps a typed figure the neighbour cannot cover, and says what is over", () => {
    open(-442, "EUR");
    const [first] = document.querySelectorAll<HTMLInputElement>(".split-part input[inputmode='decimal']");
    fireEvent.change(first, { target: { value: "5" } });
    expect(amounts()).toEqual(["5", "2.21"]);
    expect(document.querySelector(".split-bar")).toBeNull();
    expect(screen.getByText("The bar comes back once the parts add up.")).toBeTruthy();
  });

  it("hands over to typed amounts at four parts", () => {
    open(-442, "EUR");
    fireEvent.click(screen.getByRole("button", { name: "Add a part" }));
    expect(seams()).toHaveLength(2);
    fireEvent.click(screen.getByRole("button", { name: "Add a part" }));
    expect(screen.queryAllByRole("slider")).toHaveLength(0);
    const inputs = document.querySelectorAll<HTMLInputElement>(".split-part input[inputmode='decimal']");
    fireEvent.change(inputs[0], { target: { value: "2.00" } });
    // No neighbour takes it up past three parts: the remainder shows instead.
    expect(amounts()).toEqual(["2.00", "1.10", "1.11", "1.11"]);
  });
});

describe("closing a split with work in it", () => {
  it("closes at once when nothing was changed", () => {
    const { onClose } = open(-442, "EUR");
    fireEvent.click(screen.getByRole("button", { name: "Not now" }));
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("asks first, and Keep editing keeps every figure", () => {
    const { onClose } = open(-442, "EUR");
    fireEvent.keyDown(seams()[0], { key: "ArrowRight", shiftKey: true });
    fireEvent.keyDown(document, { key: "Escape" });

    expect(screen.getByRole("dialog", { name: "Discard this split?" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Keep editing" }));
    expect(onClose).not.toHaveBeenCalled();
    expect(amounts()).toEqual(["2.31", "2.11"]);
  });

  it("Escape on the question closes the question, not the panel", () => {
    const { onClose } = open(-442, "EUR");
    fireEvent.keyDown(seams()[0], { key: "ArrowRight" });
    fireEvent.click(screen.getByRole("button", { name: "Not now" }));
    act(() => {
      fireEvent.keyDown(document, { key: "Escape" });
    });
    expect(screen.queryByRole("dialog", { name: "Discard this split?" })).toBeNull();
    expect(onClose).not.toHaveBeenCalled();
    expect(amounts()).toEqual(["2.22", "2.20"]);
  });

  it("Discard closes", () => {
    const { onClose } = open(-442, "EUR");
    fireEvent.keyDown(seams()[0], { key: "ArrowRight" });
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    fireEvent.click(screen.getByRole("button", { name: "Discard" }));
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});
