// @vitest-environment jsdom

/**
 * Passkeys on the sign-in screen (#121).
 *
 * Offered only where both the server's state answer and the browser say they
 * can work (#47, decision 4) -- otherwise the screen is exactly today's. A
 * passkey signs in at once, and the screen remembers which one for "this
 * device". After a recovery code, the screen says how many passkeys still work.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";

vi.mock("../lib/api", () => ({
  api: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), put: vi.fn(), del: vi.fn(), upload: vi.fn() },
  ApiError: class ApiError extends Error {},
}));

import { api } from "../lib/api";
import { thisDevicePasskey } from "../lib/passkeys";
import type { User } from "../lib/types";
import { SignIn } from "./SignIn";

const JANE: User = {
  id: "u1",
  email: "jane@example.com",
  display_name: "Jane",
  role: "owner",
  disabled_at: null,
};

const OPTIONS = { challenge: "Y2hhbGxlbmdl", rpId: "spend.example.ts.net" };
const ASSERTION = { id: "cred-1", type: "public-key", response: {} };

/** A browser with the WebAuthn JSON helpers, and a member who picks a passkey. */
function aBrowserWithPasskeys({ conditional = false, picks = true } = {}) {
  const get = vi.fn(async () =>
    picks
      ? { toJSON: () => ASSERTION }
      : Promise.reject(Object.assign(new Error("dismissed"), { name: "NotAllowedError" })),
  );
  vi.stubGlobal("PublicKeyCredential", {
    parseCreationOptionsFromJSON: (json: unknown) => json,
    parseRequestOptionsFromJSON: (json: unknown) => json,
    isConditionalMediationAvailable: async () => conditional,
  });
  Object.defineProperty(window, "isSecureContext", { value: true, configurable: true });
  Object.defineProperty(navigator, "credentials", { value: { get }, configurable: true });
  return get;
}

function theServerSays(available: boolean, signIn: Record<string, unknown> = {}) {
  vi.mocked(api.get).mockImplementation(async (path: string) =>
    path === "/session/passkey/state"
      ? { available, reason: available ? null : "wrong_host", address: "https://spend.example.ts.net" }
      : undefined,
  );
  vi.mocked(api.post).mockImplementation(async (path: string) => {
    if (path === "/session/passkey/options") return OPTIONS;
    if (path === "/session/passkey")
      return { authenticated: true, needs_code: false, user: JANE, passkey_id: "pk-7", ...signIn };
    if (path === "/session") return { authenticated: false, needs_code: true, user: null };
    return { authenticated: true, needs_code: false, user: JANE, keys_revoked: 0, ...signIn };
  });
}

beforeEach(() => window.localStorage.clear());

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  Object.defineProperty(window, "isSecureContext", { value: false, configurable: true });
  Object.defineProperty(navigator, "credentials", { value: undefined, configurable: true });
});

const email = () => screen.getByLabelText("Email") as HTMLInputElement;

describe("where passkeys cannot work", () => {
  it("the server saying no leaves the screen as it was", async () => {
    aBrowserWithPasskeys();
    theServerSays(false);
    render(<SignIn onDone={vi.fn()} />);
    await vi.waitFor(() => expect(api.get).toHaveBeenCalledWith("/session/passkey/state"));
    expect(screen.queryByRole("button", { name: "Sign in with a passkey" })).toBeNull();
    expect(email().autocomplete).toBe("username");
    expect(api.post).not.toHaveBeenCalled();
  });

  it("a browser without the JSON helpers leaves the screen as it was", async () => {
    theServerSays(true);
    render(<SignIn onDone={vi.fn()} />);
    await vi.waitFor(() => expect(api.get).toHaveBeenCalledWith("/session/passkey/state"));
    await Promise.resolve();
    expect(screen.queryByRole("button", { name: "Sign in with a passkey" })).toBeNull();
    expect(email().autocomplete).toBe("username");
  });
});

describe("where passkeys work", () => {
  it("the button signs in at once and remembers which passkey", async () => {
    const get = aBrowserWithPasskeys();
    theServerSays(true);
    const done = vi.fn();
    render(<SignIn onDone={done} />);

    fireEvent.click(await screen.findByRole("button", { name: "Sign in with a passkey" }));
    await vi.waitFor(() => expect(done).toHaveBeenCalledWith(JANE));
    expect(get).toHaveBeenCalledWith(expect.objectContaining({ publicKey: OPTIONS }));
    expect(api.post).toHaveBeenCalledWith("/session/passkey", { credential: ASSERTION });
    expect(thisDevicePasskey()).toBe("pk-7");
    expect(email().autocomplete).toBe("username webauthn");
  });

  it("the email field's suggestion signs in without the button", async () => {
    const get = aBrowserWithPasskeys({ conditional: true });
    theServerSays(true);
    const done = vi.fn();
    render(<SignIn onDone={done} />);
    await vi.waitFor(() => expect(done).toHaveBeenCalledWith(JANE));
    expect(get).toHaveBeenCalledWith(expect.objectContaining({ mediation: "conditional" }));
    expect(api.post).toHaveBeenCalledWith("/session/passkey", { credential: ASSERTION });
  });

  it("a dismissed prompt is not an error and signs nobody in", async () => {
    aBrowserWithPasskeys({ picks: false });
    theServerSays(true);
    const done = vi.fn();
    render(<SignIn onDone={done} />);
    fireEvent.click(await screen.findByRole("button", { name: "Sign in with a passkey" }));
    await vi.waitFor(() => expect(api.post).toHaveBeenCalledWith("/session/passkey/options", {}));
    await Promise.resolve();
    expect(done).not.toHaveBeenCalled();
    expect(api.post).not.toHaveBeenCalledWith("/session/passkey", expect.anything());
    expect(screen.queryByRole("alert")).toBeNull();
    expect(thisDevicePasskey()).toBeNull();
  });
});

describe("after a recovery code", () => {
  it("says how many passkeys still work, and where to remove one", async () => {
    theServerSays(false, { passkeys_live: 2 });
    const done = vi.fn();
    render(<SignIn onDone={done} />);
    fireEvent.change(email(), { target: { value: JANE.email } });
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "a long password" } });
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
    fireEvent.click(await screen.findByRole("button", { name: /use a recovery code/ }));
    fireEvent.change(screen.getByLabelText("Recovery code"), { target: { value: "abcd-efgh-ij" } });
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));

    const notice = await screen.findByRole("status");
    expect(notice.textContent).toContain("You still have 2 passkeys");
    expect(notice.textContent).toContain("Sign-in methods");
    expect(notice.textContent).not.toContain("agent key");
    expect(done).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));
    expect(done).toHaveBeenCalledWith(JANE);
  });
});
