// @vitest-environment jsdom

/**
 * The Receipts screen's list view, its notes and its multi-select.
 *
 * Every assertion is about a value that changed: the request a note actually
 * sent, the number the confirmation says out loud, the ids the delete carried,
 * and the order the rows come out in after a heading is clicked. Asserting
 * that a checkbox exists, or that a handler fired, would pass on a selection
 * that selected nothing -- which is the whole of what this is here to catch.
 *
 * The server is mocked at `lib/api` rather than at `fetch`: what matters here
 * is which call the screen makes with which body, and the routes themselves
 * are covered over HTTP in `tests/test_receipt_routes.py`.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";

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
import { Receipts } from "./Receipts";
import type { Household, Receipt } from "../lib/types";

// `globals` is off in vite.config.ts, so Testing Library has no `afterEach` of
// its own to hang cleanup on and every render would pile up in one document.
afterEach(cleanup);

const HOUSEHOLD: Household = {
  id: "house-1",
  name: "Ours",
  base_currency: "EUR",
  date_format: "YYYY-MM-DD",
  note: null,
  theme: "default",
  accent: null,
  receipts_keep_original: false,
  receipts_keep_original_forced: false,
  receipts_with_original: 0,
  colours: null,
};

/** Two of everything: two receipts, two days, two sizes, two notes. */
function receipt(id: string, over: Partial<Receipt> = {}): Receipt {
  return {
    id,
    household_id: HOUSEHOLD.id,
    transaction_id: null,
    content_sha256: `${id}-sha`,
    original_filename: `${id}.jpeg`,
    media_type: "image/jpeg",
    byte_size: 2_400_000,
    width: 2000,
    height: 2600,
    page_count: null,
    captured_at: "2026-09-19T18:42:07Z",
    captured_at_is_local: false,
    gps_lat: null,
    gps_lon: null,
    gps_accuracy_m: null,
    gps_bearing: null,
    camera: "Fictional Handset 9",
    exif: null,
    client_encoded: false,
    note: null,
    uploaded_by_id: "user-1",
    uploaded_by_name: "Jane",
    created_at: "2026-09-20T09:00:00Z",
    download_name: `2026-09-19-${id}.avif`,
    has_original: false,
    download_bytes: 300_000,
    also_on: 0,
    ...over,
  };
}

const MERCADONA = receipt("one", {
  captured_at: "2026-09-19T18:42:07Z",
  note: "Mercadona",
  download_bytes: 310_000,
});
const REPSOL = receipt("two", {
  captured_at: "2026-09-12T08:10:00Z",
  note: "Repsol",
  download_bytes: 120_000,
  transaction_id: "txn-2",
});

function show(rows: Receipt[] = [MERCADONA, REPSOL]) {
  vi.mocked(api.get).mockResolvedValue(rows as never);
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <Receipts household={HOUSEHOLD} />
    </QueryClientProvider>,
  );
}

/** The screen opens on thumbnails; every test here is about the other view. */
async function listView(rows?: Receipt[]) {
  show(rows);
  fireEvent.click(await screen.findByRole("button", { name: "List" }));
  return await screen.findAllByRole("row");
}

function noteBox(row: HTMLElement) {
  return within(row).getByRole("textbox", { name: "Note" });
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe("the receipts list", () => {
  it("shows the note as an editable column and saves what was typed", async () => {
    vi.mocked(api.patch).mockResolvedValue(MERCADONA as never);
    const rows = await listView();

    const box = noteBox(rows[1]) as HTMLInputElement;
    expect(box.value).toBe("Mercadona");

    fireEvent.change(box, { target: { value: "  Mercadona, the big shop  " } });
    fireEvent.blur(box);

    await waitFor(() => expect(api.patch).toHaveBeenCalledTimes(1));
    expect(api.patch).toHaveBeenCalledWith("/receipts/one", {
      note: "Mercadona, the big shop",
    });
    // The confirmation is the point of saving without a button.
    expect(await screen.findByText("Note saved.")).toBeTruthy();
  });

  it("empties a note through clear_note rather than an empty string", async () => {
    // `note: ""` and "there is no note" are different requests, and the API
    // takes the second as a flag of its own -- the same shape as `clear_memo`.
    vi.mocked(api.patch).mockResolvedValue(MERCADONA as never);
    const rows = await listView();

    const box = noteBox(rows[1]);
    fireEvent.change(box, { target: { value: "   " } });
    fireEvent.blur(box);

    await waitFor(() => expect(api.patch).toHaveBeenCalledTimes(1));
    expect(api.patch).toHaveBeenCalledWith("/receipts/one", { clear_note: true });
  });

  it("does not send anything when the note comes back to what it was", async () => {
    const rows = await listView();
    const box = noteBox(rows[1]);

    fireEvent.change(box, { target: { value: "Mercadon" } });
    fireEvent.change(box, { target: { value: "Mercadona" } });
    fireEvent.blur(box);

    expect(api.patch).not.toHaveBeenCalled();
  });

  it("deletes a selection as one call, after saying how many", async () => {
    vi.mocked(api.post).mockResolvedValue(["one", "two"] as never);
    const rows = await listView();

    fireEvent.click(within(rows[1]).getByRole("checkbox"));
    fireEvent.click(within(rows[2]).getByRole("checkbox"));

    fireEvent.click(screen.getByRole("button", { name: "Delete 2 receipts" }));

    // The count is in the confirmation, because "delete the selected receipts"
    // is a different question from "delete these two".
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText(/2 receipts/)).toBeTruthy();
    expect(api.post).not.toHaveBeenCalled();

    fireEvent.click(within(dialog).getByRole("button", { name: "Delete all 2" }));

    await waitFor(() => expect(api.post).toHaveBeenCalledTimes(1));
    expect(api.post).toHaveBeenCalledWith("/households/house-1/receipts/bulk-delete", {
      receipt_ids: ["one", "two"],
    });
    // One request, not one per receipt: that is what makes it one undo.
    expect(api.del).not.toHaveBeenCalled();
    expect(await screen.findByText(/2 receipts deleted/)).toBeTruthy();
  });

  it("selects only the rows on screen, and lets go of them on a filter change", async () => {
    await listView();

    fireEvent.click(screen.getByRole("checkbox", { name: "Select every receipt shown" }));
    expect(screen.getByRole("button", { name: "Delete 2 receipts" })).toBeTruthy();

    // A selection made on the inbox must not survive into a list it was never
    // shown against -- a confirmation that says six and removes rows nobody
    // can see is the failure this prevents.
    fireEvent.click(screen.getByRole("button", { name: "Matched" }));
    expect(screen.queryByRole("button", { name: /^Delete/ })).toBeNull();
  });

  it("picks receipts in the thumbnail view too, without nesting two buttons", async () => {
    // The card stopped being one big button when it grew a tick box, and a
    // button inside a button is neither: the checkbox and the way in are
    // siblings, and both have to work.
    show();
    const tick = await screen.findByRole("checkbox", {
      name: "Select the receipt from 2026-09-19",
    });
    fireEvent.click(tick);

    expect(screen.getByRole("button", { name: "Delete 1 receipt" })).toBeTruthy();
    expect(
      screen.getByRole("button", { name: "Open the receipt from 2026-09-19" }),
    ).toBeTruthy();
  });

  it("sorts at its headings, on the meaning rather than the glyph", async () => {
    const rows = await listView();
    const taken = (row: HTMLElement) => within(row).getAllByRole("cell")[2].textContent;

    // Newest first to start with.
    expect(taken(rows[1])).toContain("2026-09-19");

    fireEvent.click(screen.getByRole("button", { name: /^Taken/ }));
    const oldestFirst = await screen.findAllByRole("row");
    expect(taken(oldestFirst[1])).toContain("2026-09-12");

    // And a different column really reorders, rather than redrawing an arrow.
    // A column that is neither a date nor money starts ascending, so the
    // smaller file comes first -- Repsol's 117 KB against Mercadona's 303 KB.
    fireEvent.click(screen.getByRole("button", { name: /^Size/ }));
    const bySize = await screen.findAllByRole("row");
    expect((noteBox(bySize[1]) as HTMLInputElement).value).toBe("Repsol");

    fireEvent.click(screen.getByRole("button", { name: /^Size/ }));
    const biggestFirst = await screen.findAllByRole("row");
    expect((noteBox(biggestFirst[1]) as HTMLInputElement).value).toBe("Mercadona");
  });

  it("shows a larger copy while the pointer is on the thumbnail", async () => {
    const rows = await listView();
    const open = within(rows[1]).getByRole("button", {
      name: "Open the receipt from 2026-09-19",
    });

    // Nothing is fetched until the pointer arrives: a list of two hundred
    // receipts would otherwise pull two hundred display copies to draw forty
    // thumbnails.
    expect(document.querySelector("img[src='/api/receipts/one/display']")).toBeNull();
    fireEvent.mouseEnter(open);
    expect(document.querySelector("img[src='/api/receipts/one/display']")).toBeTruthy();
    fireEvent.mouseLeave(open);
    expect(document.querySelector("img[src='/api/receipts/one/display']")).toBeNull();
  });
});
