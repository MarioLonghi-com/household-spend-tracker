// @vitest-environment jsdom

/**
 * What the account panel shows once and nowhere else (#445).
 *
 * New recovery codes -- whose making already stopped the old ones -- and a new
 * key's token live only in the panel. Escape, the cross, the backdrop and the
 * recovery row's own Close used to unmount them without a word. Each now asks
 * first, and the assertions are on what is still on screen and on whether the
 * panel's `onClose` ran.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

vi.mock("../lib/api", () => ({
  api: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), put: vi.fn(), del: vi.fn(), upload: vi.fn() },
  ApiError: class ApiError extends Error {},
}));

import { api } from "../lib/api";
import { Profile } from "./Profile";
import type { Household, User } from "../lib/types";

const CODES = ["0a1b2c3d4e", "5f6a7b8c9d"];
const TOKEN = "st_example_token_value";

const USER = { id: "u1", display_name: "Alex", email: "alex@example.test" } as User;
const HOUSES = [
  { id: "h-home", name: "Home" },
  { id: "h-away", name: "Away" },
] as Household[];

let ask: ReturnType<typeof vi.fn>;

beforeEach(() => {
  vi.mocked(api.get).mockImplementation(((path: string) => {
    if (path === "/me/recovery-codes") return Promise.resolve({ unused: 3 });
    if (path === "/me/authenticator") return Promise.resolve({ enrolled: true, locked_by_key: false });
    return Promise.resolve([]);
  }) as typeof api.get);
  vi.mocked(api.post).mockImplementation(((path: string) => {
    if (path === "/me/recovery-codes") return Promise.resolve({ codes: CODES });
    if (path === "/me/step-up") return Promise.resolve({ token: "grant" });
    if (path === "/me/keys") return Promise.resolve({ key: { label: "tidier" }, token: TOKEN });
    return Promise.reject(new Error(`unexpected ${path}`));
  }) as typeof api.post);
  ask = vi.fn(() => false);
  vi.stubGlobal("confirm", ask);
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  vi.unstubAllGlobals();
});

function mount() {
  const onClose = vi.fn();
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <Profile user={USER} households={HOUSES} onClose={onClose} />
    </QueryClientProvider>,
  );
  return onClose;
}

const escape = () => fireEvent.keyDown(document, { key: "Escape" });
const cross = () => fireEvent.click(screen.getByRole("button", { name: "Close" }));
function backdrop() {
  const outside = document.querySelector(".panel-backdrop")!;
  fireEvent.pointerDown(outside);
  fireEvent.click(outside);
}

async function makeCodes() {
  fireEvent.click(await screen.findByRole("button", { name: "New codes: Recovery codes" }));
  fireEvent.click(await screen.findByRole("button", { name: "Make new codes" }));
  fireEvent.change(screen.getByLabelText("Your password"), { target: { value: "pw" } });
  fireEvent.change(screen.getByLabelText(/six digits from your authenticator/), {
    target: { value: "123456" },
  });
  fireEvent.click(screen.getAllByRole("button", { name: "Make new codes" })[0]);
  await screen.findByText(CODES[0]);
}

describe("new recovery codes on the account panel", () => {
  it("stay on screen after Escape, the cross, the backdrop or the row's Close, when the answer is no", async () => {
    const onClose = mount();
    await makeCodes();

    escape();
    expect(ask).toHaveBeenCalledTimes(1);
    expect(ask.mock.calls[0][0]).toBe(
      "Close without storing the new recovery codes? They cannot be shown again.",
    );
    cross();
    expect(ask).toHaveBeenCalledTimes(2);
    // The backdrop does not close a panel holding them, and does not ask.
    backdrop();
    expect(ask).toHaveBeenCalledTimes(2);
    fireEvent.click(screen.getByRole("button", { name: "Close: Recovery codes" }));
    expect(ask).toHaveBeenCalledTimes(3);

    expect(onClose).not.toHaveBeenCalled();
    for (const one of CODES) expect(screen.getByText(one)).toBeTruthy();
  });

  it("close when the answer is yes", async () => {
    const onClose = mount();
    await makeCodes();
    ask.mockReturnValue(true);

    escape();
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("ask nothing once they are stored", async () => {
    const onClose = mount();
    await makeCodes();
    fireEvent.click(screen.getByLabelText(/I have stored these/));
    fireEvent.click(screen.getByRole("button", { name: "Done" }));
    await waitFor(() => expect(screen.queryByText(CODES[0])).toBeNull());

    escape();
    expect(ask).not.toHaveBeenCalled();
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("closing with nothing new on screen asks nothing", async () => {
    const onClose = mount();
    await screen.findByRole("button", { name: "New codes: Recovery codes" });
    escape();
    expect(ask).not.toHaveBeenCalled();
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});

describe("a new key's token on the account panel", () => {
  async function giveKey() {
    fireEvent.click(await screen.findByRole("button", { name: "Give a program a key" }));
    fireEvent.change(screen.getByLabelText("What is it for"), { target: { value: "tidier" } });
    const passwords = screen.getAllByLabelText("Your password");
    fireEvent.change(passwords[passwords.length - 1], { target: { value: "pw" } });
    const codes = screen.getAllByLabelText("The six digits from your authenticator");
    fireEvent.change(codes[codes.length - 1], { target: { value: "123456" } });
    fireEvent.click(screen.getByRole("button", { name: "Create the key" }));
    await screen.findByText(TOKEN);
  }

  it("stays on screen after Escape or the cross when the answer is no", async () => {
    const onClose = mount();
    await giveKey();

    escape();
    cross();
    expect(ask).toHaveBeenCalledTimes(2);
    expect(ask.mock.calls[0][0]).toBe("Close without copying the key? It cannot be shown again.");
    expect(onClose).not.toHaveBeenCalled();
    expect(screen.getByText(TOKEN)).toBeTruthy();
  });

  it("goes without a question once it is saved", async () => {
    const onClose = mount();
    await giveKey();
    fireEvent.click(screen.getByRole("button", { name: "I have saved it" }));

    escape();
    expect(ask).not.toHaveBeenCalled();
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});
