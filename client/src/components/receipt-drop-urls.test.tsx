// @vitest-environment jsdom

/**
 * ReceiptDrop gives back every preview URL it made (#195).
 *
 * `createObjectURL` pins the File in memory until it is revoked. The unmount
 * cleanup used to read the first render's (empty) job list, so it revoked
 * nothing. What is asserted is which URLs were revoked, not that a function
 * was called.
 */

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

import { api, ApiError } from "../lib/api";
import { ReceiptDrop } from "./Receipts";

let made = 0;
const revoked: string[] = [];

beforeEach(() => {
  made = 0;
  revoked.length = 0;
  URL.createObjectURL = vi.fn(() => `blob:${made++}`);
  URL.revokeObjectURL = vi.fn((url: string) => {
    revoked.push(url);
  });
});

afterEach(() => {
  cleanup();
  vi.mocked(api.upload).mockReset();
});

function drop(files: File[]) {
  fireEvent.drop(document.querySelector(".receipt-drop")!, { dataTransfer: { files } });
}

describe("ReceiptDrop's previews", () => {
  it("revokes a finished upload's preview at once and a failed one's on unmount", async () => {
    vi.mocked(api.upload).mockImplementation((_path, form) => {
      const file = (form as FormData).get("file") as File;
      return file.name === "bad.jpg"
        ? Promise.reject(new ApiError("that is not a picture", 422))
        : Promise.resolve({ receipt: { id: "r1" } } as never);
    });
    const onDone = vi.fn();
    const view = render(
      <ReceiptDrop target={{ householdId: "house-1" }} label="Add receipts" onDone={onDone} />,
    );

    drop([
      new File(["a"], "good.jpg", { type: "image/jpeg" }),
      new File(["b"], "bad.jpg", { type: "image/jpeg" }),
    ]);

    await screen.findByText("that is not a picture");
    await waitFor(() => expect(onDone).toHaveBeenCalledTimes(1));
    // The good one's row is gone, so its preview went with it; the failed row
    // is still drawn with its preview.
    expect(revoked).toEqual(["blob:0"]);
    expect(document.querySelector(".upload-queue img")?.getAttribute("src")).toBe("blob:1");

    view.unmount();
    expect(revoked.sort()).toEqual(["blob:0", "blob:1"]);
  });

  it("revokes every preview still waiting when it unmounts", () => {
    vi.mocked(api.upload).mockImplementation(() => new Promise(() => {}));
    const view = render(
      <ReceiptDrop target={{ householdId: "house-1" }} label="Add receipts" onDone={() => {}} />,
    );
    drop([
      new File(["a"], "one.jpg", { type: "image/jpeg" }),
      new File(["b"], "two.jpg", { type: "image/jpeg" }),
    ]);
    expect(revoked).toEqual([]);

    view.unmount();
    expect(revoked.sort()).toEqual(["blob:0", "blob:1"]);
  });
});
