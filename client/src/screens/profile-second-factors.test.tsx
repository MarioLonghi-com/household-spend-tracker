// @vitest-environment jsdom

/**
 * A passkey and an authenticator as interchangeable second factors (#456).
 *
 * Two accounts: one whose second step is a passkey only, one with an
 * authenticator only. The fake server keeps state, so each test asserts what
 * the screen shows after the act and exactly what was sent.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";

vi.mock("../lib/api", () => ({
  api: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), put: vi.fn(), del: vi.fn(), upload: vi.fn() },
  ApiError: class ApiError extends Error {},
}));

import { api } from "../lib/api";
import type { Household, Passkey, User } from "../lib/types";
import { Profile } from "./Profile";

const JANE: User = { id: "u1", email: "jane@example.com", display_name: "Jane", role: "owner", disabled_at: null };
const HOUSE = { id: "h1", name: "Ours" } as Household;

const PHONE: Passkey = {
  id: "pk-phone",
  label: "iCloud Keychain",
  created_at: "2026-09-01T10:00:00",
  last_used_at: null,
  synced: true,
  rp_id: "spend.example.ts.net",
  usable_here: true,
  aaguid: null,
};

let status: { enrolled: boolean; locked_by_key: boolean; passkeys: number; cleared: boolean };
let held: Passkey[];

function server() {
  vi.mocked(api.get).mockImplementation(async (path: string) => {
    if (path === "/me/passkeys") return held.map((one) => ({ ...one }));
    if (path === "/session/passkey/state") return { available: true, reason: null, address: null };
    if (path === "/me/authenticator") return { ...status };
    if (path === "/me/recovery-codes") return { unused: 10 };
    if (path === "/me/keys") return [];
    throw new Error(`unexpected GET ${path}`);
  });
  vi.mocked(api.post).mockImplementation(async (path: string) => {
    if (path === "/me/step-up/options") return { challenge: "Y2g", rpId: "spend.example.ts.net" };
    if (path === "/me/step-up") return { token: "pk.grant", expires_at: "2026-11-12T12:05:00" };
    if (path === "/me/keys") return { key: { id: "k1", label: "filer" }, token: "agent-token" };
    if (path === "/me/recovery-codes") return { codes: ["aaaa", "bbbb"] };
    throw new Error(`unexpected POST ${path}`);
  });
  vi.mocked(api.del).mockImplementation(async (path: string) => {
    if (path === "/me/authenticator") {
      status = { ...status, enrolled: false };
      return { devices_revoked: 1 };
    }
    throw new Error(`unexpected DELETE ${path}`);
  });
}

function aBrowserWithPasskeys() {
  const get = vi.fn(async () => ({ toJSON: () => ({ id: "cred-1", type: "public-key" }) }));
  vi.stubGlobal("PublicKeyCredential", {
    parseCreationOptionsFromJSON: (json: unknown) => json,
    parseRequestOptionsFromJSON: (json: unknown) => json,
  });
  Object.defineProperty(window, "isSecureContext", { value: true, configurable: true });
  Object.defineProperty(navigator, "credentials", { value: { get }, configurable: true });
  return get;
}

function profile() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <Profile user={JANE} households={[HOUSE]} onClose={() => {}} />
    </QueryClientProvider>,
  );
}

const methodRow = (name: string) => screen.getByText(name, { selector: "strong" }).closest("li") as HTMLElement;

beforeEach(() => {
  held = [PHONE];
  window.localStorage.clear();
  window.sessionStorage.clear();
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  for (const fn of [api.get, api.post, api.patch, api.del]) vi.mocked(fn).mockReset();
  Object.defineProperty(window, "isSecureContext", { value: false, configurable: true });
  Object.defineProperty(navigator, "credentials", { value: undefined, configurable: true });
});

describe("a passkey-only account", () => {
  beforeEach(() => {
    status = { enrolled: false, locked_by_key: false, passkeys: 1, cleared: false };
    server();
  });

  it("reads 'Not set up', never 'Cleared', and offers to set one up", async () => {
    aBrowserWithPasskeys();
    profile();
    const row = await waitFor(() => methodRow("Authenticator"));
    await within(row).findByText("Not set up");
    expect(within(row).queryByText("Cleared")).toBeNull();
    expect(within(row).getByRole("button", { name: "Set up: Authenticator" })).toBeTruthy();
    expect(screen.getByText("You sign in with a passkey, or with your password and a passkey.")).toBeTruthy();
  });

  it("issues a key with the password and a passkey, and is never asked for a code", async () => {
    const prompt = aBrowserWithPasskeys();
    profile();
    fireEvent.click(await screen.findByRole("button", { name: "Give a program a key" }));
    fireEvent.change(await screen.findByLabelText("What is it for"), { target: { value: "filer" } });
    await screen.findByText(/asks for your password and your passkey/);
    expect(screen.queryByLabelText("The six digits from your authenticator")).toBeNull();
    fireEvent.change(screen.getByLabelText("Your password"), { target: { value: "a sufficiently long password" } });
    fireEvent.click(screen.getByRole("button", { name: "Create the key" }));

    await waitFor(() => expect(api.post).toHaveBeenCalledWith("/me/keys", expect.anything()));
    expect(prompt).toHaveBeenCalledTimes(1);
    expect(api.post).toHaveBeenCalledWith("/me/step-up", {
      password: "a sufficiently long password",
      credential: { id: "cred-1", type: "public-key" },
    });
    expect(vi.mocked(api.post).mock.calls.find(([path]) => path === "/me/keys")?.[1]).toMatchObject({
      step_up_token: "pk.grant",
    });
  });

  it("makes new recovery codes with a passkey step-up, not a code", async () => {
    aBrowserWithPasskeys();
    profile();
    const row = await waitFor(() => methodRow("Recovery codes"));
    fireEvent.click(within(row).getByRole("button", { name: "New codes: Recovery codes" }));
    fireEvent.click(within(row).getByRole("button", { name: "Make new codes" }));
    await within(row).findByText("Your browser asks for your passkey when you make the new codes.");
    expect(within(row).queryByLabelText("The six digits from your authenticator")).toBeNull();
    fireEvent.change(within(row).getByLabelText("Your password"), { target: { value: "a sufficiently long password" } });
    fireEvent.click(within(row).getByRole("button", { name: "Make new codes" }));
    await waitFor(() => expect(api.post).toHaveBeenCalledWith("/me/recovery-codes", { password: "", step_up_token: "pk.grant" }));
  });
});

describe("an account with both", () => {
  beforeEach(() => {
    status = { enrolled: true, locked_by_key: false, passkeys: 1, cleared: false };
    server();
  });

  it("removes the authenticator, and the row then reads 'Not set up'", async () => {
    aBrowserWithPasskeys();
    profile();
    const row = await waitFor(() => methodRow("Authenticator"));
    await within(row).findByText("Set up");
    fireEvent.click(within(row).getByRole("button", { name: "Set up again: Authenticator" }));
    fireEvent.click(within(row).getByRole("button", { name: "Remove the authenticator" }));
    fireEvent.click(within(row).getAllByRole("button", { name: "Remove the authenticator" })[0]);
    await waitFor(() => expect(api.del).toHaveBeenCalledWith("/me/authenticator"));
    await within(methodRow("Authenticator")).findByText("Not set up");
  });
});

describe("an authenticator-only account", () => {
  beforeEach(() => {
    held = [];
    status = { enrolled: true, locked_by_key: false, passkeys: 0, cleared: false };
    server();
  });

  it("is offered no way to remove its only second factor, and steps up with a code", async () => {
    profile();
    const row = await waitFor(() => methodRow("Authenticator"));
    await within(row).findByText("Set up");
    fireEvent.click(within(row).getByRole("button", { name: "Set up again: Authenticator" }));
    expect(within(row).queryByRole("button", { name: "Remove the authenticator" })).toBeNull();

    fireEvent.click(await screen.findByRole("button", { name: "Give a program a key" }));
    await screen.findByLabelText("The six digits from your authenticator");
    expect(screen.queryByText(/your password and your passkey/)).toBeNull();
  });
});
